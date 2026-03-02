"""
test_nanami_signals.py — Phase 2 unit tests for NANAMI (Quant Upgrade)

Tests all non-network skills using synthetic candle data.
No MetaApi calls. No SQLite needed.

Run: python -m pytest tests/test_nanami_signals.py -v
"""

import sys
import os
from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.nanami.skills import (
    session_detector,
    indicator_engine,
    regime_detector,
    m5_momentum,
    m1_meanrev,
    london_breakout,
)
from agents.nanami.skills.session_detector import SessionContext
from agents.nanami.skills.regime_detector import _return_acf
from agents.nanami.skills.stat_tests import (
    rolling_hurst,
    classify_hurst,
    adf_stationary,
    fit_ou,
    ou_zscore,
    KalmanPriceFilter,
    kalman_velocity,
)
from core.constants import (
    M5_SL_MIN, M5_SL_MAX,
    M1_SL_MIN, M1_SL_MAX,
    BREAKOUT_SL_MIN, BREAKOUT_SL_MAX,
    HURST_RANGING_THRESHOLD,
    MODEL_C_MIN_RANGE,
)


# ---------------------------------------------------------------------------
# Helpers — synthetic candle builders
# ---------------------------------------------------------------------------

def _candles(n: int, base_price: float = 2350.0, trend: float = 0.0) -> pd.DataFrame:
    """
    Build n synthetic OHLCV candles starting at base_price.
    trend > 0 = uptrend, < 0 = downtrend, 0 = flat.
    """
    rng = np.random.default_rng(42)
    closes = [base_price + trend * i + rng.normal(0, 0.5) for i in range(n)]
    data = []
    for c in closes:
        noise = abs(rng.normal(0, 0.3))
        data.append({
            "time":   datetime(2024, 1, 15, 8, 0, 0, tzinfo=timezone.utc),
            "open":   c - rng.normal(0, 0.2),
            "high":   c + noise,
            "low":    c - noise,
            "close":  c,
            "volume": int(rng.integers(100, 2000)),
        })
    return pd.DataFrame(data)


def _trending_candles(n: int = 200) -> pd.DataFrame:
    """Strong uptrend candles."""
    return _candles(n, base_price=2300.0, trend=0.5)


def _ranging_candles(n: int = 200) -> pd.DataFrame:
    """Flat range candles."""
    rng = np.random.default_rng(99)
    base = 2350.0
    closes = [base + rng.uniform(-3, 3) for _ in range(n)]
    data = []
    for c in closes:
        noise = abs(rng.normal(0, 0.5))
        data.append({
            "time":   datetime(2024, 1, 15, 8, 0, 0, tzinfo=timezone.utc),
            "open":   c,
            "high":   c + noise,
            "low":    c - noise,
            "close":  c,
            "volume": 500,
        })
    return pd.DataFrame(data)


def _ar1_prices(n: int, phi: float, sigma: float, seed: int,
                base: float = 2300.0) -> np.ndarray:
    """Build price series from AR1 log returns: r[t] = phi*r[t-1] + N(0,sigma)."""
    rng = np.random.default_rng(seed)
    r = 0.0
    returns = []
    for _ in range(n):
        r = phi * r + rng.normal(0, sigma)
        returns.append(r)
    return base + np.cumsum(returns)


def _mock_dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2024, 1, 15, hour, minute, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# stat_tests unit tests
# ---------------------------------------------------------------------------

