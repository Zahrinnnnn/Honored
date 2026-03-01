"""
lot_calculator.py — TOJI skill

Calculates lot size from account balance and SL distance.

Formula (from CLAUDE.md)
────────────────────────
    lot = round((balance × risk_pct) / sl_distance, 2)

balance and sl_distance are both in USD.
MetaApi reports balance in USD even on HFM Cents account.

Public API
──────────
calculate_lot(balance, sl_distance, risk_pct) → float
calculate_risk_amount(balance, risk_pct) → float
"""

import logging

from core.constants import RISK_PER_TRADE_PCT

logger = logging.getLogger(__name__)

_MIN_LOT = 0.01


def calculate_lot(
    balance: float,
    sl_distance: float,
    risk_pct: float = RISK_PER_TRADE_PCT,
) -> float:
    """
    Calculate lot size for a trade.

    Args:
        balance:     Current account balance in USD.
        sl_distance: Stop-loss distance in USD (must be > 0).
        risk_pct:    Fraction of balance to risk (default 10%).

    Returns:
        Lot size rounded to 2 decimal places, minimum 0.01.

    Raises:
        ValueError: if sl_distance <= 0.

    Examples:
        balance=$20, SL=$5  → lot=0.40  (risk $2 / $5 = 0.40)
        balance=$40, SL=$5  → lot=0.80  (auto-scales with growth)
        balance=$20, SL=$8  → lot=0.25
    """
    if sl_distance <= 0:
        raise ValueError(f"sl_distance must be > 0, got {sl_distance!r}")
    if balance <= 0:
        raise ValueError(f"balance must be > 0, got {balance!r}")

    risk_amount = balance * risk_pct
    lot = round(risk_amount / sl_distance, 2)
    lot = max(lot, _MIN_LOT)

    logger.debug(
        "Lot calc: balance=%.2f risk_pct=%.0f%% risk=%.2f sl=%.2f → lot=%.2f",
        balance, risk_pct * 100, risk_amount, sl_distance, lot,
    )
    return lot


def calculate_risk_amount(
    balance: float,
    risk_pct: float = RISK_PER_TRADE_PCT,
) -> float:
    """Return the dollar amount risked on this trade."""
    return round(balance * risk_pct, 2)
