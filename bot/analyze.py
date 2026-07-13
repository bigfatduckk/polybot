import json
import random
import sqlite3
import statistics
from datetime import datetime, timezone

from config import DB_PATH, MIN_EDGE, PAPER_BANKROLL


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _bootstrap_ci(values, n=1000, seed=42):
    if not values:
        return None, None, None
    rng = random.Random(seed)
    m = len(values)
    means = []
    for _ in range(n):
        sample = [rng.choice(values) for _ in range(m)]
        means.append(sum(sample) / m)
    means.sort()
    lo = means[int(0.025 * n)]
    hi = means[int(0.975 * n)]
    point = sum(values) / m
    return point, lo, hi


def _fmt_ci(point, lo, hi):
    if point is None:
        return "n/a"
    return f"{point:+.4f} [95% CI {lo:+.4f}, {hi:+.4f}]"


def _resolved_signals(conn):
    rows = conn.execute(
        """SELECT c.p_model, c.ts AS cts, c.market_id, c.effective_price,
                  c.bucket_key,
                  (s.best_bid + s.best_ask)/2.0 AS market_mid,
                  st.resolved_yes AS outcome, st.date AS sdate
           FROM candidates c
           LEFT JOIN snapshots s
             ON s.market_id = c.market_id
            AND substr(s.ts,1,16) = substr(c.ts,1,16)
           LEFT JOIN settlements st ON st.market_id = c.market_id
           WHERE st.resolved_yes IS NOT NULL"""
    ).fetchall()
    out = []
    for r in rows:
        if r["market_mid"] is None:
            continue
        out.append({
            "p": float(r["p_model"]),
            "mid": float(r["market_mid"]),
            "outcome": 1 if r["outcome"] else 0,
            "ts": r["cts"],
            "date": r["sdate"],
        })
    return out


def _brier_section(sigs):
    if not sigs:
        print("Brier: insufficient data")
        return None
    diffs = [
        (s["mid"] - s["outcome"]) ** 2 - (s["p"] - s["outcome"]) ** 2 for s in sigs
    ]
    brier_model = sum((s["p"] - s["outcome"]) ** 2 for s in sigs) / len(sigs)
    brier_market = sum((s["mid"] - s["outcome"]) ** 2 for s in sigs) / len(sigs)
    point, lo, hi = _bootstrap_ci(diffs)
    print(f"Brier(model)   = {brier_model:.4f}")
    print(f"Brier(market)  = {brier_market:.4f}")
    print(f"Brier(market)-Brier(model) = {_fmt_ci(point, lo, hi)}")
    return point, lo, hi