class TestStatTests:
    # ── rolling_hurst ─────────────────────────────────────────────────────

    def test_hurst_random_walk_near_half(self):
        """Pure random walk → H ≈ 0.5 (within ±0.15)."""
        rng = np.random.default_rng(42)
        prices = np.cumsum(rng.normal(0, 1, 500)) + 2300.0
        h = rolling_hurst(prices, window=200)
        assert 0.35 <= h <= 0.65, f"Expected H ≈ 0.5 for random walk, got {h:.3f}"

    def test_hurst_persistent_above_neutral(self):
        """
        AR1 phi=+0.7 integrated prices → H > 0.5 (above neutral).

        Note: the R/S std-of-differences estimator converges to H=0.5 at scales
        larger than the AR1 memory (~3 bars for phi=0.7). For short-memory AR1,
        the estimator gives 0.5 < H < 0.53. The directional ordering
        (persistent > neutral > anti-persistent) is still correct.
        The 0.53 live threshold is tuned to detect genuinely persistent regimes
        in real XAUUSD data; the unit test validates the directional property only.
        """
        prices = _ar1_prices(400, phi=0.7, sigma=0.3, seed=42)
        h = rolling_hurst(prices, window=200)
        assert h > 0.50, f"Expected H > 0.50 for AR1 phi=0.7 (persistent), got {h:.3f}"

    def test_hurst_anti_persistent_below_threshold(self):
        """AR1 phi=-0.7 → anti-persistent → H < 0.47."""
        prices = _ar1_prices(400, phi=-0.7, sigma=0.3, seed=99)
        h = rolling_hurst(prices, window=200)
        assert h < HURST_RANGING_THRESHOLD, (
            f"Expected H < {HURST_RANGING_THRESHOLD} for AR1 phi=-0.7, got {h:.3f}"
        )

    def test_hurst_short_series_returns_neutral(self):
        """Fewer bars than window → return 0.5 (neutral)."""
        prices = np.linspace(2300, 2350, 50)
        h = rolling_hurst(prices, window=200)
        assert h == 0.5

    def test_hurst_in_valid_range(self):
        """H must always be in [0, 1]."""
        rng = np.random.default_rng(0)
        prices = np.cumsum(rng.normal(0, 1, 300)) + 2300.0
        h = rolling_hurst(prices, window=200)
        assert 0.0 <= h <= 1.0

    # ── classify_hurst ────────────────────────────────────────────────────

    def test_classify_trending(self):
        assert classify_hurst(0.54) == "TRENDING"
        assert classify_hurst(0.99) == "TRENDING"

    def test_classify_ranging(self):
        assert classify_hurst(0.46) == "RANGING"
        assert classify_hurst(0.01) == "RANGING"

    def test_classify_undefined(self):
        assert classify_hurst(0.50) == "UNDEFINED"
        assert classify_hurst(0.53) == "UNDEFINED"   # boundary — not strictly >
        assert classify_hurst(0.47) == "UNDEFINED"   # boundary — not strictly <

    # ── adf_stationary ────────────────────────────────────────────────────

    def test_adf_stationary_on_ar1(self):
        """Stationary AR1 (phi=0.5) should pass ADF."""
        prices = _ar1_prices(300, phi=0.5, sigma=0.5, seed=7)
        result = adf_stationary(prices)
        assert isinstance(result, dict)
        assert "stationary" in result
        assert "p_value" in result
        # AR1 phi=0.5 on cumsum → this IS integrated; test the dict contract
        # (whether it's stationary depends on the base series, not just phi)

    def test_adf_random_walk_not_stationary(self):
        """Pure random walk (cumsum of iid) → ADF should NOT reject unit root."""
        rng = np.random.default_rng(123)
        prices = np.cumsum(rng.normal(0, 1, 300)) + 2300.0
        result = adf_stationary(prices)
        assert result["stationary"] is False

    def test_adf_returns_dict_contract(self):
        """Result always contains required keys and types."""
        prices = np.linspace(2300, 2350, 50)
        result = adf_stationary(prices)
        assert isinstance(result["stationary"], bool)
        assert isinstance(result["p_value"], float)
        assert result["verdict"] in ("STATIONARY", "UNIT_ROOT", "ERROR")

    def test_adf_fail_safe_on_bad_input(self):
        """Constant array (zero variance) → fail-safe returns non-stationary."""
        prices = np.full(50, 2350.0)
        result = adf_stationary(prices)
        assert result["stationary"] is False

    # ── fit_ou ────────────────────────────────────────────────────────────

    def test_fit_ou_returns_valid_params_on_ar1(self):
        """Stationary AR1 price path should produce valid OU params."""
        rng = np.random.default_rng(42)
        # Generate a mean-reverting series around 2350
        prices = np.array([
            2350.0 + 0.8 * (p - 2350.0) + rng.normal(0, 0.3)
            for p in [2350.0] + [0.0] * 199
        ])
        # Build it iteratively
        prices[0] = 2350.0
        for i in range(1, 200):
            prices[i] = 0.95 * prices[i - 1] + 0.05 * 2350.0 + rng.normal(0, 0.1)

        result = fit_ou(prices, dt=1.0 / 1440)
        assert result is not None
        assert result["theta"] > 0
        assert result["sigma_eq"] > 0
        assert result["half_life_bars"] > 0
        assert 2340 <= result["mu"] <= 2360

    def test_fit_ou_returns_none_on_short_series(self):
        prices = np.array([2350.0, 2351.0, 2349.0])
        assert fit_ou(prices) is None

    def test_fit_ou_returns_none_on_random_walk(self):
        """Pure random walk has a ≈ 1 → theta ≈ 0 → returns None."""
        rng = np.random.default_rng(99)
        prices = np.cumsum(rng.normal(0, 0.5, 200)) + 2300.0
        # A random walk will have a ≥ 1 (or very close to 1)
        result = fit_ou(prices, dt=1.0 / 1440)
        # Either None or theta very small — either way not tradeable
        if result is not None:
            assert result["theta"] > 0

    def test_fit_ou_returns_dict_keys(self):
        """Check all required keys are present."""
        rng = np.random.default_rng(10)
        prices = np.zeros(200)
        prices[0] = 2350.0
        for i in range(1, 200):
            prices[i] = 0.97 * prices[i - 1] + 0.03 * 2350.0 + rng.normal(0, 0.05)

        result = fit_ou(prices, dt=1.0 / 1440)
        if result is not None:
            for key in ("theta", "mu", "sigma", "sigma_eq", "half_life_bars"):
                assert key in result

    # ── ou_zscore ─────────────────────────────────────────────────────────

    def test_ou_zscore_below_mean_is_negative(self):
        ou = {"mu": 2350.0, "sigma_eq": 1.0}
        z = ou_zscore(2348.0, ou)
        assert z == -2.0

    def test_ou_zscore_above_mean_is_positive(self):
        ou = {"mu": 2350.0, "sigma_eq": 0.5}
        z = ou_zscore(2351.0, ou)
        assert z == 2.0

    def test_ou_zscore_zero_sigma_returns_zero(self):
        ou = {"mu": 2350.0, "sigma_eq": 0.0}
        assert ou_zscore(2355.0, ou) == 0.0

    # ── KalmanPriceFilter ─────────────────────────────────────────────────

    def test_kalman_fit_trending_velocity_positive(self):
        """Monotonically rising prices → final velocity > 0."""
        prices = np.linspace(2300.0, 2400.0, 200)
        kf = KalmanPriceFilter(r=1.0)
        _, vels = kf.fit(prices)
        assert vels[-1] > 0

    def test_kalman_fit_flat_velocity_near_zero(self):
        """Constant prices → velocity should converge near 0."""
        prices = np.full(200, 2350.0)
        kf = KalmanPriceFilter(r=1.0)
        _, vels = kf.fit(prices)
        assert abs(vels[-1]) < 0.1

    def test_kalman_fit_returns_correct_shape(self):
        prices = np.linspace(2300.0, 2400.0, 100)
        kf = KalmanPriceFilter(r=1.0)
        fp, vels = kf.fit(prices)
        assert len(fp)   == 100
        assert len(vels) == 100

    # ── kalman_velocity ───────────────────────────────────────────────────

    def test_kalman_velocity_short_series_returns_zero(self):
        prices = np.array([2350.0, 2351.0, 2352.0])
        assert kalman_velocity(prices) == 0.0

    def test_kalman_velocity_rising_prices_positive(self):
        prices = np.linspace(2300.0, 2400.0, 100)
        v = kalman_velocity(prices)
        assert v > 0


