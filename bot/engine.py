import asyncio
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

import weather as w
from config import (
    CITIES,
    CLOB_BASE,
    CONSECUTIVE_LOSS_HALT,
    DAILY_LOSS_HALT_FRAC,
    DAILY_PULSE_HOUR_HKT,
    DB_PATH,
    GAMMA_BASE,
    MAX_LEAD_HOURS,
    MAX_POSITIONS_PER_REGION_DAY,
    MIN_EDGE,
    MODELS,
    OPEN_METEO_ARCHIVE,
    PAPER_BANKROLL,
    PER_TRADE_CAP_ABS,
    PER_TRADE_CAP_FRAC,
    PRICE_BAND,
    TELEGRAM_CHAT_ID_ENV,
    TELEGRAM_TOKEN_ENV,
    WEATHER_EVENT_TITLE_RE,
    tls_verify,
)

MODE = "paper"

MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


@dataclass
class MarketSnapshot:
    market_id: str
    condition_id: str
    event_id: str
    question: str
    city: str
    bucket: object
    market_date: str
    end_date: str
    best_bid: float
    best_ask: float
    bid_size: float
    ask_size: float
    depth: float
    tick_size: float
    min_order_size: float
    fee_rate: float
    fees_enabled: bool
    neg_risk: bool
    liquidity: float
    last_trade_price: float
    yes_token_id: str
    ts: str
    bids: list = field(default_factory=list)
    asks: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class Candidate:
    snapshot: MarketSnapshot
    side: str
    p_model: float
    p_by_model: dict
    edge_after_costs: float
    lead_hours: float
    run_ids: str
    effective_price: float
    blend_mean: float
    raw: dict = field(default_factory=dict)


@dataclass
class ProposedOrder:
    snapshot: MarketSnapshot
    token_id: str
    side: str
    price: float
    size: float
    maker_or_taker: str
    edge: float
    kelly_fraction: float
    raw: dict = field(default_factory=dict)


@dataclass
class RiskVerdict:
    approved: bool
    reason: str
    caps_applied: dict = field(default_factory=dict)


@dataclass
class Fill:
    order_id: int
    market_id: str
    token_id: str
    side: str
    price: float
    size: float
    maker_or_taker: str
    fill_ts: str
    pnl: float
    raw: dict = field(default_factory=dict)


@dataclass
class Settlement:
    market_id: str
    condition_id: str
    city: str
    date: str
    observed_high: float
    bucket_key: str
    resolved_yes: bool
    pnl: float
    raw: dict = field(default_factory=dict)


@dataclass
class BotState:
    bankroll: float
    realized_pnl_today: float
    consecutive_losses: int
    open_positions: list = field(default_factory=list)
    open_orders: list = field(default_factory=list)


