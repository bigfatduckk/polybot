import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config import (
    CONSECUTIVE_LOSS_HALT,
    DAILY_LOSS_HALT_FRAC,
    DB_PATH,
    HALT_FILE,
    MIN_EDGE,
    PAPER_BANKROLL_FLB,
    PAPER_BANKROLL_ARB,
    PAPER_BANKROLL_USUD,
    PER_TRADE_CAP_ABS,
    PER_TRADE_CAP_FRAC,
    PRICE_BAND,
)

MODE = "paper"

BANKROLLS = {"flb": PAPER_BANKROLL_FLB, "arb": PAPER_BANKROLL_ARB, "usud": PAPER_BANKROLL_USUD}


def set_mode(m):
    global MODE
    MODE = m


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_scan_id(edge):
    conn = _connect()
    key = f"scan_counter_{edge}"
    row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    n = int(row["v"]) if row else 0
    n += 1
    conn.execute(
        "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, str(n)),
    )
    conn.commit()
    conn.close()
    return n


@dataclass
class EdgeOrder:
    edge: str
    market_id: str
    token_id: str
    side: str
    price: float
    size: float
    maker_or_taker: str
    edge_size: float
    kelly_fraction: float
    meta: dict = field(default_factory=dict)


@dataclass
class EdgeState:
    edge: str
    bankroll: float
    realized_pnl_today: float
    consecutive_losses: int
    open_markets: set


def _today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_edge_state(edge):
    conn = _connect()
    bankroll = float(
        conn.execute("SELECT v FROM meta WHERE k=?", (f"bankroll_{edge}",)).fetchone()
        and conn.execute("SELECT v FROM meta WHERE k=?", (f"bankroll_{edge}",)).fetchone()["v"]
        or BANKROLLS.get(edge, 0.0)
    )
    pnl_today = float(conn.execute(
        "SELECT COALESCE(SUM(pnl),0) AS p FROM pm_settlements "
        "WHERE edge=? AND substr(ts,1,10)=?", (edge, _today_utc()),
    ).fetchone()["p"] or 0.0)
    rows = conn.execute(
        "SELECT pnl FROM pm_settlements WHERE edge=? ORDER BY id DESC LIMIT 20",
        (edge,),
    ).fetchall()
    losses = 0
    for r in rows:
        if (r["pnl"] or 0) < 0:
            losses += 1
        else:
            break
    open_mk = {
        r["market_id"] for r in conn.execute(
            "SELECT DISTINCT f.market_id FROM pm_fills f LEFT JOIN pm_settlements s "
            "ON s.market_id=f.market_id AND s.edge=f.edge "
            "WHERE f.edge=? AND s.id IS NULL", (edge,),
        ).fetchall()
    }
    conn.close()
    return EdgeState(edge=edge, bankroll=bankroll, realized_pnl_today=pnl_today,
                     consecutive_losses=losses, open_markets=open_mk)


def edge_propose(candidates, state):
    orders = []
    for c in candidates:
        q = c["effective_price"]
        p = c["p_model"]
        side = c["side"]
        if q <= 0.0 or q >= 1.0:
            continue
        if side == "buy":
            f = (p - q) / (1 - q)
        else:
            f = (q - p) / q
        f = max(0.0, f) / 4.0
        notional = min(f * state.bankroll, PER_TRADE_CAP_FRAC * state.bankroll,
                       PER_TRADE_CAP_ABS)
        if notional <= 0.0:
            continue
        shares = notional / q
        maker = (c["edge_after_costs"] >= MIN_EDGE + 0.02 and c.get("horizon_days", 0) > 0.25)
        orders.append(EdgeOrder(
            edge=state.edge, market_id=c["market_id"], token_id=c["token_id"],
            side=side, price=q, size=shares, maker_or_taker=("maker" if maker else "taker"),
            edge_size=c["edge_after_costs"], kelly_fraction=f * 4,
            meta={"notional": notional, "scan_id": c.get("scan_id")},
        ))
    return orders


