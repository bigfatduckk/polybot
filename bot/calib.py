"""Probability recalibration (Platt scaling) for the weather model.

p_calib = sigmoid(a * logit(p_raw) + b), 2 params fit by pure-stdlib IRLS
(no numpy/scipy/sklearn). Two params can't overfit on thin per-decile
samples (n=32-79), which is why Platt is chosen over isotonic. Tradeoff:
a monotonic sigmoid cannot fix genuinely non-monotonic miscalibration;
it smooths through and reduces max deviation. Escalate to isotonic if
Platt leaves residual >15pp.
"""
import json
import math
from pathlib import Path

EPS = 1e-4


def _sigmoid(z):
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _logit(p, eps=EPS):
    p = min(max(p, eps), 1.0 - eps)
    return math.log(p / (1.0 - p))


def calibrate_p(p, params):
    if not params or params.get("a") is None:
        return p
    return _sigmoid(params["a"] * _logit(p, params.get("eps", EPS)) + params["b"])


def fit_platt(p_raw, outcomes, n_iter=100, tol=1e-9):
    # 2-param logistic regression: outcome ~ sigmoid(a*z + b), z=logit(p_raw).
    # ponytail: full 2x2 Newton (IRLS), ~30 lines, no deps.
    n = len(p_raw)
    if n < 10:
        return None
    z = [_logit(float(p)) for p in p_raw]
    y = [float(o) for o in outcomes]
    a, b = 1.0, 0.0
    for _ in range(n_iter):
        ga = gb = 0.0
        Haa = Hab = Hbb = 0.0
        for i in range(n):
            pi = _sigmoid(a * z[i] + b)
            r = y[i] - pi
            w = pi * (1.0 - pi)
            ga += r * z[i]
            gb += r
            Haa += w * z[i] * z[i]
            Hab += w * z[i]
            Hbb += w
        det = Haa * Hbb - Hab * Hab
        if det < 1e-12:
            break
        da = (Hbb * ga - Hab * gb) / det
        db = (-Hab * ga + Haa * gb) / det
        a += da
        b += db
        if abs(da) < tol and abs(db) < tol:
            break
    return {"a": a, "b": b, "eps": EPS, "method": "platt_logit", "n": n}


def load_calib(path):
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None
