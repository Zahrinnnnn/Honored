"""
kalman_trend.py — NANAMI skill (Model C)

Kalman-velocity trend-following model.

Thesis: When Kalman velocity is consistently strong and sustained in one
direction for N consecutive M5 bars, a short-term trend is established.
Trade WITH it — the complement of Model A (which fades deviations from mean)
and Model B (which trades velocity flips at London extremes).

Entry conditions:
  - kalman_velocity > +VEL_THRESHOLD for CONSEC_BARS consecutive bars → BUY
  - kalman_velocity < -VEL_THRESHOLD for CONSEC_BARS consecutive bars → SELL

Filters:
  - Hurst > KALMAN_TREND_HURST_MIN (0.55): confirms trending environment.
    Model A gates OUT at Hurst > 0.53, so Model A and C never fire together.
  - ATR not spiking above 2× 60-bar baseline (no news spikes)
  - H4 bias gate: BUY blocked when H4=BEARISH, SELL when H4=BULLISH
  - No regime restriction

Sessions: All active (LONDON_OPEN, NY_OVERLAP, NY_CLOSE)
Cap: 3 per session
SL: 1.5 × ATR14, clamped [$6, $12]
TP: SL × 2 (fixed 1:2 RR)
Time kill: 90 min
"""

import uuid
from typing import Optional

import numpy as np
import pandas as pd

from agents.nanami.skills.stat_tests import rolling_hurst
from core.constants import (
    KALMAN_TREND_CONSEC_BARS,
    KALMAN_TREND_HURST_MIN,
    KALMAN_TREND_VEL_THRESHOLD,
    MODEL_C,
    OU_SL_ATR_MULT,
    OU_SL_MAX,
    OU_SL_MIN,
    RR_RATIO,
)

_ATR_SPIKE_MULT    = 2.0
_ATR_BASELINE_BARS = 60
_MIN_BARS_NEEDED   = 80   # enough for kalman warm-up + hurst window
_HURST_WINDOW      = 100  # bars for local Hurst estimate


def generate_signal(
    df_m5: pd.DataFrame,
    session: str,
    h4_bias: str = "NEUTRAL",
) -> Optional[dict]:
    """
    Generate a Model C signal: Kalman velocity trend following.

    Args:
        df_m5:    Rolling M5 window with indicators (kalman_velocity, atr14).
        session:  Current session name.
        h4_bias:  H4 SMA50 bias — gates direction.

    Returns:
        Signal dict or None.
    """
    if len(df_m5) < _MIN_BARS_NEEDED:
        return None

    last = df_m5.iloc[-1]
    atr  = float(last.get("atr14", 0.0))
    if atr <= 0 or pd.isna(atr):
        return None

    closes = df_m5["close"].values.astype(float)

    # ── ATR spike filter ──────────────────────────────────────────────────
    atr_series = df_m5["atr14"].values.astype(float)
    if len(atr_series) >= _ATR_BASELINE_BARS:
        baseline = float(np.median(atr_series[-_ATR_BASELINE_BARS:]))
        if baseline > 0 and atr > baseline * _ATR_SPIKE_MULT:
            return None  # volatility spike — trend is noise here

    # ── Hurst filter: must be trending (> 0.55) ───────────────────────────
    # Use a local window so recent trend conditions dominate
    hurst_window = closes[-_HURST_WINDOW:] if len(closes) >= _HURST_WINDOW else closes
    if len(hurst_window) < 50:
        return None
    hurst_val = rolling_hurst(hurst_window)
    if hurst_val <= KALMAN_TREND_HURST_MIN:
        return None  # not trending enough — leave it for Model A

    # ── Kalman velocity: sustained in same direction for N bars ───────────
    if "kalman_velocity" not in df_m5.columns:
        return None

    vel = df_m5["kalman_velocity"].values.astype(float)
    if len(vel) < KALMAN_TREND_CONSEC_BARS:
        return None

    recent_vel = vel[-KALMAN_TREND_CONSEC_BARS:]
    if np.any(np.isnan(recent_vel)):
        return None

    if all(v > KALMAN_TREND_VEL_THRESHOLD for v in recent_vel):
        direction = "BUY"
    elif all(v < -KALMAN_TREND_VEL_THRESHOLD for v in recent_vel):
        direction = "SELL"
    else:
        return None  # velocity not consistently directional

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

    avg_vel = float(np.mean(np.abs(recent_vel)))

    return {
        "id":              str(uuid.uuid4()),
        "model":           MODEL_C,
        "session":         session,
        "direction":       direction,
        "entry_price":     entry,
        "sl_price":        sl_price,
        "tp_price":        tp_price,
        "sl_distance":     sl_distance,
        "atr_at_entry":    round(atr, 2),
        # Entry context for MAHORAGA analysis
        "hurst_at_entry":  round(float(hurst_val), 4),
        "zscore_at_entry": 0.0,   # not OU-based
        "detrend_method":  "kalman_trend",
        "kalman_vel_avg":  round(avg_vel, 5),
        "h4_bias":         h4_bias,
        "status":          "PENDING",
        "reason": (
            f"Kalman trend | dir={direction} | vel_avg={avg_vel:.4f} "
            f"| hurst={hurst_val:.3f} | ATR={atr:.2f} | session={session}"
        ),
    }
