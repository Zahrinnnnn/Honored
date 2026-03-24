"""
london_trend.py — NANAMI skill (Model C: LONDON_TREND)

Phase 1 (07:00–09:00 UTC): Enter on Asian range breakout with Kalman velocity
continuation confirmation.

Phase 2 (hold): Ride the directional London flow. Exit on time kill (150 min)
or SL/TP.

Thesis: Asian session (00:00–07:00) compresses XAUUSD. London open (07:00+)
breaks it with institutional directional intent. Join the breakout, ride Phase 2.

Entry conditions:
  1. Session == LONDON_OPEN, UTC hour < 9 (Phase 1 only)
  2. H1 regime is BULLISH_GRIND or BEARISH_GRIND only (not PANIC/BLOWOFF/CHOP)
  3. Asian range >= MIN_RANGE ($5)
  4. 2-bar breakout: current AND previous M5 bar close outside Asian range
  5. Break distance <= 2×ATR (not entering at peak extension — chasing dead move)
  6. Kalman velocity in breakout direction AND |velocity| <= MAX_VEL (not panic)
  7. Regime must agree with breakout direction (BULLISH→BUY, BEARISH→SELL)
  8. H4 bias not opposing breakout direction

SL: max(asian_range/3, 1.5×ATR), clamped [$5, $20]  — ATR floor prevents wick sweeps
TP: SL × 2 (fixed 1:2 RR)
Time kill: 150 min
"""

import uuid
from typing import Optional

import numpy as np
import pandas as pd

from core.constants import (
    MODEL_C,
    RR_RATIO,
    LONDON_TREND_SL_MIN,
    LONDON_TREND_SL_MAX,
    LONDON_TREND_SL_RANGE_FRACTION,
    LONDON_TREND_SL_ATR_MULT,
    LONDON_TREND_MIN_ASIAN_RANGE,
    LONDON_TREND_ATR_MOMENTUM_MULT,
    LONDON_TREND_KALMAN_VEL_THRESHOLD,
    LONDON_TREND_MAX_KALMAN_VEL,
    LONDON_TREND_MAX_BREAK_ATR_MULT,
    LONDON_TREND_ENTRY_CUTOFF_HOUR,
    REGIME_BULLISH_GRIND, REGIME_BEARISH_GRIND,
)

# Only GRIND regimes — BLOWOFF/PANIC have extreme volatility that sweeps tight SLs
_ALLOWED_REGIMES = {REGIME_BULLISH_GRIND, REGIME_BEARISH_GRIND}
_BULLISH_REGIMES = {REGIME_BULLISH_GRIND}
_BEARISH_REGIMES = {REGIME_BEARISH_GRIND}

# ── Module constants ───────────────────────────────────────────────────────────
_ASIAN_END_HOUR    = 7    # Asian session ends at 07:00 UTC
_MIN_ASIAN_BARS    = 12   # minimum bars for a valid Asian range
_ATR_BASELINE_BARS = 60   # bars to compute ATR baseline median
_MIN_BREAK_DIST    = 0.20 # price must close at least $0.20 outside range


# ── Asian range ───────────────────────────────────────────────────────────────

def _get_asian_range(df: pd.DataFrame) -> Optional[tuple]:
    """Return (asian_high, asian_low) from 00:00–07:00 UTC bars today."""
    idx     = df.index
    idx_utc = idx.tz_convert("UTC") if idx.tzinfo is not None else idx
    today   = idx_utc[-1].date()
    mask    = np.array([d == today for d in idx_utc.date]) & (idx_utc.hour < _ASIAN_END_HOUR)
    asian   = df[mask]
    if len(asian) < _MIN_ASIAN_BARS:
        return None
    return float(asian["high"].max()), float(asian["low"].min())


# ── Kalman velocity direction ─────────────────────────────────────────────────

def _kalman_velocity_direction(df: pd.DataFrame,
                                threshold: float,
                                max_vel: float) -> Optional[str]:
    """
    Return "BUY" / "SELL" / None based on Kalman velocity.

    Requirements:
      - Velocity in breakout direction AND above threshold (real momentum)
      - |velocity| <= max_vel (not panic/free-fall — those get swept immediately)
      - Consistent over last 3 bars (not a single spike)
    """
    if "kalman_velocity" not in df.columns:
        return None

    vel = df["kalman_velocity"].values.astype(float)
    if len(vel) < 5:
        return None

    curr_vel    = float(vel[-1])
    recent_mean = float(np.mean(vel[-3:]))

    # Block panic velocities — entering into a free-fall gets swept by a single candle
    if abs(curr_vel) > max_vel:
        return None

    if curr_vel > threshold and recent_mean > threshold * 0.5:
        return "BUY"
    if curr_vel < -threshold and recent_mean < -threshold * 0.5:
        return "SELL"

    return None


