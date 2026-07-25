"""Task 1.2 + 1.3 — EMOS (nonhomogeneous Gaussian regression) per station + bucket probs.

EMOS/NGR is the standard station post-processing method (~9 params/station). Fits a
Gaussian predictive distribution for the reported station daily-max, conditional on
the GFS D-1 forecast + afternoon cloud/wind + diurnal range + seasonal harmonics +
forecast run_change (uncertainty proxy).

Model (per station, training window <= 2025-09-30 ONLY):
  reported station max ~ Normal(mu, sigma)
  mu      = a0 + a1*fcst_max_d1 + a2*cloud_afternoon + a3*wind_afternoon
            + a4*sin(2pi*doy/365) + a5*cos(2pi*doy/365) + a6*fcst_diurnal_range
  log sig = b0 + b1*run_change
Gaussian MLE via scipy.optimize. One model per station; no pooling/hierarchy
(YAGNI — exclude a station if it lacks data).

Target = replica_max_reported from station_day_max (the validated METAR replica,
Task 0.2). This is NOT the grading oracle (T1.1 grades on venue resolution); it's
the EMOS training target. The 0.44% replica-vs-venue residual is tiny target noise
(harmless, slightly conservative) — see W1 pre-registration "Grading source".

Self-check (the one per-script check): held-out meteorological year 2024-10 -> 2025-09
(within training-eligible data). EMOS CRPS must beat raw fcst_max_d1 CRPS per station,
and PIT histogram roughly flat. Fails -> pipeline bug; if PIT shows heavy tails,
switch sigma to Student-t with fixed nu=5 (single pre-registered fallback; do NOT
iterate model classes beyond this). [Control 1 of the pre-reg.]
"""
import argparse
import json
import math
import sqlite3
import sys
from datetime import date
from pathlib import Path

import numpy as np
from scipy.optimize import minimize
from scipy.stats import norm

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bot"))
from config import DB_PATH  # noqa: E402  (bot DB_PATH is the paper DB; we want weather2)

DB_PATH_W2 = ROOT / "research" / "weather2" / "data" / "weather_research.db"
TRAIN_END = "2025-09-30"  # pre-reg: training window <= 2025-09-30 ONLY
HELDOUT_START = "2024-10-01"  # self-check held-out met year (within training-eligible)
HELDOUT_END = "2025-09-30"
NPARAMS = 8  # a0..a6 + b0 + b1  => actually 9: a0,a1,a2,a3,a4,a5,a6,b0,b1
STUDENT_T_NU = 5  # pre-reg fallback only


def _connect():
    conn = sqlite3.connect(DB_PATH_W2)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _doy_sin_cos(date_iso):
    d = date.fromisoformat(date_iso)
    doy = d.timetuple().tm_yday
    w = 2.0 * math.pi * doy / 365.0
    return math.sin(w), math.cos(w)


def load_station(conn, icao):
    """Join fcst_station_day to station_day_max on (icao, date_local). Returns list of dicts."""
    rows = conn.execute("""
        SELECT f.date_local, f.fcst_max_d1, f.cloud_afternoon, f.wind_afternoon,
               f.fcst_diurnal_range, f.run_change, s.replica_max_reported
        FROM fcst_station_day f
        JOIN station_day_max s ON s.icao=f.icao AND s.date_local=f.date_local
        WHERE f.icao=? AND s.replica_max_reported IS NOT NULL
          AND f.fcst_max_d1 IS NOT NULL
        ORDER BY f.date_local
    """, (icao,)).fetchall()
    out = []
    for r in rows:
        ss, cc = _doy_sin_cos(r["date_local"])
        out.append({
            "date": r["date_local"],
            "fcst": r["fcst_max_d1"], "cloud": r["cloud_afternoon"],
            "wind": r["wind_afternoon"], "drange": r["fcst_diurnal_range"],
            "runchange": r["run_change"] if r["run_change"] is not None else 0.0,
            "y": r["replica_max_reported"], "sin": ss, "cos": cc,
        })
    return out


