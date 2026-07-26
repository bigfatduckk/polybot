"""P6 fresh-OOS pre-registered FLB verdict harness.

Implements the locked rules verbatim from "P6 FLB OOS Pre-Registration
2026-07-25.md" (commit c44aede). Pre-registered BEFORE P6 data arrives — the
analysis choices are frozen HERE, not at freeze. At freeze the verdict is:
run one script, read one file (bot/FLB_P6_VERDICT.txt).

LOCKED CHOICES (this file IS the pre-registration for the computation):
  CUTOFF = '2026-07-25T00:00:00Z' (commit c44aede, the P4 honesty fix).
  Population: settled FLB fills with ts >= CUTOFF, fill_ratio > 0, both sides.
              Pre-P4 fills (ts < CUTOFF) are EXCLUDED — used only for --dry-run.
  n: >= 200 settled fills to freeze. Do NOT freeze earlier.

  PRIMARY (the verdict):
    realistic-fill realized PnL per fill (fee + partial-cap; honest path already
    re-walked+cap+taker for post-CUTOFF fills). Cluster-bootstrap by market_id
    (markets with multiple legs aggregated), n_resample=2000, seed=42.
    SHIP : mean > 0 AND 95% CI lower bound > 0.
    RE-KILL: mean <= 0 OR CI lower bound <= 0. No appeal.

  SECONDARY 1 (reliability): max |mean(p_model) - empirical_freq| <= 10pp across
    the 6 analyze_edges price buckets [0,.1),[.1,.3),[.3,.5),[.5,.7),[.7,.9),[.9,1.01)
    on settled FLB candidates; bucket n>=10 else skipped.

  SECONDARY 2 (isotonic control — Option 1, locked 2026-07-26 pre-data):
    in-sample pava(entry_price -> outcome) on the P6 fills; retrace isotonic
    trade decisions on the SAME fills; UNIT STAKE (size=1) at each fill's stored
    entry/fee/outcome. Reuses pava_fit from flb_oos_gate.py (the P1 code path).
      - entry_price = candidate effective_price (the price the edge measured).
      - p_iso = pava(entry); trade iff |p_iso - entry| > COST (0.03, P1); side by sign.
      - same-side  : isotonic PnL = FLB-calib PnL (identical row, both arms).
      - opposite   : mirror at SAME entry price; PnL_opp = _fill_pnl(opp,entry,1,y) - fee.
                     fee is symmetric in side -> unchanged. Ignores that the opposite
                     side would really cross the WIDER book side -> generous to isotonic
                     (consistent with the control's safe-direction bias). Noted in verdict.
      - no-trade   : 0.
      - FLB arm = same-side unit PnL per fill (every fill traded its actual side).
      - mean per fill, SAME n, POINT comparison (no CI — primary owns the stats).
        FLB must STRICTLY beat isotonic; tie -> re-kill (doc's "isotonic >= FLB").

  SECONDARY 3 (partial-fill): report fill_ratio distribution; if >10% of fills
    have fill_ratio < 0.5 -> SUSPEND (do not ship; re-examine fill path).

  COST = 0.03 (P1: taker 0.02 + half-spread 0.01) — isotonic retrace threshold only.

DRY-RUN: --dry-run selects the EXCLUDED pre-P4 fills (ts < CUTOFF) purely to prove
the plumbing executes end-to-end. OUTPUT IS DISCARDED, not interpreted. No verdict
file is written in dry-run.
"""
import argparse
import json
import sqlite3
import sys
from collections import defaultdict

import numpy as np

from config import DB_PATH
from flb_oos_gate import cluster_boot_mean, pava_fit

CUTOFF = "2026-07-25T00:00:00Z"
FREEZE_N = 200
N_BOOT = 2000
SEED = 42
COST = 0.03
BUCKETS = [(0.0, 0.1), (0.1, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.01)]
RELIABILITY_MAX_PP = 0.10
PARTIAL_SUSPEND_FRAC = 0.10
PARTIAL_RATIO_FLOOR = 0.5
MIN_BUCKET_N = 10


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


