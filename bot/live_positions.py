"""Live PnL/health formatters for the Telegram `pnl live` / `live` / `opens
live` commands. Reads the live DB only (by explicit path, like
positions.format_pnl_both). No SDK, no paper DB access. Degrades gracefully
if the live DB is not initialized yet.
"""
import os
import sqlite3
from pathlib import Path

import config
from config import LIVE_BANKROLL, LIVE_DB_PATH


def _connect(path=None):
    p = Path(path or LIVE_DB_PATH)
    if not p.exists():
        return None
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    return conn


def _safe(conn, q, args=()):
    try:
        return conn.execute(q, args).fetchall()
    except sqlite3.OperationalError:
        return []


def format_live_pnl(path=None):
    conn = _connect(path)
    if conn is None:
        return "[A-LIVE] live DB not initialized yet (no live tick has run)"
    try:
        open_rows = _safe(conn,
            """SELECT o.id, o.city, o.market_date, o.signal_side, o.price, o.size,
               o.status, o.exec_token_id
               FROM live_orders o
               WHERE o.status IN ('posted','open','partial','filled')
               ORDER BY o.id DESC""")
        settled_rows = _safe(conn,
            """SELECT city, date, bucket_key, resolved_yes, pnl, ts
               FROM live_settlements ORDER BY id DESC LIMIT 10""")
        n_fills = 0
        try:
            n_fills = conn.execute("SELECT COUNT(*) FROM live_fills").fetchone()[0]
        except sqlite3.OperationalError:
            pass
        n_set = 0
        realized = 0.0
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(pnl),0) AS p FROM live_settlements"
            ).fetchone()
            n_set, realized = row["n"], float(row["p"] or 0.0)
        except sqlite3.OperationalError:
            pass
        lines = [f"[A-LIVE] live bankroll ${LIVE_BANKROLL:.0f}  (REAL funds — edge validation)", ""]
        lines.append("=== open positions ===")
        if not open_rows:
            lines.append("  (no open positions)")
        open_cost = 0.0
        for r in open_rows:
            cost = (r["price"] or 0.0) * (r["size"] or 0.0)
            open_cost += cost
            lines.append(
                f"  #{r['id']:>3} {r['city'] or '?':13s} {r['market_date'] or '?'} "
                f"{r['signal_side']:4s} @{(r['price'] or 0):.3f} x{(r['size'] or 0):.0f} "
                f"cost=${cost:.2f} [{r['status']}]")
        lines.append("")
        lines.append("=== settled (last 10) ===")
        if not settled_rows:
            lines.append("  (none settled yet)")
        for r in settled_rows:
            pnl = r["pnl"] or 0.0
            verdict = "WIN " if pnl > 0 else ("LOSS" if pnl < 0 else "push")
            won = "Y" if r["resolved_yes"] else "N"
            lines.append(
                f"  {r['city'] or '?':13s} {r['date'] or '?'} bucket={r['bucket_key'] or '?':12s} "
                f"won={won} pnl=${pnl:+.2f} {verdict}")
        lines.append("")
        lines.append(
            f"  realized=${realized:+.2f}  open_cost=${open_cost:.2f}  "
            f"fills={n_fills} settled={n_set}  bankroll+realized=${LIVE_BANKROLL + realized:.2f}")
        return "\n".join(lines)
    finally:
        conn.close()


def format_live_open(path=None):
    conn = _connect(path)
    if conn is None:
        return "[A-LIVE] live DB not initialized yet"
    try:
        rows = _safe(conn,
            """SELECT id, city, market_date, signal_side, price, size, status
               FROM live_orders WHERE status IN ('posted','open','partial','filled')
               ORDER BY id DESC""")
        lines = ["[A-LIVE] === open live bets ==="]
        if not rows:
            lines.append("  (no open)")
        for r in rows:
            cost = (r["price"] or 0.0) * (r["size"] or 0.0)
            lines.append(
                f"  #{r['id']} {r['city'] or '?'} {r['market_date'] or '?'} "
                f"{r['signal_side']} @{(r['price'] or 0):.3f} x{(r['size'] or 0):.0f} "
                f"cost=${cost:.2f} [{r['status']}]")
        return "\n".join(lines)
    finally:
        conn.close()


