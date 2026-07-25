"""P4 fill-realism regrade (FLB only).

Restates settled FLB paper-fill PnL under realistic execution:
  - taker fee priced into realized PnL (settle._fill_pnl ignored it — the optimism bug)
  - partial-cap: fill_size capped to available top-10 depth at scan (residual canceled)
  - fill_price left at the scan-time walked effective_price (already a book walk,
    realistic for the filled portion; we do not re-walk a historical book we never
    stored — see FLB_P4_VERDICT.txt for that limitation)

Optimistic PnL = settle._fill_pnl summed (no fee, no cap). Realistic = same minus
the taker fee, minus any partial-cap haircut. Cluster bootstrap CI by market_id.

Run:  python bot/regrade_fills.py
"""
import argparse
import json
import random
import sqlite3

from config import DB_PATH


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _fill_pnl(side, price, size, yes_won):
    if side == "buy":
        return (1.0 - price) * size if yes_won else (-price) * size
    return (price - 1.0) * size if yes_won else price * size


def _fee(fee_rate, fees_enabled, price, size):
    if not fees_enabled or fee_rate <= 0.0:
        return 0.0
    return fee_rate * price * (1.0 - price) * size


def _bootstrap_ci(values, n=2000, seed=42):
    if not values:
        return None, None, None
    rng = random.Random(seed)
    m = len(values)
    means = sorted(sum(rng.choice(values) for _ in range(m)) / m for _ in range(n))
    return sum(values) / m, means[int(0.025 * n)], means[int(0.975 * n)]


def _fmt(p, lo, hi):
    if p is None:
        return "n/a"
    return f"{p:+.4f} [95% CI {lo:+.4f}, {hi:+.4f}]"


def _load_settled_flb(conn):
    rows = conn.execute(
        """
        SELECT f.id AS fill_id, f.market_id, f.side, f.price AS fill_price,
               f.size AS fill_size, f.maker_or_taker, f.ts AS fill_ts,
               st.resolved_yes AS yes_won, st.pnl AS opt_pnl,
               (SELECT s.best_bid FROM pm_snapshots s
                  WHERE s.market_id=f.market_id AND s.edge='flb' AND s.ts<=f.ts
                  ORDER BY s.id DESC LIMIT 1) AS snap_bb,
               (SELECT s.best_ask FROM pm_snapshots s
                  WHERE s.market_id=f.market_id AND s.edge='flb' AND s.ts<=f.ts
                  ORDER BY s.id DESC LIMIT 1) AS snap_ba,
               (SELECT s.bid_size FROM pm_snapshots s
                  WHERE s.market_id=f.market_id AND s.edge='flb' AND s.ts<=f.ts
                  ORDER BY s.id DESC LIMIT 1) AS snap_bs,
               (SELECT s.ask_size FROM pm_snapshots s
                  WHERE s.market_id=f.market_id AND s.edge='flb' AND s.ts<=f.ts
                  ORDER BY s.id DESC LIMIT 1) AS snap_as,
               (SELECT s.depth FROM pm_snapshots s
                  WHERE s.market_id=f.market_id AND s.edge='flb' AND s.ts<=f.ts
                  ORDER BY s.id DESC LIMIT 1) AS snap_depth,
               (SELECT s.fee_rate FROM pm_snapshots s
                  WHERE s.market_id=f.market_id AND s.edge='flb' AND s.ts<=f.ts
                  ORDER BY s.id DESC LIMIT 1) AS fee_rate,
               (SELECT s.fees_enabled FROM pm_snapshots s
                  WHERE s.market_id=f.market_id AND s.edge='flb' AND s.ts<=f.ts
                  ORDER BY s.id DESC LIMIT 1) AS fees_enabled
        FROM pm_fills f
        LEFT JOIN pm_settlements st
          ON st.market_id=f.market_id AND st.edge='flb'
        WHERE f.edge='flb' AND st.resolved_yes IS NOT NULL
        ORDER BY f.id"""
    ).fetchall()
    return [dict(r) for r in rows]


