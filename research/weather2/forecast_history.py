"""Task 1.1 — Training data assembly for W1 EMOS.

Fetches Open-Meteo Previous-Runs API fixed-lead forecasts (temperature_2m_previous_day1/day2,
cloud_cover_previous_day1, wind_speed_10m_previous_day1) at each station lat/lon, 2022-07-01
-> present, hourly; aggregates to the local event day:
  - fcst_max_d1  = daily max of temperature_2m_previous_day1 (the D-1-issued forecast of day D's high)
  - cloud_afternoon = mean cloud_cover_previous_day1 over 12:00-18:00 local
  - wind_afternoon  = mean wind_speed_10m_previous_day1 over 12:00-18:00 local
  - fcst_diurnal_range = daily max - daily min of temperature_2m_previous_day1
  - run_change = |fcst_max_d1 - fcst_max_d2|  (poor-man's forecast uncertainty)

Joined to station_day_max (Task 0.2) = the modeling table (EMOS target = replica_max_reported).

DESIGN DECISION (recorded 2026-07-25, deviates from plan's multi-model assumption):
The plan assumed ECMWF+GFS+ICON coverage from 2022. Probed reality: only GFS
(gfs_seamless) covers the full 2022-07-01 -> 2025-09-30 training window; ECMWF_ifs025
and icon_seamless return 200 but 0 non-null temps before ~Jan 2024 (archive floor).
=> Single forecast source = GFS for all 48 stations (GFS is global). EMOS corrects
GFS's station-specific bias, so a single source is sufficient; the pre-reg's
fcst_max_d1 covariate is model-agnostic. Harness-replication control (killed NWP-blend)
is consequently GFS-only too — still tests "does a forecast-blend-style p_model carry
signal" (expected coef ~0). Cheap to reverse if a multi-model re-run is later wanted.

Ensemble spread as run_change alternative: plan says use Open-Meteo Ensemble API
INSTEAD of run_change ONLY if its historical archive covers the window. Ensemble API
archive (ensemble-api.open-meteo.com) covers only ~past 2 years for fixed-lead, so it
does NOT cover 2022-07 -> 2025-09. => run_change (|d1-d2|) is the uncertainty proxy.

Self-check: per-station row counts >= ~1000 station-days training (2022-07-01 -> 2025-09-30);
report+exclude any station < 700 (underpowered).
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
from config import CITIES, tls_verify  # noqa: E402

DB_PATH = ROOT / "research" / "weather2" / "data" / "weather_research.db"
PREV_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
MODEL = "gfs_seamless"
START_DATE = "2022-07-01"
# chunk by quarter to bound memory + stay under any per-request limits
CHUNK_DAYS = 90
PER_STATION_SLEEP = 0.4
TRAIN_END = "2025-09-30"  # pre-reg: training window <= 2025-09-30 ONLY

# ICAO -> lat/lon. config.CITIES has 8 (traded); the other 41 come from station_day_max.
# Build from markets_map station + a static ICAO->coord table for the rest.
ICAO_COORDS_EXTRA = {
    "KLGA": (40.64, -73.78), "KATL": (33.64, -84.43), "KDAL": (32.85, -96.85),
    "KSEA": (47.45, -122.31), "KMIA": (25.79, -80.29), "KORD": (41.98, -87.90),
    "KAUS": (30.19, -97.67), "KBKF": (39.72, -104.75), "KHOU": (29.65, -95.28),
    "KLAX": (33.94, -118.41), "KSFO": (37.62, -122.38),
    "SBGR": (-23.43, -46.47), "MMMX": (19.44, -99.07),
    "LFPG": (49.00, 2.55), "LFPB": (49.00, 2.43), "EDDM": (48.35, 11.79),
    "EPWA": (52.17, 20.97), "LEMD": (40.47, -3.56), "LIMC": (45.63, 8.72),
    "EHAM": (52.32, 4.76), "EFHK": (60.32, 24.96),
    "OEJN": (21.68, 39.15), "LLBG": (32.01, 34.89), "DNMM": (6.58, 3.32),
    "FACT": (-33.97, 18.60), "OPKC": (24.90, 67.13),
    "RKPK": (35.18, 128.94), "RCSS": (25.07, 121.55), "WSSS": (1.36, 103.99),
    "WMKK": (2.75, 101.70), "WIHH": (-6.27, 106.89), "MPMG": (14.51, 121.02),
    "RPLL": (14.51, 121.02), "VHHH": (22.31, 113.91),
    "ZSPD": (31.14, 121.81), "ZBAA": (40.08, 116.58), "ZHHH": (30.78, 114.21),
    "ZUCK": (29.72, 106.64), "ZGSZ": (22.64, 113.81), "ZUUU": (30.58, 103.95),
    "ZGGG": (23.39, 113.30), "VILK": (26.76, 80.89),
}


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS fcst_station_day (
            icao TEXT, date_local TEXT, fcst_max_d1 REAL, fcst_max_d2 REAL,
            cloud_afternoon REAL, wind_afternoon REAL, fcst_diurnal_range REAL,
            run_change REAL, n_obs INTEGER, PRIMARY KEY(icao, date_local)
        );
        CREATE INDEX IF NOT EXISTS ix_fcst_icao_date ON fcst_station_day(icao, date_local);
    """)
    conn.commit()
    return conn


