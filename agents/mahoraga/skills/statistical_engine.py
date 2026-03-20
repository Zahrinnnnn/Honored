"""
statistical_engine.py — MAHORAGA skill

Core statistics computed from a flat list of closed trade dicts.
All functions are pure (no DB access, no side effects).

Public API
──────────
compute_stats(trades)           → dict  — full aggregate stats
compute_model_stats(trades, model) → dict — same but filtered to one model
streak_stats(trades)            → dict  — longest win/loss streaks
"""

from __future__ import annotations

import math
from typing import Sequence


# ── helpers ──────────────────────────────────────────────────────────────────

def _pnls(trades: Sequence[dict]) -> list[float]:
    return [float(t.get("pnl", 0.0) or 0.0) for t in trades]


def _wins(trades: Sequence[dict]) -> list[dict]:
    return [t for t in trades if (t.get("result") or "").upper() == "WIN"]


def _losses(trades: Sequence[dict]) -> list[dict]:
    return [t for t in trades if (t.get("result") or "").upper() == "LOSS"]


def _closed(trades: Sequence[dict]) -> list[dict]:
    """Only completed trades (result not NULL/empty)."""
    return [t for t in trades if t.get("result")]


# ── expectancy ────────────────────────────────────────────────────────────────

def expectancy(trades: Sequence[dict]) -> float:
    """
    Expectancy per trade in USD.
    E = (win_rate × avg_win) + (loss_rate × avg_loss)
    avg_loss is already negative in PnL — sums naturally.
    """
    closed = _closed(trades)
    if not closed:
        return 0.0
    pnls = _pnls(closed)
    return round(sum(pnls) / len(pnls), 4)


# ── Sharpe ratio ─────────────────────────────────────────────────────────────

def sharpe_ratio(trades: Sequence[dict], risk_free: float = 0.0) -> float:
    """
    Trade-level Sharpe ratio (annualised assuming ~252 trading days, 3 trades/day).
    Returns 0.0 if std is zero or fewer than 2 trades.
    """
    closed = _closed(trades)
    if len(closed) < 2:
        return 0.0
    pnls = _pnls(closed)
    n = len(pnls)
    mean = sum(pnls) / n
    variance = sum((p - mean) ** 2 for p in pnls) / (n - 1)
    std = math.sqrt(variance) if variance > 0 else 0.0
    if std == 0.0:
        return 0.0
    # Annualise: ~756 trades/year (3/day × 252 days)
    annual_factor = math.sqrt(756)
    return round((mean - risk_free) / std * annual_factor, 4)


# ── Calmar ratio ─────────────────────────────────────────────────────────────

def calmar_ratio(trades: Sequence[dict]) -> float:
    """
    Calmar = total_pnl / max_drawdown_pct (from trade records).
    Returns 0.0 if max_drawdown is 0.
    """
    closed = _closed(trades)
    if not closed:
        return 0.0
    total_pnl = sum(_pnls(closed))
    dd_values = [float(t.get("drawdown_pct", 0.0) or 0.0) for t in closed]
    max_dd = max(dd_values) if dd_values else 0.0
    if max_dd <= 0:
        return 0.0
    return round(total_pnl / max_dd, 4)


# ── profit factor ─────────────────────────────────────────────────────────────

def profit_factor(trades: Sequence[dict]) -> float:
    """Gross profit / gross loss. Returns 0.0 if no losses."""
    closed = _closed(trades)
    gross_win  = sum(p for p in _pnls(closed) if p > 0)
    gross_loss = sum(abs(p) for p in _pnls(closed) if p < 0)
    if gross_loss == 0:
        return 0.0
    return round(gross_win / gross_loss, 4)


# ── streak analysis ───────────────────────────────────────────────────────────

