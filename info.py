"""Telegram 'info' command responder (cron-polled, no daemon).

Cron runs this every ~2 min. It checks for new 'info' messages in the bot's
Telegram chat and replies with the positions report (open bets, settled
results, totals). Only the owner (TELEGRAM_CHAT_ID) gets a reply; messages
from anyone else are silently ignored — a public bot must not leak positions.

Latency: up to ~2 min between typing 'info' and the reply (next cron tick).
Swap for an always-on systemd listener if instant response is needed.

cron line:
  */2 * * * * /root/polybot/.venv/bin/python /root/polybot/bot/info.py >> /root/polybot/info.log 2>&1
"""
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

from config import TELEGRAM_TOKEN_ENV, TELEGRAM_CHAT_ID_ENV
from positions import _connect, format_report

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
        if text.lower() in ("info", "/info") and chat_id == owner_chat:
            report = format_report(conn)
            _send(token, owner_chat, report)

    if max_uid > last:
        _set_last_update_id(conn, max_uid)
    conn.close()


if __name__ == "__main__":
    main()
