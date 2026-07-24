import sqlite3, sys, time, math
from collections import defaultdict
from datetime import datetime
import numpy as np
import pandas as pd
import statsmodels.api as sm

sys.path.insert(0, "/root/polybot/bot")
import markets

DB = "/root/polybot/bot/polymarket_bot.db"
CLIP = (0.01, 0.99)


def logit(p):
    p = min(max(float(p), CLIP[0]), CLIP[1])
    return math.log(p / (1.0 - p))


def _parse_ts(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return datetime.min


def load_resolved_bets(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    fills = conn.execute(
        "SELECT market_id, side, price, fill_ts FROM fills ORDER BY id"
    ).fetchall()

    cand = defaultdict(list)
    for r in conn.execute(
        "SELECT market_id, side, ts, p_model, condition_id FROM candidates"
    ):
        cand[(r["market_id"], r["side"])].append(
            (_parse_ts(r["ts"]), float(r["p_model"]), r["condition_id"])
        )

    geo = {}
    for r in conn.execute(
        "SELECT DISTINCT market_id, city, market_date FROM snapshots"
    ):
        geo[r["market_id"]] = (r["city"], r["market_date"])

    res_cache = {}

    def res(mid):
        if mid not in res_cache:
            closed, outcome, prices = markets.fetch_resolution(mid)
            resolved = (
                closed
                and len(prices) >= 2
                and prices[0] in (0.0, 1.0)
                and prices[1] in (0.0, 1.0)
                and prices[0] != prices[1]
            )
            res_cache[mid] = (resolved, outcome)
            time.sleep(0.08)
        return res_cache[mid]

    rows = []
    n_no_cand = 0
    n_no_geo = 0
    n_unresolved = 0
    for f in fills:
        mid, side, price = f["market_id"], f["side"], float(f["price"])
        fts = _parse_ts(f["fill_ts"])
        cands = cand.get((mid, side))
        if not cands:
            n_no_cand += 1
            continue
        c = min(cands, key=lambda c: abs((c[0] - fts).total_seconds()))
        p_model, condition_id = c[1], c[2]
        g = geo.get(mid)
        if g is None:
            n_no_geo += 1
            continue
        city, market_date = g
        resolved, outcome = res(mid)
        if not resolved:
            n_unresolved += 1
            continue
        outcome_yes = 1 if outcome == "yes" else 0
        rows.append(
            {
                "condition_id": condition_id,
                "city": city,
                "market_date": market_date,
                "side": side,
                "entry_price": price,
                "p_model": p_model,
                "outcome_yes": outcome_yes,
                "cluster": f"{city}|{market_date}",
            }
        )
    conn.close()
    print(
        f"[load] fills={len(fills)} resolved_rows={len(rows)} "
        f"no_cand={n_no_cand} no_geo={n_no_geo} unresolved={n_unresolved}"
    )
    return rows


def run_logit(rows):
    df = pd.DataFrame(rows)
    df["logit_entry"] = df["entry_price"].apply(logit)
    df["logit_pmodel"] = df["p_model"].apply(logit)
    X = sm.add_constant(df[["logit_entry", "logit_pmodel"]])
    y = df["outcome_yes"]

    m = sm.Logit(y, X).fit(disp=0, maxiter=200)
    print("\n=== Logistic: outcome_yes ~ logit(entry_price) + logit(p_model) ===")
    print(f"N={int(m.nobs)}  pseudo-R2={m.prsquared:.4f}  llf={m.llf:.1f}")
    print("\n-- ordinary SE --")
    print(m.summary().tables[1])

    mc = sm.Logit(y, X).fit(
        disp=0, maxiter=200, cov_type="cluster",
        cov_kwds={"groups": df["cluster"]},
    )
    print("\n-- cluster-robust SE (by city-date) --")
    print(mc.summary().tables[1])

    coef = mc.params["logit_pmodel"]
    pval = mc.pvalues["logit_pmodel"]
    n_clusters = df["cluster"].nunique()
    print(f"\n[key] logit(p_model) coef={coef:.4f}  cluster-p={pval:.4f}  "
          f"n_clusters={n_clusters}")
    return {
        "N": int(m.nobs),
        "pseudo_r2": float(m.prsquared),
        "coef_pmodel": float(coef),
        "pvalue_pmodel": float(pval),
        "n_clusters": int(n_clusters),
        "coef_entry": float(mc.params["logit_entry"]),
    }


def main():
    rows = load_resolved_bets(DB)
    assert len(rows) >= 200, f"N={len(rows)} < 200"
    res = run_logit(rows)
    assert np.isfinite(res["coef_pmodel"]) and np.isfinite(res["coef_entry"]), "non-finite coef"
    go = res["coef_pmodel"] > 0 and res["pvalue_pmodel"] < 0.10
    print(f"\n>>> D-4 GATE: {'PASS (proceed)' if go else 'FAIL (STOP -> option D)'}")
    print(f"    criterion: coef>0 AND cluster-p<0.10")


if __name__ == "__main__":
    main()
