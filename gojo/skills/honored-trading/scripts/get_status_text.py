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
from datetime import datetime, timezone, timedelta

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

# ── Session schedule (GMT) — only active/tradeable windows ───────────────────
_SESSION_WINDOWS = [
    ("NY_OVERLAP",  12, 0, 16, 0),   # Model A active
    ("NY_CLOSE",    19, 0, 21, 0),   # informational (Model A disabled here)
]
_MODEL_A_SESSIONS = {"NY_OVERLAP"}

# ── Regime / state metadata ───────────────────────────────────────────────────
_REGIME_META = {
    "BULLISH_GRIND":   ("Bullish Grind",   "↑ slow uptrend",   True,  "BUY signals only"),
    "BEARISH_GRIND":   ("Bearish Grind",   "↓ slow downtrend", True,  "SELL signals only"),
    "TIGHT_RANGE":     ("Tight Range",     "↔ compression",    False, "Model B — disabled"),
    "BULLISH_BLOWOFF": ("Bullish Blowoff", "↑⚡ violent spike",False, "no trades"),
    "BEARISH_PANIC":   ("Bearish Panic",   "↓⚡ freefall",     False, "no trades"),
    "TOXIC_CHOP":      ("Toxic Chop",      "↔⚡ whipsaw",      False, "no trades"),
}

_STATE_ICON = {
    "RUNNING":        "🟢",
    "PAUSED":         "🟡",
    "HALTED":         "🔴",
    "EMERGENCY HALT": "🚨",
}

_H4_ICON = {"BULLISH": "↑", "BEARISH": "↓", "NEUTRAL": "→"}

SEP = "─" * 32


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pnl(v):
    return f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}"


