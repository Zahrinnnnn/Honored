"""
state_updater.py — TOJI skill

Applies all post-trade state updates to SQLite after a trade closes.

Updates (in order)
──────────────────
1. Consecutive losses  — reset on WIN, increment on LOSS
2. Session trade count — increment for model + session
3. Account snapshot    — balance, equity, peak_balance, current_dd_pct,
                         open_positions (decremented by 1)
4. trading_state keys  — last_trade_result, last_trade_timestamp,
                         total_trades_today

Public API
──────────
post_trade_update(state, trade_id, result, exit_price, pnl,
                  balance_before, open_time, session, model)
    → (balance_after, drawdown_pct, duration_mins)
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from core.state_manager import StateManager

logger = logging.getLogger(__name__)


async def post_trade_update(
    state: StateManager,
    result: str,
    pnl: float,
    balance_before: float,
    open_time: Optional[str],
    session: str,
    model: str,
) -> tuple[float, float, float]:
    """
    Apply all post-trade state updates after a trade closes.

    Args:
        state:          Open StateManager instance.
        result:         "WIN" or "LOSS".
        pnl:            Realised P&L in USD.
        balance_before: Account balance at trade open.
        open_time:      ISO timestamp when trade was opened (for duration calc).
        session:        Session name from the signal (e.g. "LONDON_OPEN").
        model:          Model constant (e.g. MODEL_A).

    Returns:
        Tuple of (balance_after, drawdown_pct, duration_mins).
    """
    now = datetime.now(timezone.utc)

    # ── Duration ────────────────────────────────────────────────────────────
    duration_mins = 0.0
    if open_time:
        try:
            opened = datetime.fromisoformat(open_time)
            duration_mins = (now - opened).total_seconds() / 60.0
        except (ValueError, TypeError):
            pass

    # ── Balance / drawdown ──────────────────────────────────────────────────
    balance_after = round(balance_before + pnl, 2)

    account = await state.get_account()
    peak_balance = max(
        float(account.get("peak_balance", balance_before)),
        balance_after,
    )
    drawdown_pct = 0.0
    if peak_balance > 0:
        drawdown_pct = max(0.0, (peak_balance - balance_after) / peak_balance * 100)
    drawdown_pct = round(drawdown_pct, 4)

    open_positions = max(0, int(account.get("open_positions", 1)) - 1)

    # ── 1. Consecutive losses ───────────────────────────────────────────────
    if result == "WIN":
        await state.reset_consecutive_losses()
    else:
        await state.increment_consecutive_losses()

    # ── 2. Session trade count ──────────────────────────────────────────────
    await state.increment_session_trade_count(session, model)

    # ── 3. Account snapshot ─────────────────────────────────────────────────
    await state.update_account(
        balance        = balance_after,
        equity         = balance_after,
        peak_balance   = peak_balance,
        current_dd_pct = drawdown_pct,
        open_positions = open_positions,
    )

    # ── 4. trading_state keys ───────────────────────────────────────────────
    await state.set_trading_state("last_trade_result",    result)
    await state.set_trading_state("last_trade_timestamp", now.isoformat())

    total_str = await state.get_trading_state("total_trades_today")
    total = int(total_str) if total_str.isdigit() else 0
    await state.set_trading_state("total_trades_today", total + 1)

    logger.info(
        "State updated: %s | PnL=%.2f | balance %.2f→%.2f | DD=%.2f%% | open=%d",
        result, pnl, balance_before, balance_after, drawdown_pct, open_positions,
    )
    return balance_after, drawdown_pct, round(duration_mins, 1)
