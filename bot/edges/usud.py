import json
import math
import sqlite3

import markets
from config import (
    DB_PATH,
    MIN_EDGE,
    PER_TRADE_CAP_ABS,
    PRICE_BAND,
    USUD_MIN_DEPTH,
    USUD_MIN_EDGE,
    USUD_RISK_FREE,
)
from edge_engine import store_candidate, store_snapshot
from engine import _walk_book

EDGE = "usud"


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_above(spot, strike, sigma, tau, r=0.0):
    if sigma <= 0.0 or tau <= 0.0 or strike <= 0.0 or spot <= 0.0:
        return 1.0 if spot > strike else 0.0
    d2 = (math.log(spot / strike) + (r - 0.5 * sigma * sigma) * tau) / (sigma * math.sqrt(tau))
    return _norm_cdf(d2)


def _ticker_from_question(q):
    u = (q or "").upper().strip()
    if u.startswith("SPY "):
        return "SPY", "SPY"
    if u.startswith("NVDA "):
        return "NVDA", "NVDA"
    if u.startswith("TSLA ") or u.startswith("TESLA "):
        return "TSLA", "TSLA"
    if u.startswith("S&P 500") or u.startswith("S&P500"):
        return "SPX", "^GSPC"
    if u.startswith("DOW JONES") or u.startswith("DJIA"):
        return "DJIA", "^DJI"
    return None, None


def _fee(m, p):
    return m.fee_rate * p * (1.0 - p) if m.fees_enabled else 0.0


def _price(m, quote):
    spot = quote["spot"]
    iv = quote["iv"]
    strike = quote["prior_close"]
    tau = quote["tau_years"]
    r = quote.get("r", USUD_RISK_FREE)
    if iv is None or iv <= 0.0 or tau <= 0.0 or strike <= 0.0 or spot <= 0.0:
        return None
    if m.depth < USUD_MIN_DEPTH:
        return None
    p = prob_above(spot, strike, iv, tau, r)
    target_shares = PER_TRADE_CAP_ABS / max(m.best_ask, 0.01)
    eff_ask = _walk_book(m.asks, target_shares)
    eff_bid = _walk_book(m.bids, target_shares)
    if eff_ask is None or eff_bid is None:
        return None
    return {
        "p": p, "eff_ask": eff_ask, "eff_bid": eff_bid,
        "buy_edge": p - eff_ask - _fee(m, p),
        "sell_edge": eff_bid - p - _fee(m, p),
        "spot": spot, "iv": iv, "prior_close": strike, "tau": tau, "r": r,
    }


def compute_candidate(m, quote):
    priced = _price(m, quote)
    if priced is None:
        return None
    p, eff_ask, eff_bid = priced["p"], priced["eff_ask"], priced["eff_bid"]
    buy_edge, sell_edge = priced["buy_edge"], priced["sell_edge"]
    chosen = None
    if buy_edge >= USUD_MIN_EDGE and PRICE_BAND[0] <= eff_ask <= PRICE_BAND[1]:
        chosen = ("buy", p, eff_ask, buy_edge)
    if chosen is None and sell_edge >= USUD_MIN_EDGE \
       and PRICE_BAND[0] <= eff_bid <= PRICE_BAND[1]:
        chosen = ("sell", p, eff_bid, sell_edge)
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
        "lead_hours": priced["tau"] * 24.0,
        "horizon_days": priced["tau"],
        "meta": {
            "ticker": quote.get("ticker", ""),
            "spot": priced["spot"], "iv": priced["iv"], "prior_close": priced["prior_close"],
            "tau_years": priced["tau"], "buy_edge": buy_edge, "sell_edge": sell_edge,
            "best_bid": m.best_bid, "best_ask": m.best_ask, "depth": m.depth,
        },
    }


def _store_quote(scan_id, m, quote, priced):
    conn = sqlite3.connect(DB_PATH)
    from datetime import datetime, timezone
    conn.execute(
        """INSERT INTO usud_quotes(ts, scan_id, market_id, ticker, question, end_date,
           market_ask, market_bid, best_bid, best_ask, depth, p_model, spot, iv,
           prior_close, tau_years, buy_edge, sell_edge)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), scan_id,
         m.market_id, quote.get("ticker", ""), m.question, m.end_date,
         priced["eff_ask"], priced["eff_bid"], m.best_bid, m.best_ask, m.depth,
         priced["p"], priced["spot"], priced["iv"], priced["prior_close"],
         priced["tau"], priced["buy_edge"], priced["sell_edge"]),
    )
    conn.commit()
    conn.close()


def _tau_to_close(end_date):
    from datetime import datetime, timezone
    if not end_date:
        return 0.0
    try:
        dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    secs = (dt - now).total_seconds()
    return secs / (365.0 * 24.0 * 3600.0) if secs > 0 else 0.0


def _atm_iv(t, spot):
    exps = getattr(t, "options", None) or ()
    if not exps or not spot:
        return None
    try:
        chain = t.option_chain(exps[0])
    except Exception:
        return None
    calls = getattr(chain, "calls", None)
    if calls is None or len(calls) == 0 or "strike" not in calls or "impliedVolatility" not in calls:
        return None
    row = calls.iloc[(calls["strike"] - spot).abs().argmin()]
    iv = float(row.get("impliedVolatility") or 0.0)
    return iv if iv > 0.0 else None


def _fetch_quote(yf_sym, m):
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        t = yf.Ticker(yf_sym)
        info = t.fast_info
        spot = float(info.get("last_price") or info.get("previous_close") or 0.0)
        prior = float(info.get("previous_close") or 0.0)
        if spot <= 0.0 or prior <= 0.0:
            return None
        iv = _atm_iv(t, spot)
        if iv is None:
            return None
        tau = _tau_to_close(m.end_date)
        if tau <= 0.0:
            return None
        return {"spot": spot, "iv": iv, "prior_close": prior,
                "tau_years": tau, "r": USUD_RISK_FREE}
    except Exception:
        return None


def _date_offset(days):
    from datetime import datetime, timedelta, timezone
    d = datetime.now(timezone.utc) + timedelta(days=days)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def scan_usud(scan_id):
    params = {
        "closed": "false",
        "active": "true",
        "order": "endDate",
        "ascending": "true",
        "limit": 500,
        "end_date_min": _date_offset(0),
        "end_date_max": _date_offset(2),
    }
    mkts = markets.fetch_markets(params, with_book=True)
    cands = []
    for m in mkts:
        tok, yf_sym = _ticker_from_question(m.question)
        if not tok:
            continue
        store_snapshot(EDGE, m, {"scan_id": scan_id, "ticker": tok})
        quote = _fetch_quote(yf_sym, m)
        if quote is None:
            continue
        quote["ticker"] = tok
        priced = _price(m, quote)
        if priced is not None:
            _store_quote(scan_id, m, quote, priced)
        c = compute_candidate(m, quote)
        if c:
            c["scan_id"] = scan_id
            store_candidate(EDGE, scan_id, c)
            cands.append(c)
    return cands
