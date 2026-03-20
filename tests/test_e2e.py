"""
test_e2e.py — End-to-end integration tests for HONORED trading system.

Tests the full cross-agent state machine against a real SQLite DB (tmp file).
No mocks — everything hits the actual StateManager, validator, and state updater.

Scenarios:
  1. Signal lifecycle      : PENDING → GETO validates → APPROVED/REJECTED → TOJI closes
  2. Soft halt             : 3 consecutive losses → halt_flag set → signals blocked
  3. Emergency halt        : 50% DD → emergency_halt_flag → blocked even after override
  4. Override flow         : halt cleared → signals pass again
  5. Model A session limit : 8 trades/session → 9th blocked
  6. Model B session limit : 2 trades/session → 3rd blocked
  7. News blackout         : minutes_to_next_news ≤ 30 → blocked
  8. Spread guard          : spread ≥ $4 → blocked
  9. Structural break      : active cooldown → blocked
 10. Alert queue           : push → get_pending → mark_sent lifecycle
 11. Post-trade state      : consecutive losses, session counts, account balance
 12. Pause flag            : pause → blocked; resume → pass

Run: pytest tests/test_e2e.py -v
"""

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("ACCOUNT_TYPE", "CENTS")
os.environ["PAPER_MODE"] = "true"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signal(
    model: str = "OU_GRIND",
    direction: str = "BUY",
    session: str = "NY_OVERLAP",
    entry: float = 2500.0,
    sl: float = 2494.0,
    tp: float = 2512.0,
    atr: float = 4.0,
    half_life: float = 10.0,
) -> dict:
    return {
        "model":          model,
        "direction":      direction,
        "session":        session,
        "entry_price":    entry,
        "sl_price":       sl,
        "tp_price":       tp,
        "sl_distance":    abs(entry - sl),
        "atr_at_entry":   atr,
        "half_life_bars": half_life,
        "status":         "PENDING",
    }


async def _seed_account(state, balance: float = 200.0):
    """Seed account table with starting balance."""
    await state.update_account(
        balance=balance,
        equity=balance,
        peak_balance=balance,
        current_dd_pct=0.0,
        open_positions=0,
    )


async def _set_session_env(state, session: str = "NY_OVERLAP",
                           regime: str = "BULLISH_GRIND",
                           bias: str = "NEUTRAL",
                           spread: float = 1.5,
                           mins_to_news: float = 60.0):
    """Write session_info so GETO can read it."""
    await state.set_session_info("current_session", session)
    await state.set_session_info("current_regime", regime)
    await state.set_session_info("macro_bias", bias)
    await state.set_session_info("current_spread", str(spread))
    await state.set_session_info("minutes_to_next_news", str(mins_to_news))
    await state.set_session_info("structural_break_until", "")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test_honored.db")


@pytest_asyncio.fixture
async def state(tmp_db):
    from core.state_manager import StateManager
    s = StateManager(db_path=tmp_db)
    await s.connect()
    await _seed_account(s)
    yield s
    await s.close()


# ---------------------------------------------------------------------------
# 1. Signal validation lifecycle
# ---------------------------------------------------------------------------

class TestSignalValidation:

    @pytest.mark.asyncio
    async def test_valid_signal_approved(self, state):
        from agents.geto.skills.trade_validator import validate

        await _set_session_env(state)
        signal = _make_signal("OU_GRIND", "BUY", "NY_OVERLAP")
        result = await validate(signal, state, current_session="NY_OVERLAP", current_spread=1.5)

        assert result.approved is True
        assert result.fail_reason == ""

    @pytest.mark.asyncio
    async def test_wrong_session_rejected(self, state):
        from agents.geto.skills.trade_validator import validate

        await _set_session_env(state, session="ASIAN_BLACKOUT")
        signal = _make_signal()
        result = await validate(signal, state, current_session="ASIAN_BLACKOUT", current_spread=1.5)

        assert result.approved is False
        assert result.fail_reason == "session_valid"

    @pytest.mark.asyncio
    async def test_spread_too_wide_rejected(self, state):
        from agents.geto.skills.trade_validator import validate

        await _set_session_env(state, spread=5.0)
        signal = _make_signal()
        result = await validate(signal, state, current_session="NY_OVERLAP", current_spread=5.0)

        assert result.approved is False
        assert result.fail_reason == "spread_acceptable"

    @pytest.mark.asyncio
    async def test_news_blackout_rejected(self, state):
        from agents.geto.skills.trade_validator import validate

        await _set_session_env(state, mins_to_news=10.0)
        signal = _make_signal()
        result = await validate(signal, state, current_session="NY_OVERLAP", current_spread=1.5)

        assert result.approved is False
        assert result.fail_reason == "news_clear"

    @pytest.mark.asyncio
    async def test_wrong_regime_rejected(self, state):
        from agents.geto.skills.trade_validator import validate

        await _set_session_env(state, regime="TIGHT_RANGE")
        signal = _make_signal("OU_GRIND", "BUY", "NY_OVERLAP")
        result = await validate(signal, state, current_session="NY_OVERLAP", current_spread=1.5)

        assert result.approved is False
        assert result.fail_reason == "regime_and_bias_ok"

    @pytest.mark.asyncio
    async def test_structural_break_cooldown_blocks(self, state):
        from agents.geto.skills.trade_validator import validate

        await _set_session_env(state)
        future = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        await state.set_session_info("structural_break_until", future)

        signal = _make_signal()
        result = await validate(signal, state, current_session="NY_OVERLAP", current_spread=1.5)

        assert result.approved is False
        assert result.fail_reason == "structural_break_clear"

    @pytest.mark.asyncio
    async def test_all_checks_visible_in_result(self, state):
        from agents.geto.skills.trade_validator import validate

        await _set_session_env(state)
        signal = _make_signal()
        result = await validate(signal, state, current_session="NY_OVERLAP", current_spread=1.5)

        assert len(result.checks) == 10  # all 10 checks present
        assert all(isinstance(v, bool) for v in result.checks.values())


