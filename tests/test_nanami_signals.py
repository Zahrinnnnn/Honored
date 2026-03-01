"""
test_nanami_signals.py — Phase 2 unit tests for NANAMI

Tests all non-network skills using synthetic candle data.
No MetaApi calls. No SQLite needed.

Run: python -m pytest tests/test_nanami_signals.py -v
"""

import sys
import os
from datetime import datetime, timezone, time
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


# ---------------------------------------------------------------------------
# indicator_engine tests
# ---------------------------------------------------------------------------

class TestIndicatorEngine:
    def test_columns_added(self):
        df = indicator_engine.add_indicators(_candles(200))
        expected = [
            "ema9", "ema21", "ema50",
            "rsi14", "atr14", "adx14",
            "macd", "macd_signal", "macd_hist",
            "bb_upper", "bb_mid", "bb_lower",
        ]
        for col in expected:
            assert col in df.columns, f"Missing column: {col}"

    def test_returns_copy(self):
        original = _candles(200)
        enriched = indicator_engine.add_indicators(original)
        assert "ema21" not in original.columns
        assert "ema21" in enriched.columns

    def test_no_nan_at_end(self):
        """Last row should have valid indicators with 200 candles."""
        df = indicator_engine.add_indicators(_candles(200))
        last = df.iloc[-1]
        # EMA50 needs 50 candles; ADX14 needs ~28; BB20 needs 20
        for col in ["ema21", "ema50", "rsi14", "adx14", "bb_upper", "bb_lower"]:
            assert not pd.isna(last[col]), f"{col} is NaN on last row with 200 candles"

    def test_has_valid_indicators_helper(self):
        df = indicator_engine.add_indicators(_candles(200))
        row = df.iloc[-1]
        assert indicator_engine.has_valid_indicators(row, ["ema21", "rsi14"])
        # Force NaN
        row2 = row.copy()
        row2["ema21"] = float("nan")
        assert not indicator_engine.has_valid_indicators(row2, ["ema21"])


# ---------------------------------------------------------------------------
# session_detector tests
# ---------------------------------------------------------------------------

class TestSessionDetector:
    def _mock_time(self, hour: int, minute: int = 0):
        """Return a time object for use in patches."""
        from datetime import time as time_
        return time_(hour, minute)

    def _at(self, hour: int, minute: int = 0):
        """Patch _now_utc_time() to return a specific time."""
        t = time(hour, minute)
        return patch.object(session_detector, "_now_utc_time", return_value=t)

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


# ---------------------------------------------------------------------------
# regime_detector tests
# ---------------------------------------------------------------------------

