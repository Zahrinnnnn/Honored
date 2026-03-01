"""
account_monitor.py — GETO skill

Reads the current account snapshot from SQLite (written by TOJI after each
trade, and periodically by the MetaApi account poller).

Provides a single async function so trade_validator can get all account
state in one call without touching the DB multiple times.
"""

import logging

from core.state_manager import StateManager

logger = logging.getLogger(__name__)


async def get_account_snapshot(state: StateManager) -> dict:
    """
    Returns current account state with keys:
        balance, equity, peak_balance, current_dd_pct, open_positions

    Returns safe defaults (zeros / 0) if the account row is missing.
    Safe defaults make trade_validator fail drawdown_ok and open_trades_ok
    checks conservatively (open_positions=0 passes, dd_pct=0 passes) —
    which is correct: missing account data should not block trading by default.
    """
    account = await state.get_account()
    if not account:
        logger.warning("account_monitor: account row missing — returning zeros")
        return {
            "balance":        0.0,
            "equity":         0.0,
            "peak_balance":   0.0,
            "current_dd_pct": 0.0,
            "open_positions": 0,
        }
    return {
        "balance":        float(account.get("balance",        0.0)),
        "equity":         float(account.get("equity",         0.0)),
        "peak_balance":   float(account.get("peak_balance",   0.0)),
        "current_dd_pct": float(account.get("current_dd_pct", 0.0)),
        "open_positions": int(account.get("open_positions",   0)),
    }
