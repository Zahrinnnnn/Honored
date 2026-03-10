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

# Reuse helpers from get_status.py in the same directory
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


def main():
    try:
        conn = sqlite3.connect(db_path())

        account       = get_account_row(conn)
        today         = get_today_stats(conn)
        open_trades   = get_open_trades(conn)

        state           = "RUNNING"
        pause_flag      = safe_get(conn, "system_state", "pause_flag",          "false")
        halt_flag       = safe_get(conn, "system_state", "halt_flag",           "false")
        emergency_flag  = safe_get(conn, "system_state", "emergency_halt_flag", "false")
        if emergency_flag == "true":
            state = "EMERGENCY HALT"
        elif halt_flag == "true":
            state = "HALTED"
        elif pause_flag == "true":
            state = "PAUSED"

        session     = safe_get(conn, "session_info",  "current_session",      "unknown")
        regime      = safe_get(conn, "session_info",  "current_regime",       None)
        h4_bias     = safe_get(conn, "session_info",  "h4_bias",              None)
        bid         = safe_get(conn, "session_info",  "current_bid",          None)
        ask         = safe_get(conn, "session_info",  "current_ask",          None)
        spread      = safe_get(conn, "session_info",  "current_spread",       None)
        mins_news   = safe_get(conn, "session_info",  "minutes_to_next_news", "999")
        consec      = safe_get(conn, "trading_state", "consecutive_losses",   "0")
        last_dec    = safe_get(conn, "trading_state", "last_risk_decision",   None)
        last_sig_raw= safe_get(conn, "trading_state", "last_signal",          None)

        conn.close()

        now_utc = datetime.now(timezone.utc).strftime("%H:%M UTC")

        # Gold price line
        if bid and ask:
            try:
                gold_line = f"Gold: ${float(bid):.2f} – ${float(ask):.2f}"
                if spread:
                    gold_line += f"  (spread ${float(spread):.2f})"
            except Exception:
                gold_line = "Gold: price unavailable"
        else:
            gold_line = "Gold: no price yet (NANAMI starting up)"

        # Last signal
        sig_line = "Last signal: none yet"
        if last_sig_raw:
            try:
                s = json.loads(last_sig_raw)
                sig_line = (
                    f"Last signal: {s.get('model','')} {s.get('direction','')} "
                    f"@ ${float(s.get('entry_price',0)):.2f}  [{s.get('status','')}]"
                )
            except Exception:
                pass

        # Open trades
        if open_trades:
            ot_lines = [f"  #{t['id']} {t['model']} {t['direction']} entry=${t['entry_price']:.2f} lot={t['lot_size']:.2f}" for t in open_trades]
            ot_block = "Open trades:\n" + "\n".join(ot_lines)
        else:
            ot_block = "Open trades: none"

        # Today P&L sign
        pnl = today['total_pnl']
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"

        lines = [
            f"[{now_utc}] {state}",
            f"Balance: ${account['balance']:.2f}  |  DD: {account['drawdown_pct']:.1f}%",
            gold_line,
            f"Session: {session}  |  Regime: {regime or 'detecting...'}  |  H4: {h4_bias or '—'}",
            f"Today: {today['trades']} trades  {today['wins']}W {today['losses']}L  P&L: {pnl_str}",
            f"Consecutive losses: {consec}  |  Open positions: {account['open_positions']}",
            sig_line,
            f"Last decision: {last_dec or '—'}",
            f"News in: {float(mins_news or 999):.0f} min",
            ot_block,
        ]

        print("\n".join(lines))

    except Exception as exc:
        print(f"Error reading status: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
