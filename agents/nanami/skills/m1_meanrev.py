"""
m1_meanrev.py — NANAMI skill

Model B — M1 Mean Reversion Scalp

Entry logic:
  BUY:  - RSI < M1_RSI_OVERSOLD  (28) — extreme oversold
        - Close ≤ Bollinger Band lower — price at statistical extreme

  SELL: - RSI > M1_RSI_OVERBOUGHT (72) — extreme overbought
        - Close ≥ Bollinger Band upper — price at statistical extreme

Sessions: Any active session (LONDON_OPEN, NY_OVERLAP, NY_CLOSE)
Regime:   RANGING only (ADX < 20)
SL:       1.5 × distance(close, BB band), clamped to [M1_SL_MIN, M1_SL_MAX] = [$3, $5]
TP:       SL × 3 (fixed 1:3 RR)

Mean-reversion rationale: SL is placed beyond the outer BB band
to allow for one more extreme before reversing.
"""

import uuid
import logging
from typing import Optional

import pandas as pd

from core.constants import (
    MODEL_B,
    M1_RSI_OVERBOUGHT, M1_RSI_OVERSOLD,
    M1_SL_MIN, M1_SL_MAX,
)

logger = logging.getLogger(__name__)

_RR   = 3.0
_COLS = ["rsi14", "bb_upper", "bb_mid", "bb_lower"]


def generate_signal(
    df_m1: pd.DataFrame,
    session: str,
    regime: str,
) -> Optional[dict]:
    """
    Attempt to generate a Model B signal from the latest completed M1 candle.

    Args:
        df_m1:   M1 DataFrame with indicator columns (from indicator_engine).
        session: Current session name.
        regime:  Current regime string (must be "RANGING" to fire).

    Returns:
        Signal dict or None if no setup is found.
    """
    if regime != "RANGING":
        return None

    if len(df_m1) < 30:
        logger.debug("Model B: insufficient M1 candle data")
        return None

    row = df_m1.iloc[-1]

    # Guard: all required indicators must be non-NaN
    if any(pd.isna(row.get(col)) for col in _COLS):
        logger.debug("Model B: NaN in required M1 indicators")
        return None

    close    = row["close"]
    rsi      = row["rsi14"]
    bb_upper = row["bb_upper"]
    bb_lower = row["bb_lower"]
    bb_mid   = row["bb_mid"]

    # ── BUY setup (oversold at lower BB) ───────────────────────────────────
    if rsi < M1_RSI_OVERSOLD and close <= bb_lower:
        sl_distance = _sl_distance(close, bb_lower, "BUY")
        return _build("BUY", close, sl_distance, session, regime, rsi, bb_lower, bb_upper)

    # ── SELL setup (overbought at upper BB) ────────────────────────────────
    if rsi > M1_RSI_OVERBOUGHT and close >= bb_upper:
        sl_distance = _sl_distance(close, bb_upper, "SELL")
        return _build("SELL", close, sl_distance, session, regime, rsi, bb_lower, bb_upper)

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sl_distance(close: float, bb_band: float, direction: str) -> float:
    """
    SL = 1.5 × |close - BB band|, clamped to [M1_SL_MIN, M1_SL_MAX].
    Placing SL beyond the band gives room for one further extreme.
    """
    raw = abs(close - bb_band) * 1.5
    # Floor at minimum in case BB band is very tight
    raw = max(raw, (M1_SL_MIN + M1_SL_MAX) / 2.0) if raw < M1_SL_MIN else raw
    return max(M1_SL_MIN, min(M1_SL_MAX, raw))


def _build(
    direction: str,
    entry: float,
    sl_distance: float,
    session: str,
    regime: str,
    rsi: float,
    bb_lower: float,
    bb_upper: float,
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
        "model":       MODEL_B,
        "direction":   direction,
        "entry_price": round(entry, 2),
        "sl_price":    sl_price,
        "tp_price":    tp_price,
        "sl_distance": round(sl_distance, 2),
        "session":     session,
        "regime":      regime,
        "reason":      (
            f"BB touch | RSI={rsi:.1f} | "
            f"BB_lower={bb_lower:.2f}  BB_upper={bb_upper:.2f}"
        ),
    }
