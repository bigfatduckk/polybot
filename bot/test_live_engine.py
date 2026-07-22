"""Phase 0-2 tests: live DB schema, read-only paper access, signal reader,
sizing, risk checks. No SDK, no network. Paper DB seeded via engine.init_db."""
import json
import sqlite3

import engine
import live_engine as le
import config

_SNAP_COLS = """ts, market_id, event_id, condition_id, question, city,
  market_date, bucket_key, bucket_json, end_date, best_bid, best_ask, bid_size,
  ask_size, depth, tick_size, min_order_size, fee_rate, fees_enabled, neg_risk,
  liquidity, last_trade_price, yes_token_id, snapshot_json"""
_SNAP_PH = ",".join(["?"] * 24)

_CAND_COLS = """ts, scan_id, market_id, condition_id, side, p_model,
  p_by_model_json, edge_after_costs, lead_hours, run_ids, bucket_key,
  effective_price, blend_mean, market_mid, snapshot_json"""
_CAND_PH = ",".join(["?"] * 15)


def _setup_dbs(tmp_path, monkeypatch):
    paper = tmp_path / "paper.db"
    live = tmp_path / "live.db"
    monkeypatch.setattr(le, "PAPER_DB_PATH", str(paper))
    monkeypatch.setattr(le, "LIVE_DB_PATH", str(live))
    monkeypatch.setattr(engine, "DB_PATH", str(paper))
    engine.init_db()
    le.init_live_db()
    return paper, live


def _seed_candidate(conn, cid, market_id="m1", city="Seoul", mdate="2026-07-20",
                    side="buy", p=0.60, edge=0.10, yes_tok="yes1", neg=False,
                    bucket="30C", cond="c1", fee_rate=0.0, fees=False, ts=None,
                    eff_price=0.50):
    conn.execute(
        f"INSERT INTO snapshots({_SNAP_COLS}) VALUES({_SNAP_PH})",
        (ts or le._now_iso(), market_id, "e1", cond, "q", city, mdate,
         bucket, "{}", "2026-07-21T00:00:00+00:00", 0.45, 0.55, 100, 100, 200, 0.01, 5.0,
         fee_rate, int(fees), int(neg), 1000.0, 0.50, yes_tok, "{}"),
    )
    conn.execute(
        f"INSERT INTO candidates({_CAND_COLS}) VALUES({_CAND_PH})",
        (ts or le._now_iso(), 1, market_id, cond, side, p,
         json.dumps({"ecmwf_ifs025": p}), edge, 24.0, "h", bucket, eff_price, 29.0, 0.50, "{}"),
    )
    conn.commit()


def _seed_snapshot(conn, market_id, yes_tok, ts, city="Seoul", mdate="2026-07-20",
                   neg=False, cond="c1"):
    conn.execute(
        f"INSERT INTO snapshots({_SNAP_COLS}) VALUES({_SNAP_PH})",
        (ts, market_id, "e1", cond, "q", city, mdate, "30C", "{}",
         "2026-07-21T00:00:00+00:00", 0.45, 0.55, 100, 100, 200, 0.01, 5.0,
         0.0, 0, int(neg), 1000.0, 0.50, yes_tok, "{}"),
    )
    conn.commit()


def _read(paper, live):
    pc = le.paper_ro_conn()
    lc = le.get_live_db()
    sigs, _ = le.read_new_signals(pc, lc)
    lc.commit()
    lc.close()
    pc.close()
    return sigs


# ── Phase 0: schema + read-only ───────────────────────────────────────────
def test_init_live_db_creates_tables(tmp_path, monkeypatch):
    _, live = _setup_dbs(tmp_path, monkeypatch)
    conn = sqlite3.connect(str(live))
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for t in ("meta", "live_ticks", "live_orders", "live_fills",
              "live_settlements", "live_balances", "live_halts"):
        assert t in tables, f"missing {t}"
    conn.close()


