import json

import markets
from config import (
    FLB_CALIB_PATH,
    FLB_HORIZON_DAYS,
    FLB_MIN_LIQUIDITY,
    FLB_MIN_VOLUME,
    MIN_EDGE,
    PER_TRADE_CAP_ABS,
    PRICE_BAND,
)
from edge_engine import store_candidate, store_snapshot
from engine import _lead_hours, _walk_book

EDGE = "flb"
_calib = None


def load_calib(path=FLB_CALIB_PATH):
    global _calib
    try:
        with open(path, encoding="utf-8") as f:
            _calib = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _calib = None
    return _calib


def p_model(price, horizon_days, calib=None):
    calib = calib if calib is not None else _calib
    if not calib:
        return price
    table = calib["table"]
    lag = calib["snap_lag_days"]
    snap = min(calib["snapshots"], key=lambda s: abs(lag[s] - horizon_days))
    idx = int(price / calib["price_bucket_size"])
    idx = max(0, min(idx, len(table[snap]) - 1))
    cell = table[snap][idx]
    if cell is None:
        for alt in calib["snapshots"]:
            if alt == snap:
                continue
            c = table[alt][idx] if idx < len(table[alt]) else None
            if c is not None:
                cell = c
                break
    if cell is None:
        return price
    return cell[0]


def _fee(m, p):
    return m.fee_rate * p * (1 - p) if m.fees_enabled else 0.0


def compute_candidate(m, calib=None):
    calib = calib if calib is not None else _calib
    horizon_days = _lead_hours(m.end_date) / 24.0
    if not (FLB_HORIZON_DAYS[0] <= horizon_days <= FLB_HORIZON_DAYS[1]):
        return None
    target_shares = PER_TRADE_CAP_ABS / max(m.best_ask, 0.01)
    eff_ask = _walk_book(m.asks, target_shares)
    eff_bid = _walk_book(m.bids, target_shares)
    if eff_ask is None or eff_bid is None:
        return None
    p_buy = p_model(eff_ask, horizon_days, calib)
    p_sell = p_model(eff_bid, horizon_days, calib)
    buy_edge = p_buy - eff_ask - _fee(m, p_buy)
    sell_edge = eff_bid - p_sell - _fee(m, p_sell)
    chosen = None
    if buy_edge >= MIN_EDGE and PRICE_BAND[0] <= eff_ask <= PRICE_BAND[1]:
        chosen = ("buy", p_buy, eff_ask, buy_edge)
    if chosen is None and sell_edge >= MIN_EDGE \
       and PRICE_BAND[0] <= eff_bid <= PRICE_BAND[1]:
        chosen = ("sell", p_sell, eff_bid, sell_edge)
    if chosen is None:
        return None
    side, p_model_val, eff, edge = chosen
    return {
        "market_id": m.market_id,
        "token_id": m.yes_token_id,
        "side": side,
        "p_model": p_model_val,
        "effective_price": eff,
        "edge_after_costs": edge,
        "lead_hours": horizon_days * 24.0,
        "horizon_days": horizon_days,
        "meta": {"buy_edge": buy_edge, "sell_edge": sell_edge,
                 "best_bid": m.best_bid, "best_ask": m.best_ask},
    }


def scan_flb(scan_id):
    load_calib()
    params = {
        "closed": "false",
        "active": "true",
        "order": "endDate",
        "ascending": "true",
        "limit": 500,
        "volume_num_min": FLB_MIN_VOLUME,
        "liquidity_num_min": FLB_MIN_LIQUIDITY,
        "end_date_min": _date_offset(FLB_HORIZON_DAYS[0]),
        "end_date_max": _date_offset(FLB_HORIZON_DAYS[1]),
    }
    mkts = markets.fetch_markets(params, with_book=True)
    cands = []
    for m in mkts:
        store_snapshot(EDGE, m, {"scan_id": scan_id})
        c = compute_candidate(m)
        if c:
            c["scan_id"] = scan_id
            store_candidate(EDGE, scan_id, c)
            cands.append(c)
    return cands


def _date_offset(days):
    from datetime import datetime, timedelta, timezone
    d = datetime.now(timezone.utc) + timedelta(days=days)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")
