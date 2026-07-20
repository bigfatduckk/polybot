"""Telegram `halt`/`unhalt` control for the live arm. Toggles the HALT_LIVE
file (the same flag run_live.py checks at the top of weather-live).

Stateless two-word confirm: `halt` alone prompts; `halt yes` sets; `unhalt
yes` clears. Owner-only (info.py's chat-id check runs before this). Cannot
toggle LIVE_DRY_RUN (env, shell-only), so blast radius is bounded to
pausing/resuming the live arm — it cannot escalate dry-run to real trading.
Every set/clear is audited to live_halts + live_ticks.
"""
import os

import config
import live_engine as le
import live_positions as lp


def _audit(conn, action):
    if conn is None:
        return
    try:
        le.record_halt(conn, f"telegram:{action}")
        le.log_tick(conn, "control", f"halt:{action}", {"via": "telegram"})
        conn.commit()
    except Exception:
        pass


def handle_control(parts):
    """parts: lowercased command words, e.g. ['halt','yes']. Returns reply."""
    if not parts:
        return "[A-LIVE] control: usage 'halt yes' / 'unhalt yes'"
    cmd = parts[0]
    conf = parts[1] if len(parts) > 1 else ""
    conn = lp._connect()
    if cmd == "halt":
        if conf != "yes":
            return "[A-LIVE] type 'halt yes' to set HALT_LIVE (pauses live arm)"
        open(config.HALT_LIVE_FILE, "a").close()
        _audit(conn, "set")
        return "[A-LIVE] HALT_LIVE SET — live arm skips on next tick"
    if cmd == "unhalt":
        if conf != "yes":
            return "[A-LIVE] type 'unhalt yes' to clear HALT_LIVE (resumes live arm)"
        if os.path.exists(config.HALT_LIVE_FILE):
            os.remove(config.HALT_LIVE_FILE)
        _audit(conn, "cleared")
        return "[A-LIVE] HALT_LIVE CLEARED — live arm resumes next tick"
    return "[A-LIVE] control: usage 'halt yes' / 'unhalt yes'"