def test_paper_conn_is_readonly(tmp_path, monkeypatch):
    paper, _ = _setup_dbs(tmp_path, monkeypatch)
    conn = engine.get_db()
    conn.execute("INSERT INTO scans(ts, job, mode, note, snapshot_json) VALUES(?,?,?,?,?)",
                 ("2026-07-20T10:00:00+00:00", "weather", "paper", "seed", "{}"))
    conn.commit()
    conn.close()
    ro = le.paper_ro_conn()
    assert ro.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 1
    try:
        ro.execute("INSERT INTO scans(ts, job, mode, note, snapshot_json) VALUES(?,?,?,?,?)",
                   ("2026-07-20T11:00:00+00:00", "weather", "paper", "leak", "{}"))
        ro.commit()
        raise AssertionError("write to read-only paper DB succeeded — I1 broken")
    except sqlite3.OperationalError:
        pass
    ro.close()


# ── Phase 1: signal reader ─────────────────────────────────────────────────
def test_signal_edge_threshold(tmp_path, monkeypatch):
    paper, _ = _setup_dbs(tmp_path, monkeypatch)
    conn = engine.get_db()
    _seed_candidate(conn, 1, market_id="m1", edge=0.07)
    _seed_candidate(conn, 2, market_id="m2", edge=0.10)
    conn.close()
    sigs = _read(paper, _)
    assert len(sigs) == 1
    assert sigs[0].market_id == "m2"


def test_signal_evaluated_counts_pre_filter(tmp_path, monkeypatch):
    # evaluated surfaces 'filter ran over N' even when all are gated out — the
    # visibility gap that made signals=0 look like 'nothing happened'.
    paper, _ = _setup_dbs(tmp_path, monkeypatch)
    conn = engine.get_db()
    _seed_candidate(conn, 1, market_id="m1", edge=0.07)   # sub-threshold
    _seed_candidate(conn, 2, market_id="m2", edge=0.06)   # sub-threshold
    conn.close()
    pc = le.paper_ro_conn(); lc = le.get_live_db()
    sigs, evaluated = le.read_new_signals(pc, lc)
    lc.commit(); lc.close(); pc.close()
    assert sigs == []                  # nothing passed the edge gate
    assert evaluated == 2             # but the arm did look at both
    assert evaluated - len(sigs) == 2  # gated count


def test_signal_cursor_advances_past_skipped(tmp_path, monkeypatch):
    paper, _ = _setup_dbs(tmp_path, monkeypatch)
    conn = engine.get_db()
    _seed_candidate(conn, 1, market_id="m1", edge=0.07)
    conn.close()
    _read(paper, _)
    sigs = _read(paper, _)
    assert sigs == []
    lc = le.get_live_db()
    assert int(le.live_meta_get(lc, "candidate_cursor", "0")) == 1
    lc.close()


def test_signal_freshness_window(tmp_path, monkeypatch):
    paper, _ = _setup_dbs(tmp_path, monkeypatch)
    conn = engine.get_db()
    _seed_candidate(conn, 1, market_id="m1", edge=0.10,
                    ts="2020-01-01T00:00:00+00:00")
    conn.close()
    sigs = _read(paper, _)
    assert sigs == []
    lc = le.get_live_db()
    assert int(le.live_meta_get(lc, "candidate_cursor", "0")) == 1
    n = lc.execute("SELECT COUNT(*) FROM live_ticks WHERE note='skip:stale'").fetchone()[0]
    assert n == 1
    lc.close()


def test_signal_reads_latest_snapshot_per_market(tmp_path, monkeypatch):
    paper, _ = _setup_dbs(tmp_path, monkeypatch)
    conn = engine.get_db()
    _seed_candidate(conn, 1, market_id="m1", yes_tok="yes_OLD", edge=0.10)
    _seed_snapshot(conn, "m1", "yes_NEW", "2026-07-20T10:30:00+00:00")
    conn.close()
    sigs = _read(paper, _)
    assert len(sigs) == 1
    assert sigs[0].yes_token_id == "yes_NEW"


# ── Phase 2: walk_book_fill + sizing + risk ────────────────────────────────
def test_walk_book_fill_returns_fillable_and_avg():
    levels = [{"price": 0.50, "size": 5}, {"price": 0.52, "size": 10}]
    avg, filled = le.walk_book_fill(levels, 12.0)
    assert abs(avg - (0.50 * 5 + 0.52 * 7) / 12.0) < 1e-9
    assert abs(filled - 12.0) < 1e-9


def test_walk_book_fill_thin_book_caps_at_available():
    avg, filled = le.walk_book_fill([{"price": 0.50, "size": 3}], 12.0)
    assert abs(filled - 3.0) < 1e-9
    assert abs(avg - 0.50) < 1e-9


