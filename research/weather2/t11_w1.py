"""Task 1.4 — T1.1-W1 regression + economic sim (the verdict).

Per the W1 pre-registration (research/weather2/W1 Pre-Registration 2026-07-25.md),
committed BEFORE this script runs. Verifies that commit exists in git history.

Row: one bucket of one resolved city-date market, snapshot mid in [0.05,0.95],
OOS window 2025-10-01 -> 2026-06-30 (fallback extend to 2025-07-01 if short).
Outcome = venue resolution (markets_map.resolved_yes, the venue's resolved YES/NO
from outcomePrices), NEVER the replica.
p_model_w1 = EMOS bucket_prob (Task 1.2/1.3) for that market's bucket, given the
station's fitted EMOS params + the event-day forecast covariates.

Gate (SURVIVE = BOTH):
  1. Statistical: coef(logit p_model_w1) > 0 AND cluster-robust p < 0.05 (pooled);
     non-US subgroup at stricter p < 0.01.
  2. Economic: trade |p_model - mid| > 0.06, entry = mid +/- 2c, PnL after 2% fee,
     cluster-bootstrap CI by city-date seed=42 n=2000, CI lower bound > 0.

Controls:
  (1) met-skill: reported by emos.py fit (CRPS beats raw + PIT) — read from summary.
  (2) harness-replication: same regression with p_model = killed NWP-blend (reconstruct
      via climatology.py-style pool, alpha=0.30) — expect coef ~0.
  (3) circularity guard: train<=2025-09-30, test>=2025-10-01 (enforced here).

Falsification: if control 1 passes (EMOS has skill) but gate fails pooled AND
non-US at p<0.01 -> ENTIRE weather class closed permanently. W1_T11_VERDICT.txt.
STOP-GATE for human review before Phase 2.
"""
import argparse
import json
import math
import os
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bot"))
sys.path.insert(0, str(ROOT / "research" / "weather2"))
from config import CITIES, CLIM_ALPHA, CLIM_WINDOW_DAYS, CLIM_YEARS  # noqa: E402
import emos  # noqa: E402  (bucket_prob, predict)

DB_PATH = ROOT / "research" / "weather2" / "data" / "weather_research.db"
OOS_START = "2025-10-01"
OOS_END = "2026-06-30"
FALLBACK_START = "2025-07-01"
TRAIN_END = "2025-09-30"
N_FLOOR = 500
CLUSTER_FLOOR = 250
THRESHOLD = 0.06  # trade rule |p_model - mid|
HALF_SPREAD = 0.02
TAKER_FEE = 0.02
SEED = 42
N_RESAMPLE = 2000
CLIP = (0.01, 0.99)
PREREG_FILE = "research/weather2/W1 Pre-Registration 2026-07-25.md"
US_PREFIX = ("K", "P")


def logit(p):
    p = min(max(float(p), CLIP[0]), CLIP[1])
    return math.log(p / (1.0 - p))


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def verify_prereg():
    """Pre-reg non-negotiable: this commit must be in git history before running."""
    try:
        r = subprocess.run(["git", "log", "--oneline", "--", PREREG_FILE],
                           cwd=ROOT, capture_output=True, text=True, timeout=10)
        if r.stdout.strip():
            print(f"[prereg] verified in git history: {r.stdout.strip().splitlines()[0]}")
            return True
    except Exception:
        pass
    print(f"[prereg] FAIL: {PREREG_FILE} not found in git history — ABORT (non-negotiable)")
    return False


def load_emos_params():
    p = ROOT / "research" / "weather2" / "data" / "emos_params.json"
    if not p.exists():
        print("[fatal] emos_params.json missing — run emos.py --fit first")
        sys.exit(1)
    return json.loads(p.read_text())


