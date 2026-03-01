"""
consecutive_tracker.py — GETO skill

Reads the consecutive loss counter from SQLite (maintained by TOJI after
each trade result) and exposes the soft-halt detection check.

Design
──────
TOJI calls state.increment_consecutive_losses() after each loss and
state.reset_consecutive_losses() after each win. GETO reads the value
and decides whether to trigger the soft halt.

Soft halt threshold: MAX_CONSECUTIVE_LOSSES = 3
"""

import logging

from core.constants import MAX_CONSECUTIVE_LOSSES
from core.state_manager import StateManager

logger = logging.getLogger(__name__)


async def get_consecutive_losses(state: StateManager) -> int:
    """Returns the current consecutive loss count from trading_state."""
    return await state.get_consecutive_losses()


async def is_soft_halt_triggered(state: StateManager) -> bool:
    """
    Returns True if consecutive losses >= MAX_CONSECUTIVE_LOSSES.

    Does NOT check halt_flag — use this only to decide whether to SET the flag.
    The validation check in trade_validator reads halt_flag directly.
    """
    losses = await get_consecutive_losses(state)
    triggered = losses >= MAX_CONSECUTIVE_LOSSES
    if triggered:
        logger.debug(
            "consecutive_tracker: soft halt triggered (%d >= %d)",
            losses, MAX_CONSECUTIVE_LOSSES,
        )
    return triggered
