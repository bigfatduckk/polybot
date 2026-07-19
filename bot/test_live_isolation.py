"""CRITICAL isolation proof (I1): a full weather-live + maintain-live tick
leaves every paper-DB table's row count AND content hash identical. Also
proves: HALT_LIVE blocks weather-live, dry-run default, paper conn read-only.
All SDK + httpx mocked — no network."""
import hashlib
import json
import sqlite3

import engine
import live_engine as le
import live_executor as lx
import live_settle as ls
import live_positions as lp
import markets
import settle
from config import LIVE_DRY_RUN_ENV


class FakeClient:
    def __init__(self):
        self.create_calls = 0
        self.post_calls = 0
        self.cancel_calls = 0
    def set_api_creds(self, c): pass
    def create_or_derive_api_key(self):
        return type("C", (), {"api_key": "k", "api_secret": "s", "api_passphrase": "p"})()
    def create_order(self, args, options=None):
        self.create_calls += 1
        return {"signed": True}
    def post_order(self, order, order_type="GTC", post_only=False, defer_exec=False):
        self.post_calls += 1
        return {"orderID": "clob-1", "status": "matched"}
    def get_open_orders(self, params=None, only_first_page=False, next_cursor=None): return []
    def get_trades(self, params=None, only_first_page=False, next_cursor=None): return []
    def cancel_order(self, payload): self.cancel_calls += 1
    def cancel_all(self): pass
    def get_tick_size(self, t): return "0.01"
    def get_neg_risk(self, t): return False


def _yes_book():
    return {"bids": [{"price": 0.45, "size": 100}],
            "asks": [{"price": 0.50, "size": 100}],
            "tick_size": 0.01, "min_order_size": 5, "neg_risk": False,
            "last_trade_price": 0.48}


_SNAP_COLS = """ts, market_id, event_id, condition_id, question, city,
  market_date, bucket_key, bucket_json, end_date, best_bid, best_ask, bid_size,
  ask_size, depth, tick_size, min_order_size, fee_rate, fees_enabled, neg_risk,
  liquidity, last_trade_price, yes_token_id, snapshot_json"""
_SNAP_PH = ",".join(["?"] * 24)
_CAND_COLS = """ts, scan_id, market_id, condition_id, side, p_model,
  p_by_model_json, edge_after_costs, lead_hours, run_ids, bucket_key,
  effective_price, blend_mean, market_mid, snapshot_json"""
_CAND_PH = ",".join(["?"] * 15)


def _snapshot_db(path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    out = {}
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]
    for t in tables:
        rows = conn.execute(f"SELECT * FROM '{t}'").fetchall()
        data = json.dumps([dict(r) for r in rows], default=str, sort_keys=True)
        out[t] = (len(rows), hashlib.sha256(data.encode()).hexdigest())
    conn.close()
    return out


def _setup_iso(tmp_path, monkeypatch, halt=False):
    paper = tmp_path / "paper.db"
    live = tmp_path / "live.db"
    monkeypatch.setattr(le, "PAPER_DB_PATH", str(paper))
    monkeypatch.setattr(le, "LIVE_DB_PATH", str(live))
    monkeypatch.setattr(engine, "DB_PATH", str(paper))
    monkeypatch.setattr(engine, "notify", lambda *a, **k: None)
    engine.init_db()
    le.init_live_db()
    # seed one fresh, over-edge candidate (buy)
    conn = engine.get_db()
    conn.execute(f"INSERT INTO snapshots({_SNAP_COLS}) VALUES({_SNAP_PH})",
        ("2026-07-20T10:00:00+00:00", "m1", "e1", "c1", "q", "Seoul", "2026-07-20",
         "30C", "{}", "2026-07-21T00:00:00+00:00", 0.45, 0.55, 100, 100, 200, 0.01, 5.0,
         0.0, 0, 0, 1000.0, 0.50, "yes1", "{}"))
    conn.execute(f"INSERT INTO candidates({_CAND_COLS}) VALUES({_CAND_PH})",
        ("2026-07-20T10:00:00+00:00", 1, "m1", "c1", "buy", 0.60,
         json.dumps({"ecmwf_ifs025": 0.60}), 0.10, 24.0, "h", "30C", 0.50, 29.0, 0.50, "{}"))
    conn.commit()
    conn.close()
    # mock SDK + all httpx network paths
    monkeypatch.setattr(lx, "get_client", lambda: FakeClient())
    monkeypatch.setattr(markets, "fetch_book", lambda tid: _yes_book())
    monkeypatch.setattr(lx, "resolve_no_token", lambda mid: "no1")
    monkeypatch.setattr(markets, "fetch_resolution", lambda mid: (False, "none", []))
    monkeypatch.setattr(lx, "fetch_balances", lambda f: (200.0, 1.0))
    monkeypatch.delenv(LIVE_DRY_RUN_ENV, raising=False)
    if halt:
        (tmp_path / "HALT_LIVE").write_text("halt")
        monkeypatch.setattr("config.HALT_LIVE_FILE", str(tmp_path / "HALT_LIVE"))
    return paper, live


