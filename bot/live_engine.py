"""Live trading engine: live DB schema, read-only paper access, signal reader,
sizing, risk checks, live state. Pure logic only — no SDK, no network.

Invariant I1 (paper isolation): the paper DBs are opened via paper_ro_conn()
in SQLite `mode=ro`; a write raises OperationalError. No other live code path
holds a writable handle to either paper file. Enforced again by
test_paper_db_zero_new_rows_after_live_tick in test_live_isolation.py.

Live is mechanically downstream of Bot A's paper scan (I5): same probabilities,
same side, no recomputation drift. The live process only re-fetches a fresh
book, re-verifies edge at live thresholds, sizes for $200, and (in
live_executor) posts real orders.
"""
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

from config import (
    BOT_DIR,
    LIVE_BANKROLL,
    LIVE_CONSECUTIVE_LOSS_HALT,
    LIVE_DAILY_LOSS_HALT_FRAC,
    LIVE_DB_PATH,
    LIVE_MAX_OPEN_POSITIONS,
    LIVE_MAX_POSITIONS_PER_REGION_DAY,
    LIVE_MIN_EDGE,
    LIVE_PER_TRADE_CAP_ABS,
    LIVE_PER_TRADE_CAP_FRAC,
    LIVE_PRICE_BAND,
    LIVE_SIGNAL_MAX_AGE_MIN,
)

HKT = timezone(timedelta(hours=8))

# Live is the live arm of Bot A (the baseline weather bot, not climatology B).
# Read A's paper DB explicitly, regardless of BOT_INSTANCE at runtime.
PAPER_DB_PATH = str(BOT_DIR / "polymarket_bot.db")


