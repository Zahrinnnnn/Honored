"""
indicator_engine.py — NANAMI skill

Adds required indicators to a candle DataFrame.
Returns an enriched copy; never modifies in-place.

Indicators
──────────
  ema21           EMA 21-period (Model A EMA21 fallback detrend)
  ema50           EMA 50-period (Model A primary detrend)
  atr14           Average True Range 14-period (SL calc + volatility gates)
  kalman_velocity Kalman filter velocity (momentum_blowoff direction gate)

Minimum rows required: 60
"""

import numpy as np
import pandas as pd
import ta.trend
import ta.volatility

from agents.nanami.skills.stat_tests import KalmanPriceFilter


_INDICATOR_COLS = ("ema21", "ema50", "atr14", "kalman_velocity")

_MIN_ROWS = 60


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute and attach required indicators to the DataFrame.

    Args:
        df: Candle DataFrame with columns [open, high, low, close, volume].

    Returns:
        Enriched copy of df. If len(df) < 60, all indicator columns are NaN.
    """
    df = df.copy()

    if len(df) < _MIN_ROWS:
        for col in _INDICATOR_COLS:
            df[col] = float("nan")
        return df

    close = df["close"]
    high  = df["high"]
    low   = df["low"]

    # ── Exponential Moving Averages ──────────────────────────────────────────
    df["ema21"] = ta.trend.ema_indicator(close, window=21, fillna=False)
    df["ema50"] = ta.trend.ema_indicator(close, window=50, fillna=False)

    # ── ATR ──────────────────────────────────────────────────────────────────
    df["atr14"] = ta.volatility.average_true_range(
        high, low, close, window=14, fillna=False
    )

    # ── Kalman filter velocity (momentum direction gate) ─────────────────────
    _prices = close.to_numpy(dtype=float)
    _tail   = _prices[-50:] if len(_prices) >= 51 else _prices
    _r      = float(np.var(np.diff(_tail)))
    _r      = max(_r, 1e-8)
    _kf     = KalmanPriceFilter(r=_r)
    _, _kf_vels = _kf.fit(_prices)
    df["kalman_velocity"] = _kf_vels

    return df


def has_valid_indicators(row: pd.Series, required: list) -> bool:
    """
    Returns True if all required indicator columns are non-NaN in the row.
    Use before generating a signal to guard against warm-up NaNs.
    """
    return all(not pd.isna(row.get(col)) for col in required)
