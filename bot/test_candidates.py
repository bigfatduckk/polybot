import json

import engine
import weather as w
from config import INST_TAG


def _snap(market_id="m1", bb=0.30, ba=0.40):
    return engine.MarketSnapshot(
        market_id=market_id, condition_id="c1", event_id="e1",
        question="q", city="Seoul", bucket=w.Bucket(key="30C", lo=30.0,
        lo_incl=True, hi=31.0, hi_incl=False), market_date="2026-07-19",
        end_date="2026-07-20T23:59:59+00:00", best_bid=bb, best_ask=ba,
        bid_size=100.0, ask_size=100.0, depth=200.0, tick_size=0.01,
        min_order_size=5.0, fee_rate=0.0, fees_enabled=False, neg_risk=False,
        liquidity=1000.0, last_trade_price=0.35, yes_token_id="t1", ts="2026-07-19T10:00:00+00:00",
    )


def _cand(snap, p_model=0.5, side="buy"):
    return engine.Candidate(
        snapshot=snap, side=side, p_model=p_model, p_by_model={"ecmwf_ifs025": p_model},
        edge_after_costs=0.1, lead_hours=24, run_ids="h", effective_price=0.40,
        blend_mean=29.0, raw={},
    )


def test_store_candidate_persists_market_mid(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(engine, "DB_PATH", str(db))
    engine.init_db()
    conn = engine.get_db()
    snap = _snap(bb=0.30, ba=0.50)
    engine._store_candidate(conn, 1, _cand(snap, p_model=0.55))
    conn.commit()
    row = conn.execute("SELECT market_mid FROM candidates WHERE market_id='m1'").fetchone()
    conn.close()
    assert row is not None
    assert abs(row["market_mid"] - 0.40) < 1e-9  # (0.30+0.50)/2


def test_resolved_signals_reads_market_mid_without_snapshots_join(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(engine, "DB_PATH", str(db))
    engine.init_db()
    conn = engine.get_db()
    snap = _snap(market_id="m2", bb=0.20, ba=0.40)
    engine._store_candidate(conn, 1, _cand(snap, p_model=0.55))
    # no snapshots row for m2 — analyze must still resolve market_mid from candidates
    conn.execute(
        "INSERT INTO settlements(ts, market_id, condition_id, city, date, observed_high, "
        "bucket_key, resolved_yes, pnl, snapshot_json) "
        "VALUES('2026-07-20T12:00:00+00:00','m2','c1','Seoul','2026-07-19',30.5,'30C',1,0.0,'{}')"
    )
    conn.commit()
    import analyze
    sigs = analyze._resolved_signals(conn)
    conn.close()
    assert len(sigs) == 1
    assert abs(sigs[0]["mid"] - 0.30) < 1e-9  # (0.20+0.40)/2
    assert abs(sigs[0]["p"] - 0.55) < 1e-9


def test_cull_if_due_deletes_old_snapshots_keeps_recent(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(engine, "DB_PATH", str(db))
    engine.init_db()
    conn = engine.get_db()
    # one old, one recent snapshot
    conn.execute(
        "INSERT INTO snapshots(ts, market_id, event_id, condition_id, question, city, "
        "market_date, bucket_key, bucket_json, end_date, best_bid, best_ask, bid_size, "
        "ask_size, depth, tick_size, min_order_size, fee_rate, fees_enabled, neg_risk, "
        "liquidity, last_trade_price, yes_token_id, snapshot_json) "
        "VALUES('2026-01-01T00:00:00+00:00','old','e','c','q','Seoul','2026-01-01','30C','{}','',"
        "0.2,0.4,1,1,1,0.01,1,0,0,0,1,0.3,'t','{}')")
    conn.execute(
        "INSERT INTO snapshots(ts, market_id, event_id, condition_id, question, city, "
        "market_date, bucket_key, bucket_json, end_date, best_bid, best_ask, bid_size, "
        "ask_size, depth, tick_size, min_order_size, fee_rate, fees_enabled, neg_risk, "
        "liquidity, last_trade_price, yes_token_id, snapshot_json) "
        "VALUES('2026-07-19T00:00:00+00:00','new','e','c','q','Seoul','2026-07-19','30C','{}','',"
        "0.2,0.4,1,1,1,0.01,1,0,0,0,1,0.3,'t','{}')")
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    assert before == 2
    conn.close()
    deleted = engine.cull_if_due()
    conn = engine.get_db()
    after = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    market_ids = [r["market_id"] for r in conn.execute("SELECT market_id FROM snapshots")]
    conn.close()
    assert deleted == 1
    assert after == 1
    assert market_ids == ["new"]


# guard: the default instance is A so the test suite never writes the live DB
def test_default_instance_is_a():
    assert INST_TAG == "A"


def test_format_pnl_both_labels_each_db(tmp_path):
    import positions
    a = tmp_path / "polymarket_bot.db"
    b = tmp_path / "polymarket_bot_B.db"
    for p in (a, b):
        p.write_text("")  # empty file: tables missing → format_totals degrades gracefully
    out = positions.format_pnl_both([("A", a), ("B", b)])
    assert out.startswith("[A]")
    assert "\n[B]" in out