# ---------------------------------------------------------------------------
# 2 & 3. Halt conditions
# ---------------------------------------------------------------------------

class TestHaltConditions:

    @pytest.mark.asyncio
    async def test_soft_halt_blocks_signals(self, state):
        from agents.geto.skills.trade_validator import validate

        await _set_session_env(state)
        await state.set_system_flag("halt_flag", True)

        signal = _make_signal()
        result = await validate(signal, state, current_session="NY_OVERLAP", current_spread=1.5)

        assert result.approved is False
        assert result.fail_reason == "not_halted"

    @pytest.mark.asyncio
    async def test_emergency_halt_blocks_signals(self, state):
        from agents.geto.skills.trade_validator import validate

        await _set_session_env(state)
        await state.set_system_flag("emergency_halt_flag", True)

        signal = _make_signal()
        result = await validate(signal, state, current_session="NY_OVERLAP", current_spread=1.5)

        assert result.approved is False
        assert result.fail_reason == "not_halted"

    @pytest.mark.asyncio
    async def test_3_consecutive_losses_triggers_soft_halt_check(self, state):
        from agents.geto.skills.trade_validator import validate

        await _set_session_env(state)
        await state.set_trading_state("consecutive_losses", "3")

        signal = _make_signal()
        result = await validate(signal, state, current_session="NY_OVERLAP", current_spread=1.5)

        assert result.approved is False
        assert result.fail_reason == "consecutive_losses_ok"

    @pytest.mark.asyncio
    async def test_emergency_halt_not_cleared_by_halt_flag_reset(self, state):
        from agents.geto.skills.trade_validator import validate

        await _set_session_env(state)
        await state.set_system_flag("emergency_halt_flag", True)
        await state.set_system_flag("halt_flag", False)  # simulate /override clearing halt

        signal = _make_signal()
        result = await validate(signal, state, current_session="NY_OVERLAP", current_spread=1.5)

        assert result.approved is False  # emergency halt still active

    @pytest.mark.asyncio
    async def test_override_clears_halt_signals_pass(self, state):
        from agents.geto.skills.trade_validator import validate

        await _set_session_env(state)
        await state.set_system_flag("halt_flag", True)
        await state.set_trading_state("consecutive_losses", "3")

        # /override: clear halt, reset losses
        await state.set_system_flag("halt_flag", False)
        await state.reset_consecutive_losses()

        signal = _make_signal()
        result = await validate(signal, state, current_session="NY_OVERLAP", current_spread=1.5)

        assert result.approved is True

    @pytest.mark.asyncio
    async def test_50pct_drawdown_blocks(self, state):
        from agents.geto.skills.trade_validator import validate

        await _set_session_env(state)
        await state.update_account(current_dd_pct=52.0)

        signal = _make_signal()
        result = await validate(signal, state, current_session="NY_OVERLAP", current_spread=1.5)

        assert result.approved is False
        assert result.fail_reason == "drawdown_ok"

    @pytest.mark.asyncio
    async def test_pause_flag_blocks(self, state):
        from agents.geto.skills.trade_validator import validate

        await _set_session_env(state)
        await state.set_system_flag("pause_flag", True)

        signal = _make_signal()
        result = await validate(signal, state, current_session="NY_OVERLAP", current_spread=1.5)

        assert result.approved is False
        assert result.fail_reason == "not_paused"

    @pytest.mark.asyncio
    async def test_resume_clears_pause(self, state):
        from agents.geto.skills.trade_validator import validate

        await _set_session_env(state)
        await state.set_system_flag("pause_flag", True)
        await state.set_system_flag("pause_flag", False)

        signal = _make_signal()
        result = await validate(signal, state, current_session="NY_OVERLAP", current_spread=1.5)

        assert result.approved is True


