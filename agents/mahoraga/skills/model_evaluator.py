"""
model_evaluator.py — MAHORAGA skill

Evaluates each trading model's health and statistical edge using:
  - Binomial significance test (scipy.stats.binomtest) — is the win rate
    meaningfully different from 50% chance, or just noise?
  - Kelly criterion   — is there a positive mathematical edge?
  - Drift detection   — has recent performance diverged from historical?
  - Composite score 0–100 that summarises model health

The evaluator never touches live data — it consumes finished trade dicts.

Public API
──────────
evaluate_model(model_name, trades) → ModelEvaluation
binomial_significance(wins, total, null_p=0.50) → dict
kelly_fraction(win_rate, rr=3.0) → float
detect_drift(trades, lookback=20) → dict
"""

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── Thresholds ──────────────────────────────────────────────────────────────
_MIN_TRADES        = 10     # minimum trades needed for statistical analysis
_UNDERPERFORM_WR   = 0.30   # below this → underperforming
_OUTPERFORM_WR     = 0.55   # above this → outperforming
_RR_RATIO          = 3.0    # fixed 1:3 RR used across the system
_DRIFT_LOOKBACK    = 20     # recent window for drift comparison
_DRIFT_THRESHOLD   = 0.15   # recent_wr < hist_wr − 0.15 → drift alert
_SIGNIFICANCE_LVL  = 0.05   # p-value threshold for statistical tests


@dataclass
class ModelEvaluation:
    model:          str
    total_trades:   int
    win_rate:       float
    total_pnl:      float
    kelly_fraction: float
    status:         str           # HEALTHY | UNDERPERFORM | OUTPERFORM | DRIFT | INSUFFICIENT_DATA
    significance:   dict          # {p_value, significant, test_used}
    drift:          dict          # {detected, recent_wr, historical_wr, delta}
    score:          int           # 0–100 composite health score
    findings:       list          # human-readable finding strings
    recommendation: Optional[str] # specific, actionable suggestion


# ---------------------------------------------------------------------------
# Primary evaluator
# ---------------------------------------------------------------------------

def evaluate_model(model_name: str, trades: list) -> ModelEvaluation:
    """Full statistical evaluation of a single trading model.

    Args:
        model_name: e.g. "M5_MOMENTUM", "M1_MEANREV", "LONDON_BREAKOUT"
        trades:     completed trades for this model only (result IS NOT NULL)
    """
    if len(trades) < _MIN_TRADES:
        return ModelEvaluation(
            model=model_name,
            total_trades=len(trades),
            win_rate=0.0,
            total_pnl=0.0,
            kelly_fraction=0.0,
            status="INSUFFICIENT_DATA",
            significance={"p_value": 1.0, "significant": False, "test_used": "none"},
            drift={"detected": False, "recent_wr": 0.0, "historical_wr": 0.0, "delta": 0.0},
            score=50,
            findings=[f"Only {len(trades)} trades — need {_MIN_TRADES} minimum for analysis."],
            recommendation=None,
        )

    wins      = sum(1 for t in trades if t.get("result") == "WIN")
    total_pnl = round(sum(float(t.get("pnl") or 0) for t in trades), 2)
    win_rate  = wins / len(trades)

    kelly  = kelly_fraction(win_rate, _RR_RATIO)
    sig    = binomial_significance(wins, len(trades))
    drift  = detect_drift(trades, lookback=_DRIFT_LOOKBACK)

    findings: list = []
    status = "HEALTHY"

    # ── Drift takes priority — recent degradation regardless of overall stats
    if drift["detected"]:
        status = "DRIFT"
        findings.append(
            f"Performance drift: recent {_DRIFT_LOOKBACK} trades at {drift['recent_wr']:.0%} WR "
            f"vs historical {drift['historical_wr']:.0%} (Δ {drift['delta']:.0%})"
        )

    # ── Statistical underperformance
    if status == "HEALTHY" and win_rate < _UNDERPERFORM_WR and sig["significant"]:
        status = "UNDERPERFORM"
        findings.append(
            f"Win rate {win_rate:.0%} below {_UNDERPERFORM_WR:.0%} threshold — "
            f"statistically significant (p={sig['p_value']:.3f})"
        )
    elif status == "HEALTHY" and win_rate > _OUTPERFORM_WR and sig["significant"]:
        status = "OUTPERFORM"
        findings.append(
            f"Win rate {win_rate:.0%} above {_OUTPERFORM_WR:.0%} threshold — "
            f"statistically significant (p={sig['p_value']:.3f})"
        )
    elif status == "HEALTHY":
        findings.append(
            f"Win rate {win_rate:.0%} — not yet statistically significant "
            f"(p={sig['p_value']:.3f})"
        )

    # ── Kelly edge note
    if kelly <= 0:
        findings.append(
            f"Kelly fraction {kelly:.2%} — negative mathematical edge at current win rate"
        )
    else:
        findings.append(f"Kelly fraction {kelly:.2%} — positive edge")

    score = _compute_score(win_rate, kelly, drift, sig)
    rec   = _build_recommendation(model_name, status, win_rate, drift)

    return ModelEvaluation(
        model=model_name,
        total_trades=len(trades),
        win_rate=round(win_rate, 4),
        total_pnl=total_pnl,
        kelly_fraction=kelly,
        status=status,
        significance=sig,
        drift=drift,
        score=score,
        findings=findings,
        recommendation=rec,
    )


