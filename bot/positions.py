import sqlite3

from config import (
    DB_PATH,
    PAPER_BANKROLL,
    PAPER_BANKROLL_ARB,
    PAPER_BANKROLL_FLB,
    PAPER_BANKROLL_USUD,
)


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def format_report(conn):
    lines = [f"paper bankroll ${PAPER_BANKROLL:.2f}  (simulated, no real funds)", ""]

    lines.append("=== open positions (unsettled bets) ===")
    open_rows = conn.execute(
        """
        SELECT f.id, o.city, o.market_date, f.side, f.price, f.size,
               (SELECT s.bucket_key FROM snapshots s WHERE s.market_id=f.market_id LIMIT 1) AS bucket
        FROM fills f
        JOIN orders o ON f.order_id = o.id
        WHERE f.market_id NOT IN (SELECT market_id FROM settlements)
        ORDER BY f.id DESC
        """
    ).fetchall()
    if not open_rows:
        lines.append("  (no open positions)")
    for r in open_rows:
        cost = r["price"] * r["size"]
        lines.append(f"  #{r['id']:>3} {r['city']:13s} {r['market_date']} {r['side']:4s} "
                     f"@{r['price']:.3f} x{r['size']:.1f}  cost=${cost:.2f}  bucket={r['bucket'] or '?'}")
    lines.append("")

    lines.append("=== settled (win/lose + pnl) ===")
    settled_rows = conn.execute(
        """
        SELECT st.city, st.date, st.bucket_key, st.resolved_yes, st.pnl
        FROM settlements st
        WHERE st.market_id IN (SELECT market_id FROM fills)
        ORDER BY st.id DESC
        LIMIT 15
        """
    ).fetchall()
    if not settled_rows:
        lines.append("  (none settled yet)")
    for r in settled_rows:
        pnl = r["pnl"] or 0.0
        verdict = "WIN " if pnl > 0 else ("LOSS" if pnl < 0 else "push")
        won = "Y" if r["resolved_yes"] else "N"
        lines.append(f"  {r['city']:13s} {r['date']} bucket={r['bucket_key'] or '?':12s} "
                     f"bucket_won={won}  pnl=${pnl:+.2f}  {verdict}")
    lines.append("")
    lines.append(format_totals(conn))

def format_totals(conn):
    return format_edge_totals(conn)


def _edge_line(name, open_n, settled_n, realized, bankroll):
    return (f"  {name:11s} open={open_n} settled={settled_n} realized=${realized:+.2f} "
            f"bankroll ${bankroll:.0f} + realized ${realized:+.2f} = ${bankroll + realized:.2f}")


def format_edge_totals(conn):
    lines = ["=== paper PnL (all edges) ==="]
    try:
        w_total = conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
        w_settled = conn.execute(
            "SELECT COUNT(*) FROM fills WHERE market_id IN (SELECT market_id FROM settlements)"
        ).fetchone()[0]
        w_realized = conn.execute(
            "SELECT COALESCE(SUM(pnl),0) FROM settlements WHERE market_id IN (SELECT market_id FROM fills)"
        ).fetchone()[0]
        lines.append(_edge_line("weather", w_total - w_settled, w_settled, w_realized, PAPER_BANKROLL))
    except sqlite3.OperationalError:
        lines.append("  weather:     (fills table not initialized)")

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
