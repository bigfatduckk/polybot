"""Dashboard self-checks (stdlib, no framework): ro-conn read-only, clamp, endpoints, secret-grep.
Run: python test_dash.py   (also discoverable by pytest as test_*)."""
import os
import sqlite3
import tempfile

import dash


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO meta VALUES ('bankroll','1000')")
    conn.commit()
    conn.close()


def test_ro_conn_is_readonly():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "t.db")
    _make_db(p)
    conn = dash.ro_conn(p)
    row = conn.execute("SELECT v FROM meta WHERE k='bankroll'").fetchone()
    assert row["v"] == "1000"
    try:
        conn.execute("INSERT INTO meta VALUES ('x','y')")
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("ro_conn allowed a WRITE — mode=ro not enforced")
    conn.close()


def test_clamp_int_bounds_and_default():
    assert dash.clamp_int(None, 1, 365, 30) == 30
    assert dash.clamp_int("7", 1, 365, 30) == 7
    assert dash.clamp_int("999", 1, 365, 30) == 365
    assert dash.clamp_int("0", 1, 365, 30) == 1
    assert dash.clamp_int("abc", 1, 365, 30) == 30


def test_now_iso_is_utc():
    s = dash.now_iso()
    assert s.endswith("+00:00")


def _seed_health_dbs(d):
    import sqlite3 as sq
    # paper A: bankroll meta
    a = sq.connect(os.path.join(d, "a.db"))
    a.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
    a.execute("CREATE TABLE scans (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, job TEXT, mode TEXT, note TEXT, snapshot_json TEXT)")
    a.execute("INSERT INTO meta VALUES ('bankroll','1000')")
    a.execute("INSERT INTO scans(ts,job,mode,note,snapshot_json) VALUES ('2026-07-22T10:00:00+00:00','weather','paper','ok','{}')")
    a.commit(); a.close()
    # live: balances + halts + ticks
    lpath = os.path.join(d, "live.db")
    l = sq.connect(lpath)
    for stmt in [
        "CREATE TABLE live_balances (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, usdc REAL, matic REAL, source TEXT)",
        "CREATE TABLE live_halts (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, reason TEXT)",
        "CREATE TABLE live_ticks (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, job TEXT, note TEXT, detail_json TEXT)",
        "CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)",
    ]:
        l.execute(stmt)
    l.execute("INSERT INTO live_balances(ts,usdc,matic,source) VALUES('2026-07-22T10:00:00+00:00',200.0,135.6,'rpc')")
    l.execute("INSERT INTO live_halts(ts,reason) VALUES('2026-07-22T09:00:00+00:00','telegram:set')")
    l.execute("INSERT INTO live_ticks(ts,job,note,detail_json) VALUES('2026-07-22T10:00:00+00:00','maintain','ok','{}')")
    l.commit(); l.close()
    return os.path.join(d, "a.db"), os.path.join(d, "a.db"), lpath  # A=B=a.db for the fixture


def test_api_health():
    d = tempfile.mkdtemp()
    a, b, live = _seed_health_dbs(d)
    dash.PAPER_A_DB = a
    dash.PAPER_B_DB = b
    dash.LIVE_DB = live
    client = dash.app.test_client()
    r = client.get("/api/health")
    assert r.status_code == 200
    j = r.get_json()
    assert "ts" in j and "instances" in j
    assert "LIVE" in j["instances"]
    li = j["instances"]["LIVE"]
    assert li["bankroll"] == 200.0
    assert li["gas"] == 135.6
    assert li["pusd"] == 200.0
    assert li["halted"] is True  # live_halts latest row = telegram:set
    assert "A" in j["instances"] and j["instances"]["A"]["bankroll"] == 1000.0
    for inst in ("A", "B", "LIVE"):
        assert j["instances"][inst]["last_tick_age"] is not None, f"{inst} last_tick_age was None"