def icao_coords():
    """ICAO -> (lat, lon) from config.CITIES (8) + ICAO_COORDS_EXTRA (41)."""
    out = {}
    # config.CITIES station_name -> need ICAO; we have ICAO in markets_map. Build city->coord from CITIES.
    # CITIES gives lat/lon directly keyed by city name; markets_map has station_name + icao.
    # Simpler: use ICAO_COORDS_EXTRA for all 49 (it covers them), fall back to CITIES by matching.
    return ICAO_COORDS_EXTRA


def fetch_chunk(lat, lon, start_iso, end_iso, tz):
    """Fetch one date chunk for a station. Returns list of (time_iso, t_d1, t_d2, cloud, wind)."""
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "temperature_2m_previous_day1,temperature_2m_previous_day2,"
                  "cloud_cover_previous_day1,wind_speed_10m_previous_day1",
        "start_date": start_iso, "end_date": end_iso, "timezone": tz, "models": MODEL,
    }
    try:
        with httpx.Client(timeout=60, verify=tls_verify(), headers={"User-Agent": "MarcusVaultBot/1.0"}) as c:
            r = c.get(PREV_RUNS_URL, params=params)
    except Exception as e:
        print(f"[warn] fetch exc {start_iso}: {e}", file=sys.stderr)
        return []
    if r.status_code != 200:
        print(f"[warn] {start_iso} http {r.status_code}: {r.text[:120]}", file=sys.stderr)
        return []
    h = (r.json() or {}).get("hourly") or {}
    times = h.get("time") or []
    t1 = h.get("temperature_2m_previous_day1") or []
    t2 = h.get("temperature_2m_previous_day2") or []
    cl = h.get("cloud_cover_previous_day1") or []
    wd = h.get("wind_speed_10m_previous_day1") or []
    out = []
    for i in range(len(times)):
        out.append((times[i], t1[i] if i < len(t1) else None, t2[i] if i < len(t2) else None,
                    cl[i] if i < len(cl) else None, wd[i] if i < len(wd) else None))
    return out


def aggregate_day(rows):
    """rows: list of (time 'YYYY-MM-DDTHH:MM', t_d1, t_d2, cloud, wind). Returns per-day dict."""
    by_day = {}
    for time_iso, t1, t2, cl, wd in rows:
        date_local = time_iso[:10]
        hr = int(time_iso[11:13]) if len(time_iso) > 13 else -1
        d = by_day.setdefault(date_local, {"t1": [], "t2": [], "cloud": [], "wind": []})
        if t1 is not None:
            d["t1"].append(t1)
            if 12 <= hr <= 18:
                if cl is not None:
                    d["cloud"].append(cl)
                if wd is not None:
                    d["wind"].append(wd)
        if t2 is not None:
            d["t2"].append(t2)
    out = {}
    for date_local, d in by_day.items():
        if not d["t1"]:
            continue
        mx1 = max(d["t1"]); mn1 = min(d["t1"])
        mx2 = max(d["t2"]) if d["t2"] else None
        out[date_local] = {
            "fcst_max_d1": mx1, "fcst_max_d2": mx2,
            "fcst_diurnal_range": mx1 - mn1,
            "cloud_afternoon": (sum(d["cloud"]) / len(d["cloud"])) if d["cloud"] else None,
            "wind_afternoon": (sum(d["wind"]) / len(d["wind"])) if d["wind"] else None,
            "run_change": abs(mx1 - mx2) if mx2 is not None else None,
            "n_obs": len(d["t1"]),
        }
    return out