# ── live DB handle (writable) ──────────────────────────────────────────────
def get_live_db():
    conn = sqlite3.connect(LIVE_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_live_db():
    conn = get_live_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS live_ticks (
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, job TEXT,
          note TEXT, detail_json TEXT
        );
        CREATE TABLE IF NOT EXISTS live_orders (
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,
          candidate_id INTEGER, market_id TEXT, condition_id TEXT, exec_token_id TEXT,
          city TEXT, market_date TEXT, bucket_key TEXT,
          signal_side TEXT, exec_side TEXT,
          price REAL, size REAL, notional REAL,
          edge_at_exec REAL, kelly_fraction REAL,
          neg_risk INTEGER, dry_run INTEGER,
          clob_order_id TEXT, status TEXT, raw_json TEXT
        );
        CREATE TABLE IF NOT EXISTS live_fills (
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, order_id INTEGER,
          clob_trade_id TEXT, market_id TEXT, exec_token_id TEXT,
          side TEXT, price REAL, size REAL, fee REAL, fill_ts TEXT, raw_json TEXT
        );
        CREATE TABLE IF NOT EXISTS live_settlements (
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, market_id TEXT, condition_id TEXT,
          city TEXT, date TEXT, bucket_key TEXT, resolved_yes INTEGER, pnl REAL, raw_json TEXT
        );
        CREATE TABLE IF NOT EXISTS live_balances (
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, usdc REAL, matic REAL, source TEXT
        );
        CREATE TABLE IF NOT EXISTS live_halts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, reason TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_lo_status ON live_orders(status);
        CREATE INDEX IF NOT EXISTS idx_lo_market ON live_orders(market_id);
        CREATE INDEX IF NOT EXISTS idx_lf_market ON live_fills(market_id);
        CREATE INDEX IF NOT EXISTS idx_ls_market ON live_settlements(market_id);
        """
    )
    conn.commit()
    conn.close()


def live_meta_get(conn, k, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def live_meta_set(conn, k, v):
    conn.execute(
        "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (k, str(v)),
    )


def log_tick(conn, job, note, detail=None):
    conn.execute(
        "INSERT INTO live_ticks(ts, job, note, detail_json) VALUES(?,?,?,?)",
        (_now_iso(), job, note, json.dumps(detail or {}, default=str)),
    )


# ── read-only paper access (I1: enforced by SQLite mode=ro) ────────────────
def paper_ro_conn():
    """Open Bot A's paper DB read-only. Any write raises sqlite3.OperationalError."""
    uri = Path(PAPER_DB_PATH).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── signal reader (Phase 1) ────────────────────────────────────────────────
@dataclass
class LiveSignal:
    candidate_id: int
    market_id: str
    condition_id: str
    side: str            # 'buy'/'sell' in YES-space (paper semantics)
    p_model: float
    edge_after_costs: float
    effective_price: float   # YES-space walked price from paper candidate
    city: str
    market_date: str
    bucket_key: str
    yes_token_id: str
    fee_rate: float
    fees_enabled: bool
    neg_risk: bool
    ts: str             # candidate insertion ts (UTC iso)


def read_new_signals(paper_conn, live_conn):
    """Read candidates past the cursor, joined to the latest snapshot per
    market_id. Filters: edge >= LIVE_MIN_EDGE, ts within LIVE_SIGNAL_MAX_AGE_MIN.
    Always advances the cursor to the max id seen (skipped or not) so a missed
    tick is never replayed from the backlog. Returns (signals, evaluated) where
    evaluated = candidates fetched past the cursor (pre-filter), so callers can
    surface 'the filter ran over N' even when signals is empty."""
    cursor = int(live_meta_get(live_conn, "candidate_cursor", "0") or "0")
    rows = paper_conn.execute(
        """
        SELECT c.id AS candidate_id, c.market_id, c.condition_id, c.side,
               c.p_model, c.edge_after_costs, c.effective_price, c.ts,
               c.bucket_key,
               s.city, s.market_date, s.yes_token_id, s.neg_risk,
               s.fee_rate, s.fees_enabled
        FROM candidates c
        LEFT JOIN snapshots s
          ON s.id = (SELECT MAX(id) FROM snapshots WHERE market_id = c.market_id)
        WHERE c.id > ?
        ORDER BY c.id
        """,
        (cursor,),
    ).fetchall()
    signals = []
    max_id = cursor
    now = datetime.now(timezone.utc)
    for r in rows:
        if r["candidate_id"] is not None and r["candidate_id"] > max_id:
            max_id = r["candidate_id"]
        if (r["edge_after_costs"] or 0.0) < LIVE_MIN_EDGE:
            log_tick(live_conn, "weather-live", "skip:low_edge",
                     {"candidate_id": r["candidate_id"], "edge": r["edge_after_costs"]})
            continue
        age_min = _age_min(r["ts"], now)
        if age_min is None or age_min > LIVE_SIGNAL_MAX_AGE_MIN:
            log_tick(live_conn, "weather-live", "skip:stale",
                     {"candidate_id": r["candidate_id"], "age_min": age_min})
            continue
        if not r["yes_token_id"]:
            log_tick(live_conn, "weather-live", "skip:no_token",
                     {"candidate_id": r["candidate_id"]})
            continue
        signals.append(LiveSignal(
            candidate_id=r["candidate_id"], market_id=r["market_id"],
            condition_id=r["condition_id"], side=r["side"], p_model=r["p_model"],
            edge_after_costs=r["edge_after_costs"], effective_price=r["effective_price"],
            city=r["city"] or "", market_date=r["market_date"] or "",
            bucket_key=r["bucket_key"] or "", yes_token_id=r["yes_token_id"],
            fee_rate=float(r["fee_rate"] or 0.0),
            fees_enabled=bool(r["fees_enabled"]), neg_risk=bool(r["neg_risk"]),
            ts=r["ts"],
        ))
    live_meta_set(live_conn, "candidate_cursor", str(max_id))
    return signals, len(rows)


def _age_min(ts_str, now):
    if not ts_str:
        return None
    try:
        t = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (now - t).total_seconds() / 60.0


# ── book walk + sizing + risk (Phase 2) ───────────────────────────────────
def walk_book_fill(levels, target_shares):
    """Walk a side of the book, return (avg_fill_price, filled_shares) or
    (None, 0.0) if the book is empty. Mirrors engine._walk_book but also
    returns the fillable size (so the executor can size down on thin books)."""
    filled = 0.0
    cost = 0.0
    for lvl in levels:
        if filled >= target_shares:
            break
        take = min(lvl["size"], target_shares - filled)
        cost += lvl["price"] * take
        filled += take
    if filled <= 0:
        return None, 0.0
    return cost / filled, filled


@dataclass
class LiveOrderSpec:
    signal: LiveSignal
    exec_token_id: str        # YES for buy, NO for sell
    exec_side: str            # always 'BUY'
    price: float              # limit price in exec-token space (yes-ask / no-ask)
    size: float               # exec shares to buy
    notional: float           # price * size
    edge_at_exec: float
    kelly_fraction: float


def size_signal(signal, walked_exec_price):
    """Quarter-Kelly sizing identical to engine.propose, mapped to the
    execution token. For a sell, the execution is BUY NO at p_no; Kelly is
    computed in YES-space (q = 1 - p_no) for mechanical fidelity to paper,
    then the notional is spent buying NO shares.

    Returns (notional, shares, edge_at_exec) or (None, None, None) if the
    Kelly stake is <= 0 or below CLOB minimums (never sized up to a minimum).
    """
    p = signal.p_model
    if signal.side == "buy":
        q = walked_exec_price          # yes-ask
        if q <= 0.0 or q >= 1.0:
            return None, None, None
        f = (p - q) / (1 - q)
        edge_at_exec = p - q - _fee(signal, p)
    else:
        p_no = walked_exec_price       # no-ask
        if p_no <= 0.0 or p_no >= 1.0:
            return None, None, None
        q = 1.0 - p_no                 # YES-space equivalent (paper semantics)
        f = (q - p) / q
        edge_at_exec = (1.0 - p) - p_no - _fee(signal, p)
    f = max(0.0, f) / 4.0
    notional = min(f * LIVE_BANKROLL,
                   LIVE_PER_TRADE_CAP_FRAC * LIVE_BANKROLL,
                   LIVE_PER_TRADE_CAP_ABS)
    if notional <= 0.0:
        return None, None, None
    shares = notional / walked_exec_price   # exec-token shares
    if notional < 1.0:                       # $1 CLOB floor
        return None, None, None
    return notional, shares, edge_at_exec


def _fee(signal, p):
    return signal.fee_rate * p * (1.0 - p) if signal.fees_enabled else 0.0


@dataclass
class LiveState:
    bankroll: float
    realized_pnl_today: float
    consecutive_losses: int
    open_positions: list = field(default_factory=list)  # dicts: market_id, city, market_date


def load_live_state(conn):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pnl_today = float(conn.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM live_settlements WHERE substr(ts,1,10)=?",
        (today,),
    ).fetchone()[0] or 0.0)
    rows = conn.execute(
        "SELECT pnl FROM live_settlements ORDER BY id DESC LIMIT 20"
    ).fetchall()
    losses = 0
    for r in rows:
        if (r["pnl"] or 0) < 0:
            losses += 1
        else:
            break
    open_pos = [dict(r) for r in conn.execute(
        """SELECT market_id, city, market_date FROM live_orders
           WHERE status IN ('posted','open','filled','partial')"""
    ).fetchall()]
    return LiveState(bankroll=LIVE_BANKROLL, realized_pnl_today=pnl_today,
                     consecutive_losses=losses, open_positions=open_pos)