# ---------------------------------------------------------------------------
# 4 & 5. Session limits
# ---------------------------------------------------------------------------

class TestSessionLimits:

    @pytest.mark.asyncio
    async def test_model_a_limit_8_per_session(self, state):
        """After 8 OU_GRIND trades in NY_OVERLAP, the 9th is blocked."""
        from agents.geto.skills.trade_validator import validate
        from core.constants import MODEL_A

        await _set_session_env(state)

        # Simulate 8 completed trades in this session
        for _ in range(8):
            await state.increment_session_trade_count("NY_OVERLAP", MODEL_A)

        signal = _make_signal("OU_GRIND", "BUY", "NY_OVERLAP")
        result = await validate(signal, state, current_session="NY_OVERLAP", current_spread=1.5)

        assert result.approved is False
        assert result.fail_reason == "session_trades_within_limit"

    @pytest.mark.asyncio
    async def test_model_a_limit_7_still_allowed(self, state):
        from agents.geto.skills.trade_validator import validate
        from core.constants import MODEL_A

        await _set_session_env(state)
        for _ in range(7):
            await state.increment_session_trade_count("NY_OVERLAP", MODEL_A)

        signal = _make_signal("OU_GRIND", "BUY", "NY_OVERLAP")
        result = await validate(signal, state, current_session="NY_OVERLAP", current_spread=1.5)

        assert result.approved is True

    @pytest.mark.asyncio
    async def test_model_b_limit_2_per_session(self, state):
        """After 2 LONDON_REVERSAL trades in LONDON_OPEN, the 3rd is blocked."""
        from agents.geto.skills.trade_validator import validate
        from core.constants import MODEL_B

        await _set_session_env(state, session="LONDON_OPEN", regime="BULLISH_GRIND")
        for _ in range(2):
            await state.increment_session_trade_count("LONDON_OPEN", MODEL_B)

        signal = _make_signal("LONDON_REVERSAL", "BUY", "LONDON_OPEN")
        result = await validate(signal, state, current_session="LONDON_OPEN", current_spread=1.5)

        assert result.approved is False
        assert result.fail_reason == "session_trades_within_limit"

    @pytest.mark.asyncio
    async def test_model_b_limit_1_still_allowed(self, state):
        from agents.geto.skills.trade_validator import validate
        from core.constants import MODEL_B

        await _set_session_env(state, session="LONDON_OPEN", regime="BULLISH_GRIND")
        await state.increment_session_trade_count("LONDON_OPEN", MODEL_B)

        signal = _make_signal("LONDON_REVERSAL", "BUY", "LONDON_OPEN")
        result = await validate(signal, state, current_session="LONDON_OPEN", current_spread=1.5)

        assert result.approved is True

    @pytest.mark.asyncio
    async def test_session_counts_independent_per_model(self, state):
        """Model A at limit does not affect Model B count."""
        from agents.geto.skills.trade_validator import validate
        from core.constants import MODEL_A, MODEL_B

        await _set_session_env(state, session="LONDON_OPEN", regime="BULLISH_GRIND")
        for _ in range(8):
            await state.increment_session_trade_count("LONDON_OPEN", MODEL_A)

        signal = _make_signal("LONDON_REVERSAL", "BUY", "LONDON_OPEN")
        result = await validate(signal, state, current_session="LONDON_OPEN", current_spread=1.5)

        assert result.approved is True


# ---------------------------------------------------------------------------
# 6. Alert queue lifecycle
# ---------------------------------------------------------------------------

