"""
performance_analyzer.py — MAHORAGA skill

Computes trade performance statistics from a list of trade dicts.
All functions are pure (no I/O) — accept a trade list, return dicts.

Metrics produced:
  - win_rate, total_pnl, avg_pnl, best/worst trade
  - Sharpe ratio (annualised trade-level)
  - Maximum drawdown % (peak-to-trough on cumulative P&L)
  - Recovery factor  (total P&L / max drawdown amount)
  - Profit factor    (gross profit / gross loss)
  - Average trade duration
  - Breakdowns: by model, by session window, rolling 7d / 30d
"""

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# Annualisation factor: assume ~5 trades/day × 250 trading days
_ANN_FACTOR = float(np.sqrt(5 * 250))


# ---------------------------------------------------------------------------
# Core aggregation
# ---------------------------------------------------------------------------

def compute_stats(trades: list) -> dict:
    """Full performance stats for a list of completed trades."""
    if not trades:
        return _empty_stats()

    pnls    = [float(t.get("pnl") or 0) for t in trades]
    results = [t.get("result", "") for t in trades]
    wins    = sum(1 for r in results if r == "WIN")
    losses  = len(trades) - wins

    total_pnl = round(sum(pnls), 2)
    win_rate  = round(wins / len(trades), 4)
    avg_pnl   = round(total_pnl / len(trades), 2)

    return {
        "total_trades":      len(trades),
        "wins":              wins,
        "losses":            losses,
        "win_rate":          win_rate,
        "total_pnl":         total_pnl,
        "avg_pnl":           avg_pnl,
        "best_trade":        round(max(pnls), 2),
        "worst_trade":       round(min(pnls), 2),
        "sharpe_ratio":      compute_sharpe(pnls),
        "max_drawdown_pct":  compute_max_drawdown(pnls),
        "recovery_factor":   compute_recovery_factor(total_pnl, pnls),
        "profit_factor":     compute_profit_factor(pnls),
        "avg_duration_mins": avg_duration(trades),
        "expectancy":        avg_pnl,   # alias used in reports
    }


# ---------------------------------------------------------------------------
# Statistical metrics
# ---------------------------------------------------------------------------

def compute_sharpe(pnls: list, risk_free: float = 0.0) -> float:
    """Annualised Sharpe ratio from trade-level P&L list."""
    if len(pnls) < 2:
        return 0.0
    arr  = np.array(pnls, dtype=float)
    mean = float(np.mean(arr)) - risk_free
    std  = float(np.std(arr, ddof=1))
    if std == 0:
        return 0.0
    return round(float(mean / std * _ANN_FACTOR), 2)


def compute_max_drawdown(pnls: list) -> float:
    """Maximum peak-to-trough drawdown as % of cumulative peak P&L."""
    if not pnls:
        return 0.0
    cumulative = np.cumsum(pnls)
    peak       = np.maximum.accumulate(cumulative)
    trough     = cumulative - peak
    if peak.max() <= 0:
        return 0.0
    max_dd = float(np.min(trough / np.where(peak > 0, peak, 1)))
    return round(abs(max_dd) * 100, 2)


def compute_recovery_factor(total_pnl: float, pnls: list) -> float:
    """Recovery factor = total P&L / max drawdown amount. Higher = better."""
    if not pnls:
        return 0.0
    cumulative = np.cumsum(pnls)
    peak       = np.maximum.accumulate(cumulative)
    drawdown   = peak - cumulative
    max_dd_amt = float(np.max(drawdown))
    if max_dd_amt == 0:
        return float("inf") if total_pnl > 0 else 0.0
    return round(total_pnl / max_dd_amt, 2)


def compute_profit_factor(pnls: list) -> float:
    """Profit factor = gross profit / gross loss."""
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss   = abs(sum(p for p in pnls if p < 0))
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return round(gross_profit / gross_loss, 2)


def avg_duration(trades: list) -> float:
    """Average trade duration in minutes."""
    durations = [
        float(t["duration_mins"])
        for t in trades
        if t.get("duration_mins") is not None
    ]
    if not durations:
        return 0.0
    return round(sum(durations) / len(durations), 1)


# ---------------------------------------------------------------------------
# Breakdowns
# ---------------------------------------------------------------------------

def by_model_stats(trades: list) -> dict:
    """Per-model performance breakdown."""
    grouped: dict = defaultdict(list)
    for t in trades:
        grouped[t.get("model") or "UNKNOWN"].append(t)
    return {model: compute_stats(mt) for model, mt in grouped.items()}


def by_session_stats(trades: list) -> dict:
    """Per-session breakdown inferred from trade time (UTC hour)."""
    grouped: dict = defaultdict(list)
    for t in trades:
        grouped[_infer_session(t)].append(t)
    return {session: compute_stats(st) for session, st in grouped.items()}


def by_direction_stats(trades: list) -> dict:
    """BUY vs SELL breakdown."""
    grouped: dict = defaultdict(list)
    for t in trades:
        grouped[t.get("direction") or "UNKNOWN"].append(t)
    return {d: compute_stats(dt) for d, dt in grouped.items()}


def rolling_stats(trades: list, days: int) -> dict:
    """Stats for the last N calendar days of trades."""
    if not trades:
        return _empty_stats()
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    recent = [t for t in trades if _trade_ts(t) >= cutoff]
    return compute_stats(recent)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _infer_session(trade: dict) -> str:
    """Assign a session label from the trade's UTC time.

    Accepts full ISO datetime strings ("2024-01-01T08:30:00+00:00") or
    bare time strings ("08:30:00") as stored by the trade logger.
    """
    from datetime import time as _time
    time_str = trade.get("time", "")
    if not time_str:
        return "OTHER"
    # Try full datetime first
    try:
        hour = datetime.fromisoformat(time_str.replace("Z", "+00:00")).hour
    except (ValueError, TypeError):
        # Fall back to bare HH:MM:SS
        try:
            hour = _time.fromisoformat(time_str.split(".")[0].split("+")[0]).hour
        except (ValueError, TypeError):
            return "OTHER"
    if 7 <= hour < 10:
        return "LONDON_OPEN"
    if 12 <= hour < 16:
        return "NY_OVERLAP"
    if 19 <= hour < 21:
        return "NY_CLOSE"
    return "OTHER"


def _trade_ts(trade: dict) -> float:
    """Extract trade datetime as unix timestamp."""
    date_str = trade.get("date", "")
    time_str = trade.get("time", "")
    try:
        combined = f"{date_str}T{time_str}" if time_str else date_str
        return datetime.fromisoformat(combined.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _empty_stats() -> dict:
    return {
        "total_trades": 0, "wins": 0, "losses": 0,
        "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0,
        "best_trade": 0.0, "worst_trade": 0.0,
        "sharpe_ratio": 0.0, "max_drawdown_pct": 0.0,
        "recovery_factor": 0.0, "profit_factor": 0.0,
        "avg_duration_mins": 0.0, "expectancy": 0.0,
    }
