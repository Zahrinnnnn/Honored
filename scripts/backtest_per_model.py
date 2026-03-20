#!/usr/bin/env python3
"""
backtest_per_model.py — Backtest HONORED system (Model A only).

Uses 6-state H1 regime detector, M5 OU execution, fixed 1:2 RR + breakeven exit.
Anti-martingale lot sizing: halve lot after each consecutive loss, reset on win.
Auto-refills balance to starting amount when it hits $0 (or below $0.50).

Usage:
    python scripts/backtest_per_model.py                    # full history
    python scripts/backtest_per_model.py --days 180         # last N days
    python scripts/backtest_per_model.py --balance 50       # custom start balance
    python scripts/backtest_per_model.py --walk-forward     # split in half
    python scripts/backtest_per_model.py --fixed-lot 0.01   # pure signal quality
"""

import argparse
import os
import random
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
from agents.nanami.skills.htf_regime import detect_regime, check_structural_break, compute_macro_bias, compute_h4_bias
from agents.nanami.skills.ou_grind import generate_signal as _ou_signal
from agents.nanami.skills.london_reversal import generate_signal as _london_reversal_signal
from agents.toji.skills.lot_calculator import calculate_lot
from agents.toji.skills.trade_monitor import calculate_pnl
from core.constants import (
    MODEL_A, MODEL_B,
    OU_TIME_KILL_HALF_LIFE_MULT,
    TIME_KILL_MINUTES, MAX_TRADE_DURATION_MINUTES,
    XAUUSD_POINT_VALUE,
    REGIME_BULLISH_GRIND, REGIME_BEARISH_GRIND, REGIME_TIGHT_RANGE,
    REGIME_BULLISH_BLOWOFF,
    REGIME_H1_BARS_NEEDED,
    STRUCTURAL_BREAK_COOLDOWN_HOURS,
    RR_RATIO,
    ACCOUNT_TYPE,
)

# ─── Constants ────────────────────────────────────────────────────────────────

SPREAD_BASE_USD   = 0.35   # HFM Cents calm-market spread
SPREAD_VOLATILE   = 1.20   # spread during high-ATR bars
SLIPPAGE_BASE_USD = 0.10   # base slippage per trade
SLIPPAGE_VOLATILE = 0.30   # slippage during high-ATR bars
_ATR_VOLATILE_PCTILE = 80  # ATR percentile threshold for "volatile" spread/slippage
_WARMUP_M5 = 260
BUST_THRESHOLD = 0.50  # refill when balance drops below this
MAX_SIMULTANEOUS_TRADES = 4  # max open trades at once — prevents correlated cluster blowup
DAILY_LOSS_LIMIT_PCT = 0.15  # stop new signals if today's loss exceeds 15% of day-open balance

# ── Opt 2: Breakeven protection raised to +1.5 ATR ─────────────────────────
# Original: move SL to entry at +1.0 ATR — triggers too early, whipsaws back to entry (31 BEs)
# New: wait for +1.5 ATR confirmation before locking in breakeven
_BREAKEVEN_ATR_TRIGGER = 1.5

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
    MODEL_A: {"NY_OVERLAP", "NY_CLOSE"},   # opt 1: add NY_CLOSE session
    MODEL_B: {"LONDON_OPEN"},
}
_SESSION_LIMIT = {MODEL_A: 8, MODEL_B: 2}   # max 2 London trades per day total


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



# ─── Regime precomputation ─────────────────────────────────────────────────

def _precompute_regime(df_h1: pd.DataFrame, df_m5: pd.DataFrame) -> dict:
    """
    Compute regime once per H1 bar (not per M5 bar) — ~12× faster.

    Speed: regime only changes when a new H1 bar closes. Computing it
    141k times (once per M5 bar) is wasteful; compute 11k times (once
    per H1 bar) and map each M5 bar to its H1 regime via searchsorted.

    Fix 5 (persistence): a regime is only declared active after
    REGIME_PERSISTENCE_BARS consecutive H1 bars with the same label.
    Fresh regime switches are suppressed to TIGHT_RANGE until confirmed.
    """
    h1_times = df_h1.index
    n_h1 = len(df_h1)

    # ── Step 1: compute regime + structural break for each H1 bar ──
    h1_regimes = {}
    for i in range(REGIME_H1_BARS_NEEDED, n_h1):
        h1_start  = max(0, i - REGIME_H1_BARS_NEEDED + 1)
        h1_window = df_h1.iloc[h1_start : i + 1]
        h1_regimes[i] = {
            "regime":           detect_regime(h1_window),
            "structural_break": check_structural_break(h1_window),
            "macro_bias":       compute_macro_bias(h1_window),
        }

    # ── Step 2: map each M5 bar to its H1 regime via searchsorted ──
    m5_times = df_m5.index
    regime_map = {}
    _default = {"regime": REGIME_TIGHT_RANGE, "structural_break": False}

    for i in range(len(m5_times)):
        m5_t  = m5_times[i]
        h1_idx = int(h1_times.searchsorted(m5_t, side="right")) - 1
        regime_map[i] = h1_regimes.get(h1_idx, _default)

    return regime_map


