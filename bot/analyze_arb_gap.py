import sqlite3
import sys

import numpy as np

sys.path.insert(0, "/root/polybot/bot")
from config import ARB_MIN_GAP, DB_PATH


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT net_gap, qualifies, n_outcomes, side, ts FROM arb_gap_log ORDER BY id"
    ).fetchall()
    conn.close()
    if not rows:
        print("arb_gap_log empty. Accrue >=4wk / >=500 scans (job_arb cron) first.")
        return
    nets = np.array([r["net_gap"] for r in rows], dtype=float)
    n = len(rows)
    crossings = int((nets > ARB_MIN_GAP).sum())
    print(f"ARB gap-log: n={n} scans  ARB_MIN_GAP={ARB_MIN_GAP}")
    print(f"  net_gap: min={nets.min():.4f} p50={np.percentile(nets, 50):.4f} "
          f"p95={np.percentile(nets, 95):.4f} p99={np.percentile(nets, 99):.4f} "
          f"max={nets.max():.4f}")
    print(f"  crossings of ARB_MIN_GAP ({ARB_MIN_GAP}): {crossings}/{n}")
    print(f"  qualifies (logged by scan): {int(sum(1 for r in rows if r['qualifies']))}/{n}")
    if crossings == 0:
        print(f"\n>>> VERDICT: 0 crossings in {n} scans; ARB class not observed at this latency.")
        if n >= 500:
            print(">>> KILL (>=500 scans, max net_gap never crossed ARB_MIN_GAP).")
        else:
            print(f">>> inconclusive (n={n} < 500); accrue to >=500 scans / 4wk.")
    else:
        print(f"\n>>> {crossings} crossing(s) observed -> proceed to P5 leg-exec simulator.")


if __name__ == "__main__":
    main()
