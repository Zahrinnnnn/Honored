"""
test_geto_validation.py — GETO risk manager tests

Tests all validation checks using a mock StateManager (no SQLite, no network).

Coverage
────────
- _regime_and_bias_ok(): all model/direction/regime/bias combinations
- _is_structural_break_active(): cooldown logic
- ValidationResult.summary()
- validate(): each of the 12 checks — one test per check, pass and fail
- validate(): all checks passing → APPROVED
- account_monitor.get_account_snapshot(): missing row fallback
- consecutive_tracker: thresholds
- dd_monitor: thresholds
- news_calendar: primary source, fallback, fail-safe
- agent._monitor_halt_conditions(): soft halt and emergency halt trigger

Run: python -m pytest tests/test_geto_validation.py -v
"""

import sys
import os
import asyncio

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.geto.skills.trade_validator import (
    validate,
    ValidationResult,
    _regime_and_bias_ok,
)
from agents.geto.skills.account_monitor   import get_account_snapshot
from agents.geto.skills.consecutive_tracker import (
    get_consecutive_losses,
    is_soft_halt_triggered,
)
from agents.geto.skills.dd_monitor import (
    get_drawdown_pct,
    is_emergency_halt_triggered,
)
from agents.geto.skills.news_calendar import (
    get_minutes_to_next_news,
    is_news_clear,
)
from core.constants import (
    MODEL_A, MODEL_B, MODEL_C,
    HTF_BIAS_BULLISH, HTF_BIAS_BEARISH, HTF_BIAS_NEUTRAL,
    REGIME_BULLISH_GRIND, REGIME_BEARISH_GRIND, REGIME_TIGHT_RANGE,
    MAX_CONSECUTIVE_LOSSES,
    MAX_DRAWDOWN_PCT,
    MAX_SPREAD_DOLLARS,
    NEWS_BLACKOUT_MINUTES,
)


# ---------------------------------------------------------------------------
# Helpers — mock state factory
# ---------------------------------------------------------------------------

def _make_state(
    session:            str   = "LONDON_OPEN",
    model:              str   = MODEL_A,
    trade_count:        int   = 0,
    consecutive_losses: int   = 0,
    dd_pct:             float = 0.0,
    open_positions:     int   = 0,
    minutes_to_news:    float = 999.0,
    spread:             float = 1.5,
    pause_flag:         bool  = False,
    halt_flag:          bool  = False,
    emergency_halt:     bool  = False,
    current_regime:     str   = REGIME_BULLISH_GRIND,
    h4_bias:            str   = HTF_BIAS_BULLISH,
    structural_break_until: str = "",
) -> MagicMock:
    """Build a fully pre-configured mock StateManager."""
    state = MagicMock()

    state.get_session_trade_count = AsyncMock(return_value=trade_count)
    state.get_consecutive_losses  = AsyncMock(return_value=consecutive_losses)
    state.get_account             = AsyncMock(return_value={
        "balance":        100.0,
        "equity":         100.0,
        "peak_balance":   100.0,
        "current_dd_pct": dd_pct,
        "open_positions": open_positions,
    })

    async def _get_session_info(key):
        data = {
            "minutes_to_next_news":    str(minutes_to_news),
            "current_spread":          str(spread),
            "current_regime":          current_regime,
            "h4_bias":                 h4_bias,
            "structural_break_until":  structural_break_until,
        }
        return data.get(key, "")

    state.get_session_info = AsyncMock(side_effect=_get_session_info)

    async def _get_system_flag(key):
        flags = {
            "pause_flag":          pause_flag,
            "halt_flag":           halt_flag,
            "emergency_halt_flag": emergency_halt,
        }
        return flags.get(key, False)

    state.get_system_flag = AsyncMock(side_effect=_get_system_flag)

    state.set_trading_state = AsyncMock()
    state.set_system_flag   = AsyncMock()
    state.push_alert        = AsyncMock()

    return state


def _make_signal(
    model:     str = MODEL_A,
    direction: str = "BUY",
    session:   str = "LONDON_OPEN",
    regime:    str = HTF_BIAS_BULLISH,
) -> dict:
    return {
        "id":          "test-uuid",
        "model":       model,
        "direction":   direction,
        "entry_price": 2345.00,
        "sl_price":    2340.00,
        "tp_price":    2360.00,
        "sl_distance": 5.00,
        "session":     session,
        "regime":      regime,
        "reason":      "test signal",
        "status":      "PENDING",
    }