def set_mode(m):
    global MODE
    MODE = m


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, job TEXT, mode TEXT,
            note TEXT, snapshot_json TEXT
        );
        CREATE TABLE IF NOT EXISTS model_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, city TEXT, model TEXT,
            run_ts TEXT, content_hash TEXT, daily_highs_json TEXT, snapshot_json TEXT
        );
        CREATE TABLE IF NOT EXISTS station_obs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, city TEXT, date TEXT,
            observed_high REAL, blend_mean REAL, residual REAL, source TEXT, snapshot_json TEXT
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, market_id TEXT, event_id TEXT,
            condition_id TEXT, question TEXT, city TEXT, market_date TEXT, bucket_key TEXT,
            bucket_json TEXT, end_date TEXT, best_bid REAL, best_ask REAL, bid_size REAL,
            ask_size REAL, depth REAL, tick_size REAL, min_order_size REAL, fee_rate REAL,
            fees_enabled INTEGER, neg_risk INTEGER, liquidity REAL, last_trade_price REAL,
            yes_token_id TEXT, snapshot_json TEXT
        );
        CREATE TABLE IF NOT EXISTS candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, scan_id INTEGER, market_id TEXT,
            condition_id TEXT, side TEXT, p_model REAL, p_by_model_json TEXT,
            edge_after_costs REAL, lead_hours REAL, run_ids TEXT, bucket_key TEXT,
            effective_price REAL, blend_mean REAL, snapshot_json TEXT
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, scan_id INTEGER, market_id TEXT,
            token_id TEXT, side TEXT, price REAL, size REAL, maker_or_taker TEXT, edge REAL,
            kelly_fraction REAL, status TEXT, city TEXT, market_date TEXT, snapshot_json TEXT
        );
        CREATE TABLE IF NOT EXISTS fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, order_id INTEGER, market_id TEXT,
            token_id TEXT, side TEXT, price REAL, size REAL, maker_or_taker TEXT, fill_ts TEXT,
            pnl REAL, snapshot_json TEXT
        );
        CREATE TABLE IF NOT EXISTS settlements (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, market_id TEXT, condition_id TEXT,
            city TEXT, date TEXT, observed_high REAL, bucket_key TEXT, resolved_yes INTEGER,
            pnl REAL, snapshot_json TEXT
        );
        CREATE TABLE IF NOT EXISTS reward_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, market_id TEXT, max_spread REAL,
            min_size REAL, midpoint REAL, q_score REAL, two_sided_min REAL, snapshot_json TEXT
        );
        CREATE TABLE IF NOT EXISTS halts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, reason TEXT, snapshot_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_snap_market ON snapshots(market_id);
        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
        CREATE INDEX IF NOT EXISTS idx_sett_city_date ON settlements(city, date);
        CREATE INDEX IF NOT EXISTS idx_stobs_city ON station_obs(city);
        CREATE INDEX IF NOT EXISTS idx_runs_city_model ON model_runs(city, model);
        """
    )
    conn.commit()
    conn.close()


def meta_get(conn, k, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def meta_set(conn, k, v):
    conn.execute(
        "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (k, v),
    )


def log(event):
    init_db()
    conn = get_db()
    conn.execute(
        "INSERT INTO scans(ts, job, mode, note, snapshot_json) VALUES(?,?,?,?,?)",
        (now_iso(), str(event.get("job", "")), MODE, str(event.get("note", "")),
         json.dumps(event, default=str)),
    )
    conn.commit()
    conn.close()


def notify(text):
    token = os.environ.get(TELEGRAM_TOKEN_ENV)
    chat = os.environ.get(TELEGRAM_CHAT_ID_ENV)
    if not token or not chat:
        return
    try:
        from telegram import Bot
        bot = Bot(token=token)
        asyncio.run(bot.send_message(chat_id=chat, text=text[:4000]))
    except Exception:
        pass


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_market_date(description):
    import re
    m = re.search(r"(\d{1,2})\s+(\w{3})\w*\s+'?(\d{2,4})", description or "")
    if not m:
        return None
    day = int(m.group(1))
    mon = MONTHS.get(m.group(2)[:3].title()[:3])
    if not mon:
        return None
    yr = int(m.group(3))
    if yr < 100:
        yr += 2000
    return f"{yr:04d}-{mon:02d}-{day:02d}"


def _slug(city, d):
    months = ["january", "february", "march", "april", "may", "june",
              "july", "august", "september", "october", "november", "december"]
    cslug = city.lower().replace(" ", "-")
    return f"highest-temperature-in-{cslug}-on-{months[d.month-1]}-{d.day}-{d.year}"


def _parse_book(book):
    bids = sorted(
        [{"price": float(b["price"]), "size": float(b["size"])} for b in (book.get("bids") or [])],
        key=lambda x: -x["price"],
    )
    asks = sorted(
        [{"price": float(a["price"]), "size": float(a["size"])} for a in (book.get("asks") or [])],
        key=lambda x: x["price"],
    )
    best_bid = bids[0]["price"] if bids else 0.0
    best_ask = asks[0]["price"] if asks else 1.0
    bid_size = bids[0]["size"] if bids else 0.0
    ask_size = asks[0]["size"] if asks else 0.0
    depth = sum(b["size"] for b in bids[:10]) + sum(a["size"] for a in asks[:10])
    tick = float(book.get("tick_size") or 0.001)
    min_sz = float(book.get("min_order_size") or 5)
    neg = bool(book.get("neg_risk"))
    last = float(book.get("last_trade_price") or 0.0)
    return bids, asks, best_bid, best_ask, bid_size, ask_size, depth, tick, min_sz, neg, last


def fetch_snapshots():
    snaps = []
    today = datetime.now(timezone.utc)
    dates = [today, datetime.fromtimestamp(today.timestamp() + 86400, timezone.utc)]
    seen_events = set()
    with httpx.Client(timeout=30, headers={"User-Agent": "MarcusVaultBot/1.0"},
                      verify=tls_verify()) as client:
        for d in dates:
            for city, meta in CITIES.items():
                slug = _slug(city, d)
                try:
                    r = client.get(f"{GAMMA_BASE}/events", params={"slug": slug}).json()
                except Exception:
                    continue
                if not r:
                    continue
                ev = r[0] if isinstance(r, list) else r
                if not ev.get("markets"):
                    continue
                ev_id = str(ev.get("id", ""))
                if (ev_id, slug) in seen_events:
                    continue
                seen_events.add((ev_id, slug))
                for mkt in ev["markets"]:
                    q = mkt.get("question", "")
                    bucket = w.parse_bucket(q)
                    if not bucket:
                        continue
                    desc = mkt.get("description", "")
                    if meta["station_name"] not in desc:
                        continue
                    mdate = _parse_market_date(desc) or f"{d.year}-{d.month:02d}-{d.day:02d}"
                    tokens = json.loads(mkt.get("clobTokenIds") or "[]")
                    if len(tokens) < 2:
                        continue
                    yes_token = tokens[0]
                    try:
                        book = client.get(
                            f"{CLOB_BASE}/book", params={"token_id": yes_token}
                        ).json()
                    except Exception:
                        continue
                    (bids, asks, bb, ba, bs, asz, depth, tick, min_sz,
                     neg, last) = _parse_book(book)
                    fees_enabled = bool(mkt.get("feesEnabled"))
                    sched = mkt.get("feeSchedule") or {}
                    fee_rate = float(sched.get("rate") or 0.0) if fees_enabled else 0.0
                    snap = MarketSnapshot(
                        market_id=str(mkt.get("id", "")),
                        condition_id=str(mkt.get("conditionId", "")),
                        event_id=ev_id,
                        question=q,
                        city=city,
                        bucket=bucket,
                        market_date=mdate,
                        end_date=str(mkt.get("endDate", "")),
                        best_bid=bb, best_ask=ba, bid_size=bs, ask_size=asz,
                        depth=depth, tick_size=tick, min_order_size=min_sz,
                        fee_rate=fee_rate, fees_enabled=fees_enabled,
                        neg_risk=neg, liquidity=float(mkt.get("liquidity") or 0.0),
                        last_trade_price=last, yes_token_id=yes_token,
                        bids=bids, asks=asks, ts=now_iso(), raw=mkt,
                    )
                    snaps.append(snap)
                    _store_snapshot(snap)
    return snaps


def _store_snapshot(s):
    conn = get_db()
    conn.execute(
        """INSERT INTO snapshots(ts, market_id, event_id, condition_id, question, city,
           market_date, bucket_key, bucket_json, end_date, best_bid, best_ask, bid_size,
           ask_size, depth, tick_size, min_order_size, fee_rate, fees_enabled, neg_risk,
           liquidity, last_trade_price, yes_token_id, snapshot_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (s.ts, s.market_id, s.event_id, s.condition_id, s.question, s.city, s.market_date,
         s.bucket.key, json.dumps({"lo": s.bucket.lo, "lo_incl": s.bucket.lo_incl,
         "hi": s.bucket.hi, "hi_incl": s.bucket.hi_incl}), s.end_date, s.best_bid, s.best_ask,
         s.bid_size, s.ask_size, s.depth, s.tick_size, s.min_order_size, s.fee_rate,
         int(s.fees_enabled), int(s.neg_risk), s.liquidity, s.last_trade_price,
         s.yes_token_id, json.dumps(s.raw, default=str)),
    )
    conn.commit()
    conn.close()


