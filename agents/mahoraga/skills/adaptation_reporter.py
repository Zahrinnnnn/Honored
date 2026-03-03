"""
adaptation_reporter.py — MAHORAGA skill

Compiles all analysis results into a structured report and formats
a JARVIS-style WhatsApp summary for delivery via alert_queue.

Output artefacts:
  - Full JSON report dict   → stored in mahoraga_state.last_report
  - WhatsApp summary string → pushed to alert_queue as MAHORAGA_REPORT
  - Sorted Recommendation list (CRITICAL > HIGH > MEDIUM > LOW)

Priority levels:
  CRITICAL — drift detected or score < 30; requires immediate attention
  HIGH     — statistically significant underperformance
  MEDIUM   — marginal performance, regime concerns, early warning
  LOW      — informational; outperform notes, no action needed

Public API
──────────
compile_report(overall_stats, model_evaluations, regime_accuracy,
               param_suggestions, regime_suggestions, period_days)  → dict
format_whatsapp_summary(report, is_weekly)                          → str
"""

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Recommendation:
    priority:        str   # CRITICAL | HIGH | MEDIUM | LOW
    model:           str
    type:            str   # UNDERPERFORM | OUTPERFORM | DRIFT | REGIME | PARAM | INFO
    finding:         str
    suggestion:      str
    confidence_pct:  int   # 0–100
    expected_impact: str


# ---------------------------------------------------------------------------
# Report compiler
# ---------------------------------------------------------------------------

def compile_report(
    overall_stats:     dict,
    model_evaluations: list,
    regime_accuracy:   dict,
    param_suggestions: list,
    regime_suggestions: list,
    period_days:       int = 30,
) -> dict:
    """Compile all analysis into a single structured report dict.

    This is what gets stored in mahoraga_state.last_report (as JSON).
    """
    now  = datetime.now(timezone.utc).isoformat()
    recs = _build_recommendations(
        model_evaluations, regime_accuracy, param_suggestions, regime_suggestions
    )
    recs_sorted = sorted(recs, key=lambda r: _priority_order(r.priority))

    counts = {
        "CRITICAL": sum(1 for r in recs if r.priority == "CRITICAL"),
        "HIGH":     sum(1 for r in recs if r.priority == "HIGH"),
        "MEDIUM":   sum(1 for r in recs if r.priority == "MEDIUM"),
        "LOW":      sum(1 for r in recs if r.priority == "LOW"),
    }

    return {
        "generated_at":    now,
        "period_days":     period_days,
        "overall":         overall_stats,
        "by_model":        {e.model: _eval_to_dict(e) for e in model_evaluations},
        "regime_accuracy": regime_accuracy,
        "recommendations": [asdict(r) for r in recs_sorted],
        "param_hints":     param_suggestions,
        "summary_counts":  counts,
    }


# ---------------------------------------------------------------------------
# WhatsApp formatter (JARVIS tone)
# ---------------------------------------------------------------------------

def format_whatsapp_summary(report: dict, is_weekly: bool = False) -> str:
    """Produce a concise JARVIS-style WhatsApp message from the report.

    Full details live in mahoraga_state.last_report — this is the digest.
    """
    overall = report.get("overall", {})
    counts  = report.get("summary_counts", {})
    period  = report.get("period_days", 30)

    lines = []
    label = "Weekly Deep Dive" if is_weekly else "Daily Analysis"
    lines.append(f"*MAHORAGA — {label}*")
    lines.append(
        f"Last {period} days · {overall.get('total_trades', 0)} trades · "
        f"WR {overall.get('win_rate', 0):.0%} · P&L ${overall.get('total_pnl', 0):+.2f}"
    )

    sr  = overall.get("sharpe_ratio", 0)
    mdd = overall.get("max_drawdown_pct", 0)
    pf  = overall.get("profit_factor", 0)
    lines.append(f"Sharpe {sr:.2f} · Max DD {mdd:.1f}% · PF {pf:.2f}")

    # Per-model quick status
    by_model = report.get("by_model", {})
    if by_model:
        lines.append("")
        for model, ev in by_model.items():
            status = ev.get("status", "?")
            m_wr   = ev.get("win_rate", 0)
            score  = ev.get("score", 0)
            emoji  = _status_emoji(status)
            short  = _short_model_name(model)
            lines.append(f"{emoji} {short}: {m_wr:.0%} WR · {score}/100 · {status}")

    # Recommendation summary
    critical = counts.get("CRITICAL", 0)
    high     = counts.get("HIGH", 0)
    medium   = counts.get("MEDIUM", 0)
    low      = counts.get("LOW", 0)

    lines.append("")
    if critical > 0:
        lines.append(f"🚨 {critical} CRITICAL — action warranted.")
    if high > 0:
        lines.append(f"⚠️  {high} HIGH priority finding(s).")
    if medium > 0:
        lines.append(f"📊 {medium} MEDIUM — worth watching.")
    if low > 0 and not (critical or high or medium):
        lines.append(f"✅ {low} informational notes — system healthy.")

    # Top finding
    recs = report.get("recommendations", [])
    if recs:
        top = recs[0]
        lines.append(f"\nTop finding: {top['finding']}")
        if top.get("suggestion"):
            lines.append(f"Suggestion: {top['suggestion']}")

    lines.append("")
    lines.append("_(All recommendations require your approval before any change is made.)_")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Recommendation builder