def _seed_pnl_dbs(d):
    import sqlite3 as sq
    a = sq.connect(os.path.join(d, "a.db"))
    for s in [
        "CREATE TABLE settlements (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, market_id TEXT, condition_id TEXT, city TEXT, date TEXT, observed_high REAL, bucket_key TEXT, resolved_yes INTEGER, pnl REAL, snapshot_json TEXT)",
        "CREATE TABLE pm_settlements (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, edge TEXT, market_id TEXT, condition_id TEXT, resolved_yes INTEGER, pnl REAL, meta_json TEXT)",
    ]:
        a.execute(s)
    # weather: day1 +2.0, day1 +3.0 (HKT). ts UTC -> HKT day.
    a.execute("INSERT INTO settlements(ts,pnl,resolved_yes) VALUES('2026-07-21T16:00:00+00:00',2.0,1)")  # HKT 07-22 00:00
    a.execute("INSERT INTO settlements(ts,pnl,resolved_yes) VALUES('2026-07-22T10:00:00+00:00',3.0,1)")  # HKT 07-22 18:00
    a.execute("INSERT INTO pm_settlements(ts,edge,pnl,resolved_yes) VALUES('2026-07-22T10:00:00+00:00','flb',-1.0,0)")
    a.commit(); a.close()
    b = sq.connect(os.path.join(d, "b.db"))
    b.execute("CREATE TABLE settlements (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, market_id TEXT, condition_id TEXT, city TEXT, date TEXT, observed_high REAL, bucket_key TEXT, resolved_yes INTEGER, pnl REAL, snapshot_json TEXT)")
    b.execute("INSERT INTO settlements(ts,pnl,resolved_yes) VALUES('2026-07-22T10:00:00+00:00',5.0,1)")
    b.commit(); b.close()
    l = sq.connect(os.path.join(d, "live.db"))
    l.execute("CREATE TABLE live_settlements (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, market_id TEXT, condition_id TEXT, city TEXT, date TEXT, bucket_key TEXT, resolved_yes INTEGER, pnl REAL, raw_json TEXT)")
    l.execute("INSERT INTO live_settlements(ts,pnl,resolved_yes) VALUES('2026-07-22T10:00:00+00:00',-2.0,0)")
    l.commit(); l.close()
    return os.path.join(d, "a.db"), os.path.join(d, "b.db"), os.path.join(d, "live.db")


def test_api_equity_and_daily():
    d = tempfile.mkdtemp()
    a, b, live = _seed_pnl_dbs(d)
    dash.PAPER_A_DB, dash.PAPER_B_DB, dash.LIVE_DB = a, b, live
    c = dash.app.test_client()
    # A realized = weather 2+3 + flb -1 = 4.0
    je = c.get("/api/equity?days=30").get_json()
    assert "A" in je["series"]
    assert je["series"]["A"][-1]["cum"] == 4.0  # hand-computed
    assert je["series"]["B"][-1]["cum"] == 5.0
    assert je["series"]["LIVE"][-1]["cum"] == -2.0
    jd = c.get("/api/daily-pnl?days=30").get_json()
    # A has two HKT days; sum of daily == 4.0
    a_days = {x["day"]: x["pnl"] for x in jd["series"]["A"]}
    assert abs(sum(a_days.values()) - 4.0) < 1e-9


def test_api_edge_pnl_and_winrate():
    d = tempfile.mkdtemp()
    a, b, live = _seed_pnl_dbs(d)
    dash.PAPER_A_DB, dash.PAPER_B_DB, dash.LIVE_DB = a, b, live
    c = dash.app.test_client()
    ep = c.get("/api/edge-pnl").get_json()
    by = {(e["edge"], e["instance"]): e["pnl"] for e in ep["edges"]}
    assert by[("weather", "A")] == 5.0      # A weather 2+3
    assert by[("weather", "B")] == 5.0      # B weather 5.0
    assert by[("flb", "A")] == -1.0
    assert by[("live", "LIVE")] == -2.0
    wr = c.get("/api/winrate").get_json()
    wby = {e["edge"]: e for e in wr["edges"]}
    assert wby["flb"]["won"] == 0 and wby["flb"]["total"] == 1
    assert wby["weather"]["won"] == 2 and wby["weather"]["total"] == 2


def test_api_equity_window_filters_old_rows():
    d = tempfile.mkdtemp()
    a, b, live = _seed_pnl_dbs(d)
    dash.PAPER_A_DB, dash.PAPER_B_DB, dash.LIVE_DB = a, b, live
    import sqlite3 as sq
    c = sq.connect(a)
    c.execute("INSERT INTO settlements(ts,pnl,resolved_yes) VALUES('2026-01-01T00:00:00+00:00',100.0,1)")
    c.commit(); c.close()
    j30 = dash.app.test_client().get("/api/equity?days=30").get_json()
    assert j30["series"]["A"][-1]["cum"] == 4.0, "past row leaked into 30-day window"
    j365 = dash.app.test_client().get("/api/equity?days=365").get_json()
    assert j365["series"]["A"][-1]["cum"] == 104.0, "past row missing from 365-day window"


