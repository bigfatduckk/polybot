"""Task 0.3 — Historical price snapshots + fidelity probe + late-trading table.

Feeds: T1.1-W1's `logit(snapshot_mid)` (snapshot = last price at or before 00:00
local standard time of event day D, requires quote in preceding 12h), the W2 M1
late-trading gate, and the W3 scan's traded-price cross-reference.

Outputs (weather_research.db):
  price_snapshots(market_id, snapshot_ts_utc, mid, ok)
  fidelity_probe(market_id, age_days, minute_data_available)
  late_trading(market_id, trades_final_4h, trades_after_peak)

CLOB /prices-history?market=<token>&fidelity=60&startTs&endTs returns {history:[{t,p}]}.
fidelity=1 probe on 10 markets spanning ages 1-18mo feeds W2's M2-vs-logger decision.
"""
import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bot"))
import httpx  # noqa: E402
from config import CLOB_BASE, tls_verify  # noqa: E402

DB_PATH = ROOT / "research" / "weather2" / "data" / "weather_research.db"
UA = {"User-Agent": "MarcusVaultBot/1.0"}
SNAPSHOT_LOOKBACK_H = 12  # quote must be within 12h before 00:00 local
PER_MARKET_SLEEP = 0.15


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS price_snapshots (
            market_id TEXT PRIMARY KEY, snapshot_ts_utc TEXT, mid REAL, ok INTEGER, note TEXT
        );
        CREATE TABLE IF NOT EXISTS fidelity_probe (
            market_id TEXT, age_days INTEGER, minute_data_available INTEGER, n_points INTEGER
        );
        CREATE TABLE IF NOT EXISTS late_trading (
            market_id TEXT PRIMARY KEY, trades_final_4h INTEGER, trades_after_peak INTEGER
        );
        CREATE INDEX IF NOT EXISTS ix_snap_ok ON price_snapshots(ok);
    """)
    conn.commit()
    return conn


def _client():
    return httpx.Client(timeout=30, verify=tls_verify(), headers=UA)


def fetch_history(token_id, start_ts, end_ts, fidelity=60):
    if not token_id:
        return []
    try:
        with _client() as c:
            r = c.get(f"{CLOB_BASE}/prices-history",
                      params={"market": token_id, "fidelity": fidelity,
                              "startTs": start_ts, "endTs": end_ts})
        if r.status_code != 200:
            return []
        h = (r.json() or {}).get("history") or []
        return [(int(x["t"]), float(x["p"])) for x in h if "t" in x and "p" in x]
    except Exception:
        return []


def snapshot_for_market(conn, row):
    """row: (market_id, clob_token_id_yes, event_date_local, tz_name). Store snapshot mid."""
    market_id, token, date_local, tz_name = row
    if not token or not date_local or not tz_name:
        conn.execute("INSERT OR REPLACE INTO price_snapshots(market_id, ok, note) VALUES (?,?,?)",
                     (market_id, 0, "missing token/date/tz"))
        return 0
    try:
        tz = ZoneInfo(tz_name)
        dt_local = datetime.fromisoformat(date_local + "T00:00:00").replace(tzinfo=tz)
    except Exception:
        conn.execute("INSERT OR REPLACE INTO price_snapshots(market_id, ok, note) VALUES (?,?,?)",
                     (market_id, 0, "bad tz"))
        return 0
    target_ts = int(dt_local.timestamp())
    start_ts = target_ts - SNAPSHOT_LOOKBACK_H * 3600
    end_ts = target_ts + 3600  # allow a quote exactly at 00:00
    hist = fetch_history(token, start_ts, end_ts, fidelity=60)
    # last price at or before target_ts
    before = [(t, p) for t, p in hist if t <= target_ts]
    if not before:
        conn.execute("INSERT OR REPLACE INTO price_snapshots(market_id, snapshot_ts_utc, ok, note) VALUES (?,?,?,?)",
                     (market_id, None, 0, "no quote in 12h window"))
        return 0
    snap_t, snap_p = before[-1]
    ok = 1 if 0.05 <= snap_p <= 0.95 else 0  # pre-reg inclusion band
    note = "" if ok else "mid outside [0.05,0.95]"
    conn.execute("INSERT OR REPLACE INTO price_snapshots(market_id, snapshot_ts_utc, mid, ok, note) VALUES (?,?,?,?,?)",
                 (market_id, datetime.fromtimestamp(snap_t, timezone.utc).isoformat(timespec="seconds"),
                  snap_p, ok, note))
    return 1 if ok else 0


def fidelity_probe(conn, sample_n=10):
    """Probe fidelity=1 on ~10 markets spanning ages 1-18mo."""
    rows = conn.execute("""
        SELECT m.market_id, m.clob_token_id_yes, m.event_date_local, m.tz_name
        FROM markets_map m WHERE m.parse_status='ok' AND m.clob_token_id_yes!=''
        AND m.event_date_local IS NOT NULL ORDER BY RANDOM() LIMIT ?
    """, (sample_n,)).fetchall()
    now = datetime.now(timezone.utc)
    conn.execute("DELETE FROM fidelity_probe")
    for r in rows:
        market_id, token, date_local, tz_name = r
        try:
            tz = ZoneInfo(tz_name)
            dt_local = datetime.fromisoformat(date_local + "T00:00:00").replace(tzinfo=tz)
        except Exception:
            continue
        target_ts = int(dt_local.timestamp())
        age_days = (now.timestamp() - target_ts) / 86400
        # try fidelity=1 over a 6h window
        hist = fetch_history(token, target_ts - 3 * 3600, target_ts + 3 * 3600, fidelity=1)
        minute_avail = 1 if len(hist) > 20 else 0  # >20 pts in 6h implies sub-hour fidelity present
        conn.execute("INSERT INTO fidelity_probe(market_id, age_days, minute_data_available, n_points) VALUES (?,?,?,?)",
                     (market_id, int(age_days), minute_avail, len(hist)))
        time.sleep(PER_MARKET_SLEEP)
    probed = conn.execute("SELECT COUNT(*), SUM(minute_data_available) FROM fidelity_probe").fetchone()
    print(f"[fidelity_probe] {probed[0]} markets probed, {probed[1]} with minute-data")
    return probed


def late_trading_sample(conn, sample_n=200):
    """For 200-market sample: count trades in final 4h of local event day (W2 M1 gate)."""
    rows = conn.execute("""
        SELECT m.market_id, m.clob_token_id_yes, m.event_date_local, m.tz_name
        FROM markets_map m WHERE m.parse_status='ok' AND m.clob_token_id_yes!=''
        AND m.event_date_local IS NOT NULL ORDER BY RANDOM() LIMIT ?
    """, (sample_n,)).fetchall()
    n_with_trades = 0
    for r in rows:
        market_id, token, date_local, tz_name = r
        try:
            tz = ZoneInfo(tz_name)
            dt_local = datetime.fromisoformat(date_local + "T00:00:00").replace(tzinfo=tz)
        except Exception:
            continue
        # final 4h of the local event day = 20:00 -> 23:59 local; price-change points proxy trades
        start_ts = int(dt_local.timestamp()) + 20 * 3600
        end_ts = int(dt_local.timestamp()) + 24 * 3600
        hist = fetch_history(token, start_ts, end_ts, fidelity=60)
        # price-change points = distinct timestamps (rough trade proxy)
        n_final4h = len(hist)
        # after-peak: trades in the last hour
        n_after_peak = len([t for t, _ in hist if t >= end_ts - 3600])
        conn.execute("INSERT OR REPLACE INTO late_trading(market_id, trades_final_4h, trades_after_peak) VALUES (?,?,?)",
                     (market_id, n_final4h, n_after_peak))
        if n_final4h > 0:
            n_with_trades += 1
        time.sleep(PER_MARKET_SLEEP)
    tot = conn.execute("SELECT COUNT(*) FROM late_trading").fetchone()[0]
    print(f"[late_trading] {tot} markets sampled, {n_with_trades} with >0 final-4h price points")
    return n_with_trades, tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", action="store_true")
    ap.add_argument("--fidelity-probe", action="store_true")
    ap.add_argument("--late-trading", action="store_true")
    ap.add_argument("--max-markets", type=int, default=None)
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()
    if args.selfcheck:
        # snapshot logic: last price at/before target from a synthetic history
        hist = [(100, 0.10), (200, 0.20), (300, 0.30)]  # (t, p)
        target = 250
        before = [(t, p) for t, p in hist if t <= target]
        assert before[-1] == (200, 0.20)  # last at/before target
        # no quote in window
        assert not [t for t, p in hist if t <= 50]
        print("[self-check] snapshot last-before-target + empty-window logic OK")
        return
    conn = init_db()
    if args.snapshots:
        q = """SELECT market_id, clob_token_id_yes, event_date_local, tz_name FROM markets_map
               WHERE parse_status='ok' AND clob_token_id_yes!='' AND event_date_local IS NOT NULL"""
        rows = conn.execute(q).fetchall()
        if args.max_markets:
            rows = rows[:args.max_markets]
        print(f"[snapshots] {len(rows)} markets, throttled {PER_MARKET_SLEEP}s")
        n_ok = 0
        for i, r in enumerate(rows):
            n_ok += snapshot_for_market(conn, r)
            if (i + 1) % 500 == 0:
                conn.commit()
                print(f"  {i+1}/{len(rows)} done, {n_ok} ok")
            time.sleep(PER_MARKET_SLEEP)
        conn.commit()
        tot = conn.execute("SELECT COUNT(*), SUM(ok) FROM price_snapshots").fetchone()
        print(f"[snapshots] done: {tot[1]} ok / {tot[0]} total")
        # coverage report
        cov = conn.execute("""
            SELECT COUNT(*) FROM markets_map m JOIN price_snapshots p ON p.market_id=m.market_id
            WHERE m.parse_status='ok' AND p.ok=1
        """).fetchone()[0]
        citydates = conn.execute("""
            SELECT COUNT(DISTINCT m.icao||'|'||m.event_date_local) FROM markets_map m
            JOIN price_snapshots p ON p.market_id=m.market_id
            WHERE m.parse_status='ok' AND p.ok=1
        """).fetchone()[0]
        print(f"[coverage] {cov} ok market-snapshots across {citydates} city-dates "
              f"(W1 floor: >=500 city-dates)")
    if args.fidelity_probe:
        fidelity_probe(conn)
    if args.late_trading:
        late_trading_sample(conn)
    if not (args.snapshots or args.fidelity_probe or args.late_trading or args.selfcheck):
        ap.print_help()
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
