"""D1 self-checks. No network, no real orders. Mocked CLOB client (real SDK
types for order args — exercises the BUY-NO/GTD/post_only/expiration path that
go-live will use). Run: python test_d1.py"""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone

# Allow running from repo root or bot/
sys.path.insert(0, str(Path(__file__).resolve().parent))

import d1

# D1 posts BUY-NO; submit resolves the NO token via Gamma. Tests have no
# network, so stub resolve_no_token to a fixed NO token id.
d1.resolve_no_token = lambda mid: "0xNO"


class FakeClient:
    """Records create_order/post_order calls. post_order returns a fake orderID.
    Mirrors the real ClobClient surface d1.submit touches."""
    def __init__(self):
        self.created = []
        self.posted = []
        self._open = []
        self._trades = []

    def create_order(self, args, opts):
        self.created.append((args, opts))
        return {"signed": True, "token_id": args.token_id, "side": args.side}

    def post_order(self, order, order_type="GTC", post_only=False, defer_exec=False):
        self.posted.append((order, order_type, post_only, defer_exec))
        return {"orderID": "fake-clob-id-123"}

    def get_open_orders(self):
        return self._open

    def get_trades(self):
        return self._trades


def _sig(cid=1, mid=0.115, gap=0.065, min_sz=5, tick=0.001, fee_rate=0.04,
         yes_tok="0xYES", neg_risk=False, p_model=0.05, eff=0.115):
    return d1.D1Signal(
        candidate_id=cid, market_id="mkt%d" % cid, condition_id="cond%d" % cid,
        side="sell", p_model=p_model, edge_after_costs=0.07,
        effective_price=eff, scan_mid=mid, gap=gap, yes_token_id=yes_tok,
        tick_size=tick, min_order_size=min_sz, fee_rate=fee_rate,
        fees_enabled=True, neg_risk=neg_risk, ts="2026-07-26T06:30:00+00:00",
    )


def test_prepare_order_ask_level_and_size():
    """ask = scan_mid + gap/2 rounded to tick; size = venue min; cap enforced."""
    sig = _sig(mid=0.115, gap=0.065, min_sz=5, tick=0.001)
    spec = d1.prepare_order(sig)
    assert spec is not None, "expected a spec"
    # 0.115 + 0.0325 = 0.1475, tick 0.001 → 0.148 (round-half-to-even may vary
    # by platform; accept either 0.147 or 0.148)
    assert abs(spec.ask_price - 0.1475) < 0.002, spec.ask_price
    assert spec.size == 5.0, spec.size
    assert spec.max_loss == 5.0 * (1.0 - spec.ask_price), spec.max_loss
    assert spec.max_loss <= d1.D1_CAP, spec.max_loss
    assert spec.expiration > int(datetime.now(timezone.utc).timestamp()), spec.expiration


def test_prepare_order_skip_when_cap_exceeded():
    """A huge min_order_size at a low ask → max_loss > $25 → skip (None)."""
    sig = _sig(mid=0.10, gap=0.05, min_sz=300, tick=0.001)  # ask~0.125, 300 shares
    spec = d1.prepare_order(sig)
    assert spec is None, "should skip: 300*(1-0.125)=$262.5 > $25 cap"


def test_prepare_order_skip_degenerate_ask():
    """ask rounding to >=1 or <=0 → skip."""
    sig = _sig(mid=0.98, gap=0.05, tick=0.001)  # ask = 1.005 → >1
    assert d1.prepare_order(sig) is None