def run(coro):
    """Shorthand to run a coroutine in tests."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# _regime_and_bias_ok unit tests
# ---------------------------------------------------------------------------

class TestRegimeAndBiasOk:
    # Model A — requires exact regime match (BULLISH_GRIND for BUY, BEARISH_GRIND for SELL)
    def test_model_a_buy_bullish_grind(self):
        assert _regime_and_bias_ok(MODEL_A, "BUY", REGIME_BULLISH_GRIND, HTF_BIAS_NEUTRAL) is True

    def test_model_a_sell_bearish_grind(self):
        assert _regime_and_bias_ok(MODEL_A, "SELL", REGIME_BEARISH_GRIND, HTF_BIAS_NEUTRAL) is True

    def test_model_a_buy_bearish_grind(self):
        assert _regime_and_bias_ok(MODEL_A, "BUY", REGIME_BEARISH_GRIND, HTF_BIAS_NEUTRAL) is False

    def test_model_a_sell_bullish_grind(self):
        assert _regime_and_bias_ok(MODEL_A, "SELL", REGIME_BULLISH_GRIND, HTF_BIAS_NEUTRAL) is False

    def test_model_a_buy_tight_range(self):
        assert _regime_and_bias_ok(MODEL_A, "BUY", REGIME_TIGHT_RANGE, HTF_BIAS_NEUTRAL) is False

    # Model B — requires TIGHT_RANGE for any direction
    def test_model_b_buy_tight_range(self):
        assert _regime_and_bias_ok(MODEL_B, "BUY", REGIME_TIGHT_RANGE, HTF_BIAS_NEUTRAL) is True

    def test_model_b_sell_tight_range(self):
        assert _regime_and_bias_ok(MODEL_B, "SELL", REGIME_TIGHT_RANGE, HTF_BIAS_NEUTRAL) is True

    def test_model_b_buy_bullish_grind(self):
        assert _regime_and_bias_ok(MODEL_B, "BUY", REGIME_BULLISH_GRIND, HTF_BIAS_NEUTRAL) is False

    # Model C — uses H4 bias (not regime)
    def test_model_c_buy_neutral(self):
        assert _regime_and_bias_ok(MODEL_C, "BUY", "", HTF_BIAS_NEUTRAL) is True

    def test_model_c_buy_bearish(self):
        assert _regime_and_bias_ok(MODEL_C, "BUY", "", HTF_BIAS_BEARISH) is False

    def test_model_c_sell_bullish(self):
        assert _regime_and_bias_ok(MODEL_C, "SELL", "", HTF_BIAS_BULLISH) is False

    def test_model_c_sell_neutral(self):
        assert _regime_and_bias_ok(MODEL_C, "SELL", "", HTF_BIAS_NEUTRAL) is True

    def test_unknown_model_fails(self):
        assert _regime_and_bias_ok("UNKNOWN", "BUY", REGIME_BULLISH_GRIND, HTF_BIAS_BULLISH) is False


# ---------------------------------------------------------------------------
# _is_structural_break_active / structural_break_clear tests
# ---------------------------------------------------------------------------

class TestStructuralBreakClear:

    def test_no_cooldown(self):
        """Empty structural_break_until means no cooldown — check passes."""
        state  = _make_state(structural_break_until="")
        signal = _make_signal()
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["structural_break_clear"] is True

    def test_active_cooldown(self):
        """Future datetime means cooldown active — check fails."""
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        state  = _make_state(structural_break_until=future)
        signal = _make_signal()
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["structural_break_clear"] is False

    def test_expired_cooldown(self):
        """Past datetime means cooldown expired — check passes."""
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        state  = _make_state(structural_break_until=past)
        signal = _make_signal()
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["structural_break_clear"] is True


# ---------------------------------------------------------------------------
# ValidationResult tests
# ---------------------------------------------------------------------------

class TestValidationResult:
    def test_approved_summary(self):
        r = ValidationResult(
            approved=True,
            checks={"a": True, "b": True},
            fail_reason="",
            signal={},
        )
        assert "APPROVED" in r.summary()

    def test_rejected_summary(self):
        r = ValidationResult(
            approved=False,
            checks={"a": True, "b": False},
            fail_reason="b",
            signal={},
        )
        assert "REJECTED" in r.summary()
        assert "b" in r.summary()


# ---------------------------------------------------------------------------
# validate() — individual check tests
# ---------------------------------------------------------------------------

class TestValidateChecks:

    # ── 1. session_valid ────────────────────────────────────────────────────

    def test_session_valid_passes(self):
        state  = _make_state()
        signal = _make_signal()
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["session_valid"] is True

    def test_session_valid_fails_when_none(self):
        state  = _make_state()
        signal = _make_signal()
        result = run(validate(signal, state, current_session=None))
        assert result.checks["session_valid"] is False
        assert result.approved is False
        assert result.fail_reason == "session_valid"

    def test_session_valid_fails_when_blackout(self):
        state  = _make_state()
        signal = _make_signal()
        result = run(validate(signal, state, current_session="ASIAN_BLACKOUT"))
        assert result.checks["session_valid"] is False

    # ── 2. model_priority_ok ────────────────────────────────────────────────

    def test_model_priority_model_a_during_breakout_fails(self):
        state  = _make_state()
        signal = _make_signal(model=MODEL_A)
        result = run(validate(
            signal, state,
            current_session="LONDON_BREAKOUT",
            is_breakout_window=True,
        ))
        assert result.checks["model_priority_ok"] is False

    def test_model_priority_model_c_during_breakout_passes(self):
        state  = _make_state(trade_count=0, h4_bias=HTF_BIAS_BULLISH)
        signal = _make_signal(model=MODEL_C, session="LONDON_BREAKOUT", direction="BUY")
        result = run(validate(
            signal, state,
            current_session="LONDON_BREAKOUT",
            is_breakout_window=True,
        ))
        assert result.checks["model_priority_ok"] is True

    def test_model_priority_outside_breakout_always_passes(self):
        state  = _make_state()
        signal = _make_signal(model=MODEL_A)
        result = run(validate(signal, state, current_session="LONDON_OPEN",
                              is_breakout_window=False))
        assert result.checks["model_priority_ok"] is True

    # ── 3. regime_and_bias_ok ─────────────────────────────────────────────

    def test_regime_model_a_buy_bullish_grind_passes(self):
        state  = _make_state(current_regime=REGIME_BULLISH_GRIND)
        signal = _make_signal(model=MODEL_A, direction="BUY")
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["regime_and_bias_ok"] is True

    def test_regime_model_a_sell_bearish_grind_passes(self):
        state  = _make_state(current_regime=REGIME_BEARISH_GRIND)
        signal = _make_signal(model=MODEL_A, direction="SELL")
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["regime_and_bias_ok"] is True

    def test_regime_model_a_buy_bearish_grind_fails(self):
        state  = _make_state(current_regime=REGIME_BEARISH_GRIND)
        signal = _make_signal(model=MODEL_A, direction="BUY")
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["regime_and_bias_ok"] is False

    def test_regime_model_b_buy_tight_range_passes(self):
        state  = _make_state(current_regime=REGIME_TIGHT_RANGE)
        signal = _make_signal(model=MODEL_B, direction="BUY")
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["regime_and_bias_ok"] is True

    def test_regime_model_b_sell_bullish_grind_fails(self):
        state  = _make_state(current_regime=REGIME_BULLISH_GRIND)
        signal = _make_signal(model=MODEL_B, direction="SELL")
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["regime_and_bias_ok"] is False

    def test_regime_model_c_buy_h4_bearish_fails(self):
        state  = _make_state(h4_bias=HTF_BIAS_BEARISH)
        signal = _make_signal(model=MODEL_C, session="LONDON_BREAKOUT", direction="BUY")
        result = run(validate(
            signal, state,
            current_session="LONDON_BREAKOUT",
            is_breakout_window=True,
        ))
        assert result.checks["regime_and_bias_ok"] is False

    def test_regime_empty_string_fails_model_a(self):
        """Missing regime (empty string) should fail for Model A."""
        state  = _make_state(current_regime="")
        signal = _make_signal(model=MODEL_A, direction="BUY")
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["regime_and_bias_ok"] is False

    # ── 4. session_trades_within_limit ──────────────────────────────────────

    def test_session_trades_below_limit_passes(self):
        state  = _make_state(trade_count=1)   # limit for MODEL_A is 2
        signal = _make_signal(model=MODEL_A)
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["session_trades_within_limit"] is True

    def test_session_trades_at_limit_fails(self):
        state  = _make_state(trade_count=8)   # exactly at limit (8)
        signal = _make_signal(model=MODEL_A)
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["session_trades_within_limit"] is False

    def test_session_trades_model_b_limit(self):
        # MODEL_B limit = 8
        state  = _make_state(trade_count=7, current_regime=REGIME_TIGHT_RANGE)
        signal = _make_signal(model=MODEL_B, direction="BUY")
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["session_trades_within_limit"] is True

        state2 = _make_state(trade_count=8, current_regime=REGIME_TIGHT_RANGE)
        result2 = run(validate(signal, state2, current_session="LONDON_OPEN"))
        assert result2.checks["session_trades_within_limit"] is False

    def test_session_trades_model_c_daily_limit(self):
        # MODEL_C limit = 1
        state  = _make_state(trade_count=0, h4_bias=HTF_BIAS_BULLISH)
        signal = _make_signal(model=MODEL_C, session="LONDON_BREAKOUT", direction="BUY")
        result = run(validate(
            signal, state,
            current_session="LONDON_BREAKOUT",
            is_breakout_window=True,
        ))
        assert result.checks["session_trades_within_limit"] is True

        state2 = _make_state(trade_count=1, h4_bias=HTF_BIAS_BULLISH)
        result2 = run(validate(
            signal, state2,
            current_session="LONDON_BREAKOUT",
            is_breakout_window=True,
        ))
        assert result2.checks["session_trades_within_limit"] is False

    # ── 5. consecutive_losses_ok ────────────────────────────────────────────

    def test_consecutive_losses_below_limit_passes(self):
        state  = _make_state(consecutive_losses=2)
        signal = _make_signal()
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["consecutive_losses_ok"] is True

    def test_consecutive_losses_at_limit_fails(self):
        state  = _make_state(consecutive_losses=MAX_CONSECUTIVE_LOSSES)
        signal = _make_signal()
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["consecutive_losses_ok"] is False

    def test_consecutive_losses_zero_passes(self):
        state  = _make_state(consecutive_losses=0)
        signal = _make_signal()
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["consecutive_losses_ok"] is True

    # ── 6. drawdown_ok ──────────────────────────────────────────────────────

    def test_drawdown_below_limit_passes(self):
        state  = _make_state(dd_pct=25.0)
        signal = _make_signal()
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["drawdown_ok"] is True

    def test_drawdown_at_limit_fails(self):
        state  = _make_state(dd_pct=50.0)   # exactly at 50%
        signal = _make_signal()
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["drawdown_ok"] is False

    def test_drawdown_above_limit_fails(self):
        state  = _make_state(dd_pct=65.0)
        signal = _make_signal()
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["drawdown_ok"] is False

    # ── 8. news_clear ───────────────────────────────────────────────────────

    def test_news_clear_far_away_passes(self):
        state  = _make_state(minutes_to_news=999.0)
        signal = _make_signal()
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["news_clear"] is True

    def test_news_clear_just_beyond_threshold_passes(self):
        state  = _make_state(minutes_to_news=float(NEWS_BLACKOUT_MINUTES) + 1)
        signal = _make_signal()
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["news_clear"] is True

    def test_news_clear_at_threshold_fails(self):
        state  = _make_state(minutes_to_news=float(NEWS_BLACKOUT_MINUTES))
        signal = _make_signal()
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["news_clear"] is False

    def test_news_clear_zero_fails(self):
        state  = _make_state(minutes_to_news=0.0)
        signal = _make_signal()
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["news_clear"] is False

    # ── 9. spread_acceptable ────────────────────────────────────────────────

    def test_spread_below_max_passes(self):
        state  = _make_state(spread=1.5)
        signal = _make_signal()
        result = run(validate(signal, state, current_session="LONDON_OPEN",
                              current_spread=1.5))
        assert result.checks["spread_acceptable"] is True

    def test_spread_at_max_fails(self):
        state  = _make_state()
        signal = _make_signal()
        result = run(validate(signal, state, current_session="LONDON_OPEN",
                              current_spread=MAX_SPREAD_DOLLARS))
        assert result.checks["spread_acceptable"] is False

    def test_spread_above_max_fails(self):
        state  = _make_state()
        signal = _make_signal()
        result = run(validate(signal, state, current_session="LONDON_OPEN",
                              current_spread=5.0))
        assert result.checks["spread_acceptable"] is False

    # ── 10. not_paused ──────────────────────────────────────────────────────

    def test_not_paused_passes(self):
        state  = _make_state(pause_flag=False)
        signal = _make_signal()
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["not_paused"] is True

    def test_paused_fails(self):
        state  = _make_state(pause_flag=True)
        signal = _make_signal()
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["not_paused"] is False

    # ── 11. not_halted ──────────────────────────────────────────────────────

    def test_not_halted_passes(self):
        state  = _make_state(halt_flag=False, emergency_halt=False)
        signal = _make_signal()
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["not_halted"] is True

    def test_halt_flag_fails(self):
        state  = _make_state(halt_flag=True)
        signal = _make_signal()
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["not_halted"] is False

    def test_emergency_halt_flag_fails(self):
        state  = _make_state(emergency_halt=True)
        signal = _make_signal()
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert result.checks["not_halted"] is False


# ---------------------------------------------------------------------------
# validate() — integration: all checks pass → APPROVED
# ---------------------------------------------------------------------------

class TestValidateApproved:

    def test_all_checks_pass_model_a(self):
        state  = _make_state(
            session="LONDON_OPEN",
            model=MODEL_A,
            trade_count=0,
            consecutive_losses=0,
            dd_pct=10.0,
            open_positions=0,
            minutes_to_news=999.0,
            spread=1.5,
            pause_flag=False,
            halt_flag=False,
            emergency_halt=False,
            current_regime=REGIME_BULLISH_GRIND,
            h4_bias=HTF_BIAS_BULLISH,
        )
        signal = _make_signal(model=MODEL_A, session="LONDON_OPEN", direction="BUY")
        result = run(validate(
            signal, state,
            current_session="LONDON_OPEN",
            is_breakout_window=False,
            current_spread=1.5,
        ))
        assert result.approved is True
        assert result.fail_reason == ""
        assert all(result.checks.values()), f"Failed checks: {[k for k,v in result.checks.items() if not v]}"

    def test_all_checks_pass_model_b(self):
        state  = _make_state(
            trade_count=0,
            consecutive_losses=1,
            dd_pct=5.0,
            open_positions=1,
            minutes_to_news=60.0,
            current_regime=REGIME_TIGHT_RANGE,
        )
        signal = _make_signal(model=MODEL_B, session="NY_OVERLAP", direction="BUY")
        result = run(validate(
            signal, state,
            current_session="NY_OVERLAP",
            is_breakout_window=False,
            current_spread=2.0,
        ))
        assert result.approved is True

    def test_all_checks_pass_model_c(self):
        state  = _make_state(
            trade_count=0,
            minutes_to_news=999.0,
            h4_bias=HTF_BIAS_BULLISH,
        )
        signal = _make_signal(
            model=MODEL_C,
            session="LONDON_BREAKOUT",
            direction="BUY",
        )
        result = run(validate(
            signal, state,
            current_session="LONDON_BREAKOUT",
            is_breakout_window=True,
            current_spread=1.0,
        ))
        assert result.approved is True

    def test_result_has_all_11_checks(self):
        state  = _make_state()
        signal = _make_signal()
        result = run(validate(signal, state, current_session="LONDON_OPEN"))
        assert len(result.checks) == 11

    def test_first_failing_check_is_fail_reason(self):
        """When multiple checks fail, fail_reason is the FIRST one."""
        state  = _make_state(
            pause_flag=True,
            halt_flag=True,
        )
        signal = _make_signal()
        result = run(validate(signal, state, current_session=None))
        assert result.fail_reason == "session_valid"  # first in order


# ---------------------------------------------------------------------------
# account_monitor tests
# ---------------------------------------------------------------------------

class TestAccountMonitor:

    def test_returns_all_keys(self):
        state = _make_state(dd_pct=10.0, open_positions=1)
        snap  = run(get_account_snapshot(state))
        assert set(snap.keys()) == {
            "balance", "equity", "peak_balance", "current_dd_pct", "open_positions"
        }

    def test_missing_account_returns_zeros(self):
        state = MagicMock()
        state.get_account = AsyncMock(return_value={})
        snap = run(get_account_snapshot(state))
        assert snap["current_dd_pct"] == 0.0
        assert snap["open_positions"] == 0

    def test_values_cast_correctly(self):
        state = MagicMock()
        state.get_account = AsyncMock(return_value={
            "balance":        "1234.56",
            "equity":         "1200.00",
            "peak_balance":   "1300.00",
            "current_dd_pct": "5.2",
            "open_positions": "2",
        })
        snap = run(get_account_snapshot(state))
        assert snap["balance"] == 1234.56
        assert snap["open_positions"] == 2


# ---------------------------------------------------------------------------
# consecutive_tracker tests
# ---------------------------------------------------------------------------

class TestConsecutiveTracker:

    def test_get_consecutive_losses(self):
        state = MagicMock()
        state.get_consecutive_losses = AsyncMock(return_value=2)
        assert run(get_consecutive_losses(state)) == 2

    def test_soft_halt_not_triggered_below(self):
        state = MagicMock()
        state.get_consecutive_losses = AsyncMock(
            return_value=MAX_CONSECUTIVE_LOSSES - 1
        )
        assert run(is_soft_halt_triggered(state)) is False

    def test_soft_halt_triggered_at_limit(self):
        state = MagicMock()
        state.get_consecutive_losses = AsyncMock(
            return_value=MAX_CONSECUTIVE_LOSSES
        )
        assert run(is_soft_halt_triggered(state)) is True

    def test_soft_halt_triggered_above_limit(self):
        state = MagicMock()
        state.get_consecutive_losses = AsyncMock(
            return_value=MAX_CONSECUTIVE_LOSSES + 2
        )
        assert run(is_soft_halt_triggered(state)) is True


# ---------------------------------------------------------------------------
# dd_monitor tests
# ---------------------------------------------------------------------------

class TestDDMonitor:

    def test_get_drawdown_pct(self):
        state = MagicMock()
        state.get_account = AsyncMock(return_value={"current_dd_pct": 12.5})
        assert run(get_drawdown_pct(state)) == 12.5

    def test_get_drawdown_pct_missing(self):
        state = MagicMock()
        state.get_account = AsyncMock(return_value={})
        assert run(get_drawdown_pct(state)) == 0.0

    def test_emergency_halt_not_triggered_below(self):
        state = MagicMock()
        state.get_account = AsyncMock(
            return_value={"current_dd_pct": MAX_DRAWDOWN_PCT * 100 - 1}
        )
        assert run(is_emergency_halt_triggered(state)) is False

    def test_emergency_halt_triggered_at_threshold(self):
        state = MagicMock()
        state.get_account = AsyncMock(
            return_value={"current_dd_pct": MAX_DRAWDOWN_PCT * 100}   # 50.0
        )
        assert run(is_emergency_halt_triggered(state)) is True

    def test_emergency_halt_triggered_above_threshold(self):
        state = MagicMock()
        state.get_account = AsyncMock(return_value={"current_dd_pct": 75.0})
        assert run(is_emergency_halt_triggered(state)) is True


# ---------------------------------------------------------------------------
# news_calendar tests
# ---------------------------------------------------------------------------

class TestNewsCalendar:

    def test_reads_from_session_info(self):
        state = MagicMock()
        state.get_session_info = AsyncMock(return_value="120.5")
        assert run(get_minutes_to_next_news(state)) == 120.5

    def test_returns_zero_when_missing_and_fetcher_fails(self):
        state2 = MagicMock()
        state2.get_session_info = AsyncMock(return_value="0.0")
        assert run(is_news_clear(state2)) is False

    def test_is_news_clear_true_when_far(self):
        state = MagicMock()
        state.get_session_info = AsyncMock(return_value="999.0")
        assert run(is_news_clear(state)) is True

    def test_is_news_clear_false_at_threshold(self):
        state = MagicMock()
        state.get_session_info = AsyncMock(
            return_value=str(float(NEWS_BLACKOUT_MINUTES))
        )
        assert run(is_news_clear(state)) is False

    def test_fallback_returns_zero_on_fetcher_error(self):
        """When session_info is empty and news_fetcher raises, returns 0.0 (fail-safe)."""
        state = MagicMock()
        state.get_session_info = AsyncMock(return_value="")

        import builtins
        real_import = builtins.__import__

        def _block_news_fetcher(name, *args, **kwargs):
            if "news_fetcher" in name:
                raise ImportError("blocked for test")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_block_news_fetcher):
            result = run(get_minutes_to_next_news(state))

        assert result == 0.0, f"Expected 0.0 on fallback failure, got {result}"


# ---------------------------------------------------------------------------
# Halt condition monitoring (agent._monitor_halt_conditions)
# ---------------------------------------------------------------------------

class TestHaltConditions:

    def test_soft_halt_sets_flag_and_pushes_alert(self):
        from agents.geto.agent import _monitor_halt_conditions

        state = MagicMock()
        state.get_system_flag = AsyncMock(side_effect=lambda key: {
            "emergency_halt_flag": False,
            "halt_flag": False,
        }[key])
        state.get_account = AsyncMock(
            return_value={"current_dd_pct": 0.0}
        )
        state.get_consecutive_losses = AsyncMock(return_value=MAX_CONSECUTIVE_LOSSES)
        state.set_system_flag = AsyncMock()
        state.push_alert      = AsyncMock()

        run(_monitor_halt_conditions(state))

        state.set_system_flag.assert_any_call("halt_flag", True)
        assert state.push_alert.called
        call_args = state.push_alert.call_args
        assert call_args[0][0] == "SOFT_HALT"

    def test_emergency_halt_sets_both_flags_and_pushes_alert(self):
        from agents.geto.agent import _monitor_halt_conditions

        state = MagicMock()
        state.get_system_flag = AsyncMock(side_effect=lambda key: {
            "emergency_halt_flag": False,
            "halt_flag": False,
        }[key])
        state.get_account = AsyncMock(
            return_value={"current_dd_pct": 50.0}
        )
        state.get_consecutive_losses = AsyncMock(return_value=0)
        state.set_system_flag = AsyncMock()
        state.push_alert      = AsyncMock()

        run(_monitor_halt_conditions(state))

        state.set_system_flag.assert_any_call("emergency_halt_flag", True)
        state.set_system_flag.assert_any_call("halt_flag", True)
        alert_types = [call[0][0] for call in state.push_alert.call_args_list]
        assert "EMERGENCY_HALT" in alert_types

    def test_no_halt_when_already_halted(self):
        from agents.geto.agent import _monitor_halt_conditions

        state = MagicMock()
        state.get_system_flag = AsyncMock(side_effect=lambda key: {
            "emergency_halt_flag": True,
            "halt_flag": True,
        }[key])
        state.get_account = AsyncMock(return_value={"current_dd_pct": 80.0})
        state.get_consecutive_losses = AsyncMock(return_value=10)
        state.set_system_flag = AsyncMock()
        state.push_alert      = AsyncMock()

        run(_monitor_halt_conditions(state))

        state.set_system_flag.assert_not_called()
        state.push_alert.assert_not_called()

    def test_no_halt_when_conditions_clear(self):
        from agents.geto.agent import _monitor_halt_conditions

        state = MagicMock()
        state.get_system_flag = AsyncMock(side_effect=lambda key: {
            "emergency_halt_flag": False,
            "halt_flag": False,
        }[key])
        state.get_account = AsyncMock(return_value={"current_dd_pct": 5.0})
        state.get_consecutive_losses = AsyncMock(return_value=1)
        state.set_system_flag = AsyncMock()
        state.push_alert      = AsyncMock()

        run(_monitor_halt_conditions(state))

        state.set_system_flag.assert_not_called()
        state.push_alert.assert_not_called()
