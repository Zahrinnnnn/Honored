#!/usr/bin/env python3
"""
diagnose_signals.py — Deep diagnostic for Model A and B signal generation.

Measures:
1. Regime distribution across all M5 bars
2. How many bars are in GRIND vs TIGHT_RANGE vs NO_TRADE regimes
3. For eligible bars, which OU gate blocks the signal
4. ADF pass rate on real M5 data
5. OU fit success rate
6. Half-life distribution
7. Z-score distribution
"""

import os
import sys
from collections import defaultdict
from datetime import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv()

from agents.nanami.skills.indicator_engine import add_indicators
from agents.nanami.skills.htf_regime import detect_regime, check_structural_break
from agents.nanami.skills.stat_tests import adf_stationary, fit_ou, ou_zscore
from core.constants import (
    REGIME_BULLISH_GRIND, REGIME_BEARISH_GRIND, REGIME_TIGHT_RANGE,
    REGIME_H1_BARS_NEEDED,
    OU_ZSCORE_ENTRY_THRESHOLD, OU_MIN_HALF_LIFE, OU_MAX_HALF_LIFE,
)

# ─── Data loading ────────────────────────────────────────────────────────────

def _load_csv(path, label):
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index, utc=True)
    df.columns = [c.lower() for c in df.columns]
    df = df.sort_index()
    df = df[df["close"] > 0]
    print(f"  {label}: {len(df):,} bars | {df.index[0]:%Y-%m-%d} to {df.index[-1]:%Y-%m-%d}")
    return df

# ─── Session check ───────────────────────────────────────────────────────────

SESSIONS = {
    "LONDON_OPEN":     (time(7, 0), time(10, 0)),
    "NY_OVERLAP":      (time(12, 0), time(16, 0)),
    "NY_CLOSE":        (time(19, 0), time(21, 0)),
    "LONDON_BREAKOUT": (time(7, 0), time(7, 30)),
}
MODEL_A_SESSIONS = {"LONDON_OPEN", "NY_OVERLAP"}
MODEL_B_SESSIONS = {"LONDON_OPEN", "NY_OVERLAP", "NY_CLOSE"}

def _get_session(dt):
    t = dt.time()
    for name, (start, end) in SESSIONS.items():
        if start <= t < end:
            return name
    return None

# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    data_dir = os.path.join(os.path.dirname(__file__), "..", "data")
    print("Loading data...")
    df_m5 = _load_csv(os.path.join(data_dir, "XAUUSD_M5.csv"), "M5")
    df_h1 = _load_csv(os.path.join(data_dir, "XAUUSD_H1.csv"), "H1")

    print("\nComputing M5 indicators...")
    df_m5 = add_indicators(df_m5)

    # ── 1. Regime distribution ───────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  1. REGIME DISTRIBUTION (sampled every 12 M5 bars = 1 hour)")
    print("=" * 70)

    h1_times = df_h1.index.to_list()
    regime_counts = defaultdict(int)
    regime_by_session = defaultdict(lambda: defaultdict(int))
    h1_ptr = 0
    sample_count = 0

    for i in range(200, len(df_m5), 12):  # sample every hour
        m5_t = df_m5.index[i]
        while h1_ptr < len(h1_times) - 1 and h1_times[h1_ptr + 1] <= m5_t:
            h1_ptr += 1
        h1_start = max(0, h1_ptr - REGIME_H1_BARS_NEEDED + 1)
        h1_window = df_h1.iloc[h1_start:h1_ptr + 1]
        regime = detect_regime(h1_window)
        regime_counts[regime] += 1
        session = _get_session(m5_t)
        if session:
            regime_by_session[session][regime] += 1
        sample_count += 1

    print(f"\n  Total samples: {sample_count}")
    for r in sorted(regime_counts.keys()):
        pct = regime_counts[r] / sample_count * 100
        print(f"    {r:<25} {regime_counts[r]:>5}  ({pct:>5.1f}%)")

    print("\n  By session:")
    for sess in ["LONDON_OPEN", "NY_OVERLAP", "NY_CLOSE"]:
        total = sum(regime_by_session[sess].values())
        if total == 0:
            continue
        print(f"\n    {sess} ({total} samples):")
        for r in sorted(regime_by_session[sess].keys()):
            pct = regime_by_session[sess][r] / total * 100
            print(f"      {r:<25} {regime_by_session[sess][r]:>5}  ({pct:>5.1f}%)")

    # ── 2. OU Gate Analysis (on eligible bars) ──────────────────────────────
    print("\n" + "=" * 70)
    print("  2. OU GATE ANALYSIS (every eligible bar in active sessions)")
    print("=" * 70)

    gate_stats = {
        "total_eligible_bars": 0,
        "regime_grind": 0,
        "regime_tight": 0,
        "adf_tested": 0,
        "adf_passed": 0,
        "ou_fit_tested": 0,
        "ou_fit_success": 0,
        "half_life_ok": 0,
        "zscore_ok": 0,
        "direction_ok": 0,
        "signal_would_fire": 0,
    }
    half_lives = []
    zscores = []
    adf_pvalues = []

    h1_ptr = 0
    # Sample every 6 bars (30 min) to keep it fast
    for i in range(200, len(df_m5), 6):
        m5_t = df_m5.index[i]
        session = _get_session(m5_t)
        if session not in MODEL_A_SESSIONS and session not in MODEL_B_SESSIONS:
            continue
        # Skip breakout window for Model A
        lb_start, lb_end = time(7, 0), time(7, 30)
        if lb_start <= m5_t.time() < lb_end:
            continue

        # Get regime
        while h1_ptr < len(h1_times) - 1 and h1_times[h1_ptr + 1] <= m5_t:
            h1_ptr += 1
        h1_start = max(0, h1_ptr - REGIME_H1_BARS_NEEDED + 1)
        h1_window = df_h1.iloc[h1_start:h1_ptr + 1]
        regime = detect_regime(h1_window)

        gate_stats["total_eligible_bars"] += 1

        is_grind = regime in (REGIME_BULLISH_GRIND, REGIME_BEARISH_GRIND)
        is_tight = regime == REGIME_TIGHT_RANGE

        if is_grind:
            gate_stats["regime_grind"] += 1
        if is_tight:
            gate_stats["regime_tight"] += 1

        if not (is_grind or is_tight):
            continue  # No trade regime

        # Get M5 window
        win_start = max(0, i - 499)
        df_win = df_m5.iloc[win_start:i + 1]
        if len(df_win) < 200:
            continue

        closes = df_win["close"].values
        atr14 = df_win["atr14"].iloc[-1]
        if atr14 <= 0:
            continue

        # Gate 1: ADF
        gate_stats["adf_tested"] += 1
        adf_result = adf_stationary(closes)
        adf_pvalues.append(adf_result["p_value"])
        if adf_result["stationary"]:
            gate_stats["adf_passed"] += 1
        else:
            continue

        # Gate 2: OU fit
        gate_stats["ou_fit_tested"] += 1
        ou = fit_ou(closes)
        if ou is None:
            continue
        gate_stats["ou_fit_success"] += 1

        # Gate 3: Half-life
        hl = ou["half_life_bars"]
        half_lives.append(hl)
        if OU_MIN_HALF_LIFE <= hl <= OU_MAX_HALF_LIFE:
            gate_stats["half_life_ok"] += 1
        else:
            continue

        # Gate 4: Z-score
        z = ou_zscore(closes[-1], ou)
        zscores.append(z)
        if abs(z) > OU_ZSCORE_ENTRY_THRESHOLD:
            gate_stats["zscore_ok"] += 1
        else:
            continue

        # Gate 5: Direction matches regime
        if is_grind:
            if regime == REGIME_BULLISH_GRIND and z < -OU_ZSCORE_ENTRY_THRESHOLD:
                gate_stats["direction_ok"] += 1
                gate_stats["signal_would_fire"] += 1
            elif regime == REGIME_BEARISH_GRIND and z > OU_ZSCORE_ENTRY_THRESHOLD:
                gate_stats["direction_ok"] += 1
                gate_stats["signal_would_fire"] += 1
            # else: z and regime don't match
        elif is_tight:
            gate_stats["direction_ok"] += 1
            gate_stats["signal_would_fire"] += 1

    print(f"\n  Total eligible session bars (sampled): {gate_stats['total_eligible_bars']}")
    print(f"  In GRIND regimes:                      {gate_stats['regime_grind']}")
    print(f"  In TIGHT_RANGE:                        {gate_stats['regime_tight']}")
    print("\n  -- Gate funnel --")
    print(f"  ADF tested:       {gate_stats['adf_tested']}")
    print(f"  ADF passed:       {gate_stats['adf_passed']}  ({gate_stats['adf_passed']/max(gate_stats['adf_tested'],1)*100:.1f}%)")
    print(f"  OU fit success:   {gate_stats['ou_fit_success']}  ({gate_stats['ou_fit_success']/max(gate_stats['ou_fit_tested'],1)*100:.1f}%)")
    print(f"  Half-life OK:     {gate_stats['half_life_ok']}  ({gate_stats['half_life_ok']/max(gate_stats['ou_fit_success'],1)*100:.1f}%)")
    print(f"  Z-score > {OU_ZSCORE_ENTRY_THRESHOLD}:     {gate_stats['zscore_ok']}  ({gate_stats['zscore_ok']/max(gate_stats['half_life_ok'],1)*100:.1f}%)")
    print(f"  Direction match:  {gate_stats['direction_ok']}")
    print(f"  SIGNAL FIRES:     {gate_stats['signal_would_fire']}")

    # ── 3. Distributions ────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  3. DISTRIBUTIONS")
    print("=" * 70)

    if adf_pvalues:
        arr = np.array(adf_pvalues)
        print(f"\n  ADF p-values ({len(arr)} tests):")
        print(f"    mean={arr.mean():.4f}  median={np.median(arr):.4f}  "
              f"min={arr.min():.4f}  max={arr.max():.4f}")
        for thresh in [0.01, 0.05, 0.10, 0.20, 0.50]:
            pct = (arr < thresh).sum() / len(arr) * 100
            print(f"    p < {thresh}: {(arr < thresh).sum()}/{len(arr)} ({pct:.1f}%)")

    if half_lives:
        arr = np.array(half_lives)
        print(f"\n  Half-life bars ({len(arr)} fits):")
        print(f"    mean={arr.mean():.1f}  median={np.median(arr):.1f}  "
              f"min={arr.min():.1f}  max={arr.max():.1f}")
        for lo, hi in [(1, 5), (5, 10), (10, 20), (20, 30), (30, 50), (50, 100)]:
            count = ((arr >= lo) & (arr < hi)).sum()
            print(f"    [{lo}-{hi}): {count}/{len(arr)} ({count/len(arr)*100:.1f}%)")

    if zscores:
        arr = np.array(zscores)
        print(f"\n  OU Z-scores ({len(arr)} scores):")
        print(f"    mean={arr.mean():.2f}  median={np.median(arr):.2f}  "
              f"min={arr.min():.2f}  max={arr.max():.2f}")
        for thresh in [1.0, 1.5, 2.0, 2.5, 3.0]:
            count = (np.abs(arr) > thresh).sum()
            print(f"    |z| > {thresh}: {count}/{len(arr)} ({count/len(arr)*100:.1f}%)")

    # ── 4. Structural breaks ────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  4. STRUCTURAL BREAKS")
    print("=" * 70)
    sb_count = 0
    h1_ptr = 0
    for i in range(200, len(df_h1)):
        h1_window = df_h1.iloc[max(0, i-200):i+1]
        if check_structural_break(h1_window):
            sb_count += 1
            print(f"    Break at {df_h1.index[i]}")
    print(f"\n  Total structural breaks: {sb_count}")
    if sb_count > 0:
        print("  Each blocks trading for 4 hours")

    print("\n" + "=" * 70)
    print("  DIAGNOSIS COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    main()
