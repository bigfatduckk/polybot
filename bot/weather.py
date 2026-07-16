import hashlib
import json
import re
from dataclasses import dataclass, field

import httpx

from config import (
    CONSENSUS_MAX_DISAGREEMENT_C,
    MODEL_COL_SUFFIX,
    MODELS,
    OPEN_METEO_ENSEMBLE,
    STATION_BIAS_MIN,
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
    b = sum(last) / len(last)
    if abs(b) < STATION_BIAS_MIN:
        return 0.0
    return b
