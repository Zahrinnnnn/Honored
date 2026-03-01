"""
agent.py — GETO (Risk Manager)

Autonomous risk validation loop. Runs as a standalone asyncio process.

Responsibilities
────────────────
1. Poll trading_state.last_signal every 5 seconds.
2. Skip signals that are not "PENDING" (already processed or empty).
3. Gather live context from session_detector + session_info.
4. Run trade_validator (11 checks) against the pending signal.
5. Write "APPROVED" or "REJECTED:<reason>" to trading_state.last_risk_decision.
6. Monitor halt conditions every poll:
     - 50% drawdown → emergency halt (set emergency_halt_flag + halt_flag)
     - 3 consecutive losses → soft halt (set halt_flag)
   Both push an alert to alert_queue for GOJO to deliver via WhatsApp.

All checks are pure if/else — no LLM involvement.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
load_dotenv()

from core.constants import (  # noqa: E402
    MAX_CONSECUTIVE_LOSSES,
    MAX_DRAWDOWN_PCT,
)
from core.state_manager import StateManager  # noqa: E402
from agents.nanami.skills import session_detector  # noqa: E402
from agents.geto.skills.consecutive_tracker import (  # noqa: E402
    get_consecutive_losses,
    is_soft_halt_triggered,
)
from agents.geto.skills.dd_monitor import (  # noqa: E402
    get_drawdown_pct,
    is_emergency_halt_triggered,
)
from agents.geto.skills.trade_validator import validate  # noqa: E402


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [GETO] %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("geto")

_POLL_INTERVAL = 5  # seconds between validation checks


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run():
    logger.info("GETO starting up — risk manager online")

    async with StateManager() as state:
        while True:
            try:
                await _poll(state)
            except Exception as exc:
                logger.exception("Unhandled error in GETO poll: %s", exc)
            await asyncio.sleep(_POLL_INTERVAL)


async def _poll(state: StateManager):
    # ── 1. Halt condition monitoring (runs every poll, before signal check) ──
    await _monitor_halt_conditions(state)

    # ── 2. Read pending signal ────────────────────────────────────────────────
    raw = await state.get_trading_state("last_signal")
    if not raw:
        return

    try:
        signal = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.debug("last_signal is not valid JSON — skipping")
        return

    if signal.get("status") != "PENDING":
        return

    # ── 3. Build live context ─────────────────────────────────────────────────
    ctx = session_detector.get_session_context()

    spread_str = await state.get_session_info("current_spread")
    try:
        current_spread = float(spread_str) if spread_str else 0.0
    except ValueError:
        current_spread = 0.0

    # ── 4. Run validation ─────────────────────────────────────────────────────
    result = await validate(
        signal             = signal,
        state              = state,
        current_session    = ctx.name,
        is_breakout_window = ctx.is_breakout_window,
        current_spread     = current_spread,
    )

    # ── 5. Write decision ─────────────────────────────────────────────────────
    if result.approved:
        await state.set_trading_state("last_risk_decision", "APPROVED")
        signal["status"] = "APPROVED"
        await state.set_trading_state("last_signal", json.dumps(signal))
        logger.info(
            "Signal APPROVED — %s %s | last_risk_decision=APPROVED",
            signal.get("model"), signal.get("direction"),
        )
    else:
        decision = f"REJECTED:{result.fail_reason}"
        await state.set_trading_state("last_risk_decision", decision)
        signal["status"] = "REJECTED"
        await state.set_trading_state("last_signal", json.dumps(signal))
        logger.warning(
            "Signal REJECTED — %s | reason: %s",
            signal.get("model"), result.fail_reason,
        )


# ---------------------------------------------------------------------------
# Halt condition monitoring
# ---------------------------------------------------------------------------

async def _monitor_halt_conditions(state: StateManager):
    """
    Check drawdown and consecutive loss thresholds each poll.
    Sets halt flags in system_state and pushes WhatsApp alerts if triggered.
    """

    # ── Emergency halt: 50% drawdown ─────────────────────────────────────────
    already_emergency = await state.get_system_flag("emergency_halt_flag")
    if not already_emergency and await is_emergency_halt_triggered(state):
        dd_pct = await get_drawdown_pct(state)
        await state.set_system_flag("emergency_halt_flag", True)
        await state.set_system_flag("halt_flag", True)
        await state.push_alert(
            "EMERGENCY_HALT",
            f"EMERGENCY HALT. Drawdown reached {dd_pct:.1f}% "
            f"(limit: {int(MAX_DRAWDOWN_PCT * 100)}%). "
            f"All trading suspended. Only you can unlock this.",
        )
        logger.critical("EMERGENCY HALT — %.1f%% drawdown", dd_pct)

    # ── Soft halt: 3 consecutive losses ──────────────────────────────────────
    # Only trigger if not already halted (avoids duplicate alerts).
    already_halted    = await state.get_system_flag("halt_flag")
    already_emergency = await state.get_system_flag("emergency_halt_flag")

    if not already_halted and not already_emergency:
        if await is_soft_halt_triggered(state):
            losses = await get_consecutive_losses(state)
            await state.set_system_flag("halt_flag", True)
            await state.push_alert(
                "SOFT_HALT",
                f"Soft halt after {losses} consecutive losses. "
                f"Not gambling your account away. "
                f"Say 'override' when you're ready to go again.",
            )
            logger.warning("SOFT HALT — %d consecutive losses", losses)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(run())