def test_paper_db_zero_new_rows_after_live_tick(tmp_path, monkeypatch):
    paper, live = _setup_iso(tmp_path, monkeypatch)
    before = _snapshot_db(paper)
    import run_live
    run_live.job_weather_live()
    ls.job_maintain_live()
    after = _snapshot_db(paper)
    assert before == after, f"paper DB mutated by live tick:\nbefore={before}\nafter={after}"
    # live DB received the dry-run order (proves live wrote to live, not paper)
    lc = sqlite3.connect(str(live))
    n = lc.execute("SELECT COUNT(*) FROM live_orders WHERE status='dry_run'").fetchone()[0]
    assert n == 1
    lc.close()


def test_paper_conn_is_readonly(tmp_path, monkeypatch):
    paper, _ = _setup_iso(tmp_path, monkeypatch)
    ro = le.paper_ro_conn()
    try:
        ro.execute("INSERT INTO scans(ts, job, mode, note, snapshot_json) VALUES(?,?,?,?,?)",
                   ("x", "weather", "paper", "leak", "{}"))
        ro.commit()
        raise AssertionError("write to read-only paper DB succeeded — I1 broken")
    except sqlite3.OperationalError:
        pass
    ro.close()


def test_halt_live_blocks_weather_live(tmp_path, monkeypatch):
    paper, live = _setup_iso(tmp_path, monkeypatch, halt=True)
    before = _snapshot_db(paper)
    import run_live
    run_live.job_weather_live()
    ls.job_maintain_live()   # HALT blocks both jobs
    after = _snapshot_db(paper)
    assert before == after
    lc = sqlite3.connect(str(live))
    n_orders = lc.execute("SELECT COUNT(*) FROM live_orders").fetchone()[0]
    n_ticks = lc.execute("SELECT COUNT(*) FROM live_ticks").fetchone()[0]
    assert n_orders == 0 and n_ticks == 0   # HALT = true no-op for both jobs
    lc.close()


def test_dry_run_signs_never_posts_in_full_tick(tmp_path, monkeypatch):
    paper, live = _setup_iso(tmp_path, monkeypatch)
    import run_live
    run_live.job_weather_live()
    lc = sqlite3.connect(str(live))
    row = lc.execute("SELECT dry_run, status FROM live_orders WHERE id=1").fetchone()
    assert row[1] == "dry_run" and row[0] == 1


def test_info_pnl_routing(tmp_path, monkeypatch):
    # pnl live → format_live_pnl reads only the live DB; bare pnl → format_pnl_both
    # (still importable + callable unchanged). The info.py edit lazy-imports
    # format_live_pnl inside the `pnl live` branch so a failure can't break the
    # info cron.
    _setup_iso(tmp_path, monkeypatch)
    monkeypatch.setattr(lp, "LIVE_DB_PATH", str(tmp_path / "nope.db"))
    out_live = lp.format_live_pnl()
    assert out_live.startswith("[A-LIVE]")
    import positions
    # bare pnl path unchanged: format_pnl_both is callable and returns a string
    assert callable(positions.format_pnl_both)
    assert isinstance(positions.format_pnl_both(), str)


def test_live_settlement_uses_fill_pnl_after_resolution(tmp_path, monkeypatch):
    paper, live = _setup_iso(tmp_path, monkeypatch)
    # seed a filled live order + fill, then resolve via Gamma → settlement pnl
    lc = le.get_live_db()
    lc.execute(
        f"INSERT INTO live_orders({lx._LO_COLS}) VALUES({lx._LO_PH})",
        (le._now_iso(), 1, "m1", "c1", "yes1", "Seoul", "2026-07-20", "30C",
         "buy", "BUY", 0.50, 20.0, 10.0, 0.10, 0.2, 0, 0, "clob1", "filled", "{}"))
    lc.execute(
        f"INSERT INTO live_fills({ls._LF_COLS}) VALUES({ls._LF_PH})",
        (le._now_iso(), 1, "t1", "m1", "yes1", "buy", 0.50, 20.0, 0.0,
         "2026-07-20T11:00:00Z", "{}"))
    lc.commit()
    monkeypatch.setattr(markets, "fetch_resolution",
                        lambda mid: (True, "yes", [1.0, 0.0]))
    monkeypatch.setattr(lx, "get_client", lambda: FakeClient())
    monkeypatch.setattr(lx, "fetch_balances", lambda f: (200.0, 1.0))
    ls.job_maintain_live()
    row = sqlite3.connect(str(live)).execute(
        "SELECT pnl, resolved_yes FROM live_settlements WHERE market_id='m1'").fetchone()
    assert row is not None
    assert row[1] == 1
    assert abs(row[0] - settle._fill_pnl("buy", 0.50, 20.0, True)) < 1e-9
