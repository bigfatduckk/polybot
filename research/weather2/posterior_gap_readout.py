"""W1 posterior-gap readout (descriptive only).

Pre-committed 2026-07-26 in 'W1 Posterior-Gap Readout Pre-Commitment 2026-07-26.md'
(decision rule locked before this script runs). Verifies that commit exists in git
history before executing.

Uses the already-estimated EMOS coefficients (no refit). For each OOS row, computes
the blended posterior gap — the part of the model-vs-market divergence the regression
says is true edge (the market discounts the raw divergence to ~coef of face value):

  blended_gap_cents = 100 * COEF * |logit(p_model) - logit(mid)| * mid*(1-mid)

where COEF = pooled coef on logit(p_model) (0.1823). mid*(1-mid) is the local
logit-to-probability derivative (Fable-5 used ~0.25, the mid=0.5 value).

Reports the distribution, the count/frequency of >4c rows, where they live, and the
model's directional win-rate on them vs the market's. Decision per the pre-commitment.
"""
import sqlite3
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "research" / "weather2"))
sys.path.insert(0, str(ROOT / "bot"))
import t11_w1  # noqa: E402  (build_rows, logit, _connect, load_emos_params)

PREREG = "research/weather2/W1 Posterior-Gap Readout Pre-Commitment 2026-07-26.md"
COEF = 0.1823  # pooled coef on logit(p_model_w1), from the verdict
COST_BAR_CENTS = 4.0  # pre-committed bar


def verify_precommit():
    try:
        r = subprocess.run(["git", "log", "--oneline", "--", PREREG], cwd=ROOT,
                           capture_output=True, text=True, timeout=10)
        if r.stdout.strip():
            print(f"[precommit] verified in git: {r.stdout.strip().splitlines()[0]}")
            return True
    except Exception:
        pass
    print("[precommit] FAIL: pre-commitment not in git history — ABORT")
    return False


def main():
    if not verify_precommit():
        sys.exit(1)
    conn = t11_w1._connect()
    ep = t11_w1.load_emos_params()
    rows = t11_w1.build_rows(conn, ep, t11_w1.OOS_START)
    print(f"[load] OOS rows: {len(rows)}")
    if not rows:
        print("[fatal] no rows"); return
    # compute blended_gap_cents per row
    for r in rows:
        lgap = abs(t11_w1.logit(r["p_model"]) - t11_w1.logit(r["mid"]))
        bg = 100.0 * COEF * lgap * r["mid"] * (1.0 - r["mid"])
        r["bg_cents"] = bg
    bgs = sorted(r["bg_cents"] for r in rows)
    n = len(bgs)
    print(f"\n=== BLENDED-GAP DISTRIBUTION (cents) ===")
    print(f"  min={bgs[0]:.3f} p25={bgs[n//4]:.3f} median={bgs[n//2]:.3f} "
          f"p75={bgs[3*n//4]:.3f} p95={bgs[int(0.95*n)]:.3f} max={bgs[-1]:.3f}")
    # bar buckets
    for bar in [1, 2, 3, 4, 5, 6, 8, 10]:
        c = sum(1 for b in bgs if b >= bar)
        print(f"  >= {bar}c: {c} rows ({100*c/n:.1f}%)")
    # the pre-committed bar
    fat = [r for r in rows if r["bg_cents"] >= COST_BAR_CENTS]
    print(f"\n=== >{COST_BAR_CENTS}c BLENDED-GAP ROWS (the pre-committed bar) ===")
    print(f"  count: {len(fat)} ({100*len(fat)/n:.1f}% of OOS rows)")
    # annualized frequency: OOS window is 2025-10-01 -> 2026-06-30 ~= 273 days
    oos_days = 273
    print(f"  annualized (~365d): ~{len(fat)*365//oos_days}/year")
    # where do they live
    if fat:
        print(f"\n  by station (top):")
        for icao, c in Counter(r["icao"] for r in fat).most_common(10):
            print(f"    {icao}: {c}")
        print(f"  by bucket position (lo vs hi — is it tail buckets?):")
        # tail = lo is None or hi is None (the 'or below'/'or higher' extremes)
        # we didn't carry bucket into row; re-derive from market via mid vs 0.5? skip — use mid extremes
        extreme = sum(1 for r in fat if r["mid"] < 0.15 or r["mid"] > 0.85)
        print(f"    rows with mid<0.15 or >0.85 (extreme buckets): {extreme}/{len(fat)}")
        # model win-rate on fat rows vs market win-rate
        # model-correct: (p_model>mid and outcome=1) or (p_model<mid and outcome=0)
        mc = sum(1 for r in fat if (r["p_model"] > r["mid"]) == (r["outcome"] == 1))
        # market-correct on same rows: mid>0.5 -> outcome=1? (directional, rough)
        mkc = sum(1 for r in fat if (r["mid"] > 0.5) == (r["outcome"] == 1))
        print(f"  model directional win-rate on >{COST_BAR_CENTS}c rows: {mc}/{len(fat)} ({100*mc/len(fat):.1f}%)")
        print(f"  market directional win-rate on same rows: {mkc}/{len(fat)} ({100*mkc/len(fat):.1f}%)")
        print(f"  model edge over market on these rows: {100*mc/len(fat) - 100*mkc/len(fat):+.1f}pp")
    # apply pre-committed decision rule
    print(f"\n=== PRE-COMMITTED DECISION RULE ===")
    if not fat:
        verdict = "THIN (zero rows >4c) -> CLOSURE CONFIRMED, never run another weather regression"
    else:
        annual = len(fat) * 365 // oos_days
        mc_rate = mc / len(fat) if fat else 0
        mkc_rate = mkc / len(fat) if fat else 0
        model_error_sig = mc_rate <= mkc_rate + 0.02  # model not clearly beating market
        thin = annual < 50
        if thin or model_error_sig:
            verdict = (f"{'THIN' if thin else 'MODEL-ERROR'} tail (annual ~{annual}/yr, "
                       f"model win {mc_rate:.1%} vs market {mkc_rate:.1%}) -> CLOSURE CONFIRMED, "
                       f"never run another weather regression")
        else:
            verdict = (f"FAT + broad-based tail (annual ~{annual}/yr, model win {mc_rate:.1%} vs "
                       f"market {mkc_rate:.1%}) -> ONE split-sample test justified (new pre-reg, "
                       f"theory-derived, run once on unexploited data)")
    print(f"  {verdict}")
    # write to a results file
    out = ROOT / "research" / "weather2" / "posterior_gap_readout_results.txt"
    out.write_text(f"W1 posterior-gap readout — 2026-07-26\n{'='*40}\n"
                   f"OOS rows: {n}\nblended-gap cents: min={bgs[0]:.3f} median={bgs[n//2]:.3f} "
                   f"p95={bgs[int(0.95*n)]:.3f} max={bgs[-1]:.3f}\n"
                   f">{COST_BAR_CENTS}c rows: {len(fat)} ({100*len(fat)/n:.1f}%), annualized ~{len(fat)*365//oos_days}/yr\n"
                   + (f"model win-rate on >4c rows: {mc_rate:.1%} vs market {mkc_rate:.1%}\n" if fat else "")
                   + f"\nDECISION: {verdict}\n")
    print(f"\n[done] wrote {out}")
    conn.close()


if __name__ == "__main__":
    main()
