"""
regime_detector.py — NANAMI skill

Multi-factor ensemble regime classifier for XAUUSD.

Architecture decision (research summary)
─────────────────────────────────────────
• HMM / GMM / Wasserstein clustering — REJECTED: require pre-training on
  historical data; non-stationarity of gold (driven by Fed policy,
  geopolitical risk) makes learned transition matrices stale quickly.
• Kalman Filter — useful for trend smoothing, not regime classification.
• Hurst Exponent (Variance Ratio) — EVALUATED, REPLACED:
  The VR overlapping-window estimator has a systematic negative bias on
  typical stochastic market data (random walk with drift), making the
  H > 0.55 TRENDING threshold effectively unreachable (even AR1 phi=0.7
  gives H ≈ 0.53 on 60-bar windows).
• Return Autocorrelation (lag-1..3 average) — SELECTED as secondary signal.
  Directly measures persistence / anti-persistence in log returns without
  the VR bias. Symmetric, more sensitive on 30–60 bar windows, and aligns
  cleanly with the fBm theoretical model:
    ACF(k) = 2^(2H-1) - 1   →   ACF > 0 ↔ H > 0.5 (trending)
                                  ACF < 0 ↔ H < 0.5 (ranging)
  Threshold ±0.10 corresponds to H ≈ 0.53 / 0.47 (conservative, ≈ 0.8σ
  above/below random-walk null for n = 60 observations).

Ensemble design
───────────────
Priority 1 — ATR spike VETO (immediate VOLATILE, no vote)
  If current ATR > ATR_VOLATILE_MULTIPLIER × 20-bar mean ATR, we are in a
  volatility spike (e.g. news event). Return VOLATILE immediately.

Priority 2 — Voting (each factor casts a score):
  ADX (weight 2)
    adx > 25  → +2 trending
    adx < 20  → +2 ranging

  Return Autocorrelation — avg lag 1..3 on last 60 returns (weight 2)
    acf > +0.10 → +2 trending   (persistent returns — momentum works)
    acf < -0.10 → +2 ranging    (anti-persistent — mean-reversion works)
    neutral (|acf| ≤ 0.10) → no vote

  BB Width Percentile (weight 1)
    bb_width_pct < 30th percentile → +1 ranging  (BB squeeze → coiling)
    bb_width_pct > 70th percentile → +1 trending  (BB expansion → breakout)

Decision: regime with score ≥ 2 AND strictly highest wins.
           Ties or insufficient evidence → VOLATILE (safe default, no trade).

Public API
──────────
detect_regime(df) → "TRENDING" | "RANGING" | "VOLATILE"
_return_acf(prices) → float   (exposed for unit-testing)
"""

import logging

import numpy as np
import pandas as pd

from core.constants import (
    ADX_TRENDING_THRESHOLD,
    ADX_RANGING_THRESHOLD,
    ATR_VOLATILE_MULTIPLIER,
)

logger = logging.getLogger(__name__)

REGIME_TRENDING = "TRENDING"
REGIME_RANGING  = "RANGING"
REGIME_VOLATILE = "VOLATILE"

# Return Autocorrelation thresholds
# ACF > +0.10 ↔ approx H > 0.53 → persistent / trending
# ACF < -0.10 ↔ approx H < 0.47 → anti-persistent / ranging
_ACF_TRENDING  =  0.10
_ACF_RANGING   = -0.10
_ACF_LAGS      = 3      # average lags 1..3 for noise robustness
_ACF_LOOKBACK  = 61     # 61 prices → 60 log returns (≈5 hours on M5)
_ACF_MIN_RETS  = 30     # minimum returns for a valid estimate

# Minimum total score for a non-VOLATILE regime call.
_MIN_SCORE = 2


# ─────────────────────────────────────────────────────────────────────────────
# Return Autocorrelation implementation
# ─────────────────────────────────────────────────────────────────────────────

def _return_acf(prices: np.ndarray) -> float:
    """
    Average lag-1..3 autocorrelation of log returns.

    Theory (fractional Brownian motion):
        For fBm with Hurst exponent H:
            ACF(k) = 2^(2H-1) - 1   for all lags k
        H > 0.5  →  ACF > 0  (persistent / trending — momentum works)
        H < 0.5  →  ACF < 0  (anti-persistent / ranging — reversion works)
        H = 0.5  →  ACF = 0  (random walk, no exploitable regime)

    Why ACF over Hurst Variance Ratio:
        The VR overlapping-window estimator has a systematic negative bias
        on finite samples of stochastic data (random walk + drift gives
        H ≈ 0.35–0.40, far below the theoretical 0.50). Direct ACF on log
        returns is symmetric: AR1 phi = ±0.7 reliably gives |ACF| > 0.10.

    Averaging lags 1..3 reduces single-lag noise while remaining responsive
    to short-term momentum / mean-reversion in M5 gold data.

    Args:
        prices: 1-D array of close prices. Minimum _ACF_MIN_RETS + 1 values.

    Returns:
        Average ACF in [-1, 1], or 0.0 (neutral) if insufficient data.
    """
    if len(prices) < _ACF_MIN_RETS + 1:
        return 0.0

    log_returns = np.diff(np.log(prices))
    n = len(log_returns)
    if n < _ACF_MIN_RETS:
        return 0.0

    mean_r = log_returns.mean()
    var_r  = log_returns.var(ddof=1)

    if var_r <= 0:
        return 0.0  # constant prices → neutral

    acf_vals = []
    for k in range(1, _ACF_LAGS + 1):
        if k >= n:
            break
        cov = np.mean(
            (log_returns[:-k] - mean_r) * (log_returns[k:] - mean_r)
        )
        acf_vals.append(cov / var_r)

    if not acf_vals:
        return 0.0

    return float(np.clip(np.mean(acf_vals), -1.0, 1.0))


