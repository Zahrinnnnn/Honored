"""
lot_calculator.py — TOJI skill

Calculates lot size from account balance and SL distance.

Formula
───────
    lot = round((balance × risk_pct) / (sl_distance × XAUUSD_POINT_VALUE), 2)

XAUUSD_POINT_VALUE = 100 for both CENTS and STANDARD accounts.
  MetaApi returns CENTS balance in USC (e.g. $5 USD = 500 USC).
  1 cent-lot XAUUSD = 100 USC per $1 gold move — same formula as STANDARD.

Examples (balance=$500, SL=$8, risk=20%):
  lot = ($500 × 0.20) / ($8 × 100) = $100 / $800 = 0.13 lots

Public API
──────────
calculate_lot(balance, sl_distance, risk_pct) → float
calculate_risk_amount(balance, risk_pct) → float
"""

import logging

from core.constants import RISK_PER_TRADE_PCT, XAUUSD_POINT_VALUE

logger = logging.getLogger(__name__)

_MIN_LOT = 0.01


def calculate_lot(
    balance: float,
    sl_distance: float,
    risk_pct: float = RISK_PER_TRADE_PCT,
    consecutive_losses: int = 0,
) -> float:
    """
    Calculate lot size for a trade with anti-martingale sizing.

    After each consecutive loss the lot is halved (2^consecutive_losses).
    This bleeds slowly during losing streaks instead of hemorrhaging.

    Args:
        balance:            Current account balance in USD.
        sl_distance:        Stop-loss distance in USD (must be > 0).
        risk_pct:           Fraction of balance to risk (default RISK_PER_TRADE_PCT).
        consecutive_losses: Current streak of consecutive losses (0 = full size).

    Returns:
        Lot size rounded to 2 decimal places, minimum 0.01.
        Value depends on ACCOUNT_TYPE (CENTS vs STANDARD).

    Raises:
        ValueError: if sl_distance <= 0 or balance <= 0.
    """
    if sl_distance <= 0:
        raise ValueError(f"sl_distance must be > 0, got {sl_distance!r}")
    if balance <= 0:
        raise ValueError(f"balance must be > 0, got {balance!r}")

    risk_amount = balance * risk_pct
    lot = round(risk_amount / (sl_distance * XAUUSD_POINT_VALUE), 2)

    # Anti-martingale: halve lot for each consecutive loss
    if consecutive_losses > 0:
        lot = round(lot / (2 ** consecutive_losses), 2)

    lot = max(lot, _MIN_LOT)

    logger.debug(
        "Lot calc: balance=%.2f risk_pct=%.0f%% risk=%.2f sl=%.2f consec=%d → lot=%.2f",
        balance, risk_pct * 100, risk_amount, sl_distance, consecutive_losses, lot,
    )
    return lot


def calculate_risk_amount(
    balance: float,
    risk_pct: float = RISK_PER_TRADE_PCT,
) -> float:
    """Return the dollar amount risked on this trade."""
    return round(balance * risk_pct, 2)
