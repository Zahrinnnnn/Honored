"""
session_detector.py — NANAMI skill

Determines the current GMT trading session based on UTC clock.
Sessions are defined in core/constants.py.

Design
──────
• All session logic derives from a single UTC clock read per call to
  prevent clock drift between multiple time() calls in the same poll.
• get_session_context() is the preferred API: returns a frozen dataclass
  with all session attributes computed atomically from one datetime.now().
• Legacy helpers (get_current_session, is_blackout_period, etc.) are kept
  for backward compatibility — they delegate to get_session_context().

Session priority order
──────────────────────
  1. LONDON_BREAKOUT (07:00–07:30) — exclusive window, Model C only
  2. LONDON_OPEN     (07:30–10:00) — Models A after breakout window
  3. NY_OVERLAP      (12:00–16:00) — Models A + B
  4. NY_CLOSE        (19:00–21:00) — Model B only
  None = outside active windows (includes Asian blackout 21:00–07:00)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Optional

from core.constants import SESSIONS, BLACKOUT_START, BLACKOUT_END


# ---------------------------------------------------------------------------
# Session context dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionContext:
    """
    Atomic snapshot of the current session state, computed from a single
    UTC clock read. All fields are consistent with each other.

    Attributes:
        name:               Session name, or None if outside all windows.
                            "LONDON_BREAKOUT" | "LONDON_OPEN" | "NY_OVERLAP"
                            | "NY_CLOSE" | None
        is_active:          True if inside any active trading session.
        is_blackout:        True during Asian blackout (21:00–07:00 GMT).
        is_breakout_window: True during 07:00–07:30 GMT (Model C exclusive).
        minutes_elapsed:    Minutes elapsed since current session opened.
                            0.0 if not in an active session.
        minutes_remaining:  Minutes remaining until current session closes.
                            0.0 if not in an active session.
    """
    name:               Optional[str]
    is_active:          bool
    is_blackout:        bool
    is_breakout_window: bool
    minutes_elapsed:    float
    minutes_remaining:  float


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    """Full-precision UTC datetime (single call per context computation)."""
    return datetime.now(timezone.utc)


def _now_utc_time() -> time:
    """Current UTC time truncated to minutes (for window comparisons)."""
    return _now_utc().time().replace(second=0, microsecond=0)


def _hhmm(s: str) -> time:
    """Parse 'HH:MM' string into a time object."""
    h, m = int(s[:2]), int(s[3:])
    return time(h, m)


def _in_window(t: time, start: time, end: time) -> bool:
    """
    True if t is in [start, end).
    Handles midnight-crossing windows correctly (e.g. 21:00 → 07:00).
    """
    if start <= end:
        return start <= t < end
    # Midnight-crossing: e.g. start=21:00, end=07:00
    return t >= start or t < end


def _minutes_between(t_from: time, t_to: time) -> float:
    """
    Signed minutes from t_from to t_to, respecting midnight crossing.
    Always returns a non-negative value when used for elapsed/remaining
    within a known-valid window.
    """
    from_mins = t_from.hour * 60 + t_from.minute
    to_mins   = t_to.hour   * 60 + t_to.minute
    diff = to_mins - from_mins
    if diff < 0:
        diff += 24 * 60  # midnight crossing
    return float(diff)


def _detect(t: time) -> tuple[Optional[str], bool, bool]:
    """
    Internal: classify a UTC time into session components.

    Returns:
        (session_name, is_blackout, is_breakout_window)
        session_name is None outside all active sessions.
    """
    # Blackout check first (Asian 21:00–07:00)
    blackout_start = _hhmm(BLACKOUT_START)
    blackout_end   = _hhmm(BLACKOUT_END)
    is_blackout = _in_window(t, blackout_start, blackout_end)

    # Breakout window (07:00–07:30) — exclusive priority
    b_start_str, b_end_str = SESSIONS["LONDON_BREAKOUT"]
    is_breakout = _in_window(t, _hhmm(b_start_str), _hhmm(b_end_str))

    if is_blackout:
        return None, True, False

    if is_breakout:
        return "LONDON_BREAKOUT", False, True

    for session_name in ("LONDON_OPEN", "NY_OVERLAP", "NY_CLOSE"):
        start_str, end_str = SESSIONS[session_name]
        if _in_window(t, _hhmm(start_str), _hhmm(end_str)):
            return session_name, False, False

    return None, False, False


# ---------------------------------------------------------------------------
# Primary public API
# ---------------------------------------------------------------------------

def get_session_context() -> SessionContext:
    """
    Compute and return the full session state from a single UTC clock read.

    Prefer this over calling multiple individual helpers — all fields are
    derived from the same timestamp, eliminating edge-case clock skew
    between successive calls.

    Returns:
        SessionContext with all fields populated.
    """
    now      = _now_utc()
    t        = now.time().replace(second=0, microsecond=0)
    name, is_blackout, is_breakout = _detect(t)
    is_active = name is not None

    # Compute elapsed + remaining for the current session window
    elapsed   = 0.0
    remaining = 0.0

    if is_active and name is not None:
        sess_key = name  # key used in SESSIONS dict
        start_str, end_str = SESSIONS[sess_key]
        s_start = _hhmm(start_str)
        s_end   = _hhmm(end_str)
        elapsed   = _minutes_between(s_start, t)
        remaining = _minutes_between(t, s_end)

    return SessionContext(
        name               = name,
        is_active          = is_active,
        is_blackout        = is_blackout,
        is_breakout_window = is_breakout,
        minutes_elapsed    = elapsed,
        minutes_remaining  = remaining,
    )


# ---------------------------------------------------------------------------
# Legacy helpers (delegate to get_session_context for consistency)
# ---------------------------------------------------------------------------

def is_blackout_period() -> bool:
    """True during the Asian blackout (21:00–07:00 GMT). No active trading."""
    return get_session_context().is_blackout


def is_london_breakout_window() -> bool:
    """True only during 07:00–07:30 GMT (Model C exclusive window)."""
    return get_session_context().is_breakout_window


def get_current_session() -> Optional[str]:
    """
    Returns the primary session name at the current UTC time.

    Returns:
        "LONDON_BREAKOUT" | "LONDON_OPEN" | "NY_OVERLAP" | "NY_CLOSE" | None
        None means outside all active windows (no trading).
    """
    return get_session_context().name


def minutes_until_next_session() -> float:
    """
    Returns approximate minutes until the next active session opens.
    Useful for blackout-period sleep tuning (informational only).
    """
    now_dt = _now_utc()
    t = now_dt.time().replace(second=0, microsecond=0)

    # Session open times in order (all GMT)
    opens = [
        _hhmm(SESSIONS["LONDON_BREAKOUT"][0]),  # 07:00
        _hhmm(SESSIONS["NY_OVERLAP"][0]),        # 12:00
        _hhmm(SESSIONS["NY_CLOSE"][0]),          # 19:00
    ]

    now_mins = t.hour * 60 + t.minute
    for open_t in opens:
        open_mins = open_t.hour * 60 + open_t.minute
        diff = open_mins - now_mins
        if diff > 0:
            return float(diff)

    # All opens today have passed — next is tomorrow's London open
    london_open = _hhmm(SESSIONS["LONDON_BREAKOUT"][0])
    return float(london_open.hour * 60 + london_open.minute + 24 * 60 - now_mins)