def test_api_drawdown_sign():
    d = tempfile.mkdtemp()
    a, b, live = _seed_pnl_dbs(d)
    dash.PAPER_A_DB, dash.PAPER_B_DB, dash.LIVE_DB = a, b, live
    dd = dash.app.test_client().get("/api/drawdown?days=30").get_json()["series"]
    # LIVE single point cum -2.0, cummax starts 0.0 -> drawdown +2.0 (below peak)
    assert dd["LIVE"][-1]["dd"] == 2.0, f"LIVE drawdown sign wrong: {dd['LIVE'][-1]['dd']}"
    # A one day cum 4.0 -> at peak, drawdown 0.0
    assert dd["A"][-1]["dd"] == 0.0, f"A drawdown should be 0 at peak: {dd['A'][-1]['dd']}"


def _seed_pipeline_dbs(d):
    import sqlite3 as sq
    a = sq.connect(os.path.join(d, "a.db"))
    for s in [
        "CREATE TABLE candidates (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, scan_id INTEGER, market_id TEXT, condition_id TEXT, side TEXT, p_model REAL, p_by_model_json TEXT, edge_after_costs REAL, lead_hours REAL, run_ids TEXT, bucket_key TEXT, effective_price REAL, blend_mean REAL, market_mid REAL, snapshot_json TEXT)",
        "CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, scan_id INTEGER, market_id TEXT, token_id TEXT, side TEXT, price REAL, size REAL, maker_or_taker TEXT, edge REAL, kelly_fraction REAL, status TEXT, city TEXT, market_date TEXT, snapshot_json TEXT)",
        "CREATE TABLE fills (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, order_id INTEGER, market_id TEXT, token_id TEXT, side TEXT, price REAL, size REAL, maker_or_taker TEXT, fill_ts TEXT, pnl REAL, snapshot_json TEXT)",
        "CREATE TABLE pm_candidates (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, edge TEXT, scan_id INTEGER, market_id TEXT, side TEXT, p_model REAL, edge_after_costs REAL, effective_price REAL, lead_hours REAL, horizon_days REAL, meta_json TEXT)",
        "CREATE TABLE pm_orders (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, edge TEXT, scan_id INTEGER, market_id TEXT, token_id TEXT, side TEXT, price REAL, size REAL, maker_or_taker TEXT, edge_size REAL, kelly_fraction REAL, status TEXT, meta_json TEXT)",
    ]:
        a.execute(s)
    a.execute("INSERT INTO candidates(ts,market_id,side,p_model,edge_after_costs) VALUES('2026-07-22T10:00:00+00:00','mktA','YES',0.7,0.08)")
    a.execute("INSERT INTO candidates(ts,market_id,side,p_model,edge_after_costs) VALUES('2026-07-22T10:00:00+00:00','mktB','NO',0.3,0.02)")
    a.execute("INSERT INTO pm_candidates(ts,edge,market_id,side,p_model,edge_after_costs) VALUES('2026-07-22T10:00:00+00:00','flb','mktF','YES',0.6,0.05)")
    a.execute("INSERT INTO orders(ts,market_id,side,status) VALUES('2026-07-22T10:00:00+00:00','mktA','YES','open')")
    a.execute("CREATE TABLE pm_fills (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, edge TEXT, order_id INTEGER, market_id TEXT, token_id TEXT, side TEXT, price REAL, size REAL, maker_or_taker TEXT, fill_ts TEXT, pnl REAL, meta_json TEXT)")
    a.execute("INSERT INTO pm_fills(ts,edge,market_id) VALUES('2026-07-22T10:00:00+00:00','flb','mktF')")
    a.commit(); a.close()
    l = sq.connect(os.path.join(d, "live.db"))
    for s in [
        "CREATE TABLE live_ticks (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, job TEXT, note TEXT, detail_json TEXT)",
        "CREATE TABLE live_orders (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, candidate_id INTEGER, market_id TEXT, condition_id TEXT, exec_token_id TEXT, city TEXT, market_date TEXT, bucket_key TEXT, signal_side TEXT, exec_side TEXT, price REAL, size REAL, notional REAL, edge_at_exec REAL, kelly_fraction REAL, neg_risk INTEGER, dry_run INTEGER, clob_order_id TEXT, status TEXT, raw_json TEXT)",
        "CREATE TABLE live_fills (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, order_id INTEGER, clob_trade_id TEXT, market_id TEXT, exec_token_id TEXT, side TEXT, price REAL, size REAL, fee REAL, fill_ts TEXT, raw_json TEXT)",
    ]:
        l.execute(s)
    l.execute("INSERT INTO live_ticks(ts,job,note,detail_json) VALUES('2026-07-22T10:00:00+00:00','weather','skip:low_edge','{}')")
    l.execute("INSERT INTO live_ticks(ts,job,note,detail_json) VALUES('2026-07-22T10:01:00+00:00','weather','posted','{}')")
    l.execute("INSERT INTO live_orders(ts,market_id,signal_side,price,size,status,dry_run) VALUES('2026-07-22T10:01:00+00:00','mktL','YES',0.55,10,'posted',1)")
    l.commit(); l.close()
    return os.path.join(d, "a.db"), os.path.join(d, "live.db")


