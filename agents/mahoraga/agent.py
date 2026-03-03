"""
mahoraga/agent.py — The Learning Agent

Scheduled runs:
  Daily   — MAHORAGA_DAILY_RUN_TIME (21:30 GMT, after NY close): 30-day review
  Weekly  — MAHORAGA_WEEKLY_RUN_DAY (Sunday) same time: 90-day deep review

On-demand:
  When mahoraga_state.manual_trigger == "true" the agent runs immediately
  on the next poll. trigger_mahoraga.py (GOJO tool script) sets this flag.

Safety constraints:
  - NEVER auto-applies parameter changes
  - All recommendations are delivered via alert_queue → GOJO → WhatsApp
  - Minimum MIN_TRADES_FOR_ANALYSIS completed trades required before analysis
  - Reads trades table only; writes to mahoraga_state + alert_queue

Poll cadence: 60 seconds (light — just checks time / manual flag).
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from core.constants import (
    MAHORAGA_DAILY_RUN_TIME,
    MAHORAGA_WEEKLY_RUN_DAY,
    MIN_TRADES_FOR_ANALYSIS,
    MODEL_A, MODEL_B, MODEL_C,
)
from core.state_manager import StateManager

from agents.mahoraga.skills.performance_analyzer import (
    compute_stats,
    by_model_stats,
    by_session_stats,
    by_direction_stats,
    rolling_stats,
)
from agents.mahoraga.skills.model_evaluator import evaluate_model
from agents.mahoraga.skills.parameter_optimizer import (
    analyze_sl_ranges,
    analyze_session_timing,
    analyze_direction_bias,
    analyze_duration_vs_outcome,
    generate_parameter_suggestions,
)
from agents.mahoraga.skills.regime_validator import (
    validate_regime_accuracy,
    generate_regime_suggestions,
)
from agents.mahoraga.skills.adaptation_reporter import (
    compile_report,
    format_whatsapp_summary,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MAHORAGA] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_POLL_SECS    = 60
_DAILY_HOUR   = int(MAHORAGA_DAILY_RUN_TIME.split(":")[0])
_DAILY_MINUTE = int(MAHORAGA_DAILY_RUN_TIME.split(":")[1])


# ---------------------------------------------------------------------------
# Analysis runner
# ---------------------------------------------------------------------------

async def run_analysis(state: StateManager, is_weekly: bool = False) -> dict:
    """Full MAHORAGA analysis pipeline.

    Returns the compiled report dict.
    Writes to mahoraga_state.last_report + pushes WhatsApp alert.
    """
    period_days = 90 if is_weekly else 30
    paper       = os.environ.get("PAPER_MODE", "true").lower() == "true"

    logger.info("Starting %s analysis (last %d days)...", "weekly" if is_weekly else "daily", period_days)

    trades = await state.get_trades_by_period(days=period_days, paper=paper)
    logger.info("Loaded %d completed trades", len(trades))

    if len(trades) < MIN_TRADES_FOR_ANALYSIS:
        logger.info(
            "Insufficient trades (%d < %d) — skipping analysis.",
            len(trades), MIN_TRADES_FOR_ANALYSIS,
        )
        await state.set_mahoraga_state("last_run_result", "SKIPPED_INSUFFICIENT_DATA")
        await state.set_mahoraga_state("last_run_at", _now())
        await state.set_mahoraga_state("manual_trigger", "false")
        return {}

    # ── 1. Overall performance statistics ───────────────────────────────────
    overall       = compute_stats(trades)
    rolling_7d    = rolling_stats(trades, 7)
    rolling_30d   = rolling_stats(trades, 30)
    by_session    = by_session_stats(trades)
    by_direction  = by_direction_stats(trades)

    enhanced_overall = {
        **overall,
        "rolling_7d":   rolling_7d,
        "rolling_30d":  rolling_30d,
        "by_session":   by_session,
        "by_direction": by_direction,
    }

    # ── 2. Per-model statistical evaluation ──────────────────────────────────
    model_evals = []
    for model_name in [MODEL_A, MODEL_B, MODEL_C]:
        model_trades = [t for t in trades if t.get("model") == model_name]
        ev = evaluate_model(model_name, model_trades)
        model_evals.append(ev)
        logger.info(
            "  %s: %d trades | WR %.0f%% | Status: %s | Score: %d/100",
            model_name, ev.total_trades,
            ev.win_rate * 100, ev.status, ev.score,
        )

    # ── 3. Regime quality validation ─────────────────────────────────────────
    regime_accuracy  = validate_regime_accuracy(trades)
    regime_sug       = generate_regime_suggestions(regime_accuracy)

    # ── 4. Parameter optimisation hints ──────────────────────────────────────
    sl_analysis      = analyze_sl_ranges(trades)
    session_analysis = analyze_session_timing(trades)
    direction_bias   = analyze_direction_bias(trades)
    duration_info    = analyze_duration_vs_outcome(trades)
    param_sug        = generate_parameter_suggestions(
        sl_analysis, session_analysis, direction_bias, model_evals,
    )

    # ── 5. Compile report ────────────────────────────────────────────────────
    report = compile_report(
        overall_stats=enhanced_overall,
        model_evaluations=model_evals,
        regime_accuracy=regime_accuracy,
        param_suggestions=param_sug,
        regime_suggestions=regime_sug,
        period_days=period_days,
    )
    report["duration_analysis"] = duration_info
    report["sl_analysis"]       = sl_analysis

    # ── 6. Persist to mahoraga_state ─────────────────────────────────────────
    await state.set_mahoraga_state("last_report",  json.dumps(report))
    await state.set_mahoraga_state("last_run_at",  _now())
    await state.set_mahoraga_state("last_run_result", "OK")
    await state.set_mahoraga_state("manual_trigger",  "false")

    # ── 7. Push WhatsApp alert ────────────────────────────────────────────────
    summary = format_whatsapp_summary(report, is_weekly=is_weekly)
    await state.push_alert("MAHORAGA_REPORT", summary)

    sc = report["summary_counts"]
    logger.info(
        "Analysis complete: %d CRITICAL | %d HIGH | %d MEDIUM | %d LOW",
        sc["CRITICAL"], sc["HIGH"], sc["MEDIUM"], sc["LOW"],
    )
    return report


# ---------------------------------------------------------------------------
# Schedule logic
# ---------------------------------------------------------------------------

def _should_run_daily(last_run_at: str) -> bool:
    """Return True if it's time for the daily run and we haven't run yet today."""
    now = datetime.now(timezone.utc)
    if now.hour != _DAILY_HOUR or now.minute < _DAILY_MINUTE:
        return False
    if last_run_at:
        try:
            last_dt = datetime.fromisoformat(last_run_at.replace("Z", "+00:00"))
            if last_dt.date() == now.date():
                return False   # already ran today
        except (ValueError, TypeError):
            pass
    return True


def _should_run_weekly(last_run_at: str) -> bool:
    """Return True if it's Sunday and the daily schedule window is open."""
    now = datetime.now(timezone.utc)
    if now.strftime("%A") != MAHORAGA_WEEKLY_RUN_DAY:
        return False
    return _should_run_daily(last_run_at)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def main():
    logger.info("MAHORAGA agent starting...")
    state = StateManager()
    await state.connect()

    try:
        while True:
            try:
                manual = await state.get_mahoraga_state("manual_trigger")
                if manual == "true":
                    logger.info("Manual trigger detected — running analysis now.")
                    await run_analysis(state, is_weekly=False)
                else:
                    last_run_at = await state.get_mahoraga_state("last_run_at")
                    if _should_run_weekly(last_run_at):
                        logger.info("Weekly schedule triggered.")
                        await run_analysis(state, is_weekly=True)
                    elif _should_run_daily(last_run_at):
                        logger.info("Daily schedule triggered.")
                        await run_analysis(state, is_weekly=False)

            except Exception as exc:
                logger.error("MAHORAGA cycle error: %s", exc, exc_info=True)

            await asyncio.sleep(_POLL_SECS)

    finally:
        await state.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    asyncio.run(main())