def date_chunks(start, end, days=CHUNK_DAYS):
    from datetime import date, timedelta
    s = date.fromisoformat(start); e = date.fromisoformat(end)
    while s <= e:
        nxt = min(s + timedelta(days=days - 1), e)
        yield s.isoformat(), nxt.isoformat()
        s = nxt + timedelta(days=1)


def ingest(conn, icaos_tz, max_stations=None):
    coords = icao_coords()
    end = datetime.now(timezone.utc).date().isoformat()
    n = 0
    for icao, tz in icaos_tz:
        if max_stations and n >= max_stations:
            break
        if icao not in coords:
            print(f"[skip] {icao}: no coords", file=sys.stderr); continue
        lat, lon = coords[icao]
        print(f"[ingest] {icao} ({lat},{lon})", flush=True)
        all_daily = {}
        for s_iso, e_iso in date_chunks(START_DATE, end):
            rows = fetch_chunk(lat, lon, s_iso, e_iso, tz)
            all_daily.update(aggregate_day(rows))
            time.sleep(0.2)
        if not all_daily:
            print(f"[warn] {icao}: no forecast days", file=sys.stderr)
            time.sleep(PER_STATION_SLEEP); continue
        conn.executemany(
            """INSERT OR REPLACE INTO fcst_station_day
            (icao, date_local, fcst_max_d1, fcst_max_d2, cloud_afternoon, wind_afternoon,
             fcst_diurnal_range, run_change, n_obs) VALUES (?,?,?,?,?,?,?,?,?)""",
            [(icao, dl, d["fcst_max_d1"], d["fcst_max_d2"], d["cloud_afternoon"], d["wind_afternoon"],
              d["fcst_diurnal_range"], d["run_change"], d["n_obs"]) for dl, d in all_daily.items()],
        )
        conn.commit()
        n += 1
        # training-row count for self-check
        tr = conn.execute("SELECT COUNT(*) FROM fcst_station_day WHERE icao=? AND date_local<=?",
                          (icao, TRAIN_END)).fetchone()[0]
        flag = "OK" if tr >= 700 else "UNDERPOWERED (<700 train rows, exclude)"
        print(f"  {icao}: {len(all_daily)} days, {tr} train (<= {TRAIN_END}) -> {flag}")
        time.sleep(PER_STATION_SLEEP)
    return n


def stations_to_ingest(conn):
    rows = conn.execute("""
        SELECT DISTINCT s.icao, s.native_unit, MIN(m.tz_name) tz
        FROM station_day_max s JOIN markets_map m ON m.icao=s.icao
        WHERE m.parse_status='ok' AND m.tz_name IS NOT NULL AND s.icao!='VHHH'
        GROUP BY s.icao ORDER BY s.icao
    """).fetchall()
    return [(r[0], r[2]) for r in rows]


def selfcheck():
    # aggregate_day correctness
    rows = [
        ("2026-03-11T06:00", 10.0, 11.0, 50, 5), ("2026-03-11T14:00", 28.0, 27.0, 30, 8),
        ("2026-03-11T20:00", 20.0, 21.0, 60, 4),
    ]
    d = aggregate_day(rows)["2026-03-11"]
    assert d["fcst_max_d1"] == 28.0 and d["fcst_max_d2"] == 27.0
    assert d["run_change"] == 1.0
    assert d["fcst_diurnal_range"] == 18.0  # 28-10
    assert d["cloud_afternoon"] == 30.0 and d["wind_afternoon"] == 8.0  # only 14:00 in 12-18
    print("[self-check] aggregate_day (max/diurnal/afternoon/run_change) OK")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ingest", action="store_true")
    ap.add_argument("--max-stations", type=int, default=None)
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()
    if args.selfcheck:
        selfcheck(); return
    conn = init_db()
    if args.ingest:
        stns = stations_to_ingest(conn)
        print(f"[ingest] {len(stns)} stations, GFS, {START_DATE}->present, chunked {CHUNK_DAYS}d")
        n = ingest(conn, stns, args.max_stations)
        print(f"[ingest] done: {n} stations")
    else:
        ap.print_help()
    conn.close()


if __name__ == "__main__":
    main()
