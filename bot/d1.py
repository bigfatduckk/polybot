"""D1 — maker-execution FLB shadow arm.

Pre-reg: D1 Maker-Execution FLB Shadow Pre-Registration 2026-07-26.md.
Posts resting SELL-YES maker asks (post_only, GTD, 24h expiry) on the SAME FLB
sell candidates the frozen P6 taker path produces. Reads pm_candidates from
Bot A's paper DB read-only (live_engine.paper_ro_conn); posts to the CLOB via
its OWN client (d1.get_client) reading D1-ONLY env names — D1 structurally
cannot sign with the shared/live-arm POLY_PRIVATE_KEY (the 0x8c9d key exposed
to AI context since 07-24). env HTTPS_PROXY at go-live. Own DB
(polymarket_bot_d1.db), own HALT_D1 file, own D1_DRY_RUN env (default ON =
sign-but-not-post, independent of LIVE_DRY_RUN).

Does NOT touch the frozen taker path
(edge_engine/settle/edges/flb/engine/markets/config/regrade_fills/run_scan/
analyze_edges/build_flb_calib). config.py is left byte-identical to c44aede —
D1 constants live here. Does NOT import live_executor (the live-arm key module)
— D1 builds its own ClobClient from POLY_PRIVATE_KEY_D1, so the shared key can
never reach a D1 order even if both are set in .env.

Micro size: size = venue min_order_size (can't go lower), int token units;
skip if size*(1-ask) > D1_CAP ($25 hard cap). Purpose: measure fill rate +
adverse selection, not earn money. Maker fee = 0, so settle._fill_pnl is called
with fee_rate=0 (correct by construction); the maker rebate is added as a
separate settlement term (sole-maker upper bound: pool_pct * taker_fee_equivalent).
"""
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
import live_engine as le
import markets
import settle
from config import BOT_DIR, CLOB_BASE, GAMMA_BASE, tls_verify

# ── D1 config (here, not config.py — keeps the frozen P6 path byte-identical) ─
D1_DB_PATH = str(BOT_DIR / "polymarket_bot_d1.db")
HALT_D1_FILE = str(BOT_DIR / "HALT_D1")
D1_DRY_RUN_ENV = "D1_DRY_RUN"
# D1-ONLY signer env names. Distinct from the live arm's POLY_PRIVATE_KEY so D1
# cannot accidentally sign with the 0x8c9d key exposed to AI context since 07-24.
# The fresh D1 signer enters the VPS only via .env over SSH, never AI context.
D1_KEY_ENV = "POLY_PRIVATE_KEY_D1"
D1_FUNDER_ENV = "POLY_FUNDER_D1"
D1_SIG_TYPE_ENV = "POLY_SIG_TYPE_D1"
D1_CAP = 25.0           # hard capital cap: max loss per position = size*(1-ask)
D1_WINDOW_HOURS = 24    # resting maker order expiry (native GTD, no cancel cron)
D1_JOB = "d1"           # log tag
# Rebate pool size as fraction of taker fees, by category (verified primary-source
# docs.polymarket.com/programs/maker-rebates). 0.25 is the majority default and
# the MAX — using it for all markets overstates crypto(0.20)/sports(0.15) rebate,
# so the stored rebate is a conservative upper bound for those classes. The
# rebate is a secondary term; zero maker fee is the dominant D1 lever.
REBATE_POOL_PCT_DEFAULT = 0.25


class D1KeyMissing(Exception):
    pass


def is_dry_run():
    """Dry-run defaults ON; go-live requires explicit D1_DRY_RUN=0.
    Independent of LIVE_DRY_RUN so D1 can run while the live arm stays HALTED."""
    return os.environ.get(D1_DRY_RUN_ENV, "1") != "0"


