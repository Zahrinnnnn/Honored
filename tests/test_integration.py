"""
test_integration.py — Signal pipeline integration tests.

Uses a real SQLite database (tmp_path temp file, not mocks) to test the
complete state machine across GETO validation, TOJI trade execution,
halt conditions, and the alert queue.

Every test creates a fresh DB and calls agent functions directly
(no asyncio loop spinning, no MetaApi, no network).

Coverage
────────
- Signal validation with a real StateManager (all 11 checks)
- Model/regime exclusivity (TRENDING ↔ Model A, RANGING ↔ Model B)
- Session count limits (per-session and daily)
- Full trade lifecycle: log_trade_open → post_trade_update → log_trade_close
- Consecutive loss counter: increment, double-increment, reset on win
- Halt condition monitoring: soft halt + emergency halt trigger logic
- Halt duplicate suppression (already-halted guard)
- Alert queue: pushed on halt, marked sent, cleared correctly
- Override flow: halt → clear flags → signal approved again
- Emergency halt NOT cleared by override alone
- Signal status written to DB (APPROVED / REJECTED) as GETO does it

Run: python -m pytest tests/test_integration.py -v
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.state_manager import StateManager
from core.constants import MODEL_A, MODEL_B, MODEL_C

from agents.geto.skills.trade_validator import validate
from agents.geto.agent import _monitor_halt_conditions
from agents.toji.skills.trade_logger import log_trade_open, log_trade_close
from agents.toji.skills.state_updater import post_trade_update
from agents.toji.skills.trade_monitor import check_exit, calculate_pnl
from core.constants import XAUUSD_POINT_VALUE


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _db(tmp_path) -> str:
    return str(tmp_path / "integration.db")


async def _setup(db_path: str, balance: float = 20.0) -> StateManager:
    """Connect StateManager and seed default state."""
    sm = StateManager(db_path=db_path)
    await sm.connect()
    await sm.update_account(
        balance        = balance,
        equity         = balance,
        peak_balance   = balance,
        current_dd_pct = 0.0,
        open_positions = 0,
    )
    await sm.set_session_info("current_spread",       "1.5")
    await sm.set_session_info("minutes_to_next_news", "120")
    return sm


def _signal(
    model       = MODEL_A,
    direction   = "BUY",
    regime      = "TRENDING",
    session     = "LONDON_OPEN",
    entry_price = 2345.0,
    sl_price    = 2340.0,
    tp_price    = 2360.0,
    sl_distance = 5.0,
    status      = "PENDING",
) -> dict:
    return {
        "id":          "test-001",
        "model":       model,
        "direction":   direction,
        "regime":      regime,
        "session":     session,
        "entry_price": entry_price,
        "sl_price":    sl_price,
        "tp_price":    tp_price,
        "sl_distance": sl_distance,
        "status":      status,
        "reason":      "Integration test signal",
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }


async def _val(sm, sig, session="LONDON_OPEN", breakout=False, spread=1.5):
    """Thin wrapper around validate() with sensible defaults."""
    return await validate(
        signal             = sig,
        state              = sm,
        current_session    = session,
        is_breakout_window = breakout,
        current_spread     = spread,
    )


# ---------------------------------------------------------------------------
# 1. Signal validation — real SQLite reads
# ---------------------------------------------------------------------------

class TestSignalValidation:

    def test_clean_signal_approved(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                return await _val(sm, _signal())
            finally:
                await sm.close()
        r = asyncio.run(run())
        assert r.approved
        assert r.fail_reason == ""

    def test_all_11_checks_present(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                return await _val(sm, _signal())
            finally:
                await sm.close()
        r = asyncio.run(run())
        assert len(r.checks) == 11

    def test_rejected_when_paused(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                await sm.set_system_flag("pause_flag", True)
                return await _val(sm, _signal())
            finally:
                await sm.close()
        r = asyncio.run(run())
        assert not r.approved
        assert r.fail_reason == "not_paused"

    def test_rejected_when_halt_flag(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                await sm.set_system_flag("halt_flag", True)
                return await _val(sm, _signal())
            finally:
                await sm.close()
        r = asyncio.run(run())
        assert not r.approved
        assert r.fail_reason == "not_halted"

    def test_rejected_when_emergency_halt(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                await sm.set_system_flag("emergency_halt_flag", True)
                return await _val(sm, _signal())
            finally:
                await sm.close()
        r = asyncio.run(run())
        assert not r.approved
        assert r.fail_reason == "not_halted"

    def test_rejected_on_3_consecutive_losses(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                await sm.set_trading_state("consecutive_losses", "3")
                return await _val(sm, _signal())
            finally:
                await sm.close()
        r = asyncio.run(run())
        assert not r.approved
        assert r.fail_reason == "consecutive_losses_ok"

    def test_rejected_on_50pct_drawdown(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                await sm.update_account(current_dd_pct=50.0)
                return await _val(sm, _signal())
            finally:
                await sm.close()
        r = asyncio.run(run())
        assert not r.approved
        assert r.fail_reason == "drawdown_ok"

    def test_rejected_on_max_open_trades(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                await sm.update_account(open_positions=2)   # MAX_OPEN_TRADES = 2
                return await _val(sm, _signal())
            finally:
                await sm.close()
        r = asyncio.run(run())
        assert not r.approved
        assert r.fail_reason == "open_trades_ok"

    def test_rejected_in_news_blackout(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                await sm.set_session_info("minutes_to_next_news", "15")
                return await _val(sm, _signal())
            finally:
                await sm.close()
        r = asyncio.run(run())
        assert not r.approved
        assert r.fail_reason == "news_clear"

    def test_rejected_on_wide_spread(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                return await _val(sm, _signal(), spread=5.0)   # > $4.00
            finally:
                await sm.close()
        r = asyncio.run(run())
        assert not r.approved
        assert r.fail_reason == "spread_acceptable"

    def test_rejected_outside_active_session(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                return await _val(sm, _signal(), session=None)
            finally:
                await sm.close()
        r = asyncio.run(run())
        assert not r.approved
        assert r.fail_reason == "session_valid"


# ---------------------------------------------------------------------------
# 2. Model / regime exclusivity
# ---------------------------------------------------------------------------

class TestModelExclusivity:

    def test_model_a_rejected_in_ranging(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                return await _val(sm, _signal(model=MODEL_A, regime="RANGING"))
            finally:
                await sm.close()
        r = asyncio.run(run())
        assert not r.approved
        assert r.fail_reason == "regime_matches_model"

    def test_model_b_rejected_in_trending(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                sig = _signal(model=MODEL_B, regime="TRENDING", session="LONDON_OPEN")
                return await _val(sm, sig)
            finally:
                await sm.close()
        r = asyncio.run(run())
        assert not r.approved
        assert r.fail_reason == "regime_matches_model"

    def test_model_c_approved_regardless_of_regime(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                sig = _signal(model=MODEL_C, regime="TRENDING", session="LONDON_BREAKOUT")
                return await _val(sm, sig, session="LONDON_BREAKOUT", breakout=True)
            finally:
                await sm.close()
        r = asyncio.run(run())
        assert r.approved   # Model C fires regardless of regime

    def test_model_a_blocked_in_breakout_window(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                sig = _signal(model=MODEL_A, regime="TRENDING", session="LONDON_OPEN")
                return await _val(sm, sig, session="LONDON_OPEN", breakout=True)
            finally:
                await sm.close()
        r = asyncio.run(run())
        assert not r.approved
        assert r.fail_reason == "model_priority_ok"


# ---------------------------------------------------------------------------
# 3. Session count limits
# ---------------------------------------------------------------------------

class TestSessionCountLimits:

    def test_model_a_blocked_at_session_limit(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                # M5_MAX_TRADES_PER_SESSION = 3
                for _ in range(3):
                    await sm.increment_session_trade_count("LONDON_OPEN", MODEL_A)
                return await _val(sm, _signal(model=MODEL_A))
            finally:
                await sm.close()
        r = asyncio.run(run())
        assert not r.approved
        assert r.fail_reason == "session_trades_within_limit"

    def test_model_a_allowed_below_limit(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                # 2 trades (limit is 3 → still under)
                for _ in range(2):
                    await sm.increment_session_trade_count("LONDON_OPEN", MODEL_A)
                return await _val(sm, _signal(model=MODEL_A))
            finally:
                await sm.close()
        r = asyncio.run(run())
        assert r.approved

    def test_model_c_blocked_after_daily_limit(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                # BREAKOUT_MAX_TRADES_PER_DAY = 1 — uses LONDON_BREAKOUT key
                await sm.increment_session_trade_count("LONDON_BREAKOUT", MODEL_C)
                sig = _signal(model=MODEL_C, regime="TRENDING", session="LONDON_BREAKOUT")
                return await _val(sm, sig, session="LONDON_BREAKOUT", breakout=True)
            finally:
                await sm.close()
        r = asyncio.run(run())
        assert not r.approved
        assert r.fail_reason == "session_trades_within_limit"


# ---------------------------------------------------------------------------
# 4. Trade lifecycle — open → state update → close
# ---------------------------------------------------------------------------

class TestTradeLifecycle:

    def test_trade_logged_open_with_result_null(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                sig = _signal()
                order = {"entry_price": 2345.0, "order_id": "PAPER-abc"}
                trade_id = await log_trade_open(sm, sig, lot_size=0.04, order_result=order)
                open_trades = await sm.get_open_trades()
                return trade_id, open_trades
            finally:
                await sm.close()
        trade_id, open_trades = asyncio.run(run())
        assert trade_id > 0
        assert len(open_trades) == 1
        assert open_trades[0]["result"] is None
        assert open_trades[0]["entry_price"] == 2345.0
        assert open_trades[0]["model"] == MODEL_A

    def test_trade_closed_win_updates_balance(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                sig = _signal()
                order = {"entry_price": 2345.0, "order_id": "PAPER-win"}
                trade_id = await log_trade_open(sm, sig, lot_size=0.04, order_result=order)
                await sm.update_account(open_positions=1)

                pnl = 0.60
                bal_after, dd, dur = await post_trade_update(
                    state=sm, result="WIN", pnl=pnl,
                    balance_before=20.0, open_time=None,
                    session="LONDON_OPEN", model=MODEL_A,
                )
                await log_trade_close(
                    state=sm, trade_id=trade_id, result="WIN",
                    exit_price=2360.0, pnl=pnl,
                    balance_after=bal_after, drawdown_pct=dd, duration_mins=dur,
                )
                open_trades = await sm.get_open_trades()
                closed      = await sm.get_trades(limit=1)
                account     = await sm.get_account()
                return open_trades, closed, account
            finally:
                await sm.close()
        open_trades, closed, account = asyncio.run(run())
        assert len(open_trades) == 0
        assert closed[0]["result"] == "WIN"
        assert closed[0]["exit_price"] == 2360.0
        assert account["balance"] == pytest.approx(20.60, abs=0.01)

    def test_trade_closed_loss_reduces_balance(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                sig = _signal()
                order = {"entry_price": 2345.0, "order_id": "PAPER-loss"}
                trade_id = await log_trade_open(sm, sig, lot_size=0.04, order_result=order)
                await sm.update_account(open_positions=1)

                pnl = -0.20
                bal_after, dd, dur = await post_trade_update(
                    state=sm, result="LOSS", pnl=pnl,
                    balance_before=20.0, open_time=None,
                    session="LONDON_OPEN", model=MODEL_A,
                )
                await log_trade_close(
                    state=sm, trade_id=trade_id, result="LOSS",
                    exit_price=2340.0, pnl=pnl,
                    balance_after=bal_after, drawdown_pct=dd, duration_mins=dur,
                )
                closed  = await sm.get_trades(limit=1)
                account = await sm.get_account()
                return closed, account
            finally:
                await sm.close()
        closed, account = asyncio.run(run())
        assert closed[0]["result"] == "LOSS"
        assert account["balance"] == pytest.approx(19.80, abs=0.01)

    def test_open_positions_decremented_after_close(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                await sm.update_account(open_positions=1)
                await post_trade_update(
                    state=sm, result="WIN", pnl=1.00,
                    balance_before=20.0, open_time=None,
                    session="LONDON_OPEN", model=MODEL_A,
                )
                return await sm.get_account()
            finally:
                await sm.close()
        account = asyncio.run(run())
        assert account["open_positions"] == 0

    def test_session_count_incremented_after_close(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                await sm.update_account(open_positions=1)
                await post_trade_update(
                    state=sm, result="WIN", pnl=1.00,
                    balance_before=20.0, open_time=None,
                    session="LONDON_OPEN", model=MODEL_A,
                )
                return await sm.get_session_trade_count("LONDON_OPEN", MODEL_A)
            finally:
                await sm.close()
        count = asyncio.run(run())
        assert count == 1


# ---------------------------------------------------------------------------
# 5. Consecutive loss tracking
# ---------------------------------------------------------------------------

class TestConsecutiveLossTracking:

    def test_loss_increments_counter(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                await sm.update_account(open_positions=1)
                await post_trade_update(
                    state=sm, result="LOSS", pnl=-0.50,
                    balance_before=20.0, open_time=None,
                    session="LONDON_OPEN", model=MODEL_A,
                )
                return await sm.get_consecutive_losses()
            finally:
                await sm.close()
        assert asyncio.run(run()) == 1

    def test_two_losses_increments_to_two(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                for balance_before in [20.0, 19.5]:
                    await sm.update_account(open_positions=1)
                    await post_trade_update(
                        state=sm, result="LOSS", pnl=-0.50,
                        balance_before=balance_before, open_time=None,
                        session="LONDON_OPEN", model=MODEL_A,
                    )
                return await sm.get_consecutive_losses()
            finally:
                await sm.close()
        assert asyncio.run(run()) == 2

    def test_win_resets_counter(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                await sm.set_trading_state("consecutive_losses", "2")
                await sm.update_account(open_positions=1)
                await post_trade_update(
                    state=sm, result="WIN", pnl=1.50,
                    balance_before=19.0, open_time=None,
                    session="LONDON_OPEN", model=MODEL_A,
                )
                return await sm.get_consecutive_losses()
            finally:
                await sm.close()
        assert asyncio.run(run()) == 0

    def test_three_losses_blocks_next_signal(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                await sm.set_trading_state("consecutive_losses", "3")
                return await _val(sm, _signal())
            finally:
                await sm.close()
        r = asyncio.run(run())
        assert not r.approved
        assert r.fail_reason == "consecutive_losses_ok"


# ---------------------------------------------------------------------------
# 6. Halt condition monitoring
# ---------------------------------------------------------------------------

class TestHaltConditions:

    def test_soft_halt_triggered_on_3_losses(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                await sm.set_trading_state("consecutive_losses", "3")
                await _monitor_halt_conditions(sm)
                return (
                    await sm.get_system_flag("halt_flag"),
                    await sm.get_system_flag("emergency_halt_flag"),
                )
            finally:
                await sm.close()
        halt, emergency = asyncio.run(run())
        assert halt is True
        assert emergency is False

    def test_emergency_halt_triggered_on_50pct_drawdown(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                await sm.update_account(current_dd_pct=50.0)
                await _monitor_halt_conditions(sm)
                return (
                    await sm.get_system_flag("halt_flag"),
                    await sm.get_system_flag("emergency_halt_flag"),
                )
            finally:
                await sm.close()
        halt, emergency = asyncio.run(run())
        assert halt is True
        assert emergency is True

    def test_no_halt_below_thresholds(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                await sm.set_trading_state("consecutive_losses", "2")
                await sm.update_account(current_dd_pct=30.0)
                await _monitor_halt_conditions(sm)
                return await sm.get_system_flag("halt_flag")
            finally:
                await sm.close()
        assert asyncio.run(run()) is False

    def test_duplicate_halt_not_triggered(self, tmp_path):
        """Already-halted system: monitor runs twice → 0 duplicate SOFT_HALT alerts."""
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                await sm.set_system_flag("halt_flag", True)
                await sm.set_trading_state("consecutive_losses", "3")
                await _monitor_halt_conditions(sm)
                await _monitor_halt_conditions(sm)
                return await sm.get_pending_alerts()
            finally:
                await sm.close()
        alerts = asyncio.run(run())
        soft_halts = [a for a in alerts if a["alert_type"] == "SOFT_HALT"]
        assert len(soft_halts) == 0   # already_halted guard suppresses both


# ---------------------------------------------------------------------------
# 7. Alert queue
# ---------------------------------------------------------------------------

class TestAlertQueue:

    def test_soft_halt_pushes_alert(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                await sm.set_trading_state("consecutive_losses", "3")
                await _monitor_halt_conditions(sm)
                return await sm.get_pending_alerts()
            finally:
                await sm.close()
        alerts = asyncio.run(run())
        soft = [a for a in alerts if a["alert_type"] == "SOFT_HALT"]
        assert len(soft) == 1
        assert "consecutive" in soft[0]["message"].lower()

    def test_emergency_halt_pushes_alert(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                await sm.update_account(current_dd_pct=50.0)
                await _monitor_halt_conditions(sm)
                return await sm.get_pending_alerts()
            finally:
                await sm.close()
        alerts = asyncio.run(run())
        emergency = [a for a in alerts if a["alert_type"] == "EMERGENCY_HALT"]
        assert len(emergency) == 1

    def test_alert_marked_sent(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                await sm.push_alert("TEST", "test message")
                pending = await sm.get_pending_alerts()
                await sm.mark_alert_sent(pending[0]["id"])
                return await sm.get_pending_alerts()
            finally:
                await sm.close()
        assert asyncio.run(run()) == []


# ---------------------------------------------------------------------------
# 8. Override flow
# ---------------------------------------------------------------------------

class TestOverrideFlow:

    def test_halted_signal_rejected(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                await sm.set_system_flag("halt_flag", True)
                return await _val(sm, _signal())
            finally:
                await sm.close()
        r = asyncio.run(run())
        assert not r.approved

    def test_override_clears_halt_and_approves(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                # Halt via soft halt
                await sm.set_system_flag("halt_flag", True)
                await sm.set_trading_state("consecutive_losses", "3")
                # User override: clear flag + reset counter
                await sm.set_system_flag("halt_flag", False)
                await sm.reset_consecutive_losses()
                return await _val(sm, _signal())
            finally:
                await sm.close()
        r = asyncio.run(run())
        assert r.approved

    def test_emergency_halt_not_cleared_by_halt_flag_alone(self, tmp_path):
        """emergency_halt_flag remains → not_halted still fails after clearing halt_flag."""
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                await sm.set_system_flag("emergency_halt_flag", True)
                await sm.set_system_flag("halt_flag", True)
                # Override only clears halt_flag (emergency requires manual intervention)
                await sm.set_system_flag("halt_flag", False)
                await sm.reset_consecutive_losses()
                return await _val(sm, _signal())
            finally:
                await sm.close()
        r = asyncio.run(run())
        assert not r.approved
        assert r.fail_reason == "not_halted"


# ---------------------------------------------------------------------------
# 9. Signal status written to DB (as GETO agent.py does it)
# ---------------------------------------------------------------------------

class TestSignalStatusFlow:

    def test_approved_decision_written_to_db(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                sig = _signal()
                result = await _val(sm, sig)
                if result.approved:
                    sig["status"] = "APPROVED"
                    await sm.set_trading_state("last_risk_decision", "APPROVED")
                    await sm.set_trading_state("last_signal", json.dumps(sig))
                decision   = await sm.get_trading_state("last_risk_decision")
                last_raw   = await sm.get_trading_state("last_signal")
                last_sig   = json.loads(last_raw)
                return decision, last_sig["status"]
            finally:
                await sm.close()
        decision, status = asyncio.run(run())
        assert decision == "APPROVED"
        assert status   == "APPROVED"

    def test_rejected_decision_written_to_db(self, tmp_path):
        async def run():
            sm = await _setup(_db(tmp_path))
            try:
                sig = _signal()
                await sm.set_system_flag("halt_flag", True)
                result = await _val(sm, sig)
                if not result.approved:
                    sig["status"] = "REJECTED"
                    decision = f"REJECTED:{result.fail_reason}"
                    await sm.set_trading_state("last_risk_decision", decision)
                    await sm.set_trading_state("last_signal", json.dumps(sig))
                decision = await sm.get_trading_state("last_risk_decision")
                last_sig = json.loads(await sm.get_trading_state("last_signal"))
                return decision, last_sig["status"]
            finally:
                await sm.close()
        decision, status = asyncio.run(run())
        assert decision.startswith("REJECTED:")
        assert status == "REJECTED"


# ---------------------------------------------------------------------------
# 10. Trade monitor helpers (pure functions — no DB needed)
# ---------------------------------------------------------------------------

class TestTradeMonitorHelpers:

    def test_check_exit_buy_sl(self):
        trade = {"direction": "BUY", "sl_price": 2340.0, "tp_price": 2360.0,
                 "entry_price": 2345.0, "lot_size": 0.04}
        assert check_exit(trade, 2339.0, 2340.0) == "LOSS"

    def test_check_exit_buy_tp(self):
        trade = {"direction": "BUY", "sl_price": 2340.0, "tp_price": 2360.0,
                 "entry_price": 2345.0, "lot_size": 0.04}
        assert check_exit(trade, 2361.0, 2362.0) == "WIN"

    def test_check_exit_sell_sl(self):
        trade = {"direction": "SELL", "sl_price": 2360.0, "tp_price": 2330.0,
                 "entry_price": 2350.0, "lot_size": 0.04}
        assert check_exit(trade, 2359.0, 2361.0) == "LOSS"

    def test_check_exit_sell_tp(self):
        trade = {"direction": "SELL", "sl_price": 2360.0, "tp_price": 2330.0,
                 "entry_price": 2350.0, "lot_size": 0.04}
        assert check_exit(trade, 2329.0, 2330.0) == "WIN"

    def test_check_exit_no_hit(self):
        trade = {"direction": "BUY", "sl_price": 2340.0, "tp_price": 2360.0,
                 "entry_price": 2345.0, "lot_size": 0.04}
        assert check_exit(trade, 2348.0, 2349.0) is None

    def test_calculate_pnl_buy_win(self):
        # BUY: price_diff = exit - entry = +15; pnl = 0.04 × 15 × POINT_VALUE
        trade = {"direction": "BUY", "lot_size": 0.04, "entry_price": 2345.0}
        pnl = calculate_pnl(trade, exit_price=2360.0)
        expected = round(0.04 * 15.0 * XAUUSD_POINT_VALUE, 2)
        assert pnl == pytest.approx(expected, abs=0.01)
        assert pnl > 0

    def test_calculate_pnl_buy_loss(self):
        # BUY: price_diff = exit - entry = -5; pnl = 0.04 × (-5) × POINT_VALUE
        trade = {"direction": "BUY", "lot_size": 0.04, "entry_price": 2345.0}
        pnl = calculate_pnl(trade, exit_price=2340.0)
        expected = round(0.04 * (-5.0) * XAUUSD_POINT_VALUE, 2)
        assert pnl == pytest.approx(expected, abs=0.01)
        assert pnl < 0

    def test_calculate_pnl_sell_win(self):
        # SELL: price_diff = entry - exit = +20; pnl = 0.04 × 20 × POINT_VALUE
        trade = {"direction": "SELL", "lot_size": 0.04, "entry_price": 2350.0}
        pnl = calculate_pnl(trade, exit_price=2330.0)
        expected = round(0.04 * 20.0 * XAUUSD_POINT_VALUE, 2)
        assert pnl == pytest.approx(expected, abs=0.01)
        assert pnl > 0
