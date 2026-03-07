"""
regime_validator.py — MAHORAGA skill

Validates regime detection quality by cross-referencing trade outcomes.

Core insight:
  A TRENDING regime (Hurst > 0.53) should favour Model A (momentum) wins.
  A RANGING regime  (Hurst < 0.47) should favour Model B (mean-rev) wins.
  Model C fires on any regime — outcome depends on breakout quality.

Since we cannot replay Hurst values for past trades, we use outcome as
a proxy: a high win rate ≈ regime was correctly identified.

Additional:
  - Regime stability analysis (how often does the regime flip?)
    Requires a list of recent regime labels from session_info history.

Public API
──────────
validate_regime_accuracy(trades)           → dict
analyze_regime_stability(recent_regimes)   → dict
generate_regime_suggestions(accuracy)      → list[str]
"""

import logging
from collections import Counter

logger = logging.getLogger(__name__)

_MIN_TRADES = 5   # minimum trades per model to draw conclusions


# ---------------------------------------------------------------------------
# Regime accuracy (via trade outcome proxy)
# ---------------------------------------------------------------------------

def validate_regime_accuracy(trades: list) -> dict:
    """Evaluate how well regime detection is working for each model.

    Groups trades by model and computes win rate (proxy for regime correctness).
    Returns dict keyed by model name.
    """
    model_groups: dict = {}
    for t in trades:
        model = t.get("model") or "UNKNOWN"
        model_groups.setdefault(model, []).append(t)

    result = {}
    for model, mt in model_groups.items():
        expected = _expected_regime(model)
        result[model] = _regime_metrics(mt, expected_regime=expected)

    return result


def _expected_regime(model: str) -> str:
    """Return the regime this model is supposed to operate in."""
    if "OU_GRIND" in model or "MODEL_A" in model:
        return "TRENDING"
    if "OU_RANGE" in model or "MODEL_B" in model:
        return "RANGING"
    if "BREAKOUT" in model or "MODEL_C" in model:
        return "ANY"
    return "UNKNOWN"


def _regime_metrics(trades: list, expected_regime: str) -> dict:
    """Compute accuracy proxy and verdict for a single model's trades."""
    if len(trades) < _MIN_TRADES:
        return {
            "trades":            len(trades),
            "expected_regime":   expected_regime,
            "accuracy_proxy":    None,
            "false_signal_rate": None,
            "verdict":           "INSUFFICIENT_DATA",
            "note": f"Need at least {_MIN_TRADES} trades for regime analysis.",
        }

    wins             = sum(1 for t in trades if t.get("result") == "WIN")
    accuracy_proxy   = wins / len(trades)
    false_signal_rate = 1.0 - accuracy_proxy

    if accuracy_proxy >= 0.55:
        verdict = "GOOD"
        note    = f"Regime {expected_regime} correctly identified in the majority of cases."
    elif accuracy_proxy >= 0.40:
        verdict = "MARGINAL"
        note    = (
            f"Regime {expected_regime} marginal — some mismatch between detected "
            "regime and actual market conditions. Monitor before adjusting."
        )
    else:
        verdict = "POOR"
        note    = (
            f"Regime {expected_regime} frequently misclassified. "
            "Consider recalibrating Hurst/ACF thresholds."
        )

    return {
        "trades":            len(trades),
        "expected_regime":   expected_regime,
        "accuracy_proxy":    round(accuracy_proxy, 4),
        "false_signal_rate": round(false_signal_rate, 4),
        "verdict":           verdict,
        "note":              note,
    }


# ---------------------------------------------------------------------------
# Regime stability
# ---------------------------------------------------------------------------

def analyze_regime_stability(recent_regimes: list) -> dict:
    """Measure how stable the regime detection is.

    Args:
        recent_regimes: list of regime label strings in chronological order
                        (e.g. ["TRENDING", "TRENDING", "RANGING", "VOLATILE"])

    Returns dict with:
        flip_rate   : fraction of consecutive pairs that changed
        dominant    : most common regime label
        distribution: {label: count}
        stable      : True if flip_rate < 0.30
    """
    if len(recent_regimes) < 2:
        counts = Counter(recent_regimes)
        dominant = counts.most_common(1)[0][0] if counts else None
        return {
            "flip_rate":    0.0,
            "dominant":     dominant,
            "distribution": dict(counts),
            "stable":       True,
        }

    flips     = sum(1 for i in range(1, len(recent_regimes)) if recent_regimes[i] != recent_regimes[i - 1])
    flip_rate = flips / (len(recent_regimes) - 1)
    counts    = Counter(recent_regimes)
    dominant  = counts.most_common(1)[0][0]

    return {
        "flip_rate":    round(flip_rate, 4),
        "dominant":     dominant,
        "distribution": dict(counts),
        "stable":       flip_rate < 0.30,
    }


# ---------------------------------------------------------------------------
# Suggestion generator
# ---------------------------------------------------------------------------

def generate_regime_suggestions(accuracy: dict) -> list:
    """Generate specific regime tuning suggestions from accuracy results.

    Returns list of suggestion strings. Pure function.
    """
    suggestions = []

    for model, metrics in accuracy.items():
        verdict = metrics.get("verdict")
        ap      = metrics.get("accuracy_proxy")

        if verdict == "POOR":
            if "OU_GRIND" in model or "MODEL_A" in model:
                suggestions.append(
                    f"[{model}] Regime accuracy poor ({ap:.0%}). "
                    "Suggestion: raise Hurst TRENDING threshold from 0.53 → 0.56 "
                    "to require stronger trend confirmation before Model A fires."
                )
            elif "OU_RANGE" in model or "MODEL_B" in model:
                suggestions.append(
                    f"[{model}] Regime accuracy poor ({ap:.0%}). "
                    "Suggestion: tighten ADF significance level from 0.05 → 0.01 "
                    "to require stronger stationarity evidence for Model B entries."
                )
            else:
                suggestions.append(
                    f"[{model}] Regime accuracy poor ({ap:.0%}). "
                    "Review entry conditions — market regime may not support this model."
                )

        elif verdict == "MARGINAL":
            suggestions.append(
                f"[{model}] Regime accuracy marginal ({ap:.0%}). "
                "Collect 20 more trades before adjusting thresholds."
            )

    if not suggestions:
        suggestions.append(
            "Regime detection quality is acceptable across all active models."
        )

    return suggestions