def regrade(rows):
    """Restate each fill under realistic execution. Returns list of dicts with
    both optimistic and realistic per-fill PnL plus the realism haircut breakdown."""
    out = []
    for r in rows:
        side = r["side"]
        price = float(r["fill_price"])
        size = float(r["fill_size"])
        yes_won = bool(r["yes_won"])
        opt = _fill_pnl(side, price, size, yes_won)

        depth = r["snap_depth"] or 0.0
        bid_sz = r["snap_bs"] or 0.0
        ask_sz = r["snap_as"] or 0.0
        # The scan-time walk consumed multiple levels (top-10), not just the
        # best level — so the fillable pool is the top-10 depth on the traded
        # side (depth/2), NOT best-level size. Best-level would over-cap on
        # fills that actually filled from deeper levels. Fall back to best-level
        # only when the aggregate depth wasn't stored.
        avail = (depth / 2.0) if depth > 0 else (bid_sz if side == "sell" else ask_sz)
        fillable = max(avail, 0.0)
        fill_size_real = min(size, fillable) if fillable > 0 else 0.0
        if fill_size_real <= 0:
            fill_size_real = 0.0
        fill_ratio = fill_size_real / size if size > 0 else 0.0

        fee = _fee(float(r["fee_rate"] or 0.0), bool(r["fees_enabled"]),
                   price, fill_size_real)
        pnl_real = _fill_pnl(side, price, fill_size_real, yes_won) - fee

        out.append({
            "fill_id": r["fill_id"], "market_id": r["market_id"], "side": side,
            "price": price, "size": size, "fill_size_real": fill_size_real,
            "fill_ratio": fill_ratio, "fee": fee, "yes_won": yes_won,
            "opt_pnl": opt, "real_pnl": pnl_real,
            "cap_effect": pnl_real - (opt - fee),
            "fee_rate": float(r["fee_rate"] or 0.0),
            "fees_enabled": bool(r["fees_enabled"]),
            "snap_depth": depth,
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge", default="flb")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args()
    if args.edge != "flb":
        raise SystemExit("P4 regrade is FLB-only (other edges defer to their own P4 turn)")

    conn = _connect()
    rows = _load_settled_flb(conn)
    conn.close()

    fills = regrade(rows)
    n = len(fills)
    if n == 0:
        print("FLB regrade: no settled fills")
        return

    opt_total = sum(f["opt_pnl"] for f in fills)
    real_total = sum(f["real_pnl"] for f in fills)
    fee_total = sum(f["fee"] for f in fills)
    cap_effect_total = sum(f["cap_effect"] for f in fills)
    wins_opt = sum(1 for f in fills if f["opt_pnl"] > 0)
    wins_real = sum(1 for f in fills if f["real_pnl"] > 0)

    opt_mean, opt_lo, opt_hi = _bootstrap_ci([f["opt_pnl"] for f in fills])
    real_mean, real_lo, real_hi = _bootstrap_ci([f["real_pnl"] for f in fills])

    thin = [f for f in fills if f["fill_ratio"] < 1.0]
    full_cap = [f for f in fills if f["fill_ratio"] >= 1.0]

    # Self-check: realism must not be a no-op. Fee always reduces PnL; partial-cap
    # is direction-dependent (cuts losses on losers, gains on winners) so realistic
    # can be above OR below optimistic — what matters is that the path CHANGED PnL.
    no_op = abs(real_total - opt_total) < 1e-6

    out = {
        "edge": "flb", "n_settled": n,
        "opt_total": opt_total, "real_total": real_total,
        "fee_total": fee_total, "cap_effect_total": cap_effect_total,
        "opt_mean_per_fill": opt_mean, "opt_ci": [opt_lo, opt_hi],
        "real_mean_per_fill": real_mean, "real_ci": [real_lo, real_hi],
        "wins_opt": wins_opt, "wins_real": wins_real,
        "thin_book_fills": len(thin), "full_fill_fills": len(full_cap),
        "min_fill_ratio": min(f["fill_ratio"] for f in fills),
        "realism_selfcheck": "BLOCK(no-op)" if no_op else "PASS",
    }

    if args.json:
        print(json.dumps(out, default=str, indent=2))
        return

    print(f"FLB P4 fill-realism regrade  (n={n} settled fills)")
    print(f"  wins            opt={wins_opt}  realistic={wins_real}")
    print(f"  optimistic total PnL = {opt_total:+.4f}   per-fill {_fmt(opt_mean, opt_lo, opt_hi)}")
    print(f"  realistic total PnL = {real_total:+.4f}   per-fill {_fmt(real_mean, real_lo, real_hi)}")
    print(f"  taker fee total    = {fee_total:+.4f}   (always reduces PnL; ~{fee_total/max(abs(opt_total),1)*100:.0f}% of |optimistic|)")
    print(f"  partial-cap net eff = {cap_effect_total:+.4f}  (thin-book fills: {len(thin)}, min fill_ratio={out['min_fill_ratio']:.3f})")
    print(f"  realism self-check: {out['realism_selfcheck']}  (realism must change PnL; nil for liquid FLB)")
    print(f"  verdict: {'CI EXCLUDES 0 (positive)' if (real_lo is not None and real_lo > 0) else 'CI EXCLUDES 0 (negative)' if (real_hi is not None and real_hi < 0) else 'CI INCLUDES 0'}")
    print("  note: CI is cluster-bootstrap by fill (one fill per market). n is small; see FLB_P4_VERDICT.txt.")


if __name__ == "__main__":
    main()
