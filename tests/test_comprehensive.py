"""
test_comprehensive.py — Unit tests for HONORED trading system components.

Covers:
  - lot_calculator    : sizing, anti-martingale, account types, edge cases
  - trade_monitor     : SL/TP, breakeven, time-kill, max-duration, PnL
  - trade_validator   : regime+bias gates, structural break check
  - htf_regime        : 6-state regime detection, structural break
  - ou_grind          : signal gates (regime, data length, ATR, zero ATR)
  - london_reversal   : session gate, pre-08h block, H4 bias filter

Run: pytest tests/test_comprehensive.py -v
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("ACCOUNT_TYPE", "CENTS")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_m5_df(n: int = 200, close_base: float = 2500.0, atr: float = 5.0) -> pd.DataFrame:
    """Synthetic M5 OHLCV with all required indicator columns."""
    rng = np.random.default_rng(42)
    closes = close_base + np.cumsum(rng.normal(0, atr * 0.1, n))
    highs  = closes + rng.uniform(0, atr * 0.5, n)
    lows   = closes - rng.uniform(0, atr * 0.5, n)
    opens  = np.roll(closes, 1); opens[0] = closes[0]
    volumes = rng.uniform(100, 500, n)
    idx = pd.date_range("2025-01-02 12:05", periods=n, freq="5min", tz="UTC")
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )
    df["atr14"]           = atr
    df["ema50"]           = pd.Series(closes).ewm(span=50).mean().values
    df["ema21"]           = pd.Series(closes).ewm(span=21).mean().values
    df["ema9"]            = pd.Series(closes).ewm(span=9).mean().values
    df["kalman_price"]    = closes
    df["kalman_velocity"] = np.gradient(closes)
    return df


def _make_h1_df(n: int = 200, trend_per_bar: float = 0.0,
                atr_mult: float = 1.0, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    base_atr = 10.0 * atr_mult
    closes = 2500.0 + np.cumsum(trend_per_bar + rng.normal(0, base_atr * 0.3, n))
    highs = closes + rng.uniform(base_atr * 0.3, base_atr * 0.7, n)
    lows  = closes - rng.uniform(base_atr * 0.3, base_atr * 0.7, n)
    idx   = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": np.roll(closes, 1), "high": highs, "low": lows, "close": closes},
        index=idx,
    )


def _make_trade(
    direction: str = "BUY",
    entry: float = 2500.0,
    sl: float = 2494.0,
    tp: float = 2512.0,
    atr: float = 4.0,
    lot: float = 0.5,
    model: str = "OU_GRIND",
    half_life: float = 10.0,
    open_time: str = None,
) -> dict:
    if open_time is None:
        open_time = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    return {
        "model":          model,
        "direction":      direction,
        "entry_price":    entry,
        "sl_price":       sl,
        "tp_price":       tp,
        "atr_at_entry":   atr,
        "lot_size":       lot,
        "half_life_bars": half_life,
        "open_time":      open_time,
    }


# ===========================================================================
# LOT CALCULATOR
# ===========================================================================

class TestLotCalculator:
    """agents/toji/skills/lot_calculator.py"""

    @pytest.fixture(autouse=True)
    def _reload_cents(self, monkeypatch):
        monkeypatch.setenv("ACCOUNT_TYPE", "CENTS")
        import importlib, core.constants as cc, agents.toji.skills.lot_calculator as lc
        importlib.reload(cc)
        importlib.reload(lc)
        self.lc = lc
        self.cc = cc

    def test_basic_lot_cents(self):
        # balance=200 USC, sl=8, risk=5%, PV=100 → lot = 10/800 = 0.0125 → 0.01
        # MetaApi returns balance in USC for CENTS accounts (500 USC = $5 USD)
        assert self.lc.calculate_lot(200.0, 8.0) == 0.01

    def test_basic_lot_standard(self, monkeypatch):
        monkeypatch.setenv("ACCOUNT_TYPE", "STANDARD")
        import importlib, core.constants as cc, agents.toji.skills.lot_calculator as lc
        importlib.reload(cc); importlib.reload(lc)
        # lot = 10 / (8 × 100) = 0.0125 → 0.01 (POINT_VALUE=100 for both)
        assert lc.calculate_lot(200.0, 8.0) == 0.01

    def test_anti_martingale_1_loss(self):
        # 0.03 / 2^1 = 0.015 → min floor 0.01
        assert self.lc.calculate_lot(200.0, 8.0, consecutive_losses=1) == 0.01

    def test_anti_martingale_2_losses(self):
        # 0.03 / 2^2 → min floor 0.01
        assert self.lc.calculate_lot(200.0, 8.0, consecutive_losses=2) == 0.01

    def test_anti_martingale_3_losses(self):
        # 0.03 / 2^3 → min floor 0.01
        assert self.lc.calculate_lot(200.0, 8.0, consecutive_losses=3) == 0.01

    def test_min_lot_floor(self):
        lot = self.lc.calculate_lot(0.10, 8.0, consecutive_losses=10)
        assert lot == 0.01

    def test_sl_zero_raises(self):
        with pytest.raises(ValueError):
            self.lc.calculate_lot(200.0, 0.0)

    def test_balance_zero_raises(self):
        with pytest.raises(ValueError):
            self.lc.calculate_lot(0.0, 8.0)

    def test_custom_risk_pct(self):
        # (200 × 0.10) / (8 × 100) = 20/800 = 0.025 → 0.03
        assert self.lc.calculate_lot(200.0, 8.0, risk_pct=0.10) == 0.03

    def test_calculate_risk_amount(self):
        assert self.lc.calculate_risk_amount(200.0) == 10.0  # 200 × 0.05

    def test_lot_respects_rr_sizing(self):
        # Smaller SL → larger lot (same risk amount)
        lot_tight = self.lc.calculate_lot(200.0, 6.0)
        lot_wide  = self.lc.calculate_lot(200.0, 12.0)
        assert lot_tight > lot_wide

    def test_no_anti_martingale_at_zero_losses(self):
        lot0 = self.lc.calculate_lot(200.0, 8.0, consecutive_losses=0)
        lot_base = self.lc.calculate_lot(200.0, 8.0)
        assert lot0 == lot_base


# ===========================================================================
# TRADE MONITOR — Exits
# ===========================================================================

class TestTradeMonitorExits:
    """agents/toji/skills/trade_monitor.py"""

    @pytest.fixture(autouse=True)
    def _import(self, monkeypatch):
        monkeypatch.setenv("ACCOUNT_TYPE", "CENTS")
        import importlib, core.constants as cc, agents.toji.skills.trade_monitor as tm
        importlib.reload(cc); importlib.reload(tm)
        self.tm = tm

    # ── SL / TP ─────────────────────────────────────────────────────────────

    def test_buy_sl_hit_loss(self):
        t = _make_trade("BUY", entry=2500, sl=2494, tp=2512)
        r = self.tm.check_exit(t, 2493.0, 2493.5)
        assert r["result"] == "LOSS"
        assert r["exit_reason"] == "SL_HIT"
        assert r["exit_price"] == 2494.0

    def test_buy_tp_hit_win(self):
        t = _make_trade("BUY", entry=2500, sl=2494, tp=2512)
        r = self.tm.check_exit(t, 2513.0, 2513.5)
        assert r["result"] == "WIN"
        assert r["exit_reason"] == "TP_HIT"
        assert r["exit_price"] == 2512.0

    def test_sell_sl_hit_loss(self):
        t = _make_trade("SELL", entry=2500, sl=2506, tp=2488)
        r = self.tm.check_exit(t, 2505.5, 2506.5)
        assert r["result"] == "LOSS"
        assert r["exit_reason"] == "SL_HIT"

    def test_sell_tp_hit_win(self):
        t = _make_trade("SELL", entry=2500, sl=2506, tp=2488)
        r = self.tm.check_exit(t, 2487.5, 2488.0)
        assert r["result"] == "WIN"
        assert r["exit_reason"] == "TP_HIT"

    def test_no_exit_within_range(self):
        t = _make_trade("BUY", entry=2500, sl=2494, tp=2512)
        assert self.tm.check_exit(t, 2505.0, 2505.5) is None

    def test_breakeven_sl_hit_returns_breakeven(self):
        # SL at entry → BREAKEVEN result
        t = _make_trade("BUY", entry=2500, sl=2500, tp=2512)
        r = self.tm.check_exit(t, 2499.5, 2500.0)
        assert r["result"] == "BREAKEVEN"

    # ── Breakeven protection ─────────────────────────────────────────────────

    def test_buy_breakeven_triggers(self):
        # ATR=4, threshold=1.5 → fire at +6 profit
        t = _make_trade("BUY", entry=2500, sl=2494, atr=4.0)
        assert self.tm.check_breakeven(t, 2507.0, 2507.5) == 2500.0

    def test_buy_breakeven_not_yet(self):
        t = _make_trade("BUY", entry=2500, sl=2494, atr=4.0)
        assert self.tm.check_breakeven(t, 2503.0, 2503.5) is None

    def test_buy_breakeven_already_set(self):
        t = _make_trade("BUY", entry=2500, sl=2500, atr=4.0)
        assert self.tm.check_breakeven(t, 2510.0, 2510.5) is None

    def test_sell_breakeven_triggers(self):
        t = _make_trade("SELL", entry=2500, sl=2506, atr=4.0)
        assert self.tm.check_breakeven(t, 2492.5, 2493.0) == 2500.0

    def test_sell_breakeven_already_set(self):
        t = _make_trade("SELL", entry=2500, sl=2500, atr=4.0)
        assert self.tm.check_breakeven(t, 2490.0, 2490.5) is None

    def test_zero_atr_no_breakeven(self):
        t = _make_trade("BUY", entry=2500, sl=2494, atr=0.0)
        assert self.tm.check_breakeven(t, 2510.0, 2510.5) is None

    # ── Time kill ────────────────────────────────────────────────────────────

    def test_ou_grind_time_kill_fires(self):
        # half_life=10 → kill at 10×3×5 = 150 min; open 160 min ago, not profitable
        ot = (datetime.now(timezone.utc) - timedelta(minutes=160)).isoformat()
        t  = _make_trade("BUY", entry=2500, sl=2494, tp=2512, model="OU_GRIND",
                         half_life=10, open_time=ot)
        r  = self.tm.check_exit(t, 2499.0, 2499.5, now=datetime.now(timezone.utc))
        assert r is not None
        assert r["exit_reason"] == "TIME_KILL"
        assert r["result"] == "LOSS"

    def test_time_kill_no_fire_if_profitable(self):
        ot = (datetime.now(timezone.utc) - timedelta(minutes=160)).isoformat()
        t  = _make_trade("BUY", entry=2500, sl=2494, tp=2512, model="OU_GRIND",
                         half_life=10, open_time=ot)
        # bid > entry → profitable → time kill should NOT fire
        r  = self.tm.check_exit(t, 2505.0, 2505.5, now=datetime.now(timezone.utc))
        assert r is None

    def test_london_reversal_time_kill_120min(self):
        # Model B time kill = 120 min, half_life=0
        ot = (datetime.now(timezone.utc) - timedelta(minutes=130)).isoformat()
        t  = _make_trade("BUY", entry=2500, sl=2494, tp=2512, model="LONDON_REVERSAL",
                         half_life=0, open_time=ot)
        r  = self.tm.check_exit(t, 2499.0, 2499.5, now=datetime.now(timezone.utc))
        assert r is not None
        assert r["exit_reason"] == "TIME_KILL"

    def test_no_exit_without_now(self):
        # If now=None, time-based exits are skipped
        ot = (datetime.now(timezone.utc) - timedelta(minutes=9999)).isoformat()
        t  = _make_trade("BUY", entry=2500, sl=2494, tp=2512, open_time=ot)
        r  = self.tm.check_exit(t, 2499.0, 2499.5, now=None)
        assert r is None

    # ── Max duration ─────────────────────────────────────────────────────────

    def test_max_duration_win(self):
        ot = (datetime.now(timezone.utc) - timedelta(minutes=250)).isoformat()
        t  = _make_trade("BUY", entry=2500, open_time=ot)
        r  = self.tm.check_exit(t, 2505.0, 2505.5, now=datetime.now(timezone.utc))
        assert r["exit_reason"] == "MAX_DURATION"
        assert r["result"] == "WIN"

    def test_max_duration_loss(self):
        # half_life=26 → time_kill = 26×2×5 = 260 min > 250 elapsed → time_kill skips
        # elapsed=250 >= MAX_DURATION=240 → MAX_DURATION fires
        ot = (datetime.now(timezone.utc) - timedelta(minutes=250)).isoformat()
        t  = _make_trade("BUY", entry=2500, open_time=ot, half_life=26)
        r  = self.tm.check_exit(t, 2498.0, 2498.5, now=datetime.now(timezone.utc))
        assert r["exit_reason"] == "MAX_DURATION"
        assert r["result"] == "LOSS"

    # ── PnL ─────────────────────────────────────────────────────────────────

    def test_pnl_buy_win_cents(self):
        t = _make_trade("BUY", entry=2500, lot=0.5)
        # 0.5 × (2512-2500) × PV(100) = 600.0 USC
        assert self.tm.calculate_pnl(t, 2512.0) == 600.0

    def test_pnl_sell_win_cents(self):
        t = _make_trade("SELL", entry=2500, lot=0.5)
        # 0.5 × (2500-2488) × PV(100) = 600.0 USC
        assert self.tm.calculate_pnl(t, 2488.0) == 600.0

    def test_pnl_buy_loss_cents(self):
        t = _make_trade("BUY", entry=2500, lot=0.5)
        # 0.5 × (2494-2500) × PV(100) = -300.0 USC
        assert self.tm.calculate_pnl(t, 2494.0) == -300.0

    def test_pnl_zero_lot(self):
        t = _make_trade("BUY", entry=2500, lot=0.0)
        assert self.tm.calculate_pnl(t, 2512.0) == 0.0

    def test_pnl_rr_symmetry(self):
        # Win should be exactly 2× the loss given 1:2 RR
        t_buy = _make_trade("BUY", entry=2500, sl=2494, tp=2512, lot=1.0)
        win_pnl  = self.tm.calculate_pnl(t_buy, 2512.0)
        loss_pnl = self.tm.calculate_pnl(t_buy, 2494.0)
        assert abs(win_pnl) == abs(loss_pnl) * 2


# ===========================================================================
# TRADE VALIDATOR — Pure logic tests (no DB)
# ===========================================================================

class TestTradeValidator:
    """agents/geto/skills/trade_validator.py — _regime_and_bias_ok + structural break."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from agents.geto.skills.trade_validator import (
            _regime_and_bias_ok, _is_structural_break_active,
        )
        from core.constants import (
            MODEL_A, MODEL_B,
            REGIME_BULLISH_GRIND, REGIME_BEARISH_GRIND,
            REGIME_BULLISH_BLOWOFF, REGIME_TIGHT_RANGE,
        )
        self.ok  = _regime_and_bias_ok
        self.sb  = _is_structural_break_active
        self.A   = MODEL_A
        self.B   = MODEL_B
        self.BG  = REGIME_BULLISH_GRIND
        self.BR  = REGIME_BEARISH_GRIND
        self.BLO = REGIME_BULLISH_BLOWOFF
        self.TR  = REGIME_TIGHT_RANGE

    # ── Model A BUY ──────────────────────────────────────────────────────────

    def test_a_buy_bull_grind_neutral(self):
        assert self.ok(self.A, "BUY", self.BG, "NEUTRAL") is True

    def test_a_buy_bull_grind_bullish(self):
        assert self.ok(self.A, "BUY", self.BG, "BULLISH") is True

    def test_a_buy_bull_grind_bearish_h4_blocked(self):
        assert self.ok(self.A, "BUY", self.BG, "BEARISH") is False

    def test_a_buy_bear_grind_blocked(self):
        assert self.ok(self.A, "BUY", self.BR, "NEUTRAL") is False

    def test_a_buy_blowoff_allowed(self):
        assert self.ok(self.A, "BUY", self.BLO, "NEUTRAL") is True

    def test_a_buy_blowoff_bearish_blocked(self):
        assert self.ok(self.A, "BUY", self.BLO, "BEARISH") is False

    # ── Model A SELL ─────────────────────────────────────────────────────────

    def test_a_sell_bear_grind_neutral(self):
        assert self.ok(self.A, "SELL", self.BR, "NEUTRAL") is True

    def test_a_sell_bear_grind_bearish(self):
        assert self.ok(self.A, "SELL", self.BR, "BEARISH") is True

    def test_a_sell_bear_grind_bullish_blocked(self):
        assert self.ok(self.A, "SELL", self.BR, "BULLISH") is False

    def test_a_sell_bull_grind_blocked(self):
        assert self.ok(self.A, "SELL", self.BG, "NEUTRAL") is False

    def test_a_sell_blowoff_blocked(self):
        assert self.ok(self.A, "SELL", self.BLO, "NEUTRAL") is False

    # ── Model B ──────────────────────────────────────────────────────────────

    def test_b_buy_neutral(self):
        assert self.ok(self.B, "BUY", self.TR, "NEUTRAL") is True

    def test_b_buy_bearish_blocked(self):
        assert self.ok(self.B, "BUY", self.TR, "BEARISH") is False

    def test_b_sell_neutral(self):
        assert self.ok(self.B, "SELL", self.TR, "NEUTRAL") is True

    def test_b_sell_bullish_blocked(self):
        assert self.ok(self.B, "SELL", self.TR, "BULLISH") is False

    def test_b_regime_agnostic(self):
        # Model B doesn't care what regime it is
        assert self.ok(self.B, "BUY", self.BG, "NEUTRAL") is True
        assert self.ok(self.B, "BUY", self.BR, "NEUTRAL") is True

    # ── Unknown model ────────────────────────────────────────────────────────

    def test_unknown_model_blocked(self):
        assert self.ok("UNKNOWN", "BUY", self.BG, "NEUTRAL") is False

    # ── Structural break ─────────────────────────────────────────────────────

    def test_sb_active(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        assert self.sb(future) is True

    def test_sb_expired(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        assert self.sb(past) is False

    def test_sb_empty_string(self):
        assert self.sb("") is False

    def test_sb_invalid_string(self):
        assert self.sb("not-a-date") is False

    def test_sb_none_like(self):
        assert self.sb(None) is False


# ===========================================================================
# HTF REGIME
# ===========================================================================

class TestHTFRegime:
    """agents/nanami/skills/htf_regime.py"""

    @pytest.fixture(autouse=True)
    def _import(self):
        from agents.nanami.skills.htf_regime import detect_regime, check_structural_break
        from core.constants import (
            REGIME_TIGHT_RANGE, REGIME_BULLISH_GRIND, REGIME_BEARISH_GRIND,
            REGIME_BULLISH_BLOWOFF, REGIME_BEARISH_PANIC, REGIME_TOXIC_CHOP,
        )
        self.detect   = detect_regime
        self.sb       = check_structural_break
        self.TIGHT    = REGIME_TIGHT_RANGE
        self.BG       = REGIME_BULLISH_GRIND
        self.BR       = REGIME_BEARISH_GRIND
        self.BLO      = REGIME_BULLISH_BLOWOFF
        self.PANIC    = REGIME_BEARISH_PANIC
        self.CHOP     = REGIME_TOXIC_CHOP
        self.VALID    = {REGIME_TIGHT_RANGE, REGIME_BULLISH_GRIND, REGIME_BEARISH_GRIND,
                         REGIME_BULLISH_BLOWOFF, REGIME_BEARISH_PANIC, REGIME_TOXIC_CHOP}

    def test_insufficient_data_defaults_to_tight_range(self):
        df = _make_h1_df(n=50)
        assert self.detect(df) == self.TIGHT

    def test_always_returns_valid_regime(self):
        df = _make_h1_df(n=200)
        assert self.detect(df) in self.VALID

    def test_strong_uptrend_is_bullish(self):
        df = _make_h1_df(n=200, trend_per_bar=8.0, seed=42)
        assert self.detect(df) in (self.BG, self.BLO)

    def test_strong_downtrend_is_bearish(self):
        df = _make_h1_df(n=200, trend_per_bar=-8.0, seed=42)
        assert self.detect(df) in (self.BR, self.PANIC)

    def test_flat_market_neutral_regime(self):
        # Sine wave oscillates around 2500 — z-score stays near 0 → low-vol neutral
        n      = 200
        idx    = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
        closes = 2500.0 + 5.0 * np.sin(np.linspace(0, 6 * np.pi, n))
        highs  = closes + 3.0
        lows   = closes - 3.0
        df     = pd.DataFrame(
            {"open": np.roll(closes, 1), "high": highs, "low": lows, "close": closes},
            index=idx,
        )
        assert self.detect(df) in (self.TIGHT, self.CHOP)

    def test_structural_break_detected_on_massive_candle(self):
        df = _make_h1_df(n=200)
        df.iloc[-1, df.columns.get_loc("high")] = df["close"].iloc[-1] + 120.0
        df.iloc[-1, df.columns.get_loc("low")]  = df["close"].iloc[-1] - 120.0
        assert self.sb(df)  # np.bool_ or bool — both truthy

    def test_no_structural_break_on_normal_candles(self):
        df = _make_h1_df(n=200)
        assert not self.sb(df)

    def test_structural_break_requires_enough_data(self):
        df = _make_h1_df(n=10)
        # Should not crash with insufficient data
        result = self.sb(df)
        assert isinstance(result, bool)


# ===========================================================================
# OU GRIND — Signal gates
# ===========================================================================

class TestOUGrindGates:
    """agents/nanami/skills/ou_grind.py"""

    @pytest.fixture(autouse=True)
    def _import(self):
        from agents.nanami.skills.ou_grind import generate_signal
        from core.constants import (
            REGIME_TIGHT_RANGE, REGIME_TOXIC_CHOP, REGIME_BEARISH_PANIC,
            REGIME_BULLISH_GRIND, REGIME_BEARISH_GRIND, MODEL_A,
        )
        self.gen     = generate_signal
        self.TIGHT   = REGIME_TIGHT_RANGE
        self.CHOP    = REGIME_TOXIC_CHOP
        self.PANIC   = REGIME_BEARISH_PANIC
        self.BG      = REGIME_BULLISH_GRIND
        self.BR      = REGIME_BEARISH_GRIND
        self.MODEL_A = MODEL_A

    def test_no_trade_regimes_return_none(self):
        df = _make_m5_df(200)
        for regime in (self.TIGHT, self.CHOP, self.PANIC):
            assert self.gen(df, "NY_OVERLAP", regime) is None

    def test_insufficient_data_returns_none(self):
        df = _make_m5_df(20)
        assert self.gen(df, "NY_OVERLAP", self.BG) is None

    def test_zero_atr_returns_none(self):
        df = _make_m5_df(200)
        df["atr14"] = 0.0
        assert self.gen(df, "NY_OVERLAP", self.BG) is None

    def test_nan_atr_returns_none(self):
        df = _make_m5_df(200)
        df["atr14"] = float("nan")
        assert self.gen(df, "NY_OVERLAP", self.BG) is None

    def test_signal_structure_when_fired(self):
        """If a signal is returned it must have all required keys and obey 1:2 RR."""
        from agents.nanami.skills.ou_grind import generate_signal
        from core.constants import REGIME_BULLISH_GRIND, MODEL_A

        # Build a mean-reverting series with a deep BUY z-score
        rng = np.random.default_rng(0)
        n   = 200
        base = 2500.0
        series = np.zeros(n)
        theta  = 0.20
        for i in range(1, n):
            series[i] = series[i - 1] + theta * (0 - series[i - 1]) + rng.normal(0, 0.4)
        closes          = base + series
        closes[-1]      = base - 12.0  # force deep below EMA50 → BUY z

        idx = pd.date_range("2025-01-02 12:05", periods=n, freq="5min", tz="UTC")
        df  = pd.DataFrame(
            {"open": closes, "high": closes + 2, "low": closes - 2, "close": closes},
            index=idx,
        )
        df["atr14"]           = 5.0
        df["ema50"]           = pd.Series(closes).ewm(span=50).mean().values
        df["ema21"]           = pd.Series(closes).ewm(span=21).mean().values
        df["ema9"]            = pd.Series(closes).ewm(span=9).mean().values
        df["kalman_price"]    = closes
        df["kalman_velocity"] = np.gradient(closes)

        signal = generate_signal(df, "NY_OVERLAP", REGIME_BULLISH_GRIND)
        if signal is not None:
            required = {
                "model", "direction", "entry_price",
                "sl_price", "tp_price", "sl_distance", "session",
            }
            assert required.issubset(signal.keys())
            assert signal["model"] == MODEL_A
            assert signal["direction"] in ("BUY", "SELL")
            sl_dist = abs(signal["entry_price"] - signal["sl_price"])
            tp_dist = abs(signal["tp_price"] - signal["entry_price"])
            assert abs(tp_dist - sl_dist * 2.0) < 0.11  # 1:2 RR within rounding

    def test_sl_clamped_to_range(self):
        """SL distance must stay within [$6, $12]."""
        from agents.nanami.skills.ou_grind import generate_signal
        from core.constants import REGIME_BULLISH_GRIND

        rng = np.random.default_rng(7)
        n   = 200
        closes = 2500.0 + np.cumsum(rng.normal(0, 0.3, n))
        closes[-1] = 2500.0 - 15.0  # force strong BUY z

        idx = pd.date_range("2025-01-02 12:05", periods=n, freq="5min", tz="UTC")
        df  = pd.DataFrame(
            {"open": closes, "high": closes + 1, "low": closes - 1, "close": closes},
            index=idx,
        )
        df["atr14"]           = 2.0  # very tight ATR → SL should hit $6 floor
        df["ema50"]           = pd.Series(closes).ewm(span=50).mean().values
        df["ema21"]           = pd.Series(closes).ewm(span=21).mean().values
        df["ema9"]            = pd.Series(closes).ewm(span=9).mean().values
        df["kalman_price"]    = closes
        df["kalman_velocity"] = np.gradient(closes)

        signal = generate_signal(df, "NY_OVERLAP", REGIME_BULLISH_GRIND)
        if signal is not None:
            assert 6.0 <= signal["sl_distance"] <= 12.0


# ===========================================================================
# LONDON REVERSAL — Session / gates
# ===========================================================================

class TestLondonReversalGates:
    """agents/nanami/skills/london_reversal.py"""

    @pytest.fixture(autouse=True)
    def _import(self):
        from agents.nanami.skills.london_reversal import generate_signal
        self.gen = generate_signal

    def _df(self, n: int = 300, hour_start: int = 8) -> pd.DataFrame:
        rng = np.random.default_rng(12)
        closes  = 2500.0 + np.cumsum(rng.normal(0, 1.0, n))
        highs   = closes + rng.uniform(0.5, 2.0, n)
        lows    = closes - rng.uniform(0.5, 2.0, n)
        opens   = np.roll(closes, 1); opens[0] = closes[0]
        volumes = rng.uniform(200, 800, n)
        idx = pd.date_range(
            f"2025-01-02 {hour_start:02d}:05", periods=n, freq="5min", tz="UTC"
        )
        df = pd.DataFrame(
            {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
            index=idx,
        )
        df["atr14"]           = 4.0
        df["kalman_price"]    = closes
        df["kalman_velocity"] = np.gradient(closes)
        return df

    def test_wrong_session_returns_none(self):
        df = self._df()
        assert self.gen(df, "NY_OVERLAP") is None
        assert self.gen(df, "NY_CLOSE")   is None

    def test_insufficient_data_returns_none(self):
        df = self._df(n=30)
        assert self.gen(df, "LONDON_OPEN") is None

    def test_pre_08h_entry_blocked(self):
        df = self._df(hour_start=7)
        assert self.gen(df, "LONDON_OPEN") is None

    def test_signal_has_correct_model(self):
        """Any fired signal must carry MODEL_B = 'LONDON_REVERSAL'."""
        from core.constants import MODEL_B
        df = self._df(n=300, hour_start=8)
        signal = self.gen(df, "LONDON_OPEN")
        if signal is not None:
            assert signal["model"] == MODEL_B

    def test_signal_respects_rr(self):
        """Fired signal must obey 1:2 RR."""
        df = self._df(n=300, hour_start=8)
        signal = self.gen(df, "LONDON_OPEN")
        if signal is not None:
            sl_dist = abs(signal["entry_price"] - signal["sl_price"])
            tp_dist = abs(signal["tp_price"] - signal["entry_price"])
            assert abs(tp_dist - sl_dist * 2.0) < 0.11

    def test_h4_bearish_blocks_buy(self):
        df = self._df(n=300, hour_start=8)
        signal = self.gen(df, "LONDON_OPEN", h4_bias="BEARISH")
        if signal is not None:
            assert signal["direction"] != "BUY"

    def test_h4_bullish_blocks_sell(self):
        df = self._df(n=300, hour_start=8)
        signal = self.gen(df, "LONDON_OPEN", h4_bias="BULLISH")
        if signal is not None:
            assert signal["direction"] != "SELL"
