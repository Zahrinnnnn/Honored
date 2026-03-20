#!/usr/bin/env python3
"""
debug_model_a.py — Comprehensive Model A (OU_GRIND) diagnostic.

Answers:
  1. Decisive WR by session, direction, hour, z-score, half-life, detrend
  2. Realized RR breakdown — why avg win < 2× avg loss
  3. Exit reason breakdown — TP / SL / BE / TIME_KILL / MAX_DURATION
  4. Spread/slippage transparency (NOT random — fixed 2-tier)
  5. Max simultaneous trades sensitivity: 1 / 2 / 3 / 4 / 5
  6. Sharpe ratio (trade-level annualised)
  7. Signal rejection breakdown per session
  8. Dead zone impact (14:00-15:00 UTC blocked vs unblocked)

Usage:
    python scripts/debug_model_a.py --days 180
    python scripts/debug_model_a.py --days 180 --show-be   # show BE as separate result
"""

import argparse
import os
import sys
from collections import defaultdict
from datetime import time, timedelta

import numpy as np
import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv()

from agents.nanami.skills.indicator_engine import add_indicators
from agents.nanami.skills.htf_regime import detect_regime, check_structural_break
from agents.nanami.skills.stat_tests import adf_stationary, fit_ou, ou_zscore
from agents.toji.skills.lot_calculator import calculate_lot
from core.constants import (
    XAUUSD_POINT_VALUE,
    REGIME_BULLISH_GRIND, REGIME_BEARISH_GRIND,
    REGIME_H1_BARS_NEEDED, STRUCTURAL_BREAK_COOLDOWN_HOURS,
    OU_LOOKBACK, OU_LOOKBACK_SHORT, OU_MIN_HALF_LIFE, OU_MAX_HALF_LIFE,
    OU_SL_ATR_MULT, OU_SL_MIN, OU_SL_MAX, OU_ZSCORE_GRIND_THRESHOLD,
    OU_ZSCORE_ENTRY_THRESHOLD, BREAKEVEN_ATR_THRESHOLD, OU_TIME_KILL_HALF_LIFE_MULT,
    MAX_TRADE_DURATION_MINUTES, RISK_PER_TRADE_PCT, RR_RATIO,
    NY_OVERLAP_DEAD_HOUR_START, NY_OVERLAP_DEAD_HOUR_END,
)

# ─── Session windows (Model A runs in all 3) ─────────────────────────────────

_SESSIONS = {
    "LONDON_OPEN": (time(7, 30), time(10, 0)),
    "NY_OVERLAP":  (time(12, 0), time(16, 0)),
    "NY_CLOSE":    (time(19, 0), time(21, 0)),
}
_WARMUP_M5   = 260

# Fixed 2-tier spread/slippage (NOT random — ATR percentile gates it)
_SPREAD_CALM     = 0.35   # calm market
_SPREAD_VOLATILE = 1.20   # high-ATR bar (ATR pctile > 80)
_SLIP_CALM       = 0.10
_SLIP_VOLATILE   = 0.30
_ATR_VOLATILE_PCTILE = 80

W = 74  # print width

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _session_of(t: time):
    for name, (start, end) in _SESSIONS.items():
        if start <= t < end:
            return name
    return None


def _load_csv(path, label):
    print(f"Loading {label} ({path}) ...")
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index, utc=True)
    df.columns = [c.lower() for c in df.columns]
    df = df.sort_index()
    df = df[df["close"] > 0]
    print(f"  {len(df):>8,} bars  |  {df.index[0]:%Y-%m-%d}  to  {df.index[-1]:%Y-%m-%d}")
    return df


def _precompute_regime(df_h1, df_m5):
    h1_times = df_h1.index.to_list()
    regime_map = {}
    h1_ptr = 0
    for i, m5_t in enumerate(df_m5.index):
        while h1_ptr < len(h1_times) - 1 and h1_times[h1_ptr + 1] <= m5_t:
            h1_ptr += 1
        h1_start  = max(0, h1_ptr - REGIME_H1_BARS_NEEDED + 1)
        h1_window = df_h1.iloc[h1_start:h1_ptr + 1]
        regime_map[i] = {
            "regime": detect_regime(h1_window),
            "sb":     check_structural_break(h1_window),
        }
    return regime_map


def _atr_percentile(df_m5, i, window=200):
    start = max(0, i - window)
    vals = df_m5["atr14"].iloc[start:i + 1]
    vals = vals[vals > 0]
    if len(vals) < 10:
        return 50.0
    return float(np.searchsorted(np.sort(vals.values), vals.iloc[-1]) / len(vals) * 100)


def _spread_slip(atr_pctile):
    if atr_pctile > _ATR_VOLATILE_PCTILE:
        return _SPREAD_VOLATILE, _SLIP_VOLATILE
    return _SPREAD_CALM, _SLIP_CALM