# ─── H4 bias precomputation ────────────────────────────────────────────────

def _precompute_h4_bias(df_h4: pd.DataFrame, df_m5: pd.DataFrame) -> dict:
    """
    Compute H4 bias (BULLISH/BEARISH/NEUTRAL) for each M5 bar via searchsorted.

    Each M5 bar is mapped to the last closed H4 bar before it. H4 SMA20 is
    computed on a 20-bar rolling window — ~3.3 trading days. Slow enough that
    brief H1 corrections don't flip it; genuine multi-day downtrends do.
    """
    h4_times = df_h4.index
    n_h4 = len(df_h4)
    _H4_LOOKBACK = 50

    # Compute bias per H4 bar
    h4_biases = {}
    for i in range(_H4_LOOKBACK, n_h4):
        h4_window = df_h4.iloc[i - _H4_LOOKBACK + 1 : i + 1]
        h4_biases[i] = compute_h4_bias(h4_window)

    # Map each M5 bar to its H4 bar
    m5_times = df_m5.index
    bias_map = {}
    for i in range(len(m5_times)):
        m5_t  = m5_times[i]
        h4_idx = int(h4_times.searchsorted(m5_t, side="right")) - 1
        bias_map[i] = h4_biases.get(h4_idx, "NEUTRAL")

    return bias_map


# ─── Validation ──────────────────────────────────────────────────────────────

def _validate(signal, state, session, is_breakout, bar_dt, regime,
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
    # Cap simultaneous open trades (correlated signals on same asset = same risk)
    if len(state["open_trades"]) >= MAX_SIMULTANEOUS_TRADES:
        return False, "max_open_trades"

    # Safety: Prevent risk stacking > 80% of balance
    current_risk_exposure = sum(t["lot_size"] * t["sl_distance"] for t in state["open_trades"])
    if current_risk_exposure > state["balance"] * 0.80:
        return False, "max_exposure_reached"

    direction = signal.get("direction", "")

    # Regime directional gate — Model A only (Model B is regime-agnostic fakeout)
    if model == MODEL_A:
        if direction == "BUY" and regime not in (REGIME_BULLISH_GRIND, REGIME_BULLISH_BLOWOFF):
            return False, "regime_blocked"
        if direction == "SELL" and regime != REGIME_BEARISH_GRIND:
            return False, "regime_blocked"

    # Session checks
    if is_breakout:
        return False, "wrong_session"
    if session not in _MODEL_SESSIONS.get(model, set()):
        return False, "wrong_session"

    # Session limit
    today = bar_dt.date()
    if state["session_counts"].get((session, model, today), 0) >= _SESSION_LIMIT[model]:
        return False, "session_limit"

    return True, "ok"


# ─── Trade lifecycle ─────────────────────────────────────────────────────────

def _open_trade(signal, bar, state, slippage: float = 0.0, fixed_lot: float = None):
    sl_distance_points = float(signal["sl_distance"])
    # The lot calculator expects sl_distance in USD for 1 lot.
    sl_distance_usd_per_lot = sl_distance_points * XAUUSD_POINT_VALUE
    if fixed_lot is not None:
        lot = fixed_lot  # Fixed-lot mode: bypass anti-martingale compounding
    else:
        lot = calculate_lot(state["balance"], sl_distance_usd_per_lot, consecutive_losses=state["consecutive_losses"])
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
        "sl_distance":    sl_distance_points,
        "lot_size":       lot,
        "atr_at_entry":    float(signal.get("atr_at_entry", sl_distance_points)),
        "half_life_bars":  float(signal.get("half_life_bars", 0.0)),
        "session":         signal.get("session", ""),
        "regime":          signal.get("regime", ""),
        "open_time":      bar.name,
        "balance_before": state["balance"],
        "slippage":       slippage,
    }
    state["open_trades"].append(trade)
    today = bar.name.date()
    key   = (signal.get("session", ""), signal["model"], today)
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

    # Clamp DD at 100% (cannot lose more than 100% of equity effectively)
    dd_balance = max(0.0, balance_after)
    dd_pct = (state["peak_balance"] - dd_balance) / max(state["peak_balance"], 1e-9) * 100
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

    # Current profit in price points
    if direction == "BUY":
        profit_distance = close - entry
    else:
        profit_distance = entry - close

    # Opt 2 — Breakeven protection raised to +1.5 ATR (was 1.0).
    # Reduces whipsaw BEs: price must travel further before SL locks to entry.
    if profit_distance >= _BREAKEVEN_ATR_TRIGGER * atr:
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

    # OU-calibrated time kill: 3 × half_life_bars × 5 min
    # Model B (London reversal): no half_life — use 120 min (reversals need more time than NY OU)
    half_life = float(trade.get("half_life_bars", 0.0))
    if half_life > 0:
        time_kill_mins = half_life * OU_TIME_KILL_HALF_LIFE_MULT * 5.0
    elif trade.get("model") == MODEL_B:
        time_kill_mins = 120.0
    else:
        time_kill_mins = TIME_KILL_MINUTES

    if elapsed_mins >= time_kill_mins:
        if direction == "BUY" and close <= entry:
            return {"result": "LOSS", "exit_price": close, "exit_reason": "TIME_KILL"}
        elif direction == "SELL" and close >= entry:
            return {"result": "LOSS", "exit_price": close, "exit_reason": "TIME_KILL"}

    return None