def streak_stats(trades: Sequence[dict]) -> dict:
    """
    Returns longest and current win/loss streaks from chronological trade list.
    trades should be ordered oldest → newest.
    """
    closed = _closed(trades)
    if not closed:
        return {"longest_win_streak": 0, "longest_loss_streak": 0,
                "current_streak_type": "", "current_streak_len": 0}

    longest_win = longest_loss = 0
    cur_win = cur_loss = 0
    current_type = ""
    current_len = 0

    for t in closed:
        result = (t.get("result") or "").upper()
        if result == "WIN":
            cur_win  += 1
            cur_loss  = 0
            longest_win = max(longest_win, cur_win)
        elif result == "LOSS":
            cur_loss += 1
            cur_win   = 0
            longest_loss = max(longest_loss, cur_loss)
        else:  # BREAKEVEN — reset streak
            cur_win = cur_loss = 0

    # Current streak
    if cur_win > 0:
        current_type = "WIN"
        current_len  = cur_win
    elif cur_loss > 0:
        current_type = "LOSS"
        current_len  = cur_loss

    return {
        "longest_win_streak":  longest_win,
        "longest_loss_streak": longest_loss,
        "current_streak_type": current_type,
        "current_streak_len":  current_len,
    }


# ── max drawdown from trade PnL sequence ─────────────────────────────────────

def max_drawdown_pct_from_trades(trades: Sequence[dict]) -> float:
    """
    Recompute max drawdown % from trade PnL series (peak-to-trough on cumulative PnL).
    More reliable than reading per-trade drawdown_pct which may be stale.
    Returns 0.0 if no closed trades.
    """
    closed = _closed(trades)
    if not closed:
        return 0.0

    # Use balance_before of first trade as baseline
    baseline = float(closed[0].get("balance_before", 0.0) or 0.0)
    if baseline <= 0:
        return 0.0

    peak = baseline
    max_dd = 0.0
    cumulative = baseline

    for t in closed:
        cumulative += float(t.get("pnl", 0.0) or 0.0)
        peak = max(peak, cumulative)
        dd = (peak - cumulative) / peak * 100 if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    return round(max_dd, 4)


# ── full aggregate stats ──────────────────────────────────────────────────────

def compute_stats(trades: Sequence[dict]) -> dict:
    """
    Aggregate statistics across all provided trades.
    trades: list of closed trade dicts from state_manager.get_trades_by_period().
    """
    closed = _closed(trades)
    n = len(closed)
    if n == 0:
        return _empty_stats()

    wins   = _wins(closed)
    losses = _losses(closed)
    pnls   = _pnls(closed)

    win_rate  = len(wins) / n
    total_pnl = sum(pnls)
    avg_pnl   = total_pnl / n

    avg_win  = (sum(_pnls(wins))   / len(wins))   if wins   else 0.0
    avg_loss = (sum(_pnls(losses)) / len(losses)) if losses else 0.0

    durations = [float(t.get("duration_mins", 0.0) or 0.0) for t in closed]
    avg_dur   = sum(durations) / n if durations else 0.0

    pnl_sorted = sorted(pnls)
    best_trade  = round(pnl_sorted[-1], 2) if pnl_sorted else 0.0
    worst_trade = round(pnl_sorted[0],  2) if pnl_sorted else 0.0

    return {
        "n_trades":        n,
        "n_wins":          len(wins),
        "n_losses":        len(losses),
        "win_rate":        round(win_rate, 4),
        "total_pnl":       round(total_pnl, 2),
        "avg_pnl":         round(avg_pnl, 4),
        "avg_win":         round(avg_win, 4),
        "avg_loss":        round(avg_loss, 4),
        "best_trade":      best_trade,
        "worst_trade":     worst_trade,
        "avg_duration_mins": round(avg_dur, 1),
        "expectancy":      expectancy(closed),
        "sharpe":          sharpe_ratio(closed),
        "calmar":          calmar_ratio(closed),
        "profit_factor":   profit_factor(closed),
        "max_drawdown_pct": max_drawdown_pct_from_trades(closed),
        **streak_stats(closed),
    }


def compute_model_stats(trades: Sequence[dict], model: str) -> dict:
    """Same as compute_stats() but filtered to a single model."""
    filtered = [t for t in trades if t.get("model") == model]
    stats = compute_stats(filtered)
    stats["model"] = model
    return stats


def _empty_stats() -> dict:
    return {
        "n_trades": 0, "n_wins": 0, "n_losses": 0,
        "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0,
        "avg_win": 0.0, "avg_loss": 0.0,
        "best_trade": 0.0, "worst_trade": 0.0,
        "avg_duration_mins": 0.0,
        "expectancy": 0.0, "sharpe": 0.0, "calmar": 0.0,
        "profit_factor": 0.0, "max_drawdown_pct": 0.0,
        "longest_win_streak": 0, "longest_loss_streak": 0,
        "current_streak_type": "", "current_streak_len": 0,
    }