def _load_residuals(conn, city):
    rows = conn.execute(
        "SELECT residual FROM station_obs WHERE city=? ORDER BY id DESC LIMIT 30",
        (city,),
    ).fetchall()
    return [r["residual"] for r in rows if r["residual"] is not None]


def _walk_book(levels, target_shares):
    filled = 0.0
    cost = 0.0
    for lvl in levels:
        if filled >= target_shares:
            break
        take = min(lvl["size"], target_shares - filled)
        cost += lvl["price"] * take
        filled += take
    if filled <= 0:
        return None
    return cost / filled


def _lead_hours(end_date):
    try:
        ed = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
    except Exception:
        return 0.0
    if ed.tzinfo is None:
        ed = ed.replace(tzinfo=timezone.utc)
    return max(0.0, (ed - datetime.now(timezone.utc)).total_seconds() / 3600.0)


def scan_weather(runs, snapshots):
    conn = get_db()
    scan_id = _new_scan(conn, "weather")
    cands = []
    by_city_date = {}
    for s in snapshots:
        by_city_date.setdefault((s.city, s.market_date), []).append(s)
    for (city, mdate), group in by_city_date.items():
        city_runs = [r for r in runs if r.city == city]
        if not city_runs:
            continue
        residuals = _load_residuals(conn, city)
        bias = w.station_bias(residuals)
        buckets = [s.bucket for s in group]
        if not w.consensus_ok(city_runs, mdate):
            continue
        probs_by_model = {}
        for run in city_runs:
            probs_by_model[run.model] = w.daily_high_probs(run, mdate, buckets, bias)
        blended = w.blend(probs_by_model, MODELS)
        means = {}
        for run in city_runs:
            mem = run.daily_high_by_date.get(mdate, [])
            if mem:
                means[run.model] = sum(mem) / len(mem)
        wsum = sum(MODELS.get(m, 0) for m in means) or 1.0
        blend_mean = sum(MODELS.get(m, 0) * v for m, v in means.items()) / wsum
        run_ids = ",".join(r.content_hash for r in city_runs)
        for s in group:
            p = blended.get(s.bucket.key, 0.0)
            lead = _lead_hours(s.end_date)
            if lead > MAX_LEAD_HOURS:
                continue
            target_shares = PER_TRADE_CAP_ABS / max(s.best_ask, 0.01)
            eff_ask = _walk_book(s.asks, target_shares)
            eff_bid = _walk_book(s.bids, target_shares)
            if eff_ask is None or eff_bid is None:
                continue
            fee = s.fee_rate * p * (1 - p) if s.fees_enabled else 0.0
            buy_edge = p - eff_ask - fee
            sell_edge = eff_bid - p - fee
            chosen = None
            if buy_edge >= MIN_EDGE and PRICE_BAND[0] <= s.best_ask <= PRICE_BAND[1]:
                chosen = ("buy", buy_edge, eff_ask)
            if sell_edge >= MIN_EDGE and chosen is None and \
               PRICE_BAND[0] <= s.best_bid <= PRICE_BAND[1]:
                chosen = ("sell", sell_edge, eff_bid)
            if chosen is None:
                continue
            side, edge, eff = chosen
            cands.append(Candidate(
                snapshot=s, side=side, p_model=p, p_by_model=probs_by_model,
                edge_after_costs=edge, lead_hours=lead, run_ids=run_ids,
                effective_price=eff, blend_mean=blend_mean,
                raw={"probs_by_model": probs_by_model, "blended": blended,
                     "bias": bias, "blend_mean": blend_mean},
            ))
    for c in cands:
        _store_candidate(conn, scan_id, c)
    conn.commit()
    conn.close()
    return cands


