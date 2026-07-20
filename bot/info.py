"""Telegram command responder (cron-polled, no daemon).

Cron runs this every ~2 min. It polls the bot's Telegram chat for new
owner messages and replies. Only the owner (TELEGRAM_CHAT_ID) gets a
reply; messages from anyone else are silently ignored.

Commands:
  info              open positions + settled + totals
  pnl               paper PnL per edge (totals only)
  pnl live          live-arm PnL (open + last 10 settled + realized)
  opens             all open bets grouped by edge
  opens live        open LIVE bets only
  live              one-glance live-arm health (HALT/DRY_RUN/counts/gas/last tick)
  ticks [N]         last N live_ticks rows (evaluated/gated/skip history; default 10)
  gas               last 3 live_balances reads (gas/usdc trend)
  halt yes          set HALT_LIVE (pauses live arm; `halt` alone prompts)
  unhalt yes        clear HALT_LIVE (resumes live arm; `unhalt` alone prompts)
  settled           bets settled today (HKT)
  settled YYYY-MM-DD bets settled on that date (HKT)

Latency: up to ~2 min between typing a command and the reply (next cron
tick). Swap for an always-on systemd listener if instant response is needed.

cron line:
  */2 * * * * /root/polybot/.venv/bin/python /root/polybot/bot/info.py >> /root/polybot/info.log 2>&1
"""
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

from config import TELEGRAM_TOKEN_ENV, TELEGRAM_CHAT_ID_ENV, INST_TAG
from positions import (
    _connect,
    format_report,
    format_totals,
    format_open_all,
    format_pnl_both,
    format_settled_day,
)

ENV_PATH = Path(__file__).resolve().parent / ".env"
META_KEY = "telegram_last_update_id"
API = "https://api.telegram.org/bot{token}/{method}"


def _get_last_update_id(conn):
    r = conn.execute("SELECT v FROM meta WHERE k=?", (META_KEY,)).fetchone()
    return int(r["v"]) if r and r["v"].isdigit() else 0


def _set_last_update_id(conn, uid):
    conn.execute(
        "INSERT INTO meta(k,v) VALUES(?,?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (META_KEY, str(uid)),
    )
    conn.commit()


def _send(token, chat_id, text):
    if not text.startswith("["):
        text = f"[{INST_TAG}] {text}"
    if len(text) > 4000:
        text = text[:3990] + "\n...[truncated]"
    try:
        httpx.get(API.format(token=token, method="sendMessage"),
                  params={"chat_id": chat_id, "text": text}, timeout=30)
    except Exception:
        pass


def main():
    load_dotenv(ENV_PATH)
    token = os.environ.get(TELEGRAM_TOKEN_ENV)
    owner_chat = os.environ.get(TELEGRAM_CHAT_ID_ENV)
    if not token or not owner_chat:
        return
    conn = _connect()
    last = _get_last_update_id(conn)
    params = {"timeout": 0}
    if last:
        params["offset"] = last + 1
    try:
        r = httpx.get(API.format(token=token, method="getUpdates"), params=params, timeout=30)
        data = r.json()
    except Exception:
        conn.close()
        return
    if not data.get("ok"):
        conn.close()
        return

    max_uid = last
    for upd in data.get("result", []):
        uid = int(upd.get("update_id", 0))
        if uid > max_uid:
            max_uid = uid
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != owner_chat:
            continue
        low = text.lower()
        if low.startswith("/"):
            low = low[1:]
        parts = low.split()
        cmd = parts[0] if parts else ""
        arg = parts[1] if len(parts) > 1 else None
        if cmd == "info":
            _send(token, owner_chat, format_report(conn))
        elif cmd == "pnl":
            if arg == "live":
                try:
                    from live_positions import format_live_pnl
                    _send(token, owner_chat, format_live_pnl())
                except Exception as e:
                    _send(token, owner_chat, f"[A-LIVE] live pnl unavailable: {e}")
            else:
                _send(token, owner_chat, format_pnl_both())
        elif cmd == "opens":
            if arg == "live":
                try:
                    from live_positions import format_live_open
                    _send(token, owner_chat, format_live_open())
                except Exception as e:
                    _send(token, owner_chat, f"[A-LIVE] live opens unavailable: {e}")
            else:
                _send(token, owner_chat, format_open_all(conn))
        elif cmd == "live":
            try:
                from live_positions import format_live_health
                _send(token, owner_chat, format_live_health())
            except Exception as e:
                _send(token, owner_chat, f"[A-LIVE] live health unavailable: {e}")
        elif cmd == "ticks":
            try:
                from live_positions import format_live_ticks
                n = int(arg) if arg and arg.isdigit() else 10
                _send(token, owner_chat, format_live_ticks(n))
            except Exception as e:
                _send(token, owner_chat, f"[A-LIVE] ticks unavailable: {e}")
        elif cmd == "gas":
            try:
                from live_positions import format_live_gas
                _send(token, owner_chat, format_live_gas())
            except Exception as e:
                _send(token, owner_chat, f"[A-LIVE] gas unavailable: {e}")
        elif cmd in ("halt", "unhalt"):
            try:
                from live_control import handle_control
                _send(token, owner_chat, handle_control(parts))
            except Exception as e:
                _send(token, owner_chat, f"[A-LIVE] control unavailable: {e}")
        elif cmd == "settled":
            _send(token, owner_chat, format_settled_day(conn, arg))

    if max_uid > last:
        _set_last_update_id(conn, max_uid)
    conn.close()


if __name__ == "__main__":
    main()
