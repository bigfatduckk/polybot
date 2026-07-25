import json
import sqlite3
from datetime import datetime, timezone

import markets
from config import (
    ARB_MAX_OUTCOMES,
    ARB_MIN_DEPTH,
    ARB_MIN_GAP,
    ARB_SCAN_TAGS,
    DB_PATH,
    PER_TRADE_CAP_ABS,
)
from edge_engine import store_snapshot
from engine import _walk_book

EDGE = "arb"


def _fee(m, p):
    return m.fee_rate * p * (1 - p) if m.fees_enabled else 0.0


def _has_depth(siblings, side):
    if side == "buy":
        sizes = [m.ask_size for m in siblings]
    else:
        sizes = [m.bid_size for m in siblings]
    return all(s >= ARB_MIN_DEPTH for s in sizes) if sizes else False


def compute_bundle(siblings, event_id=""):
    elig = [m for m in siblings if m.neg_risk and m.yes_token_id
            and m.best_ask > 0 and m.best_bid > 0]
    if not (2 <= len(elig) <= ARB_MAX_OUTCOMES):
        return None
    elig.sort(key=lambda m: m.market_id)
    min_depth = min((m.ask_size for m in elig), default=0.0)
    min_depth_bid = min((m.bid_size for m in elig), default=0.0)
    cap_shares = PER_TRADE_CAP_ABS / max(min(sum(m.best_ask for m in elig), 0.01), 0.01)
    bundles = []
    if _has_depth(elig, "buy"):
        shares = min(min_depth, cap_shares)
        walked = [_walk_book(m.asks, shares) for m in elig]
        if all(w is not None for w in walked):
            effs = [w[0] for w in walked]
            # Atomic guard: a multi-leg bundle cannot partially fill — if any leg's
            # book can't absorb `shares`, the whole bundle is unexecutable.
            if all(w[1] >= shares - 1e-6 for w in walked):
                sum_eff = sum(effs)
                fees = sum(_fee(m, e) for m, e in zip(elig, effs))
                gap = 1.0 - sum_eff
                net = gap - fees
                bundles.append(_bundle(event_id, "buy", elig, effs, shares,
                                       sum_eff, gap, net, min_depth))
    if _has_depth(elig, "sell"):
        shares = min(min_depth_bid, cap_shares)
        walked = [_walk_book(m.bids, shares) for m in elig]
        if all(w is not None for w in walked):
            effs = [w[0] for w in walked]
            if all(w[1] >= shares - 1e-6 for w in walked):
                sum_eff = sum(effs)
                fees = sum(_fee(m, e) for m, e in zip(elig, effs))
                gap = sum_eff - 1.0
                net = gap - fees
                bundles.append(_bundle(event_id, "sell", elig, effs, shares,
                                       sum_eff, gap, net, min_depth_bid))
    if not bundles:
        return None
    return max(bundles, key=lambda b: b["net_gap"])


def _bundle(event_id, side, elig, effs, shares, sum_eff, gap, net, min_depth):
    bundle_id = f"{event_id}|{side}"
    return {
        "event_id": event_id,
        "bundle_id": bundle_id,
        "side": side,
        "shares": shares,
        "sum_eff": sum_eff,
        "gap": gap,
        "net_gap": net,
        "min_depth": min_depth,
        "n_outcomes": len(elig),
        "legs": [
            {"market_id": m.market_id, "token_id": m.yes_token_id,
             "price": e, "shares": shares}
            for m, e in zip(elig, effs)
        ],
        "meta": {"fees": sum_eff - gap, "side": side, "bundle_id": bundle_id},
    }


def _log_gap(scan_id, b):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS arb_gap_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, scan_id INTEGER,
            event_id TEXT, side TEXT, n_outcomes INTEGER, sum_eff REAL,
            gap REAL, net_gap REAL, min_depth REAL, qualifies INTEGER
        )"""
    )
    conn.execute(
        """INSERT INTO arb_gap_log(ts, scan_id, event_id, side, n_outcomes,
           sum_eff, gap, net_gap, min_depth, qualifies)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), scan_id,
         b["event_id"], b["side"], b["n_outcomes"], b["sum_eff"], b["gap"],
         b["net_gap"], b["min_depth"], int(b["net_gap"] > ARB_MIN_GAP)),
    )
    conn.commit()
    conn.close()


def scan_arb(scan_id):
    seen_events = set()
    bundles = []
    for tag in ARB_SCAN_TAGS:
        events = markets.fetch_events({"tag": tag, "closed": "false", "active": "true",
                                       "limit": 100})
        for ev in events or []:
            ev_id = str(ev.get("id", ""))
            if not ev_id or ev_id in seen_events:
                continue
            seen_events.add(ev_id)
            sibs = markets.sibling_markets(ev_id, with_book=True)
            for m in sibs:
                store_snapshot(EDGE, m, {"scan_id": scan_id, "event_id": ev_id})
            b = compute_bundle(sibs, event_id=ev_id)
            if b:
                _log_gap(scan_id, b)
                if b["net_gap"] > ARB_MIN_GAP:
                    b["scan_id"] = scan_id
                    bundles.append(b)
    return bundles