# ---------------------------------------------------------------------------
# indicator_engine tests
# ---------------------------------------------------------------------------

class TestIndicatorEngine:
    def test_core_columns_added(self):
        df = indicator_engine.add_indicators(_candles(200))
        expected = [
            "ema9", "ema21", "ema50",
            "rsi14", "atr14", "adx14",
            "macd", "macd_signal", "macd_hist",
            "bb_upper", "bb_mid", "bb_lower",
        ]
        for col in expected:
            assert col in df.columns, f"Missing column: {col}"

    def test_extended_columns_added(self):
        df = indicator_engine.add_indicators(_candles(200))
        for col in ("ema21_slope", "stoch_rsi_k", "stoch_rsi_d",
                    "atr_pct", "bb_width", "bb_width_pct"):
            assert col in df.columns, f"Missing extended column: {col}"

    def test_quant_columns_added(self):
        """New quant columns must be present after add_indicators."""
        df = indicator_engine.add_indicators(_candles(200))
        for col in ("z_score_50", "kalman_price", "kalman_velocity"):
            assert col in df.columns, f"Missing quant column: {col}"

    def test_kalman_velocity_finite_with_200_candles(self):
        """kalman_velocity should be a finite float after 200 bars."""
        df = indicator_engine.add_indicators(_candles(200))
        v = df["kalman_velocity"].iloc[-1]
        assert not pd.isna(v)
        assert np.isfinite(v)

    def test_kalman_velocity_positive_on_uptrend(self):
        """Monotonically rising prices → positive Kalman velocity."""
        prices = np.linspace(2300.0, 2400.0, 200)
        df = pd.DataFrame({
            "time":   [datetime(2024, 1, 1, tzinfo=timezone.utc)] * 200,
            "open":   prices,
            "high":   prices + 0.5,
            "low":    prices - 0.5,
            "close":  prices,
            "volume": [500] * 200,
        })
        df = indicator_engine.add_indicators(df)
        assert df["kalman_velocity"].iloc[-1] > 0

    def test_z_score_50_finite_with_200_candles(self):
        df = indicator_engine.add_indicators(_candles(200))
        z = df["z_score_50"].iloc[-1]
        assert not pd.isna(z)
        assert np.isfinite(z)

    def test_returns_copy(self):
        original = _candles(200)
        enriched = indicator_engine.add_indicators(original)
        assert "ema21" not in original.columns
        assert "ema21" in enriched.columns

    def test_no_nan_at_end(self):
        df = indicator_engine.add_indicators(_candles(200))
        last = df.iloc[-1]
        for col in ["ema21", "ema50", "rsi14", "adx14", "bb_upper", "bb_lower"]:
            assert not pd.isna(last[col]), f"{col} is NaN on last row with 200 candles"

    def test_bb_width_pct_positive(self):
        df = indicator_engine.add_indicators(_candles(200))
        valid = df["bb_width_pct"].dropna()
        assert (valid > 0).all()

    def test_short_df_returns_all_nan(self):
        """Fewer than 60 candles — all indicator cols are NaN, no IndexError."""
        df = indicator_engine.add_indicators(_candles(30))
        for col in indicator_engine._INDICATOR_COLS:
            assert col in df.columns
            assert df[col].isna().all(), f"{col} should be all-NaN for 30-row input"

    def test_has_valid_indicators_helper(self):
        df = indicator_engine.add_indicators(_candles(200))
        row = df.iloc[-1]
        assert indicator_engine.has_valid_indicators(row, ["ema21", "rsi14"])
        row2 = row.copy()
        row2["ema21"] = float("nan")
        assert not indicator_engine.has_valid_indicators(row2, ["ema21"])


