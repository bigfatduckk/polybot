"""Live trading entrypoint. Never invoked by any paper cron (paper crons run
run_scan.py only). Two jobs:
  --job weather-live  : read Bot A's paper candidates (read-only), size, risk-check, submit (dry-run default)
  --job maintain-live  : reconcile fills, cancel stale orders, settle resolved, balance check, daily pulse

Isolation (I1): the paper DB is opened read-only via live_engine.paper_ro_conn();
a write raises OperationalError. All live state → polymarket_bot_live.db.
"""
import argparse
import os
import sys
import traceback
from pathlib import Path

from dotenv import load_dotenv

import engine
import live_engine as le
import live_executor
import live_settle
from config import HALT_LIVE_FILE

ENV_PATH = Path(__file__).resolve().parent / ".env"
KEY_NOTICE_KEY = "last_key_missing_notice"


def _today_hkt():
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def _notify_once_daily(live_conn, meta_key, text):
    today = _today_hkt()
    last = le.live_meta_get(live_conn, meta_key, "")
    if last == today:
        return
    le.live_meta_set(live_conn, meta_key, today)
    live_conn.commit()
    engine.notify(text)


def job_weather_live():
    le.init_live_db()
    if os.path.exists(HALT_LIVE_FILE):
        engine.notify("[A-LIVE] weather-live skipped — HALT_LIVE present")
        return
    live_conn = le.get_live_db()
    try:
        try:
            client = live_executor.get_client()
        except live_executor.LiveKeyMissing as e:
            _notify_once_daily(live_conn, KEY_NOTICE_KEY,
                               f"[A-LIVE] live key not configured ({e}); no orders. "
                               f"Set POLY_PRIVATE_KEY/POLY_FUNDER in bot/.env")
            return
        dry = live_executor.is_dry_run()
        paper_conn = le.paper_ro_conn()
        try:
            sigs = le.read_new_signals(paper_conn, live_conn)
            state = le.load_live_state(live_conn)
            posted = dry_signed = rejected = skipped = 0
            for sig in sigs:
                prepared = live_executor.prepare_order(sig, client, live_conn)
                if prepared is None:
                    skipped += 1
                    continue
                spec, opts = prepared
                verdict = le.live_risk_check(spec, state)
                if not verdict.approved:
                    le.log_tick(live_conn, "weather-live", "skip:risk",
                                {"candidate_id": sig.candidate_id, "reason": verdict.reason})
                    skipped += 1
                    continue
                status, _ = live_executor.submit(spec, opts, client, live_conn, dry)
                if status == "posted":
                    posted += 1
                    state.open_positions.append(
                        {"market_id": sig.market_id, "city": sig.city,
                         "market_date": sig.market_date})
                    engine.notify(
                        f"[A-LIVE] posted {sig.side} {sig.city} {sig.market_date} "
                        f"@{spec.price:.3f} x{spec.size:.0f} edge {spec.edge_at_exec:+.3f}")
                elif status == "dry_run":
                    dry_signed += 1
                    engine.notify(
                        f"[A-LIVE] dry-run signed {sig.side} {sig.city} {sig.market_date} "
                        f"@{spec.price:.3f} x{spec.size:.0f} (NOT posted)")
                else:
                    rejected += 1
                    engine.notify(f"[A-LIVE] order rejected {sig.city} {sig.market_date}")
            tag = "DRY-RUN" if dry else "LIVE"
            engine.notify(
                f"[A-LIVE] weather-live tick ({tag}): signals={len(sigs)} "
                f"posted={posted} dry_signed={dry_signed} rejected={rejected} skipped={skipped}")
            live_conn.commit()
        finally:
            paper_conn.close()
    finally:
        live_conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True, choices=["weather-live", "maintain-live"])
    args = ap.parse_args()
    load_dotenv(ENV_PATH)
    try:
        if args.job == "weather-live":
            job_weather_live()
        else:
            live_settle.job_maintain_live()
    except Exception:
        tb = traceback.format_exc()
        try:
            le.init_live_db()
            conn = le.get_live_db()
            le.log_tick(conn, args.job, "crash", {"tb": tb[-2000:]})
            conn.commit()
            conn.close()
        except Exception:
            pass
        try:
            engine.notify(f"[A-LIVE] crash {args.job}\n{tb[-1400:]}")
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
