"""
drift_detector.py — MAHORAGA skill

CUSUM-based sequential drift detector per model.

Thesis:
  A model's win rate naturally fluctuates. Small random dips are noise.
  CUSUM accumulates deviations from a reference win rate. When the
  cumulative deviation exceeds a threshold, it signals a sustained
  structural degradation — not random variance.

Algorithm (Page's CUSUM, lower-side only — detecting performance decay):
  target_wr   = historical win rate of the model (or default 0.50)
  slack       = CUSUM_SLACK (allows minor natural variance)
  threshold   = CUSUM_THRESHOLD

  For each trade (1=win, 0=loss):
    contribution = (win_indicator - target_wr) - slack
    cusum = max(0, cusum - contribution)   ← lower-side: accumulates negative drift
    if cusum >= threshold → DRIFT_DETECTED

State is persisted externally (stored by agent as JSON in mahoraga_state).
This module is stateless / pure — it receives the cusum value and returns the new one.

Public API
──────────
update_cusum(cusum, result, target_wr)          → (new_cusum, drifting: bool)
check_model_drift(trades_recent, target_wr)     → DriftResult
reset_cusum()                                   → 0.0
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


# ── tuning ────────────────────────────────────────────────────────────────────

CUSUM_SLACK       = 0.10   # tolerate up to 10pp below target before accumulating
CUSUM_THRESHOLD   = 3.0    # 3 "loss units" above target needed to trigger alert
CUSUM_RESET_ON_WIN = False  # don't reset on wins — sustained drift needs sustained wins

MIN_TRADES_FOR_DRIFT = 10  # ignore drift on fewer than N trades
DEFAULT_TARGET_WR    = 0.50


# ── per-trade CUSUM update ────────────────────────────────────────────────────

def update_cusum(cusum: float, result: str, target_wr: float = DEFAULT_TARGET_WR) -> tuple[float, bool]:
    """
    Process one trade result and update the CUSUM accumulator.

    Args:
        cusum:     Current CUSUM value (starts at 0.0, increases on bad trades).
        result:    "WIN" | "LOSS" | "BREAKEVEN"
        target_wr: Expected win rate for this model (0.0–1.0).

    Returns:
        (new_cusum, is_drifting)
    """
    result = (result or "").upper()
    if result == "WIN":
        win_indicator = 1.0
    elif result == "LOSS":
        win_indicator = 0.0
    else:
        return cusum, cusum >= CUSUM_THRESHOLD   # BREAKEVEN — no update

    contribution = (win_indicator - target_wr) - CUSUM_SLACK
    new_cusum = max(0.0, cusum - contribution)   # lower-side: subtract (positive = ok, negative = bad)
    return round(new_cusum, 4), new_cusum >= CUSUM_THRESHOLD


def reset_cusum() -> float:
    return 0.0


# ── batch check across a window of recent trades ─────────────────────────────

@dataclass
class DriftResult:
    model:           str
    is_drifting:     bool
    cusum:           float
    n_trades:        int
    recent_win_rate: float
    target_win_rate: float
    severity:        str    # "NONE" | "WATCH" | "ALERT" | "CRITICAL"
    message:         str


def check_model_drift(
    model: str,
    trades_recent: Sequence[dict],
    target_wr: float = DEFAULT_TARGET_WR,
    initial_cusum: float = 0.0,
) -> DriftResult:
    """
    Run CUSUM over a window of recent trades for one model and return the drift status.

    This is a batch check — typically called at scheduled analysis time.
    For continuous (per-trade) monitoring, use update_cusum() in the agent loop.

    Args:
        model:          Model name (for labelling only).
        trades_recent:  Closed trades (chronological order, already filtered to this model).
        target_wr:      Baseline win rate to defend (from historical stats or default).
        initial_cusum:  Starting CUSUM from previous run (loaded from mahoraga_state).

    Returns:
        DriftResult dataclass.
    """
    closed = [t for t in trades_recent if t.get("result")]
    n = len(closed)

    if n < MIN_TRADES_FOR_DRIFT:
        return DriftResult(
            model=model, is_drifting=False, cusum=initial_cusum,
            n_trades=n, recent_win_rate=0.0, target_win_rate=target_wr,
            severity="NONE",
            message=f"Insufficient data ({n}/{MIN_TRADES_FOR_DRIFT} trades)",
        )

    wins = sum(1 for t in closed if (t.get("result") or "").upper() == "WIN")
    recent_wr = wins / n

    cusum = initial_cusum
    for t in closed:
        cusum, _ = update_cusum(cusum, t.get("result", ""))

    drifting = cusum >= CUSUM_THRESHOLD

    # Severity tiers
    if not drifting:
        severity = "NONE" if cusum < CUSUM_THRESHOLD * 0.5 else "WATCH"
    else:
        severity = "CRITICAL" if cusum >= CUSUM_THRESHOLD * 2 else "ALERT"

    wr_diff = recent_wr - target_wr
    message = (
        f"{model}: recent WR={recent_wr:.1%} vs target={target_wr:.1%} "
        f"(Δ{wr_diff:+.1%}) | CUSUM={cusum:.2f}/{CUSUM_THRESHOLD} | {severity}"
    )

    return DriftResult(
        model=model, is_drifting=drifting, cusum=round(cusum, 4),
        n_trades=n, recent_win_rate=round(recent_wr, 4),
        target_win_rate=round(target_wr, 4),
        severity=severity, message=message,
    )


# ── multi-model batch check ───────────────────────────────────────────────────

def check_all_models_drift(
    trades: Sequence[dict],
    model_names: list[str],
    target_wrs: dict[str, float] | None = None,
    cusum_state: dict[str, float] | None = None,
) -> dict[str, DriftResult]:
    """
    Run drift check across all models in one call.

    Args:
        trades:      All recent closed trades (all models mixed).
        model_names: List of model name strings to evaluate.
        target_wrs:  {model: target_win_rate}. Defaults to 0.50 per model.
        cusum_state: {model: cusum_value} from previous state load.

    Returns:
        {model_name: DriftResult}
    """
    target_wrs  = target_wrs  or {}
    cusum_state = cusum_state or {}

    results = {}
    for model in model_names:
        model_trades = [t for t in trades if t.get("model") == model]
        results[model] = check_model_drift(
            model         = model,
            trades_recent = model_trades,
            target_wr     = target_wrs.get(model, DEFAULT_TARGET_WR),
            initial_cusum = cusum_state.get(model, 0.0),
        )
    return results