def test_walk_book_fill_empty():
    avg, filled = le.walk_book_fill([], 12.0)
    assert avg is None and filled == 0.0


def _sig(side="buy", p=0.60, fee_rate=0.0, fees=False, city="Seoul", mdate="2026-07-20",
         market_id="m1"):
    return le.LiveSignal(
        candidate_id=1, market_id=market_id, condition_id="c1", side=side, p_model=p,
        edge_after_costs=0.10, effective_price=0.50, city=city, market_date=mdate,
        bucket_key="30C", yes_token_id="yes1", fee_rate=fee_rate, fees_enabled=fees,
        neg_risk=False, ts="2026-07-20T10:00:00+00:00",
    )


def test_sizing_quarter_kelly_capped_buy():
    notional, shares, edge = le.size_signal(_sig(side="buy", p=0.60), walked_exec_price=0.50)
    assert abs(notional - 10.0) < 1e-9
    assert abs(shares - 20.0) < 1e-9
    assert abs(edge - 0.10) < 1e-9


def test_sizing_quarter_kelly_capped_sell():
    notional, shares, edge = le.size_signal(_sig(side="sell", p=0.30), walked_exec_price=0.60)
    assert abs(notional - 10.0) < 1e-9
    assert abs(shares - 10.0 / 0.60) < 1e-6
    assert abs(edge - ((1.0 - 0.30) - 0.60)) < 1e-9


def test_sizing_skips_below_clob_min():
    notional, shares, edge = le.size_signal(_sig(side="buy", p=0.501), walked_exec_price=0.50)
    assert notional is None and shares is None and edge is None


def test_sizing_rejects_degenerate_price():
    assert le.size_signal(_sig(p=0.5), walked_exec_price=0.0)[0] is None
    assert le.size_signal(_sig(p=0.5), walked_exec_price=1.0)[0] is None


def _spec(side="buy", p=0.60, exec_price=0.50, market_id="m1", city="Seoul",
          mdate="2026-07-20"):
    sig = _sig(side=side, p=p, city=city, mdate=mdate, market_id=market_id)
    notional, shares, edge = le.size_signal(sig, walked_exec_price=exec_price)
    return le.LiveOrderSpec(signal=sig, exec_token_id="t", exec_side="BUY",
                            price=exec_price, size=shares or 0.0, notional=notional or 0.0,
                            edge_at_exec=edge or 0.0, kelly_fraction=0.0)


def _state(realized_today=0.0, consec=0, opens=None):
    return le.LiveState(bankroll=200.0, realized_pnl_today=realized_today,
                        consecutive_losses=consec, open_positions=opens or [])


def test_risk_approves_clean_buy():
    assert le.live_risk_check(_spec(), _state()).approved


def test_risk_daily_loss_halt_at_20():
    v = le.live_risk_check(_spec(), _state(realized_today=-20.0))
    assert not v.approved and "daily loss" in v.reason


def test_risk_consecutive_loss_halt_6():
    v = le.live_risk_check(_spec(), _state(consec=6))
    assert not v.approved and "consecutive" in v.reason


def test_risk_max_open_5():
    opens = [{"market_id": f"mx{i}", "city": "Seoul", "market_date": "2026-07-20"} for i in range(5)]
    v = le.live_risk_check(_spec(market_id="mNEW"), _state(opens=opens))
    assert not v.approved and "max open" in v.reason


def test_risk_region_day_cap_3():
    opens = [{"market_id": f"mx{i}", "city": "Seoul", "market_date": "2026-07-20"} for i in range(3)]
    v = le.live_risk_check(_spec(market_id="mNEW", city="Seoul", mdate="2026-07-20"),
                           _state(opens=opens))
    assert not v.approved and "region-day" in v.reason


def test_risk_one_per_market():
    opens = [{"market_id": "m1", "city": "Seoul", "market_date": "2026-07-20"}]
    v = le.live_risk_check(_spec(market_id="m1"), _state(opens=opens))
    assert not v.approved and "one position" in v.reason