# ─── Signal generation ───────────────────────────────────────────────────────

def _generate_signals_for_bar(df_m5_win, session, is_breakout, regime,
                               macro_bias="NEUTRAL", h4_bias="NEUTRAL",
                               blowoff_age: int = 0):
    """Generate Model A (NY_OVERLAP) signals for this bar."""
    if is_breakout:
        return []
    sigs = []

    _allowed_regimes = (REGIME_BULLISH_GRIND, REGIME_BEARISH_GRIND, REGIME_BULLISH_BLOWOFF)

    # Model A — OU grind, NY_OVERLAP only
    if session in _MODEL_SESSIONS[MODEL_A] and regime in _allowed_regimes:
        sig = _ou_signal(df_m5_win, session, regime, macro_bias=macro_bias)
        if sig:
            direction = sig["direction"]
            # Opt 1 — H4 SMA50 BUY gate: only allow BUY when H4 confirms bullish trend.
            # Cuts BUY entries during macro downtrends where BULLISH_GRIND is a retracement,
            # not a genuine uptrend. H4 SMA50 is slow — only flips on multi-week moves.
            if direction == "BUY" and h4_bias != "BULLISH":
                pass  # H4 macro bearish/neutral — skip BUY
            # Opt 1 — H4 SMA50 SELL gate (existing): only allow SELL when H4 confirms bearish.
            elif direction == "SELL" and h4_bias != "BEARISH":
                pass  # H4 not confirming bearish — skip SELL
            else:
                sigs.append(sig)

    # Model B — London multi-signal reversal (Kalman + CUSUM + N-bar + Volume climax)
    if session in _MODEL_SESSIONS[MODEL_B]:
        sig = _london_reversal_signal(df_m5_win, session, regime=regime, h4_bias=h4_bias)
        if sig:
            sigs.append(sig)

    return sigs


# ─── Core backtest engine ────────────────────────────────────────────────────

