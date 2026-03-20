"""
parameter_proposer.py — MAHORAGA skill

Generates concrete, actionable parameter proposals from statistical analysis.
Each proposal targets a specific constant in core/constants.py.

Design rules:
  - Never auto-apply anything — proposals are written to param_proposals table
    for user review via Telegram (/proposals command).
  - Proposals must be conservative: never suggest a change > 30% from current.
  - Each proposal includes: what to change, current value, suggested value,
    rationale, confidence %, expected impact.
  - Confidence is based on sample size: <30 trades = LOW, 30-80 = MEDIUM, 80+ = HIGH.

Public API
──────────
propose_from_analysis(report_data, current_constants) → list[Proposal]
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any


# ── Proposal dataclass ────────────────────────────────────────────────────────

@dataclass
class Proposal:
    proposal_id:    str
    model:          str
    parameter:      str
    current_value:  str
    proposed_value: str
    rationale:      str
    confidence:     int          # 0–100
    expected_impact: str
    priority:       str          # "LOW" | "MEDIUM" | "HIGH" | "CRITICAL"

    @classmethod
    def make(cls, model: str, parameter: str, current: Any, proposed: Any,
             rationale: str, confidence: int, impact: str,
             priority: str = "MEDIUM") -> "Proposal":
        return cls(
            proposal_id    = str(uuid.uuid4())[:8],
            model          = model,
            parameter      = parameter,
            current_value  = str(current),
            proposed_value = str(proposed),
            rationale      = rationale,
            confidence     = confidence,
            expected_impact = impact,
            priority       = priority,
        )


# ── confidence helper ─────────────────────────────────────────────────────────

def _confidence(n: int) -> int:
    """Sample-size-based confidence (0–100)."""
    if n < 20:
        return 25
    if n < 40:
        return 45
    if n < 80:
        return 65
    if n < 150:
        return 80
    return 90


# ── individual proposal generators ───────────────────────────────────────────

def _propose_zscore_threshold(
    model: str, stats: dict, current_threshold: float,
    param_name: str,
) -> list[Proposal]:
    """
    Raise or lower the OU z-score entry threshold based on outcome quality.
    Logic:
      - If win_rate < 40% and n >= 20: current threshold lets in too many bad trades → raise it.
      - If win_rate > 65% and n >= 30: good hit rate, try lowering threshold for more signals.
    """
    proposals = []
    n  = stats.get("n_trades", 0)
    wr = stats.get("win_rate", 0.0)
    conf = _confidence(n)

    if n < 20:
        return proposals

    if wr < 0.40:
        new_val = round(min(current_threshold + 0.2, current_threshold * 1.30), 2)
        proposals.append(Proposal.make(
            model     = model,
            parameter = param_name,
            current   = current_threshold,
            proposed  = new_val,
            rationale = (f"Win rate {wr:.1%} on {n} trades is below 40%. "
                         f"Tightening z-score threshold to filter weaker setups."),
            confidence = conf,
            impact    = "Fewer signals but higher quality — expected WR improvement +5–10pp.",
            priority  = "HIGH" if wr < 0.33 else "MEDIUM",
        ))

    elif wr > 0.65 and n >= 30:
        new_val = round(max(current_threshold - 0.1, current_threshold * 0.85), 2)
        proposals.append(Proposal.make(
            model     = model,
            parameter = param_name,
            current   = current_threshold,
            proposed  = new_val,
            rationale = (f"Win rate {wr:.1%} on {n} trades is strong. "
                         f"Slightly relaxing z-score to capture more signals."),
            confidence = conf,
            impact    = "More signals at similar quality — expected +15–25% trade frequency.",
            priority  = "LOW",
        ))

    return proposals


def _propose_session_limit(
    model: str, session_stats: dict, current_limit: int,
) -> list[Proposal]:
    """
    Adjust per-session trade limits based on session profitability.
    If session avg_pnl is negative and we're hitting the limit: lower it.
    """
    proposals = []
    for session, s in session_stats.items():
        n  = s.get("n", 0)
        avg = s.get("avg_pnl", 0.0)
        if n < 15:
            continue
        if avg < -1.0:
            new_limit = max(1, current_limit - 2)
            proposals.append(Proposal.make(
                model     = model,
                parameter = f"SESSION_LIMIT_{model}_{session}",
                current   = current_limit,
                proposed  = new_limit,
                rationale = (f"{model} losing on average ${avg:.2f}/trade in {session} "
                             f"({n} trades). Reducing session cap to limit exposure."),
                confidence = _confidence(n),
                impact    = f"Reduces {session} trade count by ~{current_limit - new_limit}/session.",
                priority  = "HIGH" if avg < -3.0 else "MEDIUM",
            ))
    return proposals


def _propose_hour_blocking(
    model: str, hour_heatmap: dict,
) -> list[Proposal]:
    """
    Identify loss-heavy UTC hours and propose blocking them (dead zone expansion).
    Only flags hours with >8 trades and avg_pnl < -2.0.
    """
    proposals = []
    bad_hours = []
    for hour_str, s in hour_heatmap.items():
        n   = s.get("n", 0)
        avg = s.get("avg_pnl", 0.0)
        wr  = s.get("win_rate", 0.0)
        if n >= 8 and avg < -2.0 and wr < 0.35:
            bad_hours.append((int(hour_str), n, avg, wr))

    if not bad_hours:
        return proposals

    hours_str = ", ".join(f"{h:02d}:00" for h, *_ in bad_hours)
    total_n = sum(n for _, n, *_ in bad_hours)
    proposals.append(Proposal.make(
        model     = model,
        parameter = f"DEAD_ZONE_HOURS_{model}",
        current   = "current dead zone",
        proposed  = f"block UTC hours: {hours_str}",
        rationale = (f"Trades in {hours_str} show negative expectancy ({total_n} trades). "
                     f"Adding to blocked hours will prevent systematic losses."),
        confidence = _confidence(total_n),
        impact    = f"Eliminates ~{total_n} unprofitable trades from the sample period.",
        priority  = "HIGH",
    ))
    return proposals


def _propose_direction_block(
    model: str, direction_stats: dict,
) -> list[Proposal]:
    """If one direction is severely underperforming, propose limiting it."""
    proposals = []
    for direction, s in direction_stats.items():
        n  = s.get("n", 0)
        wr = s.get("win_rate", 0.0)
        avg = s.get("avg_pnl", 0.0)
        if n < 20:
            continue
        if wr < 0.30 and avg < -2.0:
            proposals.append(Proposal.make(
                model     = model,
                parameter = f"ALLOW_{direction}_{model}",
                current   = "enabled",
                proposed  = "disabled",
                rationale = (f"{model} {direction} has {wr:.1%} WR on {n} trades "
                             f"(avg ${avg:.2f}). This direction is systematically losing."),
                confidence = _confidence(n),
                impact    = f"Removes all {direction} trades for {model}.",
                priority  = "CRITICAL" if wr < 0.20 else "HIGH",
            ))
    return proposals


def _propose_breakeven_threshold(
    model: str, stats: dict, current_threshold: float,
) -> list[Proposal]:
    """
    If the model has many BREAKEVENs that subsequently would have been wins:
    suggest lowering breakeven trigger from current 1.5× ATR.
    Uses avg_duration as proxy: long-duration wins suggest trades are running well.
    """
    proposals = []
    n = stats.get("n_trades", 0)
    if n < 30:
        return proposals

    avg_dur  = stats.get("avg_duration_mins", 0.0)
    avg_win  = stats.get("avg_win", 0.0)
    if avg_dur > 120 and avg_win > 0 and current_threshold > 1.0:
        # Trades running long and winning — breakeven may be getting triggered too early
        new_val = round(current_threshold + 0.2, 1)
        proposals.append(Proposal.make(
            model     = model,
            parameter = "BREAKEVEN_ATR_THRESHOLD",
            current   = current_threshold,
            proposed  = new_val,
            rationale = (f"{model} avg winning duration {avg_dur:.0f} min suggests trades "
                         f"need room to run. Raising BE threshold reduces premature exits."),
            confidence = _confidence(n),
            impact    = "Fewer breakevens, more wins reaching TP. Risk: more losses if SL not moved.",
            priority  = "LOW",
        ))
    return proposals


# ── main entry point ──────────────────────────────────────────────────────────

def propose_from_analysis(report_data: dict) -> list[Proposal]:
    """
    Generate parameter proposals from a compiled MAHORAGA report dict.

    Expected report_data structure (from adaptation_reporter.compile_report()):
    {
        "by_model": {
            "OU_GRIND": {
                "stats": {...},
                "hour_heatmap": {...},
                "direction_breakdown": {...},
                "session_breakdown": {...},
            },
            ...
        },
        "constants": {
            "OU_ZSCORE_GRIND_THRESHOLD": 0.9,
            "OU_ZSCORE_ENTRY_THRESHOLD": 1.3,
            "BREAKEVEN_ATR_THRESHOLD":   1.5,
            "M5_MAX_TRADES_PER_SESSION": 8,
            "LONDON_REVERSAL_MAX_TRADES_PER_SESSION": 2,
        }
    }
    """
    proposals: list[Proposal] = []
    by_model  = report_data.get("by_model", {})
    constants = report_data.get("constants", {})

    from core.constants import (  # noqa: E402 — import inside fn to allow mocking in tests
        MODEL_A, MODEL_B,
        OU_ZSCORE_GRIND_THRESHOLD,
        OU_ZSCORE_ENTRY_THRESHOLD,
        BREAKEVEN_ATR_THRESHOLD,
        M5_MAX_TRADES_PER_SESSION,
        LONDON_REVERSAL_MAX_TRADES_PER_SESSION,
    )

    param_map = {
        MODEL_A: {
            "zscore_primary":  constants.get("OU_ZSCORE_GRIND_THRESHOLD",  OU_ZSCORE_GRIND_THRESHOLD),
            "zscore_fallback": constants.get("OU_ZSCORE_ENTRY_THRESHOLD",  OU_ZSCORE_ENTRY_THRESHOLD),
            "session_limit":   constants.get("M5_MAX_TRADES_PER_SESSION",  M5_MAX_TRADES_PER_SESSION),
        },
        MODEL_B: {
            "session_limit":   constants.get("LONDON_REVERSAL_MAX_TRADES_PER_SESSION",
                                             LONDON_REVERSAL_MAX_TRADES_PER_SESSION),
        },
    }
    be_threshold = constants.get("BREAKEVEN_ATR_THRESHOLD", BREAKEVEN_ATR_THRESHOLD)

    for model, model_data in by_model.items():
        stats     = model_data.get("stats", {})
        heatmap   = model_data.get("hour_heatmap", {})
        dir_stats = model_data.get("direction_breakdown", {})
        sess_stats= model_data.get("session_breakdown", {})
        pm        = param_map.get(model, {})

        # Z-score proposals (Model A only)
        if model == MODEL_A:
            proposals += _propose_zscore_threshold(
                model, stats,
                pm.get("zscore_primary", OU_ZSCORE_GRIND_THRESHOLD),
                "OU_ZSCORE_GRIND_THRESHOLD",
            )
            proposals += _propose_zscore_threshold(
                model, stats,
                pm.get("zscore_fallback", OU_ZSCORE_ENTRY_THRESHOLD),
                "OU_ZSCORE_ENTRY_THRESHOLD",
            )

        # Session limit proposals
        limit = pm.get("session_limit", M5_MAX_TRADES_PER_SESSION)
        proposals += _propose_session_limit(model, sess_stats, limit)

        # Hour blocking
        proposals += _propose_hour_blocking(model, heatmap)

        # Direction block
        proposals += _propose_direction_block(model, dir_stats)

        # Breakeven threshold
        proposals += _propose_breakeven_threshold(model, stats, be_threshold)

    # Deduplicate by parameter (keep highest priority)
    priority_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    seen: dict[str, Proposal] = {}
    for p in proposals:
        key = f"{p.model}_{p.parameter}"
        if key not in seen or priority_order[p.priority] < priority_order[seen[key].priority]:
            seen[key] = p

    return sorted(seen.values(), key=lambda p: priority_order[p.priority])
