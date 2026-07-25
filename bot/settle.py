import json
import sqlite3

import markets
from config import DB_PATH


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _fill_pnl(side, price, size, yes_won, fee_rate=0.0, fees_enabled=False):
    # P4: price the taker fee into realized PnL. settle previously recomputed
    # gross PnL from fill price/size and ignored the fee the edge calc had
    # subtracted — realized PnL was an upper bound. Fee model matches the edge
    # gate (_fee = fee_rate * p * (1-p)) so edge assumption and realized PnL use
    # the same fee.
    if side == "buy":
        gross = (1.0 - price) * size if yes_won else (-price) * size
    else:
        gross = (price - 1.0) * size if yes_won else price * size
    fee = fee_rate * price * (1.0 - price) * size if fees_enabled else 0.0
    return gross - fee


def _pending_markets(conn, edge):
    rows = conn.execute(
        """SELECT DISTINCT f.market_id,
           (SELECT snap.condition_id FROM pm_snapshots snap
            WHERE snap.market_id = f.market_id
              AND snap.condition_id IS NOT NULL AND snap.condition_id != ''
            ORDER BY snap.id DESC LIMIT 1) AS condition_id
           FROM pm_fills f
           LEFT JOIN pm_settlements st
             ON st.market_id = f.market_id AND st.edge = f.edge
           WHERE f.edge = ? AND st.id IS NULL""",
        (edge,),
    ).fetchall()
    return rows


def _fills_for(conn, edge, market_id):
    return conn.execute(
        "SELECT id, side, price, size FROM pm_fills WHERE edge=? AND market_id=?",
        (edge, market_id),
    ).fetchall()


def _market_fee(conn, edge, market_id):
    # fee_rate is set at market creation and is stable; latest snapshot carries it.
    row = conn.execute(
        "SELECT fee_rate, fees_enabled FROM pm_snapshots "
        "WHERE edge=? AND market_id=? AND fee_rate IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (edge, market_id),
    ).fetchone()
    if not row:
        return 0.0, False
    return float(row["fee_rate"] or 0.0), bool(row["fees_enabled"])


def _store_settlement(conn, edge, market_id, condition_id, yes_won, pnl, meta):
    conn.execute(
        """INSERT INTO pm_settlements(ts, edge, market_id, condition_id,
           resolved_yes, pnl, meta_json) VALUES (?,?,?,?,?,?,?)""",
        (_now_iso(), edge, market_id, condition_id, int(yes_won), pnl,
         json.dumps(meta, default=str)),
    )


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sweep_resolutions(edge):
    conn = _connect()
    markets.init_edge_db()
    settled = 0
    for r in _pending_markets(conn, edge):
        closed, outcome, prices = markets.fetch_resolution(r["market_id"])
        if not closed or outcome == "none":
            continue
        yes_won = outcome == "yes"
        pnl = 0.0
        n = 0
        fee_rate, fees_enabled = _market_fee(conn, edge, r["market_id"])
        for f in _fills_for(conn, edge, r["market_id"]):
            pnl += _fill_pnl(f["side"], f["price"], f["size"], yes_won,
                             fee_rate, fees_enabled)
            n += 1
        _store_settlement(conn, edge, r["market_id"], r["condition_id"], yes_won, pnl,
                          {"outcome": outcome, "prices": prices, "fills": n})
        settled += 1
    conn.commit()
    conn.close()
    return settled