@dataclass
class LiveRiskVerdict:
    approved: bool
    reason: str


def live_risk_check(order_spec, state):
    """Mirror engine.risk_check against live constants + HALT_LIVE."""
    if os.path.exists(_halt_live_path()):
        return LiveRiskVerdict(False, "HALT_LIVE file present")
    price = order_spec.price
    if not (LIVE_PRICE_BAND[0] <= price <= LIVE_PRICE_BAND[1]):
        return LiveRiskVerdict(False, f"price {price} outside band {LIVE_PRICE_BAND}")
    notional = order_spec.price * order_spec.size
    cap = min(LIVE_PER_TRADE_CAP_FRAC * state.bankroll, LIVE_PER_TRADE_CAP_ABS)
    if notional > cap + 1e-9:
        return LiveRiskVerdict(False, f"per-trade cap exceeded {notional:.2f}>{cap:.2f}")
    if state.realized_pnl_today <= -LIVE_DAILY_LOSS_HALT_FRAC * state.bankroll:
        return LiveRiskVerdict(False, "daily loss halt")
    if state.consecutive_losses >= LIVE_CONSECUTIVE_LOSS_HALT:
        return LiveRiskVerdict(False, "consecutive loss halt")
    if len(state.open_positions) >= LIVE_MAX_OPEN_POSITIONS:
        return LiveRiskVerdict(False, f"max open positions {LIVE_MAX_OPEN_POSITIONS}")
    sig = order_spec.signal
    region_count = sum(
        1 for p in state.open_positions
        if p.get("city") == sig.city and p.get("market_date") == sig.market_date
    )
    if region_count >= LIVE_MAX_POSITIONS_PER_REGION_DAY:
        return LiveRiskVerdict(False, f"region-day cap {region_count}")
    if any(p["market_id"] == sig.market_id for p in state.open_positions):
        return LiveRiskVerdict(False, "one position per market")
    return LiveRiskVerdict(True, "approved")


def _halt_live_path():
    from config import HALT_LIVE_FILE
    return HALT_LIVE_FILE


def record_halt(conn, reason):
    conn.execute(
        "INSERT INTO live_halts(ts, reason) VALUES(?,?)",
        (_now_iso(), reason),
    )
    conn.commit()
