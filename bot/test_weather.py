import weather as w


def _run(members, date="2026-07-13", model="ecmwf_ifs025", city="Seoul"):
    return w.ModelRun(
        city=city, model=model, run_ts="",
        daily_high_by_date={date: list(members)}, content_hash="x",
    )


def test_load_residuals_chronological_order(tmp_path, monkeypatch):
    # _load_residuals must return the last 30 in ASCENDING id order so
    # station_bias's [-N:] takes the newest N. DESC here was the bug: [-N:]
    # took the oldest N of the window, lagging the bias across regime changes.
    import engine, config
    db = tmp_path / "e.db"
    monkeypatch.setattr(engine, "DB_PATH", str(db))
    monkeypatch.setattr(config, "DB_PATH", str(db))
    engine.init_db()
    conn = engine.get_db()
    for i in range(1, 31):
        conn.execute(
            "INSERT INTO station_obs(ts, city, date, observed_high, blend_mean, "
            "residual, source, snapshot_json) VALUES(?,?,?,?,?,?,?,?)",
            (engine.now_iso(), "Hong Kong", f"2026-01-{i:02d}", 30.0, 30.0,
             float(i), "test", "{}"))
    conn.commit(); conn.close()
    conn = engine.get_db()
    res = engine._load_residuals(conn, "Hong Kong")
    conn.close()
    assert res == [float(i) for i in range(1, 31)], res


def test_station_bias_uses_newest_20(tmp_path, monkeypatch):
    # 30 residuals: oldest 20 = -1.0, newest 10 = +5.0. Newest-20 mean (the
    # intent) = (10*-1 + 10*5)/20 = +2.0; the DESC bug used the oldest-20 = -1.0.
    import engine, config
    db = tmp_path / "e.db"
    monkeypatch.setattr(engine, "DB_PATH", str(db))
    monkeypatch.setattr(config, "DB_PATH", str(db))
    engine.init_db()
    conn = engine.get_db()
    for i in range(1, 31):
        val = -1.0 if i <= 20 else 5.0
        conn.execute(
            "INSERT INTO station_obs(ts, city, date, observed_high, blend_mean, "
            "residual, source, snapshot_json) VALUES(?,?,?,?,?,?,?,?)",
            (engine.now_iso(), "Hong Kong", f"2026-01-{i:02d}", 30.0, 30.0, val, "t", "{}"))
    conn.commit(); conn.close()
    conn = engine.get_db()
    res = engine._load_residuals(conn, "Hong Kong")
    conn.close()
    assert abs(w.station_bias(res) - 2.0) < 1e-9


def test_parse_bucket_exact_closed_low_open_high():
    b = w.parse_bucket("Will the highest temperature in Seoul be 30°C on July 13?")
    assert b.lo == 30.0 and b.lo_incl is True
    assert b.hi == 31.0 and b.hi_incl is False
    assert b.contains(30.0) is True
    assert b.contains(29.9999) is False
    assert b.contains(30.9999) is True
    assert b.contains(31.0) is False


def test_parse_bucket_or_higher_closed_low():
    b = w.parse_bucket("Will the highest temperature in Seoul be 34°C or higher on July 13?")
    assert b.lo == 34.0 and b.lo_incl is True and b.hi is None
    assert b.contains(34.0) is True
    assert b.contains(33.9999) is False
    assert b.contains(40.0) is True


def test_parse_bucket_or_below_open_high():
    b = w.parse_bucket("Will the highest temperature in Hong Kong be 27°C or below on July 13?")
    assert b.lo is None and b.hi == 28.0 and b.hi_incl is False
    assert b.contains(27.0) is True
    assert b.contains(27.9999) is True
    assert b.contains(28.0) is False


def test_parse_bucket_negative_temps():
    b = w.parse_bucket("Will the highest temperature in Helsinki be -3°C on July 13?")
    assert b.lo == -3.0 and b.hi == -2.0
    assert b.contains(-3.0) is True
    assert b.contains(-2.0) is False
    assert b.contains(-2.5) is True


def test_parse_bucket_fahrenheit_converted_to_celsius():
    b = w.parse_bucket("Will the highest temperature in Toronto be 90°F on July 13?")
    expected = (90 - 32) * 5 / 9
    assert abs(b.lo - expected) < 1e-9
    assert b.hi == expected + 1.0
    assert b.contains(expected) is True


