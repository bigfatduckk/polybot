"""Task 0.1 — Polymarket daily-temperature market metadata mapper.

Enumerates all "Highest temperature" markets (closed + active) via gamma tag_id,
parses each description for station/ICAO/unit/buckets/tz, stores venue resolution.
Output: markets_map table in research/weather2/data/weather_research.db.

NON-NEGOTIABLE (plan): resolution/station/unit parsed per-market from the
description + resolutionSource URL; never assumed/defaulted. Unparsed rows are
excluded (parse_status='manual'), not guessed. Grading oracle = venue resolution
+ validated METAR replica only; this script never touches ERA5 or the old
settlements table.

Self-check: --selfcheck runs a synthetic parse test. Default run prints 20 random
parsed rows + coverage stats for manual review (STOP-gate before Task 0.2).
"""
import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # polybot root
sys.path.insert(0, str(ROOT / "bot"))
import httpx  # noqa: E402
import markets  # noqa: E402
from config import CITIES, GAMMA_BASE, tls_verify  # noqa: E402

DB_PATH = ROOT / "research" / "weather2" / "data" / "weather_research.db"
TAG_ID = 104596  # "Highest temperature" tag (probed 2026-07-25)
UA = {"User-Agent": "MarcusVaultBot/1.0"}
MONTHS = ["january", "february", "march", "april", "may", "june",
          "july", "august", "september", "october", "november", "december"]
MONTH_IDX = {m: i + 1 for i, m in enumerate(MONTHS)}

TITLE_RE = re.compile(r"highest temperature in (.+?) on (.+)", re.I)
SLUG_DATE_RE = re.compile(r"-on-([a-z]+)-(\d+)-(\d+)$")
BUCKET_BELOW = re.compile(r"(-?\d+)°([CF])\s+or\s+below", re.I)
BUCKET_ABOVE = re.compile(r"(-?\d+)°([CF])\s+or\s+higher", re.I)
BUCKET_EXACT = re.compile(r"(-?\d+)°([CF])(?!\s+or)", re.I)
STATION_RE = re.compile(r"recorded at the (.+?)(?:\.|,|$)", re.I)
ICAO_RE = re.compile(r"wunderground\.com/history/daily/[^/]+/[^/]+/([A-Z]{4})", re.I)
DEG_UNIT_RE = re.compile(r"in degrees (celsius|fahrenheit)", re.I)

# City -> tz. config.CITIES (8 traded) + common US cities Polymarket covers.
# Unknown cities -> parse_status='manual' (tz left NULL; filled at STOP-gate review).
CITY_TZ = {c: v["timezone"] for c, v in CITIES.items()}
CITY_TZ.update({
    "NYC": "America/New_York", "New York": "America/New_York",
    "Dallas": "America/Chicago", "Houston": "America/Chicago",
    "Chicago": "America/Chicago", "Austin": "America/Chicago",
    "Atlanta": "America/New_York", "Miami": "America/New_York",
    "Boston": "America/New_York", "Philadelphia": "America/New_York",
    "Washington DC": "America/New_York", "DC": "America/New_York",
    "Denver": "America/Denver", "Phoenix": "America/Phoenix",
    "Los Angeles": "America/Los_Angeles", "LA": "America/Los_Angeles",
    "Seattle": "America/Los_Angeles", "San Francisco": "America/Los_Angeles",
    "Las Vegas": "America/Los_Angeles", "Salt Lake City": "America/Denver",
    "Minneapolis": "America/Chicago", "Detroit": "America/Detroit",
})


