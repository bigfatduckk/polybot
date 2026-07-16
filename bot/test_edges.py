import markets
import settle
from datetime import datetime, timedelta, timezone
from edges import arb, crossvenue, flb


def _arb_market(mid, best_bid, best_ask, size=100, neg_risk=True):
    return markets.Market(
        market_id=mid, condition_id="c", event_id="e", event_slug="s",
        question="q", end_date=_iso_future(30),
        best_bid=best_bid, best_ask=best_ask, bid_size=size, ask_size=size,
        depth=size * 2, tick_size=0.01, min_order_size=5.0, fee_rate=0.0,
        fees_enabled=False, neg_risk=neg_risk, liquidity=10000.0,
        last_trade_price=(best_bid + best_ask) / 2, yes_token_id="tok-" + mid,
        no_token_id="no-" + mid, ts="",
        bids=[{"price": best_bid, "size": size}],
        asks=[{"price": best_ask, "size": size}], raw={},
    )


def test_arb_detects_long_bundle_when_sum_ask_below_one():
    sibs = [_arb_market("a", 0.28, 0.30), _arb_market("b", 0.28, 0.30),
            _arb_market("c", 0.33, 0.35)]
    b = arb.compute_bundle(sibs, event_id="e1")
    assert b is not None
    assert b["side"] == "buy"
    assert abs(b["sum_eff"] - 0.95) < 1e-9
    assert b["gap"] > arb.ARB_MIN_GAP
    assert b["n_outcomes"] == 3
    assert len(b["legs"]) == 3


def test_arb_detects_short_bundle_when_sum_bid_above_one():
    sibs = [_arb_market("a", 0.40, 0.42), _arb_market("b", 0.40, 0.42),
            _arb_market("c", 0.40, 0.42)]
    b = arb.compute_bundle(sibs, event_id="e1")
    assert b is not None
    assert b["side"] == "sell"
    assert abs(b["sum_eff"] - 1.20) < 1e-9
    assert b["gap"] > arb.ARB_MIN_GAP


def test_arb_phantom_depth_guard_rejects_thin_leg():
    sibs = [_arb_market("a", 0.28, 0.30, size=100),
            _arb_market("b", 0.28, 0.30, size=10),
            _arb_market("c", 0.33, 0.35, size=100)]
    assert arb.compute_bundle(sibs, event_id="e1") is None


def test_arb_no_bundle_at_parity():
    sibs = [_arb_market("a", 0.30, 0.34), _arb_market("b", 0.30, 0.33),
            _arb_market("c", 0.30, 0.33)]
    assert arb.compute_bundle(sibs, event_id="e1") is None


def test_arb_rejects_non_neg_risk_and_wrong_count():
    non_excl = [_arb_market("a", 0.28, 0.30, neg_risk=False),
                _arb_market("b", 0.28, 0.30, neg_risk=False)]
    assert arb.compute_bundle(non_excl, event_id="e1") is None
    single = [_arb_market("a", 0.28, 0.30)]
    assert arb.compute_bundle(single, event_id="e1") is None


def _synth_calib():
    table = {"final": [None] * 20, "24h": [None] * 20, "7d": [None] * 20}
    table["final"][16] = [0.90, 200]
    table["24h"][16] = [0.92, 200]
    table["7d"][16] = [0.95, 200]
    table["7d"][1] = [0.02, 200]
    return {
        "price_bucket_size": 0.05,
        "snapshots": ["final", "24h", "7d"],
        "snap_lag_days": {"final": 0.0, "24h": 1.0, "7d": 7.0},
        "min_cell_n": 50,
        "table": table,
    }


def _synth_market(best_bid, best_ask, size=1000, end_days=10, token="t1",
                  fee_rate=0.0, fees=False):
    return markets.Market(
        market_id="m1", condition_id="c1", event_id="e1", event_slug="s",
        question="q", end_date=_iso_future(end_days),
        best_bid=best_bid, best_ask=best_ask, bid_size=size, ask_size=size,
        depth=size * 2, tick_size=0.01, min_order_size=5.0, fee_rate=fee_rate,
        fees_enabled=fees, neg_risk=False, liquidity=10000.0,
        last_trade_price=(best_bid + best_ask) / 2, yes_token_id=token,
        no_token_id="t0", ts="",
        bids=[{"price": best_bid, "size": size}],
        asks=[{"price": best_ask, "size": size}], raw={},
    )


def _iso_future(days):
    d = datetime.now(timezone.utc) + timedelta(days=days)
    return d.isoformat(timespec="seconds")


def test_p_model_uses_nearest_snapshot_and_cell():
    calib = _synth_calib()
    assert abs(flb.p_model(0.82, 7.0, calib) - 0.95) < 1e-9
    assert abs(flb.p_model(0.82, 1.0, calib) - 0.92) < 1e-9
    assert abs(flb.p_model(0.82, 0.0, calib) - 0.90) < 1e-9


def test_p_model_falls_back_across_snapshots():
    calib = _synth_calib()
    assert abs(flb.p_model(0.07, 7.0, calib) - 0.02) < 1e-9
    assert abs(flb.p_model(0.82, 7.0, calib) - 0.95) < 1e-9


