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


if __name__ == "__main__":
    fns = [v for k, v in sorted(vars(dash).items()) if k.startswith("test_")]
    fns += [v for k, v in sorted(globals().items()) if k.startswith("test_")]
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
