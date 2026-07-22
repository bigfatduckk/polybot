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
import time
from flask import Flask, jsonify, request

app = Flask(__name__)


def _query(conn_fn, sql, args=()):
    """Run sql read-only; retry once on WAL lock; never raise. Returns (rows, error)."""
    try:
        conn = conn_fn()
    except sqlite3.Error as e:
        return [], f"db-unreachable: {e}"
    try:
        try:
            rows = conn.execute(sql, args).fetchall()
            return rows, None
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                time.sleep(0.1)
                rows = conn.execute(sql, args).fetchall()
                return rows, None
            return [], f"query-error: {e}"
    except Exception as e:
        return [], f"query-error: {e}"
    finally:
        conn.close()


def _row(r):
    return dict(r) if r is not None else None


def _latest_halt_halted(conn_fn):
    rows, err = _query(conn_fn, "SELECT reason FROM live_halts ORDER BY id DESC LIMIT 1")
    if err or not rows:
        return False, err
    reason = (rows[0]["reason"] or "").lower()
    return ("clear" not in reason and "unhalt" not in reason), None


def _last_tick_age(conn_fn, table="live_ticks"):
    rows, err = _query(conn_fn, f"SELECT MAX(ts) AS m FROM {table}")
    if err or not rows or rows[0]["m"] is None:
        return None
    from datetime import datetime
    try:
        then = datetime.fromisoformat(rows[0]["m"])
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - then).total_seconds())
    except Exception:
        return None


@app.get("/api/health")
def api_health():
    out = {"ts": now_iso(), "instances": {}}

    # paper A
    rows, _ = _query(conn_a, "SELECT v FROM meta WHERE k='bankroll'")
    a_bank = float(rows[0]["v"]) if rows and rows[0]["v"] else None
    a_age = _last_tick_age(conn_a, "scans")  # paper uses scans as its tick log
    out["instances"]["A"] = {
        "status": "running", "bankroll": a_bank, "gas": None, "pusd": None,
        "last_tick_age": a_age, "halted": False,
    }

    # paper B
    rows, _ = _query(conn_b, "SELECT v FROM meta WHERE k='bankroll'")
    b_bank = float(rows[0]["v"]) if rows and rows[0]["v"] else None
    out["instances"]["B"] = {
        "status": "running", "bankroll": b_bank, "gas": None, "pusd": None,
        "last_tick_age": _last_tick_age(conn_b, "scans"), "halted": False,
    }

    # live
    halted, _ = _latest_halt_halted(conn_live)
    rows, _ = _query(conn_live, "SELECT usdc, matic FROM live_balances ORDER BY id DESC LIMIT 1")
    gas = pusd = None
    if rows:
        pusd = rows[0]["usdc"]
        gas = rows[0]["matic"]
    out["instances"]["LIVE"] = {
        "status": "halted" if halted else "running",
        "bankroll": 200.0, "gas": gas, "pusd": pusd,
        "last_tick_age": _last_tick_age(conn_live), "halted": halted,
    }
    return jsonify(out)
