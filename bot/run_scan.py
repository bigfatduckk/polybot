import argparse
import json
import os
import sys
import traceback
from pathlib import Path

from dotenv import load_dotenv

import engine
import edge_engine
import markets
import settle
import weather as w
from edges import arb, crossvenue, flb
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


def job_settle():
    engine.init_db()
    settled = 0
    for edge in ("flb", "arb"):
        settled += settle.sweep_resolutions(edge)
    msg = f"settle: settled={settled}"
    engine.notify(msg)
    engine.log({"job": "settle", "note": msg, "settled": settled})


def job_flb():
    if os.path.exists(_halt_path()):
        engine.notify("[HALT] flb scan skipped")
        return
    markets.init_edge_db()
    scan_id = edge_engine.new_scan_id("flb")
    cands = flb.scan_flb(scan_id)
    state = edge_engine.load_edge_state("flb")
    orders = edge_engine.edge_propose(cands, state)
    fills = 0
    for o in orders:
        verdict = edge_engine.edge_risk_check(o, state)
        if edge_engine.edge_execute(o, verdict) is not None:
            fills += 1
            state.open_markets.add(o.market_id)
    msg = f"flb scan: cands={len(cands)} orders={len(orders)} fills={fills}"
    engine.notify(msg)
    engine.log({"job": "flb", "note": msg, "fills": fills})


def job_arb():
    if os.path.exists(_halt_path()):
        engine.notify("[HALT] arb scan skipped")
        return
    markets.init_edge_db()
    scan_id = edge_engine.new_scan_id("arb")
    bundles = arb.scan_arb(scan_id)
    state = edge_engine.load_edge_state("arb")
    fills = 0
    executed = 0
    for b in bundles:
        leg_orders = []
        for leg in b["legs"]:
            leg_orders.append(edge_engine.EdgeOrder(
                edge="arb", market_id=leg["market_id"], token_id=leg["token_id"],
                side=b["side"], price=leg["price"], size=leg["shares"],
                maker_or_taker="taker", edge_size=b["net_gap"], kelly_fraction=0.0,
                meta={"scan_id": scan_id, "bundle": b["event_id"], "n_outcomes": b["n_outcomes"]},
            ))
        ok = True
        for o in leg_orders:
            v = edge_engine.edge_risk_check(o, state)
            if not v[0]:
                ok = False
                break
        if not ok:
            continue
        for o in leg_orders:
            if edge_engine.edge_execute(o, edge_engine.edge_risk_check(o, state)) is not None:
                fills += 1
                state.open_markets.add(o.market_id)
        executed += 1
    msg = f"arb scan: bundles={len(bundles)} executed={executed} legs={fills}"
    engine.notify(msg)
    engine.log({"job": "arb", "note": msg, "fills": fills})


def job_crossvenue():
    if os.path.exists(_halt_path()):
        engine.notify("[HALT] crossvenue scan skipped")
        return
    markets.init_edge_db()
    scan_id = edge_engine.new_scan_id("crossvenue")
    gaps = crossvenue.scan_crossvenue(scan_id)
    msg = f"crossvenue scan: gaps={len(gaps)}"
    hot = crossvenue.notify_threshold_gaps(gaps)
    engine.notify(msg + (("\n" + hot) if hot else ""))
    engine.log({"job": "crossvenue", "note": msg, "gaps": len(gaps)})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True,
                    choices=["weather", "maintain", "settle", "flb", "arb", "crossvenue"])
    ap.add_argument("--mode", default="paper", choices=["paper", "real"])
    args = ap.parse_args()
    load_dotenv(ENV_PATH)
    engine.set_mode(args.mode)
    edge_engine.set_mode(args.mode)
    try:
        if args.job == "weather":
            job_weather()
        elif args.job == "settle":
            job_settle()
        elif args.job == "flb":
            job_flb()
        elif args.job == "arb":
            job_arb()
        elif args.job == "crossvenue":
            job_crossvenue()
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
