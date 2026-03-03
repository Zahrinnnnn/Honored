"""
parameter_optimizer.py — MAHORAGA skill

Analyses trade outcomes to surface data-driven parameter hints.
All analysis is observational — MAHORAGA never auto-applies anything.

Analyses:
  - SL distance buckets   → which SL range had the best win rate?
  - Session timing        → which UTC hours performed best?
  - Direction bias        → BUY vs SELL win rate asymmetry
  - Duration vs outcome   → do quicker exits win more?

Public API
──────────
analyze_sl_ranges(trades)            → list[dict]
analyze_session_timing(trades)       → list[dict]
analyze_direction_bias(trades)       → dict
analyze_duration_vs_outcome(trades)  → dict
generate_parameter_suggestions(...)  → list[str]
"""

import logging
from collections import defaultdict
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_MIN_BUCKET = 5   # minimum trades per bucket before drawing conclusions


# ---------------------------------------------------------------------------
# SL distance analysis
# ---------------------------------------------------------------------------

def analyze_sl_ranges(trades: list) -> list:
    """Group trades by SL distance buckets; rank by avg P&L per trade.

    Buckets: $0–4, $4–6, $6–8, $8+
    Only includes buckets with ≥ MIN_BUCKET trades.
    """
    buckets: dict = defaultdict(list)
    for t in trades:
        sl = float(t.get("sl_distance") or 0)
        if sl > 0:
            buckets[_sl_bucket(sl)].append(t)

    results = []
    for bucket_name, bt in buckets.items():
        if len(bt) < _MIN_BUCKET:
            continue
        wins = sum(1 for t in bt if t.get("result") == "WIN")
        pnl  = sum(float(t.get("pnl") or 0) for t in bt)
        results.append({
            "sl_range":  bucket_name,
            "trades":    len(bt),
            "win_rate":  round(wins / len(bt), 4),
            "total_pnl": round(pnl, 2),
            "avg_pnl":   round(pnl / len(bt), 2),
        })

    return sorted(results, key=lambda x: x["avg_pnl"], reverse=True)


def _sl_bucket(sl: float) -> str:
    if sl < 4.0:
        return "$0–4"
    if sl < 6.0:
        return "$4–6"
    if sl < 8.0:
        return "$6–8"
    return "$8+"


# ---------------------------------------------------------------------------
# Session timing analysis
# ---------------------------------------------------------------------------

def analyze_session_timing(trades: list) -> list:
    """Break trades into UTC-hour buckets; rank by avg P&L.

    Only includes hours with ≥ MIN_BUCKET trades.
    """
    buckets: dict = defaultdict(list)
    for t in trades:
        h = _trade_hour(t)
        if h is not None:
            buckets[h].append(t)

    results = []
    for hour, ht in buckets.items():
        if len(ht) < _MIN_BUCKET:
            continue
        wins = sum(1 for t in ht if t.get("result") == "WIN")
        pnl  = sum(float(t.get("pnl") or 0) for t in ht)
        results.append({
            "hour_utc":  hour,
            "trades":    len(ht),
            "win_rate":  round(wins / len(ht), 4),
            "total_pnl": round(pnl, 2),
            "avg_pnl":   round(pnl / len(ht), 2),
        })

    return sorted(results, key=lambda x: x["avg_pnl"], reverse=True)


def _trade_hour(trade: dict) -> Optional[int]:
    time_str = trade.get("time", "")
    if not time_str:
        return None
    try:
        return datetime.fromisoformat(time_str.replace("Z", "+00:00")).hour
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Direction bias
# ---------------------------------------------------------------------------

