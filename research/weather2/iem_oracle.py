"""Task 0.2 — IEM ASOS METAR ingest + resolution-replica oracle gate.

Fetches hourly+SPECI temperatures from IEM ASOS for every ICAO in markets_map
(parse_status='ok'), 2022-07-01 -> present, chunked per-station-year (1 GB RAM).
Computes replica_daily_max per local calendar day, replicating the venue's chain:
  native whole-degree °C METAR report -> display-unit conversion
  (°F stations: °F = round(C*9/5 + 32)) -> max over the local day.

Validation STOP-gate: >=60 resolved station-days, >=5 stations incl >=2 non-US,
sample incl BOTH °C and °F stations (rounding chain exercised). Require >=95%
agreement between the replica-implied bucket and the venue resolution.

NON-NEGOTIABLE: if <95% cannot be reached and explained, the ENTIRE weather class
is BLOCKED (cannot grade -> nothing downstream runs). That outcome is a verdict.

Mismatch investigation order: (1) local-day boundary std vs DST; (2) SPECIs;
(3) METAR 6-hr max-temperature groups; (4) COR corrections; (5) Wunderground
processing. Each mismatch documented in ORACLE_AUDIT.md.

The max-°C-then-convert vs convert-each-then-max ambiguity: for °F stations we
compute BOTH replica_max_F_convmax (round(max_C * 9/5 + 32)) and
replica_max_F_each (max of round(c*9/5+32) per obs). They usually agree; a
disagreement on a resolved day is itself a mismatch to investigate (the venue's
rule reveals itself by which one matches). Stored as station_day_max columns.
"""
import argparse
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bot"))
import httpx  # noqa: E402
from config import tls_verify  # noqa: E402

DB_PATH = ROOT / "research" / "weather2" / "data" / "weather_research.db"
IEM_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
UA = {"User-Agent": "MarcusVaultBot/1.0"}
START_YEAR = 2022
# IEM rate-limits (429 on rapid parallel hits observed); throttle between stations.
PER_STATION_SLEEP = 1.5
# max concurrent station-years per fetch window (chunk by year to bound memory)
CHUNK_YEARS = 1


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS metar_obs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, icao TEXT, valid_local TEXT,
            tmpc REAL, is_special INTEGER, fetched TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_metar_icao ON metar_obs(icao);
        CREATE INDEX IF NOT EXISTS ix_metar_valid ON metar_obs(icao, valid_local);
        CREATE TABLE IF NOT EXISTS station_day_max (
            icao TEXT, date_local TEXT, native_unit TEXT, display_unit TEXT,
            replica_max_reported REAL, replica_max_c REAL,
            replica_max_f_convmax INTEGER, replica_max_f_each INTEGER,
            n_obs INTEGER, agree_method INTEGER, PRIMARY KEY(icao, date_local)
        );
        CREATE TABLE IF NOT EXISTS oracle_validation (
            icao TEXT, date_local TEXT, unit TEXT, replica_max_display INTEGER,
            bucket_lo INTEGER, bucket_hi INTEGER, venue_resolved_yes INTEGER,
            replica_yes INTEGER, agree INTEGER, mismatch_note TEXT
        );
    """)
    conn.commit()
    return conn


def fetch_station_year(icao, year, tz):
    """Fetch tmpc for one station-year in the station's local tz. Returns list of (valid_local, tmpc)."""
    params = {
        "station": icao, "data": "tmpc",
        "year1": str(year), "month1": "1", "day1": "1",
        "year2": str(year), "month2": "12", "day2": "31",
        "tz": tz,
    }
    try:
        with httpx.Client(timeout=60, verify=tls_verify(), headers=UA) as c:
            r = c.get(IEM_URL, params=params)
    except Exception as e:
        print(f"[warn] {icao} {year} fetch exc: {e}", file=sys.stderr)
        return []
    if r.status_code != 200:
        print(f"[warn] {icao} {year} http {r.status_code}", file=sys.stderr)
        return []
    out = []
    for ln in r.text.strip().split("\n")[1:]:
        p = ln.split(",")
        if len(p) < 3:
            continue
        valid, tmpc_s = p[1], p[2]
        if tmpc_s in ("", "M"):
            continue
        try:
            tmpc = float(tmpc_s)
        except ValueError:
            continue
        out.append((valid, tmpc))
    return out


def c_to_f_round(c):
    return int(round(c * 9.0 / 5.0 + 32.0))