def _snap(conn, market_id, fill_ts):
    row = conn.execute(
        """SELECT fee_rate, fees_enabled, depth, bid_size, ask_size FROM pm_snapshots
           WHERE edge='flb' AND market_id=? AND ts<=?
             AND fee_rate IS NOT NULL
           ORDER BY id DESC LIMIT 1""",
        (market_id, fill_ts),
    ).fetchone()
    if not row:
        row = conn.execute(
            """SELECT fee_rate, fees_enabled, depth, bid_size, ask_size FROM pm_snapshots
               WHERE edge='flb' AND market_id=? ORDER BY id DESC LIMIT 1""",
            (market_id,),
        ).fetchone()
    if not row:
        return {"fee_rate": 0.0, "fees_enabled": False, "depth": 0.0,
                "bid_size": 0.0, "ask_size": 0.0}
    return {"fee_rate": float(row["fee_rate"] or 0.0),
            "fees_enabled": bool(row["fees_enabled"]),
            "depth": float(row["depth"] or 0.0),
            "bid_size": float(row["bid_size"] or 0.0),
            "ask_size": float(row["ask_size"] or 0.0)}


def _realistic(f, snap):
    """Restate one fill under realistic execution. Honest path (post-CUTOFF,
    meta carries fill_ratio) uses the capped+rewalked fill_size/fill_price as
    stored by edge_engine._store_fill. Pre-P4 path (dry-run) restates via the
    top-10 depth/2 cap from pm_snapshots (mirrors regrade_fills.py)."""
    side = f["side"]
    price = float(f["price"])
    req = float(f["size"])
    yes = bool(f["yes_won"])
    meta = json.loads(f["meta_json"] or "{}")
    if "fill_ratio" in meta:
        fill_size = float(meta.get("fill_size", req))
        fill_price = float(meta.get("fill_price", price))
    else:
        depth = snap["depth"]
        avail = (depth / 2.0) if depth > 0 else (
            snap["bid_size"] if side == "sell" else snap["ask_size"])
        fillable = max(avail, 0.0)
        fill_size = min(req, fillable) if fillable > 0 else 0.0
        fill_price = price
    fill_ratio = fill_size / req if req > 0 else 0.0
    fee = _fee(snap["fee_rate"], snap["fees_enabled"], fill_price, fill_size)
    pnl = _fill_pnl(side, fill_price, fill_size, yes) - fee
    opt = _fill_pnl(side, price, req, yes)
    return {"fill_size": fill_size, "fill_price": fill_price, "fill_ratio": fill_ratio,
            "fee": fee, "real_pnl": pnl, "opt_pnl": opt}


def load_fills(conn, op):
    rows = conn.execute(
        f"""SELECT f.id, f.market_id, f.side, f.price, f.size, f.ts, f.meta_json,
                  s.resolved_yes AS yes_won,
                  c.p_model, c.effective_price
           FROM pm_fills f
           JOIN pm_settlements s ON s.market_id=f.market_id AND s.edge='flb'
           LEFT JOIN pm_candidates c
             ON c.market_id=f.market_id AND c.side=f.side
             AND c.scan_id=(SELECT o.scan_id FROM pm_orders o WHERE o.id=f.order_id)
           WHERE f.edge='flb' AND s.resolved_yes IS NOT NULL
             AND f.ts {op} ?
           ORDER BY f.id""",
        (CUTOFF,),
    ).fetchall()
    fills = []
    for r in rows:
        d = dict(r)
        snap = _snap(conn, d["market_id"], d["ts"])
        rc = _realistic(d, snap)
        d.update(rc)
        d["snap"] = snap
        d["entry"] = float(d["effective_price"]) if d["effective_price"] is not None else float(d["price"])
        d["p_model"] = float(d["p_model"]) if d["p_model"] is not None else None
        d["yes_won"] = bool(d["yes_won"])
        fills.append(d)
    return fills


def load_candidates(conn, op):
    rows = conn.execute(
        f"""SELECT c.p_model, c.effective_price, s.resolved_yes AS outcome
           FROM pm_candidates c
           JOIN pm_settlements s ON s.market_id=c.market_id AND s.edge='flb'
           WHERE c.edge='flb' AND s.resolved_yes IS NOT NULL
             AND c.ts {op} ?""",
        (CUTOFF,),
    ).fetchall()
    return [{"p": float(r["p_model"]), "price": float(r["effective_price"]),
             "outcome": 1 if r["outcome"] else 0} for r in rows
            if r["p_model"] is not None and r["effective_price"] is not None]


