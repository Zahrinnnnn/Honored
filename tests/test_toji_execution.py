"""
test_toji_execution.py — Phase 4 TOJI tests

Tests:
  TestLotCalculator       (9)  — calculate_lot, calculate_risk_amount
  TestOrderPlacer         (6)  — paper mode simulation
  TestTradeMonitor       (10)  — check_exit, calculate_pnl, get_current_price
  TestTradeLogger         (6)  — log_trade_open, log_trade_close
  TestStateUpdater        (9)  — post_trade_update (WIN/LOSS, all fields)
  TestAgentSignalPoll     (6)  — _poll_signals / _execute_signal
  TestIntegration         (6)  — full flow via in-memory SQLite
                         ──
                         52 total
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ─── ensure PAPER_MODE is True for all tests ────────────────────────────────
os.environ.setdefault("PAPER_MODE", "true")

from core.constants import MODEL_A, MODEL_B, MODEL_C
from core.state_manager import StateManager
from agents.toji.skills.lot_calculator import calculate_lot, calculate_risk_amount
from agents.toji.skills.order_placer import place_order, _simulate_fill
from agents.toji.skills.trade_monitor import check_exit, calculate_pnl, get_current_price
from agents.toji.skills.trade_logger import log_trade_open, log_trade_close
from agents.toji.skills.state_updater import post_trade_update


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def run(coro):
    """Shorthand to run a coroutine in tests."""
    return asyncio.run(coro)


def _make_signal(
    model=MODEL_A,
    direction="BUY",
    entry=2350.0,
    sl=2345.0,
    tp=2365.0,
    sl_distance=5.0,
    session="LONDON_OPEN",
    regime="TRENDING",
    reason="unit test",
    status="APPROVED",
) -> dict:
    return {
        "id":          "sig-001",
        "model":       model,
        "direction":   direction,
        "entry_price": entry,
        "sl_price":    sl,
        "tp_price":    tp,
        "sl_distance": sl_distance,
        "session":     session,
        "regime":      regime,
        "reason":      reason,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "status":      status,
    }


def _make_open_trade(
    trade_id=1,
    direction="BUY",
    entry=2350.0,
    sl=2345.0,
    tp=2365.0,
    lot_size=0.40,
    balance_before=20.0,
    model=MODEL_A,
    created_at=None,
) -> dict:
    return {
        "id":            trade_id,
        "model":         model,
        "direction":     direction,
        "entry_price":   entry,
        "sl_price":      sl,
        "tp_price":      tp,
        "lot_size":      lot_size,
        "sl_distance":   5.0,
        "risk_amount":   2.0,
        "balance_before": balance_before,
        "result":        None,
        "paper":         1,
        "created_at":    created_at or datetime.now(timezone.utc).isoformat(),
        "reason":        "LONDON_OPEN",
    }


async def _make_state_db() -> StateManager:
    """In-memory SQLite state for integration tests."""
    state = StateManager(":memory:")
    await state.connect()
    await state.update_account(
        balance=20.0, equity=20.0, peak_balance=20.0,
        current_dd_pct=0.0, open_positions=0,
    )
    return state


# ─────────────────────────────────────────────────────────────────────────────
# TestLotCalculator
# ─────────────────────────────────────────────────────────────────────────────

class TestLotCalculator:

    def test_basic_calculation(self):
        # balance=$20, SL=$5 → risk=$2, lot=0.40
        assert calculate_lot(20.0, 5.0) == 0.40

    def test_growth_scaling(self):
        # balance=$40, SL=$5 → risk=$4, lot=0.80
        assert calculate_lot(40.0, 5.0) == 0.80

    def test_larger_sl_reduces_lot(self):
        # balance=$20, SL=$8 → risk=$2, lot=0.25
        assert calculate_lot(20.0, 8.0) == 0.25

    def test_model_b_sl_min(self):
        # balance=$20, SL=$3 → risk=$2, lot=0.67
        assert calculate_lot(20.0, 3.0) == 0.67

    def test_minimum_lot_floor(self):
        # Very large SL → lot would be tiny; floor at 0.01
        result = calculate_lot(0.05, 100.0)
        assert result == 0.01

    def test_custom_risk_pct(self):
        # 5% risk: balance=$20, SL=$5 → risk=$1, lot=0.20
        assert calculate_lot(20.0, 5.0, risk_pct=0.05) == 0.20

    def test_zero_sl_raises(self):
        with pytest.raises(ValueError, match="sl_distance"):
            calculate_lot(20.0, 0.0)

    def test_negative_sl_raises(self):
        with pytest.raises(ValueError, match="sl_distance"):
            calculate_lot(20.0, -5.0)

    def test_calculate_risk_amount(self):
        assert calculate_risk_amount(20.0) == 2.0
        assert calculate_risk_amount(40.0) == 4.0
        assert calculate_risk_amount(100.0, 0.05) == 5.0


# ─────────────────────────────────────────────────────────────────────────────
# TestOrderPlacer
# ─────────────────────────────────────────────────────────────────────────────

class TestOrderPlacer:

    def test_paper_mode_returns_dict(self):
        signal = _make_signal()
        result = run(place_order(signal, 0.40, connection=None))
        assert isinstance(result, dict)

    def test_paper_order_id_format(self):
        signal = _make_signal()
        result = run(place_order(signal, 0.40, connection=None))
        assert result["order_id"].startswith("PAPER-")

    def test_paper_entry_price_from_signal(self):
        signal = _make_signal(entry=2355.50)
        result = run(place_order(signal, 0.40, connection=None))
        assert result["entry_price"] == 2355.50

    def test_paper_sl_tp_from_signal(self):
        signal = _make_signal(sl=2348.0, tp=2370.0)
        result = run(place_order(signal, 0.40, connection=None))
        assert result["sl_price"] == 2348.0
        assert result["tp_price"] == 2370.0

    def test_paper_flag_is_true(self):
        signal = _make_signal()
        result = run(place_order(signal, 0.40, connection=None))
        assert result["paper"] is True

    def test_simulate_fill_sell_direction(self):
        signal = _make_signal(direction="SELL")
        result = _simulate_fill(signal, 0.30)
        assert result["order_id"].startswith("PAPER-")
        assert result["lot_size"] == 0.30


# ─────────────────────────────────────────────────────────────────────────────
# TestTradeMonitor
# ─────────────────────────────────────────────────────────────────────────────

class TestTradeMonitor:

    # ── check_exit BUY ──────────────────────────────────────────────────────

    def test_buy_tp_hit(self):
        trade = _make_open_trade(direction="BUY", sl=2345.0, tp=2365.0)
        assert check_exit(trade, current_bid=2365.0, current_ask=2366.0) == "WIN"

    def test_buy_sl_hit(self):
        trade = _make_open_trade(direction="BUY", sl=2345.0, tp=2365.0)
        assert check_exit(trade, current_bid=2345.0, current_ask=2346.0) == "LOSS"

    def test_buy_still_open(self):
        trade = _make_open_trade(direction="BUY", sl=2345.0, tp=2365.0)
        assert check_exit(trade, current_bid=2355.0, current_ask=2356.0) is None

    def test_buy_tp_not_yet_hit(self):
        trade = _make_open_trade(direction="BUY", sl=2345.0, tp=2365.0)
        assert check_exit(trade, current_bid=2364.9, current_ask=2365.0) is None

    # ── check_exit SELL ─────────────────────────────────────────────────────

    def test_sell_tp_hit(self):
        trade = _make_open_trade(direction="SELL", sl=2360.0, tp=2335.0)
        assert check_exit(trade, current_bid=2334.0, current_ask=2335.0) == "WIN"

    def test_sell_sl_hit(self):
        trade = _make_open_trade(direction="SELL", sl=2360.0, tp=2335.0)
        assert check_exit(trade, current_bid=2359.0, current_ask=2360.0) == "LOSS"

    def test_sell_still_open(self):
        trade = _make_open_trade(direction="SELL", sl=2360.0, tp=2335.0)
        assert check_exit(trade, current_bid=2349.0, current_ask=2350.0) is None

    # ── calculate_pnl ───────────────────────────────────────────────────────

    def test_buy_win_pnl(self):
        # BUY: entry=2350, exit=2365, lot=0.40 → pnl = 0.40 × 15 = 6.00
        trade = _make_open_trade(direction="BUY", entry=2350.0, lot_size=0.40)
        assert calculate_pnl(trade, 2365.0) == pytest.approx(6.0, abs=0.01)

    def test_buy_loss_pnl(self):
        # BUY: entry=2350, exit=2345, lot=0.40 → pnl = 0.40 × (-5) = -2.00
        trade = _make_open_trade(direction="BUY", entry=2350.0, lot_size=0.40)
        assert calculate_pnl(trade, 2345.0) == pytest.approx(-2.0, abs=0.01)

    def test_sell_win_pnl(self):
        # SELL: entry=2350, exit=2335, lot=0.40 → pnl = 0.40 × 15 = 6.00
        trade = _make_open_trade(direction="SELL", entry=2350.0, lot_size=0.40)
        assert calculate_pnl(trade, 2335.0) == pytest.approx(6.0, abs=0.01)

    # ── get_current_price ────────────────────────────────────────────────────

    def test_get_current_price_returns_bid_ask(self):
        connection = MagicMock()
        connection.get_symbol_price = AsyncMock(return_value={"bid": 2350.0, "ask": 2351.0})
        result = run(get_current_price(connection))
        assert result == {"bid": 2350.0, "ask": 2351.0}

    def test_get_current_price_returns_none_on_error(self):
        connection = MagicMock()
        connection.get_symbol_price = AsyncMock(side_effect=Exception("timeout"))
        result = run(get_current_price(connection))
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# TestTradeLogger
# ─────────────────────────────────────────────────────────────────────────────

class TestTradeLogger:

    def test_log_trade_open_returns_trade_id(self):
        async def _run():
            state = await _make_state_db()
            signal = _make_signal()
            order_result = {"entry_price": 2350.0, "order_id": "PAPER-ABC12345"}
            trade_id = await log_trade_open(state, signal, 0.40, order_result)
            await state.close()
            return trade_id

        trade_id = run(_run())
        assert isinstance(trade_id, int)
        assert trade_id >= 1

    def test_log_trade_open_reads_balance(self):
        async def _run():
            state = await _make_state_db()
            await state.update_account(balance=25.0)
            signal = _make_signal()
            order_result = {"entry_price": 2350.0, "order_id": "PAPER-XYZ"}
            await log_trade_open(state, signal, 0.50, order_result)
            trades = await state.get_trades()
            await state.close()
            return trades[0]

        trade = run(_run())
        assert trade["balance_before"] == 25.0

    def test_log_trade_open_risk_amount(self):
        async def _run():
            state = await _make_state_db()
            signal = _make_signal(sl_distance=5.0)
            order_result = {"entry_price": 2350.0, "order_id": "PAPER-XYZ"}
            await log_trade_open(state, signal, 0.40, order_result)
            trades = await state.get_trades()
            await state.close()
            return trades[0]

        trade = run(_run())
        # risk_amount = lot_size × sl_distance = 0.40 × 5.0 = 2.0
        assert trade["risk_amount"] == pytest.approx(2.0, abs=0.01)

    def test_log_trade_open_direction_model(self):
        async def _run():
            state = await _make_state_db()
            signal = _make_signal(model=MODEL_B, direction="SELL")
            order_result = {"entry_price": 2350.0, "order_id": "PAPER-XYZ"}
            await log_trade_open(state, signal, 0.30, order_result)
            trades = await state.get_trades()
            await state.close()
            return trades[0]

        trade = run(_run())
        assert trade["model"]     == MODEL_B
        assert trade["direction"] == "SELL"

    def test_log_trade_close_updates_result(self):
        async def _run():
            state = await _make_state_db()
            signal = _make_signal()
            order_result = {"entry_price": 2350.0, "order_id": "PAPER-XYZ"}
            trade_id = await log_trade_open(state, signal, 0.40, order_result)
            await log_trade_close(state, trade_id, "WIN", 2365.0, 6.0, 26.0, 0.0, 12.5)
            trades = await state.get_trades()
            await state.close()
            return trades[0]

        trade = run(_run())
        assert trade["result"]     == "WIN"
        assert trade["exit_price"] == 2365.0
        assert trade["pnl"]        == pytest.approx(6.0, abs=0.01)

    def test_log_trade_close_duration(self):
        async def _run():
            state = await _make_state_db()
            signal = _make_signal()
            order_result = {"entry_price": 2350.0, "order_id": "PAPER-XYZ"}
            trade_id = await log_trade_open(state, signal, 0.40, order_result)
            await log_trade_close(state, trade_id, "LOSS", 2345.0, -2.0, 18.0, 10.0, 8.3)
            trades = await state.get_trades()
            await state.close()
            return trades[0]

        trade = run(_run())
        assert trade["duration_mins"] == pytest.approx(8.3, abs=0.1)


# ─────────────────────────────────────────────────────────────────────────────
# TestStateUpdater
# ─────────────────────────────────────────────────────────────────────────────

class TestStateUpdater:

    def test_win_resets_consecutive_losses(self):
        async def _run():
            state = await _make_state_db()
            await state.set_trading_state("consecutive_losses", "2")
            await post_trade_update(state, "WIN", 6.0, 20.0, None, "LONDON_OPEN", MODEL_A)
            losses = await state.get_consecutive_losses()
            await state.close()
            return losses

        assert run(_run()) == 0

    def test_loss_increments_consecutive_losses(self):
        async def _run():
            state = await _make_state_db()
            await state.set_trading_state("consecutive_losses", "1")
            await post_trade_update(state, "LOSS", -2.0, 20.0, None, "LONDON_OPEN", MODEL_A)
            losses = await state.get_consecutive_losses()
            await state.close()
            return losses

        assert run(_run()) == 2

    def test_balance_updated_after_win(self):
        async def _run():
            state = await _make_state_db()
            balance_after, _, _ = await post_trade_update(
                state, "WIN", 6.0, 20.0, None, "LONDON_OPEN", MODEL_A
            )
            account = await state.get_account()
            await state.close()
            return account["balance"], balance_after

        balance, returned = run(_run())
        assert balance == pytest.approx(26.0, abs=0.01)
        assert returned == pytest.approx(26.0, abs=0.01)

    def test_balance_updated_after_loss(self):
        async def _run():
            state = await _make_state_db()
            balance_after, _, _ = await post_trade_update(
                state, "LOSS", -2.0, 20.0, None, "LONDON_OPEN", MODEL_A
            )
            account = await state.get_account()
            await state.close()
            return account["balance"]

        assert run(_run()) == pytest.approx(18.0, abs=0.01)

    def test_peak_balance_tracks_high_water_mark(self):
        async def _run():
            state = await _make_state_db()
            await state.update_account(balance=20.0, peak_balance=20.0)
            # Win: balance goes to 26
            await post_trade_update(state, "WIN", 6.0, 20.0, None, "LONDON_OPEN", MODEL_A)
            # Loss: balance goes to 24, peak stays at 26
            await post_trade_update(state, "LOSS", -2.0, 26.0, None, "LONDON_OPEN", MODEL_A)
            account = await state.get_account()
            await state.close()
            return account["peak_balance"], account["current_dd_pct"]

        peak, dd = run(_run())
        assert peak == pytest.approx(26.0, abs=0.01)
        # DD = (26 - 24) / 26 × 100 ≈ 7.69%
        assert dd == pytest.approx(7.69, abs=0.1)

    def test_open_positions_decremented(self):
        async def _run():
            state = await _make_state_db()
            await state.update_account(open_positions=2)
            await post_trade_update(state, "WIN", 6.0, 20.0, None, "LONDON_OPEN", MODEL_A)
            account = await state.get_account()
            await state.close()
            return account["open_positions"]

        assert run(_run()) == 1

    def test_open_positions_floor_at_zero(self):
        async def _run():
            state = await _make_state_db()
            await state.update_account(open_positions=0)
            await post_trade_update(state, "WIN", 6.0, 20.0, None, "LONDON_OPEN", MODEL_A)
            account = await state.get_account()
            await state.close()
            return account["open_positions"]

        assert run(_run()) == 0

    def test_session_trade_count_incremented(self):
        async def _run():
            state = await _make_state_db()
            await post_trade_update(state, "WIN", 6.0, 20.0, None, "LONDON_OPEN", MODEL_A)
            count = await state.get_session_trade_count("LONDON_OPEN", MODEL_A)
            await state.close()
            return count

        assert run(_run()) == 1

    def test_model_c_uses_london_breakout_key(self):
        async def _run():
            state = await _make_state_db()
            await post_trade_update(state, "WIN", 6.0, 20.0, None, "LONDON_OPEN", MODEL_C)
            count_c      = await state.get_session_trade_count("LONDON_BREAKOUT", MODEL_C)
            count_wrong  = await state.get_session_trade_count("LONDON_OPEN",     MODEL_C)
            await state.close()
            return count_c, count_wrong

        count_c, count_wrong = run(_run())
        assert count_c == 1
        assert count_wrong == 0


# ─────────────────────────────────────────────────────────────────────────────
# TestAgentSignalPoll
# ─────────────────────────────────────────────────────────────────────────────

class TestAgentSignalPoll:

    def _make_approved_signal(self, **kwargs) -> str:
        sig = _make_signal(**kwargs)
        sig["status"] = "APPROVED"
        return json.dumps(sig)

    def test_approved_signal_triggers_execution(self):
        async def _run():
            state = await _make_state_db()
            await state.update_account(balance=20.0, open_positions=0)
            await state.set_trading_state("last_risk_decision", "APPROVED")
            await state.set_trading_state("last_signal", self._make_approved_signal())

            from agents.toji.agent import _poll_signals
            await _poll_signals(state, connection=None)

            decision = await state.get_trading_state("last_risk_decision")
            await state.close()
            return decision

        assert run(_run()) == "PLACED"

    def test_placed_signal_clears_queue(self):
        async def _run():
            state = await _make_state_db()
            await state.update_account(balance=20.0)
            await state.set_trading_state("last_risk_decision", "APPROVED")
            await state.set_trading_state("last_signal", self._make_approved_signal())

            from agents.toji.agent import _poll_signals
            await _poll_signals(state, connection=None)

            raw = await state.get_trading_state("last_signal")
            signal = json.loads(raw)
            await state.close()
            return signal["status"]

        assert run(_run()) == "PLACED"

    def test_pending_decision_skipped(self):
        async def _run():
            state = await _make_state_db()
            await state.set_trading_state("last_risk_decision", "PENDING")
            await state.set_trading_state("last_signal", self._make_approved_signal())

            from agents.toji.agent import _poll_signals
            await _poll_signals(state, connection=None)

            # decision should still be PENDING (not changed)
            decision = await state.get_trading_state("last_risk_decision")
            await state.close()
            return decision

        assert run(_run()) == "PENDING"

    def test_rejected_decision_skipped(self):
        async def _run():
            state = await _make_state_db()
            await state.set_trading_state("last_risk_decision", "REJECTED:spread")
            from agents.toji.agent import _poll_signals
            await _poll_signals(state, connection=None)
            decision = await state.get_trading_state("last_risk_decision")
            await state.close()
            return decision

        assert run(_run()) == "REJECTED:spread"

    def test_trade_logged_after_execution(self):
        async def _run():
            state = await _make_state_db()
            await state.update_account(balance=20.0, open_positions=0)
            await state.set_trading_state("last_risk_decision", "APPROVED")
            await state.set_trading_state("last_signal", self._make_approved_signal())

            from agents.toji.agent import _poll_signals
            await _poll_signals(state, connection=None)

            trades = await state.get_trades()
            await state.close()
            return trades

        trades = run(_run())
        assert len(trades) == 1

    def test_open_positions_incremented(self):
        async def _run():
            state = await _make_state_db()
            await state.update_account(balance=20.0, open_positions=0)
            await state.set_trading_state("last_risk_decision", "APPROVED")
            await state.set_trading_state("last_signal", self._make_approved_signal())

            from agents.toji.agent import _poll_signals
            await _poll_signals(state, connection=None)

            account = await state.get_account()
            await state.close()
            return account["open_positions"]

        assert run(_run()) == 1


# ─────────────────────────────────────────────────────────────────────────────
# TestIntegration — full cycle via in-memory SQLite
# ─────────────────────────────────────────────────────────────────────────────

class TestIntegration:

    def test_full_win_cycle(self):
        """Signal → lot calc → paper order → log open → monitor → close WIN."""
        async def _run():
            state = await _make_state_db()
            await state.update_account(balance=20.0, open_positions=0)

            # 1. APPROVED signal
            await state.set_trading_state("last_risk_decision", "APPROVED")
            signal = _make_signal(entry=2350.0, sl=2345.0, tp=2365.0, sl_distance=5.0)
            signal["status"] = "APPROVED"
            await state.set_trading_state("last_signal", json.dumps(signal))

            from agents.toji.agent import _poll_signals, _close_trade
            await _poll_signals(state, connection=None)

            # 2. Trade is now open
            open_trades = await state.get_open_trades()
            assert len(open_trades) == 1

            # 3. Price hits TP
            trade = open_trades[0]
            await _close_trade(state, trade, "WIN", bid=2365.0, ask=2366.0)

            # 4. Verify final state
            trades = await state.get_trades()
            account = await state.get_account()
            losses  = await state.get_consecutive_losses()
            await state.close()
            return trades[0], account, losses

        trade, account, losses = run(_run())
        assert trade["result"]  == "WIN"
        assert trade["pnl"]     == pytest.approx(6.0, abs=0.01)   # 0.40 × 15 = 6.0
        assert account["balance"] == pytest.approx(26.0, abs=0.01)
        assert losses == 0

    def test_full_loss_cycle(self):
        """Signal → paper order → SL hit → LOSS state update."""
        async def _run():
            state = await _make_state_db()
            await state.update_account(balance=20.0, open_positions=0)

            await state.set_trading_state("last_risk_decision", "APPROVED")
            signal = _make_signal(entry=2350.0, sl=2345.0, tp=2365.0, sl_distance=5.0)
            signal["status"] = "APPROVED"
            await state.set_trading_state("last_signal", json.dumps(signal))

            from agents.toji.agent import _poll_signals, _close_trade
            await _poll_signals(state, connection=None)

            trade = (await state.get_open_trades())[0]
            await _close_trade(state, trade, "LOSS", bid=2345.0, ask=2346.0)

            account = await state.get_account()
            losses  = await state.get_consecutive_losses()
            await state.close()
            return account["balance"], losses

        balance, losses = run(_run())
        assert balance == pytest.approx(18.0, abs=0.01)   # 20 - 2 loss
        assert losses  == 1

    def test_consecutive_loss_tracking(self):
        """Three losses in a row increments consecutive_losses to 3."""
        async def _run():
            state = await _make_state_db()
            await state.update_account(balance=20.0, open_positions=0)

            from agents.toji.agent import _poll_signals, _close_trade

            for _ in range(3):
                signal = _make_signal(
                    entry=2350.0, sl=2345.0, tp=2365.0, sl_distance=5.0,
                )
                signal["status"] = "APPROVED"
                await state.set_trading_state("last_risk_decision", "APPROVED")
                await state.set_trading_state("last_signal", json.dumps(signal))
                await state.update_account(open_positions=0)
                await _poll_signals(state, connection=None)

                trade = (await state.get_open_trades())[0]
                await _close_trade(state, trade, "LOSS", bid=2345.0, ask=2346.0)

            losses = await state.get_consecutive_losses()
            await state.close()
            return losses

        assert run(_run()) == 3

    def test_balance_growth_scales_lot(self):
        """At $40 balance, same SL → double the lot."""
        lot_20  = calculate_lot(20.0, 5.0)
        lot_40  = calculate_lot(40.0, 5.0)
        assert lot_40 == pytest.approx(lot_20 * 2, abs=0.01)

    def test_alert_pushed_on_open(self):
        async def _run():
            state = await _make_state_db()
            await state.update_account(balance=20.0, open_positions=0)
            await state.set_trading_state("last_risk_decision", "APPROVED")
            signal = _make_signal()
            signal["status"] = "APPROVED"
            await state.set_trading_state("last_signal", json.dumps(signal))

            from agents.toji.agent import _poll_signals
            await _poll_signals(state, connection=None)

            alerts = await state.get_pending_alerts()
            await state.close()
            return alerts

        alerts = run(_run())
        assert len(alerts) >= 1
        assert any(a["alert_type"] == "TRADE_OPENED" for a in alerts)

    def test_alert_pushed_on_close(self):
        async def _run():
            state = await _make_state_db()
            await state.update_account(balance=20.0, open_positions=0)
            await state.set_trading_state("last_risk_decision", "APPROVED")
            signal = _make_signal()
            signal["status"] = "APPROVED"
            await state.set_trading_state("last_signal", json.dumps(signal))

            from agents.toji.agent import _poll_signals, _close_trade
            await _poll_signals(state, connection=None)

            trade = (await state.get_open_trades())[0]
            await _close_trade(state, trade, "WIN", bid=2365.0, ask=2366.0)

            alerts = await state.get_pending_alerts()
            await state.close()
            return alerts

        alerts = run(_run())
        types = [a["alert_type"] for a in alerts]
        assert "TRADE_OPENED" in types
        assert "TRADE_CLOSED" in types
