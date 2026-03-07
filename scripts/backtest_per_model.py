#!/usr/bin/env python3
"""
backtest_per_model.py — Backtest HONORED system: combined or per-model.

Default mode: COMBINED — all 3 models share one account, exactly like live deployment.
Per-model mode: test a single model in isolation (--model A/B/C).

Uses 6-state H1 regime detector, M5 OU execution, fixed 1:2 RR + breakeven exit.
Anti-martingale lot sizing: halve lot after each consecutive loss, reset on win.
Auto-refills balance to starting amount when it hits $0 (or below $0.50).

Usage:
    python scripts/backtest_per_model.py                    # combined (default)
    python scripts/backtest_per_model.py --model A          # single model
    python scripts/backtest_per_model.py --balance 50       # custom start balance
"""

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, time, timedelta

import numpy as np
import pandas as pd

# Force UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from agents.nanami.skills.indicator_engine import add_indicators
from agents.nanami.skills.htf_regime import detect_regime, check_structural_break, compute_h4_bias
from agents.nanami.skills.ou_grind import generate_signal as _model_a
from agents.nanami.skills.ou_range import generate_signal as _model_b
from agents.nanami.skills.asian_breakout import generate_signal as _model_c
from agents.toji.skills.lot_calculator import calculate_lot
from agents.toji.skills.trade_monitor import calculate_pnl
from core.constants import (
    HTF_BIAS_BULLISH, HTF_BIAS_BEARISH, HTF_BIAS_NEUTRAL,
    MODEL_A, MODEL_B, MODEL_C,
    BREAKEVEN_ATR_THRESHOLD,
    OU_TIME_KILL_HALF_LIFE_MULT,
    TIME_KILL_MINUTES, MAX_TRADE_DURATION_MINUTES,
    XAUUSD_POINT_VALUE,
    REGIME_BULLISH_GRIND, REGIME_BEARISH_GRIND, REGIME_TIGHT_RANGE,
    REGIME_H1_BARS_NEEDED,
    STRUCTURAL_BREAK_COOLDOWN_HOURS,
)

# ─── Constants ────────────────────────────────────────────────────────────────

SPREAD_BASE_USD   = 0.35   # HFM Cents calm-market spread
SPREAD_VOLATILE   = 1.20   # spread during high-ATR bars
SLIPPAGE_BASE_USD = 0.10   # base slippage per trade
SLIPPAGE_VOLATILE = 0.30   # slippage during high-ATR bars
_ATR_VOLATILE_PCTILE = 80  # ATR percentile threshold for "volatile" spread/slippage
_WARMUP_M5 = 260
BUST_THRESHOLD = 0.50  # refill when balance drops below this

_WINDOWS = {
    "LONDON_BREAKOUT": (time(7,  0), time(7, 30)),
    "LONDON_OPEN":     (time(7, 30), time(10, 0)),
    "NY_OVERLAP":      (time(12, 0), time(16, 0)),
    "NY_CLOSE":        (time(19, 0), time(21, 0)),
}
_BLACKOUT_START = time(21, 0)
_BLACKOUT_END   = time(7,  0)

# Model-session routing: which models can fire in which sessions
_MODEL_SESSIONS = {
    MODEL_A: {"LONDON_OPEN", "NY_OVERLAP", "NY_CLOSE"},
    MODEL_B: {"NY_OVERLAP"},
    MODEL_C: {"LONDON_BREAKOUT"},
}
_SESSION_LIMIT = {MODEL_A: 8, MODEL_B: 8, MODEL_C: 1}


# ─── Session helpers ──────────────────────────────────────────────────────────

