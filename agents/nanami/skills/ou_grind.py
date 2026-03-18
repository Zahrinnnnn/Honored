"""
ou_grind.py — NANAMI skill (Model A)

OU mean-reversion in directional grind regimes.

Fires only in BULLISH_GRIND (long-only) or BEARISH_GRIND (short-only).
Uses Ornstein-Uhlenbeck process calibration on detrended M5 residuals
to detect mean-reversion opportunities in the trend direction.

Dual detrend: tries EMA50 residuals first, then EMA20 residuals as a
second independent signal surface for higher frequency.

Gates (all must pass):
    1. regime in {BULLISH_GRIND, BEARISH_GRIND}
    2. len(df_m5) >= OU_LOOKBACK
    3. Valid indicators (atr14 > 0, ema present)
    4. Relative ATR: current ATR <= 60-bar median ATR × 1.8 (volatility spike filter)
    5. Hurst <= 0.53 (price is mean-reverting, not trending)
    6. ADF stationarity test passes on residuals (p < 0.10)
    7. OU fit succeeds on residuals
    8. 3 <= half_life_bars <= 50
    9. |ou_zscore| > 1.3
   10. Direction matches regime

SL: 1.5 × ATR14, clamped [$6, $12]
TP: SL × 2 (fixed 1:2 RR)
"""

import uuid
from typing import Optional

import numpy as np
import pandas as pd

from agents.nanami.skills.stat_tests import adf_stationary, fit_ou, ou_zscore, rolling_hurst
from core.constants import (
    HURST_TRENDING_THRESHOLD,
    MODEL_A,
    OU_LOOKBACK,
    OU_LOOKBACK_MID,
    OU_LOOKBACK_SHORT,
    OU_MAX_HALF_LIFE,
    OU_MAX_HALF_LIFE_BLOWOFF,
    OU_MIN_HALF_LIFE,
    OU_SL_ATR_MULT,
    OU_SL_MAX,
    OU_SL_MIN,
    OU_ZSCORE_BLOWOFF_THRESHOLD,
    OU_ZSCORE_EMA34_THRESHOLD,
    OU_ZSCORE_ENTRY_THRESHOLD,
    OU_ZSCORE_GRIND_THRESHOLD,
    REGIME_BEARISH_GRIND,
    REGIME_BULLISH_BLOWOFF,
    REGIME_BULLISH_GRIND,
    RR_RATIO,
)

# ATR ratio threshold — block signal when current ATR > baseline × this multiplier
_ATR_SPIKE_MULT = 1.8
# Bars of ATR history to use as baseline
_ATR_BASELINE_BARS = 60
# BLOWOFF parabola gate: EMA50 slope / ATR per bar — blocks if trending too fast for OU
_BLOWOFF_EMA_SLOPE_BARS = 60   # 5-hour lookback for slope measurement
_BLOWOFF_EMA_SLOPE_THRESH = 0.06  # EMA50 moving > 6% of ATR per bar = parabolic


