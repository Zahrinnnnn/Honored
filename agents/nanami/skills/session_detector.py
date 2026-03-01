"""
session_detector.py — NANAMI skill

Determines the current GMT trading session based on UTC clock.
Sessions are defined in core/constants.py.

Priority order for get_current_session():
  1. LONDON_BREAKOUT (07:00–07:30) — exclusive window
  2. LONDON_OPEN     (07:00–10:00, but only 07:30+ after breakout check)
  3. NY_OVERLAP      (12:00–16:00)
  4. NY_CLOSE        (19:00–21:00)
  None = outside active windows (includes Asian blackout 21:00–07:00)
"""

from datetime import datetime, time, timezone
from typing import Optional

from core.constants import SESSIONS, BLACKOUT_START, BLACKOUT_END


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc_time() -> time:
    """Current UTC time truncated to minutes."""
    dt = datetime.now(timezone.utc)
    return dt.time().replace(second=0, microsecond=0)


def _hhmm(s: str) -> time:
    h, m = int(s[:2]), int(s[3:])
    return time(h, m)


def _in_window(t: time, start: time, end: time) -> bool:
    """
    True if t is in [start, end).
    Handles midnight-crossing windows (start > end).
    """
    if start <= end:
        return start <= t < end
    # Crosses midnight (e.g. 21:00 → 07:00)
    return t >= start or t < end


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_blackout_period() -> bool:
    """True during the Asian blackout (21:00–07:00 GMT). No active trading."""
    t = _now_utc_time()
    return _in_window(t, _hhmm(BLACKOUT_START), _hhmm(BLACKOUT_END))


def is_london_breakout_window() -> bool:
    """True only during 07:00–07:30 GMT (Model C exclusive window)."""
    t = _now_utc_time()
    start, end = SESSIONS["LONDON_BREAKOUT"]
    return _in_window(t, _hhmm(start), _hhmm(end))


def get_current_session() -> Optional[str]:
    """
    Returns the primary session name at the current UTC time.

    Returns:
        "LONDON_BREAKOUT" | "LONDON_OPEN" | "NY_OVERLAP" | "NY_CLOSE" | None
        None means outside all active windows (no trading).
    """
    t = _now_utc_time()

    # London Breakout window takes exclusive priority during 07:00–07:30
    if is_london_breakout_window():
        return "LONDON_BREAKOUT"

    # Check remaining sessions in priority order
    for session_name in ("LONDON_OPEN", "NY_OVERLAP", "NY_CLOSE"):
        start_str, end_str = SESSIONS[session_name]
        if _in_window(t, _hhmm(start_str), _hhmm(end_str)):
            return session_name

    return None


def minutes_until_next_session() -> float:
    """
    Returns approximate minutes until the next active session opens.
    Useful for blackout-period sleep tuning (informational only).
    """
    now_dt = datetime.now(timezone.utc)
    t = now_dt.time().replace(second=0, microsecond=0)

    # Session open times in order
    opens = [
        _hhmm(SESSIONS["LONDON_BREAKOUT"][0]),  # 07:00
        _hhmm(SESSIONS["NY_OVERLAP"][0]),        # 12:00
        _hhmm(SESSIONS["NY_CLOSE"][0]),          # 19:00
    ]

    for open_t in opens:
        # Minutes from now to that open time
        open_minutes = open_t.hour * 60 + open_t.minute
        now_minutes  = t.hour * 60 + t.minute
        diff = open_minutes - now_minutes
        if diff > 0:
            return float(diff)

    # Next open is tomorrow's London open
    tomorrow_london = _hhmm(SESSIONS["LONDON_BREAKOUT"][0])
    open_minutes = tomorrow_london.hour * 60 + tomorrow_london.minute + 24 * 60
    now_minutes  = t.hour * 60 + t.minute
    return float(open_minutes - now_minutes)
