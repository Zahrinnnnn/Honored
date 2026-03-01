"""
indicator_engine.py — NANAMI skill

Adds all required technical indicators to a candle DataFrame in-place.
Uses the `ta` library throughout — no manual implementations.

Input DataFrame must have columns: open, high, low, close (float)
All added columns use float64. NaN is left as-is (not filled).

Added columns:
    ema9, ema21, ema50          — Exponential Moving Averages
    rsi14                       — RSI (14-period)
    atr14                       — Average True Range (14-period)
    adx14                       — ADX (14-period)
    macd, macd_signal, macd_hist — MACD (12/26/9)
    bb_upper, bb_mid, bb_lower  — Bollinger Bands (20, 2σ)
"""

import pandas as pd
import ta
import ta.trend
import ta.momentum
import ta.volatility


_INDICATOR_COLS = (
    "ema9", "ema21", "ema50",
    "rsi14", "atr14", "adx14",
    "macd", "macd_signal", "macd_hist",
    "bb_upper", "bb_mid", "bb_lower",
)

# ta library raises IndexError on datasets smaller than the indicator window.
# Require a minimum of 60 rows; below that, return NaN columns so callers
# can guard with has_valid_indicators() rather than catching exceptions.
_MIN_ROWS = 60


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute and attach all indicators.
    Operates on a copy — does not modify the original DataFrame.
    Returns the enriched DataFrame.

    If len(df) < 60, all indicator columns are added as NaN (safe fallback).
    """
    df = df.copy()

    if len(df) < _MIN_ROWS:
        for col in _INDICATOR_COLS:
            df[col] = float("nan")
        return df

    close = df["close"]
    high  = df["high"]
    low   = df["low"]

    # --- Exponential Moving Averages ----------------------------------------
    df["ema9"]  = ta.trend.ema_indicator(close, window=9,  fillna=False)
    df["ema21"] = ta.trend.ema_indicator(close, window=21, fillna=False)
    df["ema50"] = ta.trend.ema_indicator(close, window=50, fillna=False)

    # --- RSI -----------------------------------------------------------------
    df["rsi14"] = ta.momentum.rsi(close, window=14, fillna=False)

    # --- ATR -----------------------------------------------------------------
    df["atr14"] = ta.volatility.average_true_range(
        high, low, close, window=14, fillna=False
    )

    # --- ADX -----------------------------------------------------------------
    df["adx14"] = ta.trend.adx(high, low, close, window=14, fillna=False)

    # --- MACD (12 fast, 26 slow, 9 signal) -----------------------------------
    df["macd"]        = ta.trend.macd(close,        fillna=False)
    df["macd_signal"] = ta.trend.macd_signal(close, fillna=False)
    df["macd_hist"]   = ta.trend.macd_diff(close,   fillna=False)

    # --- Bollinger Bands (20, 2σ) --------------------------------------------
    df["bb_upper"] = ta.volatility.bollinger_hband(
        close, window=20, window_dev=2, fillna=False
    )
    df["bb_mid"] = ta.volatility.bollinger_mavg(
        close, window=20, fillna=False
    )
    df["bb_lower"] = ta.volatility.bollinger_lband(
        close, window=20, window_dev=2, fillna=False
    )

    return df


def has_valid_indicators(row: pd.Series, required: list) -> bool:
    """
    Returns True if all required indicator columns are non-NaN in the row.
    Use before generating a signal to guard against warm-up NaNs.
    """
    return all(not pd.isna(row[col]) for col in required)