def _try_signal(df_win, regime):
    """Returns (signal_dict, reason_str)."""
    if regime not in (REGIME_BULLISH_GRIND, REGIME_BEARISH_GRIND):
        return None, "regime_not_grind"
    if len(df_win) < OU_LOOKBACK:
        return None, "not_enough_bars"

    last = df_win.iloc[-1]
    atr  = float(last.get("atr14", 0.0))
    if atr <= 0 or pd.isna(atr):
        return None, "atr_zero"

    closes = df_win["close"].values.astype(float)

    # ── EMA50 primary ──────────────────────────────────────────────────────
    ema50_col = df_win.get("ema50")
    if ema50_col is not None and not pd.isna(last.get("ema50", float("nan"))):
        ema50 = ema50_col.values.astype(float)
        c = closes[-OU_LOOKBACK:]
        e = ema50[-OU_LOOKBACK:]
        if not np.any(np.isnan(e)):
            res = c - e
            adf = adf_stationary(res)
            if adf["stationary"]:
                ou = fit_ou(res)
                if ou is not None and OU_MIN_HALF_LIFE <= ou["half_life_bars"] <= OU_MAX_HALF_LIFE:
                    z = ou_zscore(res[-1], ou)
                    sl = round(max(OU_SL_MIN, min(OU_SL_MAX, atr * OU_SL_ATR_MULT)), 2)
                    if regime == REGIME_BULLISH_GRIND and z < -OU_ZSCORE_GRIND_THRESHOLD:
                        return {"direction": "BUY",  "z": z, "detrend": "ema50",
                                "half_life": ou["half_life_bars"], "atr": atr, "sl_distance": sl,
                                "mu": ou["mu"], "sigma_eq": ou.get("sigma_eq", 0)}, None
                    if regime == REGIME_BEARISH_GRIND and z > OU_ZSCORE_GRIND_THRESHOLD:
                        return {"direction": "SELL", "z": z, "detrend": "ema50",
                                "half_life": ou["half_life_bars"], "atr": atr, "sl_distance": sl,
                                "mu": ou["mu"], "sigma_eq": ou.get("sigma_eq", 0)}, None

    # ── EMA21 fallback ─────────────────────────────────────────────────────
    ema21_col = df_win.get("ema21")
    if ema21_col is not None and not pd.isna(last.get("ema21", float("nan"))):
        if len(closes) >= OU_LOOKBACK_SHORT:
            ema21 = ema21_col.values.astype(float)
            c = closes[-OU_LOOKBACK_SHORT:]
            e = ema21[-OU_LOOKBACK_SHORT:]
            if not np.any(np.isnan(e)):
                res = c - e
                adf = adf_stationary(res)
                if adf["stationary"]:
                    ou = fit_ou(res)
                    if ou is not None and OU_MIN_HALF_LIFE <= ou["half_life_bars"] <= OU_MAX_HALF_LIFE:
                        z = ou_zscore(res[-1], ou)
                        sl = round(max(OU_SL_MIN, min(OU_SL_MAX, atr * OU_SL_ATR_MULT)), 2)
                        if regime == REGIME_BULLISH_GRIND and z < -OU_ZSCORE_ENTRY_THRESHOLD:
                            return {"direction": "BUY",  "z": z, "detrend": "ema21",
                                    "half_life": ou["half_life_bars"], "atr": atr, "sl_distance": sl,
                                    "mu": ou["mu"], "sigma_eq": ou.get("sigma_eq", 0)}, None
                        if regime == REGIME_BEARISH_GRIND and z > OU_ZSCORE_ENTRY_THRESHOLD:
                            return {"direction": "SELL", "z": z, "detrend": "ema21",
                                    "half_life": ou["half_life_bars"], "atr": atr, "sl_distance": sl,
                                    "mu": ou["mu"], "sigma_eq": ou.get("sigma_eq", 0)}, None

    return None, "no_signal"


