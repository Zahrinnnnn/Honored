"""
test_nanami_signals.py — Phase 2 unit tests for NANAMI

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

# Make sure project root is on the path
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
from agents.nanami.skills.regime_detector import _hurst_vr
from core.constants import (
    M5_SL_MIN, M5_SL_MAX,
    M1_SL_MIN, M1_SL_MAX,
    BREAKOUT_SL_MIN, BREAKOUT_SL_MAX,
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
    """Strong uptrend candles — will produce ADX > 25."""
    return _candles(n, base_price=2300.0, trend=0.5)


def _ranging_candles(n: int = 200) -> pd.DataFrame:
    """Flat range candles — will produce ADX < 20."""
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


def _mock_dt(hour: int, minute: int = 0) -> datetime:
    """Return a timezone-aware UTC datetime for a given hour/minute."""
    return datetime(2024, 1, 15, hour, minute, 0, tzinfo=timezone.utc)


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
        """New columns added in the robust indicator engine."""
        df = indicator_engine.add_indicators(_candles(200))
        for col in ("ema21_slope", "stoch_rsi_k", "stoch_rsi_d",
                    "atr_pct", "bb_width", "bb_width_pct"):
            assert col in df.columns, f"Missing extended column: {col}"

    def test_returns_copy(self):
        original = _candles(200)
        enriched = indicator_engine.add_indicators(original)
        assert "ema21" not in original.columns
        assert "ema21" in enriched.columns

    def test_no_nan_at_end(self):
        """Last row should have valid indicators with 200 candles."""
        df = indicator_engine.add_indicators(_candles(200))
        last = df.iloc[-1]
        for col in ["ema21", "ema50", "rsi14", "adx14", "bb_upper", "bb_lower"]:
            assert not pd.isna(last[col]), f"{col} is NaN on last row with 200 candles"

    def test_extended_columns_valid_at_end(self):
        """Extended columns should be non-NaN at the last row with 200 candles."""
        df = indicator_engine.add_indicators(_candles(200))
        last = df.iloc[-1]
        for col in ("atr_pct", "bb_width", "bb_width_pct"):
            assert not pd.isna(last[col]), f"{col} is NaN on last row with 200 candles"

    def test_bb_width_pct_positive(self):
        df = indicator_engine.add_indicators(_candles(200))
        valid = df["bb_width_pct"].dropna()
        assert (valid > 0).all(), "bb_width_pct should always be positive"

    def test_atr_pct_positive(self):
        df = indicator_engine.add_indicators(_candles(200))
        # atr14 starts at 0 during the warm-up period (ta library behaviour);
        # only check rows where atr14 is actually positive (post-warm-up).
        valid = df.loc[df["atr14"] > 0, "atr_pct"]
        assert len(valid) > 0, "Expected some positive atr14 values in 200 rows"
        assert (valid > 0).all(), "atr_pct should be positive wherever atr14 > 0"

    def test_stoch_rsi_in_range(self):
        df = indicator_engine.add_indicators(_trending_candles(200))
        valid_k = df["stoch_rsi_k"].dropna()
        valid_d = df["stoch_rsi_d"].dropna()
        assert ((valid_k >= 0) & (valid_k <= 1)).all(), "stoch_rsi_k out of [0,1]"
        assert ((valid_d >= 0) & (valid_d <= 1)).all(), "stoch_rsi_d out of [0,1]"

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
# Hurst VR unit tests
# ---------------------------------------------------------------------------

class TestHurstVR:
    def test_insufficient_data_returns_neutral(self):
        """Less than 60 prices → 0.5 (neutral)."""
        prices = np.linspace(2300, 2330, 59)
        assert _hurst_vr(prices) == 0.5

    def test_exactly_min_prices_does_not_crash(self):
        """60 prices should not raise and return a float in [0, 1]."""
        rng = np.random.default_rng(1)
        prices = np.cumsum(rng.normal(0, 1, 60)) + 2300.0
        h = _hurst_vr(prices)
        assert 0.0 <= h <= 1.0

    def test_constant_prices_returns_neutral(self):
        """All prices identical → variance = 0 → degenerate → 0.5."""
        prices = np.full(120, 2350.0)
        assert _hurst_vr(prices) == 0.5

    def test_returns_float_in_range(self):
        """Any non-degenerate input should give H in [0, 1]."""
        rng = np.random.default_rng(42)
        prices = np.cumsum(rng.normal(0, 1, 150)) + 2300.0
        h = _hurst_vr(prices)
        assert 0.0 <= h <= 1.0

    def test_strong_uptrend_gives_high_h(self):
        """
        A strong deterministic uptrend with small noise should give H > 0.55.
        Theory: Var[X(t+τ) - X(t)] ≈ (slope*τ)² + 2σ²
        At large τ, the τ² term dominates → slope ≈ 2 → H ≈ 1.
        """
        rng = np.random.default_rng(7)
        prices = 2300.0 + 1.0 * np.arange(120) + rng.normal(0, 0.05, 120)
        h = _hurst_vr(prices)
        assert h > 0.55, f"Expected H > 0.55 for strong uptrend, got {h:.3f}"

    def test_random_walk_h_near_half(self):
        """
        Pure random walk: H should be roughly 0.5 (no strong bias).
        We use a generous band (0.3–0.7) since it's stochastic.
        """
        rng = np.random.default_rng(123)
        prices = np.cumsum(rng.normal(0, 1, 500)) + 2300.0
        h = _hurst_vr(prices)
        assert 0.3 <= h <= 0.7, f"Expected H ≈ 0.5 for random walk, got {h:.3f}"


# ---------------------------------------------------------------------------
# session_detector tests
# ---------------------------------------------------------------------------

class TestSessionDetector:
    def _at(self, hour: int, minute: int = 0):
        """Patch _now_utc() to return a specific UTC datetime."""
        dt = _mock_dt(hour, minute)
        return patch.object(session_detector, "_now_utc", return_value=dt)

    # ── Legacy helpers ────────────────────────────────────────────────────

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
        # 10:30 — after London Open, before NY Overlap
        with self._at(10, 30):
            assert session_detector.get_current_session() is None
            assert session_detector.is_blackout_period() is False

    # ── SessionContext ────────────────────────────────────────────────────

    def test_context_has_all_fields(self):
        with self._at(8, 0):
            ctx = session_detector.get_session_context()
            assert isinstance(ctx, SessionContext)
            assert hasattr(ctx, "name")
            assert hasattr(ctx, "is_active")
            assert hasattr(ctx, "is_blackout")
            assert hasattr(ctx, "is_breakout_window")
            assert hasattr(ctx, "minutes_elapsed")
            assert hasattr(ctx, "minutes_remaining")

    def test_context_london_open(self):
        with self._at(8, 30):
            ctx = session_detector.get_session_context()
            assert ctx.name == "LONDON_OPEN"
            assert ctx.is_active is True
            assert ctx.is_blackout is False
            assert ctx.is_breakout_window is False
            assert ctx.minutes_elapsed == 90.0   # 07:00 → 08:30
            assert ctx.minutes_remaining == 90.0  # 08:30 → 10:00

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
            assert ctx.minutes_elapsed == 0.0
            assert ctx.minutes_remaining == 0.0

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
            assert ctx.minutes_elapsed == 120.0   # 12:00 → 14:00
            assert ctx.minutes_remaining == 120.0  # 14:00 → 16:00

    def test_context_is_immutable(self):
        """SessionContext is frozen — attribute assignment should raise."""
        import dataclasses
        with self._at(8, 0):
            ctx = session_detector.get_session_context()
            try:
                ctx.name = "HACKED"  # type: ignore[misc]
                assert False, "Should have raised FrozenInstanceError"
            except dataclasses.FrozenInstanceError:
                pass


# ---------------------------------------------------------------------------
# regime_detector tests
# ---------------------------------------------------------------------------

class TestRegimeDetector:
    def test_trending_regime(self):
        df = indicator_engine.add_indicators(_trending_candles(200))
        regime = regime_detector.detect_regime(df)
        assert regime in ("TRENDING", "RANGING", "VOLATILE")

    def test_ranging_regime(self):
        df = indicator_engine.add_indicators(_ranging_candles(200))
        regime = regime_detector.detect_regime(df)
        assert regime in ("TRENDING", "RANGING", "VOLATILE")

    def test_empty_df_returns_volatile(self):
        assert regime_detector.detect_regime(pd.DataFrame()) == "VOLATILE"

    def test_missing_indicators_returns_volatile(self):
        df = _candles(50)  # no indicators added
        assert regime_detector.detect_regime(df) == "VOLATILE"

    def test_short_df_nan_indicators_returns_volatile(self):
        df = indicator_engine.add_indicators(_candles(10))  # too few for ADX
        assert regime_detector.detect_regime(df) == "VOLATILE"

    def test_atr_spike_overrides_to_volatile(self):
        """If ATR is 3× mean, regime should be VOLATILE regardless of ADX."""
        df = indicator_engine.add_indicators(_trending_candles(200))
        df = df.copy()
        df.iloc[-1, df.columns.get_loc("atr14")] = df["atr14"].iloc[-20:].mean() * 3.0
        df.iloc[-1, df.columns.get_loc("adx14")] = 40.0
        assert regime_detector.detect_regime(df) == "VOLATILE"

    def test_all_trending_signals_give_trending(self):
        """When ADX, Hurst, and BB expansion all agree → TRENDING."""
        df = indicator_engine.add_indicators(_trending_candles(200))
        df = df.copy()
        last = len(df) - 1

        # Override the last 120 close prices with a strong linear trend + tiny noise
        # so Hurst VR gives H > 0.55 (variance ∝ τ² for large lags → H ≈ 1).
        rng = np.random.default_rng(777)
        for i, idx in enumerate(range(max(0, last - 119), last + 1)):
            df.at[idx, "close"] = 2300.0 + 1.0 * i + rng.normal(0, 0.05)

        # Force ADX trending
        df.at[last, "adx14"] = 35.0

        # Force BB expansion: last value above the 70th percentile of last 50 bars
        bb_max = df["bb_width_pct"].iloc[-50:].max()
        if pd.isna(bb_max) or bb_max == 0:
            bb_max = 1.0
        df.at[last, "bb_width_pct"] = bb_max * 2.0

        result = regime_detector.detect_regime(df)
        assert result == "TRENDING"

    def test_regime_returns_valid_string(self):
        df = indicator_engine.add_indicators(_ranging_candles(200))
        result = regime_detector.detect_regime(df)
        assert result in ("TRENDING", "RANGING", "VOLATILE")


# ---------------------------------------------------------------------------
# m5_momentum (Model A) tests
# ---------------------------------------------------------------------------

class TestModelA:
    def _setup_trending_buy(self):
        """
        Build a DataFrame where the last candle should produce a BUY signal:
        - strong uptrend (price > ema50 on M15 equivalent)
        - EMA21 below close but above low (wick touched)
        - RSI ~52
        - MACD hist positive
        """
        df = indicator_engine.add_indicators(_trending_candles(200))
        df_copy = df.copy()

        last_idx = len(df_copy) - 1
        close  = 2450.0
        ema21  = 2448.0
        ema50_htf = 2400.0

        df_copy.at[last_idx, "close"]     = close
        df_copy.at[last_idx, "low"]       = ema21 - 0.5
        df_copy.at[last_idx, "ema21"]     = ema21
        df_copy.at[last_idx, "ema50"]     = 2449.0
        df_copy.at[last_idx, "rsi14"]     = 52.0
        df_copy.at[last_idx, "macd_hist"] = 0.05
        df_copy.at[last_idx, "adx14"]     = 30.0
        df_copy.at[last_idx, "atr14"]     = 6.0

        df_m15 = df_copy.copy()
        df_m15.at[last_idx, "ema50"] = ema50_htf
        df_m15.at[last_idx, "close"] = close

        return df_copy, df_m15

    def test_buy_signal_produced(self):
        df_m5, df_m15 = self._setup_trending_buy()
        signal = m5_momentum.generate_signal(df_m5, df_m15, "LONDON_OPEN", "TRENDING")
        assert signal is not None
        assert signal["direction"] == "BUY"
        assert signal["model"] == "M5_MOMENTUM"

    def test_signal_has_required_keys(self):
        df_m5, df_m15 = self._setup_trending_buy()
        signal = m5_momentum.generate_signal(df_m5, df_m15, "LONDON_OPEN", "TRENDING")
        assert signal is not None
        for key in ("id", "model", "direction", "entry_price", "sl_price",
                    "tp_price", "sl_distance", "session", "regime", "reason"):
            assert key in signal, f"Missing key: {key}"

    def test_sl_within_range(self):
        df_m5, df_m15 = self._setup_trending_buy()
        signal = m5_momentum.generate_signal(df_m5, df_m15, "LONDON_OPEN", "TRENDING")
        assert signal is not None
        assert M5_SL_MIN <= signal["sl_distance"] <= M5_SL_MAX

    def test_tp_is_3x_sl(self):
        df_m5, df_m15 = self._setup_trending_buy()
        signal = m5_momentum.generate_signal(df_m5, df_m15, "LONDON_OPEN", "TRENDING")
        assert signal is not None
        sl = signal["sl_distance"]
        tp = round(signal["tp_price"] - signal["entry_price"], 2)
        assert abs(tp - sl * 3) < 0.05

    def test_no_signal_when_ranging(self):
        df_m5, df_m15 = self._setup_trending_buy()
        signal = m5_momentum.generate_signal(df_m5, df_m15, "LONDON_OPEN", "RANGING")
        assert signal is None

    def test_no_signal_on_insufficient_data(self):
        df = indicator_engine.add_indicators(_candles(20))
        signal = m5_momentum.generate_signal(df, df, "LONDON_OPEN", "TRENDING")
        assert signal is None


# ---------------------------------------------------------------------------
# m1_meanrev (Model B) tests
# ---------------------------------------------------------------------------

class TestModelB:
    def _setup_oversold_buy(self):
        """Last candle: RSI < 28, close ≤ BB lower."""
        df = indicator_engine.add_indicators(_ranging_candles(200))
        df_copy = df.copy()
        last = len(df_copy) - 1

        close    = 2340.0
        bb_lower = 2341.0

        df_copy.at[last, "close"]    = close
        df_copy.at[last, "rsi14"]    = 25.0
        df_copy.at[last, "bb_lower"] = bb_lower
        df_copy.at[last, "bb_upper"] = 2360.0
        df_copy.at[last, "bb_mid"]   = 2350.0

        return df_copy

    def _setup_overbought_sell(self):
        df = indicator_engine.add_indicators(_ranging_candles(200))
        df_copy = df.copy()
        last = len(df_copy) - 1

        close    = 2362.0
        bb_upper = 2360.0

        df_copy.at[last, "close"]    = close
        df_copy.at[last, "rsi14"]    = 75.0
        df_copy.at[last, "bb_lower"] = 2340.0
        df_copy.at[last, "bb_upper"] = bb_upper
        df_copy.at[last, "bb_mid"]   = 2350.0

        return df_copy

    def test_buy_signal_oversold(self):
        df = self._setup_oversold_buy()
        signal = m1_meanrev.generate_signal(df, "NY_OVERLAP", "RANGING")
        assert signal is not None
        assert signal["direction"] == "BUY"
        assert signal["model"] == "M1_MEANREV"

    def test_sell_signal_overbought(self):
        df = self._setup_overbought_sell()
        signal = m1_meanrev.generate_signal(df, "NY_OVERLAP", "RANGING")
        assert signal is not None
        assert signal["direction"] == "SELL"

    def test_sl_within_range(self):
        df = self._setup_oversold_buy()
        signal = m1_meanrev.generate_signal(df, "NY_OVERLAP", "RANGING")
        assert signal is not None
        assert M1_SL_MIN <= signal["sl_distance"] <= M1_SL_MAX

    def test_tp_is_3x_sl(self):
        df = self._setup_oversold_buy()
        signal = m1_meanrev.generate_signal(df, "NY_OVERLAP", "RANGING")
        assert signal is not None
        sl = signal["sl_distance"]
        tp = round(signal["tp_price"] - signal["entry_price"], 2)
        assert abs(tp - sl * 3) < 0.05

    def test_no_signal_when_trending(self):
        df = self._setup_oversold_buy()
        signal = m1_meanrev.generate_signal(df, "NY_OVERLAP", "TRENDING")
        assert signal is None

    def test_no_signal_when_rsi_not_extreme(self):
        df = self._setup_oversold_buy()
        df.at[len(df) - 1, "rsi14"] = 40.0
        signal = m1_meanrev.generate_signal(df, "NY_OVERLAP", "RANGING")
        assert signal is None

    def test_no_signal_when_not_at_bb(self):
        df = self._setup_oversold_buy()
        df.at[len(df) - 1, "close"] = 2355.0
        signal = m1_meanrev.generate_signal(df, "NY_OVERLAP", "RANGING")
        assert signal is None


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

    def test_sl_within_range(self):
        df = _candles(50, base_price=2365.0)
        signal = london_breakout.generate_signal(df, 2360.0, 2350.0)
        assert signal is not None
        assert BREAKOUT_SL_MIN <= signal["sl_distance"] <= BREAKOUT_SL_MAX

    def test_sl_clamped_to_min_when_range_narrow(self):
        """Asian range of $1 → SL clamped to BREAKOUT_SL_MIN ($6)."""
        df = _candles(50, base_price=2361.5)
        signal = london_breakout.generate_signal(df, 2361.0, 2360.0)
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
        assert london_breakout.generate_signal(df, 2350.0, 2360.0) is None  # high < low

    def test_signal_has_required_keys(self):
        df = _candles(50, base_price=2365.0)
        signal = london_breakout.generate_signal(df, 2360.0, 2350.0)
        assert signal is not None
        for key in ("id", "model", "direction", "entry_price", "sl_price",
                    "tp_price", "sl_distance", "session", "regime", "reason"):
            assert key in signal