def test_read_new_signals_cursor_and_sell_filter():
    """Only FLB sell candidates past the cursor are returned; cursor advances
    past all rows seen (skipped or not)."""
    tmp = tempfile.mkdtemp()
    pdb = os.path.join(tmp, "paper.db")
    ddb = os.path.join(tmp, "d1.db")
    d1.D1_DB_PATH = ddb
    d1.init_d1_db()
    conn = sqlite3.connect(pdb)
    conn.executescript(
        """
        CREATE TABLE pm_candidates (id INTEGER PRIMARY KEY, ts TEXT, edge TEXT,
          scan_id INTEGER, market_id TEXT, side TEXT, p_model REAL,
          edge_after_costs REAL, effective_price REAL, lead_hours REAL,
          horizon_days REAL, meta_json TEXT);
        CREATE TABLE pm_snapshots (id INTEGER PRIMARY KEY, ts TEXT, edge TEXT,
          market_id TEXT, condition_id TEXT, yes_token_id TEXT, tick_size REAL,
          min_order_size REAL, fee_rate REAL, fees_enabled INTEGER, neg_risk INTEGER);
        """)
    now = "2026-07-26T06:30:00+00:00"
    conn.execute(
        "INSERT INTO pm_candidates(id,ts,edge,scan_id,market_id,side,p_model,"
        "edge_after_costs,effective_price,lead_hours,horizon_days,meta_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, now, "flb", 1, "m1", "sell", 0.05, 0.07, 0.115, 24, 30,
         json.dumps({"best_bid": 0.10, "best_ask": 0.13})))
    conn.execute(
        "INSERT INTO pm_candidates(id,ts,edge,scan_id,market_id,side,p_model,"
        "edge_after_costs,effective_price,lead_hours,horizon_days,meta_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (2, now, "flb", 1, "m2", "buy", 0.95, 0.08, 0.92, 24, 30,
         json.dumps({"best_bid": 0.90, "best_ask": 0.93})))  # buy → skipped by D1
    conn.execute(
        "INSERT INTO pm_snapshots(ts,edge,market_id,condition_id,yes_token_id,"
        "tick_size,min_order_size,fee_rate,fees_enabled,neg_risk) "
        "VALUES('ts','flb','m1','c1','0xYES1',0.001,5,0.04,1,0)")
    conn.commit()
    # read-only handle like paper_ro_conn
    import urllib.parse
    uri = Path(pdb).resolve().as_uri() + "?mode=ro"
    ro = sqlite3.connect(uri, uri=True)
    ro.row_factory = sqlite3.Row
    d1c = d1.get_d1_db()
    # First run: cursor unset → seeds to current max id, returns empty (no replay).
    sigs0, ev0 = d1.read_new_signals(ro, d1c)
    assert ev0 == 0 and sigs0 == [], "first run must seed cursor, return empty"
    # Now add a NEW sell candidate past the seeded cursor.
    conn.execute(
        "INSERT INTO pm_candidates(id,ts,edge,scan_id,market_id,side,p_model,"
        "edge_after_costs,effective_price,lead_hours,horizon_days,meta_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (3, now, "flb", 2, "m3", "sell", 0.05, 0.07, 0.115, 24, 30,
         json.dumps({"best_bid": 0.10, "best_ask": 0.13})))
    conn.execute(
        "INSERT INTO pm_snapshots(ts,edge,market_id,condition_id,yes_token_id,"
        "tick_size,min_order_size,fee_rate,fees_enabled,neg_risk) "
        "VALUES('ts','flb','m3','c3','0xYES3',0.001,5,0.04,1,0)")
    conn.commit()
    sigs, evaluated = d1.read_new_signals(ro, d1c)
    assert evaluated == 1, evaluated
    assert len(sigs) == 1, "only the new sell candidate passes"
    assert sigs[0].candidate_id == 3
    assert sigs[0].scan_mid == 0.115  # (0.10+0.13)/2
    # cursor advanced past id 3 → second read returns 0
    sigs2, ev2 = d1.read_new_signals(ro, d1c)
    assert ev2 == 0, ev2
    ro.close()
    d1c.close()
    conn.close()
    print("  read_new_signals: cursor + sell-filter OK")