def test_api_feed_positions_candidates():
    d = tempfile.mkdtemp()
    a, live = _seed_pipeline_dbs(d)
    dash.PAPER_A_DB = a; dash.PAPER_B_DB = a; dash.LIVE_DB = live
    c = dash.app.test_client()
    feed = c.get("/api/feed?limit=10").get_json()["rows"]
    assert any(r["note"] == "posted" for r in feed)
    pos = c.get("/api/positions").get_json()["rows"]
    assert any(r["market_id"] == "mktL" for r in pos)  # live posted order is open
    cand = c.get("/api/candidates?limit=10").get_json()["rows"]
    assert len(cand) == 3  # 2 weather + 1 flb
    # mktA became an order; mktB did not
    conv = {r["market_id"]: r.get("became_order") for r in cand}
    assert conv.get("mktA") is True
    assert conv.get("mktB") is False


def test_api_rejections_edgedist_funnel():
    d = tempfile.mkdtemp()
    a, live = _seed_pipeline_dbs(d)
    dash.PAPER_A_DB = a; dash.PAPER_B_DB = a; dash.LIVE_DB = live
    c = dash.app.test_client()
    rej = c.get("/api/rejections?hours=24").get_json()["rows"]
    by = {r["reason"]: r["count"] for r in rej}
    assert by.get("skip:low_edge") == 1
    ed = c.get("/api/edge-dist?days=7").get_json()["buckets"]
    assert sum(b["count"] for b in ed) == 3  # 3 candidates total
    fn = c.get("/api/funnel?days=7").get_json()["stages"]
    sby = {s["edge"]: s for s in fn}
    assert sby["weather"]["candidates"] == 2 and sby["weather"]["orders"] == 1
    assert sby["flb"]["fills"] == 1, "flb funnel fills stage should count the seeded pm_fill"


