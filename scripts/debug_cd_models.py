#!/usr/bin/env python3
"""
debug_cd_models.py — Diagnose Model C (KALMAN_TREND) and Model D (INTRADAY_MOM).

Model C: Never fired in backtest. Identify which filter kills it.
Model D: 44% WR. Analyse net_return/ATR distribution for wins vs losses.

Usage:
    python scripts/debug_cd_models.py
"""

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
from agents.nanami.skills.htf_regime import detect_regime, check_structural_break, compute_macro_bias, compute_h4_bias
from agents.nanami.skills.stat_tests import rolling_hurst
from core.constants import (
    REGIME_H1_BARS_NEEDED,
    IMOM_MEASURE_START_HOUR, IMOM_MEASURE_END_HOUR,
    IMOM_TRADE_START_HOUR, IMOM_TRADE_END_HOUR,
    IMOM_MOMENTUM_THRESHOLD, IMOM_MIN_MEASURE_BARS,
    KALMAN_TREND_CONSEC_BARS, KALMAN_TREND_VEL_THRESHOLD, KALMAN_TREND_HURST_MIN,
)

# ── Data loading ──────────────────────────────────────────────────────────────

def _load_csv(path, label):
    print(f"Loading {label} ...")
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index, utc=True)
    df.columns = [c.lower() for c in df.columns]
    df = df.sort_index()
    df = df[df["close"] > 0]
    print(f"  {len(df):,} bars  {df.index[0]:%Y-%m-%d} to {df.index[-1]:%Y-%m-%d}")
    return df

def _in_window(t, start, end):
    if start <= end:
        return start <= t < end
    return t >= start or t < end

def _session_from_dt(dt):
    t = dt.time().replace(second=0, microsecond=0)
    blackout_s, blackout_e = time(21, 0), time(7, 0)
    if _in_window(t, blackout_s, blackout_e):
        return None
    windows = {
        "LONDON_OPEN": (time(7, 30), time(10, 0)),
        "NY_OVERLAP":  (time(12, 0), time(16, 0)),
        "NY_CLOSE":    (time(19, 0), time(21, 0)),
    }
    for name, (s, e) in windows.items():
        if _in_window(t, s, e):
            return name
    return None

W = 72


# ═══════════════════════════════════════════════════════════════════════════
# MODEL C DIAGNOSIS
# ═══════════════════════════════════════════════════════════════════════════