def _new_scan(conn, job):
    cur = conn.execute(
        "INSERT INTO scans(ts, job, mode, note, snapshot_json) VALUES(?,?,?,?,?)",
        (now_iso(), job, MODE, "", "{}"),
    )
    conn.commit()
    return cur.lastrowid


def _store_candidate(conn, scan_id, c):
    conn.execute(
        """INSERT INTO candidates(ts, scan_id, market_id, condition_id, side, p_model,
           p_by_model_json, edge_after_costs, lead_hours, run_ids, bucket_key,
           effective_price, blend_mean, snapshot_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (now_iso(), scan_id, c.snapshot.market_id, c.snapshot.condition_id, c.side,
         c.p_model, json.dumps(c.p_by_model, default=str), c.edge_after_costs,
         c.lead_hours, c.run_ids, c.snapshot.bucket.key, c.effective_price,
         c.blend_mean, json.dumps(c.raw, default=str)),
    )


def load_state():
    conn = get_db()
    bankroll = float(meta_get(conn, "bankroll", PAPER_BANKROLL))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COALESCE(SUM(pnl),0) AS p FROM settlements WHERE substr(ts,1,10)=?",
        (today,),
    ).fetchone()
    pnl_today = float(row["p"] or 0.0)
    consec = conn.execute(
        "SELECT pnl FROM settlements ORDER BY id DESC LIMIT 20"
    ).fetchall()
    losses = 0
    for r in consec:
        if (r["pnl"] or 0) < 0:
            losses += 1
        else:
            break
    open_pos = [dict(r) for r in conn.execute(
        "SELECT market_id, city, market_date FROM orders WHERE status IN ('open','filled')"
    ).fetchall()]
    open_orders = [dict(r) for r in conn.execute(
        "SELECT id, market_id, token_id, side, price, size FROM orders WHERE status='open'"
    ).fetchall()]
    conn.close()
    return BotState(bankroll=bankroll, realized_pnl_today=pnl_today,
                    consecutive_losses=losses, open_positions=open_pos,
                    open_orders=open_orders)


def propose(candidates, state):
    orders = []
    for c in candidates:
        q = c.effective_price
        if q <= 0.0 or q >= 1.0:
            continue
        if any(p["market_id"] == c.snapshot.market_id for p in state.open_positions):
            continue
        if c.side == "buy":
            f = (c.p_model - q) / (1 - q)
        else:
            f = (q - c.p_model) / q
        f = max(0.0, f) / 4.0
        notional = min(f * state.bankroll, PER_TRADE_CAP_FRAC * state.bankroll,
                       PER_TRADE_CAP_ABS)
        if notional <= 0.0:
            continue
        shares = notional / q
        maker = (c.edge_after_costs >= MIN_EDGE + 0.02 and c.lead_hours > 6)
        order = ProposedOrder(
            snapshot=c.snapshot, token_id=c.snapshot.yes_token_id, side=c.side,
            price=q, size=shares, maker_or_taker=("maker" if maker else "taker"),
            edge=c.edge_after_costs, kelly_fraction=f * 4, raw={"notional": notional},
        )
        orders.append(order)
    return orders


def risk_check(order, state):
    caps = {}
    if os.path.exists(_halt_path()):
        return RiskVerdict(False, "halt file present", caps)
    price = order.price
    if not (PRICE_BAND[0] <= price <= PRICE_BAND[1]):
        return RiskVerdict(False, f"price {price} outside band {PRICE_BAND}", caps)
    notional = order.price * order.size
    cap = min(PER_TRADE_CAP_FRAC * state.bankroll, PER_TRADE_CAP_ABS)
    caps["per_trade_notional"] = min(notional, cap)
    if notional > cap + 1e-9:
        return RiskVerdict(False, f"per-trade cap exceeded {notional}>{cap}", caps)
    if state.realized_pnl_today <= -DAILY_LOSS_HALT_FRAC * state.bankroll:
        return RiskVerdict(False, "daily loss halt", caps)
    if state.consecutive_losses >= CONSECUTIVE_LOSS_HALT:
        return RiskVerdict(False, "consecutive loss halt", caps)
    city = order.snapshot.city
    mdate = order.snapshot.market_date
    region_count = sum(
        1 for p in state.open_positions
        if p.get("city") == city and p.get("market_date") == mdate
    )
    caps["region_day_positions"] = region_count
    if region_count >= MAX_POSITIONS_PER_REGION_DAY:
        return RiskVerdict(False, f"region-day cap {region_count}", caps)
    if any(p["market_id"] == order.snapshot.market_id for p in state.open_positions):
        return RiskVerdict(False, "one position per market per cycle", caps)
    return RiskVerdict(True, "approved", caps)


def _halt_path():
    from config import HALT_FILE
    return HALT_FILE


def execute(order, verdict):
    if not verdict.approved:
        return None
    if MODE == "real":
        raise NotImplementedError("real execution is Milestone 4")
    conn = get_db()
    order_id = _store_order_final(conn, order, verdict)
    if order.maker_or_taker == "taker":
        fill = Fill(order_id=order_id, market_id=order.snapshot.market_id,
                    token_id=order.token_id, side=order.side, price=order.price,
                    size=order.size, maker_or_taker="taker", fill_ts=now_iso(),
                    pnl=0.0, raw={})
        _store_fill(conn, fill)
        _set_order_status(conn, order_id, "filled")
        conn.commit()
        conn.close()
        return fill
    _set_order_status(conn, order_id, "open")
    conn.commit()
    conn.close()
    return None


def _store_order_final(conn, order, verdict):
    cur = conn.execute(
        """INSERT INTO orders(ts, scan_id, market_id, token_id, side, price, size,
           maker_or_taker, edge, kelly_fraction, status, city, market_date, snapshot_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (now_iso(), 0, order.snapshot.market_id, order.token_id, order.side, order.price,
         order.size, order.maker_or_taker, order.edge, order.kelly_fraction,
         "pending", order.snapshot.city, order.snapshot.market_date,
         json.dumps({"verdict": verdict.reason, "caps": verdict.caps_applied}, default=str)),
    )
    conn.commit()
    return cur.lastrowid


def _set_order_status(conn, order_id, status):
    conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))