def _mins_to_hhmm(mins):
    mins = int(mins)
    if mins <= 0:
        return "now"
    h, m = divmod(mins, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _next_session(now_utc):
    """Return (name, start_hh, start_mm, minutes_away) for the next active session."""
    now_mins = now_utc.hour * 60 + now_utc.minute
    candidates = []
    for name, sh, sm, eh, em in _SESSION_WINDOWS:
        start = sh * 60 + sm
        end   = eh * 60 + em
        if now_mins < start:
            candidates.append((start - now_mins, name, sh, sm))
        elif now_mins >= end:
            candidates.append((1440 - now_mins + start, name, sh, sm))
        # currently inside this window → skip
    if not candidates:
        return None
    candidates.sort()
    away, name, sh, sm = candidates[0]
    return name, sh, sm, away


def _regime_info(regime):
    """Return (label, note, tradeable, model_note)."""
    if regime in _REGIME_META:
        return _REGIME_META[regime]
    if regime:
        label = regime.replace("_", " ").title()
        return label, "", False, "unknown regime"
    return "Detecting...", "", False, "—"


def _flag_true(val):
    return str(val or "").lower() in ("true", "1")


def main():
    try:
        conn = sqlite3.connect(db_path())
        conn.row_factory = sqlite3.Row

        account     = get_account_row(conn)
        today       = get_today_stats(conn)
        open_trades = get_open_trades(conn)

        # ── system flags ──────────────────────────────────────────────────────
        e_halt    = _flag_true(safe_get(conn, "system_state", "emergency_halt_flag", "false"))
        halt      = _flag_true(safe_get(conn, "system_state", "halt_flag",           "false"))
        paused    = _flag_true(safe_get(conn, "system_state", "pause_flag",          "false"))
        state = "EMERGENCY HALT" if e_halt else "HALTED" if halt else "PAUSED" if paused else "RUNNING"

        # ── session & market ──────────────────────────────────────────────────
        session        = safe_get(conn, "session_info", "current_session",       "ASIAN_BLACKOUT")
        regime         = safe_get(conn, "session_info", "current_regime",        None)
        h4_bias        = safe_get(conn, "session_info", "h4_bias",               None)
        bid_raw        = safe_get(conn, "session_info", "current_bid",           None)
        ask_raw        = safe_get(conn, "session_info", "current_ask",           None)
        spread_raw     = safe_get(conn, "session_info", "current_spread",        None)
        mins_news_raw  = safe_get(conn, "session_info", "minutes_to_next_news",  "9999")
        struct_break   = safe_get(conn, "session_info", "structural_break_until","")
        asian_hi_raw   = safe_get(conn, "session_info", "asian_range_high",      None)
        asian_lo_raw   = safe_get(conn, "session_info", "asian_range_low",       None)

        # ── trading state ─────────────────────────────────────────────────────
        consec       = int(safe_get(conn, "trading_state", "consecutive_losses", "0") or 0)
        last_dec     = safe_get(conn, "trading_state", "last_risk_decision", None)
        last_sig_raw = safe_get(conn, "trading_state", "last_signal",        None)

        # ── session trade counts ──────────────────────────────────────────────
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cur = conn.cursor()
        cur.execute(
            "SELECT model, count FROM session_trades WHERE date=? AND session=?",
            (today_str, session),
        )
        session_counts = {r["model"]: r["count"] for r in cur.fetchall()}

        # ── upcoming news (next 6h) ───────────────────────────────────────────
        cur.execute(
            "SELECT value FROM trading_state WHERE key='upcoming_news_cache'"
        )
        news_cache_row = cur.fetchone()
        upcoming_news = []
        if news_cache_row and news_cache_row[0]:
            try:
                upcoming_news = json.loads(news_cache_row[0])
            except Exception:
                pass

        conn.close()

        now_utc  = datetime.now(timezone.utc)
        now_str  = now_utc.strftime("%H:%M UTC")
        date_str = now_utc.strftime("%a %d %b %Y")

        # ── compute values ────────────────────────────────────────────────────
        try:
            bid    = float(bid_raw)
            ask    = float(ask_raw)
            spread = float(spread_raw or 0)
            spread_ok = spread < 4.0
        except (TypeError, ValueError):
            bid = ask = spread = None
            spread_ok = None

        try:
            asian_hi = float(asian_hi_raw)
            asian_lo = float(asian_lo_raw)
            asian_range = asian_hi - asian_lo
        except (TypeError, ValueError):
            asian_hi = asian_lo = asian_range = None

        try:
            mins_news = float(mins_news_raw or 9999)
        except ValueError:
            mins_news = 9999
        news_clear = mins_news > 30

        # next session
        next_sess = _next_session(now_utc)

        # regime
        r_label, r_note, r_tradeable, r_model_note = _regime_info(regime)

        # dead zone check
        in_ny_overlap = session == "NY_OVERLAP"
        in_dead_zone  = in_ny_overlap and 14 <= now_utc.hour < 15

        # structural break
        struct_active = False
        struct_until_str = ""
        if struct_break:
            try:
                sb_until = datetime.fromisoformat(struct_break.replace("Z", "+00:00"))
                if sb_until > now_utc:
                    struct_active = True
                    struct_until_str = sb_until.strftime("%H:%M UTC")
            except Exception:
                pass

        # can Model A fire right now?
        model_a_eligible = (
            session in _MODEL_A_SESSIONS
            and r_tradeable
            and regime in ("BULLISH_GRIND", "BEARISH_GRIND")
            and not in_dead_zone
            and not struct_active
            and not halt
            and not e_halt
            and not paused
            and news_clear
            and state == "RUNNING"
        )

        # ── BUILD OUTPUT ──────────────────────────────────────────────────────
        lines = []
        L = lines.append

        # Header
        L(f"⚡ *HONORED*  ·  {now_str}  ·  {date_str}")
        L(SEP)

        # System state
        icon = _STATE_ICON.get(state, "⚡")
        L(f"{icon} SYSTEM: {state}")
        if e_halt:
            L("  ⚠ Emergency halt active — send 'emergency override' to unlock")
        elif halt:
            L("  ⚠ Soft halt — send 'override' to resume trading")
        elif paused:
            L("  ⚠ Trading paused — send 'resume' to unpause")

        # Account
        L("")
        L("💰 ACCOUNT")
        L(f"  Balance   ${account['balance']:,.2f}   Peak  ${account['peak_balance']:,.2f}")
        L(f"  Equity    ${account['equity']:,.2f}   DD    {account['drawdown_pct']:.1f}%")
        dd_pct = account['drawdown_pct']
        if dd_pct >= 40:
            L(f"  ⚠ Drawdown elevated — halt triggers at 50%")
        if account['open_positions'] > 0:
            L(f"  {account['open_positions']} position(s) open")

        # Gold price
        L("")
        L("📈 GOLD / XAUUSD")
        if bid is not None:
            L(f"  Bid  ${bid:,.2f}   Ask  ${ask:,.2f}")
            spread_flag = "✓" if spread_ok else "⚠ HIGH"
            L(f"  Spread  ${spread:.2f}  {spread_flag}  (limit $4.00)")
        else:
            L("  Price data unavailable")
        if asian_range is not None:
            L(f"  Asian range   ${asian_lo:,.2f} – ${asian_hi:,.2f}  (${asian_range:.2f} wide)")

        # Market / session
        L("")
        L("🕐 MARKET")

        session_label = session.replace("_", " ").title()
        _NS_LABELS = {"NY_OVERLAP": "NY Overlap", "NY_CLOSE": "NY Close", "LONDON_OPEN": "London Open"}
        if session == "ASIAN_BLACKOUT":
            if next_sess:
                ns_name, ns_h, ns_m, ns_away = next_sess
                ns_label = _NS_LABELS.get(ns_name, ns_name.replace("_", " ").title())
                L(f"  Session   🌙 {session_label}")
                L(f"            Next: {ns_label} in {_mins_to_hhmm(ns_away)}  ({ns_h:02d}:{ns_m:02d} UTC)")
            else:
                L(f"  Session   🌙 {session_label}")
        else:
            L(f"  Session   🟢 {session_label}  (active)")
            if in_dead_zone:
                L("            ⏸ Dead zone active  (14:00–15:00 UTC)  — no signals")

        L(f"  Regime    {r_label}  ·  {r_note}")
        L(f"  H4 Bias   {_H4_ICON.get(h4_bias, '?')} {h4_bias or '—'}")

        if struct_active:
            L(f"  🔴 Structural break cooldown until {struct_until_str}  (4h lockout)")

        # Model A status
        if model_a_eligible:
            direction = "BUY" if regime == "BULLISH_GRIND" else "SELL"
            L(f"  ✅ Model A ACTIVE — watching for {direction} signals")
        elif session in _MODEL_A_SESSIONS:
            if in_dead_zone:
                L("  ⏸ Model A paused  (dead zone)")
            elif not r_tradeable:
                L(f"  ⛔ Model A waiting  ({r_model_note})")
            elif struct_active:
                L("  ⛔ Model A blocked  (structural break cooldown)")
            else:
                L("  ⛔ Model A blocked  (check halts/news/spread)")
        else:
            L(f"  ⛔ Model A inactive  (session: {session_label})")

        # Today's performance
        L("")
        L("📊 TODAY'S PERFORMANCE")
        t_w = today.get("wins", 0)
        t_l = today.get("losses", 0)
        t_be = today.get("trades", 0) - t_w - t_l
        t_be = max(t_be, 0)
        L(f"  Trades   {today['trades']}   ({t_w}W / {t_l}L / {t_be}BE)")
        L(f"  P&L      {_pnl(today['total_pnl'])}")

        by_model = today.get("by_model", {})
        if by_model:
            L("  By model:")
            for m, stats in by_model.items():
                m_label = m.replace("_", " ").title()
                m_pnl   = _pnl(stats.get("pnl", 0))
                L(f"    {m_label}  {stats.get('wins',0)}W/{stats.get('losses',0)}L  {m_pnl}")

        # Session trade counts
        MODEL_LIMITS = {"OU_GRIND": 8, "OU_RANGE": 8, "ASIAN_BREAKOUT": 1}
        if session not in ("ASIAN_BLACKOUT", "NONE"):
            L("  Session counts:")
            for model, limit in MODEL_LIMITS.items():
                used = session_counts.get(model, 0)
                bar  = "█" * used + "░" * (limit - used)
                flag = " ⛔ LIMIT" if used >= limit else ""
                label = model.replace("_", " ").title()
                L(f"    {label:18s}  {used}/{limit}  [{bar}]{flag}")

        # Risk status
        L("")
        L("🛡 RISK")

        streak_icon = "✅" if consec == 0 else ("⚠" if consec < 3 else "🔴")
        L(f"  {streak_icon} Streak        {consec} / 3 consecutive losses")

        dd_icon = "✅" if dd_pct < 30 else ("⚠" if dd_pct < 45 else "🔴")
        L(f"  {dd_icon} Drawdown      {dd_pct:.1f}% / 50.0% (emergency halt)")

        if halt or e_halt:
            L("  🔴 HALT ACTIVE — no new trades")
        else:
            L("  ✅ No halts active")

        news_icon = "✅" if news_clear else "⚠"
        if mins_news >= 9999:
            news_detail = "no events in next 6h"
        elif not news_clear:
            news_detail = f"⚠ BLACKOUT — event in {_mins_to_hhmm(mins_news)} (30 min guard)"
        else:
            news_detail = f"clear  (next event in {_mins_to_hhmm(mins_news)})"
        L(f"  {news_icon} News          {news_detail}")

        if spread is not None:
            sp_icon = "✅" if spread_ok else "⚠"
            L(f"  {sp_icon} Spread        ${spread:.2f} / $4.00 limit")

        # Last signal & decision
        L("")
        L("📡 LAST SIGNAL")
        if last_sig_raw:
            try:
                s      = json.loads(last_sig_raw)
                s_model  = s.get("model", "").replace("_", " ").title()
                s_dir    = s.get("direction", "—")
                s_price  = float(s.get("entry_price", 0))
                s_sl     = float(s.get("sl_price", 0))
                s_tp     = float(s.get("tp_price", 0))
                s_status = s.get("status", "—")
                s_z      = s.get("ou_z", None)
                s_hl     = s.get("half_life", None)
                s_atr    = s.get("atr", None)
                L(f"  Model    {s_model}  {s_dir}")
                L(f"  Entry    ${s_price:,.2f}   SL ${s_sl:,.2f}   TP ${s_tp:,.2f}")
                if s_z is not None:
                    L(f"  OU z     {float(s_z):+.3f}   HL {s_hl} bars   ATR ${float(s_atr):.2f}" if s_atr else f"  OU z     {float(s_z):+.3f}   HL {s_hl} bars")
                L(f"  Status   [{s_status}]")
            except Exception:
                L("  (parse error)")
        else:
            L("  — no signal generated yet this session")

        dec_icon = {"APPROVED": "✅", "REJECTED": "❌"}.get(str(last_dec or ""), "—")
        L(f"\n✅ LAST GETO DECISION")
        if last_dec:
            L(f"  {dec_icon} {last_dec}")
        else:
            L("  — no decision yet")

        # Upcoming news
        L("")
        L("📰 UPCOMING NEWS  (next 6h)")
        if upcoming_news:
            for ev in upcoming_news[:5]:
                ev_time    = ev.get("time", "?")
                ev_name    = ev.get("event", "?")[:40]
                ev_country = ev.get("country", "?")
                ev_impact  = ev.get("impact", "?")
                ev_away    = ev.get("minutes_away", None)
                away_str   = f"  in {_mins_to_hhmm(ev_away)}" if ev_away is not None else ""
                affects    = " 🥇" if ev.get("affects_gold") else ""
                L(f"  {ev_time}  [{ev_impact.upper()[:3]}] {ev_country}  {ev_name}{affects}{away_str}")
        else:
            L("  ✅ All clear — no events in next 6h")

        # Open trades
        L("")
        L(f"📂 OPEN TRADES  ({len(open_trades)})")
        if open_trades:
            for t in open_trades:
                t_model = t["model"].replace("_", " ").title()
                t_dir   = t["direction"]
                t_entry = t["entry_price"]
                t_sl    = t["sl_price"]
                t_tp    = t["tp_price"]
                t_lot   = t["lot_size"]
                t_risk  = t.get("risk_amount", None)
                # distance from current price
                if bid is not None:
                    cur_price = bid if t_dir == "BUY" else ask
                    dist_sl = abs(cur_price - t_sl)
                    dist_tp = abs(t_tp - cur_price)
                    move_str = f"  SL dist ${dist_sl:.2f}  TP dist ${dist_tp:.2f}"
                else:
                    move_str = ""
                L(f"  #{t['id']}  {t_model}  {t_dir}")
                L(f"      Entry ${t_entry:,.2f}   SL ${t_sl:,.2f}   TP ${t_tp:,.2f}")
                L(f"      Lot {t_lot:.2f}" + (f"   Risk ${t_risk:.2f}" if t_risk else "") + move_str)
        else:
            L("  none")

        L("")
        L(SEP)
        L(f"Updated {now_str}")

        print("\n".join(lines))

    except Exception as exc:
        import traceback
        print(f"Status error: {exc}\n{traceback.format_exc()}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