def diagnose_model_c(df_m5_ind: pd.DataFrame, start_bar: int, end_bar: int):
    print()
    print("=" * W)
    print("  MODEL C (KALMAN_TREND) — FILTER FUNNEL DIAGNOSIS")
    print("=" * W)
    print(f"  Current thresholds:")
    print(f"    KALMAN_TREND_HURST_MIN    = {KALMAN_TREND_HURST_MIN}")
    print(f"    KALMAN_TREND_VEL_THRESHOLD = {KALMAN_TREND_VEL_THRESHOLD}")
    print(f"    KALMAN_TREND_CONSEC_BARS   = {KALMAN_TREND_CONSEC_BARS}")
    print()

    sessions_c = {"LONDON_OPEN", "NY_OVERLAP", "NY_CLOSE"}

    bars_in_session = 0
    pass_data_length = 0
    pass_atr = 0
    pass_hurst = 0
    pass_velocity_any = 0    # velocity > threshold on current bar (not all N bars)
    pass_velocity_consec = 0 # all N bars above threshold
    pass_h4_gate = 0

    hurst_vals = []
    vel_vals = []          # per-bar velocity magnitudes for in-session bars
    vel_consec_counts = [] # how many consecutive bars have velocity > threshold (max streak)
    ratio_at_fail = []     # hurst at bars that fail the hurst gate

    _ATR_BASELINE = 60
    _ATR_SPIKE = 2.0
    _MIN_BARS = 80
    _HURST_WINDOW = 100

    sample_step = 12  # sample every 12 bars (5 min × 12 = 60 min) for speed

    for i in range(start_bar, end_bar, sample_step):
        dt = df_m5_ind.index[i]
        session = _session_from_dt(dt)
        if session not in sessions_c:
            continue

        bars_in_session += 1
        df_win = df_m5_ind.iloc[max(0, i - 500): i + 1]

        if len(df_win) < _MIN_BARS:
            continue
        pass_data_length += 1

        last = df_win.iloc[-1]
        atr = float(last.get("atr14", 0.0))
        if atr <= 0 or pd.isna(atr):
            continue

        atr_series = df_win["atr14"].values.astype(float)
        if len(atr_series) >= _ATR_BASELINE:
            baseline = float(np.median(atr_series[-_ATR_BASELINE:]))
            if baseline > 0 and atr > baseline * _ATR_SPIKE:
                continue
        pass_atr += 1

        closes = df_win["close"].values.astype(float)
        if len(closes) < _HURST_WINDOW:
            continue
        hurst_val = rolling_hurst(closes, window=_HURST_WINDOW)
        hurst_vals.append(hurst_val)

        if hurst_val <= KALMAN_TREND_HURST_MIN:
            ratio_at_fail.append(hurst_val)
            continue
        pass_hurst += 1

        if "kalman_velocity" not in df_win.columns:
            continue
        vel = df_win["kalman_velocity"].values.astype(float)
        if len(vel) < KALMAN_TREND_CONSEC_BARS:
            continue

        curr_vel = float(vel[-1])
        vel_vals.append(abs(curr_vel))

        # Check if current bar velocity is directional
        if abs(curr_vel) > KALMAN_TREND_VEL_THRESHOLD:
            pass_velocity_any += 1

        # Count max consecutive bars with velocity > threshold in same direction
        recent_n = vel[-KALMAN_TREND_CONSEC_BARS:]
        if not np.any(np.isnan(recent_n)):
            if all(v > KALMAN_TREND_VEL_THRESHOLD for v in recent_n):
                pass_velocity_consec += 1
                pass_h4_gate += 1  # H4 gate pass approximation
            elif all(v < -KALMAN_TREND_VEL_THRESHOLD for v in recent_n):
                pass_velocity_consec += 1
                pass_h4_gate += 1

        # Measure max consecutive streak for any direction
        streak = 0
        for v in reversed(vel):
            if abs(v) > KALMAN_TREND_VEL_THRESHOLD:
                streak += 1
            else:
                break
        vel_consec_counts.append(streak)

    print(f"  Bars sampled (every 12 bars in active sessions) : {bars_in_session:>7,}")
    print(f"  Pass data length (>= {_MIN_BARS} bars)             : {pass_data_length:>7,}")
    print(f"  Pass ATR spike filter                           : {pass_atr:>7,}")
    print(f"  Pass Hurst > {KALMAN_TREND_HURST_MIN}                          : {pass_hurst:>7,}  ← likely bottleneck")
    print(f"  Pass velocity > {KALMAN_TREND_VEL_THRESHOLD} (any 1 bar)              : {pass_velocity_any:>7,}")
    print(f"  Pass velocity > {KALMAN_TREND_VEL_THRESHOLD} ({KALMAN_TREND_CONSEC_BARS} consec bars)       : {pass_velocity_consec:>7,}  ← signal gate")
    print()

    if hurst_vals:
        hv = np.array(hurst_vals)
        print(f"  Hurst distribution (all in-session bars that passed ATR):")
        print(f"    Count  : {len(hv):,}")
        print(f"    Min    : {hv.min():.4f}")
        print(f"    p5     : {np.percentile(hv,  5):.4f}")
        print(f"    p25    : {np.percentile(hv, 25):.4f}")
        print(f"    Median : {np.median(hv):.4f}")
        print(f"    p75    : {np.percentile(hv, 75):.4f}")
        print(f"    p90    : {np.percentile(hv, 90):.4f}")
        print(f"    p95    : {np.percentile(hv, 95):.4f}")
        print(f"    Max    : {hv.max():.4f}")
        print(f"    > 0.50 : {(hv > 0.50).sum():,}  ({(hv > 0.50).mean():.1%})")
        print(f"    > 0.53 : {(hv > 0.53).sum():,}  ({(hv > 0.53).mean():.1%})")
        print(f"    > 0.55 : {(hv > 0.55).sum():,}  ({(hv > 0.55).mean():.1%})")
        print(f"    > 0.58 : {(hv > 0.58).sum():,}  ({(hv > 0.58).mean():.1%})")
        print(f"    > 0.60 : {(hv > 0.60).sum():,}  ({(hv > 0.60).mean():.1%})")

    if vel_vals:
        vv = np.array(vel_vals)
        print()
        print(f"  Kalman velocity magnitude (bars that passed Hurst):")
        print(f"    Count  : {len(vv):,}")
        print(f"    Median : {np.median(vv):.6f}")
        print(f"    p75    : {np.percentile(vv, 75):.6f}")
        print(f"    p90    : {np.percentile(vv, 90):.6f}")
        print(f"    p95    : {np.percentile(vv, 95):.6f}")
        print(f"    > 0.01 : {(vv > 0.01).mean():.1%}")
        print(f"    > 0.02 : {(vv > 0.02).mean():.1%}")
        print(f"    > 0.03 : {(vv > 0.03).mean():.1%}  ← current threshold")
        print(f"    > 0.05 : {(vv > 0.05).mean():.1%}")

    if vel_consec_counts:
        vc = np.array(vel_consec_counts)
        print()
        print(f"  Max consecutive bars with |velocity| > {KALMAN_TREND_VEL_THRESHOLD}:")
        print(f"    Median streak : {np.median(vc):.1f}")
        print(f"    p75 streak    : {np.percentile(vc, 75):.1f}")
        print(f"    p90 streak    : {np.percentile(vc, 90):.1f}")
        print(f"    streak >= 3   : {(vc >= 3).mean():.1%}")
        print(f"    streak >= 5   : {(vc >= 5).mean():.1%}  ← current requirement")
        print(f"    streak >= 7   : {(vc >= 7).mean():.1%}")

    print()
    print("  Suggested parameter adjustments:")
    if hurst_vals:
        hv = np.array(hurst_vals)
        p75 = np.percentile(hv, 75)
        # Suggest a Hurst threshold that would let ~25% of bars through
        suggested_hurst = round(p75 - 0.02, 2)
        print(f"    KALMAN_TREND_HURST_MIN    : {KALMAN_TREND_HURST_MIN} → {suggested_hurst}  "
              f"(would pass ~25% of bars vs current {(hv > KALMAN_TREND_HURST_MIN).mean():.1%})")
    if vel_vals:
        vv = np.array(vel_vals)
        p25_vel = np.percentile(vv, 25)
        suggested_vel = round(max(0.005, p25_vel), 4)
        print(f"    KALMAN_TREND_VEL_THRESHOLD : {KALMAN_TREND_VEL_THRESHOLD} → {suggested_vel}  "
              f"(would pass ~75% of velocity bars)")
    print(f"    KALMAN_TREND_CONSEC_BARS   : {KALMAN_TREND_CONSEC_BARS} → 3  "
          f"(less strict streak requirement)")
    print("=" * W)