def _simulate_trade(sig, df_m5_full, open_idx, entry_price, spread, slip):
    """Simulate trade bar-by-bar. Returns detailed result dict."""
    direction   = sig["direction"]
    sl_distance = sig["sl_distance"]
    half_life   = sig["half_life"]
    atr         = sig["atr"]

    sl_price = round(entry_price - sl_distance if direction == "BUY"
                     else entry_price + sl_distance, 2)
    tp_price = round(entry_price + sl_distance * RR_RATIO if direction == "BUY"
                     else entry_price - sl_distance * RR_RATIO, 2)

    time_kill_mins = half_life * OU_TIME_KILL_HALF_LIFE_MULT * 5.0
    sl_moved_to_be = False
    be_then_sl     = False
    max_fav        = 0.0
    max_adv        = 0.0
    open_dt        = df_m5_full.index[open_idx]

    for j in range(open_idx + 1, min(open_idx + 300, len(df_m5_full))):
        bar    = df_m5_full.iloc[j]
        bar_dt = df_m5_full.index[j]
        lo, hi = float(bar["low"]), float(bar["high"])
        close  = float(bar["close"])
        elapsed = (bar_dt - open_dt).total_seconds() / 60.0

        if direction == "BUY":
            max_fav = max(max_fav, hi - entry_price)
            max_adv = max(max_adv, entry_price - lo)
        else:
            max_fav = max(max_fav, entry_price - lo)
            max_adv = max(max_adv, hi - entry_price)

        if direction == "BUY":
            if lo <= sl_price:
                result = "BREAKEVEN" if sl_moved_to_be else "LOSS"
                if sl_moved_to_be:
                    be_then_sl = True
                exit_p = sl_price
                return _make_result(result, "SL_HIT", exit_p, entry_price, elapsed,
                                    max_fav, max_adv, sl_moved_to_be, be_then_sl,
                                    sig, open_dt, spread, slip)
            if hi >= tp_price:
                return _make_result("WIN", "TP_HIT", tp_price, entry_price, elapsed,
                                    max_fav, max_adv, sl_moved_to_be, be_then_sl,
                                    sig, open_dt, spread, slip)
        else:
            if hi >= sl_price:
                result = "BREAKEVEN" if sl_moved_to_be else "LOSS"
                if sl_moved_to_be:
                    be_then_sl = True
                exit_p = sl_price
                return _make_result(result, "SL_HIT", exit_p, entry_price, elapsed,
                                    max_fav, max_adv, sl_moved_to_be, be_then_sl,
                                    sig, open_dt, spread, slip)
            if lo <= tp_price:
                return _make_result("WIN", "TP_HIT", tp_price, entry_price, elapsed,
                                    max_fav, max_adv, sl_moved_to_be, be_then_sl,
                                    sig, open_dt, spread, slip)

        # Breakeven protection
        profit = (close - entry_price) if direction == "BUY" else (entry_price - close)
        if not sl_moved_to_be and profit >= BREAKEVEN_ATR_THRESHOLD * atr:
            if direction == "BUY" and sl_price < entry_price:
                sl_price = entry_price
                sl_moved_to_be = True
            elif direction == "SELL" and sl_price > entry_price:
                sl_price = entry_price
                sl_moved_to_be = True

        if elapsed >= MAX_TRADE_DURATION_MINUTES:
            result = "WIN" if profit > 0 else "LOSS"
            return _make_result(result, "MAX_DURATION", close, entry_price, elapsed,
                                max_fav, max_adv, sl_moved_to_be, be_then_sl,
                                sig, open_dt, spread, slip)

        if elapsed >= time_kill_mins:
            if (direction == "BUY" and close <= entry_price) or \
               (direction == "SELL" and close >= entry_price):
                return _make_result("LOSS", "TIME_KILL", close, entry_price, elapsed,
                                    max_fav, max_adv, sl_moved_to_be, be_then_sl,
                                    sig, open_dt, spread, slip)

    last_close = float(df_m5_full["close"].iloc[min(open_idx + 299, len(df_m5_full) - 1)])
    return _make_result("LOSS", "NO_EXIT", last_close, entry_price, 0,
                        max_fav, max_adv, sl_moved_to_be, be_then_sl,
                        sig, open_dt, spread, slip)


def _make_result(result, exit_reason, exit_price, entry_price, duration,
                 max_fav, max_adv, sl_moved_to_be, be_then_sl, sig, open_dt,
                 spread, slip):
    direction = sig["direction"]
    raw_pnl_pts = (exit_price - entry_price) if direction == "BUY" else (entry_price - exit_price)
    # Net P&L in points (spread on entry + spread on exit both cost money)
    net_pnl_pts = raw_pnl_pts - spread - slip
    return {
        "result":        result,
        "exit_reason":   exit_reason,
        "exit_price":    exit_price,
        "raw_pnl_pts":   raw_pnl_pts,
        "net_pnl_pts":   net_pnl_pts,
        "spread_used":   spread,
        "duration_min":  duration,
        "max_fav_pts":   round(max_fav, 2),
        "max_adv_pts":   round(max_adv, 2),
        "sl_to_be":      sl_moved_to_be,
        "be_then_sl":    be_then_sl,
        "direction":     direction,
        "z":             sig["z"],
        "detrend":       sig["detrend"],
        "half_life":     sig["half_life"],
        "atr":           sig["atr"],
        "sl_distance":   sig["sl_distance"],
        "open_dt":       open_dt,
        "open_hour_utc": open_dt.hour + open_dt.minute / 60,
    }


def _decisive_wr(df_sub):
    """Decisive win rate: WIN / (WIN + LOSS). Excludes BREAKEVEN."""
    wins   = (df_sub["result"] == "WIN").sum()
    losses = (df_sub["result"] == "LOSS").sum()
    total  = wins + losses
    return wins / total if total > 0 else 0.0, wins, losses, total