def compute_daily_max(rows, native_unit):
    """rows: list of (valid_local 'YYYY-MM-DD HH:MM', tmpc). Returns dict date_local -> max dict."""
    by_day = {}
    for valid, tmpc in rows:
        date_local = valid[:10]
        d = by_day.setdefault(date_local, {"vals": [], "n": 0})
        d["vals"].append(tmpc)
        d["n"] += 1
    out = {}
    for date_local, d in by_day.items():
        vals = d["vals"]
        max_c = max(vals)
        if native_unit == "C":
            replica_max_reported = int(round(max_c))  # native whole-degree
            convmax = c_to_f_round(max_c)
            each = max(c_to_f_round(v) for v in vals)
        else:  # °F-display station: METAR is °C, venue displays °F
            replica_max_reported = c_to_f_round(max_c)  # convert the max
            convmax = c_to_f_round(max_c)
            each = max(c_to_f_round(v) for v in vals)
        agree_method = 1 if convmax == each else 0
        out[date_local] = {
            "native_unit": native_unit, "display_unit": "F" if native_unit == "C" else "F",
            "replica_max_reported": replica_max_reported, "replica_max_c": max_c,
            "replica_max_f_convmax": convmax, "replica_max_f_each": each,
            "n_obs": d["n"], "agree_method": agree_method,
        }
    return out


def ingest(conn, icaos_tz_unit, max_stations=None):
    """Fetch + store metar_obs + station_day_max for each station."""
    n_stations = 0
    cur_year = datetime.now().year
    for icao, tz, native_unit in icaos_tz_unit:
        if max_stations and n_stations >= max_stations:
            break
        print(f"[ingest] {icao} ({tz}, {native_unit}) ...", flush=True)
        all_rows = []
        for y in range(START_YEAR, cur_year + 1):
            rows = fetch_station_year(icao, y, tz)
            all_rows.extend(rows)
            time.sleep(0.3)
        if not all_rows:
            print(f"[warn] {icao}: no obs fetched", file=sys.stderr)
            time.sleep(PER_STATION_SLEEP)
            continue
        # store metar_obs (chunked insert)
        fetched = datetime.utcnow().isoformat(timespec="seconds")
        conn.executemany(
            "INSERT INTO metar_obs(icao, valid_local, tmpc, is_special, fetched) VALUES (?,?,?,?,?)",
            [(icao, v, t, 0, fetched) for v, t in all_rows],
        )
        daily = compute_daily_max(all_rows, native_unit)
        conn.executemany(
            """INSERT OR REPLACE INTO station_day_max
            (icao, date_local, native_unit, display_unit, replica_max_reported,
             replica_max_c, replica_max_f_convmax, replica_max_f_each, n_obs, agree_method)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [(icao, dl, d["native_unit"], d["display_unit"], d["replica_max_reported"],
              d["replica_max_c"], d["replica_max_f_convmax"], d["replica_max_f_each"],
              d["n_obs"], d["agree_method"]) for dl, d in daily.items()],
        )
        conn.commit()
        n_stations += 1
        print(f"  {icao}: {len(all_rows)} obs, {len(daily)} station-days, "
              f"max={max(d['replica_max_reported'] for d in daily.values())}")
        time.sleep(PER_STATION_SLEEP)
    return n_stations


def validate(conn, sample_n=None):
    """Compare replica bucket vs venue resolution on resolved station-days. Returns stats."""
    # join station_day_max to markets_map resolved rows; one comparison per (icao, date, bucket)
    # use replica_max_reported (display-unit max) as the primary replica
    q = """
        SELECT m.icao, m.event_date_local, m.unit, m.bucket_lo, m.bucket_hi,
               m.resolved_yes, s.replica_max_reported, s.replica_max_f_convmax,
               s.replica_max_f_each, s.agree_method, m.question
        FROM markets_map m
        JOIN station_day_max s ON s.icao=m.icao AND s.date_local=m.event_date_local
        WHERE m.parse_status='ok' AND m.resolved_yes IS NOT NULL
          AND m.event_date_local IS NOT NULL
    """
    rows = conn.execute(q).fetchall()
    if not rows:
        print("[validate] no joinable resolved station-days yet — run --ingest first")
        return
    # de-dup: one row per (icao, date, bucket) — markets_map has one market per bucket already
    n_total = 0
    n_agree = 0
    n_method_disagree = 0
    mismatches = []
    units_seen = {}
    icaos_seen = set()
    non_us_seen = set()
    US_PREFIX = ("K", "P")
    for r in rows:
        icao, date, unit, lo, hi, vres, rmax, convmax, each, am, qtxt = r
        rmax_int = int(rmax)
        in_bucket = (lo is None or rmax_int >= lo) and (hi is None or rmax_int < hi)
        replica_yes = 1 if in_bucket else 0
        agree = 1 if replica_yes == vres else 0
        n_total += 1
        n_agree += agree
        if am == 0:
            n_method_disagree += 1
        icaos_seen.add(icao)
        if not icao.startswith(US_PREFIX):
            non_us_seen.add(icao)
        units_seen[unit] = units_seen.get(unit, 0) + 1
        if not agree:
            mismatches.append((icao, date, unit, lo, hi, vres, rmax_int, convmax, each, am, qtxt))
    pct = 100.0 * n_agree / n_total if n_total else 0.0
    print("\n=== ORACLE VALIDATION ===")
    print(f"comparisons: {n_total} | agree: {n_agree} ({pct:.1f}%)")
    print(f"distinct stations: {len(icaos_seen)} (non-US: {len(non_us_seen)})")
    print(f"units: {units_seen}")
    print(f"method-disagree (convmax != each) station-days: {n_method_disagree}")
    print(f"mismatches: {len(mismatches)}")
    # store validation rows
    conn.execute("DELETE FROM oracle_validation")
    conn.executemany(
        """INSERT INTO oracle_validation
        (icao, date_local, unit, replica_max_display, bucket_lo, bucket_hi,
         venue_resolved_yes, replica_yes, agree, mismatch_note)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        [(m[0], m[1], m[2], m[6], m[3], m[4], m[5], 1 if (m[3] is None or m[6]>=m[3]) and (m[4] is None or m[6]<m[4]) else 0,
          1 if (1 if (m[3] is None or m[6]>=m[3]) and (m[4] is None or m[6]<m[4]) else 0)==m[5] else 0,
          "" if (1 if (m[3] is None or m[6]>=m[3]) and (m[4] is None or m[6]<m[4]) else 0)==m[5] else "MISMATCH")
         for m in mismatches],
    )
    conn.commit()
    # gate check
    floor_stations = len(icaos_seen) >= 5
    floor_nonus = len(non_us_seen) >= 2
    floor_days = n_total >= 60
    floor_units = "C" in units_seen and "F" in units_seen
    gate_pct = pct >= 95.0
    print("\n=== GATE ===")
    print(f"  >=60 station-days: {'PASS' if floor_days else 'FAIL'} ({n_total})")
    print(f"  >=5 stations:      {'PASS' if floor_stations else 'FAIL'} ({len(icaos_seen)})")
    print(f"  >=2 non-US:        {'PASS' if floor_nonus else 'FAIL'} ({len(non_us_seen)})")
    print(f"  both °C+°F:        {'PASS' if floor_units else 'FAIL'} ({units_seen})")
    print(f"  >=95% agreement:   {'PASS' if gate_pct else 'FAIL'} ({pct:.1f}%)")
    passed = floor_days and floor_stations and floor_nonus and floor_units and gate_pct
    print(f"\n>>> GATE: {'PASS (oracle validated — proceed to W1 fan-out)' if passed else 'FAIL (investigate mismatches; <95% explained = weather class BLOCKED)'}")
    # print mismatch sample for investigation
    if mismatches:
        print(f"\n=== first 20 mismatches (investigate per ORACLE_AUDIT order) ===")
        print(f"{'icao':<6} {'date':<11} {'u':<2} {'lo':>4} {'hi':>4} {'vres':>4} {'rmax':>4} {'cmax':>4} {'each':>4} {'am':>3} {'q'}")
        for m in mismatches[:20]:
            print(f"{m[0]:<6} {m[1]:<11} {m[2]:<2} {str(m[3]):>4} {str(m[4]):>4} {m[5]:>4} {m[6]:>4} {m[7]:>4} {m[8]:>4} {m[9]:>3} {m[10][:50]}")
    return passed


