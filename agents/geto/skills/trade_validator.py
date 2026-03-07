"""
trade_validator.py — GETO skill

Runs all validation checks against a pending signal.
Pure if/else — no LLM, no network calls (reads only from SQLite state).

Validation checks (all must pass for APPROVED)
───────────────────────────────────────────────
 1. session_valid              — live session is an active trading session
 2. model_priority_ok         — breakout window not violated (Model C exclusive)
 3. regime_and_bias_ok        — regime/H4 bias allows the trade direction
 4. session_trades_within_limit — count < model's per-session max
 5. consecutive_losses_ok     — streak < MAX_CONSECUTIVE_LOSSES (3)
 6. drawdown_ok               — current_dd_pct < 50%
 7. news_clear                — minutes_to_next_news > NEWS_BLACKOUT_MINUTES (30)
 8. spread_acceptable         — current_spread < MAX_SPREAD_DOLLARS ($4.00)
 9. not_paused                — pause_flag is False
10. not_halted                — halt_flag AND emergency_halt_flag are both False
11. structural_break_clear    — no active structural break cooldown

Public API
──────────
validate(signal, state, current_session, is_breakout_window, current_spread)
    → ValidationResult

_regime_and_bias_ok(model, direction, regime, h4_bias) → bool   (exposed for testing)
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from core.constants import (
    MODEL_A, MODEL_B, MODEL_C,
    ACTIVE_SESSIONS,
    HTF_BIAS_BULLISH, HTF_BIAS_BEARISH,
    MODEL_SESSION_LIMITS,
    MAX_CONSECUTIVE_LOSSES,
    MAX_DRAWDOWN_PCT,
    MAX_SPREAD_DOLLARS,
    NEWS_BLACKOUT_MINUTES,
    REGIME_BULLISH_GRIND,
    REGIME_BEARISH_GRIND,
    REGIME_TIGHT_RANGE,
)
from core.state_manager import StateManager
from agents.geto.skills.account_monitor import get_account_snapshot
from agents.geto.skills.news_calendar import get_minutes_to_next_news

# LONDON_BREAKOUT is a valid session for Model C; include it in the session
# validity check (model_priority_ok enforces Model-C-exclusivity separately).
_ALLOWED_SESSIONS = set(ACTIVE_SESSIONS) | {"LONDON_BREAKOUT"}

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    """
    Outcome of a single validation run.

    Attributes:
        approved:    True if ALL checks passed.
        checks:      Ordered dict of check_name → bool (all 12 checks).
        fail_reason: Name of the first failed check, or "" if approved.
        signal:      The original signal dict that was validated.
    """
    approved:    bool
    checks:      dict  = field(default_factory=dict)
    fail_reason: str   = ""
    signal:      dict  = field(default_factory=dict)

    def summary(self) -> str:
        """Human-readable single-line summary."""
        if self.approved:
            passed = sum(self.checks.values())
            return f"APPROVED — {passed}/{len(self.checks)} checks passed"
        return f"REJECTED — {self.fail_reason}"


# ─────────────────────────────────────────────────────────────────────────────
# Regime + bias compatibility helper
# ─────────────────────────────────────────────────────────────────────────────

def _regime_and_bias_ok(
    model: str, direction: str, regime: str, h4_bias: str,
) -> bool:
    """
    Returns True if the regime/bias allows this trade direction.

    Model A (OU Grind):
        BUY  → requires regime == BULLISH_GRIND
        SELL → requires regime == BEARISH_GRIND

    Model B (OU Range):
        Any direction → requires regime == TIGHT_RANGE

    Model C (London Breakout):
        BUY  → requires h4_bias != BEARISH
        SELL → requires h4_bias != BULLISH
    """
    if model == MODEL_A:
        if direction == "BUY":
            return regime == REGIME_BULLISH_GRIND
        if direction == "SELL":
            return regime == REGIME_BEARISH_GRIND
        return False

    if model == MODEL_B:
        return regime == REGIME_TIGHT_RANGE

    if model == MODEL_C:
        if direction == "BUY":
            return h4_bias != HTF_BIAS_BEARISH
        if direction == "SELL":
            return h4_bias != HTF_BIAS_BULLISH
        return True

    logger.warning("_regime_and_bias_ok: unknown model '%s'", model)
    return False


def _is_structural_break_active(sb_until_str: str) -> bool:
    """Returns True if a structural break cooldown is currently active."""
    if not sb_until_str:
        return False
    try:
        sb_time = datetime.fromisoformat(sb_until_str)
        return datetime.now(timezone.utc) < sb_time
    except (ValueError, TypeError):
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Main validator
# ─────────────────────────────────────────────────────────────────────────────

async def validate(
    signal:             dict,
    state:              StateManager,
    current_session:    Optional[str] = None,
    is_breakout_window: bool = False,
    current_spread:     float = 0.0,
) -> ValidationResult:
    """
    Run all 12 validation checks against the signal.

    Args:
        signal:             Signal dict from NANAMI.
                            Required keys: model, session, direction.
        state:              Open StateManager — all SQLite reads happen here.
        current_session:    Live session name from session_detector (or None).
        is_breakout_window: True if 07:00–07:30 GMT right now.
        current_spread:     Current XAUUSD spread in USD (from session_info).

    Returns:
        ValidationResult with all check outcomes and the first fail reason.
    """
    model     = signal.get("model",     "")
    session   = signal.get("session",   "")
    direction = signal.get("direction", "")

    checks: dict[str, bool] = {}

    # ── 1. session_valid ────────────────────────────────────────────────────
    checks["session_valid"] = current_session in _ALLOWED_SESSIONS

    # ── 2. model_priority_ok ────────────────────────────────────────────────
    if is_breakout_window:
        checks["model_priority_ok"] = (model == MODEL_C)
    else:
        checks["model_priority_ok"] = True

    # ── 3. regime_and_bias_ok ───────────────────────────────────────────────
    regime = await state.get_session_info("current_regime") or ""
    h4_bias = await state.get_session_info("h4_bias") or ""
    checks["regime_and_bias_ok"] = _regime_and_bias_ok(model, direction, regime, h4_bias)

    # ── 4. session_trades_within_limit ──────────────────────────────────────
    session_key = "LONDON_BREAKOUT" if model == MODEL_C else session
    trade_count = await state.get_session_trade_count(session_key, model)
    limit       = MODEL_SESSION_LIMITS.get(model, 0)
    checks["session_trades_within_limit"] = trade_count < limit

    # ── 5. consecutive_losses_ok ────────────────────────────────────────────
    consecutive = await state.get_consecutive_losses()
    checks["consecutive_losses_ok"] = consecutive < MAX_CONSECUTIVE_LOSSES

    # ── 6. drawdown_ok ──────────────────────────────────────────────────────
    account = await get_account_snapshot(state)
    checks["drawdown_ok"] = account["current_dd_pct"] < (MAX_DRAWDOWN_PCT * 100)

    # ── 8. news_clear ───────────────────────────────────────────────────────
    mins_to_news = await get_minutes_to_next_news(state)
    checks["news_clear"] = mins_to_news > NEWS_BLACKOUT_MINUTES

    # ── 9. spread_acceptable ────────────────────────────────────────────────
    checks["spread_acceptable"] = current_spread < MAX_SPREAD_DOLLARS

    # ── 10. not_paused ──────────────────────────────────────────────────────
    checks["not_paused"] = not await state.get_system_flag("pause_flag")

    # ── 11. not_halted (combines halt_flag + emergency_halt_flag) ───────────
    checks["not_halted"] = (
        not await state.get_system_flag("halt_flag")
        and not await state.get_system_flag("emergency_halt_flag")
    )

    # ── 12. structural_break_clear ──────────────────────────────────────────
    sb_until = await state.get_session_info("structural_break_until") or ""
    checks["structural_break_clear"] = not _is_structural_break_active(sb_until)

    # ── Decision ────────────────────────────────────────────────────────────
    fail_reason = next((name for name, ok in checks.items() if not ok), "")
    approved    = (fail_reason == "")

    _log_result(signal, checks, approved, fail_reason)

    return ValidationResult(
        approved    = approved,
        checks      = checks,
        fail_reason = fail_reason,
        signal      = signal,
    )


def _log_result(
    signal:      dict,
    checks:      dict,
    approved:    bool,
    fail_reason: str,
):
    passed = sum(checks.values())
    total  = len(checks)
    if approved:
        logger.info(
            "GETO APPROVED — %s %s | %d/%d checks",
            signal.get("model"), signal.get("direction"), passed, total,
        )
    else:
        logger.warning(
            "GETO REJECTED — %s %s | failed: %s | %d/%d checks",
            signal.get("model"), signal.get("direction"),
            fail_reason, passed, total,
        )