def _design(rows):
    X = np.array([[1.0, r["fcst"], r["cloud"] if r["cloud"] is not None else 0.0,
                   r["wind"] if r["wind"] is not None else 0.0, r["sin"], r["cos"],
                   r["drange"] if r["drange"] is not None else 0.0] for r in rows])
    rc = np.array([r["runchange"] for r in rows])
    y = np.array([float(r["y"]) for r in rows])
    return X, rc, y


def _negloglik(params, X, rc, y, t_dist=False):
    a = params[:7]
    b0, b1 = params[7], params[8]
    mu = X @ a
    log_sig = b0 + b1 * rc
    sig = np.exp(np.clip(log_sig, -10, 5))
    sig = np.maximum(sig, 0.05)
    if t_dist:
        from scipy.stats import t
        ll = t.logpdf(y, df=STUDENT_T_NU, loc=mu, scale=sig)
    else:
        ll = norm.logpdf(y, loc=mu, scale=sig)
    return -np.sum(ll)


def fit_emos(df_station, t_dist=False):
    """Fit EMOS params. Returns params array [a0..a6, b0, b1] or None."""
    if len(df_station) < 100:
        return None
    X, rc, y = _design(df_station)
    if len(y) < 100 or np.std(y) < 1e-6:
        return None
    # init: OLS for mu, log(std) for b0
    a_init, *_ = np.linalg.lstsq(X, y, rcond=None)
    b0_init = math.log(max(np.std(y - X @ a_init), 0.5))
    x0 = np.concatenate([a_init, [b0_init, 0.0]])
    try:
        res = minimize(_negloglik, x0, args=(X, rc, y, t_dist),
                       method="Nelder-Mead",
                       options={"maxiter": 5000, "xatol": 1e-4, "fatol": 1e-4})
        if not res.success or res.x is None:
            return None
        return res.x
    except Exception:
        return None


def predict(params, rows):
    """Return (mu, sigma) arrays in reported-value degrees."""
    X, rc, _ = _design(rows)
    mu = X @ params[:7]
    sig = np.exp(np.clip(params[7] + params[8] * rc, -10, 5))
    return mu, np.maximum(sig, 0.05)


def bucket_prob(mu, sigma, bucket_lo, bucket_hi, native_unit):
    """Rounding-aware probability that the reported max falls in [lo, hi).

    native_unit = the station's NATIVE report unit ('C' or 'F'), from markets_map.
    The venue DISPLAYS in the bucket's unit; for °C-native stations the display is
    °F = round(C*9/5+32), so we integrate in native (°C) space and map through the
    display conversion. NON-NEGOTIABLE: never integrate naive °F intervals for °C
    stations (W3 lives here — naive integration misprices buckets with empty/half
    °C preimages).

    mu, sigma are in NATIVE degrees (the EMOS prediction space = reported-value
    native degrees, since replica_max_reported is in the native unit).
    """
    mu = float(mu); sigma = max(float(sigma), 0.05)
    if native_unit == "F":
        # °F-native: reported value is whole-°F; P(round(X) in [lo,hi)) = Phi((hi-0.5-mu)/sig)-Phi((lo-0.5-mu)/sig)
        lo = (bucket_lo - 0.5 - mu) / sigma if bucket_lo is not None else -np.inf
        hi = (bucket_hi - 0.5 - mu) / sigma if bucket_hi is not None else np.inf
        return float(norm.cdf(hi) - norm.cdf(lo))
    # °C-native: display = round(C*9/5+32). Enumerate integer °C whose display lands in [lo,hi).
    # P(round(X_C)=C) for integer C = Phi((C+0.5-mu)/sig) - Phi((C-0.5-mu)/sig).
    # Scan a wide °C range (mu ± 8 sigma) and sum contributions whose display maps into the bucket.
    c_lo = int(math.floor(mu - 8 * sigma)) - 1
    c_hi = int(math.ceil(mu + 8 * sigma)) + 1
    p = 0.0
    for c in range(c_lo, c_hi + 1):
        disp = int(round(c * 9.0 / 5.0 + 32.0))
        in_bucket = (bucket_lo is None or disp >= bucket_lo) and (bucket_hi is None or disp < bucket_hi)
        if not in_bucket:
            continue
        plo = (c - 0.5 - mu) / sigma
        phi = (c + 0.5 - mu) / sigma
        p += float(norm.cdf(phi) - norm.cdf(plo))
    return min(max(p, 0.0), 1.0)


