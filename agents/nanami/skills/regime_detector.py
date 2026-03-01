"""
regime_detector.py — NANAMI skill

Classifies the current market regime from M5 indicator data.

Regimes:
    TRENDING  — ADX > ADX_TRENDING_THRESHOLD (25)  → Model A eligible
    RANGING   — ADX < ADX_RANGING_THRESHOLD  (20)  → Model B eligible
    VOLATILE  — ADX between thresholds or ATR spike → no model fires

ATR-based volatility override:
    If ATR > ATR_VOLATILE_MULTIPLIER × 20-period ATR mean → VOLATILE
    (catches explosive moves that fool the ADX threshold)
"""

import logging

import pandas as pd

from core.constants import (
    ADX_TRENDING_THRESHOLD,
    ADX_RANGING_THRESHOLD,
    ATR_VOLATILE_MULTIPLIER,
)

logger = logging.getLogger(__name__)

REGIME_TRENDING = "TRENDING"
REGIME_RANGING  = "RANGING"
REGIME_VOLATILE = "VOLATILE"


def detect_regime(df: pd.DataFrame) -> str:
    """
    Determine current market regime from the last row of a DataFrame
    that already has indicator columns added by indicator_engine.

    Args:
        df: DataFrame with at least 'adx14' and 'atr14' columns.

    Returns:
        "TRENDING" | "RANGING" | "VOLATILE"
        Defaults to "VOLATILE" on missing/NaN data (safe — no trade fires).
    """
    if df.empty or "adx14" not in df.columns:
        logger.warning("detect_regime: missing adx14 column, defaulting to VOLATILE")
        return REGIME_VOLATILE

    row = df.iloc[-1]
    adx = row.get("adx14")
    atr = row.get("atr14")

    if pd.isna(adx):
        logger.warning("detect_regime: ADX is NaN, defaulting to VOLATILE")
        return REGIME_VOLATILE

    # ATR spike check (if ATR is available and we have enough history)
    if not pd.isna(atr) and len(df) >= 20:
        atr_mean = df["atr14"].iloc[-20:].mean()
        if not pd.isna(atr_mean) and atr_mean > 0 and atr > ATR_VOLATILE_MULTIPLIER * atr_mean:
            logger.info(
                "detect_regime: ATR spike (%.2f > %.1f × mean %.2f) → VOLATILE",
                atr, ATR_VOLATILE_MULTIPLIER, atr_mean,
            )
            return REGIME_VOLATILE

    # ADX classification
    if adx > ADX_TRENDING_THRESHOLD:
        regime = REGIME_TRENDING
    elif adx < ADX_RANGING_THRESHOLD:
        regime = REGIME_RANGING
    else:
        regime = REGIME_VOLATILE

    logger.debug("detect_regime: ADX=%.1f → %s", adx, regime)
    return regime
