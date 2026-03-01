import os
import json
import logging
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import List
from dotenv import load_dotenv
from core.constants import NEWS_BLACKOUT_MINUTES

load_dotenv()
logger = logging.getLogger(__name__)

_FINNHUB_URL = "https://finnhub.io/api/v1/calendar/economic"
_CACHE_FILE  = Path("news_cache.json")


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_cache() -> dict:
    try:
        if _CACHE_FILE.exists():
            return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_cache(data: dict):
    try:
        _CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to save news cache: %s", exc)


def _blocking_sentinel() -> List[dict]:
    return [{"_blocking": True, "event": "API_UNAVAILABLE"}]


def fetch_high_impact_events() -> List[dict]:
    """
    Returns today's high-impact economic events from Finnhub.
    Caches result for the day (refreshes at UTC midnight).
    Falls back to a blocking sentinel if the API is unreachable —
    which causes is_news_clear() to return False and block all trades.
    """
    today = _today_utc()
    cache = _load_cache()

    if cache.get("date") == today and "events" in cache:
        return cache["events"]

    api_key = os.environ.get("FINNHUB_API_KEY", "")
    if not api_key:
        logger.error("FINNHUB_API_KEY not set — blocking all trades (safety default)")
        return _blocking_sentinel()

    try:
        resp = requests.get(
            _FINNHUB_URL,
            params={"token": api_key, "from": today, "to": today},
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json().get("economicCalendar", [])
        events = [e for e in raw if str(e.get("impact", "")).lower() == "high"]
        _save_cache({"date": today, "events": events})
        logger.info("Fetched %d high-impact events for %s", len(events), today)
        return events

    except requests.RequestException as exc:
        logger.error("Finnhub API error: %s — blocking all trades (safety default)", exc)
        return _blocking_sentinel()


def minutes_to_next_high_impact_event() -> float:
    """
    Returns minutes until the next high-impact event.
    Returns 0.0 if currently inside a blackout window or API is unavailable.
    Returns 999.0 if no events remain today.
    """
    events = fetch_high_impact_events()

    if any(e.get("_blocking") for e in events):
        return 0.0

    now = datetime.now(timezone.utc)
    closest = 999.0

    for event in events:
        raw_time = event.get("time") or event.get("releaseTime") or event.get("datetime")
        if not raw_time:
            continue
        try:
            event_dt      = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
            diff_minutes  = (event_dt - now).total_seconds() / 60.0

            if abs(diff_minutes) <= NEWS_BLACKOUT_MINUTES:
                return 0.0                          # inside blackout window right now

            if diff_minutes > 0:
                closest = min(closest, diff_minutes)

        except (ValueError, TypeError):
            continue

    return closest


def is_news_clear() -> bool:
    """Returns True if safe to trade (no high-impact event within blackout window)."""
    return minutes_to_next_high_impact_event() > NEWS_BLACKOUT_MINUTES


def refresh_cache():
    """Force-refresh the news cache regardless of date."""
    _CACHE_FILE.unlink(missing_ok=True)
    return fetch_high_impact_events()