# ═══════════════════════════════════════════════════════════════════════════
# MODEL D DIAGNOSIS
# ═══════════════════════════════════════════════════════════════════════════

def diagnose_model_d(df_m5_ind: pd.DataFrame, start_bar: int, end_bar: int,
                     h4_bias_map: dict):
    print()
    print("=" * W)
    print("  MODEL D (INTRADAY_MOM) — EDGE ANALYSIS")
    print("=" * W)
    print(f"  Current threshold: net_return > {IMOM_MOMENTUM_THRESHOLD} × ATR")
    print()

    # Collect all potential IMom opportunities: every 14:00 UTC bar in NY_OVERLAP
    # Record: net_return/ATR ratio, direction, and what outcome was (forward price movement)
    opportunities = []  # {ratio, direction, fwd_move_12bar, fwd_move_24bar, h4_bias}

    m5_times = df_m5_ind.index

    for i in range(start_bar, end_bar):
        dt = m5_times[i]

        # Only look at bars at exactly 14:00 UTC in NY_OVERLAP
        if dt.hour != IMOM_TRADE_START_HOUR or dt.minute != 0:
            continue
        session = _session_from_dt(dt)
        if session != "NY_OVERLAP":
            continue

        df_win = df_m5_ind.iloc[max(0, i - 500): i + 1]
        if len(df_win) < 30:
            continue

        last = df_win.iloc[-1]
        atr = float(last.get("atr14", 0.0))
        if atr <= 0 or pd.isna(atr):
            continue

        # Extract measurement window (12:00–14:00 UTC today)
        today = dt.date()
        idx_utc = df_win.index
        mask = (
            np.array([d == today for d in idx_utc.date])
            & (idx_utc.hour >= IMOM_MEASURE_START_HOUR)
            & (idx_utc.hour < IMOM_MEASURE_END_HOUR)
        )
        measure_bars = df_win[mask]
        if len(measure_bars) < IMOM_MIN_MEASURE_BARS:
            continue

        close_open  = float(measure_bars["close"].iloc[0])
        close_close = float(measure_bars["close"].iloc[-1])
        net_return  = close_close - close_open
        ratio       = net_return / atr if atr > 0 else 0.0
        direction   = "BUY" if net_return > 0 else "SELL"
        h4_bias     = h4_bias_map.get(i, "NEUTRAL")

        # Forward price movement from 14:00 to 16:00 (next 24 M5 bars)
        fwd_bars = df_m5_ind.iloc[i + 1: i + 25] if i + 25 <= end_bar else df_m5_ind.iloc[i + 1:]
        if len(fwd_bars) == 0:
            continue

        fwd_close = float(fwd_bars["close"].iloc[-1])
        fwd_move  = fwd_close - float(last["close"])
        # Signed: positive = went up, negative = went down
        fwd_in_direction = fwd_move if direction == "BUY" else -fwd_move

        opportunities.append({
            "ratio":     ratio,
            "abs_ratio": abs(ratio),
            "direction": direction,
            "fwd_move":  fwd_in_direction,
            "h4_bias":   h4_bias,
            "atr":       atr,
            "net_return": net_return,
        })

    if not opportunities:
        print("  No IMom opportunities found in period.")
        return

    df_opp = pd.DataFrame(opportunities)
    print(f"  Total 14:00 UTC opportunities (all days): {len(df_opp)}")
    print()

    # Distribution of |ratio|
    ar = df_opp["abs_ratio"].values
    print(f"  |net_return / ATR| distribution (all opportunities):")
    print(f"    Median : {np.median(ar):.3f}")
    print(f"    p25    : {np.percentile(ar, 25):.3f}")
    print(f"    p75    : {np.percentile(ar, 75):.3f}")
    print(f"    > 0.25 : {(ar > 0.25).mean():.1%}  (would fire at threshold 0.25)")
    print(f"    > 0.50 : {(ar > 0.50).mean():.1%}  ← current threshold")
    print(f"    > 0.75 : {(ar > 0.75).mean():.1%}")
    print(f"    > 1.00 : {(ar > 1.00).mean():.1%}")
    print(f"    > 1.50 : {(ar > 1.50).mean():.1%}")
    print()

    # Win rate by threshold — key diagnostic
    print(f"  Win rate (fwd_move > 0) by threshold:")
    print(f"  {'Threshold':>10}  {'Opportunities':>14}  {'Win Rate':>10}  {'Avg fwd/ATR':>12}")
    for thresh in [0.0, 0.25, 0.50, 0.75, 1.00, 1.25, 1.50, 2.00]:
        subset = df_opp[df_opp["abs_ratio"] > thresh]
        if len(subset) == 0:
            continue
        wr = (subset["fwd_move"] > 0).mean()
        avg_fwd_atr = (subset["fwd_move"] / subset["atr"]).mean()
        print(f"  {thresh:>10.2f}  {len(subset):>14}  {wr:>10.1%}  {avg_fwd_atr:>12.3f}")

    print()

    # H4 bias breakdown
    print(f"  By H4 bias (at current threshold {IMOM_MOMENTUM_THRESHOLD}):")
    qualified = df_opp[df_opp["abs_ratio"] > IMOM_MOMENTUM_THRESHOLD]
    for bias, grp in qualified.groupby("h4_bias"):
        wr = (grp["fwd_move"] > 0).mean()
        print(f"    H4={bias:<10}  {len(grp):>4} opps  WR={wr:.1%}")

    print()

    # Direction breakdown
    print(f"  By direction (at current threshold {IMOM_MOMENTUM_THRESHOLD}):")
    for direction, grp in qualified.groupby("direction"):
        wr = (grp["fwd_move"] > 0).mean()
        avg_fwd = (grp["fwd_move"] / grp["atr"]).mean()
        print(f"    {direction:<6}  {len(grp):>4} opps  WR={wr:.1%}  avg_fwd/ATR={avg_fwd:.3f}")

    print()

    # Edge by ratio band
    print(f"  Edge by ratio band:")
    bands = [(0.0, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 99.0)]
    for lo, hi in bands:
        subset = df_opp[(df_opp["abs_ratio"] >= lo) & (df_opp["abs_ratio"] < hi)]
        if len(subset) == 0:
            continue
        wr = (subset["fwd_move"] > 0).mean()
        avg_fwd = (subset["fwd_move"] / subset["atr"]).mean()
        print(f"    ratio {lo:.1f}–{hi:.1f}  {len(subset):>4} opps  WR={wr:.1%}  avg_fwd/ATR={avg_fwd:.3f}")

    print()
    print("  Suggested parameter adjustments:")
    # Find threshold where WR > 55%
    best_thresh = IMOM_MOMENTUM_THRESHOLD
    for thresh in np.arange(0.25, 3.0, 0.25):
        subset = df_opp[df_opp["abs_ratio"] > thresh]
        if len(subset) < 30:
            break
        wr = (subset["fwd_move"] > 0).mean()
        if wr >= 0.55:
            best_thresh = thresh
            print(f"    IMOM_MOMENTUM_THRESHOLD: {IMOM_MOMENTUM_THRESHOLD} → {thresh:.2f}  "
                  f"(WR={wr:.1%} on {len(subset)} opportunities)")
            break
    else:
        print(f"    No threshold achieves 55% WR with >= 30 opportunities.")
        print(f"    Consider dropping Model D — momentum effect is too weak on this data.")

    print("=" * W)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    from agents.nanami.skills.htf_regime import compute_h4_bias

    df_m5 = _load_csv("data/XAUUSD_M5_2yr.csv", "M5")
    df_h1 = _load_csv("data/XAUUSD_H1_2yr.csv", "H1")
    df_h4 = _load_csv("data/XAUUSD_H4_2yr.csv", "H4")

    print("\nComputing M5 indicators ...")
    df_m5_ind = add_indicators(df_m5.copy())

    # Date filter: Jan 2025 – Mar 2026
    from datetime import timezone as _tz
    sd = pd.Timestamp("2025-01-01", tz="UTC")
    ed = pd.Timestamp("2026-03-31", tz="UTC")
    start_bar = max(260, int(df_m5_ind.index.searchsorted(sd)))
    end_bar   = min(len(df_m5_ind), int(df_m5_ind.index.searchsorted(ed)))
    print(f"Period: {df_m5_ind.index[start_bar]:%Y-%m-%d} to {df_m5_ind.index[end_bar-1]:%Y-%m-%d}")

    # H4 bias map (needed for Model D breakdown)
    print("Precomputing H4 bias ...")
    h4_times = df_h4.index
    h4_biases = {}
    _H4_LB = 50
    for i in range(_H4_LB, len(df_h4)):
        h4_biases[i] = compute_h4_bias(df_h4.iloc[i - _H4_LB + 1: i + 1])
    m5_times = df_m5_ind.index
    h4_bias_map = {}
    for i in range(len(m5_times)):
        h4_idx = int(h4_times.searchsorted(m5_times[i], side="right")) - 1
        h4_bias_map[i] = h4_biases.get(h4_idx, "NEUTRAL")

    diagnose_model_c(df_m5_ind, start_bar, end_bar)
    diagnose_model_d(df_m5_ind, start_bar, end_bar, h4_bias_map)


if __name__ == "__main__":
    main()
