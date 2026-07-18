import json
import math
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import markets
from config import (
    DB_PATH,
    MIN_EDGE,
    PER_TRADE_CAP_ABS,
    PRICE_BAND,
    USUD_MIN_DEPTH,
    USUD_MIN_EDGE,
    USUD_RISK_FREE,
    USUD_TICKERS,
)
from edge_engine import store_candidate, store_snapshot
from engine import _walk_book

EDGE = "usud"

TICKERS = {
    "SPY": ("spy", "SPY"),
    "SPX": ("spx", "^GSPC"),
    "DJIA": ("djia", "^DJI"),
    "NVDA": ("nvda", "NVDA"),
    "TSLA": ("tsla", "TSLA"),
}

MONTHS = ["january", "february", "march", "april", "may", "june",
          "july", "august", "september", "october", "november", "december"]


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_above(spot, strike, sigma, tau, r=0.0):
    if sigma <= 0.0 or tau <= 0.0 or strike <= 0.0 or spot <= 0.0:
        return 1.0 if spot > strike else 0.0
    d2 = (math.log(spot / strike) + (r - 0.5 * sigma * sigma) * tau) / (sigma * math.sqrt(tau))
    return _norm_cdf(d2)


def _et_today():
    return datetime.now(ZoneInfo("America/New_York")).date()


def _slug_for(slug_prefix, d):
    return f"{slug_prefix}-up-or-down-on-{MONTHS[d.month - 1]}-{d.day}-{d.year}"


def _fetch_event_markets(slug):
    events = markets.fetch_events({"slug": slug, "closed": "false", "active": "true"})
    if not events:
        return []
    out = []
    for mkt in (events[0].get("markets") or []):
        m = markets._market_from_mkt(mkt)
        if m.yes_token_id:
            markets._hydrate(m, markets.fetch_book(m.yes_token_id))
        out.append(m)
    return out


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


def scan_usud(scan_id):
    d = _et_today()
    if d.weekday() >= 5:
        return []
    cands = []
    for name in USUD_TICKERS:
        slug_prefix, yf_sym = TICKERS[name]
        for m in _fetch_event_markets(_slug_for(slug_prefix, d)):
            store_snapshot(EDGE, m, {"scan_id": scan_id, "ticker": name})
            if not m.yes_token_id or m.best_ask <= 0.0:
                continue
            quote = _fetch_quote(yf_sym, m)
            if quote is None:
                continue
            quote["ticker"] = name
            priced = _price(m, quote)
            if priced is not None:
                _store_quote(scan_id, m, quote, priced)
            c = compute_candidate(m, quote)
            if c:
                c["scan_id"] = scan_id
                store_candidate(EDGE, scan_id, c)
                cands.append(c)
    return cands