# ---------------------------------------------------------------------------
# Statistical helpers
# ---------------------------------------------------------------------------

def binomial_significance(wins: int, total: int, null_p: float = 0.50) -> dict:
    """Two-sided binomial test: is win rate significantly different from null_p?

    Uses scipy.stats.binomtest when available; falls back to normal approximation.
    """
    if total == 0:
        return {"p_value": 1.0, "significant": False, "test_used": "none"}

    try:
        from scipy.stats import binomtest
        result   = binomtest(wins, total, null_p, alternative="two-sided")
        p_value  = float(result.pvalue)
        test_used = "scipy_binomtest"
    except ImportError:
        # Normal approximation for environments without scipy
        p_hat   = wins / total
        se      = math.sqrt(null_p * (1 - null_p) / total)
        z       = abs(p_hat - null_p) / se if se > 0 else 0.0
        p_value = float(2 * (1 - _normal_cdf(z)))
        test_used = "normal_approx"

    return {
        "p_value":    round(p_value, 4),
        "significant": p_value < _SIGNIFICANCE_LVL,
        "test_used":  test_used,
    }


def kelly_fraction(win_rate: float, rr: float = _RR_RATIO) -> float:
    """Kelly criterion: f* = p − q/b, capped at [0, 0.25] (half-Kelly safety).

    Args:
        win_rate: fraction of trades that are wins (0–1)
        rr:       reward/risk ratio (3.0 for 1:3 system)
    """
    if win_rate <= 0:
        return 0.0
    f_star = win_rate - (1 - win_rate) / rr
    return round(max(0.0, min(f_star, 0.25)), 4)


def detect_drift(trades: list, lookback: int = _DRIFT_LOOKBACK) -> dict:
    """Compare recent `lookback` trades vs historical baseline.

    Drift is flagged when recent win rate drops more than _DRIFT_THRESHOLD
    below the historical win rate — signals market condition change.
    """
    if len(trades) <= lookback:
        return {"detected": False, "recent_wr": 0.0, "historical_wr": 0.0, "delta": 0.0}

    recent     = trades[-lookback:]
    historical = trades[:-lookback]

    def _wr(ts: list) -> float:
        if not ts:
            return 0.0
        return sum(1 for t in ts if t.get("result") == "WIN") / len(ts)

    recent_wr = _wr(recent)
    hist_wr   = _wr(historical)
    delta     = hist_wr - recent_wr

    return {
        "detected":      delta >= _DRIFT_THRESHOLD,
        "recent_wr":     round(recent_wr, 4),
        "historical_wr": round(hist_wr, 4),
        "delta":         round(delta, 4),
    }


# ---------------------------------------------------------------------------
# Score and recommendation builders
# ---------------------------------------------------------------------------

def _compute_score(win_rate: float, kelly: float, drift: dict, sig: dict) -> int:
    """Composite health score 0–100.

    Base: win_rate × 100
    Bonuses/penalties: Kelly edge, drift, statistical significance.
    """
    score = win_rate * 100

    if kelly > 0.10:
        score += 10
    elif kelly <= 0:
        score -= 20

    if drift["detected"]:
        score -= 25

    if sig["significant"]:
        score += 10 if win_rate > 0.5 else -10

    return int(max(0, min(100, round(score))))


def _build_recommendation(
    model: str,
    status: str,
    win_rate: float,
    drift: dict,
) -> Optional[str]:
    """Build a specific, model-aware recommendation string."""
    if status == "INSUFFICIENT_DATA":
        return None

    if status == "DRIFT":
        return (
            f"[{model}] Recent deterioration: {drift['recent_wr']:.0%} WR vs "
            f"{drift['historical_wr']:.0%} historical. "
            "Consider pausing this model temporarily until regime stabilises."
        )

    if status == "UNDERPERFORM":
        if "M5_MOMENTUM" in model:
            return (
                f"[{model}] Win rate {win_rate:.0%}. "
                "Suggestion: raise z-score entry threshold from 1.5 → 2.0 "
                "to filter weaker pullback signals."
            )
        if "M1_MEANREV" in model:
            return (
                f"[{model}] Win rate {win_rate:.0%}. "
                "Suggestion: raise OU entry z-score from 2.0 → 2.5, "
                "or tighten half-life filter to 8–25 bars."
            )
        if "LONDON_BREAKOUT" in model:
            return (
                f"[{model}] Win rate {win_rate:.0%}. "
                "Suggestion: raise minimum Asian range filter from $3 → $5 "
                "to avoid narrow-range false breakouts."
            )

    if status == "OUTPERFORM":
        return (
            f"[{model}] Performing above expectations at {win_rate:.0%} WR. "
            "Parameters well-calibrated — maintain as-is."
        )

    return None


def _normal_cdf(z: float) -> float:
    """Standard normal CDF approximation (used when scipy is absent)."""
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))
