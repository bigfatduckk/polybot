import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from config import CLOB_BASE, DB_PATH, GAMMA_BASE, tls_verify

UA = {"User-Agent": "MarcusVaultBot/1.0"}


def _client():
    return httpx.Client(timeout=30, headers=UA, verify=tls_verify())


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


@dataclass
class Market:
    market_id: str
    condition_id: str
    event_id: str
    event_slug: str
    question: str
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
    no_token_id: str
    ts: str
    bids: list = field(default_factory=list)
    asks: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _market_from_mkt(mkt, ev_id="", ev_slug=""):
    tokens = json.loads(mkt.get("clobTokenIds") or "[]")
    yes_tok = tokens[0] if len(tokens) >= 1 else ""
    no_tok = tokens[1] if len(tokens) >= 2 else ""
    fees_enabled = bool(mkt.get("feesEnabled"))
    sched = mkt.get("feeSchedule") or {}
    fee_rate = float(sched.get("rate") or 0.0) if fees_enabled else 0.0
    return Market(
        market_id=str(mkt.get("id", "")),
        condition_id=str(mkt.get("conditionId", "")),
        event_id=ev_id or str(mkt.get("eventId", "")),
        event_slug=ev_slug or str(mkt.get("slug", "")),
        question=str(mkt.get("question", "")),
        end_date=str(mkt.get("endDate", "")),
        best_bid=0.0, best_ask=1.0, bid_size=0.0, ask_size=0.0, depth=0.0,
        tick_size=0.001, min_order_size=5.0, fee_rate=fee_rate,
        fees_enabled=fees_enabled, neg_risk=bool(mkt.get("negRisk")),
        liquidity=float(mkt.get("liquidity") or 0.0),
        last_trade_price=0.0, yes_token_id=yes_tok, no_token_id=no_tok,
        ts=_now_iso(), raw=mkt,
    )


def fetch_book(token_id):
    if not token_id:
        return None
    try:
        with _client() as c:
            return c.get(f"{CLOB_BASE}/book", params={"token_id": token_id}).json()
    except Exception:
        return None


def _hydrate(m, book):
    if not book:
        return m
    (bids, asks, bb, ba, bs, asz, depth, tick, min_sz,
     neg, last) = _parse_book(book)
    m.bids, m.asks = bids, asks
    m.best_bid, m.best_ask = bb, ba
    m.bid_size, m.ask_size = bs, asz
    m.depth = depth
    m.tick_size = tick
    m.min_order_size = min_sz
    m.neg_risk = m.neg_risk or neg
    m.last_trade_price = last
    return m


def fetch_markets(params, with_book=True):
    out = []
    with _client() as c:
        r = c.get(f"{GAMMA_BASE}/markets", params=params)
        mkts = r.json() if r.status_code == 200 else []
        for mkt in mkts or []:
            m = _market_from_mkt(mkt)
            if with_book and m.yes_token_id:
                _hydrate(m, fetch_book(m.yes_token_id))
            out.append(m)
    return out


def fetch_events(params):
    with _client() as c:
        r = c.get(f"{GAMMA_BASE}/events", params=params)
        return r.json() if r.status_code == 200 else []


def sibling_markets(event_id, with_book=True):
    out = []
    with _client() as c:
        r = c.get(f"{GAMMA_BASE}/markets", params={"event_id": event_id})
        mkts = r.json() if r.status_code == 200 else []
        for mkt in mkts or []:
            m = _market_from_mkt(mkt, ev_id=event_id)
            if with_book and m.yes_token_id:
                _hydrate(m, fetch_book(m.yes_token_id))
            out.append(m)
    return out


