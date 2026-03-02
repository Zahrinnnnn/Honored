#!/usr/bin/env python3
"""
get_status.py — GOJO tool script

Returns system status as JSON.

Usage:
    python3 get_status.py --json
    python3 get_status.py --alerts-only --json   (heartbeat: returns + marks unsent alerts)

Reads HONORED_DB env var for SQLite path.
Falls back to paper.db if PAPER_MODE=true, honored.db otherwise.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone


def db_path() -> str:
    path = os.getenv("HONORED_DB")
    if path:
        return path
    paper = os.getenv("PAPER_MODE", "true").lower() != "false"
    return "paper.db" if paper else "honored.db"


def safe_get(conn: sqlite3.Connection, table: str, key: str, default=None):
    try:
        row = conn.execute(
            f"SELECT value FROM {table} WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else default
    except Exception:
        return default


def get_account_row(conn: sqlite3.Connection) -> dict:
    try:
        row = conn.execute(
            "SELECT balance, equity, peak_balance, current_dd_pct, open_positions "
            "FROM account ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            return {
                "balance":        round(float(row[0] or 0), 2),
                "equity":         round(float(row[1] or 0), 2),
                "peak_balance":   round(float(row[2] or 0), 2),
                "drawdown_pct":   round(float(row[3] or 0), 2),
                "open_positions": int(row[4] or 0),
            }
    except Exception:
        pass
    return {
        "balance": 0.0, "equity": 0.0, "peak_balance": 0.0,
        "drawdown_pct": 0.0, "open_positions": 0,
    }


def get_alerts(conn: sqlite3.Connection, mark_sent: bool = True) -> list:
    try:
        rows = conn.execute(
            "SELECT id, alert_type, message, created_at "
            "FROM alert_queue WHERE sent = 0 ORDER BY id ASC"
        ).fetchall()
        alerts = [
            {"id": r[0], "alert_type": r[1], "message": r[2], "created_at": r[3]}
            for r in rows
        ]
        if alerts and mark_sent:
            ids = [a["id"] for a in alerts]
            conn.execute(
                f"UPDATE alert_queue SET sent = 1 WHERE id IN ({','.join('?' * len(ids))})",
                ids,
            )
            conn.commit()
        return alerts
    except Exception:
        return []


def main():
    alerts_only = "--alerts-only" in sys.argv

    try:
        path = db_path()
        conn = sqlite3.connect(path)

        if alerts_only:
            alerts = get_alerts(conn, mark_sent=True)
            conn.close()
            print(json.dumps({"status": "ok", "data": {"alerts": alerts}}))
            return

        # Full status
        account = get_account_row(conn)

        system_status    = safe_get(conn, "system_state", "status",              "RUNNING")
        pause_flag       = safe_get(conn, "system_state", "pause_flag",           "false")
        halt_flag        = safe_get(conn, "system_state", "halt_flag",            "false")
        emergency_flag   = safe_get(conn, "system_state", "emergency_halt_flag",  "false")

        consec_losses    = safe_get(conn, "trading_state", "consecutive_losses",  "0")
        last_result      = safe_get(conn, "trading_state", "last_trade_result",   None)
        last_signal_raw  = safe_get(conn, "trading_state", "last_signal",         None)
        last_decision    = safe_get(conn, "trading_state", "last_risk_decision",  None)

        current_session  = safe_get(conn, "session_info",  "current_session",     "unknown")
        mins_to_news     = safe_get(conn, "session_info",  "minutes_to_next_news","999")
        asian_high       = safe_get(conn, "session_info",  "asian_range_high",    "0")
        asian_low        = safe_get(conn, "session_info",  "asian_range_low",     "0")

        alerts           = get_alerts(conn, mark_sent=False)
        conn.close()

        # Parse last signal if available
        last_signal = None
        if last_signal_raw:
            try:
                sig = json.loads(last_signal_raw)
                last_signal = {
                    "model":       sig.get("model"),
                    "direction":   sig.get("direction"),
                    "entry_price": sig.get("entry_price"),
                    "sl_price":    sig.get("sl_price"),
                    "tp_price":    sig.get("tp_price"),
                    "regime":      sig.get("regime"),
                    "status":      sig.get("status"),
                    "timestamp":   sig.get("timestamp"),
                }
            except Exception:
                pass

        # Determine top-level state
        if emergency_flag == "true":
            state = "EMERGENCY_HALT"
        elif halt_flag == "true":
            state = "HALT"
        elif pause_flag == "true":
            state = "PAUSED"
        else:
            state = "RUNNING"

        data = {
            "state":            state,
            "account":          account,
            "consecutive_losses": int(consec_losses or 0),
            "last_trade_result": last_result,
            "last_signal":      last_signal,
            "last_decision":    last_decision,
            "session":          current_session,
            "minutes_to_news":  float(mins_to_news or 999),
            "asian_range":      {
                "high": float(asian_high or 0),
                "low":  float(asian_low  or 0),
            },
            "pending_alerts":   len(alerts),
            "timestamp":        datetime.now(timezone.utc).isoformat(),
        }

        print(json.dumps({"status": "ok", "data": data}))

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
