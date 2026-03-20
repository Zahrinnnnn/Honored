"""
feature_analyzer.py — MAHORAGA skill

Slice-and-dice trade data by contextual features stored at entry time.
All functions are pure (list[dict] in → dict out, no DB access).

Public API
──────────
hour_heatmap(trades)                      → dict[hour_str, {n, win_rate, avg_pnl}]
regime_direction_matrix(trades)           → dict[regime, dict[direction, stats]]
zscore_bucket_analysis(trades)            → dict[bucket_label, stats]
session_breakdown(trades)                 → dict[session, stats]
direction_breakdown(trades)               → dict[direction, stats]
"""

from __future__ import annotations

from collections import defaultdict
from typing import Sequence


# ── helpers ───────────────────────────────────────────────────────────────────

def _bucket_stats(group: list[dict]) -> dict:
    """Compute n/win_rate/avg_pnl/total_pnl for a group of closed trades."""
    n = len(group)
    if n == 0:
        return {"n": 0, "win_rate": 0.0, "avg_pnl": 0.0, "total_pnl": 0.0}
    wins    = sum(1 for t in group if (t.get("result") or "").upper() == "WIN")
    pnls    = [float(t.get("pnl", 0.0) or 0.0) for t in group]
    total   = sum(pnls)
    return {
        "n":          n,
        "win_rate":   round(wins / n, 4),
        "avg_pnl":    round(total / n, 4),
        "total_pnl":  round(total, 2),
    }


def _closed(trades: Sequence[dict]) -> list[dict]:
    return [t for t in trades if t.get("result")]


# ── hour heatmap ──────────────────────────────────────────────────────────────

def hour_heatmap(trades: Sequence[dict]) -> dict[str, dict]:
    """
    Group trades by entry_hour_utc (0–23).
    Returns dict keyed by "HH" string, value = bucket_stats.
    Only hours with at least 1 trade are returned.
    """
    closed = _closed(trades)
    buckets: dict[int, list] = defaultdict(list)
    for t in closed:
        hour = t.get("entry_hour_utc")
        if hour is not None:
            try:
                buckets[int(hour)].append(t)
            except (ValueError, TypeError):
                pass

    return {
        f"{h:02d}": _bucket_stats(group)
        for h, group in sorted(buckets.items())
    }


# ── regime × direction matrix ─────────────────────────────────────────────────

def regime_direction_matrix(trades: Sequence[dict]) -> dict[str, dict[str, dict]]:
    """
    2-D breakdown: regime_at_entry → direction → bucket_stats.
    Useful for spotting which regime+direction combinations are profitable.
    """
    closed = _closed(trades)
    matrix: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for t in closed:
        regime    = t.get("regime_at_entry") or "UNKNOWN"
        direction = t.get("direction")       or "UNKNOWN"
        matrix[regime][direction].append(t)

    return {
        regime: {
            direction: _bucket_stats(group)
            for direction, group in dirs.items()
        }
        for regime, dirs in matrix.items()
    }


# ── z-score bucket analysis ───────────────────────────────────────────────────

_ZSCORE_BUCKETS = [
    (0.0,  0.5,  "0.0–0.5"),
    (0.5,  1.0,  "0.5–1.0"),
    (1.0,  1.5,  "1.0–1.5"),
    (1.5,  2.0,  "1.5–2.0"),
    (2.0,  3.0,  "2.0–3.0"),
    (3.0,  float("inf"), "3.0+"),
]


def zscore_bucket_analysis(trades: Sequence[dict]) -> dict[str, dict]:
    """
    Group Model A trades by |zscore_at_entry| magnitude.
    Helps identify whether higher z-scores lead to better or worse outcomes.
    Trades with zscore_at_entry == 0 (e.g. Model B) are excluded.
    """
    closed  = [t for t in _closed(trades) if float(t.get("zscore_at_entry", 0.0) or 0.0) != 0.0]
    buckets: dict[str, list] = {label: [] for *_, label in _ZSCORE_BUCKETS}

    for t in closed:
        z = abs(float(t.get("zscore_at_entry", 0.0) or 0.0))
        for lo, hi, label in _ZSCORE_BUCKETS:
            if lo <= z < hi:
                buckets[label].append(t)
                break

    return {label: _bucket_stats(group) for label, group in buckets.items() if group}


# ── session breakdown ─────────────────────────────────────────────────────────

def session_breakdown(trades: Sequence[dict]) -> dict[str, dict]:
    """Group by session field. Only sessions with trades are returned."""
    closed = _closed(trades)
    buckets: dict[str, list] = defaultdict(list)
    for t in closed:
        session = t.get("session") or "UNKNOWN"
        buckets[session].append(t)
    return {s: _bucket_stats(g) for s, g in buckets.items()}


# ── direction breakdown ───────────────────────────────────────────────────────

def direction_breakdown(trades: Sequence[dict]) -> dict[str, dict]:
    """BUY vs SELL breakdown."""
    closed = _closed(trades)
    buckets: dict[str, list] = defaultdict(list)
    for t in closed:
        direction = t.get("direction") or "UNKNOWN"
        buckets[direction].append(t)
    return {d: _bucket_stats(g) for d, g in buckets.items()}


# ── detrend method breakdown (Model A only) ───────────────────────────────────

def detrend_breakdown(trades: Sequence[dict]) -> dict[str, dict]:
    """
    Compare ema50 vs ema21 vs ema50_blowoff performance for Model A.
    Model B returns 'kalman', which is also captured here.
    """
    closed = _closed(trades)
    buckets: dict[str, list] = defaultdict(list)
    for t in closed:
        method = t.get("detrend_method") or "UNKNOWN"
        buckets[method].append(t)
    return {m: _bucket_stats(g) for m, g in buckets.items()}