def test_submit_dry_run_signs_never_posts():
    """Dry-run signs (create_order called) but post_order is NEVER called, and
    the order lands with status='dry_run'. This is the go-live safety invariant."""
    tmp = tempfile.mkdtemp()
    d1.D1_DB_PATH = os.path.join(tmp, "d1.db")
    d1.init_d1_db()
    conn = d1.get_d1_db()
    spec = d1.prepare_order(_sig())
    assert spec is not None
    fc = FakeClient()
    os.environ["D1_DRY_RUN"] = "1"  # dry-run ON
    status, err = d1.submit(spec, fc, conn, d1.is_dry_run())
    assert status == "dry_run", status
    assert err is None
    assert len(fc.created) == 1, "create_order must run (signs)"
    assert len(fc.posted) == 0, "post_order must NOT run in dry-run"
    args, opts = fc.created[0]
    assert args.side == "BUY", args.side
    assert args.token_id == "0xNO", args.token_id
    assert args.expiration == spec.expiration, "expiration threaded into signed order"
    row = conn.execute("SELECT status, dry_run FROM d1_orders ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "dry_run"
    assert row["dry_run"] == 1
    conn.close()
    print("  submit dry-run: signs, never posts, BUY-NO+expiration OK")


def test_submit_live_posts_with_gtd_post_only():
    """Live (D1_DRY_RUN=0) calls post_order with order_type=GTD, post_only=True."""
    tmp = tempfile.mkdtemp()
    d1.D1_DB_PATH = os.path.join(tmp, "d1.db")
    d1.init_d1_db()
    conn = d1.get_d1_db()
    spec = d1.prepare_order(_sig())
    fc = FakeClient()
    os.environ["D1_DRY_RUN"] = "0"  # LIVE
    try:
        status, _ = d1.submit(spec, fc, conn, d1.is_dry_run())
        assert status == "posted", status
        assert len(fc.posted) == 1
        _order, otype, post_only, _defer = fc.posted[0]
        assert otype == "GTD", f"maker order must be GTD, got {otype}"
        assert post_only is True, "post_only must be True (maker guarantee)"
        row = conn.execute("SELECT status, dry_run, clob_order_id FROM d1_orders ORDER BY id DESC LIMIT 1").fetchone()
        assert row["status"] == "posted"
        assert row["dry_run"] == 0
        assert row["clob_order_id"] == "fake-clob-id-123"
    finally:
        os.environ["D1_DRY_RUN"] = "1"  # restore safe default
    conn.close()
    print("  submit live: GTD + post_only=True, status=posted OK")


def test_get_client_uses_d1_env_not_shared():
    """D1 reads POLY_PRIVATE_KEY_D1 / POLY_FUNDER_D1, NOT the shared
    POLY_PRIVATE_KEY. Structural isolation: with the shared key set but the D1
    key unset, get_client must raise D1KeyMissing — it cannot fall back to the
    07-24-exposed live-arm key. No SDK, no network (raises before the import)."""
    os.environ["POLY_PRIVATE_KEY"] = "0x" + "d" * 64
    os.environ["POLY_FUNDER"] = "0x" + "f" * 40
    os.environ.pop("POLY_PRIVATE_KEY_D1", None)
    os.environ.pop("POLY_FUNDER_D1", None)
    raised = False
    try:
        d1.get_client()
    except d1.D1KeyMissing:
        raised = True
    except Exception as e:
        # any other exception is fine too (e.g. SDK trying the unset key) — the
        # point is it did NOT silently use the shared key. But D1KeyMissing is
        # the expected, structural path.
        print(f"  (note: got {type(e).__name__}, expected D1KeyMissing)")
    finally:
        os.environ.pop("POLY_PRIVATE_KEY", None)
        os.environ.pop("POLY_FUNDER", None)
    assert raised, "D1 must raise D1KeyMissing when POLY_PRIVATE_KEY_D1 is unset, even if POLY_PRIVATE_KEY is set"
    print("  get_client: refuses shared key, requires POLY_PRIVATE_KEY_D1 OK")


def test_settle_maker_fee_zero_plus_rebate():
    """Maker fills settle with fee_rate=0 (maker pays no fee) and a rebate is
    computed as pool_pct * size * fee_rate * p*(1-p) — sole-maker upper bound."""
    tmp = tempfile.mkdtemp()
    d1.D1_DB_PATH = os.path.join(tmp, "d1.db")
    d1.init_d1_db()
    conn = d1.get_d1_db()
    # one posted order + one fill (sell-YES 5 shares @ 0.148)
    conn.execute(
        "INSERT INTO d1_orders(ts,candidate_id,market_id,condition_id,yes_token_id,"
        "signal_side,exec_side,ask_price,size,notional,max_loss,edge_at_scan,p_model,"
        "scan_mid,gap,tick_size,neg_risk,fee_rate,expiration,dry_run,clob_order_id,"
        "status,raw_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ts", 1, "mkt1", "cond1", "0xYES", "sell", "SELL", 0.148, 5.0, 0.74, 4.26,
         0.07, 0.05, 0.115, 0.065, 0.001, 0, 0.04, 0, 0, "clob1", "filled", "{}"))
    conn.execute(
        "INSERT INTO d1_fills(ts,order_id,clob_trade_id,market_id,yes_token_id,side,"
        "price,size,fee,fill_ts,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("ts", 1, "t1", "mkt1", "0xYES", "sell", 0.148, 5.0, 0.0, "ts", "{}"))
    conn.commit()
    # monkeypatch fetch_resolution → closed, NO won (longshot lost → sell-YES wins)
    import markets
    orig = markets.fetch_resolution
    markets.fetch_resolution = lambda mid: (True, "no", [0.0, 1.0])
    try:
        n = d1.settle_resolved(conn)
        assert n == 1, n
        row = conn.execute(
            "SELECT resolved_yes, pnl, rebate FROM d1_settlements WHERE market_id='mkt1'"
        ).fetchone()
        assert row["resolved_yes"] == 0  # NO won
        # sell-YES at 0.148, NO won → pnl = (price-1)*size if yes_won else price*size
        # NO won → gross = price*size = 0.148*5 = 0.74; fee=0 (maker) → pnl=0.74
        assert abs(row["pnl"] - 0.74) < 1e-9, row["pnl"]
        # rebate = 0.25 * 5 * 0.04 * 0.148 * 0.852 = 0.25*5*0.04*0.126096 = 0.0063048
        assert abs(row["rebate"] - 0.25 * 5 * 0.04 * 0.148 * 0.852) < 1e-9, row["rebate"]
    finally:
        markets.fetch_resolution = orig
    conn.close()
    print("  settle: maker fee=0, pnl=0.74, rebate computed OK")


def main():
    tests = [
        test_prepare_order_ask_level_and_size,
        test_prepare_order_skip_when_cap_exceeded,
        test_prepare_order_skip_degenerate_ask,
        test_read_new_signals_cursor_and_sell_filter,
        test_submit_dry_run_signs_never_posts,
        test_submit_live_posts_with_gtd_post_only,
        test_get_client_uses_d1_env_not_shared,
        test_settle_maker_fee_zero_plus_rebate,
    ]
    failed = 0
    for t in tests:
        try:
            print(f"[D1] {t.__name__}")
            t()
            print(f"  PASS")
        except Exception as e:
            failed += 1
            import traceback
            print(f"  FAIL: {e}")
            traceback.print_exc()
    print(f"\n[D1] {len(tests)-failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
