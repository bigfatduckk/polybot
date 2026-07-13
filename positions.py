import sqlite3

from config import DB_PATH, PAPER_BANKROLL


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
        LIMIT 50
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

    total_fills = conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    open_n = conn.execute(
        "SELECT COUNT(*) FROM fills WHERE market_id NOT IN (SELECT market_id FROM settlements)"
    ).fetchone()[0]
    settled_n = total_fills - open_n
    realized = conn.execute(
        "SELECT COALESCE(SUM(pnl), 0) FROM settlements WHERE market_id IN (SELECT market_id FROM fills)"
    ).fetchone()[0]
    lines.append("=== totals ===")
    lines.append(f"  open: {open_n}   settled: {settled_n}")
    lines.append(f"  realized PnL (settled): ${realized:+.2f}")
    lines.append(f"  paper bankroll: ${PAPER_BANKROLL:.2f} + realized ${realized:+.2f} "
                 f"= ${PAPER_BANKROLL + realized:.2f}")

    return "\n".join(lines)


def main():
    conn = _connect()
    print(format_report(conn))
    conn.close()


if __name__ == "__main__":
    main()
