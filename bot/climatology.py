"""Climatological prior for the weather model (Bot B A/B).

DESIGN STATEMENT
================
Bot A (baseline) bets on p_model = ensemble blend (member fraction in bucket,
after station_bias). Bot B replaces it with a linear pool against a
climatological prior:

    p = (1 - α) * p_model + α * p_clim

where p_clim = historical fraction of daily-highs that fell in bucket B, taken
over a ±CLIM_WINDOW_DAYS calendar window around the target date across the
last CLIM_YEARS of ERA5 reanalysis (Open-Meteo Archive).

Why this design (research pass 2026-07-19):
- Warming trend: a 30-yr climatology is cold-biased for 2026 (models
  underestimate observed warming; BJP-trend paper doi:10.1002/joc.6788). A
  recent-decade window inherently embeds the trend, so detrend is unnecessary
  (deferred — YAGNI; the window IS the trend control).
- Blend: BMA (Raftery 2005) is gold-standard but heavy (EM + variance est) and
  would confound the A/B (it shifts model weights AND adds climatology). A
  linear pool is the one-knob Bayesian-shrinkage-toward-prior view — cleanest
  single-variable A/B. α defaults 0.30, tuned during IS, frozen 07-26.
- Open-Meteo Archive: temperature_2m_max daily, ERA5 back to 1950, ~5-day lag,
  timezone REQUIRED for daily vars (passed), missing values as null (rare over
  land, handled). Reuses calibration.py's fetch/TLS pattern.

The hypothesis under test: the deployed model underestimates YES at p∈[0.5,0.6)
(ensemble tail underdispersion — see [[polymarket-bot-calibration]]).
Climatology captures the historical hot-tail frequency; blending inflates those
probs toward truth. Whether the trade-off (worse day-skill, better tail) beats
the market is empirical — only the A/B can tell. Inconclusive = don't ship.

Climatology is OBSERVED historical truth, so it gets NO station_bias correction
(the bias corrects the model's systematic offset; climatology has none).
"""
import json
import ssl
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from config import (
    CITIES,
    CLIM_HIST_PATH,
    CLIM_WINDOW_DAYS,
    CLIM_YEARS,
    OPEN_METEO_ARCHIVE,
)

_HIST = None


def load_hist():
    p = Path(CLIM_HIST_PATH)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _get_hist():
    global _HIST
    if _HIST is None:
        _HIST = load_hist()
    return _HIST


def _doy(md):
    # month-day "MM-DD" → day-of-year in a non-leap reference year. Feb 29
    # (only in leap historical years) is skipped by callers via the ValueError
    # path in build; runtime only looks up committed dates that exist.
    m, d = md.split("-")
    return date(2001, int(m), int(d)).timetuple().tm_yday


def md_delta(a, b):
    """Shortest signed calendar-day distance between two MM-DD strings, with
    year-wrap. a="07-26", b="07-19" → +7; a="01-02", b="12-30" → +3."""
    delta = _doy(a) - _doy(b)
    if delta > 182:
        delta -= 365
    elif delta < -182:
        delta += 365
    return delta


def clim_probs(city, target_date, buckets):
    """Historical fraction of daily-highs (±window, across recent years) in
    each bucket. Returns {bucket.key: prob}. Empty/missing history → all-zero
    (blend falls back to pure model via caller's guard)."""
    hist = _get_hist().get(city, {})
    if not hist:
        return {b.key: 0.0 for b in buckets}
    target_md = target_date[5:]  # MM-DD
    vals = []
    for d, t in hist.items():
        if t is None:
            continue
        try:
            if abs(md_delta(d[5:], target_md)) <= CLIM_WINDOW_DAYS:
                vals.append(t)
        except ValueError:
            continue  # Feb 29 in a leap year — skip
    n = len(vals)
    if n == 0:
        return {b.key: 0.0 for b in buckets}
    return {b.key: sum(1 for v in vals if b.contains(v)) / n for b in buckets}


def blend_clim(p_blend, p_clim, alpha):
    """Linear pool (1-α)·model + α·clim. p_clim None → pure model (Bot A path
    or no-history bucket). Pure function so the wiring is unit-testable."""
    if p_clim is None:
        return p_blend
    return (1.0 - alpha) * p_blend + alpha * p_clim


# --- build side (run once, commits data/clim_hist.json) ---

def _ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "MarcusVaultBot/1.0"})
    with urllib.request.urlopen(req, timeout=60, context=_ctx()) as r:
        return json.loads(r.read().decode())


def build():
    """Fetch CLIM_YEARS[0]..[1] daily-highs per city → data/clim_hist.json.
    Structure: {city: {"YYYY-MM-DD": temp_max, ...}}."""
    start = f"{CLIM_YEARS[0]}-01-01"
    end = f"{CLIM_YEARS[1]}-12-31"
    out = {}
    for city, m in CITIES.items():
        params = {
            "latitude": m["lat"], "longitude": m["lon"],
            "daily": "temperature_2m_max",
            "start_date": start, "end_date": end,
            "timezone": m["timezone"],
        }
        url = OPEN_METEO_ARCHIVE + "?" + urllib.parse.urlencode(params)
        r = _get(url)
        times = (r.get("daily") or {}).get("time") or []
        vals = (r.get("daily") or {}).get("temperature_2m_max") or []
        out[city] = {times[i]: vals[i] for i in range(min(len(times), len(vals)))
                     if vals[i] is not None}
        print(f"{city}: {len(out[city])} days", flush=True)
    p = Path(CLIM_HIST_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out))
    print(f"wrote {p} ({p.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    build()