# ---------------------------------------------------------------------------

def _build_recommendations(
    model_evals:       list,
    regime_accuracy:   dict,
    param_suggestions: list,
    regime_suggestions: list,
) -> list:
    recs = []

    for ev in model_evals:
        if ev.status == "INSUFFICIENT_DATA":
            continue

        if ev.status == "DRIFT":
            recs.append(Recommendation(
                priority="CRITICAL",
                model=ev.model,
                type="DRIFT",
                finding=ev.findings[0] if ev.findings else "Performance drift detected.",
                suggestion=ev.recommendation or "Consider pausing this model.",
                confidence_pct=_drift_confidence(ev.drift),
                expected_impact="Prevent continued losses from regime shift",
            ))

        elif ev.status == "UNDERPERFORM" and ev.significance.get("significant"):
            recs.append(Recommendation(
                priority="HIGH",
                model=ev.model,
                type="UNDERPERFORM",
                finding=ev.findings[0] if ev.findings else "Statistically significant underperformance.",
                suggestion=ev.recommendation or "Tighten entry criteria.",
                confidence_pct=max(50, int((1 - ev.significance["p_value"]) * 100)),
                expected_impact="Improve win rate by filtering weaker signal entries",
            ))

        elif ev.status == "UNDERPERFORM":
            recs.append(Recommendation(
                priority="MEDIUM",
                model=ev.model,
                type="UNDERPERFORM",
                finding=f"{_short_model_name(ev.model)} win rate {ev.win_rate:.0%} — below threshold, not yet significant.",
                suggestion="Monitor for 20 more trades before adjusting parameters.",
                confidence_pct=40,
                expected_impact="Early warning — accumulate more data",
            ))

        elif ev.status == "OUTPERFORM":
            recs.append(Recommendation(
                priority="LOW",
                model=ev.model,
                type="OUTPERFORM",
                finding=ev.findings[0] if ev.findings else "Model outperforming.",
                suggestion=ev.recommendation or "Maintain current parameters.",
                confidence_pct=max(50, int((1 - ev.significance["p_value"]) * 100)),
                expected_impact="No action needed — performing above expectations",
            ))

        elif ev.kelly_fraction <= 0:
            recs.append(Recommendation(
                priority="MEDIUM",
                model=ev.model,
                type="INFO",
                finding=f"{_short_model_name(ev.model)}: Kelly fraction ≤ 0 — no mathematical edge at current win rate.",
                suggestion="Review entry conditions; edge may have degraded.",
                confidence_pct=55,
                expected_impact="Prevent trading without positive expectancy",
            ))

    # Regime suggestions
    for sug in regime_suggestions:
        if "poor" in sug.lower() or "recalibrat" in sug.lower():
            recs.append(Recommendation(
                priority="MEDIUM",
                model="REGIME",
                type="REGIME",
                finding="Regime detection accuracy below expectations.",
                suggestion=sug,
                confidence_pct=60,
                expected_impact="Better regime classification → higher signal quality",
            ))

    # Parameter hints (informational)
    for sug in param_suggestions:
        if "no statistically" not in sug.lower():
            recs.append(Recommendation(
                priority="LOW",
                model="PARAMS",
                type="PARAM",
                finding="Parameter optimisation opportunity identified.",
                suggestion=sug,
                confidence_pct=50,
                expected_impact="Marginal improvement in signal quality",
            ))

    return recs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _drift_confidence(drift: dict) -> int:
    delta = abs(drift.get("delta", 0))
    if delta >= 0.30:
        return 90
    if delta >= 0.20:
        return 75
    return 60


def _priority_order(p: str) -> int:
    return {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}.get(p, 4)


def _status_emoji(status: str) -> str:
    return {
        "HEALTHY":           "✅",
        "OUTPERFORM":        "🚀",
        "UNDERPERFORM":      "⚠️",
        "DRIFT":             "🔴",
        "INSUFFICIENT_DATA": "⏳",
    }.get(status, "❓")


def _short_model_name(model: str) -> str:
    return {
        "M5_MOMENTUM":    "Model A",
        "M1_MEANREV":     "Model B",
        "LONDON_BREAKOUT":"Model C",
    }.get(model, model)


def _eval_to_dict(ev) -> dict:
    """Convert ModelEvaluation dataclass to plain dict (JSON-safe)."""
    return {
        "total_trades":   ev.total_trades,
        "win_rate":       ev.win_rate,
        "total_pnl":      ev.total_pnl,
        "kelly_fraction": ev.kelly_fraction,
        "status":         ev.status,
        "significance":   ev.significance,
        "drift":          ev.drift,
        "score":          ev.score,
        "findings":       ev.findings,
        "recommendation": ev.recommendation,
    }
