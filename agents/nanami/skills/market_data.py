"""
market_data.py — NANAMI skill

Fetches live XAUUSD market data from MetaApi.

Public API:
    get_candles(timeframe, count)  → pd.DataFrame (OHLCV, UTC time index)
    get_current_price()            → dict {bid, ask, spread}
    get_asian_range()              → (float, float) — (high, low) 00:00–07:00 GMT today

Timeframe strings for MetaApi: '1m', '5m', '15m', '1h', '4h'
Candles are returned in chronological order (oldest → newest).
iloc[-1] = most recently completed candle.
"""

import logging
from datetime import datetime, timezone
from typing import Tuple

import pandas as pd

from core.metaapi_client import get_account, get_connection

logger = logging.getLogger(__name__)

SYMBOL = "XAUUSD"

# Minimum candle counts per timeframe (to satisfy indicator warm-up)
_MIN_CANDLES = {
    "1m":  300,   # ~5 hours of M1
    "5m":  200,   # ~17 hours of M5
    "15m": 100,   # ~25 hours of M15
    "1h":  60,    # ~2.5 days of H1
    "4h":  30,    # ~5 days of H4
}


# ---------------------------------------------------------------------------
# Core fetch
# ---------------------------------------------------------------------------

async def get_candles(timeframe: str, count: int = 200) -> pd.DataFrame:
    """
    Fetch OHLCV candles for XAUUSD.

    Args:
        timeframe: MetaApi timeframe string ('1m', '5m', '15m').
        count:     Number of candles to fetch (most recent N).

    Returns:
        DataFrame with columns [time, open, high, low, close, volume].
        'time' is timezone-aware UTC datetime.
        Sorted ascending (oldest first).
        Returns empty DataFrame on error.
    """
    count = max(count, _MIN_CANDLES.get(timeframe, count))

    try:
        account = await get_account()
        raw = await account.get_historical_candles(
            SYMBOL,
            timeframe,
            datetime.now(timezone.utc),
            count,
        )
        if not raw:
            logger.warning("get_candles(%s, %d): empty response", timeframe, count)
            return pd.DataFrame()

        rows = []
        for c in raw:
            # 'time' may be datetime or ISO string depending on SDK version
            t = c.get("time")
            if isinstance(t, str):
                t = datetime.fromisoformat(t.replace("Z", "+00:00"))
            elif isinstance(t, datetime) and t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)

            rows.append({
                "time":   t,
                "open":   float(c.get("open",  0.0)),
                "high":   float(c.get("high",  0.0)),
                "low":    float(c.get("low",   0.0)),
                "close":  float(c.get("close", 0.0)),
                "volume": int(c.get("tickVolume", c.get("volume", 0))),
            })

        df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
        logger.debug("Fetched %d %s candles", len(df), timeframe)
        return df

    except Exception as exc:
        logger.error("get_candles(%s, %d) failed: %s", timeframe, count, exc)
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Current price / spread
# ---------------------------------------------------------------------------

async def get_current_price() -> dict:
    """
    Fetch the current bid/ask tick for XAUUSD.

    Returns:
        dict with keys: bid (float), ask (float), spread (float in USD).
        On error returns {'bid': 0.0, 'ask': 0.0, 'spread': 0.0}.
    """
    try:
        connection = await get_connection()
        tick = await connection.get_symbol_price(SYMBOL)
        bid  = float(tick.get("bid", 0.0))
        ask  = float(tick.get("ask", 0.0))
        return {
            "bid":    bid,
            "ask":    ask,
            "spread": round(ask - bid, 5),
        }
    except Exception as exc:
        logger.error("get_current_price() failed: %s", exc)
        return {"bid": 0.0, "ask": 0.0, "spread": 0.0}


# ---------------------------------------------------------------------------
# Asian session range (00:00–07:00 GMT)
# ---------------------------------------------------------------------------

async def get_asian_range() -> Tuple[float, float]:
    """
    Compute the Asian session high/low for today (00:00–07:00 GMT).
    Used by Model C (London Breakout) to determine the breakout level.

    Returns:
        (high, low) as floats, both 0.0 on error or insufficient data.
    """
    try:
        # Fetch enough M5 candles to cover 00:00–07:00 (84 candles = 7 hours)
        # We over-fetch (200) so we definitely capture the full Asian range
        df = await get_candles("5m", count=200)
        if df.empty:
            return (0.0, 0.0)

        now_utc  = datetime.now(timezone.utc)
        asian_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        asian_end   = now_utc.replace(hour=7, minute=0, second=0, microsecond=0)

        # Filter to the Asian window
        mask = (df["time"] >= asian_start) & (df["time"] < asian_end)
        asian_df = df[mask]

        if asian_df.empty:
            logger.warning("get_asian_range: no candles found in 00:00–07:00 GMT today")
            return (0.0, 0.0)

        high = float(asian_df["high"].max())
        low  = float(asian_df["low"].min())
        logger.info("Asian range: high=%.2f  low=%.2f  (%d candles)", high, low, len(asian_df))
        return (high, low)

    except Exception as exc:
        logger.error("get_asian_range() failed: %s", exc)
        return (0.0, 0.0)