def _client():
    return httpx.Client(timeout=30, headers=UA, verify=tls_verify())


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS weather_events_raw (
            event_id TEXT PRIMARY KEY, slug TEXT, title TEXT,
            closed INTEGER, archived INTEGER, fetched_ts TEXT, json TEXT
        );
        CREATE TABLE IF NOT EXISTS markets_map (
            market_id TEXT PRIMARY KEY, condition_id TEXT, clob_token_id_yes TEXT,
            event_id TEXT, question TEXT, description TEXT, station_name TEXT,
            icao TEXT, unit TEXT, bucket_lo INTEGER, bucket_hi INTEGER,
            tz_name TEXT, event_date_local TEXT, resolution_source_url TEXT,
            resolved_yes INTEGER, end_date TEXT, closed_time TEXT, parse_status TEXT,
            native_unit TEXT, raw_json TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_mm_icao_date ON markets_map(icao, event_date_local);
        CREATE INDEX IF NOT EXISTS ix_mm_status ON markets_map(parse_status);
    """)
    conn.commit()
    return conn


def parse_bucket(question):
    m = BUCKET_BELOW.search(question)
    if m:
        v = int(m.group(1)); return (None, v + 1, m.group(2).upper())
    m = BUCKET_ABOVE.search(question)
    if m:
        v = int(m.group(1)); return (v, None, m.group(2).upper())
    m = BUCKET_EXACT.search(question)
    if m:
        v = int(m.group(1)); return (v, v + 1, m.group(2).upper())
    return None


def parse_event(ev, conn):
    title = ev.get("title") or ""
    tm = TITLE_RE.search(title)
    if not tm:
        return 0
    city = tm.group(1).strip()
    slug = ev.get("slug") or ""
    sd = SLUG_DATE_RE.search(slug)
    event_date_local = None
    if sd and sd.group(1) in MONTH_IDX:
        event_date_local = f"{sd.group(3)}-{MONTH_IDX[sd.group(1)]:02d}-{int(sd.group(2)):02d}"
    tz_name = CITY_TZ.get(city)
    n = 0
    for mkt in (ev.get("markets") or []):
        q = mkt.get("question") or ""
        desc = mkt.get("description") or ""
        b = parse_bucket(q)
        icao_m = ICAO_RE.search(desc)
        station_m = STATION_RE.search(desc)
        native_m = DEG_UNIT_RE.search(desc)
        rsrc = mkt.get("resolutionSource") or ""
        # resolution from outcomePrices (already in event response for closed markets)
        prices = mkt.get("outcomePrices")
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except (TypeError, ValueError):
                prices = []
        prices = prices or []
        try:
            prices = [float(p) for p in prices]
        except (TypeError, ValueError):
            prices = []
        resolved_yes = None
        if len(prices) >= 2 and prices[0] in (0.0, 1.0) and prices[1] in (0.0, 1.0):
            resolved_yes = 1 if prices[0] >= prices[1] else 0
        tokens = mkt.get("clobTokenIds")
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except (TypeError, ValueError):
                tokens = []
        yes_tok = tokens[0] if tokens else ""
        status = "ok"
        if b is None:
            status = "excluded"  # couldn't parse bucket -> not a usable row
        elif not icao_m:
            status = "manual"  # no ICAO -> can't grade, flag for review
        elif not tz_name:
            status = "manual"  # city tz unknown -> flag for review
        # bucket-bad rows excluded entirely; bucket-ok but missing icao/tz -> manual
        if status == "excluded":
            continue
        lo, hi, unit = b if b else (None, None, None)
        conn.execute(
            """INSERT OR REPLACE INTO markets_map
            (market_id, condition_id, clob_token_id_yes, event_id, question, description,
             station_name, icao, unit, bucket_lo, bucket_hi, tz_name, event_date_local,
             resolution_source_url, resolved_yes, end_date, closed_time, parse_status,
             native_unit, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(mkt.get("id", "")), str(mkt.get("conditionId", "")), yes_tok,
             str(ev.get("id", "")), q, desc,
             (station_m.group(1).strip() if station_m else None),
             (icao_m.group(1) if icao_m else None), unit, lo, hi, tz_name,
             event_date_local, rsrc, resolved_yes, str(mkt.get("endDate", "")),
             str(mkt.get("closedTime", "")), status,
             (native_m.group(1).lower() if native_m else None),
             json.dumps(mkt)[:4000]),
        )
        n += 1
    conn.execute(
        """INSERT OR REPLACE INTO weather_events_raw
        (event_id, slug, title, closed, archived, fetched_ts, json) VALUES (?,?,?,?,?,?,?)""",
        (str(ev.get("id", "")), slug, title, int(bool(ev.get("closed"))),
         int(bool(ev.get("archived"))),
         datetime.now(timezone.utc).isoformat(timespec="seconds"),
         json.dumps(ev)[:20000]),
    )
    return n


def enumerate_events(max_events=None, active=False):
    """Paginate gamma /events by tag_id. active=False -> closed+archived (resolved)."""
    params_base = {"tag_id": TAG_ID, "limit": 100}
    if active:
        params_base.update({"closed": "false", "active": "true"})
    else:
        params_base.update({"closed": "true", "archived": "true"})
    out = []
    offset = 0
    with _client() as c:
        while True:
            params = dict(params_base, offset=offset)
            r = c.get(f"{GAMMA_BASE}/events", params=params)
            if r.status_code != 200:
                print(f"[warn] gamma offset={offset} status={r.status_code}", file=sys.stderr)
                break
            page = r.json() or []
            if not isinstance(page, list):
                page = page.get("data") or []
            if not page:
                break
            out.extend(page)
            print(f"[fetch] offset={offset} +{len(page)} (total {len(out)})")
            offset += len(page)
            time.sleep(0.1)
            if max_events and len(out) >= max_events:
                out = out[:max_events]
                break
            if len(page) < 100:
                break
    return out


