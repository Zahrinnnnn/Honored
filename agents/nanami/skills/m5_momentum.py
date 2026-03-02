"""
m5_momentum.py — NANAMI skill

Model A — M5 Momentum Scalp (Quant Upgrade)

Entry logic:
  BUY:  - Kalman velocity > 0 on M5 (upward trend — replaces M15 EMA50)
        - z_score_50 < -MODEL_A_ZSCORE_ENTRY (-1.5): price pulled back
          more than 1.5 σ below its 50-bar mean (statistically cheap)

  SELL: - Kalman velocity < 0 on M5 (downward trend)
        - z_score_50 > +MODEL_A_ZSCORE_ENTRY (+1.5): price pushed more
          than 1.5 σ above its 50-bar mean (statistically expensive)

  Both columns are computed by indicator_engine.add_indicators() on the
  same M5 DataFrame — no second (M15) DataFrame required.

Sessions: LONDON_OPEN, NY_OVERLAP
Regime:   TRENDING only
SL:       ATR × 1.0, clamped to [M5_SL_MIN, M5_SL_MAX] = [$5, $8]
TP:       SL × 3 (fixed 1:3 RR)
"""

import uuid
import logging
from typing import Optional

import pandas as pd

from core.constants import (
    MODEL_A,
    MODEL_A_ZSCORE_ENTRY,
    M5_SL_MIN, M5_SL_MAX,
)
from agents.nanami.skills.indicator_engine import has_valid_indicators

logger = logging.getLogger(__name__)

_RR   = 3.0
_COLS = ["z_score_50", "kalman_velocity", "atr14"]


def generate_signal(
    df_m5: pd.DataFrame,
    session: str,
    regime: str,
) -> Optional[dict]:
    """
    Attempt to generate a Model A signal from the latest completed M5 candle.

    Args:
        df_m5:   M5 DataFrame with indicator columns (from indicator_engine).
        session: Current session name (e.g. "LONDON_OPEN").
        regime:  Current regime string (must be "TRENDING" to fire).

    Returns:
        Signal dict or None if no setup is found.
    """
    if regime != "TRENDING":
        return None

    if len(df_m5) < 60:
        logger.debug("Model A: insufficient candle data")
        return None

    last = df_m5.iloc[-1]

    if not has_valid_indicators(last, _COLS):
        logger.debug("Model A: NaN in required indicators")
        return None

    close    = last["close"]
    velocity = float(last["kalman_velocity"])
    z        = float(last["z_score_50"])

    sl_distance = _sl_distance(last)

    # ── BUY setup ──────────────────────────────────────────────────────────
    # Kalman uptrend + price pulled back below mean (z < -1.5)
    if velocity > 0 and z < -MODEL_A_ZSCORE_ENTRY:
        return _build("BUY", close, sl_distance, session, regime, velocity, z)

    # ── SELL setup ─────────────────────────────────────────────────────────
    # Kalman downtrend + price pushed above mean (z > +1.5)
    if velocity < 0 and z > MODEL_A_ZSCORE_ENTRY:
        return _build("SELL", close, sl_distance, session, regime, velocity, z)

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
    velocity: float,
    z: float,
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
            f"Kalman {'uptrend' if direction == 'BUY' else 'downtrend'} "
            f"| z={z:.2f} | velocity={velocity:+.4f} | ATR SL"
        ),
    }