class TestAlertQueue:

    @pytest.mark.asyncio
    async def test_push_and_retrieve(self, state):
        await state.push_alert("TRADE_OPENED", "BUY at 2500")
        alerts = await state.get_pending_alerts()
        assert len(alerts) == 1
        assert alerts[0]["alert_type"] == "TRADE_OPENED"
        assert alerts[0]["sent"] == 0

    @pytest.mark.asyncio
    async def test_mark_sent(self, state):
        await state.push_alert("SOFT_HALT", "3 losses")
        alerts = await state.get_pending_alerts()
        await state.mark_alert_sent(alerts[0]["id"])
        pending = await state.get_pending_alerts()
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_multiple_alerts_ordered(self, state):
        await state.push_alert("A", "first")
        await state.push_alert("B", "second")
        await state.push_alert("C", "third")
        alerts = await state.get_pending_alerts()
        types = [a["alert_type"] for a in alerts]
        assert types == ["A", "B", "C"]

    @pytest.mark.asyncio
    async def test_sent_alerts_not_returned(self, state):
        await state.push_alert("X", "msg")
        alerts = await state.get_pending_alerts()
        await state.mark_alert_sent(alerts[0]["id"])
        await state.push_alert("Y", "msg2")
        pending = await state.get_pending_alerts()
        assert len(pending) == 1
        assert pending[0]["alert_type"] == "Y"


# ---------------------------------------------------------------------------
# 7. Post-trade state updates
# ---------------------------------------------------------------------------

class TestPostTradeState:

    @pytest.mark.asyncio
    async def test_win_resets_consecutive_losses(self, state):
        from agents.toji.skills.state_updater import post_trade_update

        await state.set_trading_state("consecutive_losses", "2")
        await post_trade_update(
            state, result="WIN", pnl=12.0,
            balance_before=200.0, open_time=None,
            session="NY_OVERLAP", model="OU_GRIND",
        )
        assert await state.get_consecutive_losses() == 0

    @pytest.mark.asyncio
    async def test_loss_increments_consecutive_losses(self, state):
        from agents.toji.skills.state_updater import post_trade_update

        await state.set_trading_state("consecutive_losses", "1")
        await post_trade_update(
            state, result="LOSS", pnl=-6.0,
            balance_before=200.0, open_time=None,
            session="NY_OVERLAP", model="OU_GRIND",
        )
        assert await state.get_consecutive_losses() == 2

    @pytest.mark.asyncio
    async def test_balance_updates_after_win(self, state):
        from agents.toji.skills.state_updater import post_trade_update

        balance_after, dd, _ = await post_trade_update(
            state, result="WIN", pnl=12.0,
            balance_before=200.0, open_time=None,
            session="NY_OVERLAP", model="OU_GRIND",
        )
        assert balance_after == 212.0
        assert dd == 0.0  # new peak, no drawdown

    @pytest.mark.asyncio
    async def test_balance_and_drawdown_after_loss(self, state):
        from agents.toji.skills.state_updater import post_trade_update

        # First establish a peak
        await state.update_account(peak_balance=200.0)
        balance_after, dd, _ = await post_trade_update(
            state, result="LOSS", pnl=-40.0,
            balance_before=200.0, open_time=None,
            session="NY_OVERLAP", model="OU_GRIND",
        )
        assert balance_after == 160.0
        assert abs(dd - 20.0) < 0.01  # (200-160)/200 = 20%

    @pytest.mark.asyncio
    async def test_session_trade_count_incremented(self, state):
        from agents.toji.skills.state_updater import post_trade_update
        from core.constants import MODEL_A

        await post_trade_update(
            state, result="WIN", pnl=12.0,
            balance_before=200.0, open_time=None,
            session="NY_OVERLAP", model=MODEL_A,
        )
        count = await state.get_session_trade_count("NY_OVERLAP", MODEL_A)
        assert count == 1

    @pytest.mark.asyncio
    async def test_total_trades_today_incremented(self, state):
        from agents.toji.skills.state_updater import post_trade_update

        for _ in range(3):
            await post_trade_update(
                state, result="WIN", pnl=5.0,
                balance_before=200.0, open_time=None,
                session="NY_OVERLAP", model="OU_GRIND",
            )
        total = await state.get_trading_state("total_trades_today")
        assert int(total) == 3

    @pytest.mark.asyncio
    async def test_duration_calculation(self, state):
        from agents.toji.skills.state_updater import post_trade_update

        open_time = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
        _, _, duration = await post_trade_update(
            state, result="WIN", pnl=5.0,
            balance_before=200.0, open_time=open_time,
            session="NY_OVERLAP", model="OU_GRIND",
        )
        # Should be ~45 minutes (allow ±2 min for execution time)
        assert 43.0 <= duration <= 47.0


# ---------------------------------------------------------------------------
# 8. Trade log lifecycle
# ---------------------------------------------------------------------------

