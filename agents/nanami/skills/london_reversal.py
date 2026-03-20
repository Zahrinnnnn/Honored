"""
london_reversal.py — NANAMI skill (Model B)

Multi-signal fakeout/reversal model for London Open session.
Fires during LONDON_OPEN (07:30–10:00 UTC).

Thesis: London stop hunts leave 4 quantifiable fingerprints simultaneously:
  1. N-bar consecutive exhaustion  — the sweep ran for ≥3 bars (statistical improbability)
  2. CUSUM reversal               — cumulative flow peaked and is turning (sequential detection)
  3. Volume climax absorption     — high volume + small body = institutions absorbing retail
  4. Kalman velocity flip         — state estimator confirms momentum direction changed

Each component scores points. Kalman flip is mandatory (it's the reversal trigger).
Remaining components are confirmation. Fire when total score ≥ 3.

Scoring:
  Kalman velocity flip:       +2  (REQUIRED — entry trigger)
  N-bar exhaustion ≥3 bars:  +1  (setup — sweep happened)
  N-bar exhaustion ≥4 bars:  +2  (stronger setup)
  CUSUM built + reversing:    +1  (structural — cumulative flow turning)
  Volume climax absorption:   +1  (intensity — institutional footprint)

Minimum score to fire: 3 (Kalman required + at least 1 confirmation)

Gates:
  - Session == LONDON_OPEN
  - Kalman flip mandatory
  - Score >= 3
  - Asian range broken in sweep direction (stop hunt confirmed)
  - H4 bias does not oppose reversal direction
  - ATR not spiking

SL: 1.5 × ATR14, clamped [$6, $12]
TP: SL × 2 (fixed 1:2 RR)
"""

import uuid
from typing import Optional

import numpy as np
import pandas as pd

from core.constants import (
    MODEL_B,
    OU_SL_ATR_MULT,
    RR_RATIO,
)

# ── Constants ────────────────────────────────────────────────────────────────
_ASIAN_END_HOUR      = 7
_MIN_ASIAN_BARS      = 12
_MIN_ASIAN_RANGE     = 3.0       # $3 minimum overnight range

_MIN_ENTRY_HOUR      = 8         # block 07:xx entries — stop hunt in progress, reversal unconfirmed
_MIN_SCORE           = 3         # Kalman flip (2pts) + any 1 confirmation (1pt)
_KALMAN_LOOKBACK     = 5         # bars to look back for velocity flip
_CONSEC_MIN          = 3         # minimum consecutive bars for exhaustion signal
_CUSUM_WINDOW        = 20        # bars for CUSUM accumulation
_CUSUM_ATR_MULT      = 1.5       # CUSUM must exceed N×ATR to count as significant sweep
_CUSUM_REVERSAL_BARS = 2         # last N bars must oppose CUSUM direction
_VOL_CLIMAX_MULT     = 2.5       # volume must exceed N× 20-bar average
_VOL_BODY_RATIO      = 0.35      # |close-open|/(high-low) < threshold = small body (absorption)
_ATR_SPIKE_MULT      = 1.8
_ATR_BASELINE_BARS   = 60
_MIN_BREAKOUT_DIST   = 0.3       # price must have broken $0.30 outside Asian range
_SL_MIN              = 6.0
_SL_MAX              = 12.0


# ── Sub-signals ──────────────────────────────────────────────────────────────

def _get_asian_range(df: pd.DataFrame) -> Optional[tuple]:
    idx     = df.index
    idx_utc = idx.tz_convert("UTC") if idx.tzinfo is not None else idx
    today   = idx_utc[-1].date()
    mask    = np.array([d == today for d in idx_utc.date]) & (idx_utc.hour < _ASIAN_END_HOUR)
    asian   = df[mask]
    if len(asian) < _MIN_ASIAN_BARS:
        return None
    return float(asian["high"].max()), float(asian["low"].min())


