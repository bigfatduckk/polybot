import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import (
    BOT_DIR,
    DB_PATH,
    PAPER_BANKROLL_ARB,
    PAPER_BANKROLL_FLB,
    PAPER_BANKROLL_USUD,
)

HKT = timezone(timedelta(hours=8))
PM_EDGES = ("flb", "arb", "usud")


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def format_report(conn):
    # `info` command. Weather removed 2026-07-26 (class closed) — shows FLB/ARB/
    # USUD open bets + edge PnL totals only.
    return format_open_all(conn) + "\n\n" + format_edge_totals(conn)

def format_totals(conn):
    return format_edge_totals(conn)


def format_pnl_both(paths=None):
    # Paper PnL for the Telegram `pnl` command. Bot B (weather-only climatology)
    # was removed 2026-07-26 when the weather class closed — only Bot A (FLB/ARB/
    # USUD) remains. Reads by explicit path so labels are correct regardless of
    # which instance the responder runs under.
    if paths is None:
        paths = [("A", BOT_DIR / "polymarket_bot.db")]
    sections = []
    for tag, path in paths:
        path = Path(path)
        if not path.exists():
            continue
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        try:
            sections.append(f"[{tag}] {format_totals(conn)}")
        finally:
            conn.close()
    return "\n".join(sections)


def _hkt_date(ts):
    try:
        return datetime.fromisoformat(ts).astimezone(HKT).date()
    except (ValueError, TypeError):
        return None


def _parse_date(s):
    if not s:
        return datetime.now(timezone.utc).astimezone(HKT).date()
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def format_open_all(conn):
    lines = ["=== open bets (all edges) ==="]
    try:
        prows = conn.execute(
            """SELECT f.id, f.edge, f.side, f.price, f.size,
               (SELECT s.question FROM pm_snapshots s
                WHERE s.market_id = f.market_id ORDER BY s.id DESC LIMIT 1) AS question,
               (SELECT s.end_date FROM pm_snapshots s
                WHERE s.market_id = f.market_id ORDER BY s.id DESC LIMIT 1) AS end_date
               FROM pm_fills f
               LEFT JOIN pm_settlements s
                 ON s.market_id = f.market_id AND s.edge = f.edge
               WHERE s.id IS NULL
               ORDER BY f.edge, f.id DESC"""
        ).fetchall()
    except sqlite3.OperationalError:
        prows = []
    by_edge = {}
    for r in prows:
        by_edge.setdefault(r["edge"], []).append(r)
    for edge in PM_EDGES:
        lines.append(f"-- {edge} --")
        rows = by_edge.get(edge, [])
        if not rows:
            lines.append("  (no open)")
        for r in rows:
            cost = r["price"] * r["size"]
            q = (r["question"] or "?")[:42]
            end = (r["end_date"] or "")[:10]
            lines.append(f"  #{r['id']} {q} {r['side']} @{r['price']:.3f} "
                         f"x{r['size']:.1f} cost=${cost:.2f} ends {end}")
    return "\n".join(lines)


def format_settled_day(conn, date_str):
    d = _parse_date(date_str)
    if d is None:
        return f"bad date '{date_str}': use YYYY-MM-DD"
    lines = [f"=== settled on {d.isoformat()} (HKT) ==="]

    try:
        prows = conn.execute(
            """SELECT s.edge, s.resolved_yes, s.pnl, s.ts,
               (SELECT q.question FROM pm_snapshots q
                WHERE q.market_id = s.market_id ORDER BY q.id DESC LIMIT 1) AS question
               FROM pm_settlements s
               ORDER BY s.ts DESC"""
        ).fetchall()
    except sqlite3.OperationalError:
        prows = []
    by_edge = {}
    for r in prows:
        if _hkt_date(r["ts"]) == d:
            by_edge.setdefault(r["edge"], []).append(r)
    for edge in PM_EDGES:
        lines.append(f"-- {edge} --")
        rows = by_edge.get(edge, [])
        if not rows:
            lines.append("  (none)")
        for r in rows:
            pnl = r["pnl"] or 0.0
            won = "Y" if r["resolved_yes"] else "N"
            q = (r["question"] or "?")[:42]
            lines.append(f"  {q} won={won} pnl=${pnl:+.2f}")
    return "\n".join(lines)


def _edge_line(name, open_n, settled_n, realized, bankroll):
    return (f"  {name:11s} open={open_n} settled={settled_n} realized=${realized:+.2f} "
            f"bankroll ${bankroll:.0f} + realized ${realized:+.2f} = ${bankroll + realized:.2f}")


def format_edge_totals(conn):
    lines = ["=== paper PnL (all edges) ==="]
    bank = {"flb": PAPER_BANKROLL_FLB, "arb": PAPER_BANKROLL_ARB, "usud": PAPER_BANKROLL_USUD}
    try:
        settled = {r["edge"]: (r["n"], r["pnl"]) for r in conn.execute(
            "SELECT edge, COUNT(*) AS n, COALESCE(SUM(pnl),0) AS pnl "
            "FROM pm_settlements GROUP BY edge"
        ).fetchall()}
        open_n = {r["edge"]: r["n"] for r in conn.execute(
            "SELECT f.edge, COUNT(*) AS n FROM pm_fills f "
            "LEFT JOIN pm_settlements s ON s.market_id=f.market_id AND s.edge=f.edge "
            "WHERE s.id IS NULL GROUP BY f.edge"
        ).fetchall()}
    except sqlite3.OperationalError:
        lines.append("  (edge tables not initialized)")
        return "\n".join(lines)
    for edge in ("flb", "arb", "usud"):
        s_n, s_pnl = settled.get(edge, (0, 0.0))
        o_n = open_n.get(edge, 0)
        lines.append(_edge_line(edge, o_n, s_n, s_pnl, bank[edge]))
    lines.append("  cross-venue: shelved (no fills)")
    return "\n".join(lines)


def main():
    conn = _connect()
    print(format_report(conn))
    conn.close()


if __name__ == "__main__":
    main()
