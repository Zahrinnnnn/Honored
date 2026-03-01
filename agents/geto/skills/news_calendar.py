"""
news_calendar.py — GETO skill

Provides news-clearance checks for trade_validator.

Design
──────
NANAMI writes minutes_to_next_news to session_info every poll cycle.
GETO reads from session_info instead of hitting Finnhub directly — this
avoids redundant API calls and respects the single-responsibility boundary
(NANAMI owns network I/O, GETO reads SQLite state).

Fallback: If session_info is missing or unparseable, GETO calls
news_fetcher directly. If that also fails → return 0.0 (fail-safe:
blocks all trades until news status is confirmed clear).
"""

import logging

from core.constants import NEWS_BLACKOUT_MINUTES
from core.state_manager import StateManager

logger = logging.getLogger(__name__)


async def get_minutes_to_next_news(state: StateManager) -> float:
    """
    Returns minutes until the next high-impact news event.

    Primary source: session_info.minutes_to_next_news (written by NANAMI).
    Fallback: calls core.news_fetcher.minutes_to_next_high_impact_event() directly.
    On any error: returns 0.0 (fail-safe — blocks trades).
    """
    raw = await state.get_session_info("minutes_to_next_news")
    if raw:
        try:
            return float(raw)
        except ValueError:
            logger.warning(
                "news_calendar: unparseable minutes_to_next_news='%s' — falling back",
                raw,
            )

    # Fallback: call Finnhub directly
    try:
        from core.news_fetcher import minutes_to_next_high_impact_event
        minutes = minutes_to_next_high_impact_event()
        logger.info("news_calendar: fallback to news_fetcher → %.1f min", minutes)
        return minutes
    except Exception as exc:
        logger.error(
            "news_calendar: fallback news_fetcher failed: %s — blocking trades",
            exc,
        )
        return 0.0


async def is_news_clear(state: StateManager) -> bool:
    """Returns True if safe to trade (no high-impact event within blackout window)."""
    minutes = await get_minutes_to_next_news(state)
    return minutes > NEWS_BLACKOUT_MINUTES