def _in_window(t: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def _session_from_dt(dt: datetime):
    t = dt.time().replace(second=0, microsecond=0)
    if _in_window(t, _BLACKOUT_START, _BLACKOUT_END):
        return None, False
    lb_start, lb_end = _WINDOWS["LONDON_BREAKOUT"]
    if _in_window(t, lb_start, lb_end):
        return "LONDON_BREAKOUT", True
    for name in ("LONDON_OPEN", "NY_OVERLAP", "NY_CLOSE"):
        s, e = _WINDOWS[name]
        if _in_window(t, s, e):
            return name, False
    return None, False


# ─── Realistic friction (spread + slippage) ──────────────────────────────────

def _precompute_atr_pctile(df_m5: pd.DataFrame) -> np.ndarray:
    """Precompute rolling ATR percentile rank for each M5 bar (200-bar window)."""
    atr = df_m5["atr14"].values if "atr14" in df_m5.columns else np.zeros(len(df_m5))
    pctile = np.full(len(atr), 50.0)
    for i in range(200, len(atr)):
        window = atr[i - 200 : i]
        if window.max() > 0:
            pctile[i] = float(np.searchsorted(np.sort(window), atr[i]) / len(window) * 100)
    return pctile


def _get_friction(atr_pctile: float) -> tuple:
    """Return (spread, slippage) in USD based on current ATR percentile."""
    if atr_pctile >= _ATR_VOLATILE_PCTILE:
        return SPREAD_VOLATILE, SLIPPAGE_VOLATILE
    return SPREAD_BASE_USD, SLIPPAGE_BASE_USD


# ─── Asian range ──────────────────────────────────────────────────────────────

def _precompute_asian_ranges(df: pd.DataFrame) -> dict:
    ranges = {}
    df_asian = df[df.index.hour < 7]
    for d, group in df_asian.groupby(df_asian.index.date):
        if len(group) >= 3:
            ranges[d] = (float(group["high"].max()), float(group["low"].min()))
        else:
            ranges[d] = (0.0, 0.0)
    return ranges


# ─── Regime precomputation ─────────────────────────────────────────────────

def _precompute_regime(df_h1: pd.DataFrame, df_h4: pd.DataFrame,
                       df_m5: pd.DataFrame) -> dict:
    h1_times = df_h1.index.to_list()
    h4_times = df_h4.index.to_list()
    m5_times = df_m5.index.to_list()

    regime_map = {}
    h1_ptr = 0
    h4_ptr = 0

    for i, m5_t in enumerate(m5_times):
        while h1_ptr < len(h1_times) - 1 and h1_times[h1_ptr + 1] <= m5_t:
            h1_ptr += 1
        while h4_ptr < len(h4_times) - 1 and h4_times[h4_ptr + 1] <= m5_t:
            h4_ptr += 1

        h1_start = max(0, h1_ptr - REGIME_H1_BARS_NEEDED + 1)
        h1_window = df_h1.iloc[h1_start:h1_ptr + 1]
        regime = detect_regime(h1_window)

        h4_start = max(0, h4_ptr - 29)
        h4_window = df_h4.iloc[h4_start:h4_ptr + 1]
        h4_bias = compute_h4_bias(h4_window)

        sb = check_structural_break(h1_window)

        regime_map[i] = {
            "regime": regime,
            "h4_bias": h4_bias,
            "structural_break": sb,
        }

    return regime_map


# ─── Validation ──────────────────────────────────────────────────────────────

def _validate(signal, state, session, is_breakout, bar_dt, regime, h4_bias,
              allowed_models=None):
    """Validate a signal. allowed_models filters which models can fire."""
    model = signal["model"]
    if allowed_models and model not in allowed_models:
        return False, "wrong_model"
    if state["emergency_halt"]:
        return False, "emergency_halt"
    if state["soft_halt"]:
        return False, "soft_halt"
    if state["structural_break_until"] and bar_dt < state["structural_break_until"]:
        return False, "structural_break"
    dd_pct = (state["peak_balance"] - state["balance"]) / max(state["peak_balance"], 1e-9) * 100
    if dd_pct >= 50.0:
        return False, "drawdown>=50"

    direction = signal.get("direction", "")

    # Regime + bias directional gate
    if model == MODEL_A:
        if direction == "BUY" and regime != REGIME_BULLISH_GRIND:
            return False, "regime_blocked"
        if direction == "SELL" and regime != REGIME_BEARISH_GRIND:
            return False, "regime_blocked"
    elif model == MODEL_B:
        if regime != REGIME_TIGHT_RANGE:
            return False, "regime_blocked"
    elif model == MODEL_C:
        if direction == "BUY" and h4_bias == HTF_BIAS_BEARISH:
            return False, "htf_buy_blocked"
        if direction == "SELL" and h4_bias == HTF_BIAS_BULLISH:
            return False, "htf_sell_blocked"

    # Session/priority checks
    if model == MODEL_C and not is_breakout:
        return False, "wrong_session"
    if model == MODEL_A and is_breakout:
        return False, "model_priority"
    if model == MODEL_A and session not in _MODEL_SESSIONS[MODEL_A]:
        return False, "wrong_session"
    if model == MODEL_B and session not in _MODEL_SESSIONS[MODEL_B]:
        return False, "wrong_session"

    # Session limit
    today    = bar_dt.date()
    sess_key = "LONDON_BREAKOUT" if model == MODEL_C else session
    if state["session_counts"].get((sess_key, model, today), 0) >= _SESSION_LIMIT[model]:
        return False, "session_limit"

    return True, "ok"


# ─── Trade lifecycle ─────────────────────────────────────────────────────────

def _open_trade(signal, bar, state, slippage: float = 0.0):
    sl_distance = float(signal["sl_distance"])
    lot = calculate_lot(state["balance"], sl_distance, consecutive_losses=state["consecutive_losses"])
    entry = float(signal["entry_price"])

    # Apply slippage: BUY fills higher, SELL fills lower (adverse)
    direction = signal["direction"]
    if direction == "BUY":
        entry = round(entry + slippage, 2)
    else:
        entry = round(entry - slippage, 2)

    trade = {
        "trade_id":       len(state["all_trades"]) + len(state["open_trades"]) + 1,
        "model":          signal["model"],
        "direction":      direction,
        "entry_price":    entry,
        "sl_price":       float(signal["sl_price"]),
        "tp_price":       float(signal["tp_price"]),
        "sl_distance":    sl_distance,
        "lot_size":       lot,
        "atr_at_entry":    float(signal.get("atr_at_entry", sl_distance)),
        "half_life_bars":  float(signal.get("half_life_bars", 0.0)),
        "session":         signal.get("session", ""),
        "regime":          signal.get("regime", ""),
        "open_time":      bar.name,
        "balance_before": state["balance"],
        "slippage":       slippage,
    }
    state["open_trades"].append(trade)
    today    = bar.name.date()
    sess_key = "LONDON_BREAKOUT" if signal["model"] == MODEL_C else signal.get("session", "")
    key      = (sess_key, signal["model"], today)
    state["session_counts"][key] = state["session_counts"].get(key, 0) + 1


def _close_trade(trade, exit_info, bar, state, start_balance, spread: float = SPREAD_BASE_USD):
    result      = exit_info["result"]
    exit_price  = exit_info["exit_price"]
    exit_reason = exit_info.get("exit_reason", "")

    raw_pnl     = calculate_pnl(trade, exit_price)
    spread_cost = round(trade["lot_size"] * spread * XAUUSD_POINT_VALUE, 4)
    pnl         = round(raw_pnl - spread_cost, 4)
    balance_after = round(state["balance"] + pnl, 4)
    state["balance"] = balance_after

    if balance_after > state["peak_balance"]:
        state["peak_balance"] = balance_after

    dd_pct = (state["peak_balance"] - balance_after) / max(state["peak_balance"], 1e-9) * 100
    duration_mins = (bar.name - trade["open_time"]).total_seconds() / 60.0

    # BREAKEVEN counts as WIN for loss streak
    state_result = "WIN" if result in ("WIN", "BREAKEVEN") else "LOSS"
    if state_result == "WIN":
        state["consecutive_losses"] = 0
        state["soft_halt"] = False
    else:
        state["consecutive_losses"] += 1
        if state["consecutive_losses"] >= 3:
            state["soft_halt"] = True

    if dd_pct >= 50.0 and not state["emergency_halt"]:
        state["emergency_halt"] = True

    state["all_trades"].append({
        **trade,
        "result":        result,
        "exit_price":    exit_price,
        "exit_reason":   exit_reason,
        "exit_time":     bar.name,
        "raw_pnl":       raw_pnl,
        "spread_cost":   spread_cost,
        "pnl":           pnl,
        "balance_after": balance_after,
        "drawdown_pct":  round(dd_pct, 2),
        "duration_mins": round(duration_mins, 1),
    })
    state["open_trades"].remove(trade)

    # Auto-refill on bust
    if state["balance"] < BUST_THRESHOLD:
        state["refill_count"] += 1
        state["balance"]      = start_balance
        state["peak_balance"] = start_balance
        state["consecutive_losses"] = 0
        state["soft_halt"]     = False
        state["emergency_halt"] = False


def _try_exit_rr(trade, bar, bar_dt):
    """Fixed 1:2 RR + breakeven protection exit check for a single M5 bar."""
    atr = trade.get("atr_at_entry", trade["sl_distance"])
    entry = trade["entry_price"]
    direction = trade["direction"]
    lo, hi = float(bar["low"]), float(bar["high"])
    close = float(bar["close"])

    # SL/TP hit on intra-bar extremes
    if direction == "BUY":
        if lo <= trade["sl_price"]:
            is_be = abs(trade["sl_price"] - entry) < 0.10
            result = "BREAKEVEN" if is_be else "LOSS"
            return {"result": result, "exit_price": trade["sl_price"], "exit_reason": "SL_HIT"}
        if hi >= trade["tp_price"]:
            return {"result": "WIN", "exit_price": trade["tp_price"], "exit_reason": "TP_HIT"}
    else:
        if hi >= trade["sl_price"]:
            is_be = abs(trade["sl_price"] - entry) < 0.10
            result = "BREAKEVEN" if is_be else "LOSS"
            return {"result": result, "exit_price": trade["sl_price"], "exit_reason": "SL_HIT"}
        if lo <= trade["tp_price"]:
            return {"result": "WIN", "exit_price": trade["tp_price"], "exit_reason": "TP_HIT"}

    # Breakeven protection: at +1 ATR profit, move SL to entry
    if direction == "BUY":
        profit_distance = close - entry
    else:
        profit_distance = entry - close

    if profit_distance >= BREAKEVEN_ATR_THRESHOLD * atr:
        if direction == "BUY" and trade["sl_price"] < entry:
            trade["sl_price"] = entry
        elif direction == "SELL" and trade["sl_price"] > entry:
            trade["sl_price"] = entry

    # Time-based exits
    open_time = trade["open_time"]
    if hasattr(open_time, 'timestamp'):
        elapsed_mins = (bar_dt - open_time).total_seconds() / 60.0
    else:
        elapsed_mins = 0

    if elapsed_mins >= MAX_TRADE_DURATION_MINUTES:
        result = "WIN" if profit_distance > 0 else "LOSS"
        return {"result": result, "exit_price": close, "exit_reason": "MAX_DURATION"}

    # OU-calibrated time kill: 2 × half_life_bars × 5 min
    half_life = float(trade.get("half_life_bars", 0.0))
    time_kill_mins = half_life * OU_TIME_KILL_HALF_LIFE_MULT * 5.0 if half_life > 0 else TIME_KILL_MINUTES

    if elapsed_mins >= time_kill_mins:
        if direction == "BUY" and close <= entry:
            return {"result": "LOSS", "exit_price": close, "exit_reason": "TIME_KILL"}
        elif direction == "SELL" and close >= entry:
            return {"result": "LOSS", "exit_price": close, "exit_reason": "TIME_KILL"}

    return None


# ─── Signal generation (all models for a bar) ────────────────────────────────

def _generate_signals_for_bar(df_m5_win, session, is_breakout, regime, h4_bias,
                               ah, al, models_to_run):
    """
    Generate signals from all eligible models for this bar.

    Model priority: C > A > B (C has exclusive breakout window).
    In the same bar, only ONE model fires (first match wins).
    """
    signals = []

    # Model C: exclusive during LONDON_BREAKOUT
    if MODEL_C in models_to_run and is_breakout:
        sig = _model_c(df_m5_win, ah, al, h4_bias)
        if sig:
            signals.append(sig)
            return signals  # exclusive window — no other model fires

    # Model A: GRIND regimes
    if MODEL_A in models_to_run and not is_breakout and session in _MODEL_SESSIONS[MODEL_A]:
        if regime in (REGIME_BULLISH_GRIND, REGIME_BEARISH_GRIND):
            sig = _model_a(df_m5_win, session, regime)
            if sig:
                signals.append(sig)

    # Model B: TIGHT_RANGE (can fire alongside Model A if different regime,
    # but in practice regimes are mutually exclusive per bar)
    if MODEL_B in models_to_run and not is_breakout and session in _MODEL_SESSIONS[MODEL_B]:
        if regime == REGIME_TIGHT_RANGE:
            sig = _model_b(df_m5_win, session, regime)
            if sig:
                signals.append(sig)

    return signals


# ─── Core backtest engine ────────────────────────────────────────────────────

def run_backtest(df_m5_ind, asian_ranges, regime_map, balance: float,
                 total_bars: int, models_to_run=None) -> dict:
    """
    Run backtest with specified models sharing one account.

    Args:
        models_to_run: set of model names, or None for all 3.
    """
    if models_to_run is None:
        models_to_run = {MODEL_A, MODEL_B, MODEL_C}

    # Precompute ATR percentile for dynamic spread/slippage
    atr_pctile = _precompute_atr_pctile(df_m5_ind)

    state = {
        "balance":                balance,
        "peak_balance":           balance,
        "consecutive_losses":     0,
        "open_trades":            [],
        "session_counts":         {},
        "soft_halt":              False,
        "emergency_halt":         False,
        "all_trades":             [],
        "refill_count":           0,
        "structural_break_until": None,
    }

    prev_date   = None
    reject_reasons = defaultdict(int)
    exit_reason_tally = defaultdict(int)
    signals_generated = 0

    for i in range(_WARMUP_M5, total_bars):
        bar = df_m5_ind.iloc[i]
        dt  = df_m5_ind.index[i]
        today = dt.date()

        # Daily reset
        if today != prev_date:
            if state["soft_halt"] or state["emergency_halt"]:
                state["soft_halt"]          = False
                state["emergency_halt"]     = False
                state["consecutive_losses"] = 0
                state["peak_balance"]       = state["balance"]
            prev_date = today

        # Check exits on open trades
        bar_spread, _ = _get_friction(atr_pctile[i])
        for trade in list(state["open_trades"]):
            exit_info = _try_exit_rr(trade, bar, dt)
            if exit_info:
                exit_reason_tally[exit_info.get("exit_reason", "UNKNOWN")] += 1
                _close_trade(trade, exit_info, bar, state, balance, spread=bar_spread)

        # Session routing
        session, is_breakout = _session_from_dt(dt)
        if session is None:
            continue

        # Regime + H4 bias
        rm = regime_map.get(i, {"regime": REGIME_TIGHT_RANGE, "h4_bias": HTF_BIAS_NEUTRAL,
                                "structural_break": False})
        regime = rm["regime"]
        h4_bias = rm["h4_bias"]

        # Structural break cooldown
        if rm["structural_break"]:
            state["structural_break_until"] = dt + timedelta(hours=STRUCTURAL_BREAK_COOLDOWN_HOURS)
        if state["structural_break_until"] and dt < state["structural_break_until"]:
            continue

        ah, al = asian_ranges.get(today, (0.0, 0.0))
        df_m5_win = df_m5_ind.iloc[max(0, i - 500) : i + 1]

        # Generate signals from all eligible models
        sigs = _generate_signals_for_bar(df_m5_win, session, is_breakout, regime,
                                         h4_bias, ah, al, models_to_run)

        _, bar_slippage = _get_friction(atr_pctile[i])
        for sig in sigs:
            signals_generated += 1
            ok, reason = _validate(sig, state, session, is_breakout, dt, regime,
                                   h4_bias, allowed_models=models_to_run)
            if ok:
                _open_trade(sig, bar, state, slippage=bar_slippage)
            else:
                reject_reasons[reason] += 1

    return {
        "models":             sorted(models_to_run),
        "trades":             state["all_trades"],
        "final_balance":      state["balance"],
        "refill_count":       state["refill_count"],
        "signals_generated":  signals_generated,
        "reject_reasons":     dict(reject_reasons),
        "exit_reason_tally":  dict(exit_reason_tally),
    }


# ─── Summary printer ─────────────────────────────────────────────────────────

def _print_summary(res: dict, start_bal: float, trading_days: int):
    trades = res["trades"]
    total  = len(trades)

    label = " + ".join(res.get("models", ["COMBINED"]))
    W = 72
    print()
    print("=" * W)
    print(f"  {label}")
    print("=" * W)

    if total == 0:
        print("  NO TRADES generated.")
        print(f"  Signals generated: {res['signals_generated']}")
        if res["reject_reasons"]:
            print("  Rejection reasons:")
            for reason, count in sorted(res["reject_reasons"].items(), key=lambda x: -x[1]):
                print(f"    {reason:<25} {count:>5}")
        print("=" * W)
        return

    df = pd.DataFrame(trades)
    wins    = df["result"].isin(["WIN", "BREAKEVEN"]).sum()
    losses  = total - wins
    wr      = wins / total
    net_pnl = df["pnl"].sum()
    max_dd  = df["drawdown_pct"].max()
    avg_pnl = df["pnl"].mean()
    refills = res["refill_count"]
    trades_per_day = total / max(trading_days, 1)

    # Sharpe
    if total >= 2:
        arr = np.array(df["pnl"].tolist(), dtype=float)
        std = arr.std(ddof=1)
        sharpe = float(arr.mean() / std * np.sqrt(len(arr))) if std > 0 else 0.0
    else:
        sharpe = 0.0

    print(f"  Period         : {df['open_time'].min():%Y-%m-%d}  to  {df['exit_time'].max():%Y-%m-%d}")
    print(f"  Trading days   : {trading_days}")
    print(f"  Start bal      : ${start_bal:.2f}")
    print(f"  Final bal      : ${res['final_balance']:.2f}")
    print(f"  Refills        : {refills}  (balance reset to ${start_bal:.2f} on bust)")
    print()
    print(f"  Trades         : {total}  ({wins}W / {losses}L)")
    print(f"  Trades/day     : {trades_per_day:.2f}")
    print(f"  Win rate       : {wr:.1%}")
    print(f"  Net P&L        : ${net_pnl:+.2f}  (across all refills)")
    print(f"  Avg P&L        : ${avg_pnl:+.4f} / trade")
    print(f"  Sharpe         : {sharpe:.2f}")
    print(f"  Max DD         : {max_dd:.1f}%")
    print(f"  Avg duration   : {df['duration_mins'].mean():.0f} min")
    print(f"  Best trade     : ${df['pnl'].max():+.4f}")
    print(f"  Worst trade    : ${df['pnl'].min():+.4f}")
    print(f"  Spread cost    : ${df['spread_cost'].sum():.2f} total")
    if "slippage" in df.columns:
        print(f"  Slippage cost  : ${df['slippage'].sum():.2f} total (entry adverse)")
    print(f"  Signals gen    : {res['signals_generated']}")

    # By model
    print()
    print("  By model:")
    for model, grp in df.groupby("model"):
        mw  = grp["result"].isin(["WIN", "BREAKEVEN"]).sum()
        mwr = mw / len(grp) if len(grp) else 0.0
        mpnl = grp["pnl"].sum()
        mperday = len(grp) / max(trading_days, 1)
        print(f"    {model:<18}  {len(grp):>4} trades  {mwr:.0%} WR  ${mpnl:+.2f}  ({mperday:.1f}/day)")

    # By direction
    print()
    print("  By direction:")
    for direction, grp in df.groupby("direction"):
        dw  = grp["result"].isin(["WIN", "BREAKEVEN"]).sum()
        dwr = dw / len(grp) if len(grp) else 0.0
        dpnl = grp["pnl"].sum()
        print(f"    {direction:<6}  {len(grp):>4} trades  {dwr:.0%} WR  ${dpnl:+.2f}")

    # By session
    print()
    print("  By session:")
    for sess, grp in df.groupby("session"):
        sw  = grp["result"].isin(["WIN", "BREAKEVEN"]).sum()
        swr = sw / len(grp) if len(grp) else 0.0
        spnl = grp["pnl"].sum()
        print(f"    {sess:<22}  {len(grp):>4} trades  {swr:.0%} WR  ${spnl:+.2f}")

    # Exit reasons
    if res["exit_reason_tally"]:
        print()
        print("  Exit reasons:")
        for reason, count in sorted(res["exit_reason_tally"].items(), key=lambda x: -x[1]):
            print(f"    {reason:<20} {count:>5}")

    # Rejection reasons
    if res["reject_reasons"]:
        print()
        print("  Rejection reasons (signal generated but blocked):")
        for reason, count in sorted(res["reject_reasons"].items(), key=lambda x: -x[1]):
            print(f"    {reason:<25} {count:>5}")

    # Regime at entry
    print()
    print("  Regime at entry:")
    for regime, grp in df.groupby("regime"):
        rw = grp["result"].isin(["WIN", "BREAKEVEN"]).sum()
        rwr = rw / len(grp) if len(grp) else 0.0
        print(f"    {regime:<18}  {len(grp):>4} trades  {rwr:.0%} WR")

    print("=" * W)


# ─── Data loading ─────────────────────────────────────────────────────────────

def _load_csv(path: str, label: str) -> pd.DataFrame:
    print(f"Loading {label} ({path}) ...")
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index, utc=True)
    df.columns = [c.lower() for c in df.columns]
    df = df.sort_index()
    df = df[df["close"] > 0]
    print(f"  {len(df):>8,} bars  |  {df.index[0]:%Y-%m-%d}  to  {df.index[-1]:%Y-%m-%d}")
    return df