# ── Main signal generator ─────────────────────────────────────────────────────

def generate_signal(df_m5_win: pd.DataFrame, session: str,
                    regime: str = "", h4_bias: str = "NEUTRAL") -> Optional[dict]:
    """
    Generate a London Trend signal.

    Args:
        df_m5_win: rolling M5 window (up to 500 bars) with indicators
        session:   current session name — must be LONDON_OPEN
        regime:    H1 regime string
        h4_bias:   H4 SMA50 bias (BULLISH / BEARISH / NEUTRAL)

    Returns:
        signal dict or None
    """
    if session != "LONDON_OPEN":
        return None

    # Gate 1: GRIND regimes only — BLOWOFF/PANIC have ATR spikes that sweep tight SLs
    if regime not in _ALLOWED_REGIMES:
        return None

    # Gate 2: Entry cutoff — Phase 1 only (07:00–09:00 UTC)
    last_idx = df_m5_win.index[-1]
    last_utc = last_idx.tz_convert("UTC") if last_idx.tzinfo is not None else last_idx
    if last_utc.hour >= LONDON_TREND_ENTRY_CUTOFF_HOUR:
        return None

    if len(df_m5_win) < _ATR_BASELINE_BARS + 2:
        return None

    last = df_m5_win.iloc[-1]
    atr  = float(last.get("atr14", 0.0))
    if atr <= 0:
        return None

    # Gate 3: Asian range
    asian_result = _get_asian_range(df_m5_win)
    if asian_result is None:
        return None
    asian_high, asian_low = asian_result
    asian_range = asian_high - asian_low

    if asian_range < LONDON_TREND_MIN_ASIAN_RANGE:
        return None

    # Gate 5: 2-bar breakout confirmation
    current_close = float(last["close"])
    prev_close    = float(df_m5_win.iloc[-2]["close"]) if len(df_m5_win) >= 2 else current_close

    if current_close > asian_high + _MIN_BREAK_DIST and prev_close > asian_high:
        direction     = "BUY"
        break_distance = current_close - asian_high
    elif current_close < asian_low - _MIN_BREAK_DIST and prev_close < asian_low:
        direction     = "SELL"
        break_distance = asian_low - current_close
    else:
        return None

    # Gate 6: Break distance <= 2×ATR — block peak-extension entries
    # Entering $18 above asian_high on a $6 ATR day = chasing a dead move
    if break_distance > atr * LONDON_TREND_MAX_BREAK_ATR_MULT:
        return None

    # Gate 7: Kalman velocity continuation (not flat, not panic)
    kalman_dir = _kalman_velocity_direction(df_m5_win,
                                             LONDON_TREND_KALMAN_VEL_THRESHOLD,
                                             LONDON_TREND_MAX_KALMAN_VEL)
    if kalman_dir is None or kalman_dir != direction:
        return None

    # Gate 8: Regime must agree with breakout direction
    if direction == "BUY" and regime not in _BULLISH_REGIMES:
        return None
    if direction == "SELL" and regime not in _BEARISH_REGIMES:
        return None

    # Gate 9: H4 bias must not oppose breakout direction
    if direction == "BUY" and h4_bias == "BEARISH":
        return None
    if direction == "SELL" and h4_bias == "BULLISH":
        return None

    # SL: max(asian_range/3, 1.5×ATR) — ATR floor prevents single-wick sweeps
    # On panic days (ATR $28): SL = max(338/3, 1.5×28) = max($112, $42) → clamped to $20
    # On normal days (ATR $6): SL = max(40/3, 1.5×6)  = max($13, $9)  = $13
    sl_distance = float(np.clip(
        max(asian_range / LONDON_TREND_SL_RANGE_FRACTION,
            atr * LONDON_TREND_SL_ATR_MULT),
        LONDON_TREND_SL_MIN,
        LONDON_TREND_SL_MAX,
    ))

    entry = current_close
    if direction == "BUY":
        sl = round(entry - sl_distance, 2)
        tp = round(entry + sl_distance * RR_RATIO, 2)
    else:
        sl = round(entry + sl_distance, 2)
        tp = round(entry - sl_distance * RR_RATIO, 2)

    return {
        "id":             str(uuid.uuid4()),
        "model":          MODEL_C,
        "session":        session,
        "direction":      direction,
        "entry_price":    round(entry, 2),
        "sl_price":       sl,
        "tp_price":       tp,
        "sl_distance":    round(sl_distance, 2),
        "atr_at_entry":   round(atr, 2),
        "asian_range":    round(asian_range, 2),
        "break_distance": round(break_distance, 2),
        "h4_bias":        h4_bias,
        "zscore_at_entry":  0.0,
        "hurst_at_entry":   0.0,
        "detrend_method":   "kalman_trend",
        "half_life_bars":   0.0,
        "status":           "PENDING",
    }
