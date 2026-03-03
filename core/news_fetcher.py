"""
news_fetcher.py — Economic calendar via ForexFactory XML feed.

No API key required. ForexFactory provides a free weekly XML calendar.
Feed URL: https://nfs.faireconomy.media/ff_calendar_thisweek.xml
Times are in US Eastern Time (EST/EDT) — converted to UTC internally.
Cache refreshes every CACHE_TTL_HOURS (4 h) to pick up intraday updates.

Fail-safe: any fetch/parse error returns a blocking sentinel, causing
is_news_clear() to return False and block all trades.
"""

import json
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import List
from zoneinfo import ZoneInfo

import requests

from core.constants import NEWS_BLACKOUT_MINUTES

logger    = logging.getLogger(__name__)
_FF_URL   = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
_CACHE    = Path("news_cache.json")
_CACHE_TTL_HOURS = 4
_EASTERN  = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    try:
        if _CACHE.exists():
            return json.loads(_CACHE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_cache(data: dict) -> None:
    try:
        _CACHE.write_text(json.dumps(data), encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to save news cache: %s", exc)


def _blocking_sentinel() -> List[dict]:
    return [{"_blocking": True, "event": "API_UNAVAILABLE"}]


# ---------------------------------------------------------------------------
# Time parsing
# ---------------------------------------------------------------------------

def _parse_hhmm(time_str: str):
    """
    Parse ForexFactory time string to (hour, minute) in Eastern Time.
    Returns None for 'All Day', 'Tentative', or unparseable strings.

    Examples:
        "3:00pm"  -> (15, 0)
        "12:00am" -> (0, 0)   (midnight)
        "12:00pm" -> (12, 0)  (noon)
    """
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
        return (h, m)
    except (ValueError, AttributeError):
        return None


def _to_utc(date_str: str, time_str: str):
    """
    Convert ForexFactory date + time to UTC datetime.
    date_str: "03-02-2026" (MM-DD-YYYY)
    time_str: "3:00pm" (Eastern Time)
    Returns None on any parse failure.
    """
    hm = _parse_hhmm(time_str)
    if hm is None:
        return None
    try:
        d   = datetime.strptime(date_str.strip(), "%m-%d-%Y")
        naive = d.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        return naive.replace(tzinfo=_EASTERN).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _fetch_all_events() -> List[dict]:
    """
    Fetches all events from ForexFactory (all impact levels).
    Caches for CACHE_TTL_HOURS. Falls back to blocking sentinel on error.
    Each event dict has: event, country, time (ISO UTC), impact.
    """
    now_ts = datetime.now(timezone.utc).timestamp()
    cache  = _load_cache()

    if cache.get("fetched_at", 0) + _CACHE_TTL_HOURS * 3600 > now_ts and "events" in cache:
        return cache["events"]

    try:
        resp = requests.get(
            _FF_URL,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content.decode("windows-1252", errors="replace"))

        events = []
        for ev in root.findall("event"):
            impact   = (ev.findtext("impact")  or "").strip().lower()
            date_str = (ev.findtext("date")    or "").strip()
            time_str = (ev.findtext("time")    or "").strip()
            title    = (ev.findtext("title")   or "").strip()
            country  = (ev.findtext("country") or "").strip()
            dt_utc   = _to_utc(date_str, time_str)
            if dt_utc is None:
                continue
            events.append({
                "event":   title,
                "country": country,
                "time":    dt_utc.isoformat(),
                "impact":  impact,
            })

        _save_cache({"fetched_at": now_ts, "events": events})
        logger.info("ForexFactory: %d events this week", len(events))
        return events

    except Exception as exc:
        logger.error("ForexFactory feed error: %s — blocking all trades (safety default)", exc)
        return _blocking_sentinel()


def fetch_high_impact_events() -> List[dict]:
    """
    Returns this week's high-impact economic events from ForexFactory.
    Used by trading agents for blackout decisions.
    """
    return [e for e in _fetch_all_events() if e.get("impact") == "high"]


def fetch_upcoming_events(hours_ahead: float = 4.0, impacts: tuple = ("high", "medium")) -> List[dict]:
    """
    Returns upcoming events within hours_ahead, filtered to given impact levels.
    Adds minutes_away field. Returns empty list on feed error (display only — no safety implication).
    """
    all_events = _fetch_all_events()
    if any(e.get("_blocking") for e in all_events):
        return []

    now = datetime.now(timezone.utc)
    cutoff = hours_ahead * 60.0
    result = []

    for event in all_events:
        if event.get("impact") not in impacts:
            continue
        raw_time = event.get("time")
        if not raw_time:
            continue
        try:
            event_dt     = datetime.fromisoformat(raw_time)
            diff_minutes = (event_dt - now).total_seconds() / 60.0
            if 0 <= diff_minutes <= cutoff:
                result.append({**event, "minutes_away": round(diff_minutes, 1)})
        except (ValueError, TypeError):
            continue

    result.sort(key=lambda e: e["minutes_away"])
    return result


def minutes_to_next_high_impact_event() -> float:
    """
    Returns minutes until the next high-impact event.
    Returns 0.0 if currently inside a blackout window or feed unavailable.
    Returns 999.0 if no more events today.
    """
    events = fetch_high_impact_events()

    if any(e.get("_blocking") for e in events):
        return 0.0

    now     = datetime.now(timezone.utc)
    closest = 999.0

    for event in events:
        raw_time = event.get("time")
        if not raw_time:
            continue
        try:
            event_dt     = datetime.fromisoformat(raw_time)
            diff_minutes = (event_dt - now).total_seconds() / 60.0

            if abs(diff_minutes) <= NEWS_BLACKOUT_MINUTES:
                return 0.0

            if diff_minutes > 0:
                closest = min(closest, diff_minutes)

        except (ValueError, TypeError):
            continue

    return closest


def is_news_clear() -> bool:
    """Returns True if safe to trade (no high-impact event within blackout window)."""
    return minutes_to_next_high_impact_event() > NEWS_BLACKOUT_MINUTES


def refresh_cache() -> List[dict]:
    """Force-refresh the news cache regardless of TTL."""
    _CACHE.unlink(missing_ok=True)
    return fetch_high_impact_events()