def _count_trading_days(df_m5: pd.DataFrame) -> int:
    """Count days that have bars in any active session."""
    dates = set()
    for dt in df_m5.index:
        t = dt.time()
        if time(7, 0) <= t < time(21, 0):
            dates.add(dt.date())
    return len(dates)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="HONORED backtest — combined (default) or per-model.")
    p.add_argument("--m5",      default="data/XAUUSD_M5.csv")
    p.add_argument("--h1",      default="data/XAUUSD_H1.csv")
    p.add_argument("--h4",      default="data/XAUUSD_H4.csv")
    p.add_argument("--balance", type=float, default=20.0)
    p.add_argument("--model",   choices=["A", "B", "C", "combined"],
                   default="combined",
                   help="Which model to test (default: combined)")
    a = p.parse_args()

    # Load data
    df_m5 = _load_csv(a.m5, "M5")
    df_h1 = _load_csv(a.h1, "H1")
    df_h4 = _load_csv(a.h4, "H4")
    total_bars = len(df_m5)

    if total_bars < _WARMUP_M5 + 50:
        print(f"ERROR: Need at least {_WARMUP_M5 + 50} M5 bars.", file=sys.stderr)
        sys.exit(1)

    print("\nComputing M5 indicators ...")
    df_m5_ind = add_indicators(df_m5.copy())

    print("Precomputing 6-state regime for each M5 bar ...")
    regime_map = _precompute_regime(df_h1, df_h4, df_m5_ind)
    print("Done.\n")

    asian_ranges = _precompute_asian_ranges(df_m5)
    trading_days = _count_trading_days(df_m5)

    model_map = {
        "A": {MODEL_A},
        "B": {MODEL_B},
        "C": {MODEL_C},
        "combined": {MODEL_A, MODEL_B, MODEL_C},
    }

    models = model_map[a.model]
    label = "COMBINED" if a.model == "combined" else f"Model {a.model}"

    print(f"\n{'─' * 50}")
    print(f"  Running backtest: {label}")
    print(f"{'─' * 50}")

    res = run_backtest(df_m5_ind, asian_ranges, regime_map, a.balance,
                       total_bars, models_to_run=models)
    _print_summary(res, a.balance, trading_days)


if __name__ == "__main__":
    main()