class TestTradeLog:

    @pytest.mark.asyncio
    async def test_log_trade_open_and_close(self, state):
        trade_id = await state.log_trade(
            model="OU_GRIND", direction="BUY",
            entry_price=2500.0, sl_price=2494.0, tp_price=2512.0,
            lot_size=0.5, sl_distance=6.0, risk_amount=40.0,
            balance_before=200.0,
        )
        assert trade_id > 0

        # Open trade has no result
        open_trades = await state.get_open_trades()
        assert len(open_trades) == 1
        assert open_trades[0]["result"] is None

        # Close it
        await state.update_trade(
            trade_id, result="WIN", exit_price=2512.0, pnl=6.0,
            balance_after=206.0, duration_mins=45.0,
        )

        open_after = await state.get_open_trades()
        assert len(open_after) == 0

    @pytest.mark.asyncio
    async def test_get_trades_by_period(self, state):
        await state.log_trade(
            model="OU_GRIND", direction="SELL",
            entry_price=2500.0, sl_price=2506.0, tp_price=2488.0,
            lot_size=0.5, sl_distance=6.0, risk_amount=40.0,
            balance_before=200.0,
            result="LOSS", exit_price=2506.0, pnl=-3.0,
            date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        )
        trades = await state.get_trades_by_period(days=7)
        assert len(trades) == 1

    @pytest.mark.asyncio
    async def test_multiple_open_trades_tracked(self, state):
        for i in range(3):
            await state.log_trade(
                model="OU_GRIND", direction="BUY",
                entry_price=2500.0 + i, sl_price=2494.0, tp_price=2512.0,
                lot_size=0.5, sl_distance=6.0, risk_amount=40.0,
                balance_before=200.0,
            )
        open_trades = await state.get_open_trades()
        assert len(open_trades) == 3


# ---------------------------------------------------------------------------
# 9. Full signal → validation → state lifecycle
# ---------------------------------------------------------------------------

class TestFullLifecycle:

    @pytest.mark.asyncio
    async def test_full_trade_cycle_win(self, state):
        """PENDING signal → GETO approves → TOJI logs open → closes WIN → state updated."""
        import json
        from agents.geto.skills.trade_validator import validate
        from agents.toji.skills.state_updater import post_trade_update

        await _set_session_env(state)

        # 1. NANAMI writes signal
        signal = _make_signal()
        signal["timestamp"] = datetime.now(timezone.utc).isoformat()
        await state.set_trading_state("last_signal", json.dumps(signal))
        await state.set_trading_state("last_risk_decision", "")

        # 2. GETO validates
        result = await validate(signal, state, current_session="NY_OVERLAP", current_spread=1.5)
        assert result.approved is True
        await state.set_trading_state("last_risk_decision", "APPROVED")

        # 3. TOJI places order (paper) — log open
        trade_id = await state.log_trade(
            model=signal["model"], direction=signal["direction"],
            entry_price=signal["entry_price"], sl_price=signal["sl_price"],
            tp_price=signal["tp_price"], lot_size=0.5,
            sl_distance=signal["sl_distance"], risk_amount=40.0,
            balance_before=200.0,
        )

        # 4. TOJI monitors — TP hit → close
        open_time = datetime.now(timezone.utc).isoformat()
        await state.update_trade(trade_id, result="WIN", exit_price=2512.0,
                                  pnl=6.0, balance_after=206.0)

        balance_after, dd, _ = await post_trade_update(
            state, result="WIN", pnl=6.0,
            balance_before=200.0, open_time=open_time,
            session="NY_OVERLAP", model="OU_GRIND",
        )

        # 5. Verify final state
        assert balance_after == 206.0
        assert await state.get_consecutive_losses() == 0
        decision = await state.get_trading_state("last_risk_decision")
        assert decision == "APPROVED"

    @pytest.mark.asyncio
    async def test_3_losses_then_halt_then_override(self, state):
        """3 consecutive LOSS trades → halt check → /override clears → signals pass."""
        import json
        from agents.geto.skills.trade_validator import validate
        from agents.toji.skills.state_updater import post_trade_update

        await _set_session_env(state)
        open_time = datetime.now(timezone.utc).isoformat()

        # Simulate 3 losses via state_updater
        for _ in range(3):
            await post_trade_update(
                state, result="LOSS", pnl=-6.0,
                balance_before=200.0, open_time=open_time,
                session="NY_OVERLAP", model="OU_GRIND",
            )

        assert await state.get_consecutive_losses() == 3

        # GETO detects and sets halt_flag
        await state.set_system_flag("halt_flag", True)

        # Signal blocked
        signal = _make_signal()
        r = await validate(signal, state, current_session="NY_OVERLAP", current_spread=1.5)
        assert r.approved is False

        # /override: clear halt + reset losses
        await state.set_system_flag("halt_flag", False)
        await state.reset_consecutive_losses()

        # Now passes
        r2 = await validate(signal, state, current_session="NY_OVERLAP", current_spread=1.5)
        assert r2.approved is True
