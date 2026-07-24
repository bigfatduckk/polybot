import argparse
import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import statsmodels.api as sm

sys.path.insert(0, "/root/polybot/bot")
from test_model_signal import logit
from config import DB_PATH

GATE_N = 200
GATE_CLUSTERS = 60
NY = ZoneInfo("America/New_York")


def load(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT q.market_id, q.market_ask AS entry_price, q.p_model, q.end_date, q.ticker, "
        "s.resolved_yes FROM usud_quotes q "
        "JOIN pm_settlements s ON s.market_id=q.market_id AND s.edge='usud' "
        "WHERE s.resolved_yes IS NOT NULL AND q.market_ask IS NOT NULL "
        "AND q.p_model IS NOT NULL ORDER BY q.ts"
    ).fetchall()
    conn.close()
    seen = set()
    out = []
    for r in rows:
        if r["market_id"] in seen:
            continue
        seen.add(r["market_id"])
        try:
            dt = datetime.fromisoformat(r["end_date"].replace("Z", "+00:00"))
            td = dt.astimezone(NY).date().isoformat()
        except (ValueError, AttributeError):
            continue
        p = float(r["entry_price"])
        pm = float(r["p_model"])
        if not (0.0 < p < 1.0 and 0.0 < pm < 1.0):
            continue
        out.append({
            "market_id": r["market_id"], "entry_price": p, "p_model": pm,
            "trading_date": td, "ticker": r["ticker"],
            "y": int(r["resolved_yes"]),
        })
    return out


def fit(df, label):
    d = df.copy()
    d["logit_entry"] = d["entry_price"].apply(logit)
    d["logit_pmodel"] = d["p_model"].apply(logit)
    X = sm.add_constant(d[["logit_entry", "logit_pmodel"]])
    clustered = True
    try:
        m = sm.Logit(d["y"], X).fit(disp=0, maxiter=200,
                                    cov_type="cluster",
                                    cov_kwds={"groups": d["trading_date"]})
    except (np.linalg.LinAlgError, Exception):
        m = sm.Logit(d["y"], X).fit(disp=0, maxiter=200)
        clustered = False
    c = d[["logit_pmodel", "logit_entry"]].corr().iloc[0, 1]
    coef = float(m.params["logit_pmodel"])
    pval = float(m.pvalues["logit_pmodel"])
    n_clusters = d["trading_date"].nunique()
    cov_note = "cluster-robust" if clustered else "NON-ROBUST (degenerate clusters, sign only)"
    print(f"\n=== {label} (N={int(m.nobs)} n_clusters={n_clusters} cov={cov_note}) ===")
    print(m.summary().tables[1])
    print(f"[key] logit(p_model) coef={coef:.4f} p={pval:.4f} "
          f"corr(pmodel,entry)={c:.4f}")
    return {"N": int(m.nobs), "coef_pmodel": coef, "pvalue_pmodel": pval,
            "n_clusters": n_clusters, "corr": c, "clustered": clustered}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args()
    if a.selfcheck:
        rng = np.random.default_rng(7)
        n = 200
        td = [f"2026-07-{(i % 22) + 1}" for i in range(n)]
        entry = rng.uniform(0.1, 0.9, n)
        pm = 1 / (1 + np.exp(-(3 * entry - 0.4 + rng.normal(0, 0.3, n))))
        y = rng.binomial(1, np.clip(pm, 0.02, 0.98))
        df = pd.DataFrame({"entry_price": entry, "p_model": pm,
                           "trading_date": td, "y": y})
        r = fit(df, "SELF-CHECK synthetic (p_model should carry signal)")
        assert np.isfinite(r["coef_pmodel"]) and np.isfinite(r["n_clusters"])
        print("\n[self-check] fit ran, finite coefs -> harness OK")
        return

    rows = load(DB_PATH)
    print(f"[load] settled USUD rows (first-scan entry): {len(rows)}")
    df = pd.DataFrame(rows)
    if len(rows) == 0:
        print("[verdict] no settled rows; accrue via job_usud cron then rerun.")
        return
    yahoo_fail = 0
    print(f"[load] distinct trading_dates={df['trading_date'].nunique()} "
          f"tickers={sorted(df['ticker'].unique().tolist())}")
    r = fit(df, "USUD T1.1 (outcome ~ logit(entry) + logit(p_model), cluster by trading_date)")
    print(f"\nyahoo-failure-rate (stub, requires scan log): {yahoo_fail}")
    n = r["N"]
    n_c = r["n_clusters"]
    if n < GATE_N or n_c < GATE_CLUSTERS:
        verdict = "INCONCLUSIVE (sub-threshold, early-kill-only)"
        sub_kill = (r["coef_pmodel"] <= 0.0) or (r["pvalue_pmodel"] >= 0.10)
        if sub_kill:
            verdict = "EARLY-KILL (coef<=0 or p>=0.10) -> signal adds nothing beyond price"
        print(f"\n>>> N={n} < {GATE_N} (or clusters {n_c} < {GATE_CLUSTERS}): {verdict}")
        print("    sub-threshold PASS does NOT unblock; accrue to >=60 clusters / >=200 rows.")
    else:
        go = (r["coef_pmodel"] > 0) and (r["pvalue_pmodel"] < 0.10)
        print(f"\n>>> GATE: {'PASS (proceed)' if go else 'FAIL (STOP -> kill USUD)'}")
        print(f"    criterion: coef>0 AND cluster-p<0.10")


if __name__ == "__main__":
    main()
