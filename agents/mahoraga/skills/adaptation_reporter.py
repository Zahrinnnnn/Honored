"""
adaptation_reporter.py — MAHORAGA skill

Compiles the full analysis report and formats it for Telegram delivery.
No LLM. No auto-apply. Pure data → formatted text.

Public API
──────────
compile_report(trades, period_days)  → dict   (full structured JSON)
format_telegram_summary(report)      → str    (Telegram digest, max ~900 chars)
format_proposals_message(proposals)  → str    (numbered list of proposals)
format_drift_alert(model, result)    → str    (immediate drift push alert)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from agents.mahoraga.skills.drift_detector import (
    DriftResult,
    check_all_models_drift,
)
from agents.mahoraga.skills.feature_analyzer import (
    detrend_breakdown,
    direction_breakdown,
    hour_heatmap,
    session_breakdown,
    zscore_bucket_analysis,
)
from agents.mahoraga.skills.parameter_proposer import Proposal, propose_from_analysis
from agents.mahoraga.skills.regime_profiler import (
    h4_bias_breakdown,
    regime_quality_summary,
    session_regime_matrix,
)
from agents.mahoraga.skills.statistical_engine import compute_model_stats, compute_stats
from core.constants import (
    BREAKEVEN_ATR_THRESHOLD,
    M5_MAX_TRADES_PER_SESSION,
    MODEL_A,
    OU_ZSCORE_ENTRY_THRESHOLD,
    OU_ZSCORE_GRIND_THRESHOLD,
)


# ── compile full report ───────────────────────────────────────────────────────

def compile_report(
    trades: Sequence[dict],
    period_days: int,
    cusum_state: dict[str, float] | None = None,
) -> dict:
    """
    Run all analysis modules and produce a single structured report dict.

    Args:
        trades:      Closed trade dicts for the period (from state_manager).
        period_days: How many days of data are included.
        cusum_state: {model: cusum_value} loaded from mahoraga_state (for drift).

    Returns:
        Full report dict — stored in mahoraga_state.last_report.
    """
    closed = [t for t in trades if t.get("result")]
    models = [MODEL_A]

    # ── Overall stats ─────────────────────────────────────────────────────────
    overall = compute_stats(closed)

    # ── Per-model stats + features ────────────────────────────────────────────
    by_model: dict[str, dict] = {}
    for model in models:
        model_trades = [t for t in closed if t.get("model") == model]
        by_model[model] = {
            "stats":               compute_model_stats(closed, model),
            "hour_heatmap":        hour_heatmap(model_trades),
            "direction_breakdown": direction_breakdown(model_trades),
            "session_breakdown":   session_breakdown(model_trades),
            "detrend_breakdown":   detrend_breakdown(model_trades),
            "zscore_buckets":      zscore_bucket_analysis(model_trades),
            "h4_bias_breakdown":   h4_bias_breakdown(model_trades),
        }

    # ── Drift detection ───────────────────────────────────────────────────────
    target_wrs: dict[str, float] = {}
    for model in models:
        s = by_model[model]["stats"]
        # Use historical WR as target if enough data, else default 0.50
        target_wrs[model] = s["win_rate"] if s["n_trades"] >= 30 else 0.50

    drift_results = check_all_models_drift(
        trades      = closed,
        model_names = models,
        target_wrs  = target_wrs,
        cusum_state = cusum_state or {},
    )
    drift_summary = {
        model: {
            "is_drifting":     r.is_drifting,
            "cusum":           r.cusum,
            "severity":        r.severity,
            "recent_win_rate": r.recent_win_rate,
            "target_win_rate": r.target_win_rate,
            "n_trades":        r.n_trades,
            "message":         r.message,
        }
        for model, r in drift_results.items()
    }

    # ── Regime quality ────────────────────────────────────────────────────────
    regime_ranking = regime_quality_summary(closed)
    sess_regime    = session_regime_matrix(closed)

    # ── Parameter proposals ───────────────────────────────────────────────────
    constants_snapshot = {
        "OU_ZSCORE_GRIND_THRESHOLD":              OU_ZSCORE_GRIND_THRESHOLD,
        "OU_ZSCORE_ENTRY_THRESHOLD":              OU_ZSCORE_ENTRY_THRESHOLD,
        "BREAKEVEN_ATR_THRESHOLD":                BREAKEVEN_ATR_THRESHOLD,
        "M5_MAX_TRADES_PER_SESSION":              M5_MAX_TRADES_PER_SESSION,
    }
    report_data = {
        "by_model":  by_model,
        "constants": constants_snapshot,
    }
    proposals = propose_from_analysis(report_data)

    # ── Assemble ──────────────────────────────────────────────────────────────
    return {
        "generated_at":    datetime.now(timezone.utc).isoformat(),
        "period_days":     period_days,
        "n_closed_trades": len(closed),
        "overall":         overall,
        "by_model":        by_model,
        "drift":           drift_summary,
        "regime_ranking":  regime_ranking[:10],   # top 10 rows
        "session_regime":  sess_regime,
        "proposals":       [_proposal_to_dict(p) for p in proposals],
        "constants":       constants_snapshot,
    }


def _proposal_to_dict(p: Proposal) -> dict:
    return {
        "proposal_id":    p.proposal_id,
        "model":          p.model,
        "parameter":      p.parameter,
        "current_value":  p.current_value,
        "proposed_value": p.proposed_value,
        "rationale":      p.rationale,
        "confidence":     p.confidence,
        "expected_impact":p.expected_impact,
        "priority":       p.priority,
    }


# ── Telegram summary formatter ────────────────────────────────────────────────

_STATUS_ICON = {
    "NONE":     "✅",
    "WATCH":    "👀",
    "ALERT":    "⚠️",
    "CRITICAL": "🚨",
}

_MODEL_LABEL = {
    MODEL_A: "A (OU)",
}


def format_telegram_summary(report: dict) -> str:
    """
    JARVIS-style Telegram digest. Concise, actionable.
    Plain text — no markdown tables, works in all Telegram clients.
    """
    ov    = report.get("overall", {})
    n     = ov.get("n_trades",  0)
    wr    = ov.get("win_rate",  0.0)
    pnl   = ov.get("total_pnl", 0.0)
    sharp = ov.get("sharpe",    0.0)
    days  = report.get("period_days", 0)
    gen   = report.get("generated_at", "")[:10]

    lines = [
        f"📊 MAHORAGA Report — {gen} ({days}d)",
        f"Trades: {n} | WR: {wr:.1%} | P&L: ${pnl:+.2f} | Sharpe: {sharp:.2f}",
        "",
    ]

    # Per-model row
    for model in [MODEL_A]:
        label = _MODEL_LABEL.get(model, model)
        ms    = report.get("by_model", {}).get(model, {}).get("stats", {})
        drift = report.get("drift", {}).get(model, {})
        mn    = ms.get("n_trades",  0)
        mwr   = ms.get("win_rate",  0.0)
        mpnl  = ms.get("total_pnl", 0.0)
        sev   = drift.get("severity", "NONE")
        icon  = _STATUS_ICON.get(sev, "❓")
        lines.append(f"{icon} {label}: {mn}t | WR {mwr:.1%} | ${mpnl:+.2f}")

    # Drift alerts
    alerts = [
        r for r in report.get("drift", {}).values()
        if r.get("severity") in ("ALERT", "CRITICAL")
    ]
    if alerts:
        lines.append("")
        lines.append("🚨 Drift detected:")
        for r in alerts:
            lines.append(f"  {r['message']}")

    # Top proposal
    proposals = report.get("proposals", [])
    if proposals:
        p = proposals[0]
        lines.append("")
        lines.append(f"💡 Top suggestion [{p['priority']}]: {p['parameter']}")
        lines.append(f"   {p['current_value']} → {p['proposed_value']}")
        lines.append(f"   {p['rationale'][:100]}...")
        n_more = len(proposals) - 1
        if n_more > 0:
            lines.append(f"   (+{n_more} more — /proposals to review)")

    # Best/worst regime
    ranking = report.get("regime_ranking", [])
    if len(ranking) >= 2:
        best  = ranking[0]
        worst = ranking[-1]
        lines.append("")
        lines.append(
            f"🏆 Best: {best['model']} {best['regime']} {best['direction']} "
            f"WR {best['win_rate']:.1%}"
        )
        lines.append(
            f"💀 Worst: {worst['model']} {worst['regime']} {worst['direction']} "
            f"WR {worst['win_rate']:.1%}"
        )

    lines.append("")
    lines.append("Apply any change? /proposals")
    return "\n".join(lines)


def format_proposals_message(proposals: list[dict]) -> str:
    """Numbered list of proposals for /proposals Telegram command."""
    if not proposals:
        return "No parameter proposals at this time. System looks stable."

    lines = [f"Parameter Proposals ({len(proposals)} total)\n"]
    for i, p in enumerate(proposals, 1):
        icon = {
            "CRITICAL": "🚨", "HIGH": "⚠️", "MEDIUM": "💡", "LOW": "ℹ️"
        }.get(p["priority"], "")
        lines.append(
            f"{i}. {icon} [{p['priority']}] {p['parameter']}\n"
            f"   {p['current_value']} → {p['proposed_value']}\n"
            f"   {p['rationale'][:100]}...\n"
            f"   Confidence: {p['confidence']}%"
        )
    lines.append("\nNone of these are applied automatically. Update constants.py manually.")
    return "\n".join(lines)


def format_drift_alert(model: str, result: DriftResult) -> str:
    """Single-model immediate drift alert for push notification."""
    return (
        f"⚠️ Drift Alert — {model}\n"
        f"Recent WR: {result.recent_win_rate:.1%} (target: {result.target_win_rate:.1%})\n"
        f"CUSUM: {result.cusum:.2f} (threshold: 3.0)\n"
        f"Severity: {result.severity}\n"
        f"Trades sampled: {result.n_trades}\n"
        f"Review parameters: /proposals"
    )
