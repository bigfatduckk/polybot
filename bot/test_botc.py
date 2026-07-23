"""Bot C OOD-robustness self-checks: winsorized-EWM bias + stateless-hysteresis
gate. Run: python -m pytest bot/test_botc.py -q"""
import weather as w


def test_station_bias_plain_mean_when_not_c(monkeypatch):
    monkeypatch.setattr(w, "_C", False)
    assert abs(w.station_bias([0.3, 0.4, 0.4, 0.4, 0.5]) - 0.4) < 1e-9


def test_station_bias_deadband_below_min(monkeypatch):
    monkeypatch.setattr(w, "_C", False)
    assert w.station_bias([0.1, 0.1, 0.1, 0.1, 0.1]) == 0.0


def test_station_bias_min_count_5(monkeypatch):
    monkeypatch.setattr(w, "_C", False)
    assert w.station_bias([0.5, 0.5, 0.5, 0.5]) == 0.0


def test_station_bias_c_ewm_resists_ood_spike(monkeypatch):
    # plain mean of [0.4]*19 + [5.0] = 0.63 — spike drags it up. Winsorize
    # clips the 5.0 to the median (MAD=0 → band = [0.4,0.4]) so EWM stays 0.4.
    monkeypatch.setattr(w, "_C", True)
    b = w.station_bias([0.4] * 19 + [5.0])
    assert abs(b - 0.4) < 1e-9, f"OOD spike dragged bias to {b}"


def test_station_bias_c_ewm_constant_window(monkeypatch):
    monkeypatch.setattr(w, "_C", True)
    assert abs(w.station_bias([0.5] * 20) - 0.5) < 1e-9


def test_winsorize_clips_outlier():
    xs = [-1, -0.5, 0, 0.5, 1, 100.0]
    assert max(w._winsorize(xs)) < 100.0  # 100 outlier clipped into the 3×MAD band


def test_ewm_constants_and_recency():
    assert abs(w._ewm([3.0] * 10) - 3.0) < 1e-9
    # newest-weighted: a 10 in the last slot outranks a 10 in the first slot
    assert w._ewm([0.0] * 9 + [10.0]) > w._ewm([10.0] + [0.0] * 9)


def test_ood_tripped_fires_on_extreme_newest():
    assert w.ood_tripped([0.0] * 19 + [5.0], bias=0.0, sigma=1.0) is True


def test_ood_tripped_quiet_on_calm():
    assert w.ood_tripped([0.1] * 20, bias=0.0, sigma=1.0) is False


def test_ood_tripped_hysteresis_stays_tripped_in_band():
    # day-1 5σ (trip), day-2 newest 3σ (between rearm 2.5 and k 4) → still tripped
    assert w.ood_tripped([0.0, 0.0, 0.0, 5.0, 3.0], bias=0.0, sigma=1.0) is True


def test_ood_tripped_rearms_below_threshold():
    # newest 2σ (< 2.5σ rearm) → not tripped even if an older point was 5σ
    assert w.ood_tripped([0.0, 0.0, 0.0, 5.0, 2.0], bias=0.0, sigma=1.0) is False


def test_ood_tripped_min_count_guard():
    assert w.ood_tripped([5.0, 5.0, 5.0, 5.0], bias=0.0, sigma=1.0) is False


def test_ood_tripped_no_sigma_no_trip():
    assert w.ood_tripped([5.0] * 20, bias=0.0, sigma=0.0) is False
