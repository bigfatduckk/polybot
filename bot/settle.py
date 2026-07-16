import json
import sqlite3

import markets
from config import DB_PATH


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _fill_pnl(side, price, size, yes_won):
    if side == "buy":
        return (1.0 - price) * size if yes_won else (-price) * size
    return (price - 1.0) * size if yes_won else price * size


def _pending_markets(conn, edge):
    rows = conn.execute(
        """SELECT DISTINCT f.market_id, f.condition_id
           FROM pm_fills f
           LEFT JOIN pm_settlements s
             ON s.market_id = f.market_id AND s.edge = f.edge
           WHERE f.edge = ? AND s.id IS NULL""",
        (edge,),
    ).fetchall()
    return rows


def _fills_for(conn, edge, market_id):
    return conn.execute(
        "SELECT id, side, price, size FROM pm_fills WHERE edge=? AND market_id=?",
        (edge, market_id),
    ).fetchall()


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
        for f in _fills_for(conn, edge, r["market_id"]):
            pnl += _fill_pnl(f["side"], f["price"], f["size"], yes_won)
            n += 1
        _store_settlement(conn, edge, r["market_id"], r["condition_id"], yes_won, pnl,
                          {"outcome": outcome, "prices": prices, "fills": n})
        settled += 1
    conn.commit()
    conn.close()
    return settled
