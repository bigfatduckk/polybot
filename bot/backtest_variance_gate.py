"""Freeze Bot C's OOD-gate σ_calm from the pooled de-meaned residual history.

NOT a backtest — a proper backtest is IMPOSSIBLE with ≤10 days of station_obs
(1 obs/city/day, 2026-07-13 → now). This is a calm-baseline SCALE estimate:
pool all cities' residuals with per-city median removed (so a city that runs
hot/cold doesn't skew the scale), take 1.4826×MAD (robust std equivalent), apply
a 1.0°C floor. The result is frozen into config.SIGMA_CALM at launch — NOT
recomputed live, or a storm inflates its own threshold and the gate self-disarms.

Usage (VPS, reads Bot A's station_obs):
  cd /root/polybot/bot && python backtest_variance_gate.py
  # → prints `SIGMA_CALM = <val>` → paste into config.py, commit, pull on VPS.

Self-check: python backtest_variance_gate.py --demo
"""
import sqlite3
import statistics
import sys

from config import DB_PATH


def compute_sigma_calm(residuals_by_city):
    """{city: [residuals]} → (sigma, counts, medians, pooled_n). Per-city median
    removed before pooling; sigma = max(1.4826×MAD(pool), 1.0°C)."""
    pool = []
    counts = {}
    medians = {}
    for city, rs in residuals_by_city.items():
        rs = [r for r in rs if r is not None]
        counts[city] = len(rs)
        if not rs:
            continue
        med = statistics.median(rs)
        medians[city] = med
        pool.extend(r - med for r in rs)
    if len(pool) < 5:
        return 1.0, counts, medians, len(pool)  # floor; gate inert until ≥5/city
    mad = statistics.median([abs(x) for x in pool])
    sigma = max(1.4826 * mad, 1.0)
    return sigma, counts, medians, len(pool)


def main():
    db = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else DB_PATH
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT city, residual FROM station_obs WHERE residual IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    by_city = {}
    for city, r in rows:
        by_city.setdefault(city, []).append(float(r))
    if not by_city:
        print(f"# no station_obs residuals in {db}")
        return
    sigma, counts, meds, n = compute_sigma_calm(by_city)
    print(f"# Bot C gate σ_calm freeze (source: {db})")
    print(f"# pooled n={n} across {len(counts)} cities; 1.4826×MAD, 1.0°C floor")
    for city in sorted(counts):
        m = meds.get(city)
        med_s = f"{m:+.3f}" if m is not None else "—"
        print(f"#   {city:14s} n={counts[city]:3d}  median={med_s}")
    low = [c for c, k in counts.items() if k < 5]
    if low:
        print(f"# WARNING: <5 residuals in {low} → gate INERT there until ≥5")
    if sigma < 0.7 or sigma > 1.5:
        print(f"# NOTE: σ_calm={sigma:.3f}°C outside Fable's 0.7-1.5 prior — "
              f"eyeball residuals / re-examine before freezing")
    print(f"SIGMA_CALM = {sigma:.3f}")
    print(f"# → paste into bot/config.py (replaces the 1.0 floor), commit, VPS pull")


def _demo():
    # two cities, residuals -2..+2 (median 0) → pool MAD=1.0 → σ=1.4826 (above floor)
    data = {"A": [-2, -1, 0, 1, 2], "B": [-2, -1, 0, 1, 2]}
    sigma, counts, meds, n = compute_sigma_calm(data)
    assert n == 10, n
    assert counts == {"A": 5, "B": 5}, counts
    assert abs(sigma - 1.4826) < 1e-9, sigma
    # floor case: a single city, calm (MAD=0) → floor 1.0
    s2, *_ = compute_sigma_calm({"A": [0.0, 0.0, 0.0, 0.0, 0.0]})
    assert s2 == 1.0, s2
    print("OK — backtest_variance_gate math self-check passed")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        _demo()
    else:
        main()