def format_live_health(path=None):
    """One-glance live-arm health for the Telegram `live` command.
    Reads only the live DB + filesystem (HALT flag) + env (DRY_RUN). No SDK.
    """
    dry = os.environ.get(config.LIVE_DRY_RUN_ENV, "1") != "0"
    halt = os.path.exists(config.HALT_LIVE_FILE)
    conn = _connect(path)
    if conn is None:
        return (f"[A-LIVE] live DB not initialized yet | "
                f"HALT={'yes' if halt else 'no'} DRY_RUN={'on' if dry else 'OFF'}")
    try:
        def _count(q, args=()):
            try:
                return conn.execute(q, args).fetchone()[0]
            except sqlite3.OperationalError:
                return 0
        open_n = _count(
            "SELECT COUNT(*) FROM live_orders WHERE status IN ('posted','open','partial','filled')")
        dry_signed = _count(
            "SELECT COUNT(*) FROM live_orders WHERE dry_run=1 AND status NOT IN ('rejected')")
        rejected = _count("SELECT COUNT(*) FROM live_orders WHERE status='rejected'")
        settled_n = _count("SELECT COUNT(*) FROM live_settlements")
        realized = _count("SELECT COALESCE(SUM(pnl),0) FROM live_settlements")
        bal = _safe(conn,
                    "SELECT usdc, matic, ts FROM live_balances ORDER BY id DESC LIMIT 1")
        tick = _safe(conn, "SELECT ts, job, note FROM live_ticks ORDER BY id DESC LIMIT 1")
        lines = [f"[A-LIVE] HALT={'yes' if halt else 'no'} DRY_RUN={'on' if dry else 'OFF'}"]
        lines.append(
            f"open={open_n} dry_signed={dry_signed} rejected={rejected} "
            f"settled={settled_n} realized=${float(realized):+.2f}")
        if bal:
            lines.append(
                f"gas={bal[0]['matic']:.1f} POL usdc=${bal[0]['usdc']:.2f} "
                f"({(bal[0]['ts'] or '?')[:16]}Z)")
        else:
            lines.append("gas=— usdc=— (no balance row yet)")
        if tick:
            t = tick[0]
            lines.append(f"last_tick: {(t['ts'] or '?')[:19]}Z {t['job']} {t['note']}")
        else:
            lines.append("last_tick: (none)")
        return "\n".join(lines)
    finally:
        conn.close()


def format_live_ticks(n=10, path=None):
    """Last N live_ticks rows (evaluated/gated/skip history) for `ticks [N]`."""
    conn = _connect(path)
    if conn is None:
        return "[A-LIVE] live DB not initialized yet"
    try:
        rows = _safe(conn,
                     "SELECT ts, job, note FROM live_ticks ORDER BY id DESC LIMIT ?",
                     (int(n),))
        lines = [f"[A-LIVE] === last {len(rows)} live ticks ==="]
        if not rows:
            lines.append("  (none)")
        for r in rows:
            lines.append(f"  {(r['ts'] or '?')[:19]}Z {r['job']} {r['note']}")
        return "\n".join(lines)
    finally:
        conn.close()


def format_live_gas(path=None):
    """Last 3 live_balances rows (gas/usdc trend) for `gas`."""
    conn = _connect(path)
    if conn is None:
        return "[A-LIVE] live DB not initialized yet"
    try:
        rows = _safe(conn,
                     "SELECT ts, usdc, matic, source FROM live_balances "
                     "ORDER BY id DESC LIMIT 3")
        lines = ["[A-LIVE] === last 3 balance reads ==="]
        if not rows:
            lines.append("  (no balance rows yet)")
        for r in rows:
            lines.append(
                f"  {(r['ts'] or '?')[:19]}Z gas={r['matic']:.1f} POL "
                f"usdc=${r['usdc']:.2f} [{r['source']}]")
        return "\n".join(lines)
    finally:
        conn.close()