def test_risk_halt_live_file_blocks(tmp_path, monkeypatch):
    halt = tmp_path / "HALT_LIVE"
    halt.write_text("halt")
    monkeypatch.setattr(config, "HALT_LIVE_FILE", str(halt))
    v = le.live_risk_check(_spec(), _state())
    assert not v.approved and "HALT_LIVE" in v.reason


def test_risk_per_trade_cap_blocks_oversize():
    sig = _sig(p=0.60)
    spec = le.LiveOrderSpec(signal=sig, exec_token_id="t", exec_side="BUY", price=0.50,
                            size=100.0, notional=50.0, edge_at_exec=0.10, kelly_fraction=0.0)
    v = le.live_risk_check(spec, _state())
    assert not v.approved and "per-trade cap" in v.reason


# ── live_positions formatters (live / opens live) ──────────────────────────
def _seed_live_rows(live, monkeypatch, tmp_path):
    monkeypatch.setattr(le, "LIVE_DB_PATH", str(live))
    le.init_live_db()
    monkeypatch.setattr(config, "HALT_LIVE_FILE", str(tmp_path / "NOPE"))
    lc = le.get_live_db()
    lc.execute(
        "INSERT INTO live_orders(ts, city, market_date, signal_side, price, size, "
        "dry_run, status) VALUES(?,?,?,?,?,?,?,?)",
        ("2026-07-20T12:20:00+00:00", "Seoul", "2026-07-20", "buy", 0.45, 10, 1, "posted"))
    lc.execute(
        "INSERT INTO live_orders(ts, city, market_date, signal_side, price, size, "
        "dry_run, status) VALUES(?,?,?,?,?,?,?,?)",
        ("2026-07-20T12:21:00+00:00", "Tokyo", "2026-07-20", "buy", 0.30, 8, 1, "rejected"))
    lc.execute(
        "INSERT INTO live_settlements(ts, city, date, bucket_key, resolved_yes, pnl) "
        "VALUES(?,?,?,?,?,?)",
        ("2026-07-20T13:00:00+00:00", "Seoul", "2026-07-20", "30C", 1, 5.50))
    lc.execute(
        "INSERT INTO live_balances(ts, usdc, matic, source) VALUES(?,?,?,?)",
        ("2026-07-20T12:08:00+00:00", 200.0, 135.6, "rpc"))
    lc.execute(
        "INSERT INTO live_ticks(ts, job, note) VALUES(?,?,?)",
        ("2026-07-20T12:20:05+00:00", "weather-live", "skip:low_edge"))
    lc.commit()
    lc.close()


def test_live_health_one_glance(tmp_path, monkeypatch):
    import live_positions as lp
    _setup_dbs(tmp_path, monkeypatch)
    _seed_live_rows(tmp_path / "live.db", monkeypatch, tmp_path)
    monkeypatch.setenv("LIVE_DRY_RUN", "1")
    out = lp.format_live_health(path=str(tmp_path / "live.db"))
    assert "HALT=no" in out and "DRY_RUN=on" in out
    assert "open=1 dry_signed=1 rejected=1" in out   # posted counts as open+dry_signed
    assert "settled=1 realized=$+5.50" in out
    assert "gas=135.6 POL" in out and "usdc=$200.00" in out
    assert "weather-live skip:low_edge" in out


def test_live_health_halt_and_go_live(tmp_path, monkeypatch):
    import live_positions as lp
    _setup_dbs(tmp_path, monkeypatch)
    _seed_live_rows(tmp_path / "live.db", monkeypatch, tmp_path)
    halt = tmp_path / "HALT_LIVE"
    halt.write_text("x")
    monkeypatch.setattr(config, "HALT_LIVE_FILE", str(halt))
    monkeypatch.setenv("LIVE_DRY_RUN", "0")
    out = lp.format_live_health(path=str(tmp_path / "live.db"))
    assert "HALT=yes" in out and "DRY_RUN=OFF" in out


def test_live_open_lists_only_open(tmp_path, monkeypatch):
    import live_positions as lp
    _setup_dbs(tmp_path, monkeypatch)
    _seed_live_rows(tmp_path / "live.db", monkeypatch, tmp_path)
    out = lp.format_live_open(path=str(tmp_path / "live.db"))
    assert "Seoul" in out and "Tokyo" not in out   # rejected excluded