def _kalman_velocity_flip(df: pd.DataFrame, lookback: int = _KALMAN_LOOKBACK):
    """
    Detect Kalman velocity sign flip in the last `lookback` bars.
    Returns ("BUY", score) if velocity flipped from negative→positive (down→up reversal)
            ("SELL", score) if velocity flipped from positive→negative (up→down reversal)
            (None, 0) if no flip.
    """
    if "kalman_velocity" not in df.columns:
        return None, 0

    vel = df["kalman_velocity"].values.astype(float)
    if len(vel) < lookback + 1:
        return None, 0

    recent   = vel[-lookback:]
    prev_vel = vel[-(lookback + 1)]
    curr_vel = vel[-1]

    # Current velocity must be in opposite direction to prior
    if prev_vel < 0 and curr_vel > 0:
        # Was falling, now rising → BUY reversal
        # Confirm: at least one bar in the window was strongly negative before the flip
        if any(v < -0.02 for v in recent[:lookback // 2]):
            return "BUY", 2
    elif prev_vel > 0 and curr_vel < 0:
        # Was rising, now falling → SELL reversal
        if any(v > 0.02 for v in recent[:lookback // 2]):
            return "SELL", 2

    return None, 0


def _consecutive_exhaustion(closes: np.ndarray, min_bars: int = _CONSEC_MIN):
    """
    Count consecutive same-direction closes ending at the current bar.
    Returns (direction_of_exhausted_move, score, count).
    direction = direction of the SWEEP (not the reversal).
    """
    if len(closes) < min_bars + 1:
        return None, 0, 0

    count = 1
    i = len(closes) - 2
    while i >= 0 and closes[i + 1] < closes[i]:   # consecutive downs
        count += 1
        i -= 1

    if count >= min_bars:
        score = 1 if count == min_bars else 2      # 3 bars = +1, 4+ bars = +2
        return "BUY", score, count                 # sweep was down → reversal is BUY

    count = 1
    i = len(closes) - 2
    while i >= 0 and closes[i + 1] > closes[i]:   # consecutive ups
        count += 1
        i -= 1

    if count >= min_bars:
        score = 1 if count == min_bars else 2
        return "SELL", score, count                # sweep was up → reversal is SELL

    return None, 0, 0


def _cusum_reversing(closes: np.ndarray, atr: float,
                     window: int = _CUSUM_WINDOW,
                     reversal_bars: int = _CUSUM_REVERSAL_BARS):
    """
    CUSUM sequential change detector.
    Returns (direction, score) where direction is the REVERSAL direction.

    Logic:
      - Compute cumulative sum of returns over `window` bars
      - If CUSUM < -threshold (strong down sweep) AND last N returns are positive → BUY reversal
      - If CUSUM > +threshold (strong up sweep)  AND last N returns are negative → SELL reversal
    """
    if len(closes) < window + 1 or atr <= 0:
        return None, 0

    returns = np.diff(closes[-(window + 1):])  # window returns
    cusum   = float(np.sum(returns))
    threshold = _CUSUM_ATR_MULT * atr

    last_returns = returns[-reversal_bars:]

    if cusum < -threshold and all(r > 0 for r in last_returns):
        return "BUY", 1   # big down sweep, now ticking up
    if cusum > threshold and all(r < 0 for r in last_returns):
        return "SELL", 1  # big up sweep, now ticking down

    return None, 0


def _volume_climax(df: pd.DataFrame, window: int = 20):
    """
    Detect volume climax absorption candle.
    Returns (direction, score) where direction is the REVERSAL direction.

    Conditions:
      - Current bar volume > VOL_CLIMAX_MULT × mean(last 20 bars)
      - Body ratio < VOL_BODY_RATIO (small body = absorption, not continuation)
      - Bar direction (open→close) gives reversal direction
    """
    if len(df) < window + 1:
        return None, 0
    if "volume" not in df.columns:
        return None, 0

    last   = df.iloc[-1]
    vol    = float(last.get("volume", 0))
    hi     = float(last["high"])
    lo     = float(last["low"])
    op     = float(last["open"])
    cl     = float(last["close"])

    bar_range = hi - lo
    if bar_range <= 0 or vol <= 0:
        return None, 0

    body_ratio = abs(cl - op) / bar_range
    if body_ratio >= _VOL_BODY_RATIO:
        return None, 0   # large body = continuation, not absorption

    vol_arr  = df["volume"].values.astype(float)[-(window + 1):-1]
    vol_arr  = vol_arr[vol_arr > 0]
    if len(vol_arr) == 0:
        return None, 0
    avg_vol  = float(np.mean(vol_arr))

    if vol < _VOL_CLIMAX_MULT * avg_vol:
        return None, 0   # not a climax volume bar

    # Direction: where did price CLOSE relative to open? Small body pointing up = BUY
    if cl >= op:
        return "BUY", 1   # absorbed sellers, closed near high end
    else:
        return "SELL", 1  # absorbed buyers, closed near low end


# ── Main signal generator ─────────────────────────────────────────────────────

def generate_signal(df_m5_win: pd.DataFrame, session: str,
                    regime: str = "", h4_bias: str = "NEUTRAL") -> Optional[dict]:
    """
    Generate a London reversal signal using a 4-component scoring system.

    Kalman velocity flip is mandatory. Remaining components (N-bar exhaustion,
    CUSUM, volume climax) add to the score. Fire when score >= 3.

    Args:
        df_m5_win: rolling M5 window (up to 500 bars) with indicators
        session:   current session name
        regime:    unused — model is regime-agnostic
        h4_bias:   H4 SMA50 bias

    Returns:
        signal dict or None
    """
    if session != "LONDON_OPEN":
        return None

    # Block first 30 min of London (07:30–08:00) — stop hunt still in progress
    last_idx = df_m5_win.index[-1]
    last_utc = last_idx.tz_convert("UTC") if last_idx.tzinfo is not None else last_idx
    if last_utc.hour < _MIN_ENTRY_HOUR:
        return None

    if len(df_m5_win) < max(_ATR_BASELINE_BARS + 1, _CUSUM_WINDOW + 2):
        return None

    last = df_m5_win.iloc[-1]
    atr  = float(last.get("atr14", 0.0))
    if atr <= 0:
        return None

    # ── ATR spike filter ─────────────────────────────────────────────────────
    atr_arr    = df_m5_win["atr14"].values.astype(float)
    valid_atrs = atr_arr[-_ATR_BASELINE_BARS:][atr_arr[-_ATR_BASELINE_BARS:] > 0]
    if len(valid_atrs) == 0:
        return None
    if atr > float(np.median(valid_atrs)) * _ATR_SPIKE_MULT:
        return None

    closes = df_m5_win["close"].values.astype(float)

    # ── Component 1: Kalman velocity flip (MANDATORY) ────────────────────────
    kalman_dir, kalman_score = _kalman_velocity_flip(df_m5_win)
    if kalman_dir is None:
        return None   # no flip → no signal regardless of other components

    reversal_direction = kalman_dir
    total_score        = kalman_score

    # ── Component 2: N-bar consecutive exhaustion ────────────────────────────
    consec_dir, consec_score, consec_count = _consecutive_exhaustion(closes)
    if consec_dir == reversal_direction:
        total_score += consec_score

    # ── Component 3: CUSUM reversal ──────────────────────────────────────────
    cusum_dir, cusum_score = _cusum_reversing(closes, atr)
    if cusum_dir == reversal_direction:
        total_score += cusum_score

    # ── Component 4: Volume climax absorption ────────────────────────────────
    vol_dir, vol_score = _volume_climax(df_m5_win)
    if vol_dir == reversal_direction:
        total_score += vol_score

    if total_score < _MIN_SCORE:
        return None

    # ── Asian range context (stop hunt confirmation) ──────────────────────────
    asian_result = _get_asian_range(df_m5_win)
    stop_hunt_confirmed = False
    if asian_result:
        asian_high, asian_low = asian_result
        if asian_high - asian_low >= _MIN_ASIAN_RANGE:
            current_close = closes[-1]
            if reversal_direction == "BUY":
                # Stop hunt: price broke below asian_low and is now reversing up
                lows_today = df_m5_win["low"].values.astype(float)
                stop_hunt_confirmed = (
                    float(np.min(lows_today[-_KALMAN_LOOKBACK:])) < asian_low - _MIN_BREAKOUT_DIST
                    and current_close > asian_low
                )
            else:
                # Stop hunt: price broke above asian_high and is now reversing down
                highs_today = df_m5_win["high"].values.astype(float)
                stop_hunt_confirmed = (
                    float(np.max(highs_today[-_KALMAN_LOOKBACK:])) > asian_high + _MIN_BREAKOUT_DIST
                    and current_close < asian_high
                )

    # Require stop hunt confirmation — without it we're just guessing a reversal
    if not stop_hunt_confirmed:
        return None

    # ── H4 bias gate ─────────────────────────────────────────────────────────
    if reversal_direction == "BUY" and h4_bias == "BEARISH":
        return None
    if reversal_direction == "SELL" and h4_bias == "BULLISH":
        return None

    # ── SL / TP ──────────────────────────────────────────────────────────────
    current_close = float(last["close"])
    sl_distance   = float(np.clip(OU_SL_ATR_MULT * atr, _SL_MIN, _SL_MAX))

    if reversal_direction == "BUY":
        entry = current_close
        sl    = round(entry - sl_distance, 2)
        tp    = round(entry + sl_distance * RR_RATIO, 2)
    else:
        entry = current_close
        sl    = round(entry + sl_distance, 2)
        tp    = round(entry - sl_distance * RR_RATIO, 2)

    return {
        "id":                  str(uuid.uuid4()),
        "model":               MODEL_B,
        "session":             session,
        "direction":           reversal_direction,
        "entry_price":         round(entry, 2),
        "sl_price":            sl,
        "tp_price":            tp,
        "sl_distance":         round(sl_distance, 2),
        "atr_at_entry":        round(float(atr), 2),
        # Entry context — stored in trades table for MAHORAGA analysis
        "zscore_at_entry":     0.0,    # not OU-based — N/A
        "hurst_at_entry":      0.0,    # not computed for Model B
        "detrend_method":      "kalman",
        "score":               total_score,
        "kalman_score":        kalman_score,
        "consec_count":        consec_count,
        "consec_score":        consec_score if consec_dir == reversal_direction else 0,
        "cusum_score":         cusum_score if cusum_dir == reversal_direction else 0,
        "vol_score":           vol_score if vol_dir == reversal_direction else 0,
        "stop_hunt_confirmed": stop_hunt_confirmed,
        "h4_bias":             h4_bias,
        "status":              "PENDING",
    }