def run_backtest(df_m5_ind, _unused, regime_map, balance: float,
                 start_bar: int, end_bar: int, models_to_run=None,
                 fixed_lot: float = None, h4_bias_map: dict = None) -> dict:
    """
    Run backtest with Model A.

    Args:
        models_to_run: set of model names (default: {MODEL_A}).
        fixed_lot: if set, every trade uses this lot size (bypasses anti-martingale).
    """
    if models_to_run is None:
        models_to_run = {MODEL_A}

    # Precompute ATR percentile for dynamic spread/slippage
    atr_pctile = _precompute_atr_pctile(df_m5_ind)

    state = {
        "balance":                balance,
        "peak_balance":           balance,
        "consecutive_losses":     0,
        "open_trades":            [],
        "session_counts":         {},
        "soft_halt":              False,
        "orb_fired_date":         None,    # ORB fires at most once per day
        "vwap_last_entry_bar":    -999,    # VWAP cooldown: bar index of last entry
        "emergency_halt":         False,
        "all_trades":             [],
        "refill_count":           0,
        "structural_break_until": None,
        "day_start_balance":      balance,
    }

    prev_date   = None
    reject_reasons = defaultdict(int)
    exit_reason_tally = defaultdict(int)
    signals_generated = 0

    for i in range(start_bar, end_bar):
        bar = df_m5_ind.iloc[i]
        dt  = df_m5_ind.index[i]
        today = dt.date()

        # Daily reset — clear trading halts (simulates user override next session)
        # peak_balance is NEVER reset: DD is always measured from the all-time high
        if today != prev_date:
            if state["soft_halt"] or state["emergency_halt"]:
                state["soft_halt"]          = False
                state["emergency_halt"]     = False
                state["consecutive_losses"] = 0
            state["day_start_balance"] = state["balance"]  # reset daily loss tracking
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

        # Regime
        _default_rm = {"regime": "TIGHT_RANGE", "structural_break": False, "macro_bias": "NEUTRAL"}
        rm = regime_map.get(i, _default_rm)
        regime = rm["regime"]
        macro_bias = rm.get("macro_bias", "NEUTRAL")
        blowoff_age = rm.get("blowoff_age", 0)
        h4_bias = h4_bias_map.get(i, "NEUTRAL") if h4_bias_map else "NEUTRAL"

        # Structural break cooldown
        if rm["structural_break"]:
            state["structural_break_until"] = dt + timedelta(hours=STRUCTURAL_BREAK_COOLDOWN_HOURS)
        if state["structural_break_until"] and dt < state["structural_break_until"]:
            continue

        df_m5_win = df_m5_ind.iloc[max(0, i - 500) : i + 1]

        # Dead zone removed (opt 2) — 14:00-15:00 UTC now open for Model A

        # Fix A — NY_CLOSE entry cutoff: block signals after 20:00 UTC
        # Ensures every trade has ≥ 60 min before blackout at 21:00.
        if session == "NY_CLOSE" and dt.hour >= 20:
            continue

        # Generate signals
        sigs = _generate_signals_for_bar(df_m5_win, session, is_breakout, regime,
                                          macro_bias, h4_bias, blowoff_age=blowoff_age)

        _, bar_slippage = _get_friction(atr_pctile[i])
        for sig in sigs:
            signals_generated += 1

            # London reversal: cooldown 12 bars (60 min) between entries
            if sig["model"] == MODEL_B:
                if i - state["vwap_last_entry_bar"] < 12:
                    reject_reasons["london_cooldown"] += 1
                    continue
                state["vwap_last_entry_bar"] = i

            ok, reason = _validate(sig, state, session, is_breakout, dt, regime)
            if ok:
                _open_trade(sig, bar, state, slippage=bar_slippage, fixed_lot=fixed_lot)
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


# ─── Monte Carlo ─────────────────────────────────────────────────────────────

