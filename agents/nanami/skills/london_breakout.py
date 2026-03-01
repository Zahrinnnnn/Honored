"""
london_breakout.py — NANAMI skill

Model C — London Open Breakout

Entry logic:
  BUY:  Last completed M5 candle CLOSES ABOVE the Asian session high
  SELL: Last completed M5 candle CLOSES BELOW the Asian session low

Asian range: 00:00–07:00 GMT (high/low stored in session_info by agent.py)

Session: LONDON_BREAKOUT window only (07:00–07:30 GMT)
Regime:  Any — breakout overrides regime check
SL:      Width of Asian range, clamped to [BREAKOUT_SL_MIN, BREAKOUT_SL_MAX] = [$6, $8]
TP:      SL × 3 (fixed 1:3 RR)
Max:     1 trade per day
"""

import uuid
import logging
from typing import Optional

import pandas as pd

from core.constants import (
    MODEL_C,
    BREAKOUT_SL_MIN, BREAKOUT_SL_MAX,
)

logger = logging.getLogger(__name__)

_RR = 3.0


def generate_signal(
    df_m5: pd.DataFrame,
    asian_high: float,
    asian_low: float,
) -> Optional[dict]:
    """
    Attempt to generate a Model C breakout signal.

    Args:
        df_m5:      M5 DataFrame (indicators optional — only close price used).
        asian_high: Asian session (00:00–07:00 GMT) highest high.
        asian_low:  Asian session (00:00–07:00 GMT) lowest low.

    Returns:
        Signal dict or None if no breakout detected.
    """
    if asian_high <= 0 or asian_low <= 0 or asian_high <= asian_low:
        logger.warning("Model C: invalid Asian range (high=%.2f, low=%.2f)", asian_high, asian_low)
        return None

    if df_m5.empty:
        return None

    close = df_m5.iloc[-1]["close"]
    if pd.isna(close) or close <= 0:
        return None

    range_size  = asian_high - asian_low
    sl_distance = max(BREAKOUT_SL_MIN, min(BREAKOUT_SL_MAX, range_size))

    # ── BUY breakout (close above Asian high) ──────────────────────────────
    if close > asian_high:
        logger.info(
            "Model C: BUY breakout — close=%.2f > asian_high=%.2f (range=%.2f)",
            close, asian_high, range_size,
        )
        return _build("BUY", close, sl_distance, asian_high, asian_low, range_size)

    # ── SELL breakout (close below Asian low) ──────────────────────────────
    if close < asian_low:
        logger.info(
            "Model C: SELL breakout — close=%.2f < asian_low=%.2f (range=%.2f)",
            close, asian_low, range_size,
        )
        return _build("SELL", close, sl_distance, asian_high, asian_low, range_size)

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build(
    direction: str,
    entry: float,
    sl_distance: float,
    asian_high: float,
    asian_low: float,
    range_size: float,
) -> dict:
    tp_distance = sl_distance * _RR
    if direction == "BUY":
        sl_price = round(entry - sl_distance, 2)
        tp_price = round(entry + tp_distance, 2)
    else:
        sl_price = round(entry + sl_distance, 2)
        tp_price = round(entry - tp_distance, 2)

    return {
        "id":          str(uuid.uuid4()),
        "model":       MODEL_C,
        "direction":   direction,
        "entry_price": round(entry, 2),
        "sl_price":    sl_price,
        "tp_price":    tp_price,
        "sl_distance": round(sl_distance, 2),
        "session":     "LONDON_BREAKOUT",
        "regime":      "ANY",
        "reason":      (
            f"London breakout | Asian range {asian_low:.2f}–{asian_high:.2f} "
            f"(size=${range_size:.2f})"
        ),
    }