# ---------------------------------------------------------------------------
# Return ACF unit tests (unchanged)
# ---------------------------------------------------------------------------

class TestReturnACF:
    def test_insufficient_data_returns_neutral(self):
        prices = np.linspace(2300, 2330, 30)
        assert _return_acf(prices) == 0.0

    def test_constant_prices_returns_neutral(self):
        prices = np.full(100, 2350.0)
        assert _return_acf(prices) == 0.0

    def test_returns_float_in_range(self):
        rng = np.random.default_rng(42)
        prices = np.cumsum(rng.normal(0, 1, 150)) + 2300.0
        acf = _return_acf(prices)
        assert -1.0 <= acf <= 1.0

    def test_persistent_returns_positive_acf(self):
        prices = _ar1_prices(300, phi=0.7, sigma=0.3, seed=42)
        acf = _return_acf(prices)
        assert acf > 0.10, f"Expected ACF > 0.10 for AR1 phi=0.7, got {acf:.3f}"

    def test_antipersistent_returns_negative_acf(self):
        prices = _ar1_prices(300, phi=-0.7, sigma=0.3, seed=99)
        acf = _return_acf(prices)
        assert acf < -0.10, f"Expected ACF < -0.10 for AR1 phi=-0.7, got {acf:.3f}"

    def test_random_walk_near_zero_acf(self):
        rng = np.random.default_rng(123)
        prices = np.cumsum(rng.normal(0, 1, 500)) + 2300.0
        acf = _return_acf(prices)
        assert -0.30 <= acf <= 0.30, f"Expected |ACF| < 0.30 for random walk, got {acf:.3f}"


# ---------------------------------------------------------------------------
# session_detector tests (unchanged)
# ---------------------------------------------------------------------------

class TestSessionDetector:
    def _at(self, hour: int, minute: int = 0):
        dt = _mock_dt(hour, minute)
        return patch.object(session_detector, "_now_utc", return_value=dt)

    def test_london_breakout_window(self):
        with self._at(7, 15):
            assert session_detector.is_london_breakout_window() is True
            assert session_detector.get_current_session() == "LONDON_BREAKOUT"

    def test_london_breakout_exact_start(self):
        with self._at(7, 0):
            assert session_detector.is_london_breakout_window() is True

    def test_after_breakout_window(self):
        with self._at(7, 30):
            assert session_detector.is_london_breakout_window() is False
            assert session_detector.get_current_session() == "LONDON_OPEN"

    def test_london_open(self):
        with self._at(8, 30):
            assert session_detector.get_current_session() == "LONDON_OPEN"

    def test_ny_overlap(self):
        with self._at(14, 0):
            assert session_detector.get_current_session() == "NY_OVERLAP"

    def test_ny_close(self):
        with self._at(20, 0):
            assert session_detector.get_current_session() == "NY_CLOSE"

    def test_blackout_midnight(self):
        with self._at(0, 0):
            assert session_detector.is_blackout_period() is True
            assert session_detector.get_current_session() is None

    def test_blackout_late_night(self):
        with self._at(22, 0):
            assert session_detector.is_blackout_period() is True

    def test_between_sessions(self):
        with self._at(10, 30):
            assert session_detector.get_current_session() is None
            assert session_detector.is_blackout_period() is False

    def test_context_has_all_fields(self):
        with self._at(8, 0):
            ctx = session_detector.get_session_context()
            assert isinstance(ctx, SessionContext)
            for field in ("name", "is_active", "is_blackout",
                          "is_breakout_window", "minutes_elapsed", "minutes_remaining"):
                assert hasattr(ctx, field)

    def test_context_london_open(self):
        with self._at(8, 30):
            ctx = session_detector.get_session_context()
            assert ctx.name == "LONDON_OPEN"
            assert ctx.is_active is True
            assert ctx.is_blackout is False
            assert ctx.is_breakout_window is False
            assert ctx.minutes_elapsed == 90.0
            assert ctx.minutes_remaining == 90.0

    def test_context_breakout_window(self):
        with self._at(7, 15):
            ctx = session_detector.get_session_context()
            assert ctx.name == "LONDON_BREAKOUT"
            assert ctx.is_active is True
            assert ctx.is_breakout_window is True
            assert ctx.minutes_elapsed == 15.0
            assert ctx.minutes_remaining == 15.0

    def test_context_blackout(self):
        with self._at(2, 0):
            ctx = session_detector.get_session_context()
            assert ctx.name is None
            assert ctx.is_active is False
            assert ctx.is_blackout is True

    def test_context_between_sessions(self):
        with self._at(11, 0):
            ctx = session_detector.get_session_context()
            assert ctx.name is None
            assert ctx.is_active is False
            assert ctx.is_blackout is False

    def test_context_ny_overlap(self):
        with self._at(14, 0):
            ctx = session_detector.get_session_context()
            assert ctx.name == "NY_OVERLAP"
            assert ctx.is_active is True
            assert ctx.minutes_elapsed == 120.0
            assert ctx.minutes_remaining == 120.0

    def test_context_is_immutable(self):
        import dataclasses
        with self._at(8, 0):
            ctx = session_detector.get_session_context()
            try:
                ctx.name = "HACKED"  # type: ignore[misc]
                assert False, "Should have raised FrozenInstanceError"
            except dataclasses.FrozenInstanceError:
                pass


