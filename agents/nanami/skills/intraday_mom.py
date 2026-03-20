"""
intraday_mom.py — NANAMI skill (Model D)

Intraday Momentum (IMom) model.

Thesis: Gold's directional move during 12:00–14:00 UTC (NY open, first 2 hours)
tends to persist into 14:00–16:00 UTC (second half of NY Overlap). Measure the
net return of the opening window; trade the continuation.

This is a pure time-series momentum model — no detrending, no regime dependency,
no Kalman. The signal fires once per day at the 14:00 UTC bar open.

Signal logic:
  Measurement window: 12:00–14:00 UTC (up to 24 M5 bars)
  Trading window:     14:00–16:00 UTC

  net_return = close(13:55 bar) - close(12:00 bar)

  If net_return >  IMOM_MOMENTUM_THRESHOLD × ATR14 → BUY
  If net_return < -IMOM_MOMENTUM_THRESHOLD × ATR14 → SELL
  Else: no signal (momentum too weak, likely choppy open)

Filters:
  - Session == NY_OVERLAP
  - Current bar in trading window (14:00–16:00 UTC)
  - At least IMOM_MIN_MEASURE_BARS present in measurement window
  - H4 bias gate: BUY blocked when H4=BEARISH, SELL when H4=BULLISH

Sessions: NY_OVERLAP only
Cap: 1 per day (enforced via session trade count)
SL: 1.5 × ATR14, clamped [$6, $12]
TP: SL × 2 (fixed 1:2 RR)
Time kill: 120 min
"""

import uuid
from typing import Optional

import numpy as np
import pandas as pd

from core.constants import (
    IMOM_MEASURE_END_HOUR,
    IMOM_MEASURE_START_HOUR,
    IMOM_MIN_MEASURE_BARS,
    IMOM_MOMENTUM_THRESHOLD,
    IMOM_TRADE_END_HOUR,
    IMOM_TRADE_START_HOUR,
    MODEL_D,
    OU_SL_ATR_MULT,
    OU_SL_MAX,
    OU_SL_MIN,
    RR_RATIO,
)


def generate_signal(
    df_m5: pd.DataFrame,
    session: str,
    h4_bias: str = "NEUTRAL",
) -> Optional[dict]:
    """
    Generate a Model D signal: intraday momentum continuation.

    Args:
        df_m5:    Rolling M5 window with indicators.
        session:  Current session name.
        h4_bias:  H4 SMA50 bias.

    Returns:
        Signal dict or None.
    """
    if session != "NY_OVERLAP":
        return None

    if len(df_m5) < 30:
        return None

    last = df_m5.iloc[-1]
    atr  = float(last.get("atr14", 0.0))
    if atr <= 0 or pd.isna(atr):
        return None

    # ── Time gate: must be in trading window (14:00–16:00 UTC) ───────────
    last_idx = df_m5.index[-1]
    last_utc = last_idx.tz_convert("UTC") if last_idx.tzinfo is not None else last_idx
    if last_utc.hour < IMOM_TRADE_START_HOUR or last_utc.hour >= IMOM_TRADE_END_HOUR:
        return None

    # ── Extract measurement bars (12:00–14:00 UTC today) ─────────────────
    today   = last_utc.date()
    idx_utc = df_m5.index.tz_convert("UTC") if df_m5.index.tzinfo is not None else df_m5.index
    mask    = (
        np.array([d == today for d in idx_utc.date])
        & (idx_utc.hour >= IMOM_MEASURE_START_HOUR)
        & (idx_utc.hour < IMOM_MEASURE_END_HOUR)
    )
    measure_bars = df_m5[mask]
    if len(measure_bars) < IMOM_MIN_MEASURE_BARS:
        return None  # not enough measurement data

    # ── Net return of measurement window ──────────────────────────────────
    close_open  = float(measure_bars["close"].iloc[0])
    close_close = float(measure_bars["close"].iloc[-1])
    net_return  = close_close - close_open
    threshold   = IMOM_MOMENTUM_THRESHOLD * atr

    if net_return > threshold:
        direction = "BUY"
    elif net_return < -threshold:
        direction = "SELL"
    else:
        return None  # momentum too weak — choppy open, skip

    # ── H4 bias gate ──────────────────────────────────────────────────────
    if direction == "BUY"  and h4_bias == "BEARISH":
        return None
    if direction == "SELL" and h4_bias == "BULLISH":
        return None

    # ── SL / TP ───────────────────────────────────────────────────────────
    entry       = round(float(last["close"]), 2)
    sl_distance = round(float(np.clip(OU_SL_ATR_MULT * atr, OU_SL_MIN, OU_SL_MAX)), 2)

    if direction == "BUY":
        sl_price = round(entry - sl_distance, 2)
        tp_price = round(entry + sl_distance * RR_RATIO, 2)
    else:
        sl_price = round(entry + sl_distance, 2)
        tp_price = round(entry - sl_distance * RR_RATIO, 2)

    return {
        "id":               str(uuid.uuid4()),
        "model":            MODEL_D,
        "session":          session,
        "direction":        direction,
        "entry_price":      entry,
        "sl_price":         sl_price,
        "tp_price":         tp_price,
        "sl_distance":      sl_distance,
        "atr_at_entry":     round(atr, 2),
        # Entry context for MAHORAGA analysis
        "zscore_at_entry":  0.0,
        "hurst_at_entry":   0.0,
        "detrend_method":   "imom",
        "imom_net_return":  round(net_return, 4),
        "imom_threshold":   round(threshold, 4),
        "h4_bias":          h4_bias,
        "status":           "PENDING",
        "reason": (
            f"IMom | dir={direction} | net_return={net_return:.2f} "
            f"| threshold=±{threshold:.2f} | ATR={atr:.2f}"
        ),
    }
