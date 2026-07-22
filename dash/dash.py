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
    """Open a sqlite DB read-only. Any write raises sqlite3.OperationalError.

    immutable=1: the bot's crons are transient, so on close sqlite checkpoints
    and removes the -shm/-wal files; a plain mode=ro open then fails with
    "attempt to write a readonly database" (sqlite tries to recreate -shm, which
    ro forbids). immutable=1 reads the checkpointed main .db with no -shm needed
    -> accurate between bot runs (the common case), at most seconds-stale during a
    brief cron run. Reads never block or touch the bot's writes."""
    from pathlib import Path
    uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
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
from flask import Flask, jsonify, request, send_from_directory

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


def _cutoff_ts(hours):
    # ponytail: timedelta is a module global; windowing matches Task 4 _equity_points
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def _redact(s):
    """Redact an address/market id to a short prefix — no secrets in JSON."""
    if not s:
        return None
    s = str(s)
    return s[:8] + "…" if len(s) > 12 else s


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


_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

@app.get("/")
def index():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "index.html")

@app.get("/static/<path:path>")
def static_files(path):
    return send_from_directory(_STATIC_DIR, path)

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
    halted, halt_err = _latest_halt_halted(conn_live)
    rows, bal_err = _query(conn_live, "SELECT usdc, matic FROM live_balances ORDER BY id DESC LIMIT 1")
    gas = pusd = None
    if rows:
        pusd = rows[0]["usdc"]
        gas = rows[0]["matic"]
    live_err = bal_err or halt_err
    if live_err:
        out["instances"]["LIVE"] = {
            "status": "unreachable", "bankroll": None, "gas": None, "pusd": None,
            "last_tick_age": None, "halted": False, "error": live_err,
        }
    else:
        out["instances"]["LIVE"] = {
            "status": "halted" if halted else "running",
            "bankroll": pusd, "gas": gas, "pusd": pusd,
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


@app.get("/api/feed")
def api_feed():
    limit = clamp_int(request.args.get("limit"), 1, 500, 50)
    rows, err = _query(conn_live,
        "SELECT ts, job, note, detail_json FROM live_ticks ORDER BY id DESC LIMIT ?", (limit,))
    out = []
    for r in rows or []:
        out.append({"ts": r["ts"], "instance": "LIVE", "job": r["job"],
                    "action": (r["note"] or "")[:40], "note": r["note"],
                    "detail": r["detail_json"]})
    return jsonify({"ts": now_iso(), "rows": out, "error": err})


@app.get("/api/positions")
def api_positions():
    rows, err = _query(conn_live,
        """SELECT id, ts, market_id, city, market_date, signal_side, price, size,
                  status, dry_run FROM live_orders
           WHERE status IN ('posted','open','partial','filled') ORDER BY id DESC""")
    out = []
    for r in rows or []:
        cost = (r["price"] or 0) * (r["size"] or 0)
        out.append({"instance": "LIVE", "id": r["id"], "market_id": _redact(r["market_id"]),
                    "city": r["city"], "date": r["market_date"], "side": r["signal_side"],
                    "price": r["price"], "size": r["size"], "cost": round(cost, 2),
                    "status": r["status"], "dry_run": bool(r["dry_run"])})
    # paper A open orders (non-live)
    arows, _ = _query(conn_a, "SELECT market_id, side, price, size, status FROM orders WHERE status IN ('posted','open','partial','filled') ORDER BY id DESC LIMIT 50")
    for r in arows or []:
        out.append({"instance": "A", "market_id": _redact(r["market_id"]), "side": r["side"],
                    "price": r["price"], "size": r["size"], "status": r["status"], "dry_run": True})
    return jsonify({"ts": now_iso(), "rows": out})


@app.get("/api/candidates")
def api_candidates():
    limit = clamp_int(request.args.get("limit"), 1, 500, 50)
    wrows, _ = _query(conn_a,
        "SELECT ts, market_id, side, p_model, edge_after_costs, market_mid FROM candidates ORDER BY id DESC LIMIT ?", (limit,))
    aorders, _ = _query(conn_a, "SELECT DISTINCT market_id FROM orders")
    ordered_mkts = {r["market_id"] for r in aorders or []}
    out = []
    for r in wrows or []:
        out.append({"instance": "A", "edge": "weather", "ts": r["ts"],
                    "market_id": _redact(r["market_id"]), "side": r["side"],
                    "p_model": r["p_model"], "mkt_price": r["market_mid"],
                    "edge_val": r["edge_after_costs"],
                    "became_order": r["market_id"] in ordered_mkts})
    erows, _ = _query(conn_a,
        "SELECT ts, edge, market_id, side, p_model, edge_after_costs FROM pm_candidates ORDER BY id DESC LIMIT ?", (limit,))
    eorders, _ = _query(conn_a, "SELECT DISTINCT market_id FROM pm_orders")
    e_mkts = {r["market_id"] for r in eorders or []}
    for r in erows or []:
        out.append({"instance": "A", "edge": r["edge"], "ts": r["ts"],
                    "market_id": _redact(r["market_id"]), "side": r["side"],
                    "p_model": r["p_model"], "mkt_price": None,
                    "edge_val": r["edge_after_costs"],
                    "became_order": r["market_id"] in e_mkts})
    return jsonify({"ts": now_iso(), "rows": out})


@app.get("/api/rejections")
def api_rejections():
    hours = clamp_int(request.args.get("hours"), 1, 168, 24)
    rows, err = _query(conn_live,
        "SELECT note, COUNT(*) AS c FROM live_ticks WHERE note LIKE 'skip:%' AND ts >= ? GROUP BY note ORDER BY c DESC",
        (_cutoff_ts(hours),))
    out = [{"reason": r["note"], "count": r["c"]} for r in rows or []]
    return jsonify({"ts": now_iso(), "rows": out, "error": err})


@app.get("/api/edge-dist")
def api_edge_dist():
    days = clamp_int(request.args.get("days"), 1, 365, 7)
    cut = _cutoff_ts(days * 24)
    wrows, _ = _query(conn_a, "SELECT edge_after_costs FROM candidates WHERE ts >= ?", (cut,))
    erows, _ = _query(conn_a, "SELECT edge_after_costs FROM pm_candidates WHERE ts >= ?", (cut,))
    vals = [r["edge_after_costs"] for r in (wrows or []) if r["edge_after_costs"] is not None]
    vals += [r["edge_after_costs"] for r in (erows or []) if r["edge_after_costs"] is not None]
    # 0.02 buckets from 0 to 0.30
    buckets = {}
    for v in vals:
        b = round((v // 0.02) * 0.02, 2)
        buckets[b] = buckets.get(b, 0) + 1
    out = [{"bucket": k, "count": v} for k, v in sorted(buckets.items())]
    return jsonify({"ts": now_iso(), "buckets": out})


@app.get("/api/funnel")
def api_funnel():
    days = clamp_int(request.args.get("days"), 1, 365, 7)
    cut = _cutoff_ts(days * 24)
    stages = []
    def _stage(label, conn_fn, csql, osql, fsql):
        c, _ = _query(conn_fn, csql, (cut,))
        o, _ = _query(conn_fn, osql, (cut,))
        f, _ = _query(conn_fn, fsql, (cut,))
        stages.append({"edge": label, "candidates": (c[0]["n"] if c else 0),
                       "orders": (o[0]["n"] if o else 0), "fills": (f[0]["n"] if f else 0)})
    _stage("weather", conn_a,
           "SELECT COUNT(*) AS n FROM candidates WHERE ts >= ?",
           "SELECT COUNT(*) AS n FROM orders WHERE ts >= ?",
           "SELECT COUNT(*) AS n FROM fills WHERE ts >= ?")
    erows, _ = _query(conn_a, "SELECT DISTINCT edge FROM pm_candidates WHERE ts >= ?", (cut,))
    for r in erows or []:
        e = r["edge"]
        c, _ = _query(conn_a, "SELECT COUNT(*) AS n FROM pm_candidates WHERE edge=? AND ts>=?", (e, cut))
        o, _ = _query(conn_a, "SELECT COUNT(*) AS n FROM pm_orders WHERE edge=? AND ts>=?", (e, cut))
        f, _ = _query(conn_a, "SELECT COUNT(*) AS n FROM pm_fills WHERE edge=? AND ts>=?", (e, cut))
        stages.append({"edge": e, "candidates": (c[0]["n"] if c else 0),
                       "orders": (o[0]["n"] if o else 0), "fills": (f[0]["n"] if f else 0)})
    return jsonify({"ts": now_iso(), "stages": stages})


# Risk constants mirrored from bot/config.py (LIVE_* values, verified 2026-07-22).
# Mirrored as literals so the dashboard does not import the bot tree (no env reads).
RISK = {
    "max_open": 5,                  # LIVE_MAX_OPEN_POSITIONS
    "max_consec": 6,                # LIVE_CONSECUTIVE_LOSS_HALT
    "daily_loss_halt": 20.0,        # LIVE_DAILY_LOSS_HALT_FRAC * LIVE_BANKROLL = 0.10*200
    "per_trade_cap": 10.0,          # LIVE_PER_TRADE_CAP_ABS
    "min_edge": 0.08,               # LIVE_MIN_EDGE
    "bankroll": 200.0,              # LIVE_BANKROLL
}


@app.get("/api/risk")
def api_risk():
    open_rows, _ = _query(conn_live,
        "SELECT COUNT(*) AS n FROM live_orders WHERE status IN ('posted','open','partial','filled')")
    open_n = open_rows[0]["n"] if open_rows else 0
    cut = _cutoff_ts(24)
    sett, _ = _query(conn_live,
        "SELECT pnl, resolved_yes FROM live_settlements WHERE ts >= ? ORDER BY id DESC", (cut,))
    consec = 0
    for r in (sett or []):
        if (r["pnl"] or 0) < 0:
            consec += 1
        else:
            break
    daily_rows, _ = _query(conn_live, "SELECT COALESCE(SUM(pnl),0) AS s FROM live_settlements WHERE ts >= ?", (cut,))
    daily_loss = float(daily_rows[0]["s"]) if daily_rows else 0.0
    halted, _ = _latest_halt_halted(conn_live)
    return jsonify({"ts": now_iso(), "open_positions": open_n, "max_open": RISK["max_open"],
                     "consec_loss": consec, "max_consec": RISK["max_consec"],
                     "daily_loss": round(daily_loss, 2), "daily_loss_halt": RISK["daily_loss_halt"],
                     "per_trade_cap": RISK["per_trade_cap"], "halted": halted})


@app.get("/api/calib")
def api_calib():
    # ponytail: ORDER BY ts DESC (not id DESC) — id ordering only tracks time when
    # rows are appended chronologically; ts is the robust "latest" semantics.
    latest_rows, _ = _query(conn_a,
        "SELECT edge, brier_model, brier_market, reliability_maxdev_pp, n_signals, gate_pass, ts "
        "FROM calib_snapshots ORDER BY ts DESC LIMIT 5")
    latest = None
    if latest_rows:
        r = latest_rows[0]
        latest = {"edge": r["edge"], "brier_model": r["brier_model"], "brier_market": r["brier_market"],
                  "reliability_maxdev_pp": r["reliability_maxdev_pp"], "n_signals": r["n_signals"],
                  "gate_pass": bool(r["gate_pass"]), "ts": r["ts"]}
    series_rows, _ = _query(conn_a,
        "SELECT ts, edge, brier_model, brier_market FROM calib_snapshots ORDER BY ts ASC LIMIT 500")
    series = [{"ts": r["ts"], "edge": r["edge"], "brier_model": r["brier_model"],
               "brier_market": r["brier_market"]} for r in series_rows or []]
    return jsonify({"ts": now_iso(), "latest": latest, "series": series})


@app.get("/api/station-bias")
def api_station_bias():
    days = clamp_int(request.args.get("days"), 1, 365, 30)
    cut = _cutoff_ts(days * 24)
    rows, _ = _query(conn_a,
        "SELECT ts, city, residual FROM station_obs WHERE ts >= ? ORDER BY ts ASC", (cut,))
    by = {}
    for r in rows or []:
        by.setdefault(r["city"], []).append({"ts": r["ts"], "residual": r["residual"]})
    return jsonify({"ts": now_iso(), "cities": [{"city": c, "points": pts} for c, pts in by.items()]})


@app.get("/api/state")
def api_state():
    health = api_health().get_json()
    feed = api_feed().get_json()
    positions = api_positions().get_json()
    risk = api_risk().get_json()
    return jsonify({"ts": now_iso(), "health": health, "feed": feed,
                    "positions": positions, "risk": risk})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