def _store_fill(conn, fill):
    conn.execute(
        """INSERT INTO fills(ts, order_id, market_id, token_id, side, price, size,
           maker_or_taker, fill_ts, pnl, snapshot_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (now_iso(), fill.order_id, fill.market_id, fill.token_id, fill.side, fill.price,
         fill.size, fill.maker_or_taker, fill.fill_ts, fill.pnl,
         json.dumps(fill.raw, default=str)),
    )


def check_open_maker_fills():
    conn = get_db()
    open_orders = conn.execute(
        "SELECT id, market_id, token_id, side, price, size FROM orders WHERE status='open'"
    ).fetchall()
    with httpx.Client(timeout=30, headers={"User-Agent": "MarcusVaultBot/1.0"},
                      verify=tls_verify()) as client:
        for o in open_orders:
            try:
                book = client.get(f"{CLOB_BASE}/book",
                                  params={"token_id": o["token_id"]}).json()
            except Exception:
                continue
            last = float(book.get("last_trade_price") or 0.0)
            if last == 0:
                continue
            fills = False
            if o["side"] == "buy" and last <= o["price"]:
                fills = True
            elif o["side"] == "sell" and last >= o["price"]:
                fills = True
            if not fills:
                continue
            fill = Fill(order_id=o["id"], market_id=o["market_id"], token_id=o["token_id"],
                        side=o["side"], price=o["price"], size=o["size"],
                        maker_or_taker="maker", fill_ts=now_iso(), pnl=0.0, raw={})
            _store_fill(conn, fill)
            _set_order_status(conn, o["id"], "filled")
    conn.commit()
    conn.close()


def sweep_settlements():
    conn = get_db()
    rows = conn.execute(
        """SELECT DISTINCT city, market_date, condition_id, market_id, bucket_key,
           bucket_json, yes_token_id
           FROM snapshots
           WHERE market_date NOT IN (SELECT date FROM settlements GROUP BY date, city)
           LIMIT 200""",
    ).fetchall()
    pending = {}
    for r in rows:
        pending.setdefault((r["city"], r["market_date"]), []).append(r)
    settled_count = 0
    for (city, mdate), group in pending.items():
        if not _is_resolvable(city, mdate):
            continue
        obs = _fetch_observed_high(city, mdate)
        if obs is None:
            continue
        bucket_key, market_id, condition_id = _winning_bucket(group, obs)
        resolved_yes = bucket_key is not None
        for r in group:
            win = (r["bucket_key"] == bucket_key) if bucket_key else False
            pnl = _compute_settlement_pnl(conn, r, win, obs)
            st = Settlement(market_id=r["market_id"], condition_id=r["condition_id"],
                            city=city, date=mdate, observed_high=obs,
                            bucket_key=r["bucket_key"], resolved_yes=win, pnl=pnl,
                            raw={"obs": obs, "winning_bucket": bucket_key})
            _store_settlement(conn, st)
            settled_count += 1
        _store_residual(conn, city, mdate, obs, group)
    conn.commit()
    conn.close()
    return settled_count


def _is_resolvable(city, mdate):
    try:
        d = datetime.strptime(mdate, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return False
    now = datetime.now(timezone.utc)
    return (now - d).total_seconds() > 36 * 3600


def _fetch_observed_high(city, mdate):
    meta = CITIES[city]
    try:
        r = httpx.get(OPEN_METEO_ARCHIVE, params={
            "latitude": meta["lat"], "longitude": meta["lon"],
            "daily": "temperature_2m_max", "start_date": mdate, "end_date": mdate,
            "timezone": meta["timezone"],
        }, timeout=30, headers={"User-Agent": "MarcusVaultBot/1.0"},
            verify=tls_verify()).json()
        vals = (r.get("daily") or {}).get("temperature_2m_max") or []
        if not vals or vals[0] is None:
            return None
        return float(vals[0])
    except Exception:
        return None


def _winning_bucket(group, obs):
    for r in group:
        bj = json.loads(r["bucket_json"] or "{}")
        b = w.Bucket(key=r["bucket_key"], lo=bj.get("lo"), lo_incl=bj.get("lo_incl"),
                     hi=bj.get("hi"), hi_incl=bj.get("hi_incl"))
        if b.contains(obs):
            return r["bucket_key"], r["market_id"], r["condition_id"]
    return None, None, None


def _compute_settlement_pnl(conn, r, win, obs):
    fills = conn.execute(
        "SELECT side, price, size FROM fills WHERE market_id=?", (r["market_id"],)
    ).fetchall()
    pnl = 0.0
    for f in fills:
        if f["side"] == "buy":
            pnl += (1.0 - f["price"]) * f["size"] if win else (-f["price"]) * f["size"]
        else:
            pnl += (f["price"] - 1.0) * f["size"] if win else f["price"] * f["size"]
    return pnl


def _store_settlement(conn, st):
    conn.execute(
        """INSERT INTO settlements(ts, market_id, condition_id, city, date, observed_high,
           bucket_key, resolved_yes, pnl, snapshot_json) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (now_iso(), st.market_id, st.condition_id, st.city, st.date, st.observed_high,
         st.bucket_key, int(st.resolved_yes), st.pnl, json.dumps(st.raw, default=str)),
    )


