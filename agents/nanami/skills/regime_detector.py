"""
regime_detector.py — NANAMI skill

Multi-factor ensemble regime classifier for XAUUSD.

Architecture decision (research summary)
─────────────────────────────────────────
• HMM / GMM / Wasserstein clustering — REJECTED: require pre-training on
  historical data; non-stationarity of gold (driven by Fed policy,
  geopolitical risk) makes learned transition matrices stale quickly.
• Kalman Filter — useful for trend smoothing, not regime classification.
• Hurst Exponent (Variance Ratio method) — SELECTED as secondary signal.
  Most robust implementation for heavy-tailed commodity data per:
  "Improvement in Hurst Exponent Estimation" (MDPI Financial Innovation 2022).
  Variance Ratio outperforms R/S and DFA on short samples (60–200 bars)
  typical in intraday commodity trading.

Ensemble design
───────────────
Priority 1 — ATR spike VETO (immediate VOLATILE, no vote)
  If current ATR > ATR_VOLATILE_MULTIPLIER × 20-bar mean ATR, we are in a
  volatility spike (e.g. news event). Return VOLATILE immediately — no model
  should trade into unknown volatility.

Priority 2 — Voting (each factor casts a score):
  ADX (weight 2)
    adx > 25  → +2 trending
    adx < 20  → +2 ranging

  Hurst Exponent — Variance Ratio method (weight 2)
    H > 0.55  → +2 trending   (persistent returns — momentum works)
    H < 0.45  → +2 ranging    (anti-persistent — mean-reversion works)
    0.45–0.55 → neutral        (random walk, no structural edge)
    Uses last 120 bars (≈10 hours of M5) for estimation.

  BB Width Percentile (weight 1)
    bb_width_pct < 30th percentile → +1 ranging  (BB squeeze → coiling)
    bb_width_pct > 70th percentile → +1 trending  (BB expansion → breakout)

Decision: regime with score ≥ 2 AND strictly highest wins.
           Ties or insufficient evidence → VOLATILE (safe default, no trade).

Public API
──────────
detect_regime(df) → "TRENDING" | "RANGING" | "VOLATILE"
_hurst_vr(prices) → float   (exposed for unit-testing)
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

# Hurst thresholds
# Ref: Macrosynergy "Detecting trends and mean reversion with the Hurst exponent"
#      Anti-Persistent Values of the Hurst Exponent (MDPI Mathematics 2024)
_HURST_TRENDING   = 0.55   # H > 0.55 → persistent (trending) regime
_HURST_RANGING    = 0.45   # H < 0.45 → anti-persistent (mean-reverting)
_HURST_MIN_PRICES = 60     # Minimum prices for a meaningful Hurst estimate
_HURST_LOOKBACK   = 120    # Bars used for Hurst computation (≈10 h on M5)

# Minimum total score for a non-VOLATILE regime call.
# Prevents weak single-factor signals from triggering trades.
_MIN_SCORE = 2


# ─────────────────────────────────────────────────────────────────────────────
# Hurst Exponent — Variance Ratio implementation
# ─────────────────────────────────────────────────────────────────────────────

def _hurst_vr(prices: np.ndarray) -> float:
    """
    Hurst Exponent via log-variance regression across lags (Variance Ratio).

    Theory (fractional Brownian motion):
        Var[ X(t+τ) − X(t) ] ∝ τ^(2H)
        ⟹ log(Var) = 2H · log(τ) + const
        ⟹ slope of log-log OLS = 2H  ⟹  H = slope / 2

    Why Variance Ratio over R/S or DFA:
        For heavy-tailed return distributions (commodity futures / XAUUSD),
        Variance Ratio produces the least-biased estimate on short samples
        (60–200 bars) — MDPI Financial Innovation 2022 comparative study.

    Interpretation:
        H > 0.55  → persistent / trending   (use momentum strategies)
        H < 0.45  → anti-persistent / mean-reverting (use reversion)
        H ≈ 0.50  → random walk (no exploitable structural regime)

    Args:
        prices: 1-D array of close prices (not returns). Minimum 60 values.

    Returns:
        H in [0, 1], or 0.5 (neutral) if insufficient data.
    """
    n = len(prices)
    if n < _HURST_MIN_PRICES:
        return 0.5  # not enough data → neutral, cast no regime vote

    log_p = np.log(prices)

    # Use lags from 2 to min(n // 4, 20)
    # n // 4 ensures each lag has at least 4× the lag in data points,
    # preventing spurious variance estimates at large lags.
    max_lag = min(n // 4, 20)
    if max_lag < 4:
        return 0.5

    lags = np.arange(2, max_lag + 1)
    variances = np.array([
        np.var(log_p[lag:] - log_p[:-lag], ddof=1)
        for lag in lags
    ])

    if np.any(variances <= 0):
        return 0.5  # degenerate (e.g. constant price) → neutral

    slope = np.polyfit(np.log(lags), np.log(variances), 1)[0]
    return float(np.clip(slope / 2.0, 0.0, 1.0))


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

    # ── Step 3: Hurst Exponent VR vote (weight = 2) ───────────────────────
    # Use the last _HURST_LOOKBACK bars; falls back to neutral (H=0.5)
    # if insufficient data or degenerate prices.
    close_arr = df["close"].values
    lookback  = min(_HURST_LOOKBACK, len(close_arr))
    H = _hurst_vr(close_arr[-lookback:])

    if H > _HURST_TRENDING:
        trending_score += 2
        logger.debug("Hurst=%.3f > %.2f → +2 trending", H, _HURST_TRENDING)
    elif H < _HURST_RANGING:
        ranging_score += 2
        logger.debug("Hurst=%.3f < %.2f → +2 ranging", H, _HURST_RANGING)
    else:
        logger.debug("Hurst=%.3f neutral (%.2f–%.2f) → no vote",
                     H, _HURST_RANGING, _HURST_TRENDING)

    # ── Step 4: BB Width percentile vote (weight = 1) ─────────────────────
    if "bb_width_pct" in df.columns and len(df) >= 50:
        bb_hist = df["bb_width_pct"].iloc[-50:]
        bb_now  = df["bb_width_pct"].iloc[-1]
        if not pd.isna(bb_now) and not bb_hist.isna().all():
            p30 = bb_hist.quantile(0.30)
            p70 = bb_hist.quantile(0.70)
            if bb_now < p30:
                ranging_score += 1   # BB squeeze: bands tightening → coiling
                logger.debug("BB squeeze (%.3f < p30=%.3f) → +1 ranging",
                             bb_now, p30)
            elif bb_now > p70:
                trending_score += 1  # BB expansion: bands widening → breakout
                logger.debug("BB expansion (%.3f > p70=%.3f) → +1 trending",
                             bb_now, p70)

    # ── Step 5: Decision ──────────────────────────────────────────────────
    logger.info(
        "Regime vote — trending=%d  ranging=%d  (min_score=%d)  H=%.3f",
        trending_score, ranging_score, _MIN_SCORE, H,
    )

    if trending_score >= _MIN_SCORE and trending_score > ranging_score:
        return REGIME_TRENDING
    if ranging_score >= _MIN_SCORE and ranging_score > trending_score:
        return REGIME_RANGING
    return REGIME_VOLATILE  # tie or insufficient evidence — safe default