def stations_to_ingest(conn):
    rows = conn.execute("""
        SELECT DISTINCT icao, tz_name, unit FROM markets_map
        WHERE parse_status='ok' AND icao IS NOT NULL AND tz_name IS NOT NULL
        ORDER BY icao
    """).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ingest", action="store_true", help="fetch METAR + compute daily max")
    ap.add_argument("--validate", action="store_true", help="run the oracle validation gate")
    ap.add_argument("--max-stations", type=int, default=None)
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()
    if args.selfcheck:
        # c_to_f_round + compute_daily_max correctness
        assert c_to_f_round(0) == 32 and c_to_f_round(100) == 212 and c_to_f_round(25.0) == 77
        assert c_to_f_round(3.33) == 38  # observed KLGA pilot
        rows = [("2026-03-11 14:00", 28.0), ("2026-03-11 10:00", 25.0), ("2026-03-11 13:00", 18.0)]
        d = compute_daily_max(rows, "C")
        assert d["2026-03-11"]["replica_max_reported"] == 28  # SAEZ pilot
        # °F each-vs-convmax: a day with max_c=25.0 -> convmax 77; if a sub-obs rounds up to 78, each>convmax
        rows2 = [("2026-01-01 10:00", 25.6), ("2026-01-01 11:00", 25.0)]
        d2 = compute_daily_max(rows2, "C")
        # 25.6->78, 25.0->77; convmax round(25.6*9/5+32)=round(78.08)=78; each=max(78,77)=78 -> agree
        assert d2["2026-01-01"]["replica_max_f_convmax"] == 78
        assert d2["2026-01-01"]["replica_max_f_each"] == 78
        assert d2["2026-01-01"]["agree_method"] == 1
        print("[self-check] c_to_f_round + compute_daily_max (°C native, °F convmax/each) OK")
        return
    conn = init_db()
    if args.ingest:
        stns = stations_to_ingest(conn)
        print(f"[ingest] {len(stns)} stations to fetch ({START_YEAR}-present), throttled {PER_STATION_SLEEP}s/station")
        n = ingest(conn, stns, args.max_stations)
        print(f"[ingest] done: {n} stations")
    if args.validate:
        validate(conn)
    if not (args.ingest or args.validate or args.selfcheck):
        ap.print_help()
    conn.close()


if __name__ == "__main__":
    main()