def _sharpe(pnl_series):
    """Trade-level Sharpe (annualised assuming 252 trading days, 1.98 trades/day avg)."""
    if len(pnl_series) < 3:
        return float("nan")
    mu  = np.mean(pnl_series)
    std = np.std(pnl_series, ddof=1)
    if std == 0:
        return float("nan")
    # Annualise: sqrt(trades per year). Assume ~2 trades/day × 252 days.
    return (mu / std) * np.sqrt(504)


def _max_dd_pct(pnl_series, start_balance=20.0):
    balance = start_balance
    peak    = start_balance
    max_dd  = 0.0
    for p in pnl_series:
        balance += p
        peak = max(peak, balance)
        dd = (peak - balance) / peak * 100
        max_dd = max(max_dd, dd)
    return max_dd


# ─── Sensitivity: simulate with N max simultaneous trades ────────────────────

def _run_with_cap(all_trades_df, cap, start_balance=20.0):
    """
    Re-simulate equity curve with a cap on simultaneously open trades.
    Uses trade open_dt + duration_min to determine overlap.
    Returns (total_pnl_$, decisive_wr, sharpe, max_dd_pct, n_trades_taken).
    """
    if all_trades_df.empty:
        return 0.0, 0.0, float("nan"), 0.0, 0

    rows = all_trades_df.sort_values("open_dt").copy()
    rows["close_dt"] = rows["open_dt"] + pd.to_timedelta(rows["duration_min"], unit="m")

    balance     = start_balance
    consecutive = 0
    pnls        = []
    taken       = []

    for _, row in rows.iterrows():
        # Count trades still open at this bar's open_dt
        open_count = sum(
            1 for t in taken
            if t["open_dt"] <= row["open_dt"] < t["close_dt"]
        )
        if open_count >= cap:
            continue  # skip — too many open

        lot = calculate_lot(balance, row["sl_distance"],
                            RISK_PER_TRADE_PCT, consecutive)
        pnl_pts = row["net_pnl_pts"]
        pnl_usd = pnl_pts * lot * XAUUSD_POINT_VALUE

        pnls.append(pnl_usd)
        balance += pnl_usd

        result = row["result"]
        if result == "WIN":
            consecutive = 0
        elif result == "LOSS":
            consecutive += 1
        # BREAKEVEN: don't reset, don't increment

        taken.append({
            "open_dt":  row["open_dt"],
            "close_dt": row["close_dt"],
        })

    if not pnls:
        return 0.0, 0.0, float("nan"), 0.0, 0

    # decisive WR from taken subset
    taken_results = rows.loc[rows.index[:len(taken)], "result"] if taken else pd.Series(dtype=str)
    # Re-use the taken rows directly
    taken_df = rows.iloc[:len(taken)]  # approximate — order preserved
    dwr, _, _, _ = _decisive_wr(taken_df)

    total_pnl = sum(pnls)
    sharpe    = _sharpe(pnls)
    mdd       = _max_dd_pct(pnls, start_balance)

    return total_pnl, dwr, sharpe, mdd, len(taken)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--m5",   default="data/XAUUSD_M5.csv")
    p.add_argument("--h1",   default="data/XAUUSD_H1.csv")
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--balance", type=float, default=20.0)
    a = p.parse_args()

    df_m5_raw = _load_csv(a.m5, "M5")
    df_h1     = _load_csv(a.h1, "H1")

    print("\nComputing M5 indicators ...")
    df_m5 = add_indicators(df_m5_raw.copy())

    print("Precomputing regime (this takes ~30s) ...")
    regime_map = _precompute_regime(df_h1, df_m5)

    cutoff    = df_m5.index[-1] - timedelta(days=a.days)
    start_bar = max(_WARMUP_M5, df_m5.index.searchsorted(cutoff))
    total_bars = len(df_m5)

    # Pre-compute ATR percentiles (rolling 200-bar)
    atr_vals = df_m5["atr14"].values
    atr_pctiles = np.zeros(total_bars)
    for i in range(start_bar, total_bars):
        window_start = max(0, i - 200)
        window = atr_vals[window_start:i + 1]
        window = window[window > 0]
        if len(window) >= 10:
            atr_pctiles[i] = float(
                np.searchsorted(np.sort(window), atr_vals[i]) / len(window) * 100
            )
        else:
            atr_pctiles[i] = 50.0

    print(f"\nRunning diagnostic: last {a.days} days\n")

    trades      = []
    rejections  = defaultdict(lambda: defaultdict(int))  # session → reason → count
    sb_until    = None

    for i in range(start_bar, total_bars):
        dt   = df_m5.index[i]
        t    = dt.time().replace(second=0, microsecond=0)
        sess = _session_of(t)
        if sess is None:
            continue

        rm     = regime_map.get(i, {"regime": "UNKNOWN", "sb": False})
        regime = rm["regime"]

        # Structural break cooldown
        if rm["sb"]:
            sb_until = dt + timedelta(hours=STRUCTURAL_BREAK_COOLDOWN_HOURS)
        if sb_until and dt < sb_until:
            rejections[sess]["structural_break"] += 1
            continue

        if regime not in (REGIME_BULLISH_GRIND, REGIME_BEARISH_GRIND):
            rejections[sess]["wrong_regime"] += 1
            continue

        # Dead zone: Model A blocked 14:00-15:00 UTC in NY_OVERLAP
        is_dead_zone = (sess == "NY_OVERLAP"
                        and NY_OVERLAP_DEAD_HOUR_START <= dt.hour < NY_OVERLAP_DEAD_HOUR_END)
        if is_dead_zone:
            rejections[sess]["dead_zone_14-15"] += 1
            continue

        df_win = df_m5.iloc[max(0, i - 500): i + 1]
        sig, reason = _try_signal(df_win, regime)

        if sig is None:
            rejections[sess][reason] += 1
            continue

        # Determine spread/slippage (2-tier, NOT random)
        spread, slip = _spread_slip(atr_pctiles[i])

        entry = round(
            float(df_m5["close"].iloc[i]) + slip * (1 if sig["direction"] == "BUY" else -1), 2
        )
        trade = _simulate_trade(sig, df_m5, i, entry, spread, slip)
        trade["session"]    = sess
        trade["regime"]     = regime
        trade["bar_idx"]    = i
        trade["atr_pctile"] = atr_pctiles[i]
        trades.append(trade)

    if not trades:
        print("No trades generated.")
        return

    df = pd.DataFrame(trades)

    # ── Header ────────────────────────────────────────────────────────────────
    print("=" * W)
    print("  MODEL A (OU_GRIND) — ALL SESSIONS — DIAGNOSTIC")
    print("=" * W)
    print(f"  Period : {df['open_dt'].min():%Y-%m-%d}  to  {df['open_dt'].max():%Y-%m-%d}")
    print()

    # ── SPREAD / SLIPPAGE TRANSPARENCY ───────────────────────────────────────
    print("  SPREAD / SLIPPAGE (fixed 2-tier — NOT random)")
    print("  " + "-" * 52)
    print(f"    Calm  market (ATR pctile ≤{_ATR_VOLATILE_PCTILE}): spread={_SPREAD_CALM}  slip={_SLIP_CALM}")
    print(f"    Volatile mkt (ATR pctile >{_ATR_VOLATILE_PCTILE}): spread={_SPREAD_VOLATILE}  slip={_SLIP_VOLATILE}")
    n_volatile = (df["atr_pctile"] > _ATR_VOLATILE_PCTILE).sum()
    n_calm     = len(df) - n_volatile
    print(f"    Trades in calm: {n_calm} ({n_calm/len(df):.0%})  |  volatile: {n_volatile} ({n_volatile/len(df):.0%})")
    avg_spread = df["spread_used"].mean()
    print(f"    Avg spread paid per trade: ${avg_spread:.2f}")
    print()

    # ── OVERALL SUMMARY ───────────────────────────────────────────────────────
    dwr, wins, losses, decisive = _decisive_wr(df)
    bes = (df["result"] == "BREAKEVEN").sum()
    total = len(df)

    wins_df   = df[df["result"] == "WIN"]
    losses_df = df[df["result"] == "LOSS"]
    avg_win_pts  = wins_df["raw_pnl_pts"].mean()  if not wins_df.empty  else 0.0
    avg_loss_pts = losses_df["raw_pnl_pts"].abs().mean() if not losses_df.empty else 0.0
    realized_rr  = avg_win_pts / avg_loss_pts if avg_loss_pts > 0 else float("nan")

    # Rough P&L: approximate with fixed lot
    avg_sl = df["sl_distance"].mean()
    lot1   = calculate_lot(a.balance, avg_sl, 0.20, 0)

    pnl_pts_series = df["net_pnl_pts"].values
    pnl_usd_series = pnl_pts_series * lot1 * XAUUSD_POINT_VALUE
    sharpe = _sharpe(pnl_usd_series)
    days   = (df["open_dt"].max() - df["open_dt"].min()).days or 1
    tpd    = total / days

    print("  OVERALL SUMMARY")
    print("  " + "-" * 52)
    print(f"    Total trades     : {total}  ({wins}W / {losses}L / {bes}BE)")
    print(f"    Decisive WR      : {dwr:.1%}  (excl. {bes} breakevens)")
    print(f"    Trades / day     : {tpd:.2f}")
    print(f"    Avg WIN  pts     : +{avg_win_pts:.2f}  (target +{avg_sl*RR_RATIO:.2f})")
    print(f"    Avg LOSS pts     : -{avg_loss_pts:.2f}  (target -{avg_sl:.2f})")
    print(f"    Realized RR      : {realized_rr:.2f}  (target {RR_RATIO:.1f})")
    print(f"    Sharpe (approx)  : {sharpe:.2f}  [fixed-lot, annualised]")
    tp_n = (df["exit_reason"] == "TP_HIT").sum()
    sl_n = (df["exit_reason"] == "SL_HIT").sum()
    print(f"    TP hits / SL hits: {tp_n} / {sl_n}  (ratio {tp_n/max(sl_n,1):.2f})")
    print()

    # ── SESSION BREAKDOWN ─────────────────────────────────────────────────────
    print("  WIN RATE BY SESSION")
    print("  " + "-" * 52)
    for sess in ["LONDON_OPEN", "NY_OVERLAP", "NY_CLOSE"]:
        grp = df[df["session"] == sess]
        if grp.empty:
            print(f"    {sess:<15}  0 trades")
            continue
        gwr, gw, gl, gd = _decisive_wr(grp)
        gbe = (grp["result"] == "BREAKEVEN").sum()
        avg_p = grp["net_pnl_pts"].mean()
        print(f"    {sess:<15}  {len(grp):>4} trades  "
              f"{gwr:.0%} dWR ({gw}W/{gl}L/{gbe}BE)  avg_net={avg_p:+.2f}pts")
    print()

    # ── DIRECTION BREAKDOWN ───────────────────────────────────────────────────
    print("  WIN RATE BY DIRECTION")
    print("  " + "-" * 52)
    for direction, grp in df.groupby("direction"):
        gwr, gw, gl, gd = _decisive_wr(grp)
        gbe = (grp["result"] == "BREAKEVEN").sum()
        tp  = (grp["exit_reason"] == "TP_HIT").sum()
        sl  = (grp["exit_reason"] == "SL_HIT").sum()
        avg_p = grp["net_pnl_pts"].mean()
        print(f"    {direction:<6}  {len(grp):>4} trades  "
              f"{gwr:.0%} dWR ({gw}W/{gl}L/{gbe}BE)  "
              f"avg_net={avg_p:+.2f}pts  TP:{tp} SL:{sl}")
    print()

    # ── EXIT REASON BREAKDOWN ─────────────────────────────────────────────────
    print("  EXIT REASON BREAKDOWN")
    print("  " + "-" * 52)
    for reason, grp in df.groupby("exit_reason"):
        gwr, gw, gl, gd = _decisive_wr(grp)
        gbe = (grp["result"] == "BREAKEVEN").sum()
        avg_pts = grp["raw_pnl_pts"].mean()
        print(f"    {reason:<15}  {len(grp):>4} trades  "
              f"{gwr:.0%} dWR ({gw}W/{gl}L/{gbe}BE)  avg_raw={avg_pts:+.2f}pts")
    print()

    # ── BREAKEVEN ANALYSIS ────────────────────────────────────────────────────
    be_trig     = df["sl_to_be"].sum()
    be_then_sl  = df["be_then_sl"].sum()
    direct_sl   = ((df["exit_reason"] == "SL_HIT") & (~df["sl_to_be"])).sum()
    print("  BREAKEVEN PROTECTION")
    print("  " + "-" * 52)
    print(f"    BE triggered     : {be_trig} / {total} ({be_trig/total:.1%})")
    print(f"    BE → SL hit      : {be_then_sl} / {be_trig} ({be_then_sl/max(be_trig,1):.1%}) — these become BREAKEVEN exits")
    print(f"    Direct SL (no BE): {direct_sl}")
    be_cost = bes * (avg_spread + _SLIP_CALM)
    print(f"    Total BE cost    : ~${be_cost:.2f} (spread+slip on {bes} exits)")
    print()

    # ── REALIZED RR ANALYSIS ──────────────────────────────────────────────────
    sl_losses = df[(df["exit_reason"] == "SL_HIT") & (df["result"] == "LOSS")]
    print("  REALIZED RR — WHY IT'S < 2.0")
    print("  " + "-" * 52)
    print(f"    Target: avg WIN = +{avg_sl*RR_RATIO:.2f} pts, avg LOSS = -{avg_sl:.2f} pts")
    print(f"    Actual: avg WIN = +{avg_win_pts:.2f} pts, avg LOSS = -{avg_loss_pts:.2f} pts")
    print(f"    Gap on wins  : {avg_win_pts - avg_sl*RR_RATIO:+.2f} pts  "
          f"(slippage at TP costs ~{_SLIP_CALM:.2f}–{_SLIP_VOLATILE:.2f})")
    print(f"    Gap on losses: {avg_loss_pts - avg_sl:.2f} pts above SL  "
          f"(MAX_DURATION / TIME_KILL exits at market)")
    if not sl_losses.empty:
        tp_dist = sl_losses["sl_distance"] * RR_RATIO
        pct_half = (sl_losses["max_fav_pts"] >= sl_losses["sl_distance"]).mean()
        pct_full = (sl_losses["max_fav_pts"] >= tp_dist).mean()
        avg_mfe  = sl_losses["max_fav_pts"].mean()
        print(f"    On losing SL hits: avg max-fav = {avg_mfe:.2f} pts before reversing")
        print(f"      {pct_half:.0%} touched 1×SL distance (half-way to TP)")
        print(f"      {pct_full:.0%} reached TP distance before reversing — pure bad luck")
    print()

    # ── Z-SCORE BUCKETS (decisive WR) ─────────────────────────────────────────
    print("  DECISIVE WR BY |z-SCORE| AT ENTRY")
    print("  " + "-" * 52)
    df["abs_z"] = df["z"].abs()
    bins   = [0, 0.8, 1.0, 1.3, 1.6, 2.0, 99]
    labels = ["<0.8", "0.8-1.0", "1.0-1.3", "1.3-1.6", "1.6-2.0", ">2.0"]
    df["z_bucket"] = pd.cut(df["abs_z"], bins=bins, labels=labels)
    for bucket, grp in df.groupby("z_bucket", observed=True):
        gwr, gw, gl, gd = _decisive_wr(grp)
        avg_p = grp["net_pnl_pts"].mean()
        print(f"    |z| {str(bucket):<9}  {len(grp):>4} trades  "
              f"{gwr:.0%} dWR ({gw}W/{gl}L)  avg_net={avg_p:+.2f}pts")
    print()

    # ── HALF-LIFE BUCKETS ─────────────────────────────────────────────────────
    print("  DECISIVE WR BY HALF-LIFE (bars)")
    print("  " + "-" * 52)
    hl_bins   = [0, 5, 10, 20, 35, 50]
    hl_labels = ["≤5", "5-10", "10-20", "20-35", "35-50"]
    df["hl_bucket"] = pd.cut(df["half_life"], bins=hl_bins, labels=hl_labels)
    for bucket, grp in df.groupby("hl_bucket", observed=True):
        gwr, gw, gl, gd = _decisive_wr(grp)
        tk_pct = (grp["exit_reason"] == "TIME_KILL").mean()
        avg_p  = grp["net_pnl_pts"].mean()
        print(f"    HL {str(bucket):<8}  {len(grp):>4} trades  "
              f"{gwr:.0%} dWR ({gw}W/{gl}L)  avg_net={avg_p:+.2f}pts  time_kill={tk_pct:.0%}")
    print()

    # ── SL DISTANCE BUCKETS ───────────────────────────────────────────────────
    print("  DECISIVE WR BY SL DISTANCE ($)")
    print("  " + "-" * 52)
    sl_bins   = [0, 6, 7, 8, 10, 12]
    sl_labels = ["$6", "$6-7", "$7-8", "$8-10", "$10-12"]
    df["sl_bucket"] = pd.cut(df["sl_distance"], bins=sl_bins, labels=sl_labels)
    for bucket, grp in df.groupby("sl_bucket", observed=True):
        gwr, gw, gl, gd = _decisive_wr(grp)
        avg_p = grp["net_pnl_pts"].mean()
        print(f"    SL {str(bucket):<8}  {len(grp):>4} trades  "
              f"{gwr:.0%} dWR ({gw}W/{gl}L)  avg_net={avg_p:+.2f}pts")
    print()

    # ── DETREND TYPE ──────────────────────────────────────────────────────────
    print("  DECISIVE WR BY DETREND TYPE")
    print("  " + "-" * 52)
    for dt_type, grp in df.groupby("detrend"):
        gwr, gw, gl, gd = _decisive_wr(grp)
        tp = (grp["exit_reason"] == "TP_HIT").sum()
        sl = (grp["exit_reason"] == "SL_HIT").sum()
        avg_p = grp["net_pnl_pts"].mean()
        print(f"    {dt_type:<8}  {len(grp):>4} trades  "
              f"{gwr:.0%} dWR ({gw}W/{gl}L)  avg_net={avg_p:+.2f}pts  TP:{tp} SL:{sl}")
    print()

    # ── HOUR-OF-DAY BREAKDOWN ─────────────────────────────────────────────────
    print("  DECISIVE WR BY HOUR (UTC)  [dead zone = 14:00-15:00 excluded]")
    print("  " + "-" * 52)
    df["hour"] = df["open_dt"].dt.hour
    for hour, grp in df.groupby("hour"):
        gwr, gw, gl, gd = _decisive_wr(grp)
        avg_p = grp["net_pnl_pts"].mean()
        tag = " ← dead zone (blocked)" if NY_OVERLAP_DEAD_HOUR_START <= hour < NY_OVERLAP_DEAD_HOUR_END else ""
        print(f"    {hour:02d}:xx UTC  {len(grp):>4} trades  "
              f"{gwr:.0%} dWR ({gw}W/{gl}L)  avg_net={avg_p:+.2f}pts{tag}")
    print()

    # ── SIGNAL REJECTION BREAKDOWN PER SESSION ────────────────────────────────
    print("  SIGNAL REJECTION BREAKDOWN (per session)")
    print("  " + "-" * 52)
    for sess in ["LONDON_OPEN", "NY_OVERLAP", "NY_CLOSE"]:
        rej = rejections.get(sess, {})
        fired = len(df[df["session"] == sess])
        total_opp = sum(rej.values()) + fired
        print(f"    [{sess}]  {total_opp} opportunities → {fired} signals fired "
              f"({fired/max(total_opp,1):.1%} fire rate)")
        for reason, cnt in sorted(rej.items(), key=lambda x: -x[1]):
            print(f"      {reason:<28} {cnt:>5}  ({cnt/max(total_opp,1):.1%})")
    print()

    # ── MAX SIMULTANEOUS TRADES SENSITIVITY ───────────────────────────────────
    print("  MAX SIMULTANEOUS TRADES SENSITIVITY")
    print("  " + "-" * 52)
    print(f"    {'Cap':<5} {'Trades':>7} {'Total P&L':>12} {'dWR':>7} {'Sharpe':>8} {'Max DD':>8}")
    print("    " + "-" * 50)
    for cap in [1, 2, 3, 4, 5]:
        tot_pnl, cap_dwr, cap_sh, cap_dd, cap_n = _run_with_cap(df, cap, a.balance)
        tag = " ← current" if cap == 2 else ""
        print(f"    {cap:<5} {cap_n:>7} {tot_pnl:>+12.2f} {cap_dwr:>7.1%} "
              f"{cap_sh:>8.2f} {cap_dd:>7.1f}%{tag}")
    print()

    # ── CONSECUTIVE LOSS STREAKS ───────────────────────────────────────────────
    print("  CONSECUTIVE LOSS STREAKS")
    print("  " + "-" * 52)
    results = [1 if r == "WIN" else (0 if r == "LOSS" else -1) for r in df["result"]]
    streaks, cur = [], 0
    for r in results:
        if r == 0:
            cur += 1
        else:
            if cur > 0:
                streaks.append(cur)
            cur = 0
    if cur > 0:
        streaks.append(cur)
    if streaks:
        print(f"    Max consecutive losses : {max(streaks)}")
        print(f"    Avg streak length      : {np.mean(streaks):.1f}")
        print(f"    Streaks ≥3             : {sum(1 for s in streaks if s >= 3)}")
        print(f"    Streaks ≥5             : {sum(1 for s in streaks if s >= 5)}")
    else:
        print("    No loss streaks found.")
    print()

    # ── KEY FINDINGS ──────────────────────────────────────────────────────────
    print("  KEY FINDINGS")
    print("  " + "-" * 52)

    # Best z-bucket
    z_stats = []
    for b in df["z_bucket"].cat.categories:
        sub = df[df["z_bucket"] == b]
        if len(sub) >= 5:
            gwr, gw, gl, gd = _decisive_wr(sub)
            z_stats.append((b, gwr, len(sub)))
    if z_stats:
        best_z = max(z_stats, key=lambda x: x[1])
        print(f"    Best |z| bucket  : {best_z[0]}  ({best_z[1]:.0%} dWR, {best_z[2]} trades)")

    # Best SL bucket
    sl_stats = []
    for b in df["sl_bucket"].cat.categories:
        sub = df[df["sl_bucket"] == b]
        if len(sub) >= 5:
            gwr, gw, gl, gd = _decisive_wr(sub)
            sl_stats.append((b, gwr, len(sub)))
    if sl_stats:
        best_sl = max(sl_stats, key=lambda x: x[1])
        print(f"    Best SL range    : {best_sl[0]}  ({best_sl[1]:.0%} dWR, {best_sl[2]} trades)")

    # Fastest SL hits
    sl_hits_all = df[df["exit_reason"] == "SL_HIT"]
    if not sl_hits_all.empty:
        fast = (sl_hits_all["duration_min"] < 30).mean()
        print(f"    Fast SL hits (<30m): {fast:.0%} of SL exits — "
              f"{'SL may be too tight' if fast > 0.5 else 'normal pace'}")

    # Sessions worth enabling
    for sess in ["LONDON_OPEN", "NY_CLOSE"]:
        sub = df[df["session"] == sess]
        if not sub.empty:
            gwr, gw, gl, gd = _decisive_wr(sub)
            print(f"    {sess:<15}: {gwr:.0%} dWR over {len(sub)} trades — "
                  f"{'looks viable' if gwr >= 0.55 else 'caution: <55%'}")

    print("=" * W)


if __name__ == "__main__":
    main()
