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
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

_FF_URL  = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
_EASTERN = ZoneInfo("America/New_York")

# Maps FF country tag → display currency label
_COUNTRY_CURRENCY = {
    "USD": "USD",
    "EUR": "EUR",
    "GBP": "GBP",
    "JPY": "JPY",
    "CHF": "CHF",
    "AUD": "AUD",
    "CAD": "CAD",
    "NZD": "NZD",
}
# Countries whose news directly moves XAUUSD
_GOLD_MOVERS = {"USD", "EUR"}


def _parse_ff_time(date_str: str, time_str: str):
    """Convert ForexFactory date+time (Eastern) to UTC datetime. Returns None on failure."""
    s = time_str.strip().lower()
    if not s or s in ("all day", "tentative"):
        return None
    is_pm = s.endswith("pm")
    is_am = s.endswith("am")
    if not is_pm and not is_am:
        return None
    try:
        h, m = map(int, s[:-2].strip().split(":"))
        if is_pm and h != 12:
            h += 12
        elif is_am and h == 12:
            h = 0
        d = datetime.strptime(date_str.strip(), "%m-%d-%Y")
        naive = d.replace(hour=h, minute=m, second=0, microsecond=0)
        return naive.replace(tzinfo=_EASTERN).astimezone(timezone.utc)
    except Exception:
        return None


def fetch_upcoming_news(hours_ahead: float = 6.0) -> list:
    """
    Fetch medium+high impact events in the next hours_ahead from ForexFactory.
    Returns [] on any error (display-only, non-blocking).
    """
    if not _HAS_REQUESTS:
        return []
    try:
        resp = _requests.get(
            _FF_URL, timeout=8, headers={"User-Agent": "Mozilla/5.0"}
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content.decode("windows-1252", errors="replace"))
        now    = datetime.now(timezone.utc)
        cutoff = hours_ahead * 60.0
        result = []
        for ev in root.findall("event"):
            impact = (ev.findtext("impact") or "").strip().lower()
            if impact not in ("high", "medium"):
                continue
            country  = (ev.findtext("country") or "").strip().upper()
            title    = (ev.findtext("title")   or "").strip()
            date_str = (ev.findtext("date")    or "").strip()
            time_str = (ev.findtext("time")    or "").strip()
            dt_utc   = _parse_ff_time(date_str, time_str)
            if dt_utc is None:
                continue
            diff_min = (dt_utc - now).total_seconds() / 60.0
            if 0 <= diff_min <= cutoff:
                currency = _COUNTRY_CURRENCY.get(country, country)
                result.append({
                    "event":       title,
                    "currency":    currency,
                    "impact":      impact,
                    "minutes_away": round(diff_min, 1),
                    "affects_gold": country in _GOLD_MOVERS,
                })
        result.sort(key=lambda e: e["minutes_away"])
        return result
    except Exception:
        return []


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


def get_today_stats(conn: sqlite3.Connection) -> dict:
    """Today's closed trade stats: P&L, win/loss count, by model."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        rows = conn.execute(
            "SELECT model, result, pnl FROM trades "
            "WHERE date = ? AND result IS NOT NULL",
            (today,),
        ).fetchall()
    except Exception:
        rows = []

    total_pnl = 0.0
    wins = 0
    losses = 0
    by_model: dict = {}
    for model, result, pnl in rows:
        p = float(pnl or 0)
        total_pnl += p
        if result == "WIN":
            wins += 1
        else:
            losses += 1
        m = by_model.setdefault(model or "UNKNOWN", {"wins": 0, "losses": 0, "pnl": 0.0})
        m["pnl"] = round(m["pnl"] + p, 2)
        if result == "WIN":
            m["wins"] += 1
        else:
            m["losses"] += 1

    return {
        "total_pnl":   round(total_pnl, 2),
        "wins":        wins,
        "losses":      losses,
        "trades":      wins + losses,
        "by_model":    by_model,
    }


def get_open_trades(conn: sqlite3.Connection) -> list:
    """Returns open (not yet closed) paper trades."""
    try:
        rows = conn.execute(
            "SELECT id, model, direction, entry_price, sl_price, tp_price, "
            "lot_size, risk_amount, time FROM trades WHERE result IS NULL"
        ).fetchall()
        return [
            {
                "id":          r[0],
                "model":       r[1],
                "direction":   r[2],
                "entry_price": round(float(r[3] or 0), 2),
                "sl_price":    round(float(r[4] or 0), 2),
                "tp_price":    round(float(r[5] or 0), 2),
                "lot_size":    round(float(r[6] or 0), 2),
                "risk_amount": round(float(r[7] or 0), 2),
                "opened_at":   r[8],
            }
            for r in rows
        ]
    except Exception:
        return []


def get_session_counts(conn: sqlite3.Connection) -> dict:
    """Current session trade counts from session_trades table."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        rows = conn.execute(
            "SELECT session, model, count FROM session_trades WHERE date = ?",
            (today,),
        ).fetchall()
        result: dict = {}
        for session, model, count in rows:
            key = f"{session}/{model}"
            result[key] = int(count or 0)
        return result
    except Exception:
        return {}


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
        h1_bias          = safe_get(conn, "session_info",  "h1_bias",             None)
        h4_bias          = safe_get(conn, "session_info",  "h4_bias",             None)
        combined_bias    = safe_get(conn, "session_info",  "combined_bias",       None)
        mins_to_news     = safe_get(conn, "session_info",  "minutes_to_next_news","999")
        asian_high       = safe_get(conn, "session_info",  "asian_range_high",    "0")
        asian_low        = safe_get(conn, "session_info",  "asian_range_low",     "0")
        current_bid      = safe_get(conn, "session_info",  "current_bid",         None)
        current_ask      = safe_get(conn, "session_info",  "current_ask",         None)
        current_spread   = safe_get(conn, "session_info",  "current_spread",      None)

        today_stats      = get_today_stats(conn)
        open_trades      = get_open_trades(conn)
        session_counts   = get_session_counts(conn)
        alerts           = get_alerts(conn, mark_sent=False)
        conn.close()

        upcoming_news = fetch_upcoming_news(hours_ahead=6.0)

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
                    "h1_bias":     sig.get("h1_bias"),
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

        # Gold price snapshot (written by NANAMI every 60s — None if NANAMI not running)
        gold_price = None
        if current_bid and current_ask:
            try:
                gold_price = {
                    "bid":    round(float(current_bid), 2),
                    "ask":    round(float(current_ask), 2),
                    "spread": round(float(current_spread or 0), 2),
                }
            except (ValueError, TypeError):
                pass

        data = {
            "state":              state,
            "account":            account,
            "gold_price":         gold_price,
            "htf_bias":           {"h1": h1_bias, "h4": h4_bias, "combined": combined_bias},
            "session":            current_session,
            "consecutive_losses": int(consec_losses or 0),
            "last_trade_result":  last_result,
            "last_signal":        last_signal,
            "last_decision":      last_decision,
            "today":              today_stats,
            "open_trades":        open_trades,
            "session_counts":     session_counts,
            "minutes_to_news":    float(mins_to_news or 999),
            "upcoming_news":      upcoming_news,
            "asian_range":        {
                "high": float(asian_high or 0),
                "low":  float(asian_low  or 0),
            },
            "pending_alerts":     len(alerts),
            "timestamp":          datetime.now(timezone.utc).isoformat(),
        }

        print(json.dumps({"status": "ok", "data": data}))

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