def analyze_direction_bias(trades: list) -> dict:
    """Compare BUY vs SELL trade performance.

    A large asymmetry (>20 pp) suggests a persistent market bias or a
    signal quality issue in one direction.
    """
    buy_trades  = [t for t in trades if t.get("direction") == "BUY"]
    sell_trades = [t for t in trades if t.get("direction") == "SELL"]

    def _stats(ts: list) -> dict:
        if not ts:
            return {"trades": 0, "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0}
        wins = sum(1 for t in ts if t.get("result") == "WIN")
        pnl  = sum(float(t.get("pnl") or 0) for t in ts)
        return {
            "trades":    len(ts),
            "win_rate":  round(wins / len(ts), 4),
            "total_pnl": round(pnl, 2),
            "avg_pnl":   round(pnl / len(ts), 2),
        }

    buy_stats  = _stats(buy_trades)
    sell_stats = _stats(sell_trades)
    bias_delta = round(abs(buy_stats["win_rate"] - sell_stats["win_rate"]), 4)
    stronger   = "BUY" if buy_stats["win_rate"] >= sell_stats["win_rate"] else "SELL"

    return {
        "BUY":        buy_stats,
        "SELL":       sell_stats,
        "bias_delta": bias_delta,
        "stronger":   stronger if bias_delta > 0.05 else "BALANCED",
    }


# ---------------------------------------------------------------------------
# Duration vs outcome
# ---------------------------------------------------------------------------

def analyze_duration_vs_outcome(trades: list) -> dict:
    """Compare average trade duration for wins vs losses.

    Quick losses vs slow wins (or vice versa) reveals holding-time patterns.
    """
    wins   = [t for t in trades if t.get("result") == "WIN"  and t.get("duration_mins")]
    losses = [t for t in trades if t.get("result") == "LOSS" and t.get("duration_mins")]

    def _avg_dur(ts: list) -> float:
        if not ts:
            return 0.0
        return round(sum(float(t["duration_mins"]) for t in ts) / len(ts), 1)

    return {
        "avg_win_duration_mins":  _avg_dur(wins),
        "avg_loss_duration_mins": _avg_dur(losses),
        "note": (
            "Losses resolve faster than wins — possible early adverse move pattern."
            if _avg_dur(losses) < _avg_dur(wins) * 0.5 and wins and losses
            else "Duration pattern within normal range."
        ),
    }


# ---------------------------------------------------------------------------
# Suggestion generator
# ---------------------------------------------------------------------------

def generate_parameter_suggestions(
    sl_analysis:     list,
    session_analysis: list,
    direction_bias:  dict,
    model_evals:     list,
) -> list:
    """Produce human-readable parameter hints from all analyses.

    Returns list of suggestion strings. Pure function — no side effects.
    """
    suggestions = []

    # ── SL range hint
    if len(sl_analysis) >= 2:
        best  = sl_analysis[0]
        worst = sl_analysis[-1]
        if best["win_rate"] - worst["win_rate"] > 0.15:
            suggestions.append(
                f"SL analysis: {best['sl_range']} range → {best['win_rate']:.0%} WR "
                f"vs {worst['sl_range']} → {worst['win_rate']:.0%} WR "
                f"({best['trades']} vs {worst['trades']} trades). "
                f"Tighter SLs are performing better — consider capping ATR multiplier."
            )

    # ── Best trading hour
    if session_analysis and session_analysis[0]["win_rate"] > 0.60:
        best_h = session_analysis[0]
        suggestions.append(
            f"Session timing: {best_h['hour_utc']:02d}:00 UTC shows {best_h['win_rate']:.0%} WR "
            f"over {best_h['trades']} trades — your strongest trading hour."
        )

    # ── Direction bias
    stronger = direction_bias.get("stronger", "BALANCED")
    delta    = direction_bias.get("bias_delta", 0.0)
    buy_n    = direction_bias.get("BUY", {}).get("trades", 0)
    sell_n   = direction_bias.get("SELL", {}).get("trades", 0)
    if stronger not in ("BALANCED",) and delta > 0.20 and min(buy_n, sell_n) >= _MIN_BUCKET:
        weaker = "SELL" if stronger == "BUY" else "BUY"
        suggestions.append(
            f"Direction bias: {stronger} trades winning significantly more "
            f"(Δ {delta:.0%}). Market may have a {stronger.lower()} bias — "
            f"{weaker} signals are weaker right now."
        )

    if not suggestions:
        suggestions.append(
            "No statistically strong parameter adjustments identified. "
            "System is operating within expected parameters."
        )

    return suggestions
