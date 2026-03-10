#!/usr/bin/env python3
"""
get_status_text.py — GOJO tool script

Returns system status as pre-formatted plain text for WhatsApp.
GOJO must send this output VERBATIM — no reformatting, no paraphrasing.

Usage:
    python3 get_status_text.py
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)
from get_status import (
    _load_honored_env,
    db_path,
    safe_get,
    get_account_row,
    get_today_stats,
    get_open_trades,
)

_load_honored_env()

# ── formatting helpers ────────────────────────────────────────────────────────

_STATE_ICON = {
    "RUNNING":        "🟢",
    "PAUSED":         "🟡",
    "HALTED":         "🔴",
    "EMERGENCY HALT": "🚨",
}

_REGIME_LABEL = {
    "BULLISH_GRIND":  "Bullish Grind",
    "BEARISH_GRIND":  "Bearish Grind",
    "TIGHT_RANGE":    "Tight Range",
    "BULLISH_BLOWOFF":"Bullish Blowoff",
    "BEARISH_PANIC":  "Bearish Panic",
    "TOXIC_CHOP":     "Toxic Chop",
}

_SESSION_LABEL = {
    "LONDON_OPEN":     "London Open",
    "LONDON_BREAKOUT": "London Breakout",
    "NY_OVERLAP":      "NY Overlap",
    "NY_CLOSE":        "NY Close",
    "ASIAN_BLACKOUT":  "Asian Blackout",
    "NONE":            "—",
}

_H4_LABEL = {
    "BULLISH": "↑ Bullish",
    "BEARISH": "↓ Bearish",
    "NEUTRAL": "→ Neutral",
}


def _fmt_regime(r):
    return _REGIME_LABEL.get(r, r.replace("_", " ").title()) if r else "Detecting..."


def _fmt_session(s):
    return _SESSION_LABEL.get(s, s.replace("_", " ").title()) if s else "—"


def _fmt_h4(h):
    return _H4_LABEL.get(h, h) if h else "—"


def _fmt_pnl(pnl):
    return f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"


def main():
    try:
        conn = sqlite3.connect(db_path())

        account     = get_account_row(conn)
        today       = get_today_stats(conn)
        open_trades = get_open_trades(conn)

        # ── system state ──────────────────────────────────────────────────────
        state = "RUNNING"
        if safe_get(conn, "system_state", "emergency_halt_flag", "false") == "true":
            state = "EMERGENCY HALT"
        elif safe_get(conn, "system_state", "halt_flag", "false") == "true":
            state = "HALTED"
        elif safe_get(conn, "system_state", "pause_flag", "false") == "true":
            state = "PAUSED"

        session   = safe_get(conn, "session_info",  "current_session",      "—")
        regime    = safe_get(conn, "session_info",  "current_regime",       None)
        h4_bias   = safe_get(conn, "session_info",  "h4_bias",              None)
        bid       = safe_get(conn, "session_info",  "current_bid",          None)
        ask       = safe_get(conn, "session_info",  "current_ask",          None)
        spread    = safe_get(conn, "session_info",  "current_spread",       None)
        mins_news = safe_get(conn, "session_info",  "minutes_to_next_news", "999")
        consec    = safe_get(conn, "trading_state", "consecutive_losses",   "0")
        last_dec  = safe_get(conn, "trading_state", "last_risk_decision",   None)
        last_sig_raw = safe_get(conn, "trading_state", "last_signal",       None)

        conn.close()

        now_utc = datetime.now(timezone.utc).strftime("%H:%M UTC")
        icon    = _STATE_ICON.get(state, "⚡")

        # ── gold price ────────────────────────────────────────────────────────
        if bid and ask:
            try:
                sp = f"  (spread ${float(spread):.2f})" if spread else ""
                gold_line = f"🥇  ${float(bid):,.2f} – ${float(ask):,.2f}{sp}"
            except Exception:
                gold_line = "🥇  price unavailable"
        else:
            gold_line = "🥇  no price yet"

        # ── last signal ───────────────────────────────────────────────────────
        sig_line = "—"
        if last_sig_raw:
            try:
                s = json.loads(last_sig_raw)
                model  = s.get("model", "").replace("_", " ").title()
                dirn   = s.get("direction", "")
                price  = float(s.get("entry_price", 0))
                status = s.get("status", "")
                sig_line = f"{model} {dirn} @ ${price:,.2f}  [{status}]"
            except Exception:
                pass

        # ── open trades ───────────────────────────────────────────────────────
        if open_trades:
            rows = []
            for t in open_trades:
                rows.append(
                    f"  #{t['id']}  {t['model'].replace('_',' ').title()}  "
                    f"{t['direction']}  @ ${t['entry_price']:.2f}  "
                    f"SL ${t['sl_price']:.2f}  lot {t['lot_size']:.2f}"
                )
            trades_block = "Open trades:\n" + "\n".join(rows)
        else:
            trades_block = "Open trades: none"

        pnl_str  = _fmt_pnl(today["total_pnl"])
        mins_int = int(float(mins_news or 999))
        news_str = f"{mins_int} min" if mins_int < 999 else "clear"

        lines = [
            f"⚡ *HONORED*  ·  {now_utc}",
            f"{icon} {state}",
            "",
            f"💰  ${account['balance']:,.2f}   DD {account['drawdown_pct']:.1f}%",
            gold_line,
            "",
            f"🕐  {_fmt_session(session)}",
            f"📊  {_fmt_regime(regime)}   ·   H4 {_fmt_h4(h4_bias)}",
            "",
            f"📅  Today    {today['trades']} trades  ·  {today['wins']}W {today['losses']}L  ·  {pnl_str}",
            f"🔴  Streak   {consec} losses  ·  {account['open_positions']} open",
            "",
            f"📡  Signal   {sig_line}",
            f"✅  Decision {last_dec or '—'}",
            f"📰  News     {news_str}",
            "",
            trades_block,
        ]

        print("\n".join(lines))

    except Exception as exc:
        print(f"Error reading status: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