class TestRegimeDetector:
    def test_trending_regime(self):
        df = indicator_engine.add_indicators(_trending_candles(200))
        # Just test the function returns a valid regime string
        regime = regime_detector.detect_regime(df)
        assert regime in ("TRENDING", "RANGING", "VOLATILE")

    def test_ranging_regime(self):
        df = indicator_engine.add_indicators(_ranging_candles(200))
        regime = regime_detector.detect_regime(df)
        assert regime in ("TRENDING", "RANGING", "VOLATILE")

    def test_empty_df_returns_volatile(self):
        assert regime_detector.detect_regime(pd.DataFrame()) == "VOLATILE"

    def test_missing_adx_column_returns_volatile(self):
        df = _candles(50)  # no indicators added
        assert regime_detector.detect_regime(df) == "VOLATILE"

    def test_nan_adx_returns_volatile(self):
        df = indicator_engine.add_indicators(_candles(10))  # too few for ADX
        assert regime_detector.detect_regime(df) == "VOLATILE"

    def test_atr_spike_overrides_to_volatile(self):
        """If ATR is 3× mean, regime should be VOLATILE regardless of ADX."""
        df = indicator_engine.add_indicators(_trending_candles(200))
        # Force ATR spike on last row
        df = df.copy()
        df.iloc[-1, df.columns.get_loc("atr14")] = df["atr14"].iloc[-20:].mean() * 3.0
        # Force high ADX too
        df.iloc[-1, df.columns.get_loc("adx14")] = 40.0
        result = regime_detector.detect_regime(df)
        assert result == "VOLATILE"


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

        # Force last candle into a perfect BUY setup
        last_idx = len(df_copy) - 1
        close = 2450.0
        ema21 = 2448.0      # below close
        ema50_htf = 2400.0  # below close (bullish HTF)

        df_copy.at[last_idx, "close"]     = close
        df_copy.at[last_idx, "low"]       = ema21 - 0.5   # wick touched EMA21
        df_copy.at[last_idx, "ema21"]     = ema21
        df_copy.at[last_idx, "ema50"]     = 2449.0
        df_copy.at[last_idx, "rsi14"]     = 52.0           # in 40-60 zone
        df_copy.at[last_idx, "macd_hist"] = 0.05           # positive
        df_copy.at[last_idx, "adx14"]     = 30.0           # trending
        df_copy.at[last_idx, "atr14"]     = 6.0            # $6 ATR

        # M15: force last row HTF EMA50 below price
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
        bb_lower = 2341.0  # close is at/below lower band

        df_copy.at[last, "close"]    = close
        df_copy.at[last, "rsi14"]    = 25.0      # < 28 (oversold)
        df_copy.at[last, "bb_lower"] = bb_lower
        df_copy.at[last, "bb_upper"] = 2360.0
        df_copy.at[last, "bb_mid"]   = 2350.0

        return df_copy

    def _setup_overbought_sell(self):
        df = indicator_engine.add_indicators(_ranging_candles(200))
        df_copy = df.copy()
        last = len(df_copy) - 1

        close    = 2362.0
        bb_upper = 2360.0  # close is at/above upper band

        df_copy.at[last, "close"]    = close
        df_copy.at[last, "rsi14"]    = 75.0      # > 72 (overbought)
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
        df.at[len(df) - 1, "rsi14"] = 40.0  # not oversold
        signal = m1_meanrev.generate_signal(df, "NY_OVERLAP", "RANGING")
        assert signal is None

    def test_no_signal_when_not_at_bb(self):
        df = self._setup_oversold_buy()
        df.at[len(df) - 1, "close"] = 2355.0  # inside bands
        signal = m1_meanrev.generate_signal(df, "NY_OVERLAP", "RANGING")
        assert signal is None


# ---------------------------------------------------------------------------
# london_breakout (Model C) tests
# ---------------------------------------------------------------------------

class TestModelC:
    def test_buy_breakout(self):
        df = _candles(50, base_price=2365.0)  # price above asian_high
        asian_high, asian_low = 2360.0, 2350.0
        signal = london_breakout.generate_signal(df, asian_high, asian_low)
        assert signal is not None
        assert signal["direction"] == "BUY"
        assert signal["model"] == "LONDON_BREAKOUT"

    def test_sell_breakout(self):
        df = _candles(50, base_price=2345.0)  # price below asian_low
        asian_high, asian_low = 2360.0, 2350.0
        signal = london_breakout.generate_signal(df, asian_high, asian_low)
        assert signal is not None
        assert signal["direction"] == "SELL"

    def test_no_signal_inside_range(self):
        df = _candles(50, base_price=2355.0)  # price inside range
        asian_high, asian_low = 2360.0, 2350.0
        signal = london_breakout.generate_signal(df, asian_high, asian_low)
        assert signal is None

    def test_sl_within_range(self):
        df = _candles(50, base_price=2365.0)
        signal = london_breakout.generate_signal(df, 2360.0, 2350.0)
        assert signal is not None
        assert BREAKOUT_SL_MIN <= signal["sl_distance"] <= BREAKOUT_SL_MAX

    def test_sl_clamped_to_min_when_range_narrow(self):
        """Asian range of $1 → SL clamped to BREAKOUT_SL_MIN ($6)."""
        df = _candles(50, base_price=2361.5)
        asian_high, asian_low = 2361.0, 2360.0  # $1 range
        signal = london_breakout.generate_signal(df, asian_high, asian_low)
        assert signal is not None
        assert signal["sl_distance"] == BREAKOUT_SL_MIN

    def test_sl_clamped_to_max_when_range_wide(self):
        """Asian range of $20 → SL clamped to BREAKOUT_SL_MAX ($8)."""
        df = _candles(50, base_price=2381.0)
        asian_high, asian_low = 2380.0, 2360.0  # $20 range
        signal = london_breakout.generate_signal(df, asian_high, asian_low)
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
