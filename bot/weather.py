import hashlib
import json
import re
import statistics
from dataclasses import dataclass, field

import httpx

from config import (
    CONSENSUS_MAX_DISAGREEMENT_C,
    MODEL_COL_SUFFIX,
    MODELS,
    OPEN_METEO_ENSEMBLE,
    SIGMA_CALM,
    STATION_BIAS_MIN,
    VAR_GATE_K,
    VAR_GATE_REARM,
    _C,
    tls_verify,
)


@dataclass
class ModelRun:
    city: str
    model: str
    run_ts: str
    daily_high_by_date: dict
    content_hash: str


@dataclass
class Bucket:
    key: str
    lo: float | None
    lo_incl: bool
    hi: float | None
    hi_incl: bool

    def contains(self, x):
        if self.lo is not None:
            if self.lo_incl:
                if x < self.lo:
                    return False
            elif x <= self.lo:
                return False
        if self.hi is not None:
            if self.hi_incl:
                if x > self.hi:
                    return False
            elif x >= self.hi:
                return False
        return True


def parse_bucket(question):
    m = re.search(r"be\s+(-?\d+)\s*°\s*([CFcf])", question)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).upper()
    tail = question[m.end():].lower()
    if unit == "F":
        c = (n - 32) * 5 / 9
    else:
        c = float(n)
    key = question.split(" be ")[-1].split(" on ")[0].strip()
    if "or higher" in tail or "or above" in tail:
        return Bucket(key=key, lo=c, lo_incl=True, hi=None, hi_incl=False)
    if "or lower" in tail or "or below" in tail:
        return Bucket(key=key, lo=None, lo_incl=False, hi=c + 1, hi_incl=False)
    return Bucket(key=key, lo=c, lo_incl=True, hi=c + 1, hi_incl=False)


def fetch_runs(cities):
    runs = []
    with httpx.Client(timeout=60, headers={"User-Agent": "MarcusVaultBot/1.0"},
                      verify=tls_verify()) as client:
        for city, meta in cities.items():
            params = {
                "latitude": meta["lat"],
                "longitude": meta["lon"],
                "models": ",".join(MODELS.keys()),
                "hourly": "temperature_2m",
                "past_days": 2,
                "forecast_days": 4,
                "timezone": meta["timezone"],
            }
            try:
                r = client.get(OPEN_METEO_ENSEMBLE, params=params).json()
            except Exception:
                continue
            hourly = r.get("hourly") or {}
            times = hourly.get("time") or []
            for model, suffix in MODEL_COL_SUFFIX.items():
                member_cols = [
                    k for k in hourly if k.startswith(f"temperature_2m_member")
                    and k.endswith(f"_{suffix}")
                ]
                if not member_cols:
                    continue
                daily_high_by_member = {}
                for col in member_cols:
                    series = hourly[col] or []
                    by_day = {}
                    for i, t in enumerate(times):
                        if i >= len(series):
                            continue
                        v = series[i]
                        if v is None:
                            continue
                        day = t[:10]
                        if day not in by_day or v > by_day[day]:
                            by_day[day] = v
                    for day, hi in by_day.items():
                        daily_high_by_member.setdefault(day, []).append(hi)
                canon = json.dumps(daily_high_by_member, sort_keys=True)
                h = hashlib.sha1(canon.encode()).hexdigest()
                runs.append(ModelRun(
                    city=city,
                    model=model,
                    run_ts="",
                    daily_high_by_date=daily_high_by_member,
                    content_hash=h,
                ))
    return runs


def daily_high_probs(run, date, buckets, bias):
    members = run.daily_high_by_date.get(date, [])
    out = {}
    if not members:
        for b in buckets:
            out[b.key] = 0.0
        return out
    n = len(members)
    for b in buckets:
        cnt = 0
        for m in members:
            if b.contains(m + bias):
                cnt += 1
        out[b.key] = cnt / n
    return out


def blend(probs_by_model, weights):
    keys = set()
    for p in probs_by_model.values():
        keys.update(p.keys())
    wsum = sum(weights.get(m, 0.0) for m in probs_by_model) or 1.0
    out = {}
    for k in keys:
        s = 0.0
        for model, p in probs_by_model.items():
            s += weights.get(model, 0.0) * p.get(k, 0.0)
        out[k] = s / wsum
    return out


def consensus_ok(runs, date):
    means = []
    for run in runs:
        members = run.daily_high_by_date.get(date, [])
        if not members:
            continue
        means.append(sum(members) / len(members))
    for i in range(len(means)):
        for j in range(i + 1, len(means)):
            if abs(means[i] - means[j]) > CONSENSUS_MAX_DISAGREEMENT_C:
                return False
    return True


def station_bias(residuals):
    if len(residuals) < 5:
        return 0.0
    last = residuals[-20:]
    # _C: robust bias = EWM halflife-10 over winsorized residuals. Winsorizing
    # (clip to window-median ± 3×MAD) bounds any single OOD day's pull while
    # keeping the point in the series; plain EWM lets one storm shift bias ~0.7σ.
    b = _ewm(_winsorize(last)) if _C else sum(last) / len(last)
    if abs(b) < STATION_BIAS_MIN:
        return 0.0
    return b


def _winsorize(xs):
    if len(xs) < 3:
        return list(xs)
    med = statistics.median(xs)
    mad = statistics.median([abs(x - med) for x in xs])
    lo, hi = med - 3 * mad, med + 3 * mad
    return [min(hi, max(lo, x)) for x in xs]


def _ewm(xs, halflife=10):
    alpha = 1.0 - 2.0 ** (-1.0 / halflife)
    acc = wsum = 0.0
    for x in xs:
        acc = (1.0 - alpha) * acc + alpha * x
        wsum = (1.0 - alpha) * wsum + alpha
    return acc / wsum if wsum else 0.0


def ood_tripped(residuals, bias, sigma=SIGMA_CALM, k=VAR_GATE_K,
                rearm=VAR_GATE_REARM):
    """Stateless hysteresis: is a city currently in an OOD trip? Walks the
    residual series newest→oldest. Re-arms (False) at the first point with
    |r-bias| < rearm*σ. Trips (True) if a >k*σ point precedes any re-arm — i.e.
    the extreme is still active. Fires on the first observed OOD day; stays
    tripped through the rearm..k band. Uses RAW residuals; bias is the
    winsorized-EWM estimate (separation per Fable fragile-point #3)."""
    if not sigma or len(residuals) < 5:
        return False
    for r in reversed(residuals):
        z = abs(r - bias)
        if z < rearm * sigma:
            return False
        if z > k * sigma:
            return True
    return False
