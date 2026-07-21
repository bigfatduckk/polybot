"""Executor + settle behavior tests. All SDK/httpx mocked (I4). Proves:
dry-run signs-never-posts, sell→BUY NO, missing key fails safe, stale cancel,
partial reconcile, settlement pnl == settle._fill_pnl."""
import sqlite3
from datetime import datetime, timedelta, timezone

import live_engine as le
import live_executor as lx
import live_settle as ls
import markets
import settle
import engine
from config import LIVE_DRY_RUN_ENV


class FakeClient:
    def __init__(self, book=None, no_token="no1", open_orders=None, trades=None,
                 tick="0.01", neg=False):
        self._book = book or _yes_book()
        self._no_token = no_token
        self._open_orders = open_orders or []
        self._trades = trades or []
        self._tick = tick
        self._neg = neg
        self.create_calls = 0
        self.post_calls = 0
        self.cancel_calls = 0

    def set_api_creds(self, c): pass
    def create_or_derive_api_key(self):
        return type("C", (), {"api_key": "k", "api_secret": "s", "api_passphrase": "p"})()
    def create_order(self, args, options=None):
        self.create_calls += 1
        self.last_args = args
        return {"signed": True}
    def post_order(self, order, order_type="GTC", post_only=False, defer_exec=False):
        self.post_calls += 1
        return {"orderID": "clob-xyz", "status": "matched"}
    def get_open_orders(self, params=None, only_first_page=False, next_cursor=None):
        return self._open_orders
    def get_trades(self, params=None, only_first_page=False, next_cursor=None):
        return self._trades
    def cancel_order(self, payload): self.cancel_calls += 1
    def cancel_all(self): pass
    def get_tick_size(self, token_id): return self._tick
    def get_neg_risk(self, token_id): return self._neg


def _yes_book(ask=0.50, ask_size=100, bid=0.45, bid_size=100):
    return {"bids": [{"price": bid, "size": bid_size}],
            "asks": [{"price": ask, "size": ask_size}],
            "tick_size": 0.01, "min_order_size": 5, "neg_risk": False,
            "last_trade_price": 0.48}


def _no_book(ask=0.60, ask_size=100):
    return {"bids": [{"price": 0.55, "size": 100}],
            "asks": [{"price": ask, "size": ask_size}],
            "tick_size": 0.01, "min_order_size": 5, "neg_risk": False,
            "last_trade_price": 0.58}


def _setup(tmp_path, monkeypatch):
    paper = tmp_path / "paper.db"
    live = tmp_path / "live.db"
    monkeypatch.setattr(le, "PAPER_DB_PATH", str(paper))
    monkeypatch.setattr(le, "LIVE_DB_PATH", str(live))
    monkeypatch.setattr(engine, "DB_PATH", str(paper))
    monkeypatch.setattr(engine, "notify", lambda *a, **k: None)
    monkeypatch.setattr(markets, "fetch_book", lambda tid: _yes_book())
    engine.init_db()
    le.init_live_db()
    return paper, live


def _sig(side="buy", p=0.60, market_id="m1", city="Seoul", mdate="2026-07-20",
         yes_tok="yes1"):
    return le.LiveSignal(
        candidate_id=1, market_id=market_id, condition_id="c1", side=side, p_model=p,
        edge_after_costs=0.10, effective_price=0.50, city=city, market_date=mdate,
        bucket_key="30C", yes_token_id=yes_tok, fee_rate=0.0, fees_enabled=False,
        neg_risk=False, ts="2026-07-20T10:00:00+00:00",
    )


def _seed_order(conn, market_id="m1", clob_id="clob1", status="posted",
                side="buy", price=0.50, size=20.0, exec_tok="yes1", ts=None):
    conn.execute(
        f"INSERT INTO live_orders({lx._LO_COLS}) VALUES({lx._LO_PH})",
        (ts or le._now_iso(), 1, market_id, "c1", exec_tok, "Seoul", "2026-07-20",
         "30C", side, "BUY", price, size, price * size, 0.10, 0.2, 0, 0, clob_id, status, "{}"))
    conn.commit()


