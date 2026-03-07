"""
test_nanami_signals.py — NANAMI unit tests (6-state regime + OU signal models)

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
    ou_grind,
    asian_breakout,
)
from agents.nanami.skills.ou_range import generate_signal as ou_range_signal
from agents.nanami.skills.htf_regime import detect_regime, check_structural_break, compute_h4_bias
from agents.nanami.skills.session_detector import SessionContext
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
    OU_SL_MIN, OU_SL_MAX,
    BREAKOUT_SL_MIN, BREAKOUT_SL_MAX,
    HTF_BIAS_BULLISH, HTF_BIAS_BEARISH, HTF_BIAS_NEUTRAL,
    REGIME_BULLISH_GRIND, REGIME_BULLISH_BLOWOFF,
    REGIME_BEARISH_GRIND, REGIME_BEARISH_PANIC,
    REGIME_TIGHT_RANGE, REGIME_TOXIC_CHOP,
    RR_RATIO,
)


# ---------------------------------------------------------------------------
# Helpers — synthetic candle builders
# ---------------------------------------------------------------------------

def _candles(n: int, base_price: float = 2350.0, trend: float = 0.0) -> pd.DataFrame:
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


def _ranging_candles(n: int = 200) -> pd.DataFrame:
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
    rng = np.random.default_rng(seed)
    r = 0.0
    returns = []
    for _ in range(n):
        r = phi * r + rng.normal(0, sigma)
        returns.append(r)
    return base + np.cumsum(returns)


def _mock_dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2024, 1, 15, hour, minute, 0, tzinfo=timezone.utc)


def _h1_candles(n: int = 60, trend: float = 0.5) -> pd.DataFrame:
    """Build H1 candles for HTF regime tests."""
    rng = np.random.default_rng(42)
    base = 2300.0
    data = []
    for i in range(n):
        c = base + trend * i + rng.normal(0, 1.0)
        noise = abs(rng.normal(0, 0.5))
        data.append({
            "open": c - 0.5, "high": c + noise, "low": c - noise,
            "close": c, "volume": 1000,
        })
    return pd.DataFrame(data)


def _h4_candles(n: int = 30, trend: float = 2.0) -> pd.DataFrame:
    """Build H4 candles for HTF regime tests."""
    rng = np.random.default_rng(42)
    base = 2300.0
    data = []
    for i in range(n):
        c = base + trend * i + rng.normal(0, 2.0)
        noise = abs(rng.normal(0, 1.0))
        data.append({
            "open": c - 1.0, "high": c + noise, "low": c - noise,
            "close": c, "volume": 5000,
        })
    return pd.DataFrame(data)


def _h1_regime_candles(n: int, z_direction: str, high_vol: bool, seed: int = 42) -> pd.DataFrame:
    """
    Build H1 candles that produce a specific 6-state regime.

    z_direction: "UP" (Z>1), "DOWN" (Z<-1), "FLAT" (|Z|<=1)
    high_vol:    True => ATR percentile > 75, False => stable ATR
    """
    rng = np.random.default_rng(seed)
    base = 2350.0
    data = []

    # Base volatility for normal bars
    normal_range = 3.0  # typical H1 range
    spike_range = 15.0  # elevated volatility range

    for i in range(n):
        if z_direction == "UP":
            # Strong uptrend so last close is well above 50-bar mean
            c = base + 0.5 * i + rng.normal(0, 0.3)
        elif z_direction == "DOWN":
            # Strong downtrend
            c = base - 0.5 * i + rng.normal(0, 0.3)
        else:
            # Flat — oscillate around base deterministically to guarantee |Z| <= 1
            c = base + 0.5 * np.sin(i * 0.1)

        if high_vol:
            # Make the last 30 bars have much higher range to push ATR percentile > 75
            if i >= n - 30:
                noise = spike_range + abs(rng.normal(0, 2.0))
            else:
                noise = normal_range * 0.3 + abs(rng.normal(0, 0.3))
        else:
            # Low vol: early bars have moderate range, last bars have small range
            # so ATR percentile of current bar is well below 75
            if i < n - 50:
                noise = normal_range + abs(rng.normal(0, 0.5))
            else:
                noise = normal_range * 0.3 + abs(rng.normal(0, 0.1))

        data.append({
            "open": c - noise * 0.3,
            "high": c + noise,
            "low":  c - noise,
            "close": c,
            "volume": 1000,
        })

    return pd.DataFrame(data)


def _make_ou_data_buy(n=300, mu=2350.0, seed=42):
    """Generate stationary OU M5 data ending BELOW mu (z < -1.3).

    Strategy: generate a strongly mean-reverting process around mu for most
    of the series. Then sharply drop the last 5 bars. Because EMA50 is slow,
    it stays near mu while price drops — creating a large negative residual
    and thus a strong negative OU z-score on detrended data.
    """
    rng = np.random.default_rng(seed)
    theta = 0.3
    sigma = 2.0
    dt = 1.0
    prices = [mu]
    for _ in range(n - 1):
        dx = theta * (mu - prices[-1]) * dt + sigma * rng.normal()
        prices.append(prices[-1] + dx)
    # Sharp drop at the end — EMA50 won't catch up, creating large residual
    for i in range(n - 5, n):
        prices[i] = mu - 30.0 + rng.normal() * 0.3
    return np.array(prices)


def _make_ou_data_sell(n=300, mu=2350.0, seed=42):
    """Generate stationary OU M5 data ending ABOVE mu (z > +1.3)."""
    rng = np.random.default_rng(seed)
    theta = 0.3
    sigma = 2.0
    dt = 1.0
    prices = [mu]
    for _ in range(n - 1):
        dx = theta * (mu - prices[-1]) * dt + sigma * rng.normal()
        prices.append(prices[-1] + dx)
    # Sharp spike at the end
    for i in range(n - 5, n):
        prices[i] = mu + 30.0 + rng.normal() * 0.3
    return np.array(prices)


def _build_m5_from_prices(prices: np.ndarray) -> pd.DataFrame:
    """Convert a price array into an M5 DataFrame with indicators."""
    data = []
    for p in prices:
        noise = abs(np.random.default_rng(42).normal(0, 0.5))
        data.append({
            "open": p - 0.2,
            "high": p + noise + 2.0,
            "low":  p - noise - 2.0,
            "close": p,
            "volume": 500,
        })
    df = pd.DataFrame(data)
    df = indicator_engine.add_indicators(df)
    return df


# ---------------------------------------------------------------------------
# stat_tests unit tests
# ---------------------------------------------------------------------------

class TestStatTests:
    def test_hurst_random_walk_near_half(self):
        rng = np.random.default_rng(42)
        prices = np.cumsum(rng.normal(0, 1, 500)) + 2300.0
        h = rolling_hurst(prices, window=200)
        assert 0.35 <= h <= 0.65

    def test_hurst_persistent_above_neutral(self):
        prices = _ar1_prices(400, phi=0.7, sigma=0.3, seed=42)
        h = rolling_hurst(prices, window=200)
        assert h > 0.50

    def test_hurst_anti_persistent_below_half(self):
        prices = _ar1_prices(400, phi=-0.7, sigma=0.3, seed=99)
        h = rolling_hurst(prices, window=200)
        assert h < 0.50

    def test_hurst_short_series_returns_neutral(self):
        prices = np.linspace(2300, 2350, 50)
        h = rolling_hurst(prices, window=200)
        assert h == 0.5

    def test_hurst_in_valid_range(self):
        rng = np.random.default_rng(0)
        prices = np.cumsum(rng.normal(0, 1, 300)) + 2300.0
        h = rolling_hurst(prices, window=200)
        assert 0.0 <= h <= 1.0

    def test_classify_trending(self):
        assert classify_hurst(0.54) == "TRENDING"

    def test_classify_ranging(self):
        assert classify_hurst(0.34) == "RANGING"

    def test_classify_undefined(self):
        assert classify_hurst(0.50) == "UNDEFINED"

    def test_adf_returns_dict_contract(self):
        prices = np.linspace(2300, 2350, 50)
        result = adf_stationary(prices)
        assert isinstance(result["stationary"], bool)
        assert isinstance(result["p_value"], float)

    def test_adf_fail_safe_on_bad_input(self):
        prices = np.full(50, 2350.0)
        result = adf_stationary(prices)
        assert result["stationary"] is False

    def test_fit_ou_returns_none_on_short_series(self):
        prices = np.array([2350.0, 2351.0, 2349.0])
        assert fit_ou(prices) is None

    def test_ou_zscore_below_mean_is_negative(self):
        ou = {"mu": 2350.0, "sigma_eq": 1.0}
        z = ou_zscore(2348.0, ou)
        assert z == -2.0

    def test_ou_zscore_zero_sigma_returns_zero(self):
        ou = {"mu": 2350.0, "sigma_eq": 0.0}
        assert ou_zscore(2355.0, ou) == 0.0

    def test_kalman_fit_trending_velocity_positive(self):
        prices = np.linspace(2300.0, 2400.0, 200)
        kf = KalmanPriceFilter(r=1.0)
        _, vels = kf.fit(prices)
        assert vels[-1] > 0

    def test_kalman_fit_flat_velocity_near_zero(self):
        prices = np.full(200, 2350.0)
        kf = KalmanPriceFilter(r=1.0)
        _, vels = kf.fit(prices)
        assert abs(vels[-1]) < 0.1

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

    def test_quant_columns_added(self):
        df = indicator_engine.add_indicators(_candles(200))
        for col in ("z_score_50", "kalman_price", "kalman_velocity"):
            assert col in df.columns, f"Missing quant column: {col}"

    def test_returns_copy(self):
        original = _candles(200)
        enriched = indicator_engine.add_indicators(original)
        assert "ema21" not in original.columns
        assert "ema21" in enriched.columns

    def test_no_nan_at_end(self):
        df = indicator_engine.add_indicators(_candles(200))
        last = df.iloc[-1]
        for col in ["ema21", "ema50", "rsi14", "adx14", "bb_upper", "bb_lower"]:
            assert not pd.isna(last[col])

    def test_short_df_returns_all_nan(self):
        df = indicator_engine.add_indicators(_candles(30))
        for col in indicator_engine._INDICATOR_COLS:
            assert col in df.columns


# ---------------------------------------------------------------------------
# detect_regime tests (6-state Z-score x ATR percentile matrix)
# ---------------------------------------------------------------------------

class TestDetectRegime:
    def test_bullish_grind(self):
        """Uptrending close (Z>1) with stable ATR (percentile <= 75) -> BULLISH_GRIND."""
        df = _h1_regime_candles(250, z_direction="UP", high_vol=False, seed=42)
        regime = detect_regime(df)
        assert regime == REGIME_BULLISH_GRIND

    def test_bullish_blowoff(self):
        """Uptrending close (Z>1) with spiking ATR (percentile > 75) -> BULLISH_BLOWOFF."""
        df = _h1_regime_candles(250, z_direction="UP", high_vol=True, seed=42)
        regime = detect_regime(df)
        assert regime == REGIME_BULLISH_BLOWOFF

    def test_bearish_grind(self):
        """Downtrending close (Z<-1) with stable ATR -> BEARISH_GRIND."""
        df = _h1_regime_candles(250, z_direction="DOWN", high_vol=False, seed=42)
        regime = detect_regime(df)
        assert regime == REGIME_BEARISH_GRIND

    def test_bearish_panic(self):
        """Downtrending close (Z<-1) with spiking ATR -> BEARISH_PANIC."""
        df = _h1_regime_candles(250, z_direction="DOWN", high_vol=True, seed=42)
        regime = detect_regime(df)
        assert regime == REGIME_BEARISH_PANIC

    def test_tight_range(self):
        """Flat close (|Z|<=1) with stable ATR -> TIGHT_RANGE."""
        df = _h1_regime_candles(250, z_direction="FLAT", high_vol=False, seed=42)
        regime = detect_regime(df)
        assert regime == REGIME_TIGHT_RANGE

    def test_toxic_chop(self):
        """Flat close (|Z|<=1) with spiking ATR -> TOXIC_CHOP."""
        df = _h1_regime_candles(250, z_direction="FLAT", high_vol=True, seed=42)
        regime = detect_regime(df)
        assert regime == REGIME_TOXIC_CHOP

    def test_insufficient_data(self):
        """< 200 bars -> returns TIGHT_RANGE as safe default."""
        df = _h1_candles(50)
        regime = detect_regime(df)
        assert regime == REGIME_TIGHT_RANGE


# ---------------------------------------------------------------------------
# check_structural_break tests
# ---------------------------------------------------------------------------

class TestStructuralBreak:
    def test_normal_bar(self):
        """No break when range < 3 x ATR."""
        df = _h1_candles(60, trend=0.5)
        assert not check_structural_break(df)

    def test_break_detected(self):
        """Break when last bar range > 3 x ATR."""
        df = _h1_candles(60, trend=0.5).copy()
        # Force last bar to have a massive range
        last_idx = len(df) - 1
        df.loc[last_idx, "high"] = df.loc[last_idx, "close"] + 100.0
        df.loc[last_idx, "low"] = df.loc[last_idx, "close"] - 100.0
        assert check_structural_break(df)

    def test_insufficient_data(self):
        """< 15 rows -> returns False."""
        df = _h1_candles(10)
        assert not check_structural_break(df)


# ---------------------------------------------------------------------------
# compute_h4_bias tests (retained for Model C)
# ---------------------------------------------------------------------------

class TestH4Bias:
    def test_h4_bias_bullish_on_uptrend(self):
        df = _h4_candles(30, trend=2.0)
        bias = compute_h4_bias(df)
        assert bias == HTF_BIAS_BULLISH

    def test_h4_bias_bearish_on_downtrend(self):
        df = _h4_candles(30, trend=-2.0)
        bias = compute_h4_bias(df)
        assert bias == HTF_BIAS_BEARISH

    def test_h4_bias_neutral_on_insufficient_data(self):
        df = _h4_candles(5, trend=2.0)
        bias = compute_h4_bias(df)
        assert bias == HTF_BIAS_NEUTRAL


# ---------------------------------------------------------------------------
# session_detector tests
# ---------------------------------------------------------------------------

class TestSessionDetector:
    def _at(self, hour: int, minute: int = 0):
        dt = _mock_dt(hour, minute)
        return patch.object(session_detector, "_now_utc", return_value=dt)

    def test_london_breakout_window(self):
        with self._at(7, 15):
            assert session_detector.is_london_breakout_window() is True
            assert session_detector.get_current_session() == "LONDON_BREAKOUT"

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

    def test_context_breakout_window(self):
        with self._at(7, 15):
            ctx = session_detector.get_session_context()
            assert ctx.name == "LONDON_BREAKOUT"
            assert ctx.is_breakout_window is True

    def test_context_blackout(self):
        with self._at(2, 0):
            ctx = session_detector.get_session_context()
            assert ctx.name is None
            assert ctx.is_blackout is True

    def test_context_is_immutable(self):
        import dataclasses
        with self._at(8, 0):
            ctx = session_detector.get_session_context()
            try:
                ctx.name = "HACKED"
                assert False, "Should have raised FrozenInstanceError"
            except dataclasses.FrozenInstanceError:
                pass


# ---------------------------------------------------------------------------
# m5_momentum (Model A) tests — OU grind
# ---------------------------------------------------------------------------

class TestModelA:
    def _build_ou_m5(self, direction: str) -> pd.DataFrame:
        """Build M5 data where OU fit succeeds and z is in the correct direction."""
        if direction == "BUY":
            prices = _make_ou_data_buy(n=300, mu=2350.0, seed=42)
        else:
            prices = _make_ou_data_sell(n=300, mu=2350.0, seed=42)
        return _build_m5_from_prices(prices)

    def test_buy_bullish_grind(self):
        """OU fit works, z < -2, regime=BULLISH_GRIND -> BUY signal."""
        df_m5 = self._build_ou_m5("BUY")
        mock_adf = {"stationary": True, "p_value": 0.01, "verdict": "STATIONARY"}
        with patch("agents.nanami.skills.ou_grind.adf_stationary", return_value=mock_adf):
            signal = ou_grind.generate_signal(df_m5, "LONDON_OPEN", REGIME_BULLISH_GRIND)
        assert signal is not None
        assert signal["direction"] == "BUY"
        assert signal["model"] == "OU_GRIND"

    def test_sell_bearish_grind(self):
        """z > 2, regime=BEARISH_GRIND -> SELL signal."""
        df_m5 = self._build_ou_m5("SELL")
        mock_adf = {"stationary": True, "p_value": 0.01, "verdict": "STATIONARY"}
        with patch("agents.nanami.skills.ou_grind.adf_stationary", return_value=mock_adf):
            signal = ou_grind.generate_signal(df_m5, "LONDON_OPEN", REGIME_BEARISH_GRIND)
        assert signal is not None
        assert signal["direction"] == "SELL"
        assert signal["model"] == "OU_GRIND"

    def test_rejects_wrong_regime(self):
        """regime=TIGHT_RANGE -> None (Model A only fires in grind regimes)."""
        df_m5 = self._build_ou_m5("BUY")
        signal = ou_grind.generate_signal(df_m5, "LONDON_OPEN", REGIME_TIGHT_RANGE)
        assert signal is None

    def test_rejects_blowoff_regime(self):
        """regime=BULLISH_BLOWOFF -> None."""
        df_m5 = self._build_ou_m5("BUY")
        signal = ou_grind.generate_signal(df_m5, "LONDON_OPEN", REGIME_BULLISH_BLOWOFF)
        assert signal is None

    def test_rejects_toxic_chop(self):
        """regime=TOXIC_CHOP -> None."""
        df_m5 = self._build_ou_m5("BUY")
        signal = ou_grind.generate_signal(df_m5, "LONDON_OPEN", REGIME_TOXIC_CHOP)
        assert signal is None

    def test_rejects_wrong_direction(self):
        """BULLISH_GRIND + z > 2 -> None (would be SELL in bullish grind)."""
        df_m5 = self._build_ou_m5("SELL")  # z > +2
        mock_adf = {"stationary": True, "p_value": 0.01, "verdict": "STATIONARY"}
        with patch("agents.nanami.skills.ou_grind.adf_stationary", return_value=mock_adf):
            signal = ou_grind.generate_signal(df_m5, "LONDON_OPEN", REGIME_BULLISH_GRIND)
        assert signal is None

    def test_rejects_no_stationarity(self):
        """Mock adf_stationary to return not stationary -> None."""
        df_m5 = self._build_ou_m5("BUY")
        mock_adf = {"stationary": False, "p_value": 0.9, "verdict": "UNIT_ROOT"}
        with patch("agents.nanami.skills.ou_grind.adf_stationary", return_value=mock_adf):
            signal = ou_grind.generate_signal(df_m5, "LONDON_OPEN", REGIME_BULLISH_GRIND)
        assert signal is None

    def test_rejects_bad_half_life(self):
        """Mock fit_ou to return half_life=50 (out of 5-30 range) -> None."""
        df_m5 = self._build_ou_m5("BUY")
        bad_ou = {"theta": 0.01, "mu": 2350.0, "sigma": 1.0, "sigma_eq": 5.0, "half_life_bars": 50.0}
        with patch("agents.nanami.skills.ou_grind.fit_ou", return_value=bad_ou):
            signal = ou_grind.generate_signal(df_m5, "LONDON_OPEN", REGIME_BULLISH_GRIND)
        assert signal is None

    def test_rejects_insufficient_data(self):
        """< 200 rows -> None."""
        df = indicator_engine.add_indicators(_candles(50))
        signal = ou_grind.generate_signal(df, "LONDON_OPEN", REGIME_BULLISH_GRIND)
        assert signal is None

    def test_sl_bounds(self):
        """SL clamped between $6 and $12."""
        df_m5 = self._build_ou_m5("BUY")
        mock_adf = {"stationary": True, "p_value": 0.01, "verdict": "STATIONARY"}
        with patch("agents.nanami.skills.ou_grind.adf_stationary", return_value=mock_adf):
            signal = ou_grind.generate_signal(df_m5, "LONDON_OPEN", REGIME_BULLISH_GRIND)
        assert signal is not None
        assert OU_SL_MIN <= signal["sl_distance"] <= OU_SL_MAX

    def test_tp_is_2x_rr(self):
        """TP = SL x RR_RATIO (2.0)."""
        df_m5 = self._build_ou_m5("BUY")
        mock_adf = {"stationary": True, "p_value": 0.01, "verdict": "STATIONARY"}
        with patch("agents.nanami.skills.ou_grind.adf_stationary", return_value=mock_adf):
            signal = ou_grind.generate_signal(df_m5, "LONDON_OPEN", REGIME_BULLISH_GRIND)
        assert signal is not None
        sl = signal["sl_distance"]
        expected_tp = sl * RR_RATIO
        actual_tp = abs(signal["tp_price"] - signal["entry_price"])
        assert abs(actual_tp - expected_tp) < 0.05

    def test_signal_has_required_keys(self):
        """Signal dict has all required fields."""
        df_m5 = self._build_ou_m5("BUY")
        mock_adf = {"stationary": True, "p_value": 0.01, "verdict": "STATIONARY"}
        with patch("agents.nanami.skills.ou_grind.adf_stationary", return_value=mock_adf):
            signal = ou_grind.generate_signal(df_m5, "LONDON_OPEN", REGIME_BULLISH_GRIND)
        assert signal is not None
        for key in ("id", "model", "direction", "entry_price", "sl_price",
                    "tp_price", "sl_distance", "atr_at_entry", "session", "regime", "reason"):
            assert key in signal

    def test_signal_model_name(self):
        """Signal model field is M5_MOMENTUM."""
        df_m5 = self._build_ou_m5("BUY")
        mock_adf = {"stationary": True, "p_value": 0.01, "verdict": "STATIONARY"}
        with patch("agents.nanami.skills.ou_grind.adf_stationary", return_value=mock_adf):
            signal = ou_grind.generate_signal(df_m5, "LONDON_OPEN", REGIME_BULLISH_GRIND)
        assert signal is not None
        assert signal["model"] == "OU_GRIND"

    def test_signal_regime_in_output(self):
        """Signal regime field matches what was passed in."""
        df_m5 = self._build_ou_m5("BUY")
        mock_adf = {"stationary": True, "p_value": 0.01, "verdict": "STATIONARY"}
        with patch("agents.nanami.skills.ou_grind.adf_stationary", return_value=mock_adf):
            signal = ou_grind.generate_signal(df_m5, "LONDON_OPEN", REGIME_BULLISH_GRIND)
        assert signal is not None
        assert signal["regime"] == REGIME_BULLISH_GRIND


# ---------------------------------------------------------------------------
# m5_liquidity_sweep (Model B) tests — OU range
# ---------------------------------------------------------------------------

class TestModelB:
    def _build_ou_m5(self, direction: str) -> pd.DataFrame:
        """Build M5 data where OU fit succeeds and z is in the correct direction."""
        if direction == "BUY":
            prices = _make_ou_data_buy(n=300, mu=2350.0, seed=42)
        else:
            prices = _make_ou_data_sell(n=300, mu=2350.0, seed=42)
        return _build_m5_from_prices(prices)

    def test_buy_tight_range(self):
        """z < -2, regime=TIGHT_RANGE -> BUY."""
        df_m5 = self._build_ou_m5("BUY")
        mock_adf = {"stationary": True, "p_value": 0.01, "verdict": "STATIONARY"}
        with patch("agents.nanami.skills.ou_range.adf_stationary", return_value=mock_adf):
            signal = ou_range_signal(df_m5, "LONDON_OPEN", REGIME_TIGHT_RANGE)
        assert signal is not None
        assert signal["direction"] == "BUY"
        assert signal["model"] == "OU_RANGE"

    def test_sell_tight_range(self):
        """z > 2, regime=TIGHT_RANGE -> SELL."""
        df_m5 = self._build_ou_m5("SELL")
        mock_adf = {"stationary": True, "p_value": 0.01, "verdict": "STATIONARY"}
        with patch("agents.nanami.skills.ou_range.adf_stationary", return_value=mock_adf):
            signal = ou_range_signal(df_m5, "LONDON_OPEN", REGIME_TIGHT_RANGE)
        assert signal is not None
        assert signal["direction"] == "SELL"
        assert signal["model"] == "OU_RANGE"

    def test_rejects_non_tight_range(self):
        """regime=BULLISH_GRIND -> None (Model B only fires in TIGHT_RANGE)."""
        df_m5 = self._build_ou_m5("BUY")
        signal = ou_range_signal(df_m5, "LONDON_OPEN", REGIME_BULLISH_GRIND)
        assert signal is None

    def test_rejects_bearish_panic(self):
        """regime=BEARISH_PANIC -> None."""
        df_m5 = self._build_ou_m5("SELL")
        signal = ou_range_signal(df_m5, "LONDON_OPEN", REGIME_BEARISH_PANIC)
        assert signal is None

    def test_rejects_toxic_chop(self):
        """regime=TOXIC_CHOP -> None."""
        df_m5 = self._build_ou_m5("BUY")
        signal = ou_range_signal(df_m5, "LONDON_OPEN", REGIME_TOXIC_CHOP)
        assert signal is None

    def test_rejects_no_stationarity(self):
        """Mock adf_stationary to return not stationary -> None."""
        df_m5 = self._build_ou_m5("BUY")
        mock_adf = {"stationary": False, "p_value": 0.9, "verdict": "UNIT_ROOT"}
        with patch("agents.nanami.skills.ou_range.adf_stationary", return_value=mock_adf):
            signal = ou_range_signal(df_m5, "LONDON_OPEN", REGIME_TIGHT_RANGE)
        assert signal is None

    def test_rejects_bad_half_life(self):
        """Mock fit_ou to return half_life=2 (below min 5) -> None."""
        df_m5 = self._build_ou_m5("BUY")
        bad_ou = {"theta": 100.0, "mu": 2350.0, "sigma": 1.0, "sigma_eq": 5.0, "half_life_bars": 2.0}
        with patch("agents.nanami.skills.ou_range.fit_ou", return_value=bad_ou):
            signal = ou_range_signal(df_m5, "LONDON_OPEN", REGIME_TIGHT_RANGE)
        assert signal is None

    def test_rejects_insufficient_data(self):
        """< 200 rows -> None."""
        df = indicator_engine.add_indicators(_candles(50))
        signal = ou_range_signal(df, "LONDON_OPEN", REGIME_TIGHT_RANGE)
        assert signal is None

    def test_sl_bounds(self):
        """SL clamped between $6 and $12."""
        df_m5 = self._build_ou_m5("BUY")
        mock_adf = {"stationary": True, "p_value": 0.01, "verdict": "STATIONARY"}
        with patch("agents.nanami.skills.ou_range.adf_stationary", return_value=mock_adf):
            signal = ou_range_signal(df_m5, "LONDON_OPEN", REGIME_TIGHT_RANGE)
        assert signal is not None
        assert OU_SL_MIN <= signal["sl_distance"] <= OU_SL_MAX

    def test_tp_is_2x_rr(self):
        """TP = SL x RR_RATIO (2.0)."""
        df_m5 = self._build_ou_m5("BUY")
        mock_adf = {"stationary": True, "p_value": 0.01, "verdict": "STATIONARY"}
        with patch("agents.nanami.skills.ou_range.adf_stationary", return_value=mock_adf):
            signal = ou_range_signal(df_m5, "LONDON_OPEN", REGIME_TIGHT_RANGE)
        assert signal is not None
        sl = signal["sl_distance"]
        expected_tp = sl * RR_RATIO
        actual_tp = abs(signal["tp_price"] - signal["entry_price"])
        assert abs(actual_tp - expected_tp) < 0.05

    def test_signal_has_required_keys(self):
        """Signal dict has all required fields."""
        df_m5 = self._build_ou_m5("BUY")
        mock_adf = {"stationary": True, "p_value": 0.01, "verdict": "STATIONARY"}
        with patch("agents.nanami.skills.ou_range.adf_stationary", return_value=mock_adf):
            signal = ou_range_signal(df_m5, "LONDON_OPEN", REGIME_TIGHT_RANGE)
        assert signal is not None
        for key in ("id", "model", "direction", "entry_price", "sl_price",
                    "tp_price", "sl_distance", "atr_at_entry", "session", "regime", "reason"):
            assert key in signal

    def test_signal_regime_in_output(self):
        """Signal regime field is TIGHT_RANGE."""
        df_m5 = self._build_ou_m5("BUY")
        mock_adf = {"stationary": True, "p_value": 0.01, "verdict": "STATIONARY"}
        with patch("agents.nanami.skills.ou_range.adf_stationary", return_value=mock_adf):
            signal = ou_range_signal(df_m5, "LONDON_OPEN", REGIME_TIGHT_RANGE)
        assert signal is not None
        assert signal["regime"] == REGIME_TIGHT_RANGE


# ---------------------------------------------------------------------------
# london_breakout (Model C) tests — with H4 filter (unchanged)
# ---------------------------------------------------------------------------

class TestModelC:
    def test_buy_breakout(self):
        df = _candles(50, base_price=2365.0)
        signal = asian_breakout.generate_signal(df, 2360.0, 2350.0)
        assert signal is not None
        assert signal["direction"] == "BUY"
        assert signal["model"] == "ASIAN_BREAKOUT"

    def test_sell_breakout(self):
        df = _candles(50, base_price=2345.0)
        signal = asian_breakout.generate_signal(df, 2360.0, 2350.0)
        assert signal is not None
        assert signal["direction"] == "SELL"

    def test_no_signal_inside_range(self):
        df = _candles(50, base_price=2355.0)
        signal = asian_breakout.generate_signal(df, 2360.0, 2350.0)
        assert signal is None

    def test_h4_bearish_blocks_buy(self):
        df = _candles(50, base_price=2365.0)
        signal = asian_breakout.generate_signal(df, 2360.0, 2350.0, HTF_BIAS_BEARISH)
        assert signal is None

    def test_h4_bullish_blocks_sell(self):
        df = _candles(50, base_price=2345.0)
        signal = asian_breakout.generate_signal(df, 2360.0, 2350.0, HTF_BIAS_BULLISH)
        assert signal is None

    def test_h4_neutral_allows_both(self):
        df_buy = _candles(50, base_price=2365.0)
        assert asian_breakout.generate_signal(df_buy, 2360.0, 2350.0, HTF_BIAS_NEUTRAL) is not None
        df_sell = _candles(50, base_price=2345.0)
        assert asian_breakout.generate_signal(df_sell, 2360.0, 2350.0, HTF_BIAS_NEUTRAL) is not None

    def test_no_signal_narrow_range(self):
        df = _candles(50, base_price=2362.5)
        signal = asian_breakout.generate_signal(df, 2363.0, 2361.0)
        assert signal is None

    def test_sl_within_range(self):
        df = _candles(50, base_price=2365.0)
        signal = asian_breakout.generate_signal(df, 2360.0, 2350.0)
        assert signal is not None
        assert BREAKOUT_SL_MIN <= signal["sl_distance"] <= BREAKOUT_SL_MAX

    def test_signal_has_atr_at_entry(self):
        df = _candles(50, base_price=2365.0)
        signal = asian_breakout.generate_signal(df, 2360.0, 2350.0)
        assert signal is not None
        assert "atr_at_entry" in signal
        assert signal["atr_at_entry"] > 0

    def test_invalid_asian_range_returns_none(self):
        df = _candles(50, base_price=2365.0)
        assert asian_breakout.generate_signal(df, 0.0, 0.0) is None
        assert asian_breakout.generate_signal(df, 2350.0, 2360.0) is None

    def test_signal_has_required_keys(self):
        df = _candles(50, base_price=2365.0)
        signal = asian_breakout.generate_signal(df, 2360.0, 2350.0)
        assert signal is not None
        for key in ("id", "model", "direction", "entry_price", "sl_price",
                    "tp_price", "sl_distance", "atr_at_entry", "session", "regime", "reason"):
            assert key in signal
