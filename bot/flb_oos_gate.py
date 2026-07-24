import hashlib
import math
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, "/root/polybot")
sys.path.insert(0, "/root/polybot/bot")

import build_flb_calib as bfc
from build_flb_calib import _iter_records, _to_outcome, _to_price, _price_idx, SNAPSHOTS
from config import FLB_PRICE_BUCKET, MIN_EDGE, PRICE_BAND

ZENODO_PATH = bfc.ZENODO_PATH
TAKER_FEE = 0.02
HALF_SPREAD = 0.01
COST = TAKER_FEE + HALF_SPREAD
SCORE_SNAPS = (("24h", "p24h_price"), ("7d", "p7d_price"))
RNG_SEED = 42
N_BOOT = 2000


def _mid(rec):
    return str(rec.get("market_id") or rec.get("id") or "")


def _test_split(rec):
    h = int(hashlib.md5(_mid(rec).encode()).hexdigest(), 16)
    return (h % 5) == 0


def build_calib_on(records):
    sums = {s: defaultdict(float) for s in SNAPSHOTS}
    counts = {s: defaultdict(int) for s in SNAPSHOTS}
    for rec in records:
        outcome = _to_outcome(bfc._get(rec, "outcome"))
        if outcome is None:
            continue
        for snap, logical in (("final", "final_price"), ("24h", "p24h_price"), ("7d", "p7d_price")):
            p = _to_price(bfc._get(rec, logical))
            if p is None:
                continue
            idx = _price_idx(p)
            if idx is None:
                continue
            sums[snap][idx] += outcome
            counts[snap][idx] += 1
    table = {}
    for snap in SNAPSHOTS:
        table[snap] = []
        for idx in range(bfc.PRICE_BUCKETS):
            c = counts[snap].get(idx, 0)
            if c >= bfc.MIN_CELL_N:
                table[snap].append([round(sums[snap][idx] / c, 4), c])
            else:
                table[snap].append(None)
    return table


def calib_lookup(table, snap, price):
    idx = _price_idx(price)
    if idx is None:
        return None
    cell = table[snap][idx]
    if cell is None:
        return None
    return cell[0]


def logloss(p, y):
    p = min(max(p, 1e-12), 1 - 1e-12)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def pava_fit(x, y):
    order = np.argsort(x)
    xs = np.asarray(x)[order]
    ys = np.asarray(y, dtype=float)[order]
    n = len(ys)
    val = list(ys)
    w = [1.0] * n
    i = 0
    stack = []
    for j in range(n):
        stack.append([val[j], w[j]])
        while len(stack) >= 2 and stack[-2][0] > stack[-1][0]:
            a = stack.pop()
            b = stack.pop()
            tw = b[1] + a[1]
            tv = (b[0] * b[1] + a[0] * a[1]) / tw
            stack.append([tv, tw])
    fitted = []
    for block in stack:
        fitted.extend([block[0]] * int(round(block[1])))
    fitted = np.asarray(fitted)
    inv = np.empty(n, dtype=int)
    inv[order] = np.arange(n)
    return xs, fitted, inv


def cluster_boot_mean(diff, clusters, n_boot, seed):
    diffs = np.asarray(diff, dtype=float)
    clusters = np.asarray(clusters)
    order = np.argsort(clusters, kind="stable")
    sc = clusters[order]
    sd = diffs[order]
    uniq, starts = np.unique(sc, return_index=True)
    if len(uniq) == 0:
        return float("nan"), float("nan"), float("nan")
    group_sum = np.add.reduceat(sd, starts)
    group_size = np.diff(np.append(starts, len(sc)))
    rng = np.random.default_rng(seed)
    boot = np.empty(n_boot)
    n_c = len(uniq)
    for b in range(n_boot):
        idx = rng.integers(0, n_c, size=n_c)
        sz = group_size[idx]
        s = group_sum[idx]
        tot = sz.sum()
        boot[b] = s.sum() / tot if tot > 0 else float("nan")
    return float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5)), float(boot.mean())


