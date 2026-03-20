"""
mahoraga/agent.py — The Learning Agent (rebuilt)

Scheduled runs:
  Daily  — 21:30 GMT (after NY close): 30-day review
  Weekly — Sunday same time: 90-day deep review

On-demand triggers:
  1. Manual trigger: mahoraga_state.manual_trigger == "true"
     (set by trigger_mahoraga.py Telegram tool script)
  2. Micro-trigger: every 5 completed trades, run a quick drift check.
     If CUSUM crosses threshold for any model → immediate alert.

Safety:
  - NEVER auto-applies parameter changes.
  - All output goes to alert_queue → GOJO Telegram bot.
  - Min MIN_TRADES_FOR_ANALYSIS trades required for full analysis.
  - Drift alert fires immediately even with <30 trades (threshold: 10).

Poll cadence: 60 seconds.
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
    MODEL_A,
    MODEL_B,
)
from core.state_manager import StateManager

from agents.mahoraga.skills.adaptation_reporter import (
    compile_report,
    format_drift_alert,
    format_telegram_summary,
)
from agents.mahoraga.skills.drift_detector import (
    DriftResult,
    update_cusum,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MAHORAGA] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_POLL_SECS     = 60
_DAILY_HOUR    = int(MAHORAGA_DAILY_RUN_TIME.split(":")[0])
_DAILY_MINUTE  = int(MAHORAGA_DAILY_RUN_TIME.split(":")[1])
_MICRO_TRIGGER_EVERY = 5   # run drift check every N new completed trades
_PAPER_MODE    = os.environ.get("PAPER_MODE", "true").lower() == "true"
_MODELS        = [MODEL_A, MODEL_B]


# ── schedule helpers ──────────────────────────────────────────────────────────

def _should_run_daily(last_run_at: str) -> bool:
    now = datetime.now(timezone.utc)
    if now.hour != _DAILY_HOUR or now.minute < _DAILY_MINUTE:
        return False
    if last_run_at:
        try:
            last_dt = datetime.fromisoformat(last_run_at.replace("Z", "+00:00"))
            if last_dt.date() == now.date():
                return False
        except (ValueError, TypeError):
            pass
    return True


def _should_run_weekly(last_run_at: str) -> bool:
    if datetime.now(timezone.utc).strftime("%A") != MAHORAGA_WEEKLY_RUN_DAY:
        return False
    return _should_run_daily(last_run_at)


# ── CUSUM state helpers ───────────────────────────────────────────────────────

async def _load_cusum_state(state: StateManager) -> dict[str, float]:
    raw = await state.get_mahoraga_state("cusum_state")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {}


async def _save_cusum_state(state: StateManager, cusum: dict[str, float]):
    await state.set_mahoraga_state("cusum_state", json.dumps(cusum))


async def _load_trade_count_seen(state: StateManager) -> int:
    raw = await state.get_mahoraga_state("trade_count_seen")
    try:
        return int(raw) if raw else 0
    except (ValueError, TypeError):
        return 0


async def _save_trade_count_seen(state: StateManager, count: int):
    await state.set_mahoraga_state("trade_count_seen", str(count))


# ── micro drift check (every N trades) ───────────────────────────────────────

async def _check_micro_trigger(state: StateManager):
    """
    Check if N new trades completed since last check.
    If so, update CUSUM and fire immediate drift alert if threshold crossed.
    """
    trades = await state.get_trades_by_period(days=30, paper=_PAPER_MODE)
    closed = [t for t in trades if t.get("result")]
    current_count = len(closed)

    seen_count = await _load_trade_count_seen(state)
    new_trades  = current_count - seen_count

    if new_trades < _MICRO_TRIGGER_EVERY:
        return   # not enough new trades yet

    logger.info("Micro-trigger: %d new trades completed — running drift check.", new_trades)

    cusum_state = await _load_cusum_state(state)

    # Load baseline WRs from last full report (if available)
    last_report_raw = await state.get_mahoraga_state("last_report")
    target_wrs: dict[str, float] = {}
    if last_report_raw:
        try:
            last_report = json.loads(last_report_raw)
            for model in _MODELS:
                s = last_report.get("by_model", {}).get(model, {}).get("stats", {})
                if s.get("n_trades", 0) >= 30:
                    target_wrs[model] = s["win_rate"]
        except (ValueError, TypeError):
            pass

    # Run per-trade CUSUM update only on the NEW trades (since last seen)
    new_trade_batch = closed[-new_trades:] if new_trades <= len(closed) else closed
    for model in _MODELS:
        model_batch = [t for t in new_trade_batch if t.get("model") == model]
        current_cusum = cusum_state.get(model, 0.0)
        target_wr = target_wrs.get(model, 0.50)
        for t in model_batch:
            new_cusum, drifting = update_cusum(current_cusum, t.get("result", ""), target_wr)
            current_cusum = new_cusum
            if drifting:
                logger.warning("CUSUM drift detected for %s — alerting.", model)
                # Build a lightweight DriftResult for the alert
                model_all = [t2 for t2 in closed if t2.get("model") == model]
                n = len(model_all)
                wins = sum(1 for t2 in model_all if (t2.get("result") or "").upper() == "WIN")
                recent_wr = wins / n if n > 0 else 0.0
                result = DriftResult(
                    model=model, is_drifting=True, cusum=round(current_cusum, 4),
                    n_trades=n, recent_win_rate=round(recent_wr, 4),
                    target_win_rate=target_wr, severity="ALERT",
                    message=f"{model}: CUSUM={current_cusum:.2f} — drift detected",
                )
                msg = format_drift_alert(model, result)
                await state.push_alert("MAHORAGA_DRIFT", msg)
        cusum_state[model] = round(current_cusum, 4)

    await _save_cusum_state(state, cusum_state)
    await _save_trade_count_seen(state, current_count)
    logger.info("Micro drift check complete. CUSUM: %s", cusum_state)


# ── full analysis pipeline ────────────────────────────────────────────────────

async def run_analysis(state: StateManager, is_weekly: bool = False) -> dict:
    """
    Full MAHORAGA analysis: stats + features + drift + proposals.
    Writes report to mahoraga_state and pushes Telegram digest.
    """
    period_days = 90 if is_weekly else 30
    logger.info(
        "Starting %s analysis (last %d days)...",
        "weekly" if is_weekly else "daily", period_days,
    )

    trades = await state.get_trades_by_period(days=period_days, paper=_PAPER_MODE)
    closed = [t for t in trades if t.get("result")]
    logger.info("Loaded %d completed trades", len(closed))

    if len(closed) < MIN_TRADES_FOR_ANALYSIS:
        logger.info(
            "Insufficient trades (%d/%d) — skipping full analysis.",
            len(closed), MIN_TRADES_FOR_ANALYSIS,
        )
        await state.set_mahoraga_state("last_run_result", "SKIPPED_INSUFFICIENT_DATA")
        await state.set_mahoraga_state("last_run_at",     _now())
        await state.set_mahoraga_state("manual_trigger",  "false")
        return {}

    cusum_state = await _load_cusum_state(state)

    # ── Run analysis ──────────────────────────────────────────────────────────
    report = compile_report(trades=closed, period_days=period_days, cusum_state=cusum_state)

    # ── Update CUSUM state from report ────────────────────────────────────────
    new_cusum: dict[str, float] = {}
    for model in _MODELS:
        new_cusum[model] = report.get("drift", {}).get(model, {}).get("cusum", 0.0)
    await _save_cusum_state(state, new_cusum)
    await _save_trade_count_seen(state, len(closed))

    # ── Persist proposals to param_proposals table ────────────────────────────
    for p in report.get("proposals", []):
        try:
            await state.save_param_proposal(
                proposal_id    = p["proposal_id"],
                model          = p["model"],
                parameter      = p["parameter"],
                current_value  = p["current_value"],
                proposed_value = p["proposed_value"],
                rationale      = p["rationale"],
                confidence     = p["confidence"],
                expected_impact= p["expected_impact"],
                priority       = p["priority"],
            )
        except Exception as exc:
            logger.warning("Failed to save proposal %s: %s", p.get("proposal_id"), exc)

    # ── Persist report ────────────────────────────────────────────────────────
    await state.set_mahoraga_state("last_report",     json.dumps(report))
    await state.set_mahoraga_state("last_run_at",     _now())
    await state.set_mahoraga_state("last_run_result", "OK")
    await state.set_mahoraga_state("manual_trigger",  "false")

    # ── Push Telegram summary ─────────────────────────────────────────────────
    summary = format_telegram_summary(report)
    await state.push_alert("MAHORAGA_REPORT", summary)

    n_props = len(report.get("proposals", []))
    logger.info(
        "Analysis complete. Trades=%d | Proposals=%d | WR=%.1f%%",
        len(closed), n_props,
        report.get("overall", {}).get("win_rate", 0.0) * 100,
    )
    return report


# ── main loop ─────────────────────────────────────────────────────────────────

async def main():
    logger.info("MAHORAGA agent starting...")
    state = StateManager()
    await state.connect()

    try:
        while True:
            try:
                manual = await state.get_mahoraga_state("manual_trigger")
                if manual == "true":
                    logger.info("Manual trigger — running analysis.")
                    await run_analysis(state, is_weekly=False)
                else:
                    last_run_at = await state.get_mahoraga_state("last_run_at")
                    if _should_run_weekly(last_run_at):
                        logger.info("Weekly schedule triggered.")
                        await run_analysis(state, is_weekly=True)
                    elif _should_run_daily(last_run_at):
                        logger.info("Daily schedule triggered.")
                        await run_analysis(state, is_weekly=False)
                    else:
                        # Micro-trigger: check if N new trades need a drift scan
                        await _check_micro_trigger(state)

            except Exception as exc:
                logger.error("MAHORAGA cycle error: %s", exc, exc_info=True)

            await asyncio.sleep(_POLL_SECS)

    finally:
        await state.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    asyncio.run(main())
