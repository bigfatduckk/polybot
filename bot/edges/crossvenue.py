import json
import re
import sqlite3
from datetime import datetime, timezone

import httpx
import markets
from config import (
    BOT_DIR,
    CROSSVENUE_MIN_GAP,
    CROSSVENUE_TAGS,
    DB_PATH,
    KALSHI_BASE,
    tls_verify,
)

CV_GAPS_PATH = str(BOT_DIR / "data" / "crossvenue_gaps.jsonl")

EDGE = "crossvenue"
MATCH_MIN = 0.34
SPREAD_ESTIMATE = 0.01
KALSHI_FEE = 0.0
UA = {"User-Agent": "MarcusVaultBot/1.0"}


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize(q):
    q = (q or "").lower()
    q = re.sub(r"[^a-z0-9\s]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    stop = {"will", "the", "in", "on", "of", "a", "an", "for", "to", "be", "by", "and", "or"}
    return [w for w in q.split() if w not in stop and len(w) > 1]


def match_score(a, b):
    ta, tb = set(normalize(a)), set(normalize(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _kalshi_yes_mid(km):
    ya, yb = km.get("yes_ask"), km.get("yes_bid")
    try:
        if ya is not None and yb is not None:
            return (float(ya) + float(yb)) / 2.0
        if ya is not None:
            return float(ya)
        if yb is not None:
            return float(yb)
    except (TypeError, ValueError):
        pass
    last = km.get("last_price") or km.get("last_trade_price")
    try:
        return float(last) / 100.0 if last is not None and float(last) > 1.5 else float(last)
    except (TypeError, ValueError):
        return None


def compute_gap(pm_yes, kalshi_yes, pm_fee=0.0):
    if pm_yes is None or kalshi_yes is None:
        return None
    gap = abs(pm_yes - kalshi_yes)
    net = gap - pm_fee - KALSHI_FEE - SPREAD_ESTIMATE
    return {"gap": gap, "net_of_fees": net, "pm_yes": pm_yes, "kalshi_yes": kalshi_yes}


def fetch_kalshi_markets(tag):
    try:
        with httpx.Client(timeout=30, headers=UA, verify=tls_verify()) as c:
            r = c.get(f"{KALSHI_BASE}/markets",
                      params={"status": "open", "limit": 500})
            if r.status_code != 200:
                return []
            data = r.json()
            mkts = data.get("markets") if isinstance(data, dict) else data
            return mkts or []
    except Exception:
        return []


def _best_match(pm_question, kalshi_mkts):
    best = None
    best_score = 0.0
    for km in kalshi_mkts:
        title = km.get("title") or km.get("market_name") or km.get("question") or ""
        s = match_score(pm_question, title)
        if s > best_score:
            best_score = s
            best = km
    if best_score < MATCH_MIN:
        return None, 0.0
    return best, best_score


def _store_gap(pm_mid, k_mid, question, pm_yes, kalshi_yes, gap, net, meta):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """INSERT INTO cv_gaps(ts, polymarket_market_id, kalshi_market_id, question,
           pm_yes, kalshi_yes, gap, net_of_fees, snapshot_json)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (_now_iso(), pm_mid, k_mid, question, pm_yes, kalshi_yes, gap, net,
         json.dumps(meta, default=str)),
    )
    conn.commit()
    conn.close()
    with open(CV_GAPS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": _now_iso(), "pm": pm_mid, "kalshi": k_mid,
                            "q": question, "pm_yes": pm_yes, "kalshi_yes": kalshi_yes,
                            "gap": gap, "net": net}, default=str) + "\n")


def scan_crossvenue(scan_id):
    gaps = []
    for tag in CROSSVENUE_TAGS:
        pm_mkts = markets.fetch_markets(
            {"closed": "false", "active": "true", "tag": tag, "limit": 200},
            with_book=True,
        )
        k_mkts = fetch_kalshi_markets(tag)
        if not k_mkts:
            continue
        for m in pm_mkts:
            pm_yes = (m.best_bid + m.best_ask) / 2.0 if m.best_ask > m.best_bid else None
            if pm_yes is None:
                continue
            km, score = _best_match(m.question, k_mkts)
            if not km:
                continue
            k_yes = _kalshi_yes_mid(km)
            g = compute_gap(pm_yes, k_yes, m.fee_rate)
            if not g:
                continue
            k_id = str(km.get("market_name") or km.get("id") or km.get("ticker") or "")
            _store_gap(m.market_id, k_id, m.question, pm_yes, k_yes,
                       g["gap"], g["net_of_fees"],
                       {"match_score": round(score, 3), "tag": tag, "scan_id": scan_id})
            gaps.append(g | {"pm_market_id": m.market_id, "kalshi_id": k_id,
                             "question": m.question})
    return gaps


def notify_threshold_gaps(gaps):
    hits = [g for g in gaps if g["net_of_fees"] > CROSSVENUE_MIN_GAP]
    if not hits:
        return None
    lines = [f"cross-venue: {len(hits)} gaps > {CROSSVENUE_MIN_GAP:.0%} net"]
    for g in hits[:8]:
        lines.append(f"  {g['question'][:60]} pm={g['pm_yes']:.2f} k={g['kalshi_yes']:.2f} net={g['net_of_fees']:.3f}")
    return "\n".join(lines)
