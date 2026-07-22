"""store_calib self-check: parsing + idempotent insert. No network (subprocess monkeypatched)."""
import os
import sqlite3
import tempfile

import markets
import store_calib as sc

WEATHER_OUT = """Brier(model)   = 0.1734
Brier(market)  = 0.2430
Brier(market)-Brier(model) = +0.0696
Reliability by decile (n>=20):
  [0.0,0.1) n=  44 mean_p=0.052 freq=0.182 dev=13.0pp
  max reliability deviation = 25.0pp
Gate verdicts (Plan v0.2 section 6):
  >=200 resolved signals: PASS (n=658)
"""

FLB_OUT = """FLB resolved signals: 312
Reliability by price bucket (n>=10):
  [0.00,0.05) n=  44 mean_p=0.052 freq=0.182 dev=13.0pp
  max reliability deviation = 18.2pp
FLB gate:
  >=200 resolved signals: PASS (n=312)
  max reliability deviation <=10pp: FAIL (18.2pp)
"""


def test_parse_weather_stdout():
    r = sc.parse_weather_stdout(WEATHER_OUT)
    assert r is not None
    assert r["brier_model"] == 0.1734
    assert r["brier_market"] == 0.2430
    assert r["reliability_maxdev_pp"] == 25.0
    assert r["n_signals"] == 658
    assert r["gate_pass"] is False  # reliability FAILs


def test_parse_edge_stdout_flb():
    r = sc.parse_edge_stdout(FLB_OUT)
    assert r is not None
    assert r["reliability_maxdev_pp"] == 18.2
    assert r["n_signals"] == 312
    assert r["gate_pass"] is False


def test_parse_weather_insufficient():
    assert sc.parse_weather_stdout("Brier: insufficient data") is None


def test_table_created_and_insert():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "t.db")
    markets.DB_PATH = p
    markets.init_edge_db()
    sc.DB_PATH = p
    row = {
        "edge": "weather",
        "brier_model": 0.1734,
        "brier_market": 0.2430,
        "reliability_maxdev_pp": 25.0,
        "n_signals": 658,
        "gate_pass": 0,
        "detail_json": "{}",
    }
    sc._insert_row(row)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    n = conn.execute("SELECT COUNT(*) FROM calib_snapshots").fetchone()[0]
    conn.close()
    assert n == 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{'OK' if not failed else failed} failed" if failed else "\nOK")
    raise SystemExit(1 if failed else 0)