def _reliability_section(sigs):
    if not sigs:
        print("Reliability: insufficient data")
        return
    deciles = [(0.0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5),
               (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]
    print("Reliability by decile (n>=20):")
    for lo, hi in deciles:
        grp = [s for s in sigs if lo <= s["p"] < hi]
        if len(grp) < 20:
            continue
        mean_p = sum(s["p"] for s in grp) / len(grp)
        freq = sum(s["outcome"] for s in grp) / len(grp)
        dev = abs(mean_p - freq)
        print(f"  [{lo:.1f},{hi:.1f}) n={len(grp):4d} mean_p={mean_p:.3f} "
              f"freq={freq:.3f} dev={dev*100:.1f}pp")
    maxdev = 0.0
    for lo, hi in deciles:
        grp = [s for s in sigs if lo <= s["p"] < hi]
        if len(grp) < 20:
            continue
        mean_p = sum(s["p"] for s in grp) / len(grp)
        freq = sum(s["outcome"] for s in grp) / len(grp)
        maxdev = max(maxdev, abs(mean_p - freq))
    print(f"  max reliability deviation = {maxdev*100:.1f}pp")
    return maxdev


def _roi_section(sigs):
    if len(sigs) < 2:
        print("ROI: insufficient data")
        return None, None, None
    dates = sorted(datetime.fromisoformat(s["ts"].replace("Z", "+00:00")) for s in sigs)
    first = dates[0]
    def week_idx(s):
        d = datetime.fromisoformat(s["ts"].replace("Z", "+00:00"))
        return (d - first).days // 7
    def pnl(s):
        if s["p"] - s["mid"] >= MIN_EDGE:
            return (s["outcome"] - s["mid"])
        return 0.0
    ins = [pnl(s) for s in sigs if week_idx(s) < 3]
    oos = [pnl(s) for s in sigs if week_idx(s) >= 3]
    print(f"ROI in-sample (weeks 1-3): n={len(ins)}")
    ip, ilo, ihi = _bootstrap_ci(ins) if ins else (None, None, None)
    print(f"  mean per-share PnL = {_fmt_ci(ip, ilo, ihi)}")
    print(f"ROI out-of-sample (weeks 4+): n={len(oos)}")
    op, olo, ohi = _bootstrap_ci(oos) if oos else (None, None, None)
    print(f"  mean per-share PnL = {_fmt_ci(op, olo, ohi)}")
    return (op, olo, ohi)


def _station_bias_section(conn):
    rows = conn.execute(
        """SELECT city, COUNT(*) AS n, AVG(residual) AS avg_res,
                  AVG(observed_high) AS avg_obs
           FROM station_obs WHERE residual IS NOT NULL
           GROUP BY city ORDER BY city"""
    ).fetchall()
    if not rows:
        print("Station-bias: insufficient data")
        return
    print("Station-bias per city:")
    for r in rows:
        print(f"  {r['city']:14s} n={r['n']:3d} avg_residual={r['avg_res']:+.3f}C "
              f"avg_observed_high={r['avg_obs']:.2f}C")


def _reward_section(conn):
    rows = conn.execute(
        """SELECT market_id, COUNT(*) AS n, AVG(q_score) AS avg_q,
                  AVG(max_spread) AS avg_spread, AVG(midpoint) AS avg_mid
           FROM reward_snapshots GROUP BY market_id ORDER BY avg_q DESC LIMIT 30"""
    ).fetchall()
    if not rows:
        print("Reward-capture: insufficient data")
        return
    print("Reward-capture estimate per market (avg q_score):")
    for r in rows:
        print(f"  market={r['market_id'][:10]}.. n={r['n']:3d} avg_q={r['avg_q']:.2f} "
              f"spread={r['avg_spread']:.3f} mid={r['avg_mid']:.3f}")


def _gate_lines(n, brier, rel, roi):
    print("Gate verdicts (Plan v0.2 section 6):")
    g1 = n is not None and n >= 200
    print(f"  >=200 resolved signals: {'PASS' if g1 else 'FAIL'} (n={n or 0})")
    g2 = brier is not None and brier[0] is not None and brier[0] > 0 and brier[1] > 0
    print(f"  Brier(model)<Brier(market), CI excludes 0: "
          f"{'PASS' if g2 else 'FAIL'} ({_fmt_ci(*brier) if brier else 'n/a'})")
    g3 = rel is not None and rel <= 0.10
    print(f"  Max reliability deviation <=10pp: "
          f"{'PASS' if g3 else 'FAIL'} ({rel*100:.1f}pp)" if rel is not None
          else f"  Max reliability deviation <=10pp: FAIL (n/a)")
    g4 = roi is not None and roi[0] is not None and roi[0] > 0 and roi[1] > 0
    print(f"  Net ROI>0 OOS, CI excludes 0: "
          f"{'PASS' if g4 else 'FAIL'} ({_fmt_ci(*roi) if roi else 'n/a'})")


def main():
    conn = _connect()
    sigs = _resolved_signals(conn)
    print(f"Resolved signals: {len(sigs)}")
    print()
    brier = _brier_section(sigs)
    print()
    rel = _reliability_section(sigs)
    print()
    roi = _roi_section(sigs)
    print()
    _station_bias_section(conn)
    print()
    _reward_section(conn)
    print()
    _gate_lines(len(sigs), brier, rel, roi)
    conn.close()


if __name__ == "__main__":
    main()
