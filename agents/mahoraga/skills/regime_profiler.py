"""
regime_profiler.py — MAHORAGA skill

Win-rate matrix sliced by model × direction × regime × session.
Pure functions (list[dict] in → dict out, no DB access).

This is the "regime quality" layer — answers "which regimes are we
actually profitable in, vs which ones are we losing in?"

Public API
──────────
model_regime_matrix(trades, model)     → dict  — per-regime, per-direction stats
full_matrix(trades)                    → dict  — nested: model → regime → dir → stats
regime_quality_summary(trades)         → list[dict] — ranked by avg_pnl descending
session_regime_matrix(trades)          → dict  — session → regime → stats
"""

from __future__ import annotations

from collections import defaultdict
from typing import Sequence


# ── helpers ───────────────────────────────────────────────────────────────────

def _closed(trades: Sequence[dict]) -> list[dict]:
    return [t for t in trades if t.get("result")]


def _stats(group: list[dict]) -> dict:
    n = len(group)
    if n == 0:
        return {"n": 0, "win_rate": 0.0, "avg_pnl": 0.0, "total_pnl": 0.0}
    wins  = sum(1 for t in group if (t.get("result") or "").upper() == "WIN")
    pnls  = [float(t.get("pnl", 0.0) or 0.0) for t in group]
    total = sum(pnls)
    return {
        "n":         n,
        "win_rate":  round(wins / n, 4),
        "avg_pnl":   round(total / n, 4),
        "total_pnl": round(total, 2),
    }


# ── per-model regime breakdown ────────────────────────────────────────────────

def model_regime_matrix(trades: Sequence[dict], model: str) -> dict:
    """
    For one model: regime_at_entry → direction → stats.

    Returns nested dict:
    {
        "BULLISH_GRIND": {
            "BUY":  {"n": 12, "win_rate": 0.67, "avg_pnl": 3.2, "total_pnl": 38.4},
            "SELL": {"n":  2, ...}
        },
        ...
    }
    """
    closed = [t for t in _closed(trades) if t.get("model") == model]
    matrix: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for t in closed:
        regime    = t.get("regime_at_entry") or "UNKNOWN"
        direction = t.get("direction")       or "UNKNOWN"
        matrix[regime][direction].append(t)

    return {
        regime: {direction: _stats(group) for direction, group in dirs.items()}
        for regime, dirs in matrix.items()
    }


# ── full cross-model matrix ───────────────────────────────────────────────────

def full_matrix(trades: Sequence[dict]) -> dict:
    """
    model → regime_at_entry → direction → stats.
    All models in the trade log are included automatically.
    """
    closed = _closed(trades)
    models = {t.get("model") for t in closed if t.get("model")}
    return {model: model_regime_matrix(closed, model) for model in sorted(models)}


# ── ranked quality summary ────────────────────────────────────────────────────

def regime_quality_summary(trades: Sequence[dict]) -> list[dict]:
    """
    Flatten full_matrix into a ranked list sorted by avg_pnl descending.
    Each row = {model, regime, direction, n, win_rate, avg_pnl, total_pnl}.
    Useful for quickly spotting the best/worst model×regime combinations.
    """
    rows = []
    matrix = full_matrix(trades)
    for model, regimes in matrix.items():
        for regime, dirs in regimes.items():
            for direction, s in dirs.items():
                if s["n"] == 0:
                    continue
                rows.append({
                    "model":     model,
                    "regime":    regime,
                    "direction": direction,
                    **s,
                })
    rows.sort(key=lambda r: r["avg_pnl"], reverse=True)
    return rows


# ── session × regime matrix ───────────────────────────────────────────────────

def session_regime_matrix(trades: Sequence[dict]) -> dict:
    """
    session → regime_at_entry → stats (all models combined).
    Helps answer: "Is LONDON_OPEN BULLISH_GRIND actually worth trading?"
    """
    closed = _closed(trades)
    matrix: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for t in closed:
        session = t.get("session") or "UNKNOWN"
        regime  = t.get("regime_at_entry") or "UNKNOWN"
        matrix[session][regime].append(t)

    return {
        session: {regime: _stats(group) for regime, group in regimes.items()}
        for session, regimes in matrix.items()
    }


# ── H4 bias filter effectiveness ─────────────────────────────────────────────

def h4_bias_breakdown(trades: Sequence[dict]) -> dict:
    """
    h4_bias_at_entry × direction → stats.
    Shows whether the H4 bias filter is actually helping.
    """
    closed = _closed(trades)
    matrix: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for t in closed:
        bias      = t.get("h4_bias_at_entry") or "UNKNOWN"
        direction = t.get("direction")         or "UNKNOWN"
        matrix[bias][direction].append(t)

    return {
        bias: {direction: _stats(group) for direction, group in dirs.items()}
        for bias, dirs in matrix.items()
    }
