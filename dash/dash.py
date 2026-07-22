"""Polymarket bot live dashboard — read-only Flask API over the bot's three sqlite DBs.

Watch-only. Every connection is opened mode=ro (WAL-safe; reads never block the bot's writes).
The dash user has read perm on the .db files and cannot read /root/polybot/.env.
"""
import os
import sqlite3
from datetime import datetime, timezone, timedelta

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


def _pnl_rows(conn_fn, table):
    """All realized pnl rows (ts, pnl, resolved_yes) from one table, read-only."""
    rows, err = _query(conn_fn, f"SELECT ts, pnl, resolved_yes FROM {table} WHERE pnl IS NOT NULL")
    return [(r["ts"], r["pnl"], r["resolved_yes"]) for r in rows], err


def _hkt_day(ts):
    from datetime import datetime
    try:
        then = datetime.fromisoformat(ts)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return (then.astimezone(timezone(timedelta(hours=8)))).date().isoformat()
    except Exception:
        return None


def _equity_points(days):
    """Windowed cumulative realized PnL by HKT day, per instance. Trailing `days` window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    series = {}
    for name, fn, tables in [
        ("A", conn_a, ["settlements", "pm_settlements"]),
        ("B", conn_b, ["settlements"]),
        ("LIVE", conn_live, ["live_settlements"]),
    ]:
        by_day = {}
        for tbl in tables:
            rows, _ = _pnl_rows(fn, tbl)
            for ts, pnl, _rv in rows:
                if ts < cutoff:
                    continue
                day = _hkt_day(ts)
                if day is None:
                    continue
                by_day[day] = by_day.get(day, 0.0) + (pnl or 0.0)
        ordered = sorted(by_day.items())
        cum = 0.0
        pts = []
        for day, pnl in ordered:
            cum += pnl
            pts.append({"day": day, "pnl": round(pnl, 4), "cum": round(cum, 4)})
        series[name] = pts
    return series


@app.get("/api/equity")
def api_equity():
    days = clamp_int(request.args.get("days"), 1, 365, 30)
    return jsonify({"ts": now_iso(), "series": _equity_points(days)})


@app.get("/api/daily-pnl")
def api_daily_pnl():
    days = clamp_int(request.args.get("days"), 1, 365, 30)
    series = _equity_points(days)
    out = {name: [{"day": p["day"], "pnl": p["pnl"]} for p in pts] for name, pts in series.items()}
    return jsonify({"ts": now_iso(), "series": out})


@app.get("/api/drawdown")
def api_drawdown():
    days = clamp_int(request.args.get("days"), 1, 365, 30)
    series = _equity_points(days)
    dd = {}
    for name, pts in series.items():
        cummax = 0.0
        rows = []
        for p in pts:
            cummax = max(cummax, p["cum"])
            rows.append({"day": p["day"], "dd": round(cummax - p["cum"], 4)})
        dd[name] = rows
    return jsonify({"ts": now_iso(), "series": dd})


@app.get("/api/edge-pnl")
def api_edge_pnl():
    edges = []
    wrows, _ = _query(conn_a, "SELECT COALESCE(SUM(pnl),0) AS s, COUNT(*) AS n FROM settlements WHERE pnl IS NOT NULL")
    if wrows:
        edges.append({"edge": "weather", "instance": "A", "pnl": round(wrows[0]["s"], 4), "n": wrows[0]["n"]})
    brows, _ = _query(conn_b, "SELECT COALESCE(SUM(pnl),0) AS s, COUNT(*) AS n FROM settlements WHERE pnl IS NOT NULL")
    if brows:
        edges.append({"edge": "weather", "instance": "B", "pnl": round(brows[0]["s"], 4), "n": brows[0]["n"]})
    erows, _ = _query(conn_a, "SELECT edge, COALESCE(SUM(pnl),0) AS s, COUNT(*) AS n FROM pm_settlements WHERE pnl IS NOT NULL GROUP BY edge")
    for r in erows:
        edges.append({"edge": r["edge"], "instance": "A", "pnl": round(r["s"], 4), "n": r["n"]})
    lrows, _ = _query(conn_live, "SELECT COALESCE(SUM(pnl),0) AS s, COUNT(*) AS n FROM live_settlements WHERE pnl IS NOT NULL")
    if lrows:
        edges.append({"edge": "live", "instance": "LIVE", "pnl": round(lrows[0]["s"], 4), "n": lrows[0]["n"]})
    return jsonify({"ts": now_iso(), "edges": edges})


@app.get("/api/winrate")
def api_winrate():
    edges = []
    def _wr(label, fn, sql):
        rows, _ = _query(fn, sql)
        if rows:
            r = rows[0]
            total = r["n"]
            won = r["won"]
            edges.append({"edge": label, "won": won, "total": total,
                          "rate": round(won / total, 4) if total else None})
    _wr("weather", conn_a, "SELECT COUNT(*) AS n, SUM(resolved_yes) AS won FROM settlements WHERE pnl IS NOT NULL")
    _wr("weatherB", conn_b, "SELECT COUNT(*) AS n, SUM(resolved_yes) AS won FROM settlements WHERE pnl IS NOT NULL")
    erows, _ = _query(conn_a, "SELECT edge, COUNT(*) AS n, SUM(resolved_yes) AS won FROM pm_settlements WHERE pnl IS NOT NULL GROUP BY edge")
    for r in erows:
        edges.append({"edge": r["edge"], "won": r["won"], "total": r["n"],
                      "rate": round(r["won"] / r["n"], 4) if r["n"] else None})
    _wr("live", conn_live, "SELECT COUNT(*) AS n, SUM(resolved_yes) AS won FROM live_settlements WHERE pnl IS NOT NULL")
    return jsonify({"ts": now_iso(), "edges": edges})
