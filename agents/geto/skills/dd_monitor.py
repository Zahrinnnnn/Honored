"""
dd_monitor.py — GETO skill

Reads current drawdown percentage from the account table and exposes the
emergency halt detection check.

Design
──────
TOJI updates account.current_dd_pct and account.peak_balance after every
trade. GETO reads the value and decides whether to trigger the emergency halt.

Drawdown formula (maintained by TOJI):
    DD% = (peak_balance - balance) / peak_balance × 100

Emergency halt threshold: MAX_DRAWDOWN_PCT = 0.50  (50%)
The account table stores DD% on a 0–100 scale (not 0–1).
"""

import logging

from core.constants import MAX_DRAWDOWN_PCT
from core.state_manager import StateManager

logger = logging.getLogger(__name__)

# Threshold on 0–100 scale (MAX_DRAWDOWN_PCT is 0–1 in constants)
_DD_THRESHOLD_PCT = MAX_DRAWDOWN_PCT * 100.0


async def get_drawdown_pct(state: StateManager) -> float:
    """
    Returns current drawdown percentage (0–100 scale) from account table.
    Returns 0.0 if account row is missing.
    """
    account = await state.get_account()
    return float(account.get("current_dd_pct", 0.0)) if account else 0.0


async def is_emergency_halt_triggered(state: StateManager) -> bool:
    """
    Returns True if current drawdown >= 50% threshold.

    Does NOT check emergency_halt_flag — use this only to decide whether
    to SET the flag. The validation check reads the flag directly.
    """
    dd_pct    = await get_drawdown_pct(state)
    triggered = dd_pct >= _DD_THRESHOLD_PCT
    if triggered:
        logger.debug(
            "dd_monitor: emergency halt triggered (%.1f%% >= %.1f%%)",
            dd_pct, _DD_THRESHOLD_PCT,
        )
    return triggered