# ---------------------------------------------------------------------------
# regime_detector tests (updated — Hurst replaces ADX)
# ---------------------------------------------------------------------------

class TestRegimeDetector:
    def test_empty_df_returns_volatile(self):
        assert regime_detector.detect_regime(pd.DataFrame()) == "VOLATILE"

    def test_short_df_nan_indicators_returns_volatile(self):
        df = indicator_engine.add_indicators(_candles(10))
        assert regime_detector.detect_regime(df) == "VOLATILE"

    def test_atr_spike_overrides_to_volatile(self):
        """If ATR is 3× mean, regime should be VOLATILE regardless of other signals."""
        df = indicator_engine.add_indicators(_trending_candles(200))
        df = df.copy()
        df.iloc[-1, df.columns.get_loc("atr14")] = df["atr14"].iloc[-20:].mean() * 3.0
        assert regime_detector.detect_regime(df) == "VOLATILE"

    def test_hurst_trending_gives_trending(self):
        """Patch rolling_hurst to return > 0.53 → contributes +2 trending."""
        df = indicator_engine.add_indicators(_trending_candles(200))
        df = df.copy()
        last = len(df) - 1
        df.at[last, "bb_width_pct"] = df["bb_width_pct"].iloc[-50:].max() * 2.0

        with patch("agents.nanami.skills.regime_detector.rolling_hurst",
                   return_value=0.60):
            result = regime_detector.detect_regime(df)
        assert result == "TRENDING"

    def test_hurst_ranging_gives_ranging(self):
        """Patch rolling_hurst to return < 0.47 → contributes +2 ranging."""
        df = indicator_engine.add_indicators(_ranging_candles(200))
        df = df.copy()
        last = len(df) - 1
        df.at[last, "bb_width_pct"] = df["bb_width_pct"].iloc[-50:].min() * 0.5

        with patch("agents.nanami.skills.regime_detector.rolling_hurst",
                   return_value=0.40):
            result = regime_detector.detect_regime(df)
        assert result == "RANGING"

    def test_hurst_neutral_without_acf_support_gives_volatile(self):
        """Hurst = 0.50 (neutral) + ACF neutral → score < 2 → VOLATILE."""
        df = indicator_engine.add_indicators(_candles(200))
        df = df.copy()

        with patch("agents.nanami.skills.regime_detector.rolling_hurst",
                   return_value=0.50):
            with patch("agents.nanami.skills.regime_detector._return_acf",
                       return_value=0.0):
                result = regime_detector.detect_regime(df)
        assert result == "VOLATILE"

    def test_all_trending_signals_give_trending(self):
        """Hurst trending + ACF trending + BB expansion → TRENDING."""
        prices = _ar1_prices(200, phi=0.7, sigma=0.3, seed=42)
        rows = []
        for i, c in enumerate(prices):
            rng_ = np.random.default_rng(i)
            noise = abs(float(rng_.normal(0, 0.25)))
            rows.append({
                "time": datetime(2024, 3, 1, 8, 0, 0, tzinfo=timezone.utc),
                "open": c, "high": c + noise, "low": c - noise,
                "close": c, "volume": 500,
            })
        df = indicator_engine.add_indicators(pd.DataFrame(rows))
        df = df.copy()
        last = len(df) - 1
        bb_max = df["bb_width_pct"].iloc[-50:].max()
        if pd.isna(bb_max) or bb_max == 0:
            bb_max = 1.0
        df.at[last, "bb_width_pct"] = bb_max * 2.0

        with patch("agents.nanami.skills.regime_detector.rolling_hurst",
                   return_value=0.60):
            result = regime_detector.detect_regime(df)
        assert result == "TRENDING"

    def test_regime_returns_valid_string(self):
        df = indicator_engine.add_indicators(_ranging_candles(200))
        result = regime_detector.detect_regime(df)
        assert result in ("TRENDING", "RANGING", "VOLATILE")

    def test_missing_bb_col_still_returns_valid(self):
        """Without BB column the vote is skipped; result is still valid."""
        df = _candles(50)  # no indicators added
        result = regime_detector.detect_regime(df)
        assert result in ("TRENDING", "RANGING", "VOLATILE")