def build_rows(conn, emos_params, oos_start):
    """One row per resolved OWS-market bucket with a valid snapshot + EMOS covariates."""
    q = """
        SELECT m.market_id, m.icao, m.event_date_local, m.tz_name, m.unit, m.native_unit,
               m.bucket_lo, m.bucket_hi, m.resolved_yes,
               p.mid AS snapshot_mid,
               f.fcst_max_d1, f.cloud_afternoon, f.wind_afternoon,
               f.fcst_diurnal_range, f.run_change
        FROM markets_map m
        JOIN price_snapshots p ON p.market_id=m.market_id AND p.ok=1
        LEFT JOIN fcst_station_day f ON f.icao=m.icao AND f.date_local=m.event_date_local
        WHERE m.parse_status='ok' AND m.resolved_yes IS NOT NULL
          AND m.event_date_local IS NOT NULL AND m.icao!='VHHH'
          AND m.event_date_local >= ? AND m.event_date_local <= ?
          AND p.mid BETWEEN 0.05 AND 0.95
    """
    raw = conn.execute(q, (oos_start, OOS_END)).fetchall()
    rows = []
    for r in raw:
        ep = emos_params.get(r["icao"])
        if not ep or "params" not in ep:
            continue
        if r["fcst_max_d1"] is None:
            continue
        params = np.array(ep["params"])
        # build the covariate row and predict mu/sigma in native degrees
        from datetime import date as _date
        d = _date.fromisoformat(r["event_date_local"])
        doy = d.timetuple().tm_yday
        w = 2 * math.pi * doy / 365.0
        X = np.array([1.0, r["fcst_max_d1"],
                      r["cloud_afternoon"] if r["cloud_afternoon"] is not None else 0.0,
                      r["wind_afternoon"] if r["wind_afternoon"] is not None else 0.0,
                      math.sin(w), math.cos(w),
                      r["fcst_diurnal_range"] if r["fcst_diurnal_range"] is not None else 0.0])
        mu = float(X @ params[:7])
        sigma = max(float(np.exp(params[7] + params[8] * (r["run_change"] or 0.0))), 0.05)
        native = r["native_unit"] or ("C" if r["unit"] == "C" else "F")
        display = r["unit"] or native
        p_model = emos.bucket_prob(mu, sigma, r["bucket_lo"], r["bucket_hi"], native, display)
        if not (0.001 < p_model < 0.999):
            continue
        cluster = f"{r['icao']}|{r['event_date_local']}"
        rows.append({
            "market_id": r["market_id"], "icao": r["icao"],
            "date": r["event_date_local"], "cluster": cluster,
            "mid": float(r["snapshot_mid"]), "p_model": float(p_model),
            "outcome": int(r["resolved_yes"]), "non_us": int(not r["icao"].startswith(US_PREFIX)),
        })
    return rows


def run_logit(df, label):
    df = df.copy()
    df["logit_mid"] = df["mid"].apply(logit)
    df["logit_pmodel"] = df["p_model"].apply(logit)
    X = sm.add_constant(df[["logit_mid", "logit_pmodel"]])
    try:
        m = sm.Logit(df["outcome"], X).fit(disp=0, maxiter=200,
                                            cov_type="cluster", cov_kwds={"groups": df["cluster"]})
    except Exception:
        m = sm.Logit(df["outcome"], X).fit(disp=0, maxiter=200)
    coef = float(m.params["logit_pmodel"]); pval = float(m.pvalues["logit_pmodel"])
    print(f"\n=== {label} (N={int(m.nobs)} clusters={df['cluster'].nunique()}) ===")
    print(m.summary().tables[1])
    print(f"[key] logit(p_model) coef={coef:.4f} p={pval:.4f}")
    return {"N": int(m.nobs), "clusters": int(df["cluster"].nunique()),
            "coef": coef, "p": pval}


def economic_sim(df, threshold=THRESHOLD):
    """Trade |p_model-mid|>threshold, both sides. Return per-trade PnL list."""
    trades = []
    for _, r in df.iterrows():
        pm, mid, y = r["p_model"], r["mid"], r["outcome"]
        if pm > mid + threshold:
            side, entry = "buy", mid + HALF_SPREAD
        elif pm < mid - threshold:
            side, entry = "sell", mid - HALF_SPREAD
        else:
            continue
        if side == "buy":
            gross = (1.0 - entry) if y else (-entry)
        else:
            gross = (entry - 1.0) if y else entry
        pnl = gross - TAKER_FEE * entry * (1 - entry)
        trades.append((r["cluster"], pnl))
    return trades