def _store_residual(conn, city, mdate, obs, group):
    snap = group[0]
    bmean = _recompute_blend_mean(city, mdate)
    residual = obs - bmean if bmean is not None else None
    conn.execute(
        """INSERT INTO station_obs(ts, city, date, observed_high, blend_mean, residual,
           source, snapshot_json) VALUES(?,?,?,?,?,?,?,?)""",
        (now_iso(), city, mdate, obs, bmean, residual, "open-meteo-archive",
         json.dumps({"market_id": snap["market_id"]}, default=str)),
    )


def _recompute_blend_mean(city, mdate):
    conn = get_db()
    rows = conn.execute(
        "SELECT model, daily_highs_json FROM model_runs WHERE city=? ORDER BY id DESC",
        (city,),
    ).fetchall()
    conn.close()
    seen = {}
    for r in rows:
        if r["model"] not in seen:
            seen[r["model"]] = r
    means = {}
    for model, r in seen.items():
        try:
            data = json.loads(r["daily_highs_json"] or "{}")
        except Exception:
            continue
        highs = data.get(mdate) or []
        if highs:
            means[model] = sum(highs) / len(highs)
    if not means:
        return None
    wsum = sum(MODELS.get(m, 0) for m in means) or 1.0
    return sum(MODELS.get(m, 0) * v for m, v in means.items()) / wsum


