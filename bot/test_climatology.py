import weather as w
import climatology as c


def test_md_delta_simple():
    assert c.md_delta("07-26", "07-19") == 7
    assert c.md_delta("07-19", "07-26") == -7


def test_md_delta_year_wrap():
    assert c.md_delta("01-02", "12-30") == 3
    assert c.md_delta("12-30", "01-02") == -3
    assert c.md_delta("01-01", "12-31") == 1


def test_md_delta_zero():
    assert c.md_delta("07-19", "07-19") == 0


def _buck(question):
    return w.parse_bucket(question)


def test_clim_probs_counts_window_history():
    # 5 days within ±7 of 07-19 (07-15..07-23), 5 outside. Bucket "30°C or higher".
    hist = {
        "2016-07-15": 31.0, "2017-07-17": 29.5, "2018-07-19": 30.0,
        "2019-07-21": 28.0, "2020-07-23": 32.0,
        "2021-07-10": 35.0, "2022-08-01": 33.0,  # outside window
    }
    c._HIST = {"Seoul": hist}
    b = _buck("Will the highest temperature in Seoul be 30°C or higher on July 19?")
    p = c.clim_probs("Seoul", "2026-07-19", [b])
    # window has 5 days, 3 are >=30 → 0.6
    assert abs(p[b.key] - 0.6) < 1e-9


def test_clim_probs_empty_history_returns_zero():
    c._HIST = {}
    b = _buck("Will the highest temperature in Seoul be 30°C or higher on July 19?")
    p = c.clim_probs("Seoul", "2026-07-19", [b])
    assert p[b.key] == 0.0


def test_clim_probs_window_boundary_inclusive():
    # 07-12 and 07-26 are exactly ±7 of 07-19 → included; 07-11 is ±8 → excluded.
    hist = {"2016-07-12": 30.0, "2017-07-26": 30.0, "2018-07-11": 30.0}
    c._HIST = {"Ankara": hist}
    b = _buck("Will the highest temperature in Ankara be 30°C or higher on July 19?")
    p = c.clim_probs("Ankara", "2026-07-19", [b])
    assert abs(p[b.key] - 1.0) < 1e-9


def test_clim_probs_skips_none_and_feb29():
    hist = {"2016-07-19": None, "2020-02-29": 30.0, "2017-07-19": 31.0}
    c._HIST = {"Tokyo": hist}
    b = _buck("Will the highest temperature in Tokyo be 30°C or higher on July 19?")
    p = c.clim_probs("Tokyo", "2026-07-19", [b])
    # Feb 29 is outside ±7 of 07-19 anyway; None skipped; 1 valid day, 1 hit.
    assert abs(p[b.key] - 1.0) < 1e-9


def test_blend_clim_linear_pool():
    assert abs(c.blend_clim(0.50, 0.90, 0.30) - 0.62) < 1e-9


def test_blend_clim_none_returns_model():
    assert c.blend_clim(0.50, None, 0.30) == 0.50


def test_blend_clim_alpha_zero_returns_model():
    assert abs(c.blend_clim(0.50, 0.90, 0.0) - 0.50) < 1e-9


def test_blend_clim_alpha_one_returns_clim():
    assert abs(c.blend_clim(0.50, 0.90, 1.0) - 0.90) < 1e-9