def fetch_resolution(market_id):
    with _client() as c:
        r = c.get(f"{GAMMA_BASE}/markets", params={"id": market_id, "closed": "true", "archived": "true"})
        mkts = r.json() if r.status_code == 200 else []
    if not mkts:
        return False, "none", []
    mkt = mkts[0]
    closed = bool(mkt.get("closed"))
    prices = mkt.get("outcomePrices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except (TypeError, ValueError):
            prices = []
    prices = prices or []
    try:
        prices = [float(p) for p in prices]
    except (TypeError, ValueError):
        prices = []
    if not prices:
        return closed, "none", []
    yes_p = prices[0] if len(prices) >= 1 else 0.0
    no_p = prices[1] if len(prices) >= 2 else 0.0
    if yes_p >= no_p:
        outcome = "yes"
    else:
        outcome = "no"
    return closed, outcome, prices


def init_edge_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pm_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, edge TEXT, market_id TEXT,
            event_id TEXT, condition_id TEXT, question TEXT, end_date TEXT,
            best_bid REAL, best_ask REAL, bid_size REAL, ask_size REAL, depth REAL,
            tick_size REAL, min_order_size REAL, fee_rate REAL, fees_enabled INTEGER,
            neg_risk INTEGER, liquidity REAL, last_trade_price REAL,
            yes_token_id TEXT, no_token_id TEXT, meta_json TEXT, snapshot_json TEXT
        );
        CREATE TABLE IF NOT EXISTS pm_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, edge TEXT, scan_id INTEGER,
            market_id TEXT, side TEXT, p_model REAL, edge_after_costs REAL,
            effective_price REAL, lead_hours REAL, horizon_days REAL, meta_json TEXT
        );
        CREATE TABLE IF NOT EXISTS pm_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, edge TEXT, scan_id INTEGER,
            market_id TEXT, token_id TEXT, side TEXT, price REAL, size REAL,
            maker_or_taker TEXT, edge_size REAL, kelly_fraction REAL, status TEXT, meta_json TEXT
        );
        CREATE TABLE IF NOT EXISTS pm_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, edge TEXT, order_id INTEGER,
            market_id TEXT, token_id TEXT, side TEXT, price REAL, size REAL,
            maker_or_taker TEXT, fill_ts TEXT, pnl REAL, meta_json TEXT
        );
        CREATE TABLE IF NOT EXISTS pm_settlements (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, edge TEXT, market_id TEXT,
            condition_id TEXT, resolved_yes INTEGER, pnl REAL, meta_json TEXT
        );
        CREATE TABLE IF NOT EXISTS cv_gaps (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, polymarket_market_id TEXT,
            kalshi_market_id TEXT, question TEXT, pm_yes REAL, kalshi_yes REAL,
            gap REAL, net_of_fees REAL, snapshot_json TEXT
        );
        CREATE TABLE IF NOT EXISTS usud_quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, scan_id INTEGER,
            market_id TEXT, ticker TEXT, question TEXT, end_date TEXT,
            market_ask REAL, market_bid REAL, best_bid REAL, best_ask REAL, depth REAL,
            p_model REAL, spot REAL, iv REAL, prior_close REAL, tau_years REAL,
            buy_edge REAL, sell_edge REAL
        );
        CREATE TABLE IF NOT EXISTS calib_snapshots (
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, edge TEXT,
          brier_model REAL, brier_market REAL, reliability_maxdev_pp REAL,
          n_signals INTEGER, gate_pass INTEGER, detail_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_calib_edge_ts ON calib_snapshots(edge, ts);
        CREATE INDEX IF NOT EXISTS idx_usud_quotes_mkt ON usud_quotes(market_id);
        CREATE INDEX IF NOT EXISTS idx_pm_snaps_market ON pm_snapshots(market_id);
        CREATE INDEX IF NOT EXISTS idx_pm_orders_status ON pm_orders(status);
        CREATE INDEX IF NOT EXISTS idx_pm_fills_edge_mkt ON pm_fills(edge, market_id);
        CREATE INDEX IF NOT EXISTS idx_pm_sett_edge ON pm_settlements(edge);
        """
    )
    conn.commit()
    conn.close()