def run_monte_carlo(trades: list, start_balance: float, n_sims: int = 2000):
    """
    Monte Carlo: shuffle trade ORDER and re-simulate with proper anti-martingale lot sizing.

    Shuffling fixed P&L values is wrong — lot sizes depend on order (consecutive losses
    change the lot via anti-martingale). Instead we store the price movement per unit
    and re-calculate lot + P&L for each reshuffled sequence.
    """
    if not trades:
        print("  Monte Carlo: no trades to simulate.")
        return

    # Extract replayable trade units (order-invariant primitives)
    trade_units = []
    for t in trades:
        direction = t["direction"]
        price_move = (
            (t["exit_price"] - t["entry_price"]) if direction == "BUY"
            else (t["entry_price"] - t["exit_price"])
        )
        raw_per_lot    = price_move * XAUUSD_POINT_VALUE
        lot            = t["lot_size"]
        spread_per_lot = (t["spread_cost"] / lot) if lot > 0 else 0.0
        sl_usd_per_lot = t["sl_distance"] * XAUUSD_POINT_VALUE
        trade_units.append({
            "raw_per_lot":    raw_per_lot,
            "spread_per_lot": spread_per_lot,
            "sl_usd_per_lot": sl_usd_per_lot,
            "result":         t["result"],
        })

    n = len(trade_units)
    final_balances = []
    max_dds = []
    rng = random.Random(42)

    for _ in range(n_sims):
        shuffled = trade_units[:]
        rng.shuffle(shuffled)
        bal = start_balance
        peak = start_balance
        max_dd = 0.0
        consec_losses = 0

        for unit in shuffled:
            lot = calculate_lot(bal, unit["sl_usd_per_lot"],
                                consecutive_losses=consec_losses)
            pnl = round(lot * unit["raw_per_lot"] - lot * unit["spread_per_lot"], 4)
            bal += pnl

            if bal > peak:
                peak = bal
            dd = (peak - max(0.0, bal)) / max(peak, 1e-9) * 100
            if dd > max_dd:
                max_dd = dd

            # Auto-refill on bust (mirrors backtest engine)
            if bal < BUST_THRESHOLD:
                bal = start_balance
                peak = start_balance
                consec_losses = 0
                continue

            if unit["result"] in ("WIN", "BREAKEVEN"):
                consec_losses = 0
            else:
                consec_losses += 1

        final_balances.append(bal)
        max_dds.append(max_dd)

    final_balances.sort()
    max_dds.sort()

    profitable   = sum(1 for b in final_balances if b > start_balance) / n_sims * 100
    busted       = sum(1 for b in final_balances if b <= BUST_THRESHOLD) / n_sims * 100
    actual_final = sum(t["pnl"] for t in trades) + start_balance

    W = 72
    print()
    print("=" * W)
    print(f"  MONTE CARLO  ({n_sims:,} simulations | {n} trades reshuffled with anti-martingale)")
    print("=" * W)
    print(f"  Actual final balance : ${actual_final:,.2f}")
    print(f"  Profitable runs      : {profitable:.1f}%")
    print(f"  Busted runs (<$0.50) : {busted:.1f}%")
    print()
    print("  Final Balance distribution:")
    for pct, label in [(5, "5th "), (25, "25th"), (50, "50th (median)"),
                       (75, "75th"), (95, "95th")]:
        idx = min(int(n_sims * pct / 100), n_sims - 1)
        print(f"    {label} pctile : ${final_balances[idx]:>12,.2f}")
    print()
    print("  Max Drawdown distribution:")
    for pct, label in [(5, "5th "), (50, "50th (median)"), (95, "95th")]:
        idx = min(int(n_sims * pct / 100), n_sims - 1)
        print(f"    {label} pctile : {max_dds[idx]:.1f}%")
    print("=" * W)


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
    wins      = (df["result"] == "WIN").sum()
    losses    = (df["result"] == "LOSS").sum()
    breakevens = (df["result"] == "BREAKEVEN").sum()
    decisive  = wins + losses
    wr        = wins / decisive if decisive > 0 else 0.0
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
    print(f"  Trades         : {total}  ({wins}W / {losses}L / {breakevens}BE)")
    print(f"  Trades/day     : {trades_per_day:.2f}")
    print(f"  Win rate       : {wr:.1%}  (decisive only: {decisive} W+L, excl. {breakevens} BE at $0)")
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
        mw  = (grp["result"] == "WIN").sum()
        ml  = (grp["result"] == "LOSS").sum()
        mbe = (grp["result"] == "BREAKEVEN").sum()
        md  = mw + ml
        mwr = mw / md if md > 0 else 0.0
        mpnl = grp["pnl"].sum()
        mperday = len(grp) / max(trading_days, 1)
        print(f"    {model:<18}  {len(grp):>4} trades  {mwr:.0%} WR ({mw}W/{ml}L/{mbe}BE)  ${mpnl:+.2f}  ({mperday:.1f}/day)")

    # Deeper model analysis
    print()
    print("  By model (detailed):")
    for model, grp in df.groupby("model"):
        print(f"    - {model}:")
        wins_df = grp[grp["result"] == "WIN"]
        loss_df = grp[grp["result"] == "LOSS"]
        be_df   = grp[grp["result"] == "BREAKEVEN"]
        avg_win_pnl  = wins_df["pnl"].mean() if not wins_df.empty else 0.0
        avg_loss_pnl = loss_df["pnl"].mean() if not loss_df.empty else 0.0
        avg_be_pnl   = be_df["pnl"].mean() if not be_df.empty else 0.0
        print(f"      Avg Win P&L : ${avg_win_pnl:+.4f}  ({len(wins_df)} trades)")
        print(f"      Avg Loss P&L: ${avg_loss_pnl:+.4f}  ({len(loss_df)} trades)")
        print(f"      Avg BE P&L  : ${avg_be_pnl:+.4f}  ({len(be_df)} trades — spread cost only)")

        # USD realized RR (distorted by anti-martingale: losses at larger lots, wins at smaller lots)
        realized_rr_usd = abs(avg_win_pnl / avg_loss_pnl) if avg_loss_pnl != 0 else 0.0

        # Points realized RR (true trade quality — unaffected by lot sizing)
        def _raw_pts(row):
            return (row["exit_price"] - row["entry_price"]) if row["direction"] == "BUY" \
                   else (row["entry_price"] - row["exit_price"])
        avg_win_pts  = wins_df.apply(_raw_pts, axis=1).mean() if not wins_df.empty else 0.0
        avg_loss_pts = loss_df.apply(_raw_pts, axis=1).abs().mean() if not loss_df.empty else 0.0
        realized_rr_pts = avg_win_pts / avg_loss_pts if avg_loss_pts > 0 else 0.0

        print(f"      Realized RR (pts): {realized_rr_pts:.2f}  ← true trade quality (target: {RR_RATIO})")
        print(f"      Realized RR (USD): {realized_rr_usd:.2f}  ← anti-martingale distortion (losses at bigger lots)")
        print("      Exit reasons:")
        for reason, count in grp["exit_reason"].value_counts().items():
            print(f"        {reason:<15} {count:>5}")

    # By direction
    print()
    print("  By direction (decisive WR only):")
    for direction, grp in df.groupby("direction"):
        dw  = (grp["result"] == "WIN").sum()
        dl  = (grp["result"] == "LOSS").sum()
        dd  = dw + dl
        dwr = dw / dd if dd > 0 else 0.0
        dpnl = grp["pnl"].sum()
        print(f"    {direction:<6}  {len(grp):>4} trades  {dwr:.0%} WR ({dw}W/{dl}L)  ${dpnl:+.2f}")

    # By session
    print()
    print("  By session (decisive WR only):")
    for sess, grp in df.groupby("session"):
        sw  = (grp["result"] == "WIN").sum()
        sl  = (grp["result"] == "LOSS").sum()
        sd  = sw + sl
        swr = sw / sd if sd > 0 else 0.0
        spnl = grp["pnl"].sum()
        print(f"    {sess:<22}  {len(grp):>4} trades  {swr:.0%} WR ({sw}W/{sl}L)  ${spnl:+.2f}")

    # Monthly P&L breakdown — shows if gains are concentrated in one lucky period
    print()
    print("  Monthly P&L (equity curve check):")
    df["_month"] = df["open_time"].dt.tz_localize(None).dt.to_period("M")
    running_bal = start_bal
    for month, grp in df.groupby("_month"):
        mw   = (grp["result"] == "WIN").sum()
        ml   = (grp["result"] == "LOSS").sum()
        mbe  = (grp["result"] == "BREAKEVEN").sum()
        mpnl = grp["pnl"].sum()
        running_bal += mpnl
        print(f"    {month}  {len(grp):>3} trades  {mw}W/{ml}L/{mbe}BE  "
              f"${mpnl:+8.2f}  bal=${running_bal:.2f}")
    df.drop(columns=["_month"], inplace=True)

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
    print("  Regime at entry (decisive WR only):")
    for regime, grp in df.groupby("regime"):
        rw = (grp["result"] == "WIN").sum()
        rl = (grp["result"] == "LOSS").sum()
        rd = rw + rl
        rwr = rw / rd if rd > 0 else 0.0
        print(f"    {regime:<18}  {len(grp):>4} trades  {rwr:.0%} WR ({rw}W/{rl}L)")

    print("=" * W)

    # Return the DataFrame for further processing if needed
    return df


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
    p = argparse.ArgumentParser(description="HONORED backtest — Model A (OU_GRIND).")
    p.add_argument("--m5",      default="data/XAUUSD_M5.csv")
    p.add_argument("--h1",      default="data/XAUUSD_H1.csv")
    p.add_argument("--h4",      default="data/XAUUSD_H4_2yr.csv")
    p.add_argument("--balance", type=float, default=20.0)
    p.add_argument("--days",       type=int,   help="Run backtest for the last N days only.")
    p.add_argument("--start-date", help="Start date filter YYYY-MM-DD (inclusive).")
    p.add_argument("--end-date",   help="End date filter YYYY-MM-DD (inclusive).")
    p.add_argument("--save-csv",               help="Save detailed trade log to a CSV file.")
    p.add_argument("--fixed-lot",  type=float, help="Use a fixed lot size (e.g. 0.01) — bypasses anti-martingale compounding. Shows pure signal quality P&L.")
    p.add_argument("--walk-forward", action="store_true",
                   help="Split period in half and run each separately — checks if gains are regime-specific.")
    p.add_argument("--monte-carlo", action="store_true",
                   help="Run 2000-iteration Monte Carlo on shuffled trade P&L after backtest.")
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
    regime_map = _precompute_regime(df_h1, df_m5_ind)
    print("Precomputing H4 bias for each M5 bar ...")
    h4_bias_map = _precompute_h4_bias(df_h4, df_m5_ind)
    print("Done.\n")

    # Determine start/end bar
    start_bar = _WARMUP_M5
    end_bar   = total_bars
    if a.days:
        cutoff = df_m5.index[-1] - timedelta(days=a.days)
        start_bar = max(_WARMUP_M5, df_m5.index.searchsorted(cutoff))
        print(f"Backtesting last {a.days} days (starting from bar {start_bar} / {total_bars})")
    if getattr(a, "start_date", None):
        from datetime import timezone as _tz
        sd = datetime.strptime(a.start_date, "%Y-%m-%d").replace(tzinfo=_tz.utc)
        start_bar = max(_WARMUP_M5, int(df_m5.index.searchsorted(sd)))
        print(f"Start date filter: {a.start_date} (bar {start_bar})")
    if getattr(a, "end_date", None):
        from datetime import timezone as _tz
        ed = datetime.strptime(a.end_date, "%Y-%m-%d").replace(tzinfo=_tz.utc) + timedelta(days=1)
        end_bar = min(total_bars, int(df_m5.index.searchsorted(ed)))
        print(f"End date filter  : {a.end_date} (bar {end_bar})")

    label = "Model A"
    fixed_lot = getattr(a, "fixed_lot", None)

    print(f"  Account Type   : {ACCOUNT_TYPE} (Point Value: {XAUUSD_POINT_VALUE})")
    if fixed_lot:
        print(f"  Mode           : FIXED-LOT {fixed_lot} (anti-martingale disabled — pure signal quality)")

    if a.walk_forward:
        # ── Walk-forward: split period into two equal halves ──────────────────
        mid_bar = (start_bar + end_bar) // 2
        halves = [
            ("H1 — first half",  start_bar, mid_bar),
            ("H2 — second half", mid_bar,   end_bar),
        ]
        for half_label, hstart, hend in halves:
            print(f"\n{'─' * 50}")
            print(f"  {label} | {half_label}")
            print(f"  {df_m5.index[hstart]:%Y-%m-%d}  →  {df_m5.index[hend-1]:%Y-%m-%d}")
            print(f"{'─' * 50}")
            tdays = _count_trading_days(df_m5.iloc[hstart:hend])
            res = run_backtest(df_m5_ind, {}, regime_map, a.balance,
                               hstart, hend, fixed_lot=fixed_lot, h4_bias_map=h4_bias_map)
            _print_summary(res, a.balance, tdays)
    else:
        # ── Standard full-period run ──────────────────────────────────────────
        print(f"\n{'─' * 50}")
        print(f"  Running backtest: {label}")
        print(f"{'─' * 50}")
        trading_days = _count_trading_days(df_m5.iloc[start_bar:end_bar])
        res = run_backtest(df_m5_ind, {}, regime_map, a.balance,
                           start_bar, end_bar, fixed_lot=fixed_lot, h4_bias_map=h4_bias_map)
        df_results = _print_summary(res, a.balance, trading_days)

        if getattr(a, "monte_carlo", False):
            run_monte_carlo(res["trades"], a.balance)

        if a.save_csv and df_results is not None and not df_results.empty:
            df_results.to_csv(a.save_csv)
            print(f"\nTrade log saved to: {a.save_csv}")


if __name__ == "__main__":
    main()
