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
