"""
m5_momentum.py — NANAMI skill

Model A — M5 Momentum Scalp

Entry logic:
  BUY:  - M15 EMA50 is below current price (bullish higher-timeframe bias)
        - M5 candle low touched EMA21 (pullback to support)
        - M5 candle close above EMA21 (price recovering)
        - RSI 40–60 (momentum zone, trend not exhausted)
        - MACD histogram > 0 (confirms bullish momentum)

  SELL: Mirror conditions (bearish HTF bias, pullback to EMA21 as resistance)

Sessions: LONDON_OPEN, NY_OVERLAP
Regime:   TRENDING only (ADX > 25)
SL:       ATR × 1.0, clamped to [M5_SL_MIN, M5_SL_MAX] = [$5, $8]
TP:       SL × 3 (fixed 1:3 RR)
"""

import uuid
import logging
from typing import Optional

import pandas as pd

from core.constants import (
    MODEL_A,
    M5_RSI_MIN, M5_RSI_MAX,
    M5_SL_MIN, M5_SL_MAX,
)

logger = logging.getLogger(__name__)

_RR    = 3.0
_COLS  = ["ema21", "ema50", "rsi14", "adx14", "macd_hist", "atr14"]


def generate_signal(
    df_m5: pd.DataFrame,
    df_m15: pd.DataFrame,
    session: str,
    regime: str,
) -> Optional[dict]:
    """
    Attempt to generate a Model A signal from the latest completed candle.

    Args:
        df_m5:   M5 DataFrame with indicator columns (from indicator_engine).
        df_m15:  M15 DataFrame with indicator columns (for HTF bias).
        session: Current session name (e.g. "LONDON_OPEN").
        regime:  Current regime string (must be "TRENDING" to fire).

    Returns:
        Signal dict or None if no setup is found.
    """
    if regime != "TRENDING":
        return None

    if len(df_m5) < 60 or len(df_m15) < 60:
        logger.debug("Model A: insufficient candle data")
        return None

    m5  = df_m5.iloc[-1]
    m15 = df_m15.iloc[-1]

    # Guard: all required indicators must be non-NaN
    if any(pd.isna(m5.get(col)) for col in _COLS):
        logger.debug("Model A: NaN in required M5 indicators")
        return None
    if pd.isna(m15.get("ema50")):
        logger.debug("Model A: NaN in M15 EMA50")
        return None

    close     = m5["close"]
    low       = m5["low"]
    high      = m5["high"]
    ema21     = m5["ema21"]
    rsi       = m5["rsi14"]
    macd_hist = m5["macd_hist"]
    htf_ema50 = m15["ema50"]

    # RSI must be in the momentum zone for either direction
    if not (M5_RSI_MIN <= rsi <= M5_RSI_MAX):
        return None

    sl_distance = _sl_distance(m5)

    # ── BUY setup ──────────────────────────────────────────────────────────
    if (
        close > htf_ema50        # bullish HTF bias
        and low  <= ema21        # wick touched EMA21 (pullback to support)
        and close > ema21        # recovered above EMA21
        and macd_hist > 0        # MACD confirms bullish momentum
    ):
        return _build("BUY", close, sl_distance, session, regime, rsi, macd_hist)

    # ── SELL setup ─────────────────────────────────────────────────────────
    if (
        close < htf_ema50        # bearish HTF bias
        and high >= ema21        # wick touched EMA21 (pullback to resistance)
        and close < ema21        # rejected below EMA21
        and macd_hist < 0        # MACD confirms bearish momentum
    ):
        return _build("SELL", close, sl_distance, session, regime, rsi, macd_hist)

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sl_distance(candle: pd.Series) -> float:
    """1× ATR as SL, clamped to the model's allowed range."""
    atr = candle.get("atr14", float("nan"))
    if pd.isna(atr) or atr <= 0:
        atr = (M5_SL_MIN + M5_SL_MAX) / 2.0
    return max(M5_SL_MIN, min(M5_SL_MAX, atr))


def _build(
    direction: str,
    entry: float,
    sl_distance: float,
    session: str,
    regime: str,
    rsi: float,
    macd_hist: float,
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
        "model":       MODEL_A,
        "direction":   direction,
        "entry_price": round(entry, 2),
        "sl_price":    sl_price,
        "tp_price":    tp_price,
        "sl_distance": round(sl_distance, 2),
        "session":     session,
        "regime":      regime,
        "reason":      (
            f"EMA21 pullback on M5 | HTF bias OK | "
            f"RSI={rsi:.1f} | MACD_hist={macd_hist:+.4f}"
        ),
    }
