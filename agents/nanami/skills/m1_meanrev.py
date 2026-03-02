"""
m1_meanrev.py — NANAMI skill

Model B — M1 Mean Reversion Scalp (Quant Upgrade)

Entry logic:
  BUY:  OU z-score < -MODEL_B_OU_ENTRY_ZSCORE (price > 2σ below OU mean)
  SELL: OU z-score > +MODEL_B_OU_ENTRY_ZSCORE (price > 2σ above OU mean)

  Gates (in order — all must pass):
    1. regime == "RANGING"
    2. len(df) >= MODEL_B_OU_LOOKBACK (200 M1 bars)
    3. ATR is valid (post-warm-up)
    4. ADF test confirms stationarity (p < 0.05) — fail-safe if ADF errors
    5. fit_ou() succeeds (θ > 0 — genuine mean reversion)
    6. half_life_bars in [MODEL_B_MIN_HALF_LIFE, MODEL_B_MAX_HALF_LIFE] (5–30 bars)
    7. |z_ou| > MODEL_B_OU_ENTRY_ZSCORE (2.0)

Sessions: Any active session (LONDON_OPEN, NY_OVERLAP, NY_CLOSE)
Regime:   RANGING only
SL:       0.5 × |close - ou_mu|, clamped to [M1_SL_MIN, M1_SL_MAX] = [$3, $5]
TP:       SL × 3 (fixed 1:3 RR)
"""

import uuid
import logging
from typing import Optional

import pandas as pd

from core.constants import (
    MODEL_B,
    MODEL_B_OU_ENTRY_ZSCORE,
    MODEL_B_OU_LOOKBACK,
    MODEL_B_MIN_HALF_LIFE,
    MODEL_B_MAX_HALF_LIFE,
    M1_SL_MIN, M1_SL_MAX,
)
from agents.nanami.skills.indicator_engine import has_valid_indicators
from agents.nanami.skills.stat_tests import adf_stationary, fit_ou, ou_zscore

logger = logging.getLogger(__name__)

_RR = 3.0


def generate_signal(
    df_m1: pd.DataFrame,
    session: str,
    regime: str,
) -> Optional[dict]:
    """
    Attempt to generate a Model B signal from the latest completed M1 candle.

    Args:
        df_m1:   M1 DataFrame with indicator columns (from indicator_engine).
        session: Current session name.
        regime:  Current regime string (must be "RANGING" to fire).

    Returns:
        Signal dict or None if no setup is found.
    """
    # ── Gate 1: regime ───────────────────────────────────────────────────────
    if regime != "RANGING":
        return None

    # ── Gate 2: sufficient bars for OU fitting ───────────────────────────────
    if len(df_m1) < MODEL_B_OU_LOOKBACK:
        logger.debug("Model B: insufficient M1 data (%d < %d bars)",
                     len(df_m1), MODEL_B_OU_LOOKBACK)
        return None

    last = df_m1.iloc[-1]

    # ── Gate 3: ATR warm-up ──────────────────────────────────────────────────
    if not has_valid_indicators(last, ["atr14"]):
        logger.debug("Model B: ATR not warmed up")
        return None

    prices = df_m1["close"].values[-MODEL_B_OU_LOOKBACK:]
    close  = float(last["close"])

    # ── Gate 4: ADF stationarity ─────────────────────────────────────────────
    adf = adf_stationary(prices)
    if not adf["stationary"]:
        logger.debug("Model B: ADF gate — not stationary (p=%.4f, verdict=%s)",
                     adf["p_value"], adf["verdict"])
        return None

    # ── Gate 5: OU fit ───────────────────────────────────────────────────────
    ou = fit_ou(prices, dt=1.0 / 1440)
    if ou is None:
        logger.debug("Model B: fit_ou returned None (no mean reversion)")
        return None

    # ── Gate 6: half-life bounds ─────────────────────────────────────────────
    hl = ou["half_life_bars"]
    if not (MODEL_B_MIN_HALF_LIFE <= hl <= MODEL_B_MAX_HALF_LIFE):
        logger.debug("Model B: half_life=%.1f bars outside [%d, %d] — skipped",
                     hl, MODEL_B_MIN_HALF_LIFE, MODEL_B_MAX_HALF_LIFE)
        return None

    # ── Gate 7: OU z-score entry ─────────────────────────────────────────────
    z = ou_zscore(close, ou)

    if z < -MODEL_B_OU_ENTRY_ZSCORE:
        sl_distance = _sl_distance(close, ou["mu"])
        return _build("BUY", close, sl_distance, session, regime, z, ou)

    if z > MODEL_B_OU_ENTRY_ZSCORE:
        sl_distance = _sl_distance(close, ou["mu"])
        return _build("SELL", close, sl_distance, session, regime, z, ou)

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sl_distance(close: float, ou_mu: float) -> float:
    """
    SL = 0.5 × |close - μ_ou|, clamped to [M1_SL_MIN, M1_SL_MAX].
    The floor ensures the trade is viable given spread constraints.
    """
    raw = 0.5 * abs(close - ou_mu)
    return max(M1_SL_MIN, min(M1_SL_MAX, raw))


def _build(
    direction: str,
    entry: float,
    sl_distance: float,
    session: str,
    regime: str,
    z: float,
    ou: dict,
) -> dict:
    tp_distance = sl_distance * _RR
    if direction == "BUY":
        sl_price = round(entry - sl_distance, 2)
        tp_price = round(entry + tp_distance, 2)
    else:
        sl_price = round(entry + sl_distance, 2)
        tp_price = round(entry - tp_distance, 2)

    return {
        "id":          str(uuid.uuid4()),
        "model":       MODEL_B,
        "direction":   direction,
        "entry_price": round(entry, 2),
        "sl_price":    sl_price,
        "tp_price":    tp_price,
        "sl_distance": round(sl_distance, 2),
        "session":     session,
        "regime":      regime,
        "reason":      (
            f"OU mean-rev | z={z:.2f} | mu={ou['mu']:.2f} "
            f"| half_life={ou['half_life_bars']:.1f} bars"
        ),
    }
