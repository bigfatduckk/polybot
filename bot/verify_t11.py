import sqlite3, sys, time
from collections import defaultdict
import numpy as np
import pandas as pd
import statsmodels.api as sm

sys.path.insert(0, "/root/polybot/bot")
from test_model_signal import logit, DB, _parse_ts
import markets


def load(db_path):
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
    era5 = {}
    for r in conn.execute("SELECT market_id, resolved_yes FROM settlements"):
        if r["resolved_yes"] is not None:
            era5[r["market_id"]] = int(r["resolved_yes"])

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
    n_era5_missing = 0
    for f in fills:
        mid, side, price = f["market_id"], f["side"], float(f["price"])
        fts = _parse_ts(f["fill_ts"])
        cs = cand.get((mid, side))
        if not cs:
            continue
        c = min(cs, key=lambda c: abs((c[0] - fts).total_seconds()))
        p_model, cond = c[1], c[2]
        g = geo.get(mid)
        if not g:
            continue
        city, mdate = g
        resolved, outcome = res(mid)
        if not resolved:
            continue
        mkt_yes = 1 if outcome == "yes" else 0
        e = era5.get(mid)
        rows.append(
            {
                "market_id": mid,
                "side": side,
                "city": city,
                "market_date": mdate,
                "entry_price": price,
                "p_model": p_model,
                "mkt_yes": mkt_yes,
                "era5_yes": e,
                "cluster": f"{city}|{mdate}",
            }
        )
        if e is None:
            n_era5_missing += 1
    conn.close()
    print(f"[verify-load] rows={len(rows)} era5_missing={n_era5_missing}")
    return rows


def fit(df, outcome_col, label):
    d = df[df[outcome_col].notna()].copy()
    d["y"] = d[outcome_col].astype(int)
    X = sm.add_constant(d[["logit_entry", "logit_pmodel"]])
    m = sm.Logit(d["y"], X).fit(
        disp=0, maxiter=200, cov_type="cluster",
        cov_kwds={"groups": d["cluster"]},
    )
    print(f"\n=== {label} (N={int(m.nobs)}) ===")
    print(m.summary().tables[1])
    return m


def main():
    rows = load(DB)
    df = pd.DataFrame(rows)
    df["logit_entry"] = df["entry_price"].apply(logit)
    df["logit_pmodel"] = df["p_model"].apply(logit)

    c = df[["logit_pmodel", "logit_entry"]].corr().iloc[0, 1]
    print(f"[check3] corr(logit_pmodel, logit_entry) = {c:.4f}  (expect 0.3-0.8)")

    m_mkt = fit(df, "mkt_yes", "CHECK-1 MARKET-graded (T1.1 replication)")
    m_era5 = fit(df, "era5_yes", "CHECK-1 ERA5-graded (control, decisive)")

    df["disagree"] = df.apply(
        lambda r: pd.notna(r["era5_yes"]) and int(r["era5_yes"]) != int(r["mkt_yes"]),
        axis=1,
    )
    n_dis = int(df["disagree"].sum())
    disagree = df[df["disagree"]].head(10)
    agree_sample = df[~df["disagree"]].head(5)
    sample = pd.concat([agree_sample, disagree])
    print("\n=== CHECK-2 hand-verify (5 agree + up to 10 disagree) ===")
    print(
        sample[["market_id", "side", "entry_price", "p_model",
                "mkt_yes", "era5_yes", "city", "market_date"]].to_string(index=False)
    )
    print(f"\n[disagreements in loaded set: {n_dis}]")
    print(f"[market coef={m_mkt.params['logit_pmodel']:.4f} p={m_mkt.pvalues['logit_pmodel']:.4f}]")
    if m_era5 is not None:
        print(f"[era5   coef={m_era5.params['logit_pmodel']:.4f} p={m_era5.pvalues['logit_pmodel']:.4f}]")


if __name__ == "__main__":
    main()