def _seed_risk_calib_station(d):
    import sqlite3 as sq
    l = sq.connect(os.path.join(d, "live.db"))
    for s in [
        "CREATE TABLE live_orders (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, candidate_id INTEGER, market_id TEXT, condition_id TEXT, exec_token_id TEXT, city TEXT, market_date TEXT, bucket_key TEXT, signal_side TEXT, exec_side TEXT, price REAL, size REAL, notional REAL, edge_at_exec REAL, kelly_fraction REAL, neg_risk INTEGER, dry_run INTEGER, clob_order_id TEXT, status TEXT, raw_json TEXT)",
        "CREATE TABLE live_settlements (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, market_id TEXT, condition_id TEXT, city TEXT, date TEXT, bucket_key TEXT, resolved_yes INTEGER, pnl REAL, raw_json TEXT)",
        "CREATE TABLE live_halts (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, reason TEXT)",
        "CREATE TABLE live_ticks (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, job TEXT, note TEXT, detail_json TEXT)",
        "CREATE TABLE live_balances (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, usdc REAL, matic REAL, source TEXT)",
        "CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)",
    ]:
        l.execute(s)
    l.execute("INSERT INTO live_orders(ts,status,dry_run) VALUES('2026-07-22T10:00:00+00:00','posted',1)")
    l.execute("INSERT INTO live_settlements(ts,pnl,resolved_yes) VALUES('2026-07-22T09:00:00+00:00',-1.0,0)")
    l.execute("INSERT INTO live_settlements(ts,pnl,resolved_yes) VALUES('2026-07-22T09:30:00+00:00',-2.0,0)")
    l.execute("INSERT INTO live_halts(ts,reason) VALUES('2026-07-22T10:00:00+00:00','telegram:set')")
    l.execute("INSERT INTO live_ticks(ts,job,note,detail_json) VALUES('2026-07-22T10:00:00+00:00','maintain','ok','{}')")
    l.execute("INSERT INTO live_balances(ts,usdc,matic,source) VALUES('2026-07-22T10:00:00+00:00',200.0,135.6,'rpc')")
    l.commit(); l.close()
    a = sq.connect(os.path.join(d, "a.db"))
    for s in [
        "CREATE TABLE calib_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, edge TEXT, brier_model REAL, brier_market REAL, reliability_maxdev_pp REAL, n_signals INTEGER, gate_pass INTEGER, detail_json TEXT)",
        "CREATE TABLE station_obs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, city TEXT, date TEXT, observed_high REAL, blend_mean REAL, residual REAL, source TEXT, snapshot_json TEXT)",
        "CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)",
        "CREATE TABLE scans (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, job TEXT, mode TEXT, note TEXT, snapshot_json TEXT)",
        "CREATE TABLE settlements (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, market_id TEXT, condition_id TEXT, city TEXT, date TEXT, observed_high REAL, bucket_key TEXT, resolved_yes INTEGER, pnl REAL, snapshot_json TEXT)",
    ]:
        a.execute(s)
    a.execute("INSERT INTO settlements(ts,pnl,resolved_yes) VALUES('2026-07-22T10:00:00+00:00',5.0,1)")  # weather row so /api/edge-pnl is non-empty in degraded test
    a.execute("INSERT INTO calib_snapshots(ts,edge,brier_model,brier_market,reliability_maxdev_pp,n_signals,gate_pass,detail_json) VALUES('2026-07-22T06:30:00+00:00','weather',0.17,0.24,25.0,658,0,'{}')")
    a.execute("INSERT INTO calib_snapshots(ts,edge,brier_model,brier_market,reliability_maxdev_pp,n_signals,gate_pass,detail_json) VALUES('2026-07-21T06:30:00+00:00','weather',0.18,0.25,26.0,650,0,'{}')")
    a.execute("INSERT INTO station_obs(ts,city,residual) VALUES('2026-07-22T10:00:00+00:00','HongKong',0.3)")
    a.execute("INSERT INTO station_obs(ts,city,residual) VALUES('2026-07-22T10:00:00+00:00','Macau',-0.2)")
    a.execute("INSERT INTO meta VALUES ('bankroll','1000')")
    a.commit(); a.close()
    return os.path.join(d, "a.db"), os.path.join(d, "a.db"), os.path.join(d, "live.db")


def test_api_risk_calib_stationbias():
    d = tempfile.mkdtemp()
    a, b, live = _seed_risk_calib_station(d)
    dash.PAPER_A_DB, dash.PAPER_B_DB, dash.LIVE_DB = a, b, live
    c = dash.app.test_client()
    r = c.get("/api/risk").get_json()
    assert r["open_positions"] == 1
    assert r["max_open"] == 5
    assert r["consec_loss"] == 2          # last 2 settlements both losses
    assert r["max_consec"] == 6
    assert r["daily_loss_halt"] == 20.0   # 0.10 * 200
    assert r["halted"] is True
    cal = c.get("/api/calib").get_json()
    assert cal["latest"]["edge"] == "weather"
    assert cal["latest"]["brier_model"] == 0.17
    assert len(cal["series"]) == 2
    sb = c.get("/api/station-bias?days=30").get_json()
    cities = {x["city"] for x in sb["cities"]}
    assert "HongKong" in cities and "Macau" in cities


def test_api_state_aggregate():
    d = tempfile.mkdtemp()
    a, b, live = _seed_risk_calib_station(d)
    dash.PAPER_A_DB, dash.PAPER_B_DB, dash.LIVE_DB = a, b, live
    c = dash.app.test_client()
    s = c.get("/api/state").get_json()
    for k in ("health", "feed", "positions", "risk"):
        assert k in s
    assert s["risk"]["halted"] is True


_SECRET_PATTERNS = [
    "0x" + "a" * 40,  # 64-hex private key shape (lowercase); real key would match
    "sk-ant-",
    "POLY_PRIVATE_KEY",
    "TELEGRAM_TOKEN",
    "seed",
    "167.172.42.135",  # proxy URL
]


