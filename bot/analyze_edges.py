import argparse
import json
import random
import sqlite3
from collections import defaultdict

from config import ARB_MIN_GAP, CROSSVENUE_MIN_GAP, DB_PATH, MIN_EDGE


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _bootstrap_ci(values, n=1000, seed=42):
    if not values:
        return None, None, None
    rng = random.Random(seed)
    m = len(values)
    means = sorted(sum(rng.choice(values) for _ in range(m)) / m for _ in range(n))
    return sum(values) / m, means[int(0.025 * n)], means[int(0.975 * n)]


def _fmt(point, lo, hi):
    if point is None:
        return "n/a"
    return f"{point:+.4f} [95% CI {lo:+.4f}, {hi:+.4f}]"


def _flb_signals(conn):
    rows = conn.execute(
        """SELECT c.p_model, c.effective_price, c.side, c.market_id, c.ts,
                  st.resolved_yes AS outcome, st.pnl
           FROM pm_candidates c
           LEFT JOIN pm_settlements st ON st.market_id = c.market_id AND st.edge = 'flb'
           WHERE c.edge = 'flb' AND st.resolved_yes IS NOT NULL"""
    ).fetchall()
    return [{"p": r["p_model"], "price": r["effective_price"], "side": r["side"],
             "outcome": 1 if r["outcome"] else 0, "pnl": r["pnl"] or 0.0} for r in rows]


def _flb_section(conn):
    sigs = _flb_signals(conn)
    print(f"FLB resolved signals: {len(sigs)}")
    if not sigs:
        print("  insufficient data")
        return None
    deciles = [(0.0, 0.1), (0.1, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.01)]
    print("Reliability by price bucket (n>=10):")
    maxdev = 0.0
    for lo, hi in deciles:
        grp = [s for s in sigs if lo <= s["price"] < hi]
        if len(grp) < 10:
            continue
        mean_p = sum(s["p"] for s in grp) / len(grp)
        freq = sum(s["outcome"] for s in grp) / len(grp)
        dev = abs(mean_p - freq)
        maxdev = max(maxdev, dev)
        print(f"  [{lo:.2f},{hi:.2f}) n={len(grp):4d} mean_p={mean_p:.3f} freq={freq:.3f} dev={dev*100:.1f}pp")
    pnls = [s["pnl"] for s in sigs]
    p, lo, hi = _bootstrap_ci(pnls)
    print(f"  mean realized PnL/signal = {_fmt(p, lo, hi)}")
    print(f"  max reliability deviation = {maxdev*100:.1f}pp")
    return len(sigs), maxdev, (p, lo, hi)


def _arb_signals(conn):
    rows = conn.execute(
        """SELECT f.edge, f.market_id, f.side, f.price, f.size, f.meta_json,
                  st.resolved_yes, st.pnl, o.meta_json AS order_meta
           FROM pm_fills f
           LEFT JOIN pm_settlements st ON st.market_id = f.market_id AND st.edge = 'arb'
           LEFT JOIN pm_orders o ON o.id = f.order_id
           WHERE f.edge = 'arb'"""
    ).fetchall()
    bundles = defaultdict(list)
    for r in rows:
        try:
            meta = json.loads(r["order_meta"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        b = meta.get("bundle", r["market_id"])
        bundles[b].append(dict(r))
    return bundles


def _arb_section(conn):
    bundles = _arb_signals(conn)
    settled = [b for b in bundles.values() if all(r["resolved_yes"] is not None for r in b)]
    print(f"ARB bundles seen: {len(bundles)} | settled: {len(settled)}")
    if not settled:
        print("  insufficient data")
        return None
    pnls = [sum((r["pnl"] or 0) for r in b) for b in settled]
    p, lo, hi = _bootstrap_ci(pnls)
    print(f"  mean realized bundle PnL = {_fmt(p, lo, hi)}")
    wins = sum(1 for x in pnls if x > 0)
    print(f"  winning bundles: {wins}/{len(pnls)}")
    return len(pnls), (p, lo, hi)


def _cv_section(conn):
    rows = conn.execute(
        "SELECT gap, net_of_fees, COUNT(*) OVER() AS dummy FROM cv_gaps ORDER BY id"
    ).fetchall()
    n = len(rows)
    print(f"CROSS-VENUE gaps logged: {n}")
    if not n:
        print("  insufficient data")
        return None
    gaps = [r["gap"] for r in rows]
    nets = [r["net_of_fees"] for r in rows]
    above = sum(1 for x in nets if x > CROSSVENUE_MIN_GAP)
    print(f"  gap: mean={sum(gaps)/n:.4f} max={max(gaps):.4f}")
    print(f"  net: mean={sum(nets)/n:.4f}  >{CROSSVENUE_MIN_GAP:.0%} threshold: {above}/{n}")
    unique_pairs = len(set(r["net_of_fees"] for r in rows))
    print(f"  distinct net values: {unique_pairs}")
    return n, above


def _flb_gate(stats):
    print("FLB gate:")
    if not stats:
        print("  FAIL (no data)")
        return
    n, maxdev, pnl = stats
    g1 = n >= 200
    g2 = maxdev is not None and maxdev <= 0.10
    g3 = pnl is not None and pnl[0] is not None and pnl[0] > 0 and pnl[1] > 0
    print(f"  >=200 resolved signals: {'PASS' if g1 else 'FAIL'} (n={n})")
    print(f"  max reliability deviation <=10pp: {'PASS' if g2 else 'FAIL'} ({maxdev*100:.1f}pp)" if maxdev is not None else "  reliability: FAIL")
    print(f"  net PnL>0, CI excludes 0: {'PASS' if g3 else 'FAIL'} ({_fmt(*pnl)})")


def _arb_gate(stats):
    print("ARB gate:")
    if not stats:
        print("  FAIL (no data)")
        return
    n, pnl = stats
    g1 = n >= 50
    g2 = pnl is not None and pnl[0] is not None and pnl[0] > 0 and pnl[1] > 0
    print(f"  >=50 settled bundles: {'PASS' if g1 else 'FAIL'} (n={n})")
    print(f"  net arb PnL>0, CI excludes 0: {'PASS' if g2 else 'FAIL'} ({_fmt(*pnl)})")


def _cv_gate(stats):
    print("CROSS-VENUE gate:")
    if not stats:
        print("  FAIL (no data)")
        return
    n, above = stats
    print(f"  gaps logged: {n}")
    print(f"  gaps net > {CROSSVENUE_MIN_GAP:.0%} threshold: {above}")
    print("  go/no-go: review persistence + simulated post-hoc ROI (manual, needs resolution data)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge", required=True, choices=["flb", "arb", "crossvenue"])
    args = ap.parse_args()
    conn = _connect()
    if args.edge == "flb":
        _flb_gate(_flb_section(conn))
    elif args.edge == "arb":
        _arb_gate(_arb_section(conn))
    else:
        _cv_gate(_cv_section(conn))
    conn.close()


if __name__ == "__main__":
    main()