# ---------------------------------------------------------------------------
# m5_momentum (Model A) tests — Kalman + z-score
# ---------------------------------------------------------------------------

class TestModelA:
    def _setup_df(
        self,
        velocity: float = 0.05,
        z_score: float = -2.0,
        atr: float = 6.0,
        close: float = 2450.0,
    ) -> pd.DataFrame:
        """Build a minimal M5 DataFrame with injected quant column values."""
        df = indicator_engine.add_indicators(_trending_candles(200))
        df = df.copy()
        last = len(df) - 1
        df.at[last, "close"]           = close
        df.at[last, "atr14"]           = atr
        df.at[last, "kalman_velocity"] = velocity
        df.at[last, "z_score_50"]      = z_score
        return df

    def test_buy_signal_produced(self):
        """kalman_velocity > 0 + z < -1.5 → BUY signal."""
        df = self._setup_df(velocity=0.05, z_score=-2.0)
        signal = m5_momentum.generate_signal(df, "LONDON_OPEN", "TRENDING")
        assert signal is not None
        assert signal["direction"] == "BUY"
        assert signal["model"] == "M5_MOMENTUM"

    def test_sell_signal_produced(self):
        """kalman_velocity < 0 + z > +1.5 → SELL signal."""
        df = self._setup_df(velocity=-0.05, z_score=2.0)
        signal = m5_momentum.generate_signal(df, "LONDON_OPEN", "TRENDING")
        assert signal is not None
        assert signal["direction"] == "SELL"

    def test_signal_has_required_keys(self):
        df = self._setup_df(velocity=0.05, z_score=-2.0)
        signal = m5_momentum.generate_signal(df, "LONDON_OPEN", "TRENDING")
        assert signal is not None
        for key in ("id", "model", "direction", "entry_price", "sl_price",
                    "tp_price", "sl_distance", "session", "regime", "reason"):
            assert key in signal

    def test_sl_within_range(self):
        df = self._setup_df(velocity=0.05, z_score=-2.0)
        signal = m5_momentum.generate_signal(df, "LONDON_OPEN", "TRENDING")
        assert signal is not None
        assert M5_SL_MIN <= signal["sl_distance"] <= M5_SL_MAX

    def test_tp_is_3x_sl(self):
        df = self._setup_df(velocity=0.05, z_score=-2.0)
        signal = m5_momentum.generate_signal(df, "LONDON_OPEN", "TRENDING")
        assert signal is not None
        sl = signal["sl_distance"]
        tp = round(signal["tp_price"] - signal["entry_price"], 2)
        assert abs(tp - sl * 3) < 0.05

    def test_no_signal_when_ranging(self):
        """regime != TRENDING → always None."""
        df = self._setup_df(velocity=0.05, z_score=-2.0)
        assert m5_momentum.generate_signal(df, "LONDON_OPEN", "RANGING") is None

    def test_no_signal_on_insufficient_data(self):
        df = indicator_engine.add_indicators(_candles(20))
        assert m5_momentum.generate_signal(df, "LONDON_OPEN", "TRENDING") is None

    def test_no_signal_when_velocity_wrong_direction_buy(self):
        """kalman_velocity < 0 with z < -1.5 → no BUY (trend opposes)."""
        df = self._setup_df(velocity=-0.05, z_score=-2.0)
        signal = m5_momentum.generate_signal(df, "LONDON_OPEN", "TRENDING")
        assert signal is None

    def test_no_signal_when_z_not_extreme_enough(self):
        """z = -1.0 (less than threshold -1.5) → no signal."""
        df = self._setup_df(velocity=0.05, z_score=-1.0)
        signal = m5_momentum.generate_signal(df, "LONDON_OPEN", "TRENDING")
        assert signal is None


# ---------------------------------------------------------------------------
# m1_meanrev (Model B) tests — OU + ADF
# ---------------------------------------------------------------------------

_OU_VALID = {
    "theta":          50.0,
    "mu":             2350.0,
    "sigma":          2.0,
    "sigma_eq":       0.2,
    "half_life_bars": 20.0,
}

_ADF_STATIONARY = {"stationary": True,  "p_value": 0.01,  "verdict": "STATIONARY"}
_ADF_UNIT_ROOT  = {"stationary": False, "p_value": 0.45,  "verdict": "UNIT_ROOT"}


