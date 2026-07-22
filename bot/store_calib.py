"""Daily calibration snapshot store for the dashboard's Brier/reliability panels.

Pure stdlib (subprocess + re + json + sqlite3). No trading-code import, no SDK.
Runs analyze.py (weather) + analyze_edges.py --edge {flb,arb,usud,crossvenue} via
subprocess, parses their stdout summary, inserts one row per edge into
calib_snapshots (paper A DB, same file as pm_* tables via config.DB_PATH).

Cron: 30 6 * * *  (06:30 UTC, offset from the FLB slot) -> appends to weather.log.
First run: python store_calib.py --init   (creates the table; idempotent).
"""
import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

import config

DB_PATH = config.DB_PATH  # paper A DB (edge tables + calib_snapshots live here)

_EDGES = ["flb", "arb", "usud", "crossvenue"]
_ANALYZE = [sys.executable, "analyze.py"]
_ANALYZE_EDGES = [sys.executable, "analyze_edges.py"]


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_weather_stdout(text):
    bm = re.search(r"Brier\(model\)\s*=\s*([0-9.]+)", text)
    bk = re.search(r"Brier\(market\)\s*=\s*([0-9.]+)", text)
    rel = re.search(r"max reliability deviation\s*=\s*([0-9.]+)pp", text)
    n = re.search(r">=200 resolved signals: (PASS|FAIL) \(n=(\d+)\)", text)
    gate = re.search(r">=200 resolved signals: PASS", text)
    if not (bm and bk and rel and n):
        return None
    gate_ok = bool(gate) and float(rel.group(1)) <= 10.0
    return {
        "brier_model": float(bm.group(1)),
        "brier_market": float(bk.group(1)),
        "reliability_maxdev_pp": float(rel.group(1)),
        "n_signals": int(n.group(2)),
        "gate_pass": gate_ok,
    }


def parse_edge_stdout(text):
    # FLB/ARB print "max reliability deviation = NN.Npp"; crossvenue has none.
    rel = re.search(r"max reliability deviation\s*=\s*([0-9.]+)pp", text)
    nsig = re.search(r"resolved signals: (\d+)", text) or re.search(r"settled: (\d+)", text)
    n = re.search(r">=(?:200|50) (?:resolved signals|settled bundles): PASS \(n=(\d+)\)", text)
    if not rel:
        return None
    gate_ok = float(rel.group(1)) <= 10.0
    return {
        "reliability_maxdev_pp": float(rel.group(1)),
        "n_signals": int(nsig.group(1)) if nsig else 0,
        "gate_pass": (gate_ok and bool(n)),
    }


def _insert_row(row):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO calib_snapshots
           (ts, edge, brier_model, brier_market, reliability_maxdev_pp, n_signals, gate_pass, detail_json)
           VALUES (?,?,?,?,?,?,?,?)""",
        (_now_iso(), row["edge"], row.get("brier_model"), row.get("brier_market"),
         row.get("reliability_maxdev_pp"), row.get("n_signals"),
         row.get("gate_pass", 0), row.get("detail_json", "{}")),
    )
    conn.commit()
    conn.close()


def _run(argv):
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        return out.stdout
    except Exception as e:
        return f"__error__ {e}"


def store_all():
    w = parse_weather_stdout(_run(_ANALYZE))
    if w:
        w["edge"] = "weather"
        _insert_row(w)
    for e in _EDGES:
        r = parse_edge_stdout(_run(_ANALYZE_EDGES + ["--edge", e]))
        if r:
            r["edge"] = e
            _insert_row(r)


def init():
    import markets
    markets.DB_PATH = DB_PATH
    markets.init_edge_db()


if __name__ == "__main__":
    if "--init" in sys.argv:
        init()
        print("calib_snapshots table ensured")
    else:
        store_all()
        print("calib snapshot stored")