def primary_gate(fills):
    fills = [f for f in fills if f["fill_ratio"] > 0.0]
    n = len(fills)
    if n == 0:
        return {"n": 0, "mean": None, "ci_lo": None, "ci_hi": None, "verdict": "NO DATA"}
    vals = np.array([f["real_pnl"] for f in fills], dtype=float)
    clusters = np.array([f["market_id"] for f in fills])
    lo, hi, mu = cluster_boot_mean(vals, clusters, N_BOOT, SEED)
    mean = float(mu)
    ship = mean > 0 and lo > 0
    opt_total = float(sum(f["opt_pnl"] for f in fills))
    real_total = float(sum(f["real_pnl"] for f in fills))
    return {"n": n, "mean": mean, "ci_lo": float(lo), "ci_hi": float(hi),
            "opt_total": opt_total, "real_total": real_total,
            "realism_selfcheck": "PASS (path changed PnL)" if abs(real_total - opt_total) > 1e-6 else "BLOCK(no-op)",
            "verdict": "SHIP-eligible (mean>0 & CI-lo>0)" if ship
            else "RE-KILL (mean<=0 or CI-lo<=0)"}


def reliability_gate(cands):
    maxdev = 0.0
    rows = []
    for lo, hi in BUCKETS:
        grp = [c for c in cands if lo <= c["price"] < hi]
        if len(grp) < MIN_BUCKET_N:
            rows.append((lo, hi, len(grp), None, None, None))
            continue
        mean_p = sum(c["p"] for c in grp) / len(grp)
        freq = sum(c["outcome"] for c in grp) / len(grp)
        dev = abs(mean_p - freq)
        maxdev = max(maxdev, dev)
        rows.append((lo, hi, len(grp), mean_p, freq, dev))
    return {"maxdev_pp": maxdev * 100.0,
            "pass": maxdev <= RELIABILITY_MAX_PP,
            "buckets": rows}


def isotonic_gate(fills):
    iso = [f for f in fills if f["fill_ratio"] > 0.0 and f["p_model"] is not None]
    n = len(iso)
    if n == 0:
        return {"n": 0, "flb_mean": None, "iso_mean": None, "pass": False}
    x = np.array([f["entry"] for f in iso], dtype=float)
    y = np.array([1 if f["yes_won"] else 0 for f in iso], dtype=float)
    _, fitted, inv = pava_fit(x, y)
    p_iso = fitted[inv]
    flb_vals = []
    iso_vals = []
    for f, pi in zip(iso, p_iso):
        entry = f["entry"]
        yes = f["yes_won"]
        snap = f["snap"]
        fee1 = _fee(snap["fee_rate"], snap["fees_enabled"], entry, 1.0)
        flb_unit = _fill_pnl(f["side"], entry, 1.0, yes) - fee1
        flb_vals.append(flb_unit)
        gap = float(pi) - entry
        if abs(gap) <= COST:
            iso_vals.append(0.0)
            continue
        iso_side = "buy" if gap > 0 else "sell"
        if iso_side == f["side"]:
            iso_vals.append(flb_unit)
        else:
            iso_vals.append(_fill_pnl(iso_side, entry, 1.0, yes) - fee1)
    flb_mean = float(np.mean(flb_vals))
    iso_mean = float(np.mean(iso_vals))
    return {"n": n, "flb_mean": flb_mean, "iso_mean": iso_mean,
            "pass": flb_mean > iso_mean,
            "note": "in-sample pava (generous to isotonic); opposite-side mirrors at "
                    "same entry (ignores wider book side -> generous to isotonic)"}


def partial_gate(fills):
    n = len(fills)
    if n == 0:
        return {"n": 0, "frac_below": None, "suspend": False}
    below = sum(1 for f in fills if f["fill_ratio"] < PARTIAL_RATIO_FLOOR)
    frac = below / n
    return {"n": n, "below": below, "frac_below": frac,
            "min_ratio": min(f["fill_ratio"] for f in fills),
            "max_ratio": max(f["fill_ratio"] for f in fills),
            "suspend": frac > PARTIAL_SUSPEND_FRAC}


def _fmt(v, d=4):
    return "n/a" if v is None else f"{v:+.{d}f}"


