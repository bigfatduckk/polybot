import argparse
import json
import os
import sys
import traceback
from pathlib import Path

from dotenv import load_dotenv

import engine
import weather as w
from config import CITIES, DB_PATH, HALT_FILE

ENV_PATH = Path(__file__).resolve().parent / ".env"


def _halt_path():
    return HALT_FILE


def _run_hashes(runs):
    return {f"{r.city}|{r.model}": r.content_hash for r in runs}


def job_weather():
    if os.path.exists(_halt_path()):
        msg = "[HALT] weather scan skipped — HALT file present, no new orders"
        engine.notify(msg)
        engine.log({"job": "weather", "note": msg, "halted": True})
        return
    runs = w.fetch_runs(CITIES)
    engine.init_db()
    conn = engine.get_db()
    prev = json.loads(engine.meta_get(conn, "last_run_hashes", "{}") or "{}")
    conn.close()
    any_new = any(
        r.content_hash != prev.get(f"{r.city}|{r.model}") for r in runs
    )
    if not runs:
        msg = "weather scan: no model runs fetched (API error?)"
        engine.notify(msg)
        engine.log({"job": "weather", "note": msg})
        return
    if not any_new:
        msg = f"weather scan: no new model run ({len(runs)} runs unchanged)"
        engine.notify(msg)
        engine.log({"job": "weather", "note": msg})
        return
    conn = engine.get_db()
    engine.meta_set(conn, "last_run_hashes", json.dumps(_run_hashes(runs)))
    conn.commit()
    conn.close()
    engine.store_runs(runs)
    snapshots = engine.fetch_snapshots()
    candidates = engine.scan_weather(runs, snapshots)
    state = engine.load_state()
    orders = engine.propose(candidates, state)
    fills = []
    for o in orders:
        verdict = engine.risk_check(o, state)
        f = engine.execute(o, verdict)
        if f is not None:
            fills.append(f)
    msg = (f"weather scan: runs={len(runs)} snaps={len(snapshots)} "
           f"cands={len(candidates)} orders={len(orders)} fills={len(fills)}")
    engine.notify(msg)
    engine.log({"job": "weather", "note": msg, "fills": len(fills)})


def job_maintain():
    engine.init_db()
    settled = engine.sweep_settlements()
    engine.check_open_maker_fills()
    snapshots = engine.fetch_snapshots()
    engine.log_reward_snapshot(snapshots)
    halted = engine.halt_check()
    engine.daily_pnl_pulse_if_due()
    msg = (f"maintain: settled={settled} snaps={len(snapshots)} "
           f"halted={'yes' if halted else 'no'}")
    engine.notify(msg)
    engine.log({"job": "maintain", "note": msg, "settled": settled})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True, choices=["weather", "maintain"])
    ap.add_argument("--mode", default="paper", choices=["paper", "real"])
    args = ap.parse_args()
    load_dotenv(ENV_PATH)
    engine.set_mode(args.mode)
    try:
        if args.job == "weather":
            job_weather()
        else:
            job_maintain()
    except Exception:
        tb = traceback.format_exc()
        try:
            engine.init_db()
            engine.log({"job": args.job, "mode": args.mode, "error": tb})
        except Exception:
            pass
        try:
            engine.notify(f"[bot crash {args.job}] {tb[-1500:]}")
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