def test_live_open_includes_filled(tmp_path, monkeypatch):
    # A 'filled' but not-yet-settled order is an open position (capital
    # committed, awaiting resolution). `opens live` must list it, matching
    # the `live` health count which already counts 'filled' as open.
    import live_positions as lp
    _setup_dbs(tmp_path, monkeypatch)
    live = str(tmp_path / "live.db")
    lc = le.get_live_db()
    lc.execute(
        "INSERT INTO live_orders(ts, city, market_date, signal_side, price, size, "
        "dry_run, status) VALUES(?,?,?,?,?,?,?,?)",
        ("2026-07-20T12:30:00+00:00", "Taipei", "2026-07-20", "buy", 0.40, 12, 1, "filled"))
    lc.commit()
    lc.close()
    out = lp.format_live_open(path=live)
    health = lp.format_live_health(path=live)
    assert "Taipei" in out                       # filled shows in opens live
    assert "open=1" in health                   # and health counts it as open


def test_live_ticks_lists_history(tmp_path, monkeypatch):
    import live_positions as lp
    _setup_dbs(tmp_path, monkeypatch)
    _seed_live_rows(tmp_path / "live.db", monkeypatch, tmp_path)
    out = lp.format_live_ticks(10, path=str(tmp_path / "live.db"))
    assert "live ticks" in out and "weather-live skip:low_edge" in out


def test_live_ticks_n_clamp(tmp_path, monkeypatch):
    import live_positions as lp
    _setup_dbs(tmp_path, monkeypatch)
    _seed_live_rows(tmp_path / "live.db", monkeypatch, tmp_path)
    out = lp.format_live_ticks(1, path=str(tmp_path / "live.db"))
    assert out.count("\n") == 1   # header + 1 row


def test_live_gas_lists_balances(tmp_path, monkeypatch):
    import live_positions as lp
    _setup_dbs(tmp_path, monkeypatch)
    _seed_live_rows(tmp_path / "live.db", monkeypatch, tmp_path)
    out = lp.format_live_gas(path=str(tmp_path / "live.db"))
    assert "gas=135.6 POL" in out and "usdc=$200.00" in out


# ── live control: halt / unhalt ─────────────────────────────────────────────
def _setup_control(tmp_path, monkeypatch):
    import live_positions as lp
    live = tmp_path / "live.db"
    monkeypatch.setattr(le, "LIVE_DB_PATH", str(live))
    monkeypatch.setattr(lp, "LIVE_DB_PATH", str(live))
    le.init_live_db()
    halt = tmp_path / "HALT_LIVE"
    monkeypatch.setattr(config, "HALT_LIVE_FILE", str(halt))
    return halt


def test_control_halt_needs_confirm(tmp_path, monkeypatch):
    import live_control as lc
    halt = _setup_control(tmp_path, monkeypatch)
    out = lc.handle_control(["halt"])
    assert "halt yes" in out and not halt.exists()


def test_control_halt_confirmed_sets_file_and_audits(tmp_path, monkeypatch):
    import live_control as lc
    halt = _setup_control(tmp_path, monkeypatch)
    out = lc.handle_control(["halt", "yes"])
    assert "HALT_LIVE SET" in out and halt.exists()
    c = le.get_live_db()
    n_h = c.execute("SELECT COUNT(*) FROM live_halts WHERE reason='telegram:set'").fetchone()[0]
    n_t = c.execute("SELECT COUNT(*) FROM live_ticks WHERE note='halt:set'").fetchone()[0]
    c.close()
    assert n_h == 1 and n_t == 1


def test_control_unhalt_confirmed_clears(tmp_path, monkeypatch):
    import live_control as lc
    halt = _setup_control(tmp_path, monkeypatch)
    halt.write_text("x")
    out = lc.handle_control(["unhalt", "yes"])
    assert "CLEARED" in out and not halt.exists()


def test_control_unhalt_idempotent_when_absent(tmp_path, monkeypatch):
    import live_control as lc
    halt = _setup_control(tmp_path, monkeypatch)
    out = lc.handle_control(["unhalt", "yes"])
    assert "CLEARED" in out and not halt.exists()


def test_control_unknown_subcommand(tmp_path, monkeypatch):
    import live_control as lc
    _setup_control(tmp_path, monkeypatch)
    out = lc.handle_control(["frobnicate"])
    assert "usage" in out
