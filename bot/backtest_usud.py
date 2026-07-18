import argparse
import random
import sqlite3
from collections import defaultdict

from config import DB_PATH, USUD_MIN_EDGE

SPREAD = 0.05
TAKER_FEE = 0.04


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _load_quotes(conn):
    rows = conn.execute(
        """SELECT q.id, q.scan_id, q.market_id, q.ticker, q.ts, q.end_date,
                  q.market_ask, q.market_bid, q.p_model, q.spot, q.iv,
                  q.prior_close, q.tau_years, q.buy_edge, q.sell_edge,
                  st.resolved_yes AS outcome
           FROM usud_quotes q
           LEFT JOIN pm_settlements st
             ON st.market_id = q.market_id AND st.edge = 'usud'
           WHERE st.resolved_yes IS NOT NULL
           ORDER BY q.id"""
    ).fetchall()
    return [dict(r) for r in rows]


def _bootstrap_ci(values, n=1000, seed=42):
    if not values:
        return None, None, None
    rng = random.Random(seed)
    m = len(values)
    means = sorted(sum(rng.choice(values) for _ in range(m)) / m for _ in range(n))
    return sum(values) / m, means[int(0.025 * n)], means[int(0.975 * n)]


def _trade_pnl(side, entry, size, yes_won):
    if side == "buy":
        return ((1.0 - entry) * size) if yes_won else (-entry * size)
    return ((entry - 1.0) * size) if yes_won else (entry * size)


def _sim(rows, threshold, kelly_frac, bankroll=1000.0, per_trade_cap=50.0):
    trades = []
    cur = bankroll
    peak = bankroll
    max_dd = 0.0
    for r in rows:
        be, se = r["buy_edge"], r["sell_edge"]
        if be is None or se is None:
            continue
        side, edge, entry = None, 0.0, 0.0
        if be >= threshold and se >= threshold:
            if be >= se:
                side, edge, entry = "buy", be, r["market_ask"]
            else:
                side, edge, entry = "sell", se, r["market_bid"]
        elif be >= threshold:
            side, edge, entry = "buy", be, r["market_ask"]
        elif se >= threshold:
            side, edge, entry = "sell", se, r["market_bid"]
        if side is None:
            continue
        f = max(0.0, edge) * kelly_frac
        notional = min(f * cur, per_trade_cap)
        if notional <= 0.0:
            continue
        size = notional / entry
        pnl = _trade_pnl(side, entry, size, bool(r["outcome"]))
        trades.append(pnl)
        cur += pnl
        peak = max(peak, cur)
        max_dd = max(max_dd, (peak - cur) / peak) if peak > 0 else max_dd
    return {
        "n": len(trades), "trades": trades, "final": cur,
        "win_rate": (sum(1 for t in trades if t > 0) / len(trades)) if trades else 0.0,
        "max_drawdown": max_dd,
        "total_pnl": sum(trades),
    }


def _brier(rows, side_pick_fn):
    picked = [(r["p_model"], r["outcome"]) for r in rows if side_pick_fn(r) is not None]
    if not picked:
        return None, None, 0
    bm = sum((r["market_ask"] - r["outcome"]) ** 2 for r in rows if side_pick_fn(r) is not None) / len(picked)
    bmod = sum((p - o) ** 2 for p, o in picked) / len(picked)
    return bmod, bm, len(picked)


def _reliability(rows, threshold):
    picked = [r for r in rows
              if (r["buy_edge"] is not None and r["buy_edge"] >= threshold)
              or (r["sell_edge"] is not None and r["sell_edge"] >= threshold)]
    if len(picked) < 10:
        return None
    buckets = [(0.0, 0.1), (0.1, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.01)]
    maxdev = 0.0
    for lo, hi in buckets:
        grp = [r for r in picked if lo <= r["p_model"] < hi]
        if len(grp) < 5:
            continue
        mean_p = sum(r["p_model"] for r in grp) / len(grp)
        freq = sum(r["outcome"] for r in grp) / len(grp)
        maxdev = max(maxdev, abs(mean_p - freq))
    return maxdev


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--thresholds", default="0.03,0.05,0.07,0.10")
    ap.add_argument("--kelly", type=float, default=0.25)
    ap.add_argument("--bankroll", type=float, default=1000.0)
    args = ap.parse_args()
    conn = _connect()
    try:
        rows = _load_quotes(conn)
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    print(f"USUD backtest: {len(rows)} resolved quotes\n")
    if not rows:
        print("  insufficient data (let the paper bot run a few days first)")
        return
    by_ticker = defaultdict(list)
    for r in rows:
        by_ticker[r["ticker"]].append(r)
    print(f"  per-ticker: {', '.join(f'{t}={len(v)}' for t, v in by_ticker.items())}\n")
    thresholds = [float(x) for x in args.thresholds.split(",")]
    print(f"{'thresh':>8} {'n':>5} {'win%':>6} {'meanPnL':>9} {'95%CI':>17} {'maxDD%':>7} {'reliab':>7}")
    for th in thresholds:
        sim = _sim(rows, th, args.kelly, args.bankroll)
        p, lo, hi = _bootstrap_ci(sim["trades"])
        rel = _reliability(rows, th)
        ci = f"[{lo:+.2f},{hi:+.2f}]" if p is not None else "n/a"
        rel_s = f"{rel*100:.1f}pp" if rel is not None else "n/a"
        print(f"  {th:6.2f} {sim['n']:5d} {sim['win_rate']*100:5.1f} "
              f"{(p or 0):+.4f} {ci:>17} {sim['max_drawdown']*100:6.1f} {rel_s:>7}")
    print(f"\n  kelly={args.kelly}  bankroll={args.bankroll}  spread={SPREAD}  taker_fee={TAKER_FEE}")
    print("  PASS = mean PnL CI excludes 0 + reliability <=10pp on >=6wk data")


if __name__ == "__main__":
    main()