def render(primary, rel, iso, partial, n_total, dry_run):
    L = []
    head = "FLB P6 — fresh-OOS pre-registered verdict"
    if dry_run:
        head += "  [DRY-RUN: pre-P4 excluded fills — OUTPUT DISCARDED, plumbing proof only]"
    L.append("=" * 76)
    L.append(head)
    L.append("=" * 76)
    L.append(f"CUTOFF (c44aede): {CUTOFF}   freeze n>={FREEZE_N}   "
             f"cluster-boot n_resample={N_BOOT} seed={SEED}")
    L.append(f"settled fills in window: {n_total}   (post-CUTOFF; dry-run uses pre-CUTOFF)")
    L.append("")
    L.append("PRIMARY — realistic-fill realized PnL, cluster-boot by market_id")
    L.append(f"  n={primary['n']}   mean={_fmt(primary.get('mean'))}   "
             f"95% CI=[{_fmt(primary.get('ci_lo'))}, {_fmt(primary.get('ci_hi'))}]")
    L.append(f"  optimistic total={_fmt(primary.get('opt_total'))}  "
             f"realistic total={_fmt(primary.get('real_total'))}  "
             f"realism self-check: {primary.get('realism_selfcheck','')}")
    L.append(f"  -> {primary['verdict']}")
    L.append("")
    L.append("SECONDARY 1 — reliability (max |mean(p_model)-freq| <= 10pp, 6 buckets)")
    L.append(f"  maxdev={rel['maxdev_pp']:.1f}pp   -> {'PASS' if rel['pass'] else 'FAIL'}")
    for lo, hi, nn, mp, fr, dv in rel["buckets"]:
        if mp is None:
            L.append(f"  [{lo:.2f},{hi:.2f}) n={nn} (<{MIN_BUCKET_N}, skipped)")
        else:
            L.append(f"  [{lo:.2f},{hi:.2f}) n={nn:4d} mean_p={mp:.3f} freq={fr:.3f} "
                     f"dev={dv*100:.1f}pp")
    L.append("")
    L.append("SECONDARY 2 — isotonic control (Option 1, unit-stake, in-sample pava)")
    L.append(f"  n={iso['n']}   FLB mean={_fmt(iso.get('flb_mean'))}   "
             f"isotonic mean={_fmt(iso.get('iso_mean'))}")
    L.append(f"  -> {'PASS (FLB strictly beats isotonic)' if iso['pass'] else 'RE-KILL (isotonic >= FLB)'}")
    L.append(f"  ({iso.get('note','')})")
    L.append("")
    L.append("SECONDARY 3 — partial-fill realism (>10% below 0.5 -> SUSPEND)")
    if partial.get("frac_below") is None:
        L.append(f"  n={partial['n']}   no data")
    else:
        L.append(f"  n={partial['n']}   below<{PARTIAL_RATIO_FLOOR}: {partial['below']} "
                 f"({partial['frac_below']*100:.1f}%)   "
                 f"min_ratio={partial['min_ratio']:.3f} max_ratio={partial['max_ratio']:.3f}")
        L.append(f"  -> {'SUSPEND (>10% partial)' if partial['suspend'] else 'ok'}")
    L.append("")
    if dry_run:
        L.append("VERDICT: none — dry-run output is discarded (plumbing proof only).")
    elif primary["n"] < FREEZE_N:
        L.append(f"VERDICT: ACCRUING (n={primary['n']}/{FREEZE_N}). No freeze, no verdict.")
    else:
        ship = (primary["verdict"].startswith("SHIP") and rel["pass"]
                and iso["pass"] and not partial["suspend"])
        L.append("VERDICT: " + ("SHIP — un-HALT live arm (user-only, sized)."
                                if ship else "RE-KILL — edge stays killed; live arm HALTED."))
    L.append("")
    L.append("LIMITATIONS (locked here):")
    L.append("- fee model = fee_rate * p*(1-p) (matches P1/P4 edge gate).")
    L.append("- isotonic is in-sample (PAV ~O(n^1/3) blocks); modest inflation, safe direction.")
    L.append("- opposite-side isotonic PnL mirrors at same entry (generous to isotonic).")
    L.append("- n>=200 one fill per market (cluster=market); CI wide at 200, do not over-read mean.")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="run on EXCLUDED pre-P4 fills (ts<CUTOFF); plumbing proof, output discarded")
    ap.add_argument("--write", action="store_true",
                    help="write bot/FLB_P6_VERDICT.txt (real run, n>=FREEZE_N only)")
    args = ap.parse_args()

    op = "<" if args.dry_run else ">="
    conn = _connect()
    try:
        fills = load_fills(conn, op)
        cands = load_candidates(conn, op)
    finally:
        conn.close()

    primary = primary_gate(fills)
    rel = reliability_gate(cands)
    iso = isotonic_gate(fills)
    partial = partial_gate([f for f in fills if f["fill_ratio"] > 0.0]) if fills else {"n": 0, "frac_below": None, "suspend": False}

    text = render(primary, rel, iso, partial, len(fills), args.dry_run)
    print(text)

    if args.dry_run:
        return
    if primary["n"] < FREEZE_N:
        return
    if not args.write:
        print("(n>=FREEZE_N but --write not set; verdict NOT written. "
              "Re-run with --write to emit bot/FLB_P6_VERDICT.txt.)")
        return
    import os
    out = os.path.join(os.path.dirname(__file__), "FLB_P6_VERDICT.txt")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