def coverage_report(conn, sample_n=20):
    total = conn.execute("SELECT COUNT(*) FROM markets_map").fetchone()[0]
    by_status = conn.execute(
        "SELECT parse_status, COUNT(*) FROM markets_map GROUP BY parse_status"
    ).fetchall()
    resolved = conn.execute(
        "SELECT COUNT(*) FROM markets_map WHERE resolved_yes IS NOT NULL"
    ).fetchone()[0]
    n_events = conn.execute("SELECT COUNT(*) FROM weather_events_raw").fetchone()[0]
    n_icao = conn.execute(
        "SELECT COUNT(DISTINCT icao) FROM markets_map WHERE icao IS NOT NULL"
    ).fetchone()[0]
    n_dates = conn.execute(
        "SELECT COUNT(DISTINCT event_date_local) FROM markets_map "
        "WHERE event_date_local IS NOT NULL"
    ).fetchone()[0]
    cities = conn.execute(
        "SELECT icao, COUNT(*) n, COUNT(DISTINCT event_date_local) dates FROM markets_map "
        "WHERE icao IS NOT NULL GROUP BY icao ORDER BY n DESC LIMIT 25"
    ).fetchall()
    print("\n=== COVERAGE ===")
    print(f"events fetched: {n_events} | market rows: {total} | resolved: {resolved}")
    print(f"distinct ICAOs: {n_icao} | distinct dates: {n_dates}")
    print("by parse_status:", dict(by_status))
    print("\ntop stations (icao: rows / dates):")
    for r in cities:
        print(f"  {r[0]}: {r[1]} rows / {r[2]} dates")
    print(f"\n=== {sample_n} RANDOM PARSED ROWS (STOP-gate: manual review) ===")
    rows = conn.execute(
        """SELECT icao, station_name, unit, bucket_lo, bucket_hi, tz_name,
           event_date_local, resolved_yes, parse_status, substr(question,1,70)
           FROM markets_map WHERE parse_status='ok' ORDER BY RANDOM() LIMIT ?""",
        (sample_n,),
    ).fetchall()
    print(f"{'icao':<6} {'unit':<4} {'lo':>4} {'hi':>4} {'tz':<26} {'date':<11} "
          f"{'res':>3} {'q':<70}")
    for r in rows:
        icao, stn, unit, lo, hi, tz, dt, res, st, q = r
        print(f"{icao or '?':<6} {unit or '?':<4} {str(lo):>4} {str(hi):>4} "
              f"{tz or '(manual)':<26} {dt or '?':<11} {str(res):>3} {q}")
    # manual rows needing tz/icao
    manual = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT substr(question,1,40)) FROM markets_map "
        "WHERE parse_status='manual'"
    ).fetchone()
    print(f"\n[manual review needed] {manual[0]} rows flagged parse_status='manual' "
          f"(missing tz or ICAO). Inspect: SELECT icao,station_name,question,tz_name "
          f"FROM markets_map WHERE parse_status='manual' LIMIT 20;")


def selfcheck():
    # parse_bucket correctness
    assert parse_bucket("Will the highest temperature in X be 11°C or below on Y?") == (None, 12, "C")
    assert parse_bucket("Will the highest temperature in X be 21°C or higher on Y?") == (21, None, "C")
    assert parse_bucket("Will the highest temperature in X be 12°C on Y?") == (12, 13, "C")
    assert parse_bucket("Will the highest temperature in X be 90°F or higher on Y?") == (90, None, "F")
    # bucket partition sums to full range (no gaps/overlaps) on a synthetic 11-bucket set
    qs = ["11°C or below", "12°C", "13°C", "14°C", "15°C", "16°C", "17°C",
          "18°C", "19°C", "20°C", "21°C or higher"]
    bs = [parse_bucket(f"Will the highest temperature in X be {q} on Y?") for q in qs]
    assert bs[0][:2] == (None, 12) and bs[-1][:2] == (21, None)
    for i in range(1, len(bs) - 1):
        assert bs[i][0] == bs[i - 1][1] or (i == 1 and bs[0][1] == 12)  # contiguous
    # exact must NOT match "or below"/"or higher"
    assert parse_bucket("be 11°C or below") != (11, 12, "C")
    print("[self-check] parse_bucket: below/above/exact + partition contiguity OK")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--max-events", type=int, default=None, help="cap events (test)")
    ap.add_argument("--active", action="store_true", help="fetch active (unresolved) too")
    ap.add_argument("--sample", type=int, default=20)
    args = ap.parse_args()
    if args.selfcheck:
        selfcheck()
        return
    conn = init_db()
    events = enumerate_events(max_events=args.max_events)
    if args.active:
        events += enumerate_events(max_events=args.max_events, active=True)
    print(f"[parse] {len(events)} events -> markets_map")
    n = 0
    for ev in events:
        n += parse_event(ev, conn)
    conn.commit()
    print(f"[done] {n} market rows upserted")
    coverage_report(conn, args.sample)
    conn.close()


if __name__ == "__main__":
    main()