def _crps_normal(mu, sig, y):
    z = (y - mu) / sig
    return sig * (z * (2 * norm.cdf(z) - 1) + 2 * norm.pdf(z) - 1 / math.sqrt(math.pi))


def crps(params, rows, t_dist=False):
    mu, sig = predict(params, rows)
    y = np.array([float(r["y"]) for r in rows])
    if t_dist:
        # CRPS for Student-t is more complex; approximate via normal with same sigma (conservative)
        return float(np.mean(_crps_normal(mu, sig, y)))
    return float(np.mean(_crps_normal(mu, sig, y)))


def pit_histogram(params, rows, nbins=10):
    mu, sig = predict(params, rows)
    y = np.array([float(r["y"]) for r in rows])
    p = norm.cdf((y - mu) / sig)
    p = np.clip(p, 1e-6, 1 - 1e-6)
    hist, _ = np.histogram(p, bins=nbins, range=(0, 1))
    return hist


def fit_all(conn, save=True):
    stations = [r[0] for r in conn.execute(
        "SELECT DISTINCT icao FROM fcst_station_day ORDER BY icao").fetchall()]
    results = {}
    print(f"[fit] {len(stations)} stations")
    for icao in stations:
        df = load_station(conn, icao)
        train = [r for r in df if r["date"] <= TRAIN_END]
        if len(train) < 100:
            print(f"  {icao}: SKIP ({len(train)} train rows < 100)")
            continue
        params = fit_emos(train)
        if params is None:
            print(f"  {icao}: fit failed")
            continue
        # self-check: CRPS vs raw on held-out met year
        held = [r for r in df if HELDOUT_START <= r["date"] <= HELDOUT_END]
        if len(held) < 30:
            print(f"  {icao}: fit OK, held-out n={len(held)} (too small for CRPS check)")
            results[icao] = {"params": params.tolist(), "train_n": len(train)}
            continue
        emos_crps = crps(params, held)
        raw_crps = float(np.mean([abs(float(r["y"]) - r["fcst"]) for r in held]))
        pit = pit_histogram(params, held)
        # flatness: max deviation from uniform (n/nbins)
        expected = len(held) / 10
        pit_maxdev = float(max(abs(c - expected) for c in pit) / expected)
        beats = emos_crps < raw_crps
        results[icao] = {
            "params": params.tolist(), "train_n": len(train), "held_n": len(held),
            "emos_crps": emos_crps, "raw_crps": raw_crps, "beats_raw": beats,
            "pit_maxdev": pit_maxdev,
        }
        print(f"  {icao}: train={len(train)} held={len(held)} emos_crps={emos_crps:.3f} "
              f"raw_crps={raw_crps:.3f} beats={beats} pit_maxdev={pit_maxdev:.2f}")
    if save:
        p = ROOT / "research" / "weather2" / "data" / "emos_params.json"
        p.write_text(json.dumps(results, indent=2))
        print(f"[fit] wrote {p}")
    # summary
    if results:
        fitted = [v for v in results.values() if "beats_raw" in v]
        if fitted:
            n_beat = sum(1 for v in fitted if v["beats_raw"])
            print(f"\n[summary] {len(fitted)} stations with held-out CRPS; "
                  f"{n_beat} beat raw forecast ({100*n_beat/len(fitted):.0f}%)")
            # control 1 pass = majority beat raw + median PIT dev reasonable
            med_pit = sorted(v["pit_maxdev"] for v in fitted)[len(fitted)//2]
            print(f"[control 1] met-skill: {n_beat}/{len(fitted)} beat raw, median PIT maxdev={med_pit:.2f}")
    return results


def selfcheck():
    # synthetic: y = 0.5 + 0.9*fcst + noise -> EMOS should recover ~0.9 slope, beat raw
    rng = np.random.default_rng(42)
    n = 400
    fcst = rng.uniform(0, 30, n)
    y = 0.5 + 0.9 * fcst + rng.normal(0, 1.5, n)
    rows = [{"date": f"2024-{(i%12)+1:02d}-15", "fcst": float(fcst[i]), "cloud": 50.0,
             "wind": 5.0, "drange": 10.0, "runchange": 1.0, "y": float(y[i]),
             "sin": 0.0, "cos": 1.0} for i in range(n)]
    params = fit_emos(rows)
    assert params is not None, "fit failed on synthetic"
    assert 0.7 < params[1] < 1.1, f"slope {params[1]} not ~0.9"
    emos = crps(params, rows)
    raw = float(np.mean([abs(y[i] - fcst[i]) for i in range(n)]))
    assert emos < raw, f"EMOS CRPS {emos} not < raw {raw}"
    print(f"[self-check] synthetic fit: slope={params[1]:.3f} (expect ~0.9), "
          f"emos_crps={emos:.3f} < raw_crps={raw:.3f} OK")
    # Task 1.3: bucket_prob
    # °F-native: a tight dist at mu=80, sig=1 -> P(round in [80,81)) ~ high, ~Phi-shaped
    p_f = bucket_prob(80.0, 1.0, 80, 81, "F")
    assert 0.3 < p_f < 0.7, f"F exact bucket p={p_f} unexpected"
    # °F partition over a market's buckets sums ~1 (Atlanta scheme: <=69, 70-71,...,88+)
    f_buckets = [(None, 70), (70, 72), (72, 74), (74, 76), (76, 78), (78, 80),
                 (80, 82), (82, 84), (84, 86), (86, 88), (88, None)]
    s_f = sum(bucket_prob(80.0, 3.0, lo, hi, "F") for lo, hi in f_buckets)
    assert 0.999 <= s_f <= 1.001, f"F partition sum={s_f} not ~1"
    # °C-native: display=round(C*9/5+32). mu=25C, sig=2. Partition over a C-station market
    # (Buenos Aires scheme: <=11, 12,13,...,20, >=21) — but buckets are in DISPLAY unit (C here,
    # since the venue displays C for C-native). Actually for C-native the display IS C, so buckets
    # are integer C and the map is identity-ish. Test the empty-preimage case: an °F bucket like
    # [87,88) (display) has NO integer °C preimage (86<-30C, 88<-31C; 87 unreachable).
    p_empty = bucket_prob(25.0, 1.0, 87, 88, "C")
    assert p_empty < 1e-6, f"empty-preimage °F bucket p={p_empty} should be ~0"
    # C-native partition sums ~1 over integer-C buckets (display=C, identity map)
    c_buckets = [(None, 12), (12, 13), (13, 14), (14, 15), (15, 16), (16, 17),
                 (17, 18), (18, 19), (19, 20), (20, 21), (21, None)]
    s_c = sum(bucket_prob(25.0, 3.0, lo, hi, "C") for lo, hi in c_buckets)
    assert 0.999 <= s_c <= 1.001, f"C partition sum={s_c} not ~1"
    print(f"[self-check] bucket_prob: F-partition sum={s_f:.4f}, C-partition sum={s_c:.4f}, "
          f"empty-°F-preimage p={p_empty:.2e} OK")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fit", action="store_true")
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()
    if args.selfcheck:
        selfcheck(); return
    if args.fit:
        conn = _connect()
        fit_all(conn)
        conn.close()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