def test_dry_run_signs_never_posts(tmp_path, monkeypatch):
    _, live = _setup(tmp_path, monkeypatch)
    monkeypatch.delenv(LIVE_DRY_RUN_ENV, raising=False)
    lc = le.get_live_db()
    prepared = lx.prepare_order(_sig(p=0.60), FakeClient(), lc)
    assert prepared is not None
    spec, opts = prepared
    fake = FakeClient()
    status, _ = lx.submit(spec, opts, fake, lc, dry_run=True)
    assert status == "dry_run"
    assert fake.create_calls == 1 and fake.post_calls == 0
    row = sqlite3.connect(str(live)).execute(
        "SELECT status, dry_run FROM live_orders WHERE id=1").fetchone()
    assert row[0] == "dry_run" and row[1] == 1


def test_live_post_signs_and_posts(tmp_path, monkeypatch):
    _, live = _setup(tmp_path, monkeypatch)
    lc = le.get_live_db()
    prepared = lx.prepare_order(_sig(p=0.60), FakeClient(), lc)
    spec, opts = prepared
    fake = FakeClient()
    status, clob = lx.submit(spec, opts, fake, lc, dry_run=False)
    assert status == "posted" and fake.post_calls == 1 and clob == "clob-xyz"
    row = sqlite3.connect(str(live)).execute(
        "SELECT status, clob_order_id FROM live_orders WHERE id=1").fetchone()
    assert row[0] == "posted" and row[1] == "clob-xyz"