def edge_risk_check(order, state):
    if os.path.exists(HALT_FILE):
        return False, "halt file present", {}
    if not (PRICE_BAND[0] <= order.price <= PRICE_BAND[1]):
        return False, f"price {order.price} outside band {PRICE_BAND}", {}
    notional = order.price * order.size
    cap = min(PER_TRADE_CAP_FRAC * state.bankroll, PER_TRADE_CAP_ABS)
    if notional > cap + 1e-9:
        return False, f"per-trade cap exceeded {notional}>{cap}", {"per_trade_notional": cap}
    if state.realized_pnl_today <= -DAILY_LOSS_HALT_FRAC * state.bankroll:
        return False, "daily loss halt", {}
    if state.consecutive_losses >= CONSECUTIVE_LOSS_HALT:
        return False, "consecutive loss halt", {}
    if order.market_id in state.open_markets:
        return False, "one position per market", {}
    return True, "approved", {"per_trade_notional": min(notional, cap)}


def _store_order(conn, order):
    cur = conn.execute(
        """INSERT INTO pm_orders(ts, edge, scan_id, market_id, token_id, side, price, size,
           maker_or_taker, edge_size, kelly_fraction, status, meta_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (_now_iso(), order.edge, order.meta.get("scan_id", 0), order.market_id,
         order.token_id, order.side, order.price, order.size, order.maker_or_taker,
         order.edge_size, order.kelly_fraction, "pending",
         json.dumps(order.meta, default=str)),
    )
    conn.commit()
    return cur.lastrowid


def _store_fill(conn, order_id, order, pnl=0.0):
    conn.execute(
        """INSERT INTO pm_fills(ts, edge, order_id, market_id, token_id, side, price, size,
           maker_or_taker, fill_ts, pnl, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (_now_iso(), order.edge, order_id, order.market_id, order.token_id, order.side,
         order.price, order.size, order.maker_or_taker, _now_iso(), pnl,
         json.dumps({"synthetic_short": order.side == "sell"}, default=str)),
    )
    conn.execute("UPDATE pm_orders SET status='filled' WHERE id=?", (order_id,))
    conn.commit()


def edge_execute(order, verdict):
    if not verdict[0]:
        return None
    if MODE == "real":
        raise NotImplementedError("real execution is Milestone 4")
    conn = _connect()
    order_id = _store_order(conn, order)
    _store_fill(conn, order_id, order)
    conn.close()
    return order_id


def _new_scan(edge):
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO pm_snapshots(ts, edge, meta_json) VALUES (?,?,?)",
        (_now_iso(), edge, "{}"),
    )
    conn.commit()
    sid = cur.lastrowid
    conn.close()
    return sid


def store_snapshot(edge, m, meta=None):
    conn = _connect()
    conn.execute(
        """INSERT INTO pm_snapshots(ts, edge, market_id, event_id, condition_id, question,
           end_date, best_bid, best_ask, bid_size, ask_size, depth, tick_size,
           min_order_size, fee_rate, fees_enabled, neg_risk, liquidity, last_trade_price,
           yes_token_id, no_token_id, meta_json, snapshot_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (_now_iso(), edge, m.market_id, m.event_id, m.condition_id, m.question, m.end_date,
         m.best_bid, m.best_ask, m.bid_size, m.ask_size, m.depth, m.tick_size,
         m.min_order_size, m.fee_rate, int(m.fees_enabled), int(m.neg_risk),
         m.liquidity, m.last_trade_price, m.yes_token_id, m.no_token_id,
         json.dumps(meta or {}, default=str), json.dumps(m.raw, default=str)),
    )
    conn.commit()
    conn.close()


def store_candidate(edge, scan_id, c):
    conn = _connect()
    conn.execute(
        """INSERT INTO pm_candidates(ts, edge, scan_id, market_id, side, p_model,
           edge_after_costs, effective_price, lead_hours, horizon_days, meta_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (_now_iso(), edge, scan_id, c["market_id"], c["side"], c["p_model"],
         c["edge_after_costs"], c["effective_price"], c.get("lead_hours", 0.0),
         c.get("horizon_days", 0.0), json.dumps(c.get("meta", {}), default=str)),
    )
    conn.commit()
    conn.close()
