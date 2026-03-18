"""
htf_regime.py — NANAMI skill

6-state regime detection using Z-score (direction) × ATR percentile (volatility)
on H1 data.

Public API:
    detect_regime(df_h1) -> str       (one of 6 REGIME_* constants)
    check_structural_break(df_h1) -> bool
"""

import numpy as np
import pandas as pd
import ta

from core.constants import (
    REGIME_ATR_LOOKBACK,
    REGIME_ATR_PERCENTILE_THRESHOLD,
    REGIME_ATR_PERIOD,
    REGIME_BEARISH_GRIND,
    REGIME_BEARISH_PANIC,
    REGIME_BULLISH_BLOWOFF,
    REGIME_BULLISH_GRIND,
    REGIME_H1_BARS_NEEDED,
    REGIME_TIGHT_RANGE,
    REGIME_TOXIC_CHOP,
    REGIME_Z_SCORE_THRESHOLD,
    REGIME_Z_SCORE_WINDOW,
    STRUCTURAL_BREAK_ATR_MULT,
)


def detect_regime(df_h1: pd.DataFrame) -> str:
    """
    Classify market into one of 6 regime states using H1 data.

    Axis 1 — Direction (Z-Score on H1 close, 50-bar rolling):
        Z = (close - mean) / std
        BULLISH: Z > 1.0, BEARISH: Z < -1.0, NEUTRAL: -1 <= Z <= 1

    Axis 2 — Volatility (ATR14 percentile vs 200-bar lookback):
        HIGH: percentile > 75, LOW: percentile <= 75

    Returns TIGHT_RANGE as safe default if insufficient data.
    """
    if len(df_h1) < REGIME_H1_BARS_NEEDED:
        return REGIME_TIGHT_RANGE

    close = df_h1["close"].values.astype(float)

    # ── Axis 1: Z-Score ──
    window = REGIME_Z_SCORE_WINDOW
    rolling_mean = np.mean(close[-window:])
    rolling_std = np.std(close[-window:], ddof=1)
    if rolling_std < 1e-9:
        z_score = 0.0
    else:
        z_score = (close[-1] - rolling_mean) / rolling_std

    # ── Axis 2: ATR Percentile ──
    high = df_h1["high"].values.astype(float)
    low = df_h1["low"].values.astype(float)

    atr_series = ta.volatility.average_true_range(
        pd.Series(high), pd.Series(low), pd.Series(close),
        window=REGIME_ATR_PERIOD, fillna=False,
    )
    atr_values = atr_series.dropna().values
    if len(atr_values) < REGIME_ATR_LOOKBACK:
        # Not enough ATR history — assume low volatility
        atr_percentile = 50.0
    else:
        lookback = atr_values[-REGIME_ATR_LOOKBACK:]
        current_atr = atr_values[-1]
        atr_percentile = (np.sum(lookback <= current_atr) / len(lookback)) * 100.0

    # ── 6-State Matrix ──
    threshold = REGIME_Z_SCORE_THRESHOLD
    high_vol = atr_percentile > REGIME_ATR_PERCENTILE_THRESHOLD

    if z_score > threshold:
        return REGIME_BULLISH_BLOWOFF if high_vol else REGIME_BULLISH_GRIND
    elif z_score < -threshold:
        return REGIME_BEARISH_PANIC if high_vol else REGIME_BEARISH_GRIND
    else:
        return REGIME_TOXIC_CHOP if high_vol else REGIME_TIGHT_RANGE


def compute_macro_bias(df_h1: pd.DataFrame) -> str:
    """
    Returns 'BULLISH' if H1 close is above its 500-bar SMA (~20 days),
    'BEARISH' if below, 'NEUTRAL' if insufficient data.

    SMA500 (~20 days) is slow enough to represent true macro trend —
    won't flip on 1-2 week corrections the way SMA200 (8 days) does.
    Used to gate SELL signals: only allow SELL when macro confirms bearish.
    """
    if len(df_h1) < 500:
        return "NEUTRAL"
    close = df_h1["close"].values.astype(float)
    sma500 = np.mean(close[-500:])
    if close[-1] > sma500:
        return "BULLISH"
    elif close[-1] < sma500:
        return "BEARISH"
    return "NEUTRAL"


def compute_h4_bias(df_h4: pd.DataFrame) -> str:
    """
    Returns H4 trend bias: 'BULLISH', 'BEARISH', or 'NEUTRAL'.

    Uses close vs 50-bar H4 SMA (~8 trading days).
    SMA50 is slow enough that brief 1-2 week corrections in a bull market
    don't flip it to BEARISH — only sustained multi-week downtrends do.

    Used as SELL gate for BEARISH_GRIND signals: only allow SELL when
    H4 confirms bearish trend. Blocks bad SELLs during bull-run corrections.
    """
    if len(df_h4) < 50:
        return "NEUTRAL"
    close = df_h4["close"].values.astype(float)
    sma50 = np.mean(close[-50:])
    if close[-1] < sma50:
        return "BEARISH"
    elif close[-1] > sma50:
        return "BULLISH"
    return "NEUTRAL"


def check_structural_break(df_h1: pd.DataFrame) -> bool:
    """
    Returns True if the latest H1 candle's range exceeds 3 × ATR14.

    When True, the caller should halt trading for STRUCTURAL_BREAK_COOLDOWN_HOURS.
    Requires at least 15 rows for ATR warm-up.
    """
    if len(df_h1) < REGIME_ATR_PERIOD + 1:
        return False

    high = df_h1["high"].values.astype(float)
    low = df_h1["low"].values.astype(float)
    close = df_h1["close"].values.astype(float)

    atr_series = ta.volatility.average_true_range(
        pd.Series(high), pd.Series(low), pd.Series(close),
        window=REGIME_ATR_PERIOD, fillna=False,
    )
    atr_values = atr_series.dropna().values
    if len(atr_values) == 0:
        return False

    current_atr = atr_values[-1]
    last_range = high[-1] - low[-1]

    return last_range > STRUCTURAL_BREAK_ATR_MULT * current_atr