def get_client():
    """Build a ClobClient from D1's OWN env names. Raises D1KeyMissing if
    POLY_PRIVATE_KEY_D1 / POLY_FUNDER_D1 are absent — even if the shared
    POLY_PRIVATE_KEY is set. This is the structural isolation: D1 cannot fall
    back to the live-arm key, so the 07-24-exposed 0x8c9d key can never sign a
    D1 order. py_clob_client_v2 is imported lazily so a missing SDK never
    breaks config/live_engine/paper crons."""
    key = os.environ.get(D1_KEY_ENV)
    funder = os.environ.get(D1_FUNDER_ENV)
    if not key:
        raise D1KeyMissing("POLY_PRIVATE_KEY_D1 not set in env")
    if not funder:
        raise D1KeyMissing("POLY_FUNDER_D1 not set in env")
    try:
        sig = int(os.environ.get(D1_SIG_TYPE_ENV, "2"))
    except ValueError:
        sig = 2
    from py_clob_client_v2 import ClobClient
    client = ClobClient(
        host=CLOB_BASE, chain_id=137, key=key,
        signature_type=sig, funder=funder,
    )
    try:
        creds = client.derive_api_key()
    except Exception:
        creds = client.create_api_key()
    client.set_api_creds(creds)
    return client


def resolve_no_token(market_id):
    """Gamma /markets?id= → clobTokenIds[1] (the NO token). Read-only HTTP.
    Mirrors live_executor.resolve_no_token; replicated here so D1 never
    imports the live-arm key module (structural isolation preserved)."""
    try:
        with httpx.Client(timeout=30, headers={"User-Agent": "MarcusVaultBot/1.0"},
                          verify=tls_verify()) as c:
            r = c.get(f"{GAMMA_BASE}/markets", params={"id": market_id})
            mkts = r.json() if r.status_code == 200 else []
    except Exception:
        return None
    if not mkts:
        return None
    try:
        tokens = json.loads(mkts[0].get("clobTokenIds") or "[]")
    except (TypeError, ValueError):
        return None
    return tokens[1] if len(tokens) >= 2 else None