def _try_ou(closes: np.ndarray, ema_arr: np.ndarray, lookback: int,
            regime: str, z_threshold: float = OU_ZSCORE_GRIND_THRESHOLD,
            max_half_life: int = OU_MAX_HALF_LIFE,
            ) -> Optional[tuple]:
    """
    Run the OU pipeline on detrended residuals.

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
    if half_life < OU_MIN_HALF_LIFE or half_life > max_half_life:
        return None

    z = ou_zscore(residuals[-1], ou_params)

    if regime in (REGIME_BULLISH_GRIND, REGIME_BULLISH_BLOWOFF) and z < -z_threshold:
        return ("BUY", z, ou_params, half_life)
    elif regime == REGIME_BEARISH_GRIND and z > z_threshold:
        return ("SELL", z, ou_params, half_life)

    return None


def generate_signal(
    df_m5: pd.DataFrame,
    session: str,
    regime: str,
    macro_bias: str = "NEUTRAL",
) -> Optional[dict]:
    """
    Generate a Model A signal: OU mean-reversion in grind regimes.

    Tries EMA50 detrend first, then EMA20 detrend as fallback
    for higher signal frequency.
    """
    # Gate 1: regime
    if regime not in (REGIME_BULLISH_GRIND, REGIME_BEARISH_GRIND, REGIME_BULLISH_BLOWOFF):
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

    # Gate 4: relative ATR filter — block when volatility is spiking vs baseline
    atr_series = df_m5["atr14"].values
    if len(atr_series) >= _ATR_BASELINE_BARS:
        atr_baseline = float(np.median(atr_series[-_ATR_BASELINE_BARS:]))
        if atr_baseline > 0 and atr > atr_baseline * _ATR_SPIKE_MULT:
            return None  # volatility spike — OU won't work

    # Gate 5: Hurst filter — block when price is trending, not mean-reverting
    # Skip for BLOWOFF: blowoff IS a trending regime; Hurst naturally > 0.53 there
    if regime != REGIME_BULLISH_BLOWOFF and len(closes) >= 200:
        hurst = rolling_hurst(closes)
        if hurst > HURST_TRENDING_THRESHOLD:
            return None  # trending environment — OU won't work

    # ── BLOWOFF mode: BUY only, shallower z=0.6, tighter half-life cap ──────
    if regime == REGIME_BULLISH_BLOWOFF:
        ema50_col = df_m5.get("ema50")
        if ema50_col is not None and not pd.isna(last_bar.get("ema50", float("nan"))):
            ema50_arr = ema50_col.values.astype(float)

            # Parabola gate: block if EMA50 is trending too fast for OU to work
            # Measures EMA50 velocity over last 5h relative to bar ATR.
            # In healthy blowoff: ratio ~0.016. In parabola: ratio >0.03.
            if len(ema50_arr) >= _BLOWOFF_EMA_SLOPE_BARS + 1 and atr > 0:
                ema_slope = abs(ema50_arr[-1] - ema50_arr[-_BLOWOFF_EMA_SLOPE_BARS]) / _BLOWOFF_EMA_SLOPE_BARS
                if ema_slope / atr > _BLOWOFF_EMA_SLOPE_THRESH:
                    return None  # EMA50 trending too fast — parabolic, OU mean won't hold

            result = _try_ou(closes, ema50_arr, OU_LOOKBACK, regime,
                             z_threshold=OU_ZSCORE_BLOWOFF_THRESHOLD,
                             max_half_life=OU_MAX_HALF_LIFE_BLOWOFF)
            if result:
                return _build_signal(result, df_m5, atr, session, regime, "ema50_blowoff")
        return None  # blowoff: EMA50 only, no fallback

    # ── GRIND mode: EMA50 primary, EMA21 fallback ─────────────────────────
    # EMA50 detrend (primary — z=1.0)
    ema50_col = df_m5.get("ema50")
    if ema50_col is not None and not pd.isna(last_bar.get("ema50", float("nan"))):
        ema50_arr = ema50_col.values.astype(float)
        result = _try_ou(closes, ema50_arr, OU_LOOKBACK, regime,
                         z_threshold=OU_ZSCORE_GRIND_THRESHOLD)
        if result:
            return _build_signal(result, df_m5, atr, session, regime, "ema50")

    # EMA21 detrend (fallback — stricter z=1.3, shorter window)
    ema21_col = df_m5.get("ema21")
    if ema21_col is not None and not pd.isna(last_bar.get("ema21", float("nan"))):
        ema21_arr = ema21_col.values.astype(float)
        if len(closes) >= OU_LOOKBACK_SHORT:
            result = _try_ou(closes, ema21_arr, OU_LOOKBACK_SHORT, regime,
                             z_threshold=OU_ZSCORE_ENTRY_THRESHOLD)
            if result:
                return _build_signal(result, df_m5, atr, session, regime, "ema21")

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
        "model":           MODEL_A,
        "direction":       direction,
        "entry_price":     entry,
        "sl_price":        sl_price,
        "tp_price":        tp_price,
        "sl_distance":     sl_distance,
        "atr_at_entry":    round(atr, 2),
        "half_life_bars":  round(half_life, 1),
        "session":         session,
        "regime":          regime,
        "reason":          (f"OU grind | regime={regime} | z={z:.2f} "
                           f"| mu={ou_params['mu']:.2f} | hl={half_life:.1f} "
                           f"| ATR={atr:.2f} | dt={detrend}"),
    }
