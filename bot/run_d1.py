"""D1 maker-execution FLB shadow arm entrypoint. Never invoked by any paper cron.
Two jobs:
  --job d1-scan     : read Bot A's FLB sell candidates (read-only), post resting
                       SELL-YES maker asks (dry-run default, independent of LIVE)
  --job d1-maintain : reconcile fills, settle resolved, compute rebates

Isolation: paper DB opened read-only via live_engine.paper_ro_conn(); all D1
state → polymarket_bot_d1.db (separate from polymarket_bot_live.db and the paper
DBs). HALT_D1 governs D1 only; HALT_LIVE (the income arm) is untouched.
"""
import argparse
import sys
import traceback
from pathlib import Path

from dotenv import load_dotenv

import d1
import engine

ENV_PATH = Path(__file__).resolve().parent / ".env"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True,
                    choices=["d1-scan", "d1-maintain", "d1-probe"])
    args = ap.parse_args()
    load_dotenv(ENV_PATH)
    print(f"[{d1._now_iso()}] d1 start job={args.job} dry={d1.is_dry_run()}",
          flush=True)
    try:
        if args.job == "d1-scan":
            d1.job_scan()
        elif args.job == "d1-maintain":
            d1.job_maintain()
        else:
            d1.job_probe()
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr, flush=True)
        try:
            d1.init_d1_db()
            conn = d1.get_d1_db()
            d1.log_tick(conn, args.job, "crash", {"tb": tb[-2000:]})
            conn.commit()
            conn.close()
        except Exception:
            pass
        try:
            engine.notify(f"[D1] crash {args.job}\n{tb[-1400:]}")
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
