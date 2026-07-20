"""Live maintain/settle. Runs via run_live.py --job maintain-live. Reuses
paper modules only for pure/read-only pieces: settle._fill_pnl (the PnL
algebra) and markets.fetch_resolution (read-only Gamma HTTP). Settles on
Gamma closed/outcomePrices (what actually pays), NOT Open-Meteo observed
highs — divergence from paper is the probe's point.

No live code writes to either paper DB. All state → polymarket_bot_live.db.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import live_engine as le
import live_executor
import markets
import settle
from config import (
    DAILY_PULSE_HOUR_HKT,
    LIVE_KEY_ENV,
    LIVE_MATIC_ALERT,
    LIVE_ORDER_STALE_MIN,
)

HKT = timezone(timedelta(hours=8))

_LF_COLS = """ts, order_id, clob_trade_id, market_id, exec_token_id, side,
  price, size, fee, fill_ts, raw_json"""
_LF_PH = ",".join(["?"] * 11)

_LS_COLS = """ts, market_id, condition_id, city, date, bucket_key,
  resolved_yes, pnl, raw_json"""
_LS_PH = ",".join(["?"] * 9)

_LB_PH = ",".join(["?"] * 4)  # ts, usdc, matic, source


def job_maintain_live():
    le.init_live_db()
    if os.path.exists(le._halt_live_path()):
        return   # HALT blocks both jobs; existing positions managed on resume
    conn = le.get_live_db()
    client = None
    try:
        try:
            client = live_executor.get_client()
        except live_executor.LiveKeyMissing:
            client = None   # still settle + balance + pulse (no SDK needed)
        rec_n = canc_n = 0
        if client is not None:
            rec_n = reconcile_fills(client, conn)
            canc_n = cancel_stale(client, conn)
        settled = settle_resolved(conn)
        check_balances(conn)
        daily_pulse(conn)
        conn.commit()
        import engine
        engine.notify(
            f"[A-LIVE] maintain: reconciled={rec_n} cancelled={canc_n} "
            f"settled={settled}")
    finally:
        conn.close()


def reconcile_fills(client, conn):
    """Poll get_open_orders + get_trades; insert new live_fills (YES-space for
    NO buys); update order statuses incl. partial. Returns count of new fills."""
    rows = conn.execute(
        """SELECT id, clob_order_id, market_id, exec_token_id, signal_side, size
           FROM live_orders WHERE status IN ('posted','open','partial')
           AND clob_order_id != ''"""
    ).fetchall()
    if not rows:
        return 0
    try:
        open_orders = client.get_open_orders() or []
        trades = client.get_trades() or []
    except Exception:
        return 0
    open_by_id = {str(o.get("id")): o for o in open_orders if isinstance(o, dict)}
    seen_trade_ids = {r["clob_trade_id"] for r in conn.execute(
        "SELECT clob_trade_id FROM live_fills").fetchall() if r["clob_trade_id"]}
    order_by_clob = {str(r["clob_order_id"]): r for r in rows}
    new_fills = 0
    for t in trades:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "")
        if not tid or tid in seen_trade_ids:
            continue
        # match trade → live order (by taker_order_id, else asset_id+market)
        clob_id = str(t.get("taker_order_id") or t.get("order_id") or "")
        order = order_by_clob.get(clob_id)
        if order is None:
            asset = str(t.get("asset_id") or "")
            market = str(t.get("market") or "")
            for r in rows:
                if r["exec_token_id"] == asset and r["market_id"] == market:
                    order = r
                    break
        if order is None:
            continue
        # YES-space mapping: buy signal → 'buy' at trade price; sell (NO buy) → 'sell' at 1−p_no
        t_price = float(t.get("price") or 0.0)
        if order["signal_side"] == "buy":
            fill_side, fill_price = "buy", t_price
        else:
            fill_side, fill_price = "sell", 1.0 - t_price
        size = float(t.get("size") or 0.0)
        fee_rate_bps = float(t.get("fee_rate_bps") or 0.0)
        fee = size * t_price * fee_rate_bps / 10000.0
        conn.execute(
            f"INSERT INTO live_fills({_LF_COLS}) VALUES({_LF_PH})",
            (le._now_iso(), order["id"], tid, order["market_id"],
             order["exec_token_id"], fill_side, fill_price, size, fee,
             str(t.get("match_time") or ""), json.dumps(t, default=str)[:800]),
        )
        seen_trade_ids.add(tid)
        new_fills += 1
    # update statuses
    for r in rows:
        oo = open_by_id.get(str(r["clob_order_id"]))
        if oo:
            matched = float(oo.get("size_matched") or 0.0)
            orig = float(r["size"] or 0.0)
            if orig > 0 and matched >= orig - 1e-9:
                _set_status(conn, r["id"], "filled")
            elif matched > 0:
                _set_status(conn, r["id"], "partial")
            else:
                _set_status(conn, r["id"], "open")
        else:
            # not open anymore — filled earlier or cancelled
            n_fills = conn.execute(
                "SELECT COUNT(*) FROM live_fills WHERE order_id=?", (r["id"],)
            ).fetchone()[0]
            _set_status(conn, r["id"], "filled" if n_fills else "cancelled")
    conn.commit()
    return new_fills


def cancel_stale(client, conn):
    """Cancel orders unfilled > LIVE_ORDER_STALE_MIN. Partials keep their fills."""
    from py_clob_client_v2.clob_types import OrderPayload
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=LIVE_ORDER_STALE_MIN)).isoformat()
    rows = conn.execute(
        """SELECT id, clob_order_id, ts FROM live_orders
           WHERE status IN ('posted','open') AND ts < ? AND clob_order_id != ''""",
        (cutoff,),
    ).fetchall()
    n = 0
    for r in rows:
        try:
            client.cancel_order(OrderPayload(orderID=str(r["clob_order_id"])))
        except Exception:
            continue
        n_fills = conn.execute(
            "SELECT COUNT(*) FROM live_fills WHERE order_id=?", (r["id"],)
        ).fetchone()[0]
        _set_status(conn, r["id"], "partial" if n_fills else "cancelled")
        n += 1
    conn.commit()
    return n


def settle_resolved(conn):
    """For filled markets without settlement: Gamma fetch_resolution → if closed,
    pnl via settle._fill_pnl over live_fills; insert live_settlements; notify."""
    rows = conn.execute(
        """SELECT DISTINCT f.market_id,
           (SELECT condition_id FROM live_orders o WHERE o.id=f.order_id) AS condition_id,
           (SELECT city FROM live_orders o WHERE o.id=f.order_id) AS city,
           (SELECT market_date FROM live_orders o WHERE o.id=f.order_id) AS mdate,
           (SELECT bucket_key FROM live_orders o WHERE o.id=f.order_id) AS bucket
           FROM live_fills f
           WHERE f.market_id NOT IN (SELECT market_id FROM live_settlements)"""
    ).fetchall()
    settled = 0
    for r in rows:
        market_id = r["market_id"]
        if not market_id:
            continue
        try:
            closed, outcome, prices = markets.fetch_resolution(market_id)
        except Exception:
            continue
        if not closed or outcome == "none":
            continue
        yes_won = (outcome == "yes")
        fills = conn.execute(
            "SELECT side, price, size FROM live_fills WHERE market_id=?",
            (market_id,),
        ).fetchall()
        pnl = 0.0
        for f in fills:
            pnl += settle._fill_pnl(f["side"], f["price"], f["size"], yes_won)
        conn.execute(
            f"INSERT INTO live_settlements({_LS_COLS}) VALUES({_LS_PH})",
            (le._now_iso(), market_id, r["condition_id"] or "", r["city"] or "",
             r["mdate"] or "", r["bucket"] or "", int(yes_won), pnl,
             json.dumps({"outcome": outcome, "prices": prices, "fills": len(fills)})),
        )
        settled += 1
        import engine
        verdict = "YES won" if yes_won else "NO won"
        engine.notify(
            f"[A-LIVE] settled {r['city'] or '?'} {r['mdate'] or '?'} "
            f"{verdict} pnl={pnl:+.2f}")
    conn.commit()
    return settled


def check_balances(conn):
    """Gas-wallet POL check (daily-throttled). Targets the EOA signer derived
    from POLY_PRIVATE_KEY, NOT the Polymarket proxy (POLY_FUNDER) — the proxy
    is a contract that holds 0 native POL by design; gas lives on the EOA.
    fetch_balances returns (None, None) on RPC failure → skip + no alert (safe
    direction: no false positive). The stored USDC is vestigial (the bankroll
    lives in the CTF exchange, not as an ERC-20 balanceOf)."""
    pk = os.environ.get(LIVE_KEY_ENV)
    if not pk:
        return
    try:
        from eth_account import Account
        gas_eoa = Account.from_key(pk).address
    except Exception:
        return
    usdc, matic = live_executor.fetch_balances(gas_eoa)
    if usdc is None and matic is None:
        return
    conn.execute(
        f"INSERT INTO live_balances(ts, usdc, matic, source) VALUES({_LB_PH})",
        (le._now_iso(), usdc or 0.0, matic or 0.0, "rpc"),
    )
    if matic is not None and matic < LIVE_MATIC_ALERT:
        today = datetime.now(HKT).strftime("%Y-%m-%d")
        last = le.live_meta_get(conn, "last_balance_alert", "")
        if last != today:
            le.live_meta_set(conn, "last_balance_alert", today)
            import engine
            engine.notify(
                f"[A-LIVE] MATIC low: {matic:.3f} (< {LIVE_MATIC_ALERT}). "
                f"Top up MATIC only (never the USDC bankroll).")


def daily_pulse(conn):
    now = datetime.now(timezone.utc).astimezone(HKT)
    if now.hour < DAILY_PULSE_HOUR_HKT:
        return
    today = now.strftime("%Y-%m-%d")
    last = le.live_meta_get(conn, "last_pulse_date", "")
    if last == today:
        return
    total = float(conn.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM live_settlements").fetchone()[0] or 0.0)
    n_fills = conn.execute("SELECT COUNT(*) FROM live_fills").fetchone()[0]
    n_set = conn.execute("SELECT COUNT(*) FROM live_settlements").fetchone()[0]
    n_open = conn.execute(
        "SELECT COUNT(*) FROM live_orders WHERE status IN ('posted','open','partial')"
    ).fetchone()[0]
    le.live_meta_set(conn, "last_pulse_date", today)
    import engine
    engine.notify(
        f"[A-LIVE] pulse {today} HKT: open={n_open} fills={n_fills} "
        f"settled={n_set} realized={total:+.2f}")


def _set_status(conn, order_id, status):
    conn.execute("UPDATE live_orders SET status=? WHERE id=?", (status, order_id))
