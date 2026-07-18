import math
import random

from calib import calibrate_p, fit_platt


def _sig(x):
    return 1.0 / (1.0 + math.exp(-x))


def test_calibrate_identity_when_no_params():
    assert calibrate_p(0.3, None) == 0.3
    assert calibrate_p(0.3, {}) == 0.3
    assert calibrate_p(0.3, {"a": None}) == 0.3


def test_calibrate_monotonic():
    params = {"a": 1.5, "b": 0.2}
    ps = [0.01, 0.1, 0.25, 0.4, 0.55, 0.7, 0.9, 0.99]
    cals = [calibrate_p(p, params) for p in ps]
    for i in range(len(cals) - 1):
        assert cals[i] < cals[i + 1], (ps[i], cals[i], ps[i + 1], cals[i + 1])


def test_calibrate_handles_zero_and_one():
    params = {"a": 2.0, "b": -0.1}
    lo = calibrate_p(0.0, params)
    hi = calibrate_p(1.0, params)
    assert 0.0 < lo < hi < 1.0


def test_fit_recovers_a_monotonic_remapping():
    # ponytail: generate (p_raw, outcome) where the true mapping is a sigmoid,
    # fit Platt, assert it re-calibrates roughly toward the truth.
    rng = random.Random(7)
    a_true, b_true = 1.6, -0.3
    pairs = []
    for _ in range(800):
        p_raw = rng.random()
        p_true = _sig(a_true * math.log(p_raw / (1 - p_raw)) + b_true)
        y = 1 if rng.random() < p_true else 0
        pairs.append((p_raw, y))
    params = fit_platt([p for p, _ in pairs], [y for _, y in pairs])
    assert params is not None
    # fitted mapping should move a mid-range raw prob toward the true curve
    p_raw = 0.3
    p_true = _sig(a_true * math.log(p_raw / (1 - p_raw)) + b_true)
    p_cal = calibrate_p(p_raw, params)
    assert abs(p_cal - p_true) < 0.05, (p_cal, p_true)


def test_fit_returns_none_on_tiny_n():
    assert fit_platt([0.1, 0.2, 0.3], [0, 1, 0]) is None


def test_fit_improves_reliability_on_miscalibrated():
    # model systematically says p=0.25 on bets that actually resolve YES 50%
    rng = random.Random(11)
    pairs = []
    for _ in range(400):
        pairs.append((0.25, 1 if rng.random() < 0.50 else 0))
    for _ in range(400):
        pairs.append((0.75, 1 if rng.random() < 0.90 else 0))
    params = fit_platt([p for p, _ in pairs], [y for _, y in pairs])
    # raw 0.25 should calibrate toward ~0.50; raw 0.75 toward ~0.90
    c25 = calibrate_p(0.25, params)
    c75 = calibrate_p(0.75, params)
    assert c25 > 0.40, c25
    assert c75 > 0.80, c75