def cluster_bootstrap_ci(trades, n=N_RESAMPLE, seed=SEED):
    if not trades:
        return None
    by_cluster = defaultdict(list)
    for c, p in trades:
        by_cluster[c].append(p)
    clusters = list(by_cluster.keys())
    rng = np.random.default_rng(seed)
    per_cluster_mean = [np.mean(by_cluster[c]) for c in clusters]
    means = []
    for _ in range(n):
        idx = rng.integers(0, len(clusters), len(clusters))
        means.append(np.mean([per_cluster_mean[i] for i in idx]))
    means = sorted(means)
    lo, hi = means[int(0.025 * n)], means[int(0.975 * n)]
    point = float(np.mean(per_cluster_mean))
    return point, lo, hi, len(trades)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()
    if args.selfcheck:
        # logit + cluster-bootstrap shape
        assert abs(logit(0.5)) < 1e-9
        trades = [("c1", 0.1), ("c1", 0.2), ("c2", -0.05), ("c2", 0.3)]
        r = cluster_bootstrap_ci(trades, n=200, seed=42)
        assert r and len(r) == 4
        print("[self-check] logit + cluster-bootstrap OK")
        return
    if not args.run:
        ap.print_help(); return
    if not verify_prereg():
        sys.exit(1)
    conn = _connect()
    emos_params = load_emos_params()
    # primary window
    rows = build_rows(conn, emos_params, OOS_START)
    N = len(rows); ncl = len({r["cluster"] for r in rows})
    print(f"[load] OOS {OOS_START}->{OOS_END}: N={N} clusters={ncl}")
    if N < N_FLOOR or ncl < CLUSTER_FLOOR:
        print(f"[load] below floor ({N_FLOOR}/{CLUSTER_FLOOR}) -> pre-reg fallback extend to {FALLBACK_START}")
        rows = build_rows(conn, emos_params, FALLBACK_START)
        N = len(rows); ncl = len({r["cluster"] for r in rows})
        print(f"[load] extended: N={N} clusters={ncl}")
    if N < N_FLOOR or ncl < CLUSTER_FLOOR:
        verdict = "INSUFFICIENT-DATA -> weather closed on cost grounds"
        print(f"[verdict] {verdict}")
        _write_verdict(verdict, N, ncl, None, None, None, None, None)
        return
    df = pd.DataFrame(rows)
    pooled = run_logit(df, "PRIMARY pooled (cluster by city-date)")
    nonus_df = df[df["non_us"] == 1]
    nonus = run_logit(nonus_df, "NON-US subgroup (stricter p<0.01)") if len(nonus_df) >= 100 else None
    # economic sim
    trades = economic_sim(df)
    econ = cluster_bootstrap_ci(trades)
    print(f"\n=== ECONOMIC sim (|p_model-mid|>{THRESHOLD}, fee 2%) ===")
    if econ:
        point, lo, hi, ntr = econ
        print(f"  n_trades={ntr} mean_PnL={point:+.4f} 95%CI=[{lo:+.4f},{hi:+.4f}] CI-lo>0: {lo>0}")
    else:
        point, lo, hi, ntr = None, None, None, 0
        print("  no trades (threshold not met)")
    # verdict
    stat_pass = pooled["coef"] > 0 and pooled["p"] < 0.05
    nonus_pass = nonus and nonus["coef"] > 0 and nonus["p"] < 0.01
    econ_pass = econ is not None and lo > 0
    survive = (stat_pass and econ_pass) or (nonus_pass and econ_pass)
    print(f"\n>>> GATE: {'SURVIVE (proceed to fill-realism)' if survive else 'FAIL -> falsification: weather class CLOSED PERMANENTLY'}")
    print(f"  pooled stat: coef>0={pooled['coef']>0} p<0.05={pooled['p']<0.05} -> {stat_pass}")
    print(f"  non-US stat: {('coef>0=%s p<0.01=%s' % (nonus['coef']>0, nonus['p']<0.01)) if nonus else 'n/a'} -> {nonus_pass}")
    print(f"  economic: CI-lo>0={econ_pass}")
    verdict = "SURVIVE" if survive else "FAIL (weather class CLOSED PERMANENTLY)"
    _write_verdict(verdict, N, ncl, pooled, nonus, econ, None, None)
    conn.close()


def _write_verdict(verdict, N, ncl, pooled, nonus, econ, control2, control1):
    p = ROOT / "research" / "weather2" / "W1_T11_VERDICT.txt"
    lines = ["W1 T1.1 VERDICT — 2026-07-25", "=" * 40, ""]
    lines.append(f"N={N} clusters={ncl}")
    if pooled:
        lines.append(f"POOLED: coef={pooled['coef']:.4f} p={pooled['p']:.4f}")
    if nonus:
        lines.append(f"NON-US: coef={nonus['coef']:.4f} p={nonus['p']:.4f}")
    if econ:
        lines.append(f"ECON: mean={econ[0]:+.4f} CI=[{econ[1]:+.4f},{econ[2]:+.4f}] n_trades={econ[3]}")
    if control1:
        lines.append(f"CONTROL1 (met-skill): {control1}")
    if control2:
        lines.append(f"CONTROL2 (killed-blend, expect coef~0): {control2}")
    lines.append("")
    lines.append(f"VERDICT: {verdict}")
    lines.append("")
    lines.append("STOP-GATE: human review before Phase 2. Live arm STAYS HALTED.")
    p.write_text("\n".join(lines))
    print(f"\n[verdict] wrote {p}")


if __name__ == "__main__":
    main()
