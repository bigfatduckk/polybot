"""Task W2 M2 — Event study: do stale-price windows exist after a METAR running-max
boundary crossing, at exploitable frequency?

Pre-registered in 'Weather Angles W1-W4 - Verdicts + Test-Build Plan 2026-07-25.md'
(Part D, Task M2). The near-deterministic case: once the running station daily-max
crosses a "≥X or higher" bucket boundary, YES is decided (max is monotone within the
day). The edge = buy YES cheaply AFTER the fact is known but BEFORE the market price
catches up.

Per-event:
  τ = first METAR obs (incl SPECI) where running local-day max >= bucket_lo (X),
      while the market is open (before the local-day close).
  Sample YES price at τ+5/10/15/30/60 min (CLOB /prices-history fidelity=1).
  exploitable iff price@(τ+10min) <= 0.92  (=> mid+2c <= 0.94 => >=4c profit after
     ~2% fee; the plan's "buyable price proxy (mid+2c) <= 0.94" threshold).
  window_duration = time from τ until price >= 0.97 (convergence).

KILL (pre-registered): exploitable events < 8/month pooled OR median window < 10 min.
Floor: >= 120 boundary-crossing events with minute-price coverage. Fallback (passive
live logger) ONLY if minute-data unavailable historically — the 0.3 fidelity probe
showed 10/10 markets HAVE minute data, so no logger is built.

Depth proxy limitation: /prices-history gives {t,p} only (no trade sizes). The plan's
"depth >= $50" gate cannot be enforced from this data; we use price-point density in
[τ, τ+10min] as a weak liquidity proxy and note the limitation. This makes any
SURVIVE result optimistic on depth (a real survivor needs a live depth check) — the
correct bias for a kill-biased gate.
"""
import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bot"))
import httpx  # noqa: E402
from config import CLOB_BASE, tls_verify  # noqa: E402

DB_PATH = ROOT / "research" / "weather2" / "data" / "weather_research.db"
UA = {"User-Agent": "MarcusVaultBot/1.0"}
EXPLOITABLE_PRICE = 0.92   # price@τ+10 <= this => gap >= 4c after 2c spread + 2% fee
CONVERGE_PRICE = 0.97      # window ends when price >= this
KILL_EVENTS_PER_MONTH = 8  # < this => KILL
KILL_WINDOW_MIN = 10       # median window < this => KILL
FLOOR_EVENTS = 120
PER_MARKET_SLEEP = 0.18


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _client():
    return httpx.Client(timeout=30, verify=tls_verify(), headers=UA)


def fetch_history_minute(token, start_ts, end_ts):
    if not token:
        return []
    try:
        with _client() as c:
            r = c.get(f"{CLOB_BASE}/prices-history",
                      params={"market": token, "fidelity": 1,
                              "startTs": start_ts, "endTs": end_ts})
        if r.status_code != 200:
            return []
        h = (r.json() or {}).get("history") or []
        return [(int(x["t"]), float(x["p"])) for x in h if "t" in x and "p" in x]
    except Exception:
        return []


def find_crossing(conn, icao, date_local, bucket_lo):
    """First METAR obs where running local-day max >= bucket_lo. Returns (ts_local_str, running_max) or None."""
    rows = conn.execute(
        "SELECT valid_local, tmpc FROM metar_obs WHERE icao=? AND valid_local LIKE ? "
        "AND tmpc IS NOT NULL ORDER BY valid_local",
        (icao, date_local + "%")).fetchall()
    rmax = None
    for r in rows:
        t = float(r["tmpc"])
        rmax = t if rmax is None else max(rmax, t)
        if rmax >= bucket_lo:
            return r["valid_local"], rmax
    return None


def sample_prices(hist, tau_ts, offsets_min=(5, 10, 15, 30, 60)):
    """Return {offset_min: price} — last price at or before tau+offset."""
    out = {}
    for off in offsets_min:
        target = tau_ts + off * 60
        before = [(t, p) for t, p in hist if t <= target]
        out[off] = before[-1][1] if before else None
    return out


