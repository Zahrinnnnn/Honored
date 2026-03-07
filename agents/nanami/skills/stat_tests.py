"""
stat_tests.py — NANAMI skill

Statistical primitives for quantitative regime detection and signal generation.
Used by htf_regime, indicator_engine, ou_grind, and ou_range.

Public API
──────────
rolling_hurst(prices, window=HURST_WINDOW) → float
classify_hurst(h) → str
adf_stationary(series, significance=MODEL_B_ADF_SIGNIFICANCE) → dict
fit_ou(prices, dt=1/1440) → dict | None
ou_zscore(price, ou_params) → float
KalmanPriceFilter — class (constant-velocity Kalman filter)
kalman_velocity(prices, q_scale=MODEL_A_KALMAN_Q_SCALE) → float
compute_macro_bias(prices, window, q_scale) → str
"""

import logging
from math import log, sqrt

import numpy as np

from core.constants import (
    HURST_TRENDING_THRESHOLD,
    HURST_RANGING_THRESHOLD,
    HURST_WINDOW,
    MACRO_BIAS_DOWN,
    MACRO_BIAS_NEUTRAL,
    MACRO_BIAS_Q_SCALE,
    MACRO_BIAS_UP,
    MACRO_BIAS_WINDOW,
    MODEL_A_KALMAN_Q_SCALE,
    MODEL_B_ADF_SIGNIFICANCE,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Hurst Exponent
# ─────────────────────────────────────────────────────────────────────────────

def rolling_hurst(prices: np.ndarray, window: int = HURST_WINDOW) -> float:
    """
    Estimate the Hurst exponent via the rescaled-range method.

    Method: τ(k) = std of k-lagged differences of the price series.
    Fit log(τ) vs log(k) via OLS → slope = H.
    NO ×2 multiplier — fitting std (not variance) gives H directly.

    Returns:
        H ∈ [0.0, 1.0]. Returns 0.5 (neutral / random walk) if the price
        series is shorter than `window` or the fit fails.
    """
    if len(prices) < window:
        return 0.5

    px = prices[-window:]

    # Lags spanning several octaves; each must allow ≥2 differences
    lags = [2, 4, 8, 16, 32, 64]
    lags = [k for k in lags if k < window // 2]
    if len(lags) < 2:
        return 0.5

    tau = []
    for k in lags:
        diffs = px[k:] - px[:-k]
        if len(diffs) < 2:
            return 0.5
        tau.append(float(np.std(diffs, ddof=1)))

    try:
        log_lags = np.log(np.array(lags, dtype=float))
        log_tau  = np.log(np.maximum(tau, 1e-12))
        slope    = float(np.polyfit(log_lags, log_tau, 1)[0])
    except (np.linalg.LinAlgError, ValueError):
        return 0.5

    return float(np.clip(slope, 0.0, 1.0))


def classify_hurst(h: float) -> str:
    """
    Map a Hurst exponent to a regime label.

    Returns:
        "TRENDING"  if H > HURST_TRENDING_THRESHOLD (0.53)
        "RANGING"   if H < HURST_RANGING_THRESHOLD  (0.47)
        "UNDEFINED" otherwise (neutral / random walk)
    """
    if h > HURST_TRENDING_THRESHOLD:
        return "TRENDING"
    if h < HURST_RANGING_THRESHOLD:
        return "RANGING"
    return "UNDEFINED"


# ─────────────────────────────────────────────────────────────────────────────
# ADF Stationarity Test
# ─────────────────────────────────────────────────────────────────────────────

def adf_stationary(
    series: np.ndarray,
    significance: float = MODEL_B_ADF_SIGNIFICANCE,
) -> dict:
    """
    Augmented Dickey-Fuller test for stationarity.

    Uses statsmodels adfuller with maxlag=20, autolag='AIC'.
    Fail-safe: returns {stationary: False} on any error so that
    the Model B gate stays closed rather than opening on bad data.

    Returns:
        {
            "stationary": bool,
            "p_value":    float,
            "verdict":    str ("STATIONARY" | "UNIT_ROOT" | "ERROR"),
        }
    """
    try:
        from statsmodels.tsa.stattools import adfuller  # lazy — heavy import

        max_lag = min(20, len(series) // 3 - 1)
        max_lag = max(max_lag, 1)
        result  = adfuller(series, maxlag=max_lag, autolag="AIC")
        p_value = float(result[1])
        is_stat = p_value < significance
        return {
            "stationary": is_stat,
            "p_value":    round(p_value, 6),
            "verdict":    "STATIONARY" if is_stat else "UNIT_ROOT",
        }
    except Exception as exc:
        logger.warning("ADF test failed: %s — assuming non-stationary (fail-safe)", exc)
        return {"stationary": False, "p_value": 1.0, "verdict": "ERROR"}


# ─────────────────────────────────────────────────────────────────────────────
# Ornstein-Uhlenbeck Process
# ─────────────────────────────────────────────────────────────────────────────

def fit_ou(prices: np.ndarray, dt: float = 1.0 / 1440) -> dict | None:
    """
    Fit an Ornstein-Uhlenbeck model via OLS Vasicek (exact discrete-time).

    Regression: X[t] = a * X[t-1] + b + ε
    Exact discrete solution:  a = e^(-θΔt),  b = μ(1 - a)

    Derived parameters:
        theta         = -log(a) / dt       (mean-reversion speed, 1/day)
        mu            = b / (1 - a)        (long-run mean, price units)
        sigma         = std(ε) / √dt       (instantaneous vol, price/√day)
        sigma_eq      = σ / √(2θ)         (equilibrium std, price units)
        half_life_bars = log(2) / (θ × dt) (reversion half-life in bars)

    Returns:
        dict of {theta, mu, sigma, sigma_eq, half_life_bars}, or
        None if θ ≤ 0 (no mean reversion) or if the fit fails.
    """
    if len(prices) < 10:
        return None

    x = prices[:-1]
    y = prices[1:]

    try:
        coeffs = np.polyfit(x, y, 1)
        a      = float(coeffs[0])
        b      = float(coeffs[1])
    except (np.linalg.LinAlgError, ValueError):
        return None

    # a must be in (0, 1) for stable mean reversion
    if a <= 0.0 or a >= 1.0:
        return None

    try:
        theta = -log(a) / dt
    except (ValueError, ZeroDivisionError):
        return None

    if theta <= 0.0:
        return None

    mu        = b / (1.0 - a)
    residuals = y - (a * x + b)
    sigma     = float(np.std(residuals, ddof=1)) / sqrt(dt)
    sigma_eq  = sigma / sqrt(2.0 * theta)
    half_life = log(2.0) / theta / dt          # in bars

    return {
        "theta":          round(theta,    6),
        "mu":             round(mu,       4),
        "sigma":          round(sigma,    6),
        "sigma_eq":       round(sigma_eq, 6),
        "half_life_bars": round(half_life, 2),
    }


def ou_zscore(price: float, ou_params: dict) -> float:
    """
    Compute the OU z-score: (price - μ) / σ_eq.

    Positive z → price above equilibrium → SELL pressure.
    Negative z → price below equilibrium → BUY pressure.
    Returns 0.0 if sigma_eq is zero or missing.
    """
    sigma_eq = float(ou_params.get("sigma_eq", 0.0))
    if sigma_eq <= 0.0:
        return 0.0
    return (price - float(ou_params["mu"])) / sigma_eq


# ─────────────────────────────────────────────────────────────────────────────
# Kalman Price Filter
# ─────────────────────────────────────────────────────────────────────────────

class KalmanPriceFilter:
    """
    Constant-velocity Kalman filter. State = [price, velocity].

    Tracks price and its rate of change (velocity) simultaneously.
    Velocity > 0 → uptrend. Velocity < 0 → downtrend.
    Replaces M15 EMA50 as Model A higher-timeframe bias filter.

    State transition (F):
        price_t+1    = price_t + velocity_t
        velocity_t+1 = velocity_t              (constant velocity model)
        F = [[1, 1], [0, 1]]

    Observation (H = [1, 0]): we observe price only.

    Process noise Q (discrete Wiener model):
        q = R × q_scale
        Q = [[q/4, q/2], [q/2, q]]

    Observation noise R is calibrated from recent price variance.
    """

    def __init__(self, r: float, q_scale: float = MODEL_A_KALMAN_Q_SCALE):
        q = r * q_scale
        self._F = np.array([[1.0, 1.0], [0.0, 1.0]])
        self._H = np.array([[1.0, 0.0]])
        self._Q = np.array([[q / 4.0, q / 2.0], [q / 2.0, q]])
        self._R = float(r)
        # State and covariance; _x is None until the first observation
        self._x: np.ndarray | None = None
        self._P = np.eye(2) * r * 10.0     # large initial uncertainty

    def update(self, price: float) -> tuple[float, float]:
        """
        Process one price observation.

        Returns:
            (filtered_price, velocity)
        """
        if self._x is None:
            self._x = np.array([[price], [0.0]])

        F, H, Q = self._F, self._H, self._Q

        # Predict
        x_pred = F @ self._x
        P_pred = F @ self._P @ F.T + Q

        # Update (scalar observation — simplified Kalman gain)
        innovation = price - float((H @ x_pred)[0, 0])
        S          = float((H @ P_pred @ H.T)[0, 0]) + self._R
        K          = (P_pred @ H.T) / S          # shape (2, 1)

        self._x = x_pred + K * innovation
        self._P = (np.eye(2) - K @ H) @ P_pred

        return float(self._x[0, 0]), float(self._x[1, 0])

    def fit(self, prices: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """
        Apply the filter to a full price array.

        Returns:
            (filtered_prices, velocities) — both shape (N,).
        """
        n          = len(prices)
        filtered   = np.empty(n)
        velocities = np.empty(n)

        for i, p in enumerate(prices):
            fp, v        = self.update(float(p))
            filtered[i]  = fp
            velocities[i] = v

        return filtered, velocities


def kalman_velocity(
    prices: np.ndarray,
    q_scale: float = MODEL_A_KALMAN_Q_SCALE,
) -> float:
    """
    Fit a KalmanPriceFilter on prices; return the latest velocity.

    Calibrates R from the variance of recent price differences
    (last 50 bars or all bars if fewer than 51).

    Returns 0.0 if len(prices) < 10.
    """
    if len(prices) < 10:
        return 0.0

    tail = prices[-50:] if len(prices) >= 51 else prices
    r    = float(np.var(np.diff(tail)))
    r    = max(r, 1e-8)

    kf      = KalmanPriceFilter(r=r, q_scale=q_scale)
    _, vels = kf.fit(prices)
    return float(vels[-1])


# ─────────────────────────────────────────────────────────────────────────────
# Macro Directional Bias
# ─────────────────────────────────────────────────────────────────────────────

def compute_macro_bias(
    prices: np.ndarray,
    window: int = MACRO_BIAS_WINDOW,
    q_scale: float = MACRO_BIAS_Q_SCALE,
) -> str:
    """
    Determine the macro directional bias from a large M5 price window.

    Uses a smoothed Kalman velocity filter (q_scale=0.001, 10x smoother
    than the intraday 0.01) on the last ``window`` M5 bars to detect the
    dominant trend direction over ~3.5 trading days.

    No dead zone: any positive velocity = UP, any negative = DOWN.
    The large window + small q_scale already filters intraday noise.

    Returns:
        "UP"      if final Kalman velocity > 0
        "DOWN"    if final Kalman velocity < 0
        "NEUTRAL" if insufficient data (< 50 bars)
    """
    if len(prices) < 50:
        return MACRO_BIAS_NEUTRAL

    px = prices[-window:] if len(prices) >= window else prices

    tail = px[-50:] if len(px) >= 51 else px
    r = float(np.var(np.diff(tail)))
    r = max(r, 1e-8)

    kf = KalmanPriceFilter(r=r, q_scale=q_scale)
    _, vels = kf.fit(px)
    vel = float(vels[-1])

    if vel > 0:
        return MACRO_BIAS_UP
    if vel < 0:
        return MACRO_BIAS_DOWN
    return MACRO_BIAS_NEUTRAL