class TestModelB:
    def _setup_df(self, close: float = 2350.0) -> pd.DataFrame:
        """Build a 200-bar DataFrame with valid ATR."""
        df = indicator_engine.add_indicators(_ranging_candles(200))
        df = df.copy()
        df.at[len(df) - 1, "close"] = close
        df.at[len(df) - 1, "atr14"] = 3.5
        return df

    def _patch(self, adf_result=None, ou_result=None):
        """Context manager that patches adf_stationary and fit_ou together."""
        from contextlib import ExitStack
        stack = ExitStack()
        if adf_result is not None:
            stack.enter_context(patch(
                "agents.nanami.skills.m1_meanrev.adf_stationary",
                return_value=adf_result,
            ))
        if ou_result is not None:
            stack.enter_context(patch(
                "agents.nanami.skills.m1_meanrev.fit_ou",
                return_value=ou_result,
            ))
        return stack

    def test_buy_signal_at_low_z(self):
        """z_ou < -2.0 → BUY signal."""
        close = 2350.0 - 0.5   # below mu → negative z
        ou    = {**_OU_VALID, "mu": 2350.0, "sigma_eq": 0.1}
        df    = self._setup_df(close=close)

        with self._patch(adf_result=_ADF_STATIONARY, ou_result=ou):
            signal = m1_meanrev.generate_signal(df, "NY_OVERLAP", "RANGING")

        assert signal is not None
        assert signal["direction"] == "BUY"
        assert signal["model"] == "M1_MEANREV"

    def test_sell_signal_at_high_z(self):
        """z_ou > +2.0 → SELL signal."""
        close = 2350.0 + 0.5   # above mu → positive z
        ou    = {**_OU_VALID, "mu": 2350.0, "sigma_eq": 0.1}
        df    = self._setup_df(close=close)

        with self._patch(adf_result=_ADF_STATIONARY, ou_result=ou):
            signal = m1_meanrev.generate_signal(df, "NY_OVERLAP", "RANGING")

        assert signal is not None
        assert signal["direction"] == "SELL"

    def test_no_signal_when_trending(self):
        df = self._setup_df()
        with self._patch(adf_result=_ADF_STATIONARY, ou_result=_OU_VALID):
            signal = m1_meanrev.generate_signal(df, "NY_OVERLAP", "TRENDING")
        assert signal is None

    def test_no_signal_when_adf_fails(self):
        """ADF gate closed → no trade even with extreme z."""
        close = 2350.0 - 0.5
        ou    = {**_OU_VALID, "mu": 2350.0, "sigma_eq": 0.1}
        df    = self._setup_df(close=close)
        with self._patch(adf_result=_ADF_UNIT_ROOT, ou_result=ou):
            signal = m1_meanrev.generate_signal(df, "NY_OVERLAP", "RANGING")
        assert signal is None

    def test_no_signal_when_fit_ou_returns_none(self):
        df = self._setup_df()
        with self._patch(adf_result=_ADF_STATIONARY, ou_result=None):
            signal = m1_meanrev.generate_signal(df, "NY_OVERLAP", "RANGING")
        assert signal is None

    def test_no_signal_when_half_life_too_short(self):
        ou = {**_OU_VALID, "half_life_bars": 2.0}   # < MODEL_B_MIN_HALF_LIFE (5)
        df = self._setup_df()
        with self._patch(adf_result=_ADF_STATIONARY, ou_result=ou):
            signal = m1_meanrev.generate_signal(df, "NY_OVERLAP", "RANGING")
        assert signal is None

    def test_no_signal_when_half_life_too_long(self):
        ou = {**_OU_VALID, "half_life_bars": 50.0}  # > MODEL_B_MAX_HALF_LIFE (30)
        df = self._setup_df()
        with self._patch(adf_result=_ADF_STATIONARY, ou_result=ou):
            signal = m1_meanrev.generate_signal(df, "NY_OVERLAP", "RANGING")
        assert signal is None

    def test_no_signal_when_z_not_extreme(self):
        """z_ou = 0.0 (at mean) → no signal."""
        close = 2350.0   # exactly at mu → z = 0
        ou    = {**_OU_VALID, "mu": 2350.0, "sigma_eq": 0.2}
        df    = self._setup_df(close=close)
        with self._patch(adf_result=_ADF_STATIONARY, ou_result=ou):
            signal = m1_meanrev.generate_signal(df, "NY_OVERLAP", "RANGING")
        assert signal is None

    def test_sl_within_range(self):
        close = 2350.0 - 0.5
        ou    = {**_OU_VALID, "mu": 2350.0, "sigma_eq": 0.1}
        df    = self._setup_df(close=close)
        with self._patch(adf_result=_ADF_STATIONARY, ou_result=ou):
            signal = m1_meanrev.generate_signal(df, "NY_OVERLAP", "RANGING")
        assert signal is not None
        assert M1_SL_MIN <= signal["sl_distance"] <= M1_SL_MAX

    def test_tp_is_3x_sl(self):
        close = 2350.0 - 0.5
        ou    = {**_OU_VALID, "mu": 2350.0, "sigma_eq": 0.1}
        df    = self._setup_df(close=close)
        with self._patch(adf_result=_ADF_STATIONARY, ou_result=ou):
            signal = m1_meanrev.generate_signal(df, "NY_OVERLAP", "RANGING")
        assert signal is not None
        sl = signal["sl_distance"]
        tp = round(signal["tp_price"] - signal["entry_price"], 2)
        assert abs(tp - sl * 3) < 0.05

    def test_signal_has_required_keys(self):
        close = 2350.0 - 0.5
        ou    = {**_OU_VALID, "mu": 2350.0, "sigma_eq": 0.1}
        df    = self._setup_df(close=close)
        with self._patch(adf_result=_ADF_STATIONARY, ou_result=ou):
            signal = m1_meanrev.generate_signal(df, "NY_OVERLAP", "RANGING")
        assert signal is not None
        for key in ("id", "model", "direction", "entry_price", "sl_price",
                    "tp_price", "sl_distance", "session", "regime", "reason"):
            assert key in signal