def test_no_secrets_in_payloads():
    d = tempfile.mkdtemp()
    a, b, live = _seed_risk_calib_station(d)  # reuse a full fixture
    dash.PAPER_A_DB, dash.PAPER_B_DB, dash.LIVE_DB = a, b, live
    c = dash.app.test_client()
    for ep in ["/api/state", "/api/health", "/api/equity", "/api/daily-pnl",
               "/api/drawdown", "/api/edge-pnl", "/api/winrate", "/api/feed",
               "/api/positions", "/api/candidates", "/api/rejections",
               "/api/edge-dist", "/api/funnel", "/api/risk", "/api/calib",
               "/api/station-bias"]:
        body = c.get(ep).get_data(as_text=True)
        for pat in _SECRET_PATTERNS:
            assert pat not in body, f"secret pattern {pat!r} leaked in {ep}"


def test_degraded_live_db_missing():
    d = tempfile.mkdtemp()
    a, b, _ = _seed_risk_calib_station(d)
    dash.PAPER_A_DB, dash.PAPER_B_DB = a, b
    dash.LIVE_DB = "/nonexistent/live.db"  # unreachable
    c = dash.app.test_client()
    h = c.get("/api/health").get_json()
    assert h["instances"]["LIVE"]["halted"] is False  # no halts row -> not halted, not a crash
    # paper panels still work
    assert c.get("/api/edge-pnl").get_json()["edges"]  # paper A weather row present
    # risk endpoint survives (open_positions 0, halted False)
    r = c.get("/api/risk").get_json()
    assert r["open_positions"] == 0 and r["halted"] is False


def test_redact_truncates_long_ids():
    d = tempfile.mkdtemp()
    a, live = _seed_pipeline_dbs(d)
    dash.PAPER_A_DB, dash.PAPER_B_DB, dash.LIVE_DB = a, a, live
    import sqlite3 as sq
    c = sq.connect(a)
    long_id = "0x" + "a" * 40  # 42 chars, > 12 -> _redact returns s[:8] + "…"
    c.execute("INSERT INTO candidates(ts,market_id,side,p_model,edge_after_costs,market_mid) "
              "VALUES('2026-07-22T10:00:00+00:00',?,'YES',0.7,0.08,0.5)", (long_id,))
    c.execute("INSERT INTO orders(ts,market_id,side,status) "
              "VALUES('2026-07-22T10:00:00+00:00',?,'YES','open')", (long_id,))
    c.commit(); c.close()
    cand = dash.app.test_client().get("/api/candidates?limit=50").get_json()["rows"]
    assert any(r["market_id"] == long_id[:8] + "…" for r in cand), "candidate market_id not redacted"
    pos = dash.app.test_client().get("/api/positions").get_json()["rows"]
    assert any(r["market_id"] == long_id[:8] + "…" for r in pos), "position market_id not redacted"


def test_selfcheck_exit_zero():
    """The __main__ self-check itself must pass (ponytail: one runnable check left behind)."""
    import subprocess, sys
    env = dict(os.environ, DASH_SUBPROCESS="1")
    r = subprocess.run([sys.executable, "test_dash.py"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, f"self-check failed:\n{r.stdout}\n{r.stderr}"


def test_headless_smoke():
    """Reuse the DocuSmart pattern: edge --headless --dump-dom loads the page,
    12 cards + 8 canvases present. Skipped if edge isn't on PATH."""
    import shutil, subprocess, sys
    if not shutil.which("edge"):
        print("skip test_headless_smoke (edge not found)")
        return
    d = tempfile.mkdtemp()
    a, b, live = _seed_risk_calib_station(d)
    env = dict(os.environ, PAPER_A_DB=a, PAPER_B_DB=b, LIVE_DB=live)
    server = subprocess.Popen([sys.executable, "-c",
        "import dash;dash.app.run(host='127.0.0.1',port=8766)"], env=env)
    try:
        out = subprocess.run(["edge","--headless","--dump-dom","http://127.0.0.1:8766/"],
                             capture_output=True, text=True, timeout=30).stdout
        assert out.count("card ") >= 12
        assert out.count("<canvas") >= 8
    finally:
        server.terminate()


if __name__ == "__main__":
    fns = [v for k, v in sorted(vars(dash).items()) if k.startswith("test_")]
    fns += [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    if os.environ.get("DASH_SUBPROCESS"):
        fns = [fn for fn in fns if fn.__name__ != "test_selfcheck_exit_zero"]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{'OK' if not failed else failed} failed" if failed else "\nOK — all self-checks passed")
    raise SystemExit(1 if failed else 0)