def test_sell_signal_buys_no_token(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(lx, "resolve_no_token", lambda mid: "no_token_X")
    monkeypatch.setattr(markets, "fetch_book", lambda tid: _no_book(ask=0.60))
    lc = le.get_live_db()
    prepared = lx.prepare_order(_sig(side="sell", p=0.30), FakeClient(), lc)
    assert prepared is not None
    spec, _ = prepared
    assert spec.exec_token_id == "no_token_X"
    assert spec.signal.side == "sell"
    assert spec.price == 0.60


def test_walk_book_reduces_size_on_thin_book(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    # thin but above CLOB min (5): fillable 8 < target 20 → size cut to 8, not skipped
    monkeypatch.setattr(markets, "fetch_book", lambda tid: _yes_book(ask=0.50, ask_size=8))
    lc = le.get_live_db()
    prepared = lx.prepare_order(_sig(p=0.60), FakeClient(), lc)
    assert prepared is not None
    spec, _ = prepared
    assert spec.size == 8.0


def test_skip_when_edge_below_min_at_walked_price(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(markets, "fetch_book", lambda tid: _yes_book(ask=0.58, ask_size=100))
    lc = le.get_live_db()
    prepared = lx.prepare_order(_sig(p=0.60), FakeClient(), lc)  # edge 0.02 < 0.08
    assert prepared is None
    n = lc.execute(
        "SELECT COUNT(*) FROM live_ticks WHERE note='skip:edge_below_min_at_exec'"
    ).fetchone()[0]
    assert n == 1


def test_missing_key_fails_safe(tmp_path, monkeypatch):
    _, live = _setup(tmp_path, monkeypatch)
    monkeypatch.delenv("POLY_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("POLY_FUNDER", raising=False)
    notified = []
    monkeypatch.setattr(engine, "notify", lambda t: notified.append(t))
    import run_live
    run_live.job_weather_live()
    rows = sqlite3.connect(str(live)).execute("SELECT COUNT(*) FROM live_orders").fetchone()[0]
    assert rows == 0
    assert any("not configured" in t for t in notified)


def test_missing_key_notifies_once_per_day(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.delenv("POLY_PRIVATE_KEY", raising=False)
    notified = []
    monkeypatch.setattr(engine, "notify", lambda t: notified.append(t))
    import run_live
    run_live.job_weather_live()
    run_live.job_weather_live()
    run_live.job_weather_live()
    assert sum("not configured" in t for t in notified) == 1


def test_stale_order_cancelled_after_90min(tmp_path, monkeypatch):
    _, live = _setup(tmp_path, monkeypatch)
    lc = le.get_live_db()
    old = (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat()
    _seed_order(lc, clob_id="clob1", status="open", ts=old)
    fake = FakeClient()
    n = ls.cancel_stale(fake, lc)
    assert n == 1 and fake.cancel_calls == 1
    row = sqlite3.connect(str(live)).execute(
        "SELECT status FROM live_orders WHERE id=1").fetchone()
    assert row[0] == "cancelled"


def test_reconcile_fills_partial(tmp_path, monkeypatch):
    _, live = _setup(tmp_path, monkeypatch)
    lc = le.get_live_db()
    _seed_order(lc, clob_id="clob1", status="posted")
    fake = FakeClient(
        open_orders=[{"id": "clob1", "size_matched": 10, "original_size": 20}],
        trades=[{"id": "t1", "taker_order_id": "clob1", "market": "m1",
                 "asset_id": "yes1", "price": 0.50, "size": 10.0,
                 "fee_rate_bps": 0, "match_time": "2026-07-20T11:00:00Z"}],
    )
    n = ls.reconcile_fills(fake, lc)
    assert n == 1
    rows = sqlite3.connect(str(live)).execute(
        "SELECT side, price, size FROM live_fills WHERE order_id=1").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "buy" and abs(rows[0][1] - 0.50) < 1e-9 and abs(rows[0][2] - 10.0) < 1e-9
    status = sqlite3.connect(str(live)).execute(
        "SELECT status FROM live_orders WHERE id=1").fetchone()[0]
    assert status == "partial"


def test_reconcile_sell_fill_stored_yes_space(tmp_path, monkeypatch):
    _, live = _setup(tmp_path, monkeypatch)
    lc = le.get_live_db()
    _seed_order(lc, clob_id="clob2", status="posted", side="sell", price=0.60,
                size=16.0, exec_tok="no1")
    fake = FakeClient(
        open_orders=[],
        trades=[{"id": "t2", "taker_order_id": "clob2", "market": "m1",
                 "asset_id": "no1", "price": 0.60, "size": 16.0,
                 "fee_rate_bps": 0, "match_time": "2026-07-20T11:00:00Z"}],
    )
    ls.reconcile_fills(fake, lc)
    row = sqlite3.connect(str(live)).execute(
        "SELECT side, price FROM live_fills WHERE order_id=1").fetchone()
    assert row[0] == "sell"
    assert abs(row[1] - (1.0 - 0.60)) < 1e-9


def test_live_settlement_equals_fill_pnl(tmp_path, monkeypatch):
    _, live = _setup(tmp_path, monkeypatch)
    cases = [
        ("buy", 0.40, 10.0, True),
        ("buy", 0.40, 10.0, False),
        ("sell", 0.40, 10.0, True),
        ("sell", 0.40, 10.0, False),
    ]
    for i, (side, price, size, yes_won) in enumerate(cases):
        lc = le.get_live_db()
        mid = f"m{i}"
        _seed_order(lc, market_id=mid, clob_id="", status="filled", exec_tok="t")
        lc.execute(
            f"INSERT INTO live_fills({ls._LF_COLS}) VALUES({ls._LF_PH})",
            (le._now_iso(), 1, "tr" + mid, mid, "t", side, price, size, 0.0,
             "2026-07-20T11:00:00Z", "{}"))
        lc.commit()
        outcome = "yes" if yes_won else "no"
        monkeypatch.setattr(markets, "fetch_resolution",
                            lambda mid, o=outcome: (True, o, [1.0, 0.0]))
        ls.settle_resolved(lc)
        row = sqlite3.connect(str(live)).execute(
            "SELECT pnl FROM live_settlements WHERE market_id=?", (mid,)).fetchone()
        assert row is not None
        expected = settle._fill_pnl(side, price, size, yes_won)
        assert abs(row[0] - expected) < 1e-9, f"{side} yes_won={yes_won}: {row[0]} != {expected}"


def test_notify_prefix_a_live_passes_unmodified(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(engine, "notify", lambda t: sent.append(t))
    engine.notify("[A-LIVE] test message")
    assert sent == ["[A-LIVE] test message"]


def test_pnl_live_format_reads_live_db_only(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    conn = engine.get_db()
    conn.execute("INSERT INTO scans(ts, job, mode, note, snapshot_json) VALUES(?,?,?,?,?)",
                 ("2026-07-20T10:00:00+00:00", "weather", "paper", "seed", "{}"))
    conn.commit(); conn.close()
    import live_positions as lp
    out = lp.format_live_pnl()
    assert out.startswith("[A-LIVE]")
    assert "not initialized" in out or "no open" in out


def test_rpc_result_matches_by_id_and_detects_errors():
    # order-preserving batch: id:1=MATIC, id:2=USDC — must match by id, not index
    batch = [{"id": 1, "result": "0x1bc16d674ec80000"},  # 2e18 / 1e18 = 2.0 POL
             {"id": 2, "result": "0xbebc2000"}]          # 2e9 / 1e6 = 2000 USDC
    assert lx._rpc_result(batch, 1) == "0x1bc16d674ec80000"
    assert lx._rpc_result(batch, 2) == "0xbebc2000"
    # reversed order (JSON-RPC doesn't guarantee order) — still correct by id
    rev = list(reversed(batch))
    assert lx._rpc_result(rev, 1) == "0x1bc16d674ec80000"
    # error response → None (was the false-positive root cause: silently 0)
    assert lx._rpc_result([{"id": 1, "error": {"code": -32051}}], 1) is None
    # non-list (HTTP error body like {"error": "tenant disabled"}) → None
    assert lx._rpc_result({"error": "tenant disabled"}, 1) is None
    # missing id → None
    assert lx._rpc_result([{"id": 1, "result": "0x0"}], 2) is None
    # bare "0x" (malformed empty result, no error key — publicnode flakiness) → None not 0.0
    assert lx._rpc_result([{"id": 1, "result": "0x"}], 1) is None


def _fake_clob_client(monkeypatch, *, derive_raises, derive_returns="derived", create_returns="created"):
    """Inject a fake py_clob_client_v2.ClobClient recording derive/create calls."""
    calls = {"derive": 0, "create": 0}
    class FakeCreds:
        def __init__(self, tag): self.tag = tag
    class FakeClient:
        def __init__(self, **kw): pass
        def set_api_creds(self, c): self._creds = c
        def derive_api_key(self):
            calls["derive"] += 1
            if derive_raises: raise RuntimeError("no key")
            return FakeCreds(derive_returns)
        def create_api_key(self):
            calls["create"] += 1
            return FakeCreds(create_returns)
    import py_clob_client_v2
    monkeypatch.setattr(py_clob_client_v2, "ClobClient", FakeClient)
    return calls, FakeClient


def test_get_client_derives_existing_key_without_noisy_create(monkeypatch):
    # steady state: derive succeeds → create must NOT be called (avoids the 400)
    monkeypatch.setenv("POLY_PRIVATE_KEY", "0x" + "1" * 64)
    monkeypatch.setenv("POLY_FUNDER", "0x" + "2" * 40)
    monkeypatch.setenv("POLY_SIG_TYPE", "2")
    calls, _ = _fake_clob_client(monkeypatch, derive_raises=False, derive_returns="derived")
    client = lx.get_client()
    assert calls == {"derive": 1, "create": 0}
    assert client._creds.tag == "derived"


def test_get_client_falls_back_to_create_when_no_key(monkeypatch):
    # first run: derive raises → fall back to POST create
    monkeypatch.setenv("POLY_PRIVATE_KEY", "0x" + "1" * 64)
    monkeypatch.setenv("POLY_FUNDER", "0x" + "2" * 40)
    monkeypatch.setenv("POLY_SIG_TYPE", "2")
    calls, _ = _fake_clob_client(monkeypatch, derive_raises=True, create_returns="created")
    client = lx.get_client()
    assert calls == {"derive": 1, "create": 1}
    assert client._creds.tag == "created"