def test_p_model_returns_price_when_no_calib():
    assert abs(flb.p_model(0.40, 5.0, None) - 0.40) < 1e-9
    assert abs(flb.p_model(0.40, 5.0, {}) - 0.40) < 1e-9


def test_compute_candidate_finds_buy_on_underpriced_favourite():
    calib = _synth_calib()
    m = _synth_market(0.80, 0.82, end_days=8)
    c = flb.compute_candidate(m, calib)
    assert c is not None
    assert c["side"] == "buy"
    assert abs(c["p_model"] - 0.95) < 1e-9
    assert abs(c["effective_price"] - 0.82) < 1e-9
    assert c["edge_after_costs"] > 0.06


def test_compute_candidate_no_signal_when_well_priced():
    calib = _synth_calib()
    m = _synth_market(0.94, 0.95, end_days=8)
    assert flb.compute_candidate(m, calib) is None


def test_compute_candidate_rejects_out_of_horizon():
    calib = _synth_calib()
    m = _synth_market(0.80, 0.82, end_days=200)
    assert flb.compute_candidate(m, calib) is None



def _book(bids, asks, **extra):
    return {"bids": bids, "asks": asks, "tick_size": 0.01,
            "min_order_size": 5, "neg_risk": False, "last_trade_price": 0.5, **extra}


def test_parse_book_orders_and_depth():
    b = _book(
        [{"price": "0.40", "size": "100"}, {"price": "0.38", "size": "200"}],
        [{"price": "0.42", "size": "150"}, {"price": "0.44", "size": "50"}],
    )
    bids, asks, bb, ba, bs, asz, depth, tick, min_sz, neg, last = markets._parse_book(b)
    assert bb == 0.40 and ba == 0.42
    assert bs == 100 and asz == 150
    assert bids[0]["price"] == 0.40 and asks[0]["price"] == 0.42
    assert depth == 500


def test_parse_book_empty_book_defaults():
    b = _book([], [])
    bids, asks, bb, ba, bs, asz, depth, tick, min_sz, neg, last = markets._parse_book(b)
    assert bb == 0.0 and ba == 1.0
    assert bs == 0.0 and asz == 0.0
    assert depth == 0.0 and last == 0.5


def test_fill_pnl_buy_yes_wins_and_loses():
    assert abs(settle._fill_pnl("buy", 0.30, 100, True) - 70.0) < 1e-9
    assert abs(settle._fill_pnl("buy", 0.30, 100, False) - (-30.0)) < 1e-9


def test_fill_pnl_sell_short_yes_wins_and_loses():
    assert abs(settle._fill_pnl("sell", 0.30, 100, True) - (-70.0)) < 1e-9
    assert abs(settle._fill_pnl("sell", 0.30, 100, False) - 30.0) < 1e-9


def test_arb_bundle_pnl_is_riskless_at_parity():
    a_win = settle._fill_pnl("buy", 0.30, 100, True)
    b_lose = settle._fill_pnl("buy", 0.70, 100, False)
    assert abs((a_win + b_lose) - 0.0) < 1e-9


def test_cv_normalize_strips_stopwords_and_punct():
    assert crossvenue.normalize("Will the DJIA close above 40000 on 2026-12-31?") == \
        ["djia", "close", "above", "40000", "2026", "12", "31"]


def test_cv_match_score_high_for_equivalent_questions():
    pm = "Will the Fed cut rates in September 2026?"
    kalshi = "Fed rate cut September 2026"
    assert crossvenue.match_score(pm, kalshi) >= 0.34


def test_cv_match_score_zero_for_unrelated():
    assert crossvenue.match_score("BTC above 100k", "Yankees win World Series") == 0.0


def test_cv_compute_gap_positive_and_net_after_fees():
    g = crossvenue.compute_gap(0.60, 0.64, pm_fee=0.0)
    assert abs(g["gap"] - 0.04) < 1e-9
    assert abs(g["net_of_fees"] - (0.04 - crossvenue.SPREAD_ESTIMATE)) < 1e-9


def test_cv_compute_gap_returns_none_on_missing():
    assert crossvenue.compute_gap(0.60, None) is None
    assert crossvenue.compute_gap(None, 0.64) is None


def test_cv_kalshi_yes_mid_from_bid_ask():
    assert abs(crossvenue._kalshi_yes_mid({"yes_ask": 62, "yes_bid": 60}) - 61.0) < 1e-9
    assert abs(crossvenue._kalshi_yes_mid({"yes_ask": 0.65}) - 0.65) < 1e-9
    assert crossvenue._kalshi_yes_mid({}) is None


def test_cv_best_match_filters_below_threshold():
    k = [{"title": "Unrelated race"}, {"title": "Fed rate cut September 2026",
          "yes_ask": 0.64, "yes_bid": 0.62, "market_name": "FED-26SEP"}]
    best, score = crossvenue._best_match("Will the Fed cut rates in September 2026?", k)
    assert best is not None
    assert best["market_name"] == "FED-26SEP"
    assert crossvenue._best_match("BTC above 100k", k)[0] is None