def store_runs(runs):
    conn = get_db()
    ts = now_iso()
    for r in runs:
        conn.execute(
            """INSERT INTO model_runs(ts, city, model, run_ts, content_hash,
               daily_highs_json, snapshot_json) VALUES(?,?,?,?,?,?,?)""",
            (ts, r.city, r.model, r.run_ts, r.content_hash,
             json.dumps(r.daily_high_by_date, default=str), "{}"),
        )
    conn.commit()
    conn.close()


def log_reward_snapshot(snapshots):
    conn = get_db()
    ts = now_iso()
    for s in snapshots:
        raw = s.raw
        max_spread_c = raw.get("rewardsMaxSpread")
        min_size = raw.get("rewardsMinSize")
        if max_spread_c is None:
            continue
        v = float(max_spread_c) / 100.0
        mid = (s.best_bid + s.best_ask) / 2.0
        if s.best_ask <= s.best_bid:
            continue
        bid_q = s.best_bid + s.tick_size
        ask_q = s.best_ask - s.tick_size
        s_bid = max(0.0, mid - bid_q)
        s_ask = max(0.0, ask_q - mid)
        b = float(min_size or 0.0)
        if v <= 0 or b <= 0:
            continue
        q_bid = ((v - s_bid) / v) ** 2 * b
        q_ask = ((v - s_ask) / v) ** 2 * b
        q_min = max(min(q_bid, q_ask), max(q_bid, q_ask) / 3.0)
        if mid < 0.10 or mid > 0.90:
            q_score = q_min
        else:
            q_score = max(q_bid, q_ask)
        conn.execute(
            """INSERT INTO reward_snapshots(ts, market_id, max_spread, min_size, midpoint,
               q_score, two_sided_min, snapshot_json) VALUES(?,?,?,?,?,?,?,?)""",
            (ts, s.market_id, v, b, mid, q_score, q_min,
             json.dumps({"bid_q": q_bid, "ask_q": q_ask}, default=str)),
        )
    conn.commit()
    conn.close()


