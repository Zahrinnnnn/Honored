"""
asian_breakout.py — NANAMI skill (Model C)

London Open Breakout with H4 directional filter.

Entry:
    BUY:  M5 close > Asian high AND H4 bias != BEARISH
    SELL: M5 close < Asian low  AND H4 bias != BULLISH

SL:  Asian range width, clamped [$5, $8]
TP:  SL × 2 (fixed 1:2 RR)
Max: 1 trade per day
"""

import uuid
import logging
from typing import Optional

import pandas as pd

from core.constants import (
    HTF_BIAS_BEARISH,
    HTF_BIAS_BULLISH,
    MODEL_C,
    BREAKOUT_SL_MIN,
    BREAKOUT_SL_MAX,
    MODEL_C_MIN_RANGE,
    RR_RATIO,
)

logger = logging.getLogger(__name__)


def generate_signal(
    df_m5: pd.DataFrame,
    asian_high: float,
    asian_low: float,
    h4_bias: str = "NEUTRAL",
) -> Optional[dict]:
    """
    Attempt to generate a Model C breakout signal.

    Args:
        df_m5:      M5 DataFrame (atr14 used for trailing ATR).
        asian_high: Asian session (00:00–07:00 GMT) highest high.
        asian_low:  Asian session lowest low.
        h4_bias:    H4 directional bias for filtering.

    Returns:
        Signal dict or None.
    """
    if asian_high <= 0 or asian_low <= 0 or asian_high <= asian_low:
        return None

    if (asian_high - asian_low) < MODEL_C_MIN_RANGE:
        return None

    if df_m5.empty:
        return None

    row = df_m5.iloc[-1]
    close = float(row["close"])
    if pd.isna(close) or close <= 0:
        return None

    atr = float(row.get("atr14", 0.0))
    if atr <= 0 or pd.isna(atr):
        atr = 6.0  # fallback

    range_size  = asian_high - asian_low
    sl_distance = max(BREAKOUT_SL_MIN, min(BREAKOUT_SL_MAX, range_size))

    # BUY breakout — blocked if H4 says bearish
    if close > asian_high:
        if h4_bias == HTF_BIAS_BEARISH:
            logger.debug("Model C: BUY blocked by H4 BEARISH bias")
            return None
        return _build("BUY", close, sl_distance, atr, asian_high, asian_low, range_size, h4_bias)

    # SELL breakout — blocked if H4 says bullish
    if close < asian_low:
        if h4_bias == HTF_BIAS_BULLISH:
            logger.debug("Model C: SELL blocked by H4 BULLISH bias")
            return None
        return _build("SELL", close, sl_distance, atr, asian_high, asian_low, range_size, h4_bias)

    return None


def _build(direction, entry, sl_distance, atr, asian_high, asian_low, range_size, h4_bias):
    tp_distance = sl_distance * RR_RATIO

    if direction == "BUY":
        sl_price = round(entry - sl_distance, 2)
        tp_price = round(entry + tp_distance, 2)
    else:
        sl_price = round(entry + sl_distance, 2)
        tp_price = round(entry - tp_distance, 2)

    return {
        "id":           str(uuid.uuid4()),
        "model":        MODEL_C,
        "direction":    direction,
        "entry_price":  round(entry, 2),
        "sl_price":     sl_price,
        "tp_price":     tp_price,
        "sl_distance":  round(sl_distance, 2),
        "atr_at_entry": round(atr, 2),
        "session":      "LONDON_BREAKOUT",
        "regime":       h4_bias,
        "reason":       (f"London breakout | H4={h4_bias} | Asian {asian_low:.2f}–{asian_high:.2f} "
                        f"(size=${range_size:.2f})"),
    }
