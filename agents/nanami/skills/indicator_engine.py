"""
indicator_engine.py — NANAMI skill

Adds all technical indicators to a candle DataFrame.
Uses the `ta` library. Returns an enriched copy; never modifies in-place.

Indicator catalogue
───────────────────
Trend
  ema9, ema21, ema50          Exponential Moving Averages
  ema21_slope                 Rate of change of EMA21 over 5 bars ($/bar)
                              Positive → trending up; negative → trending down

Momentum
  rsi14                       RSI (14-period), range 0–100
  stoch_rsi_k, stoch_rsi_d    Stochastic RSI K/D lines (14/3/3), range 0–1
                              More sensitive than raw RSI for extreme detection

Volatility
  atr14                       Average True Range (14-period, absolute $)
  atr_pct                     ATR as % of close price (normalised, cross-regime)

Trend strength
  adx14                       ADX (14-period), range 0–100

MACD
  macd, macd_signal,          MACD line, signal line, histogram (12/26/9)
  macd_hist

Bollinger Bands
  bb_upper, bb_mid, bb_lower  Bands (20-period, 2σ)
  bb_width                    Absolute band width  (bb_upper − bb_lower)
  bb_width_pct                Band width as % of close (normalised)
                              Key input for regime_detector squeeze detection

Minimum rows required: 60 (ta library raises IndexError below window size).
Below 60 rows all indicator columns are added as NaN — callers guard with
has_valid_indicators() rather than catching exceptions.
"""

import pandas as pd
import ta
import ta.trend
import ta.momentum
import ta.volatility


_INDICATOR_COLS = (
    "ema9", "ema21", "ema50", "ema21_slope",
    "rsi14", "stoch_rsi_k", "stoch_rsi_d",
    "atr14", "atr_pct",
    "adx14",
    "macd", "macd_signal", "macd_hist",
    "bb_upper", "bb_mid", "bb_lower", "bb_width", "bb_width_pct",
)

# ta library raises IndexError on datasets smaller than the indicator window.
# 60 rows = safe minimum for all indicators (EMA50 needs 50, ADX14 needs ~28,
# BB20 needs 20, StochRSI(14,3,3) needs ~34).
_MIN_ROWS = 60


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute and attach all indicators to the DataFrame.

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
    df["ema9"]  = ta.trend.ema_indicator(close, window=9,  fillna=False)
    df["ema21"] = ta.trend.ema_indicator(close, window=21, fillna=False)
    df["ema50"] = ta.trend.ema_indicator(close, window=50, fillna=False)

    # EMA21 slope: change per candle over the last 5 bars
    # Positive = uptrend building; negative = downtrend; ~0 = flat/ranging
    df["ema21_slope"] = df["ema21"].diff(5) / 5.0

    # ── RSI ──────────────────────────────────────────────────────────────────
    df["rsi14"] = ta.momentum.rsi(close, window=14, fillna=False)

    # ── Stochastic RSI (14/3/3) ──────────────────────────────────────────────
    # More sensitive than raw RSI — better at detecting true extremes for
    # mean-reversion Model B signals.  Values in [0, 1].
    df["stoch_rsi_k"] = ta.momentum.stochrsi_k(
        close, window=14, smooth1=3, smooth2=3, fillna=False
    )
    df["stoch_rsi_d"] = ta.momentum.stochrsi_d(
        close, window=14, smooth1=3, smooth2=3, fillna=False
    )

    # ── ATR ──────────────────────────────────────────────────────────────────
    df["atr14"]  = ta.volatility.average_true_range(
        high, low, close, window=14, fillna=False
    )
    # ATR as % of close: normalises volatility across price levels,
    # enabling comparison across sessions and regime types.
    df["atr_pct"] = df["atr14"] / close * 100.0

    # ── ADX ──────────────────────────────────────────────────────────────────
    df["adx14"] = ta.trend.adx(high, low, close, window=14, fillna=False)

    # ── MACD (12 fast / 26 slow / 9 signal) ─────────────────────────────────
    df["macd"]        = ta.trend.macd(close,        fillna=False)
    df["macd_signal"] = ta.trend.macd_signal(close, fillna=False)
    df["macd_hist"]   = ta.trend.macd_diff(close,   fillna=False)

    # ── Bollinger Bands (20-period, 2σ) ──────────────────────────────────────
    df["bb_upper"] = ta.volatility.bollinger_hband(
        close, window=20, window_dev=2, fillna=False
    )
    df["bb_mid"] = ta.volatility.bollinger_mavg(close, window=20, fillna=False)
    df["bb_lower"] = ta.volatility.bollinger_lband(
        close, window=20, window_dev=2, fillna=False
    )

    # Derived BB metrics
    df["bb_width"]     = df["bb_upper"] - df["bb_lower"]
    df["bb_width_pct"] = df["bb_width"] / close * 100.0  # normalised

    return df


def has_valid_indicators(row: pd.Series, required: list) -> bool:
    """
    Returns True if all required indicator columns are non-NaN in the row.
    Use before generating a signal to guard against warm-up NaNs.
    """
    return all(not pd.isna(row.get(col)) for col in required)
