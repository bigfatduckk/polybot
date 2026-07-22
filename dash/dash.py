"""Polymarket bot live dashboard — read-only Flask API over the bot's three sqlite DBs.

Watch-only. Every connection is opened mode=ro (WAL-safe; reads never block the bot's writes).
The dash user has read perm on the .db files and cannot read /root/polybot/.env.
"""
import os
import sqlite3
from datetime import datetime, timezone

# Absolute paths to the three bot DBs. Override via env at deploy time.
_BOT_DIR_DEFAULT = "/root/polybot/bot"
PAPER_A_DB = os.environ.get("PAPER_A_DB", f"{_BOT_DIR_DEFAULT}/polymarket_bot.db")
PAPER_B_DB = os.environ.get("PAPER_B_DB", f"{_BOT_DIR_DEFAULT}/polymarket_bot_B.db")
LIVE_DB = os.environ.get("LIVE_DB", f"{_BOT_DIR_DEFAULT}/polymarket_bot_live.db")

STALE_AFTER_SEC = 20  # 2x the 10s poll interval -> UI grey dot when exceeded


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ro_conn(path):
    """Open a sqlite DB read-only. Any write raises sqlite3.OperationalError."""
    from pathlib import Path
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def conn_a():
    return ro_conn(PAPER_A_DB)


def conn_b():
    return ro_conn(PAPER_B_DB)


def conn_live():
    return ro_conn(LIVE_DB)


def clamp_int(value, lo, hi, default):
    """Clamp a ?days/?hours-style param to [lo,hi]; fall back to default on garbage."""
    if value is None:
        return default
    try:
        n = int(str(value))
    except (ValueError, TypeError):
        return default
    return max(lo, min(hi, n))


# Task 2 appends store_calib helpers here when reused; otherwise standalone.
# Task 3+ appends Flask app + endpoint handlers below.
