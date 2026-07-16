import argparse
import json
from collections import defaultdict
from pathlib import Path

from bot.config import BOT_DIR, FLB_CALIB_PATH, FLB_PRICE_BUCKET

ZENODO_PATH = BOT_DIR / "data" / "zenodo_resolved.jsonl"

FIELD_MAP = {
    "outcome": "resolved_yes",
    "final_price": "price_final",
    "p24h_price": "price_24h",
    "p7d_price": "price_7d",
}
MIN_CELL_N = 50
PRICE_BUCKETS = int(round(1.0 / FLB_PRICE_BUCKET))
SNAPSHOTS = ["final", "24h", "7d"]
SNAP_LAG_DAYS = {"final": 0.0, "24h": 1.0, "7d": 7.0}


def _price_idx(p):
    if p is None:
        return None
    idx = int(p / FLB_PRICE_BUCKET)
    if idx < 0 or idx >= PRICE_BUCKETS:
        return None
    return idx


def _get(rec, logical):
    field = FIELD_MAP[logical]
    if field in rec:
        return rec[field]
    for k in rec:
        if k.lower().replace("-", "_") == field.lower():
            return rec[k]
    return None


def _to_outcome(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return 1 if v >= 0.5 else 0
    s = str(v).strip().lower()
    if s in ("yes", "1", "true", "resolved_yes"):
        return 1
    if s in ("no", "0", "false", "resolved_no"):
        return 0
    return None


def _to_price(v):
    if v is None:
        return None
    try:
        p = float(v)
    except (TypeError, ValueError):
        return None
    if p < 0.0 or p > 1.0:
        return None
    return p


def build(path):
    sums = {s: defaultdict(float) for s in SNAPSHOTS}
    counts = {s: defaultdict(int) for s in SNAPSHOTS}
    n_markets = 0
    n_points = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            outcome = _to_outcome(_get(rec, "outcome"))
            if outcome is None:
                continue
            n_markets += 1
            for snap, logical in (("final", "final_price"),
                                  ("24h", "p24h_price"),
                                  ("7d", "p7d_price")):
                p = _to_price(_get(rec, logical))
                if p is None:
                    continue
                idx = _price_idx(p)
                if idx is None:
                    continue
                sums[snap][idx] += outcome
                counts[snap][idx] += 1
                n_points += 1
    table = {}
    for snap in SNAPSHOTS:
        table[snap] = []
        for idx in range(PRICE_BUCKETS):
            c = counts[snap].get(idx, 0)
            if c >= MIN_CELL_N:
                table[snap].append([round(sums[snap][idx] / c, 4), c])
            else:
                table[snap].append(None)
    out = {
        "price_bucket_size": FLB_PRICE_BUCKET,
        "snapshots": SNAPSHOTS,
        "snap_lag_days": SNAP_LAG_DAYS,
        "min_cell_n": MIN_CELL_N,
        "n_markets": n_markets,
        "n_points": n_points,
        "table": table,
    }
    return out


def inspect(path, n=1):
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i >= n:
                break
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            print(f"record {i} keys: {list(rec.keys())}")
            print(json.dumps(rec, indent=2, default=str)[:800])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=str(ZENODO_PATH))
    ap.add_argument("--output", default=str(FLB_CALIB_PATH))
    ap.add_argument("--inspect", action="store_true")
    args = ap.parse_args()
    src = Path(args.input)
    if not src.exists():
        print(f"input not found: {src}")
        print("download Zenodo DOI 10.5281/zenodo.20776479 -> data/zenodo_resolved.jsonl")
        return
    if args.inspect:
        inspect(src)
        return
    out = build(src)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(f"wrote {args.output}: markets={out['n_markets']} points={out['n_points']}")
    _report(out)


def _report(out):
    print("\nFLB calibration (realized YES freq by price bucket, per snapshot):")
    for snap in out["snapshots"]:
        cells = out["table"][snap]
        populated = [(i, c[0], c[1]) for i, c in enumerate(cells) if c]
        if not populated:
            print(f"  {snap}: NO populated cells (need n>={out['min_cell_n']})")
            continue
        lo = populated[0]
        mid = populated[len(populated) // 2]
        hi = populated[-1]
        print(f"  {snap}: n_points={sum(c[1] for c in populated)}")
        print(f"    longshot  [{lo[0]*0.05:.2f}-{lo[0]*0.05+0.05:.2f}] freq={lo[1]:.3f} n={lo[2]}")
        print(f"    middle    [{mid[0]*0.05:.2f}-{mid[0]*0.05+0.05:.2f}] freq={mid[1]:.3f} n={mid[2]}")
        print(f"    favourite [{hi[0]*0.05:.2f}-{hi[0]*0.05+0.05:.2f}] freq={hi[1]:.3f} n={hi[2]}")
    print("\nGO/NO-GO: longshot freq should be << bucket midpoint; favourite freq >> midpoint.")
    print("If freq ~= price bucket midpoint at all snapshots, FLB is dead on modern Polymarket.")


if __name__ == "__main__":
    main()