def _halt_d1_path():
    return HALT_D1_FILE


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── D1 DB (own file, own tables) ────────────────────────────────────────────
def get_d1_db():
    conn = sqlite3.connect(D1_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_d1_db():
    conn = get_d1_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS d1_ticks (
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, job TEXT,
          note TEXT, detail_json TEXT
        );
        CREATE TABLE IF NOT EXISTS d1_orders (
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,
          candidate_id INTEGER, market_id TEXT, condition_id TEXT,
          yes_token_id TEXT, no_token_id TEXT, signal_side TEXT, exec_side TEXT,
          ask_price REAL, size REAL, notional REAL, max_loss REAL,
          edge_at_scan REAL, p_model REAL, scan_mid REAL, gap REAL,
          tick_size REAL, neg_risk INTEGER, fee_rate REAL, expiration INTEGER,
          dry_run INTEGER, clob_order_id TEXT, status TEXT, raw_json TEXT
        );
        CREATE TABLE IF NOT EXISTS d1_fills (
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, order_id INTEGER,
          clob_trade_id TEXT, market_id TEXT, yes_token_id TEXT,
          side TEXT, price REAL, size REAL, fee REAL, fill_ts TEXT, raw_json TEXT
        );
        CREATE TABLE IF NOT EXISTS d1_settlements (
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, market_id TEXT,
          condition_id TEXT, candidate_id INTEGER, resolved_yes INTEGER,
          pnl REAL, rebate REAL, rebate_accrued_unpaid REAL, raw_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_d1o_status ON d1_orders(status);
        CREATE INDEX IF NOT EXISTS idx_d1o_market ON d1_orders(market_id);
        CREATE INDEX IF NOT EXISTS idx_d1f_market ON d1_fills(market_id);
        CREATE INDEX IF NOT EXISTS idx_d1s_market ON d1_settlements(market_id);
        CREATE INDEX IF NOT EXISTS idx_d1o_candidate ON d1_orders(candidate_id);
        """
    )
    # Migration: D1 posts BUY-NO (SELL-YES infeasible) — store the NO token.
    # Idempotent: ALTER ADD COLUMN errors once the column exists (existing DBs).
    try:
        conn.execute("ALTER TABLE d1_orders ADD COLUMN no_token_id TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def meta_get(conn, k, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def meta_set(conn, k, v):
    conn.execute(
        "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (k, str(v)),
    )


def log_tick(conn, note, detail=None):
    conn.execute(
        "INSERT INTO d1_ticks(ts, job, note, detail_json) VALUES(?,?,?,?)",
        (_now_iso(), D1_JOB, note, json.dumps(detail or {}, default=str)),
    )


# ── signal reader: FLB sell candidates from Bot A's paper DB (read-only) ─────
@dataclass
class D1Signal:
    candidate_id: int
    market_id: str
    condition_id: str
    side: str            # 'sell' (YES-space; D1 scope is sell-YES longshots)
    p_model: float
    edge_after_costs: float
    effective_price: float   # walked bid the taker path would cross
    scan_mid: float          # (best_bid+best_ask)/2 at scan, from candidate meta
    gap: float               # |p_model - effective_price|
    yes_token_id: str
    tick_size: float
    min_order_size: float
    fee_rate: float
    fees_enabled: bool
    neg_risk: bool
    ts: str


def read_new_signals(paper_conn, d1_conn):
    """Read FLB sell candidates past the cursor, joined to the latest FLB
    snapshot per market. Always advances the cursor to the max id seen (skipped
    or not) so a missed tick is never replayed. D1 does NOT re-scan or re-gate
    — the candidate set is definitionally identical to P6's by construction."""
    cursor = meta_get(d1_conn, "candidate_cursor")
    if cursor is None:
        # First run: start from the current max candidate id so D1 does NOT replay
        # historical pm_candidates (whose markets/books are long dead). D1 accrues
        # only signals that arrive AFTER it is enabled — "accrual beside P6".
        max_id = paper_conn.execute(
            "SELECT COALESCE(MAX(id),0) FROM pm_candidates WHERE edge='flb'"
        ).fetchone()[0]
        meta_set(d1_conn, "candidate_cursor", str(max_id))
        return [], 0
    cursor = int(cursor)
    rows = paper_conn.execute(
        """
        SELECT c.id AS cid, c.ts, c.market_id, c.side, c.p_model,
               c.edge_after_costs, c.effective_price, c.meta_json,
               s.yes_token_id, s.tick_size, s.min_order_size, s.fee_rate,
               s.fees_enabled, s.neg_risk, s.condition_id
        FROM pm_candidates c
        LEFT JOIN pm_snapshots s
          ON s.id = (SELECT MAX(id) FROM pm_snapshots
                     WHERE market_id = c.market_id AND edge = 'flb')
        WHERE c.edge = 'flb' AND c.side = 'sell' AND c.id > ?
        ORDER BY c.id
        """,
        (cursor,),
    ).fetchall()
    signals = []
    max_id = cursor
    for r in rows:
        if r["cid"] is not None and r["cid"] > max_id:
            max_id = r["cid"]
        if not r["yes_token_id"]:
            log_tick(d1_conn, "skip:no_token", {"candidate_id": r["cid"]})
            continue
        try:
            meta = json.loads(r["meta_json"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        bb = float(meta.get("best_bid") or 0.0)
        ba = float(meta.get("best_ask") or 1.0)
        scan_mid = (bb + ba) / 2.0
        gap = abs(float(r["p_model"] or 0.0) - float(r["effective_price"] or 0.0))
        signals.append(D1Signal(
            candidate_id=r["cid"], market_id=r["market_id"],
            condition_id=r["condition_id"] or "", side=r["side"],
            p_model=float(r["p_model"] or 0.0),
            edge_after_costs=float(r["edge_after_costs"] or 0.0),
            effective_price=float(r["effective_price"] or 0.0),
            scan_mid=scan_mid, gap=gap, yes_token_id=r["yes_token_id"],
            tick_size=float(r["tick_size"] or 0.001),
            min_order_size=float(r["min_order_size"] or 5.0),
            fee_rate=float(r["fee_rate"] or 0.0),
            fees_enabled=bool(r["fees_enabled"]), neg_risk=bool(r["neg_risk"]),
            ts=r["ts"],
        ))
    meta_set(d1_conn, "candidate_cursor", str(max_id))
    return signals, len(rows)


# ── order prep: resting SELL-YES maker ask at scan_mid + gap/2 ──────────────
@dataclass
class D1OrderSpec:
    signal: D1Signal
    yes_token_id: str
    ask_price: float       # YES-space maker level (scan_mid + gap/2); posted as NO bid at 1-ask_price
    size: float            # int token units, >= venue min_order_size
    notional: float        # ask_price * size (YES-space)
    max_loss: float        # size * (1 - ask_price) = size * p_no (BUY-NO cost; loses all if YES wins)
    expiration: int        # unix seconds
    no_token_id: str = None  # NO token; resolved in submit (Gamma). D1 posts BUY-NO (SELL-YES is infeasible — CLOB needs YES inventory D1 does not hold).


def _round_to_tick(price, tick):
    tick = float(tick) if tick else 0.001
    return round(price / tick) * tick if tick > 0 else price


def prepare_order(signal):
    """Build a resting SELL-YES maker ask. Ask level = scan_mid + gap/2 (locked
    pre-reg). Size = venue min_order_size (can't go lower); skip if max_loss >
    D1_CAP. Returns D1OrderSpec or None (skip reason logged by caller)."""
    ask = _round_to_tick(signal.scan_mid + signal.gap / 2.0, signal.tick_size)
    # A maker SELL ask must sit above the best bid to rest (not cross). post_only
    # enforces this at the CLOB; here we only guard the degenerate ask<=0 / >=1.
    if ask <= 0.0 or ask >= 1.0:
        return None
    size = int(max(signal.min_order_size, 1.0))
    max_loss = size * (1.0 - ask)
    if max_loss > D1_CAP:
        return None
    notional = ask * size
    now = int(datetime.now(timezone.utc).timestamp())
    return D1OrderSpec(
        signal=signal, yes_token_id=signal.yes_token_id, ask_price=ask,
        size=float(size), notional=notional, max_loss=max_loss,
        expiration=now + D1_WINDOW_HOURS * 3600,
    )


_DO_COLS = """ts, candidate_id, market_id, condition_id, yes_token_id, no_token_id,
  signal_side, exec_side, ask_price, size, notional, max_loss, edge_at_scan,
  p_model, scan_mid, gap, tick_size, neg_risk, fee_rate, expiration, dry_run,
  clob_order_id, status, raw_json"""
_DO_PH = ",".join(["?"] * 24)


def _store_order(conn, spec, status, clob_id, raw):
    s = spec.signal
    conn.execute(
        f"INSERT INTO d1_orders({_DO_COLS}) VALUES({_DO_PH})",
        (_now_iso(), s.candidate_id, s.market_id, s.condition_id, spec.yes_token_id,
         spec.no_token_id,
         s.side, "BUY", spec.ask_price, spec.size, spec.notional, spec.max_loss,
         s.edge_after_costs, s.p_model, s.scan_mid, s.gap, s.tick_size,
         int(s.neg_risk), s.fee_rate, spec.expiration, int(is_dry_run()),
         clob_id or "", status, json.dumps(raw, default=str)),
    )
    conn.commit()


def submit(spec, client, conn, dry_run):
    """Sign the BUY-NO GTD maker bid always; post only if not dry_run.

    D1 posts BUY-NO (not SELL-YES): the CLOB requires YES-token inventory for a
    SELL-YES, which D1 does not hold; a SELL-YES maker post is rejected
    'balance: 0' by construction. BUY-NO is the economically identical
    short-YES / long-NO position using USDC collateral (matches the taker path:
    live_executor maps a sell signal to BUY NO). Posted NO bid = 1 - ask_price
    (YES-space level scan_mid + gap/2); fills are recorded in YES-space by
    reconcile_fills (side='sell', price = 1 - p_no) so settle/rebate are
    unchanged. post_only=True guarantees the bid rests (rejected rather than
    crossing the NO ask)."""
    from py_clob_client_v2.clob_types import (
        OrderArgsV2, PartialCreateOrderOptions, OrderType,
    )
    from py_clob_client_v2.order_builder.constants import BUY
    no_tok = resolve_no_token(spec.signal.market_id)
    if not no_tok:
        log_tick(conn, "no_token_resolve_failed",
                 {"candidate_id": spec.signal.candidate_id,
                  "market_id": spec.signal.market_id})
        _store_order(conn, spec, status="rejected", clob_id=None,
                     raw={"error": "no_token_resolve_failed"})
        return "rejected", "no_token_resolve_failed"
    spec.no_token_id = no_tok
    no_bid = _round_to_tick(1.0 - spec.ask_price, spec.signal.tick_size)
    if no_bid <= 0.0 or no_bid >= 1.0:
        _store_order(conn, spec, status="rejected", clob_id=None,
                     raw={"error": "degenerate_no_bid", "ask": spec.ask_price})
        return "rejected", "degenerate_no_bid"
    args = OrderArgsV2(
        token_id=no_tok, price=no_bid, size=spec.size,
        side=BUY, expiration=spec.expiration,
    )
    opts = PartialCreateOrderOptions(
        tick_size=str(spec.signal.tick_size), neg_risk=spec.signal.neg_risk,
    )
    try:
        signed = client.create_order(args, opts)
    except Exception as e:
        log_tick(conn, "create_order_failed",
                 {"candidate_id": spec.signal.candidate_id, "error": str(e)[:300]})
        _store_order(conn, spec, status="rejected", clob_id=None,
                     raw={"error": str(e)[:500]})
        return "rejected", str(e)
    if dry_run:
        _store_order(conn, spec, status="dry_run", clob_id=None,
                     raw={"signed": "ok", "dry_run": True})
        return "dry_run", None
    try:
        resp = client.post_order(signed, OrderType.GTD, post_only=True)
    except Exception as e:
        log_tick(conn, "post_order_failed",
                 {"candidate_id": spec.signal.candidate_id, "error": str(e)[:300]})
        _store_order(conn, spec, status="rejected", clob_id=None,
                     raw={"error": str(e)[:500]})
        return "rejected", str(e)
    clob_id = ""
    try:
        if isinstance(resp, dict):
            clob_id = str(resp.get("orderID") or resp.get("order_id")
                          or resp.get("id") or "")
    except Exception:
        pass
    _store_order(conn, spec, status="posted", clob_id=clob_id,
                 raw={"resp": str(resp)[:500]})
    return "posted", clob_id


# ── maintain: reconcile fills, settle resolved, compute rebates ─────────────
_DF_COLS = """ts, order_id, clob_trade_id, market_id, yes_token_id, side,
  price, size, fee, fill_ts, raw_json"""
_DF_PH = ",".join(["?"] * 11)

_DS_COLS = """ts, market_id, condition_id, candidate_id, resolved_yes,
  pnl, rebate, rebate_accrued_unpaid, raw_json"""
_DS_PH = ",".join(["?"] * 9)


def reconcile_fills(client, conn):
    """Poll get_open_orders + get_trades; insert new d1_fills for posted maker
    orders. Maker fills carry fee=0 (makers pay no fee). Returns new-fill count."""
    rows = conn.execute(
        """SELECT id, clob_order_id, market_id, yes_token_id, no_token_id, size
           FROM d1_orders WHERE status IN ('posted','open','partial')
           AND clob_order_id != ''"""
    ).fetchall()
    if not rows:
        return 0
    try:
        open_orders = client.get_open_orders() or []
        trades = client.get_trades() or []
    except Exception:
        return 0
    open_by_id = {str(o.get("id")): o for o in open_orders if isinstance(o, dict)}
    seen_trade_ids = {r["clob_trade_id"] for r in conn.execute(
        "SELECT clob_trade_id FROM d1_fills").fetchall() if r["clob_trade_id"]}
    order_by_clob = {str(r["clob_order_id"]): r for r in rows}
    new_fills = 0
    for t in trades:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "")
        if not tid or tid in seen_trade_ids:
            continue
        # A maker resting order is matched as the maker_order_id of a taker trade.
        clob_id = str(t.get("maker_order_id") or t.get("taker_order_id")
                      or t.get("order_id") or "")
        order = order_by_clob.get(clob_id)
        if order is None:
            asset = str(t.get("asset_id") or "")
            for r in rows:
                if r["no_token_id"] == asset:
                    order = r
                    break
        if order is None:
            continue
        t_price = float(t.get("price") or 0.0)
        size = float(t.get("size") or 0.0)
        # BUY-NO fill (NO trade at p_no) → recorded in YES-space: side='sell',
        # price = 1 - p_no (matches the taker path's YES-space mapping; settle
        # uses _fill_pnl('sell', 1-p_no, size, yes_won) = BUY-NO PnL by
        # construction, and p(1-p) is symmetric so the rebate term is unchanged).
        conn.execute(
            f"INSERT INTO d1_fills({_DF_COLS}) VALUES({_DF_PH})",
            (_now_iso(), order["id"], tid, order["market_id"],
             order["yes_token_id"], "sell", 1.0 - t_price, size, 0.0,
             str(t.get("match_time") or ""), json.dumps(t, default=str)[:800]),
        )
        seen_trade_ids.add(tid)
        new_fills += 1
    # update statuses from open_orders
    for r in rows:
        oo = open_by_id.get(str(r["clob_order_id"]))
        if oo:
            matched = float(oo.get("size_matched") or 0.0)
            orig = float(r["size"] or 0.0)
            if orig > 0 and matched >= orig - 1e-9:
                _set_status(conn, r["id"], "filled")
            elif matched > 0:
                _set_status(conn, r["id"], "partial")
            else:
                _set_status(conn, r["id"], "open")
        else:
            n_fills = conn.execute(
                "SELECT COUNT(*) FROM d1_fills WHERE order_id=?", (r["id"],)
            ).fetchone()[0]
            _set_status(conn, r["id"], "filled" if n_fills else "cancelled")
    conn.commit()
    return new_fills


def settle_resolved(conn):
    """For filled markets without a settlement: Gamma fetch_resolution →
    settle._fill_pnl(side='sell', fee_rate=0; maker pays no fee) + rebate
    (sole-maker upper bound: pool_pct * taker_fee_equivalent). Returns count."""
    rows = conn.execute(
        """SELECT DISTINCT f.market_id,
           (SELECT condition_id FROM d1_orders o WHERE o.id=f.order_id) AS condition_id,
           (SELECT candidate_id FROM d1_orders o WHERE o.id=f.order_id) AS candidate_id,
           (SELECT fee_rate FROM d1_orders o WHERE o.id=f.order_id) AS fee_rate
           FROM d1_fills f
           WHERE f.market_id NOT IN (SELECT market_id FROM d1_settlements)"""
    ).fetchall()
    settled = 0
    for r in rows:
        market_id = r["market_id"]
        if not market_id:
            continue
        try:
            closed, outcome, prices = markets.fetch_resolution(market_id)
        except Exception:
            continue
        if not closed or outcome == "none":
            continue
        yes_won = (outcome == "yes")
        fills = conn.execute(
            "SELECT side, price, size FROM d1_fills WHERE market_id=?",
            (market_id,),
        ).fetchall()
        pnl = 0.0
        rebate = 0.0
        for f in fills:
            # Maker fee = 0 → fee_rate=0.0 is correct by construction (not a
            # bookkeeping accident: makers pay zero per docs.polymarket.com/fees).
            pnl += settle._fill_pnl(f["side"], f["price"], f["size"], yes_won)
            # Rebate = pool_pct * (taker fee the matched taker paid) — sole-maker
            # upper bound. fee_equivalent = size * fee_rate * p*(1-p) (market's
            # taker fee_rate; the pool is funded by the taker's fee, not ours).
            fr = float(r["fee_rate"] or 0.0)
            p = float(f["price"] or 0.0)
            rebate += REBATE_POOL_PCT_DEFAULT * f["size"] * fr * p * (1.0 - p)
        # $1 min payout: rebate accrues until it clears $1, then paid.
        accrued_unpaid = rebate  # paid-out tracking is wallet-side; record accrual
        conn.execute(
            f"INSERT INTO d1_settlements({_DS_COLS}) VALUES({_DS_PH})",
            (_now_iso(), market_id, r["condition_id"] or "", r["candidate_id"],
             int(yes_won), pnl, rebate, accrued_unpaid,
             json.dumps({"outcome": outcome, "prices": prices, "fills": len(fills)})),
        )
        conn.execute(
            "UPDATE d1_orders SET status='settled' WHERE market_id=? "
            "AND status IN ('posted','open','filled','partial')",
            (market_id,),
        )
        settled += 1
        import engine
        verdict = "YES won" if yes_won else "NO won"
        engine.notify(
            f"[D1] settled {market_id[:10]}… {verdict} "
            f"pnl={pnl:+.4f} rebate={rebate:.4f}")
    conn.commit()
    return settled


def _set_status(conn, order_id, status):
    conn.execute("UPDATE d1_orders SET status=? WHERE id=?", (status, order_id))


# ── jobs ────────────────────────────────────────────────────────────────────
def job_scan():
    """Read new FLB sell candidates, post resting SELL-YES maker asks (dry-run
    default). One attempt per candidate (cursor advances); 24h GTD, no re-post."""
    init_d1_db()
    if os.path.exists(_halt_d1_path()):
        import engine
        engine.notify("[D1] scan skipped — HALT_D1 present")
        return
    conn = get_d1_db()
    try:
        try:
            client = get_client()
        except D1KeyMissing as e:
            import engine
            engine.notify(f"[D1] D1 key not configured ({e}); no orders. "
                          f"Set POLY_PRIVATE_KEY_D1/POLY_FUNDER_D1 in bot/.env")
            return
        dry = is_dry_run()
        paper_conn = le.paper_ro_conn()
        try:
            sigs, evaluated = read_new_signals(paper_conn, conn)
            posted = dry_signed = rejected = skipped = 0
            for sig in sigs:
                spec = prepare_order(sig)
                if spec is None:
                    log_tick(conn, "skip:prep",
                             {"candidate_id": sig.candidate_id,
                              "mid": sig.scan_mid, "gap": sig.gap})
                    skipped += 1
                    continue
                status, _ = submit(spec, client, conn, dry)
                if status == "posted":
                    posted += 1
                elif status == "dry_run":
                    dry_signed += 1
                else:
                    rejected += 1
            tag = "DRY-RUN" if dry else "LIVE"
            summary = (f"[D1] scan tick ({tag}): evaluated={evaluated} "
                       f"signals={len(sigs)} posted={posted} dry_signed={dry_signed} "
                       f"rejected={rejected} skipped={skipped}")
            import engine
            engine.notify(summary)
            print(f"[{_now_iso()}] {summary}", flush=True)
            conn.commit()
        finally:
            paper_conn.close()
    finally:
        conn.close()


def job_maintain():
    """Reconcile fills, settle resolved, compute rebates. No order posting."""
    init_d1_db()
    if os.path.exists(_halt_d1_path()):
        return
    conn = get_d1_db()
    client = None
    try:
        try:
            client = get_client()
        except D1KeyMissing:
            client = None
        rec_n = 0
        if client is not None:
            rec_n = reconcile_fills(client, conn)
        settled = settle_resolved(conn)
        conn.commit()
        import engine
        engine.notify(f"[D1] maintain: reconciled={rec_n} settled={settled}")
    finally:
        conn.close()


def job_probe():
    """Verify the full BUY-NO maker path on one live market: create_order
    (sign), post_order (geoblock/proxy + BUY-NO maker bid), get_open_orders
    (resting = USDC collateral works), cancel_order. Dry-run signs only; live
    posts+cancels. Bid = no_best_bid - 5*tick (below the inside) so it rests and
    does not fill. Does not write D1 rows — diagnostic only."""
    init_d1_db()
    try:
        client = get_client()
    except D1KeyMissing as e:
        print(f"[D1-PROBE] D1 key missing: {e}")
        return 1
    from py_clob_client_v2.clob_types import (
        OrderArgsV2, PartialCreateOrderOptions, OrderType, OrderPayload,
    )
    from py_clob_client_v2.order_builder.constants import BUY
    ms = markets.fetch_markets(
        {"limit": 100, "active": "true", "closed": "false",
         "liquidity_num_min": 1000, "volume_num_min": 1000}, with_book=True)
    m = next((c for c in ms if c.yes_token_id and 0 < c.best_ask < 0.97
              and c.tick_size), None)
    if m is None:
        print("[D1-PROBE] no live market with a book found")
        return 1
    no_tok = resolve_no_token(m.market_id)
    if not no_tok:
        print("[D1-PROBE] could not resolve NO token for the market")
        return 1
    book = markets.fetch_book(no_tok)
    (_, asks, bb, ba, _bs, _asz, _d, tick, min_sz, neg, _l) = markets._parse_book(book)
    tick = float(tick) if tick else 0.001
    bb = float(bb) if bb else 0.0
    bid = _round_to_tick(bb - 5.0 * tick, tick) if bb > 0 else _round_to_tick(0.01, tick)
    if bid <= 0.0:
        bid = _round_to_tick(0.01, tick)
    size = int(max(min_sz, 1.0))
    exp = int(datetime.now(timezone.utc).timestamp()) + 3600
    print(f"[D1-PROBE] market={m.market_id} no_tok={no_tok[:12]}… "
          f"no_best_bid={bb} bid={bid} size={size} tick={tick} neg={bool(neg) or m.neg_risk}")
    args = OrderArgsV2(token_id=no_tok, price=bid, size=size,
                       side=BUY, expiration=exp)
    opts = PartialCreateOrderOptions(
        tick_size=str(tick), neg_risk=bool(neg) or m.neg_risk)
    try:
        signed = client.create_order(args, opts)
    except Exception as e:
        print(f"[D1-PROBE] create_order FAILED: {e}")
        return 1
    print("[D1-PROBE] create_order OK (signed)")
    if is_dry_run():
        print("[D1-PROBE] DRY-RUN — not posting. Set D1_DRY_RUN=0 + HTTPS_PROXY "
              "to test the post path.")
        return 0
    try:
        resp = client.post_order(signed, OrderType.GTD, post_only=True)
    except Exception as e:
        print(f"[D1-PROBE] post_order FAILED (geoblock? proxy?): {e}")
        return 1
    clob_id = ""
    if isinstance(resp, dict):
        clob_id = str(resp.get("orderID") or resp.get("order_id")
                      or resp.get("id") or "")
    print(f"[D1-PROBE] post_order OK orderID={clob_id}")
    try:
        oo = client.get_open_orders() or []
        oo_ids = [str(o.get("id") or o.get("order_id") or o.get("orderId") or "")
                  for o in oo if isinstance(o, dict)]
        resting = clob_id in oo_ids
        print(f"[D1-PROBE] resting={resting} posted_id={clob_id[:16]} "
              f"open_count={len(oo)} open_ids={[i[:16] for i in oo_ids[:5]]}")
        if oo and isinstance(oo[0], dict):
            print(f"[D1-PROBE] open_order_keys={list(oo[0].keys())}")
    except Exception as e:
        print(f"[D1-PROBE] get_open_orders failed: {e}")
    if clob_id:
        try:
            client.cancel_order(OrderPayload(orderID=clob_id))
            print("[D1-PROBE] cancel_order OK")
        except Exception as e:
            print(f"[D1-PROBE] cancel_order FAILED: {e}")
    return 0