def test_parse_bucket_returns_none_on_unparseable():
    assert w.parse_bucket("Kraken IPO in 2025?") is None
    assert w.parse_bucket("Will it rain tomorrow?") is None


def test_member_fraction_probs():
    members = [27.0, 28.5, 30.2, 30.8, 31.0, 31.5, 34.0, 35.5]
    run = _run(members)
    b30 = w.parse_bucket("Will the highest temperature in Seoul be 30°C on July 13?")
    b31 = w.parse_bucket("Will the highest temperature in Seoul be 31°C on July 13?")
    b34 = w.parse_bucket("Will the highest temperature in Seoul be 34°C or higher on July 13?")
    b27 = w.parse_bucket("Will the highest temperature in Seoul be 27°C or below on July 13?")
    p = w.daily_high_probs(run, "2026-07-13", [b30, b31, b34, b27], 0.0)
    n = len(members)
    assert abs(p["30°C"] - 2 / n) < 1e-9
    assert abs(p["31°C"] - 2 / n) < 1e-9
    assert abs(p["34°C or higher"] - 2 / n) < 1e-9
    assert abs(p["27°C or below"] - 1 / n) < 1e-9
    assert abs(sum(p.values()) - 7 / n) < 1e-9


def test_bias_shifts_member_highs_additively():
    members = [30.4, 30.5]
    run = _run(members)
    b = w.parse_bucket("Will the highest temperature in Seoul be 31°C on July 13?")
    no_bias = w.daily_high_probs(run, "2026-07-13", [b], 0.0)
    with_bias = w.daily_high_probs(run, "2026-07-13", [b], 0.6)
    assert no_bias["31°C"] == 0.0
    assert with_bias["31°C"] == 1.0


def test_blend_weights():
    pa = {"30°C": 0.4, "31°C": 0.6}
    pb = {"30°C": 0.6, "31°C": 0.4}
    weights = {"ecmwf_ifs025": 0.5, "gfs025": 0.5}
    out = w.blend({"ecmwf_ifs025": pa, "gfs025": pb}, weights)
    assert abs(out["30°C"] - 0.5) < 1e-9
    assert abs(out["31°C"] - 0.5) < 1e-9


def test_blend_unbalanced_weights():
    pa = {"30°C": 0.2, "31°C": 0.8}
    pb = {"30°C": 0.8, "31°C": 0.2}
    out = w.blend({"ecmwf_ifs025": pa, "gfs025": pb}, {"ecmwf_ifs025": 0.75, "gfs025": 0.25})
    assert abs(out["30°C"] - 0.35) < 1e-9
    assert abs(out["31°C"] - 0.65) < 1e-9


def test_consensus_ok_within_threshold():
    a = _run([30.0, 31.0, 30.5], model="ecmwf_ifs025")
    b = _run([30.5, 31.2, 30.8], model="gfs025")
    assert w.consensus_ok([a, b], "2026-07-13") is True


def test_consensus_fails_when_disagreement_exceeds():
    a = _run([28.0, 29.0, 28.5], model="ecmwf_ifs025")
    b = _run([31.0, 32.0, 31.5], model="gfs025")
    assert w.consensus_ok([a, b], "2026-07-13") is False


def test_station_bias_mean_of_last_20():
    residuals = [1.0] * 25
    assert abs(w.station_bias(residuals) - 1.0) < 1e-9


def test_station_bias_zero_when_insufficient():
    assert w.station_bias([1.0, 2.0]) == 0.0
    assert w.station_bias([1.0] * 4) == 0.0


def test_station_bias_uses_only_last_20():
    residuals = [10.0] * 30 + [2.0] * 20
    assert abs(w.station_bias(residuals) - 2.0) < 1e-9


def test_station_bias_gated_below_threshold():
    assert w.station_bias([0.1] * 25) == 0.0
    assert w.station_bias([0.29] * 25) == 0.0
    assert abs(w.station_bias([0.30] * 25) - 0.30) < 1e-9
    assert abs(w.station_bias([-0.40] * 25) + 0.40) < 1e-9
