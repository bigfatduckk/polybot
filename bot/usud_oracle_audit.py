import json
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

import markets
from config import DB_PATH

TICKERS = {
    "SPY": ("spy", "SPY"),
    "SPX": ("spx", "^GSPC"),
    "DJIA": ("djia", "^DJI"),
    "NVDA": ("nvda", "NVDA"),
    "TSLA": ("tsla", "TSLA"),
}
MONTHS = ["january", "february", "march", "april", "may", "june",
          "july", "august", "september", "october", "november", "december"]

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def _slug_for(prefix, d):
    return f"{prefix}-up-or-down-on-{MONTHS[d.month - 1]}-{d.day}-{d.year}"


def stored_raw(market_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    r = conn.execute(
        "SELECT snapshot_json, end_date, question FROM pm_snapshots "
        "WHERE market_id=? ORDER BY id DESC LIMIT 1",
        (market_id,),
    ).fetchone()
    conn.close()
    if not r:
        return None
    try:
        raw = json.loads(r["snapshot_json"]) if r["snapshot_json"] else {}
    except (TypeError, ValueError):
        raw = {}
    return {"raw": raw, "end_date": r["end_date"], "question": r["question"]}


def yahoo_meta(yf_sym):
    try:
        with httpx.Client(timeout=20, headers=UA) as c:
            r = c.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/" + yf_sym,
                params={"interval": "5m", "range": "1d"},
            )
        if r.status_code != 200:
            return {"error": f"http {r.status_code}"}
        meta = (r.json().get("chart", {}).get("result") or [{}])[0].get("meta") or {}
        return {
            "spot": meta.get("regularMarketPrice"),
            "chartPreviousClose": meta.get("chartPreviousClose"),
            "previousClose": meta.get("previousClose"),
            "exchange": meta.get("exchangeName"),
            "symbol": meta.get("symbol"),
        }
    except Exception as e:
        return {"error": str(e)}


def main():
    out_path = "data/usud_resolution_audit.txt"
    lines = []
    def p(s=""):
        print(s)
        lines.append(s)

    p("=== USUD ORACLE AUDIT (stored pm_snapshots + live gamma + Yahoo cross-check) ===")
    p(f"run ts: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT DISTINCT ticker, market_id, end_date, question FROM usud_quotes "
        "ORDER BY ticker, end_date DESC"
    ).fetchall()
    conn.close()
    by_ticker = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(dict(r))

    today = datetime.now(ZoneInfo("America/New_York")).date()
    d = today
    while d.weekday() >= 5:
        d -= timedelta(days=1)

    for name in TICKERS:
        prefix, yf = TICKERS[name]
        p("")
        p("=" * 72)
        p(f"{name}  (yahoo sym {yf})  audit date={d} (today={today})")
        p("=" * 72)
        ms = by_ticker.get(name, [])
        if ms:
            m = ms[0]
            s = stored_raw(m["market_id"])
            p(f"[stored] market_id={m['market_id']} end_date={m['end_date']}")
            p(f"[stored] question: {m['question']}")
            if s and s["raw"]:
                raw = s["raw"]
                p(f"[stored] market.endDate: {raw.get('endDate')}")
                p(f"[stored] market.resolutionSource: {raw.get('resolutionSource')}")
                p(f"[stored] market.description:")
                p(str(raw.get("description") or "<none>"))
            else:
                p("[stored] <no snapshot_json for this market_id>")
        else:
            p("[stored] no usud_quotes rows for this ticker")
        slug = _slug_for(prefix, d)
        evs = markets.fetch_events({"slug": slug, "closed": "false", "active": "true"})
        src = "live-active"
        if not evs:
            evs = markets.fetch_events({"slug": slug, "closed": "true"})
            src = "live-closed"
        if evs:
            ev = evs[0]
            p(f"[{src}] slug={slug} event.endDate={ev.get('endDate')} "
              f"event.resolutionSource={ev.get('resolutionSource')}")
            mkts = ev.get("markets") or []
            if mkts:
                mk = mkts[0]
                p(f"[{src}] market.endDate={mk.get('endDate')} "
                  f"resolutionSource={mk.get('resolutionSource')}")
                p(f"[{src}] description:")
                p(str(mk.get("description") or "<none>"))
        else:
            p(f"[live] no event found for slug {slug}")
        ym = yahoo_meta(yf)
        p(f"[yahoo] {ym}")

    p("")
    p("=== FALSIFICATION CHECKLIST (judge from text above) ===")
    p("(a) resolves on official regular-session CLOSE (16:00 ET = 20:00 UTC summer / 21:00 UTC winter)?")
    p("(b) which exchange/index provider? (SPY=NYSE Arca, SPX=Cboe, DJIA=S&P DJI, NVDA/TSLA=Nasdaq)")
    p("(c) close-vs-PREVIOUS-TRADING-DAY-close (not open/vwap/intraday/after-hours)?")
    p("(d) Yahoo chartPreviousClose == market's stated prior close (date + split-adjust)?")
    p("(e) market.endDate == 16:00 ET close (20:00 UTC in EDT) NOT midnight UTC?")
    p("DOA if resolution source != Yahoo spot source AND not one-line-fixable.")
    Path = __import__("pathlib").Path
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
    p("")
    p(f"wrote {out_path}")


if __name__ == "__main__":
    main()