def main():
    recs = list(_iter_records(ZENODO_PATH))
    print(f"[load] records={len(recs)}")
    test = [r for r in recs if _test_split(r)]
    train = [r for r in recs if not _test_split(r)]
    print(f"[split] train={len(train)} test={len(test)} (md5%5==0 -> test)")

    table = build_calib_on(train)
    populated = {s: sum(1 for c in table[s] if c) for s in SNAPSHOTS}
    print(f"[train calib] populated cells: {populated}")

    iso_x, iso_y = [], []
    for rec in train:
        outcome = _to_outcome(bfc._get(rec, "outcome"))
        if outcome is None:
            continue
        for snap, logical in SCORE_SNAPS:
            p = _to_price(bfc._get(rec, logical))
            if p is None or not (PRICE_BAND[0] <= p <= PRICE_BAND[1]):
                continue
            iso_x.append(p)
            iso_y.append(outcome)
    iso_xs, iso_fitted, iso_inv = pava_fit(iso_x, iso_y)
    print(f"[isotonic] train points={len(iso_x)}")

    rows = []
    for rec in test:
        outcome = _to_outcome(bfc._get(rec, "outcome"))
        if outcome is None:
            continue
        mid = _mid(rec)
        for snap, logical in SCORE_SNAPS:
            p = _to_price(bfc._get(rec, logical))
            if p is None or not (PRICE_BAND[0] <= p <= PRICE_BAND[1]):
                continue
            p_calib = calib_lookup(table, snap, p)
            p_model = p_calib if p_calib is not None else p
            j = np.searchsorted(iso_xs, p)
            j = min(j, len(iso_fitted) - 1)
            p_iso = float(iso_fitted[j]) if len(iso_fitted) else p
            rows.append({
                "mid": mid, "snap": snap, "p": p, "p_model": p_model, "p_iso": p_iso,
                "y": outcome,
                "ll_calib": logloss(p_model, outcome),
                "ll_raw": logloss(p, outcome),
                "ll_iso": logloss(p_iso, outcome),
            })
    print(f"[test] scored points (24h+7d, in-band)={len(rows)} "
          f"clusters={len(set(r['mid'] for r in rows))}")

    mids = np.array([r["mid"] for r in rows])
    diff_info = np.array([r["ll_raw"] - r["ll_calib"] for r in rows])
    diff_iso = np.array([r["ll_raw"] - r["ll_iso"] for r in rows])
    lo_i, hi_i, mu_i = cluster_boot_mean(diff_info, mids, N_BOOT, RNG_SEED)
    lo_s, hi_s, mu_s = cluster_boot_mean(diff_iso, mids, N_BOOT, RNG_SEED + 1)
    print("\n=== GATE-1 INFO (OOS log-loss improvement, cluster-boot by market_id) ===")
    print(f"  calib vs raw : mean={mu_i:.6f}  95% CI=[{lo_i:.6f}, {hi_i:.6f}]  "
          f"{'PASS (CI>0)' if lo_i > 0 else 'FAIL -> FLB fiction-by-construction'}")
    print(f"  isotonic ctrl: mean={mu_s:.6f}  95% CI=[{lo_s:.6f}, {hi_s:.6f}]")
    iso_kills = (mu_s >= mu_i) or (lo_s >= lo_i)
    print(f"  isotonic-vs-calib: {'KILL (generic recalibration matches/beats calib -> non-specific)'
          if iso_kills else 'ok (calib beats generic isotonic)'}")

    trades = {"calib": [], "isotonic": [], "raw_price": []}
    for r in rows:
        for label, p_m in (("calib", r["p_model"]), ("isotonic", r["p_iso"]), ("raw_price", r["p"])):
            gap = p_m - r["p"]
            if abs(gap) <= COST:
                continue
            if gap > 0:
                realized = r["y"] - r["p"] - COST
            else:
                realized = r["p"] - r["y"] - COST
            trades[label].append((r["mid"], realized))

    print("\n=== GATE-2 PnL-after-costs (trade both sides when |p_model-price|>COST) ===")
    print(f"  costs: taker_fee={TAKER_FEE} half_spread={HALF_SPREAD} (total {COST})")
    for label in ("calib", "isotonic", "raw_price"):
        lst = trades[label]
        if not lst:
            print(f"  {label:9s}: 0 qualifying trades (|gap| never exceeded COST)")
            continue
        mids_sub = np.array([m for m, _ in lst])
        vals = np.array([v for _, v in lst])
        lo_p, hi_p, mu_p = cluster_boot_mean(vals, mids_sub, N_BOOT, 7 + (hash(label) & 7))
        print(f"  {label:9s}: n={len(lst)} mean=${mu_p:.4f} 95% CI=[${lo_p:.4f}, ${hi_p:.4f}]")
    calib_lst = trades["calib"]
    iso_lst = trades["isotonic"]
    if calib_lst and iso_lst:
        cm = np.mean([v for _, v in calib_lst])
        im = np.mean([v for _, v in iso_lst])
        print(f"  calib mean ${cm:.4f} vs isotonic mean ${im:.4f} -> "
              f"{'calib wins' if cm > im else 'isotonic >= calib (KILL: non-specific)'}")
    print("  (PnL gate: calib CI lower bound > 0 AND calib beats isotonic -> tradeable)")

    brute = sum(logloss(r["p_model"], r["y"]) for r in rows[:50])
    vec = float(np.sum([r["ll_calib"] for r in rows[:50]]))
    print("\n[self-check] logloss brute==vec:", abs(brute - vec) < 1e-9,
          f"({brute:.6f} vs {vec:.6f})")
    mono = bool(np.all(np.diff(iso_fitted) >= -1e-12)) if len(iso_fitted) > 1 else True
    print(f"[self-check] isotonic fitted monotone non-decreasing: {mono}")
    print(f"[self-check] final snapshot EXCLUDED from scoring: {'final' not in [s for s,_ in SCORE_SNAPS]}")


if __name__ == "__main__":
    main()