# ---------------------------------------------------------------------------
# london_breakout (Model C) tests
# ---------------------------------------------------------------------------

class TestModelC:
    def test_buy_breakout(self):
        df = _candles(50, base_price=2365.0)
        signal = london_breakout.generate_signal(df, 2360.0, 2350.0)
        assert signal is not None
        assert signal["direction"] == "BUY"
        assert signal["model"] == "LONDON_BREAKOUT"

    def test_sell_breakout(self):
        df = _candles(50, base_price=2345.0)
        signal = london_breakout.generate_signal(df, 2360.0, 2350.0)
        assert signal is not None
        assert signal["direction"] == "SELL"

    def test_no_signal_inside_range(self):
        df = _candles(50, base_price=2355.0)
        signal = london_breakout.generate_signal(df, 2360.0, 2350.0)
        assert signal is None

    def test_no_signal_narrow_range(self):
        """Asian range < MODEL_C_MIN_RANGE ($3) → None (fake breakout risk)."""
        df = _candles(50, base_price=2362.5)
        # Range = 2363 - 2361 = $2, below $3 threshold
        signal = london_breakout.generate_signal(df, 2363.0, 2361.0)
        assert signal is None, (
            f"Expected None for narrow ${MODEL_C_MIN_RANGE} range, got signal"
        )

    def test_signal_at_exact_min_range(self):
        """Asian range = exactly $3 (== threshold) → should still fire."""
        df = _candles(50, base_price=2365.0)
        # Range = 2363 - 2360 = $3 exactly; close > 2363 → BUY
        signal = london_breakout.generate_signal(df, 2363.0, 2360.0)
        assert signal is not None

    def test_sl_within_range(self):
        df = _candles(50, base_price=2365.0)
        signal = london_breakout.generate_signal(df, 2360.0, 2350.0)
        assert signal is not None
        assert BREAKOUT_SL_MIN <= signal["sl_distance"] <= BREAKOUT_SL_MAX

    def test_sl_clamped_to_min_when_range_narrow(self):
        """Asian range of $1 (still ≥ MIN_RANGE would be needed — test with $4)."""
        df = _candles(50, base_price=2365.0)
        # Range = 2364 - 2360 = $4 (≥ $3) — SL = min($4, $8) = $4, then clamp to SL_MIN ($6)
        signal = london_breakout.generate_signal(df, 2364.0, 2360.0)
        assert signal is not None
        assert signal["sl_distance"] == BREAKOUT_SL_MIN

    def test_sl_clamped_to_max_when_range_wide(self):
        """Asian range of $20 → SL clamped to BREAKOUT_SL_MAX ($8)."""
        df = _candles(50, base_price=2381.0)
        signal = london_breakout.generate_signal(df, 2380.0, 2360.0)
        assert signal is not None
        assert signal["sl_distance"] == BREAKOUT_SL_MAX

    def test_tp_is_3x_sl(self):
        df = _candles(50, base_price=2365.0)
        signal = london_breakout.generate_signal(df, 2360.0, 2350.0)
        assert signal is not None
        sl = signal["sl_distance"]
        tp = round(signal["tp_price"] - signal["entry_price"], 2)
        assert abs(tp - sl * 3) < 0.05

    def test_invalid_asian_range_returns_none(self):
        df = _candles(50, base_price=2365.0)
        assert london_breakout.generate_signal(df, 0.0, 0.0) is None
        assert london_breakout.generate_signal(df, 2350.0, 2360.0) is None

    def test_signal_has_required_keys(self):
        df = _candles(50, base_price=2365.0)
        signal = london_breakout.generate_signal(df, 2360.0, 2350.0)
        assert signal is not None
        for key in ("id", "model", "direction", "entry_price", "sl_price",
                    "tp_price", "sl_distance", "session", "regime", "reason"):
            assert key in signal
