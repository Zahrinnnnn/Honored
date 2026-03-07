"""
ou_range.py — NANAMI skill (Model B)

OU mean-reversion in TIGHT_RANGE regime (bidirectional).

Same OU engine as Model A but fires only in TIGHT_RANGE and allows
both BUY and SELL entries. Uses detrended residuals.

Dual detrend: tries EMA50 residuals first, then EMA20 residuals as a
second independent signal surface for higher frequency.

Gates (all must pass):
    1. regime == TIGHT_RANGE
    2. len(df_m5) >= OU_LOOKBACK
    3. Valid indicators (atr14 > 0, ema present)
    4. ADF stationarity test passes on residuals (p < 0.10)
    5. OU fit succeeds on residuals
    6. 3 <= half_life_bars <= 50
    7. |ou_zscore| > 1.3
    (bidirectional — both BUY and SELL allowed)

SL: 1.5 × ATR14, clamped [$6, $12]
TP: SL × 2 (fixed 1:2 RR)
"""

import uuid
from typing import Optional

import numpy as np
import pandas as pd

from agents.nanami.skills.stat_tests import adf_stationary, fit_ou, ou_zscore
from core.constants import (
    MODEL_B,
    OU_LOOKBACK,
    OU_LOOKBACK_SHORT,
    OU_MAX_HALF_LIFE,
    OU_MIN_HALF_LIFE,
    OU_SL_ATR_MULT,
    OU_SL_MAX,
    OU_SL_MIN,
    OU_ZSCORE_ENTRY_THRESHOLD,
    REGIME_TIGHT_RANGE,
    RR_RATIO,
)


def _try_ou(closes: np.ndarray, ema_arr: np.ndarray,
            lookback: int,
            z_threshold: float = OU_ZSCORE_ENTRY_THRESHOLD,
            ) -> Optional[tuple]:
    """
    Run the OU pipeline on detrended residuals (bidirectional).

    Returns (direction, z, ou_params, half_life) or None.
    """
    if len(closes) < lookback or len(ema_arr) < lookback:
        return None

    c = closes[-lookback:]
    e = ema_arr[-lookback:]
    if np.any(np.isnan(e)):
        return None

    residuals = c - e

    adf_result = adf_stationary(residuals)
    if not adf_result["stationary"]:
        return None

    ou_params = fit_ou(residuals)
    if ou_params is None:
        return None

    half_life = ou_params["half_life_bars"]
    if half_life < OU_MIN_HALF_LIFE or half_life > OU_MAX_HALF_LIFE:
        return None

    z = ou_zscore(residuals[-1], ou_params)

    if z < -z_threshold:
        return ("BUY", z, ou_params, half_life)
    elif z > z_threshold:
        return ("SELL", z, ou_params, half_life)

    return None


def generate_signal(
    df_m5: pd.DataFrame,
    session: str,
    regime: str,
) -> Optional[dict]:
    """
    Generate a Model B signal: OU mean-reversion in TIGHT_RANGE.

    Tries EMA50 detrend first, then EMA20 detrend as fallback
    for higher signal frequency.
    """
    # Gate 1: regime
    if regime != REGIME_TIGHT_RANGE:
        return None

    # Gate 2: data length
    if len(df_m5) < OU_LOOKBACK:
        return None

    # Gate 3: valid ATR
    last_bar = df_m5.iloc[-1]
    atr = float(last_bar.get("atr14", 0.0))
    if atr <= 0 or pd.isna(atr):
        return None

    closes = df_m5["close"].values.astype(float)

    # EMA50 detrend only (EMA21 too noisy for bidirectional model)
    ema50_col = df_m5.get("ema50")
    if ema50_col is not None and not pd.isna(last_bar.get("ema50", float("nan"))):
        ema50_arr = ema50_col.values.astype(float)
        result = _try_ou(closes, ema50_arr, OU_LOOKBACK,
                         z_threshold=OU_ZSCORE_ENTRY_THRESHOLD)
        if result:
            return _build_signal(result, df_m5, atr, session, regime, "ema50")

    return None


def _build_signal(ou_result: tuple, df_m5: pd.DataFrame, atr: float,
                  session: str, regime: str, detrend: str) -> dict:
    direction, z, ou_params, half_life = ou_result

    sl_distance = round(max(OU_SL_MIN, min(OU_SL_MAX, atr * OU_SL_ATR_MULT)), 2)
    tp_distance = round(sl_distance * RR_RATIO, 2)
    entry = round(float(df_m5["close"].iloc[-1]), 2)

    if direction == "BUY":
        sl_price = round(entry - sl_distance, 2)
        tp_price = round(entry + tp_distance, 2)
    else:
        sl_price = round(entry + sl_distance, 2)
        tp_price = round(entry - tp_distance, 2)

    return {
        "id":              str(uuid.uuid4()),
        "model":           MODEL_B,
        "direction":       direction,
        "entry_price":     entry,
        "sl_price":        sl_price,
        "tp_price":        tp_price,
        "sl_distance":     sl_distance,
        "atr_at_entry":    round(atr, 2),
        "half_life_bars":  round(half_life, 1),
        "session":         session,
        "regime":          regime,
        "reason":          (f"OU range | regime={regime} | z={z:.2f} "
                           f"| mu={ou_params['mu']:.2f} | hl={half_life:.1f} "
                           f"| ATR={atr:.2f} | dt={detrend}"),
    }