# ─────────────────────────────────────────────────────────────────────────────
# Main regime classifier
# ─────────────────────────────────────────────────────────────────────────────

def detect_regime(df: pd.DataFrame) -> str:
    """
    Classify the current market regime from the latest M5 indicator data.

    Args:
        df: DataFrame enriched by indicator_engine.add_indicators().
            Must have 'close' column. 'adx14', 'atr14', 'bb_width_pct'
            are used when present; absent columns are silently skipped.

    Returns:
        "TRENDING" | "RANGING" | "VOLATILE"
        Defaults to "VOLATILE" on missing data (no-trade safe default).
    """
    if df.empty or "close" not in df.columns:
        logger.warning("detect_regime: empty or missing close — VOLATILE")
        return REGIME_VOLATILE

    # ── Step 1: ATR spike VETO (highest priority) ─────────────────────────
    if "atr14" in df.columns and len(df) >= 20:
        atr_now  = df["atr14"].iloc[-1]
        atr_mean = df["atr14"].iloc[-20:].mean()
        if (
            not pd.isna(atr_now) and not pd.isna(atr_mean)
            and atr_mean > 0
            and atr_now > ATR_VOLATILE_MULTIPLIER * atr_mean
        ):
            logger.info(
                "ATR spike veto: atr=%.4f > %.1f× mean=%.4f → VOLATILE",
                atr_now, ATR_VOLATILE_MULTIPLIER, atr_mean,
            )
            return REGIME_VOLATILE

    trending_score = 0
    ranging_score  = 0

    # ── Step 2: ADX vote (weight = 2) ────────────────────────────────────
    if "adx14" in df.columns:
        adx = df["adx14"].iloc[-1]
        if not pd.isna(adx):
            if adx > ADX_TRENDING_THRESHOLD:
                trending_score += 2
                logger.debug("ADX=%.1f > %d → +2 trending", adx, ADX_TRENDING_THRESHOLD)
            elif adx < ADX_RANGING_THRESHOLD:
                ranging_score += 2
                logger.debug("ADX=%.1f < %d → +2 ranging", adx, ADX_RANGING_THRESHOLD)
            else:
                logger.debug("ADX=%.1f in dead zone (%.0f–%.0f) → neutral",
                             adx, ADX_RANGING_THRESHOLD, ADX_TRENDING_THRESHOLD)

    # ── Step 3: Return ACF vote (weight = 2) ──────────────────────────────
    # Average lag-1..3 autocorrelation of log returns (last _ACF_LOOKBACK bars).
    # Positive ACF → persistent (trending); negative → anti-persistent (ranging).
    close_arr = df["close"].values
    lookback  = min(_ACF_LOOKBACK, len(close_arr))
    acf = _return_acf(close_arr[-lookback:])

    if acf > _ACF_TRENDING:
        trending_score += 2
        logger.debug("ACF=%.3f > %.2f → +2 trending", acf, _ACF_TRENDING)
    elif acf < _ACF_RANGING:
        ranging_score += 2
        logger.debug("ACF=%.3f < %.2f → +2 ranging", acf, _ACF_RANGING)
    else:
        logger.debug("ACF=%.3f neutral (%.2f–%.2f) → no vote",
                     acf, _ACF_RANGING, _ACF_TRENDING)

    # ── Step 4: BB Width percentile vote (weight = 1) ─────────────────────
    if "bb_width_pct" in df.columns and len(df) >= 50:
        bb_hist = df["bb_width_pct"].iloc[-50:]
        bb_now  = df["bb_width_pct"].iloc[-1]
        if not pd.isna(bb_now) and not bb_hist.isna().all():
            p30 = bb_hist.quantile(0.30)
            p70 = bb_hist.quantile(0.70)
            if bb_now < p30:
                ranging_score += 1
                logger.debug("BB squeeze (%.3f < p30=%.3f) → +1 ranging",
                             bb_now, p30)
            elif bb_now > p70:
                trending_score += 1
                logger.debug("BB expansion (%.3f > p70=%.3f) → +1 trending",
                             bb_now, p70)

    # ── Step 5: Decision ──────────────────────────────────────────────────
    logger.info(
        "Regime vote — trending=%d  ranging=%d  (min_score=%d)  ACF=%.3f",
        trending_score, ranging_score, _MIN_SCORE, acf,
    )

    if trending_score >= _MIN_SCORE and trending_score > ranging_score:
        return REGIME_TRENDING
    if ranging_score >= _MIN_SCORE and ranging_score > trending_score:
        return REGIME_RANGING
    return REGIME_VOLATILE  # tie or insufficient evidence — safe default