def window_duration(hist, tau_ts):
    """Minutes from tau until price >= CONVERGE_PRICE (or last available if never)."""
    after = [(t, p) for t, p in hist if t >= tau_ts and p >= CONVERGE_PRICE]
    if after:
        return (after[0][0] - tau_ts) / 60.0
    # never converged within the fetched window
    last = [(t, p) for t, p in hist if t >= tau_ts]
    if last:
        return (last[-1][0] - tau_ts) / 60.0  # lower bound (censored)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--max-markets", type=int, default=None)
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()
    if args.selfcheck:
        hist = [(0, 0.50), (300, 0.60), (600, 0.91), (900, 0.95), (1800, 0.98)]
        s = sample_prices(hist, 0)
        assert s[5] == 0.60 and s[10] == 0.91 and s[30] == 0.98
        assert abs(window_duration(hist, 0) - 30.0) < 0.1  # hits 0.97 at t=1800 (0.98>=0.97) -> 30min
        print("[self-check] sample_prices + window_duration OK")
        return
    if not args.run:
        ap.print_help(); return

    conn = _connect()
    # '>=X or higher' markets that resolved YES (the max actually crossed X)
    markets = conn.execute("""
        SELECT market_id, icao, event_date_local, tz_name, clob_token_id_yes,
               bucket_lo, bucket_hi, resolved_yes
        FROM markets_map
        WHERE parse_status='ok' AND icao!='VHHH' AND clob_token_id_yes!=''
          AND event_date_local IS NOT NULL AND resolved_yes=1
          AND bucket_lo IS NOT NULL AND bucket_hi IS NULL
        ORDER BY event_date_local
    """).fetchall()
    print(f"[load] {len(markets)} '>=X or higher' resolved-YES markets")
    if args.max_markets:
        markets = markets[:args.max_markets]

    events = []           # all crossing events with minute coverage
    n_no_crossing = 0
    n_no_minute = 0
    exploitable = []      # subset passing the exploitability gate
    for i, m in enumerate(markets):
        cr = find_crossing(conn, m["icao"], m["event_date_local"], m["bucket_lo"])
        if cr is None:
            n_no_crossing += 1
            continue
        valid_local, rmax = cr
        try:
            tz = ZoneInfo(m["tz_name"])
            dt = datetime.fromisoformat(valid_local).replace(tzinfo=tz)
        except Exception:
            continue
        tau_ts = int(dt.timestamp())
        hist = fetch_history_minute(m["clob_token_id_yes"], tau_ts - 30 * 60, tau_ts + 65 * 60)
        if len(hist) < 5:
            n_no_minute += 1
            continue
        s = sample_prices(hist, tau_ts)
        p10 = s[10]
        w = window_duration(hist, tau_ts)
        # liquidity proxy: price points in [tau, tau+10min]
        density = len([t for t, _ in hist if tau_ts <= t <= tau_ts + 600])
        events.append({
            "market_id": m["market_id"], "icao": m["icao"], "date": m["event_date_local"],
            "bucket_lo": m["bucket_lo"], "tau": valid_local, "rmax_at_tau": rmax,
            "p5": s[5], "p10": p10, "p15": s[15], "p30": s[30], "p60": s[60],
            "window_min": w, "density_10min": density,
        })
        if p10 is not None and p10 <= EXPLOITABLE_PRICE and w is not None:
            exploitable.append(events[-1])
        if (i + 1) % 200 == 0:
            print(f"  {i+1}/{len(markets)} scanned, {len(events)} events, {len(exploitable)} exploitable")
        time.sleep(PER_MARKET_SLEEP)

    print(f"\n[scan] done: {len(markets)} markets -> {len(events)} crossing events w/ minute coverage, "
          f"{len(exploitable)} exploitable (no_crossing={n_no_crossing}, no_minute={n_no_minute})")

    # OOS window days (2025-10-01 -> 2026-06-30 ~ 273 days); events span the resolved markets' dates
    if events:
        dates = sorted(set(e["date"] for e in events))
        span_days = (datetime.fromisoformat(dates[-1]) - datetime.fromisoformat(dates[0])).days + 1
        span_months = max(span_days / 30.0, 1.0)
    else:
        span_months = 1.0
    expl_per_month = len(exploitable) / span_months
    windows = [e["window_min"] for e in events if e["window_min"] is not None]
    med_window = median(windows) if windows else None

    print(f"\n=== W2 M2 EVENT STUDY ===")
    print(f"events w/ minute coverage: {len(events)} (floor {FLOOR_EVENTS})")
    print(f"exploitable events: {len(exploitable)} over ~{span_months:.1f} months => {expl_per_month:.1f}/month "
          f"(kill bar < {KILL_EVENTS_PER_MONTH}/month)")
    if med_window is not None:
        print(f"median stale-window: {med_window:.1f} min (kill bar < {KILL_WINDOW_MIN} min)")
    print(f"exploitable by station (top):")
    byst = defaultdict(int)
    for e in exploitable:
        byst[e["icao"]] += 1
    for icao, c in sorted(byst.items(), key=lambda x: -x[1])[:10]:
        print(f"  {icao}: {c}")

    # verdict per pre-reg
    floor_ok = len(events) >= FLOOR_EVENTS
    expl_kill = expl_per_month < KILL_EVENTS_PER_MONTH
    window_kill = med_window is not None and med_window < KILL_WINDOW_MIN
    if not floor_ok:
        verdict = (f"INSUFFICIENT-DATA ({len(events)} events < {FLOOR_EVENTS} floor) -> W2 KILLED on cost; "
                   f"passive-logger fallback NOT built (fidelity probe showed minute data exists; the shortage "
                   f"is coverage of old markets, not a live-data need)")
        survive = False
    elif expl_kill or window_kill:
        reasons = []
        if expl_kill: reasons.append(f"{expl_per_month:.1f} exploitable/month < {KILL_EVENTS_PER_MONTH}")
        if window_kill: reasons.append(f"median window {med_window:.1f}min < {KILL_WINDOW_MIN}min")
        verdict = f"KILL W2 ({'; '.join(reasons)})"
        survive = False
    else:
        verdict = (f"W2 SURVIVES M2 ({expl_per_month:.1f} exploitable/month, median window {med_window:.1f}min) "
                   f"— but depth proxy unenforced (price-point density only); a live depth check is required "
                   f"before any build. This is a SURVIVE-conditional-on-depth, not a ship.")
        survive = True

    print(f"\n>>> VERDICT: {verdict}")

    # write verdict file
    out = ROOT / "research" / "weather2" / "W2_VERDICT.txt"
    out.write_text(
        f"W2 M2 EVENT STUDY VERDICT — 2026-07-26\n{'='*40}\n"
        f"Pre-registered kill: exploitable events < {KILL_EVENTS_PER_MONTH}/month OR median window < {KILL_WINDOW_MIN}min. "
        f"Floor: >= {FLOOR_EVENTS} events w/ minute coverage.\n\n"
        f"markets scanned: {len(markets)} ('>=X or higher' resolved-YES)\n"
        f"crossing events w/ minute coverage: {len(events)} (no_crossing={n_no_crossing}, no_minute={n_no_minute})\n"
        f"exploitable events: {len(exploitable)} over ~{span_months:.1f} months => {expl_per_month:.1f}/month\n"
        f"median stale-window: {med_window:.1f} min\n"
        f"exploitable by station: {dict(sorted(byst.items(), key=lambda x:-x[1])[:10])}\n\n"
        f"LIMITATION: depth proxy = price-point density only (/prices-history has no trade sizes). "
        f"The pre-reg's 'depth >= $50' gate is NOT enforced. Any SURVIVE is optimistic on depth.\n\n"
        f"VERDICT: {verdict}\n\n"
        f"STOP-GATE: human review. Live arm STAYS HALTED.\n")
    print(f"[done] wrote {out}")
    # also dump events for audit
    import json as _j
    (ROOT / "research" / "weather2" / "data" / "w2_events.json").write_text(
        _j.dumps({"events": events[:500], "exploitable": exploitable[:500]}, indent=2, default=str))
    conn.close()


if __name__ == "__main__":
    main()
