"""Fit weather probability recalibration (Platt) on resolved signals.

Reads the RAW blend from candidates.snapshot_json["blended"][bucket_key]
(not p_model) so refitting stays consistent once calibrate_p is wired into
the engine. Writes data/weather_calib.json. Prints before/after reliability
by decile on the fit set + a holdout overfit check.

IS-phase tool. Refit at the freeze date before OOS so the frozen params
reflect the full IS window, not just the data to date.

Run: .venv/bin/python bot/build_weather_calib.py
"""
import json
import sqlite3
from datetime import datetime, timezone

from config import BOT_DIR, DB_PATH
from calib import fit_platt, calibrate_p

CALIB_PATH = BOT_DIR / "data" / "weather_calib.json"

DECILES = [(0.0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5),
           (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]


def _raw_signals(conn):
    rows = conn.execute(
        """SELECT c.bucket_key, c.snapshot_json, st.resolved_yes AS outcome
           FROM candidates c
           JOIN settlements st ON st.market_id = c.market_id
           WHERE st.resolved_yes IS NOT NULL"""
    ).fetchall()
    out = []
    for r in rows:
        try:
            raw = json.loads(r["snapshot_json"]) if r["snapshot_json"] else {}
            blended = raw.get("blended") or {}
            p = blended.get(r["bucket_key"])
        except Exception:
            p = None
        if p is None:
            continue
        out.append((float(p), 1 if r["outcome"] else 0))
    return out


def _reliability(pairs):
    table = []
    maxdev = 0.0
    for lo, hi in DECILES:
        grp = [(p, o) for p, o in pairs if lo <= p < hi]
        if len(grp) < 5:
            continue
        mp = sum(p for p, _ in grp) / len(grp)
        fq = sum(o for _, o in grp) / len(grp)
        dev = abs(mp - fq)
        maxdev = max(maxdev, dev)
        table.append((lo, hi, len(grp), mp, fq, dev))
    return table, maxdev


def _print_rel(label, table, maxdev):
    print(label + f"  (max deviation = {maxdev * 100:.1f}pp)")
    for lo, hi, n, mp, fq, dev in table:
        print(f"  [{lo:.1f},{hi:.1f}) n={n:4d} mean_p={mp:.3f} freq={fq:.3f} dev={dev * 100:.1f}pp")


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    pairs = _raw_signals(conn)
    conn.close()
    if len(pairs) < 50:
        print(f"insufficient data: {len(pairs)} resolved signals (need >=50)")
        return
    print(f"resolved signals: {len(pairs)}")

    cut = int(len(pairs) * 0.8)
    fit_pairs = pairs[:cut]
    hold_pairs = pairs[cut:]
    params = fit_platt([p for p, _ in fit_pairs], [o for _, o in fit_pairs])
    if params is None:
        print("fit failed (n<10)")
        return

    calib_all = [(calibrate_p(p, params), o) for p, o in pairs]
    calib_hold = [(calibrate_p(p, params), o) for p, o in hold_pairs]

    print(f"fit: a={params['a']:.4f} b={params['b']:.4f} "
          f"(on {len(fit_pairs)} signals, Platt logit-space)\n")
    rt, rmd = _reliability(pairs)
    _print_rel("BEFORE (raw blend):", rt, rmd)
    print()
    ct, cmd = _reliability(calib_all)
    _print_rel("AFTER  (calibrated):", ct, cmd)
    print()
    _, hmd = _reliability(calib_hold)
    print(f"holdout ({len(hold_pairs)} signals) calibrated max deviation = {hmd * 100:.1f}pp")

    CALIB_PATH.parent.mkdir(parents=True, exist_ok=True)
    params["fit_ts"] = datetime.now(timezone.utc).isoformat()
    CALIB_PATH.write_text(json.dumps(params, indent=2))
    print(f"\nwrote {CALIB_PATH}")


if __name__ == "__main__":
    main()