def halt_check():
    if os.path.exists(_halt_path()):
        conn = get_db()
        conn.execute(
            "INSERT INTO halts(ts, reason, snapshot_json) VALUES(?,?,?)",
            (now_iso(), "HALT file present", "{}"),
        )
        conn.commit()
        conn.close()
        return True
    return False


def daily_pnl_pulse_if_due():
    conn = get_db()
    last = meta_get(conn, "last_pulse_date", "")
    now = datetime.now(timezone.utc)
    hkt = now.astimezone(timezone(timedelta(hours=8)))
    today_str = hkt.strftime("%Y-%m-%d")
    if hkt.hour < DAILY_PULSE_HOUR_HKT:
        conn.close()
        return
    if last == today_str:
        conn.close()
        return
    row = conn.execute("SELECT COALESCE(SUM(pnl),0) AS p FROM fills").fetchone()
    total = float(row["p"] or 0.0)
    n = conn.execute("SELECT COUNT(*) AS n FROM fills").fetchone()["n"]
    nset = conn.execute("SELECT COUNT(*) AS n FROM settlements").fetchone()["n"]
    meta_set(conn, "last_pulse_date", today_str)
    conn.commit()
    conn.close()
    notify(f"[PnL pulse {today_str} HKT] fills={n} settled={nset} total_pnl={total:.2f}")
