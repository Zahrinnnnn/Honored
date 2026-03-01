"""
trade_logger.py — TOJI skill

Writes trade records to the trades table via StateManager.

Two operations:
    log_trade_open()  — called immediately after order placement.
                        Creates a new row with result=NULL (trade is open).
    log_trade_close() — called after SL/TP is hit.
                        Updates the row with result, exit_price, pnl, etc.

All SQLite access goes through state_manager — never raw sqlite3.

Public API
──────────
log_trade_open(state, signal, lot_size, order_result)  → trade_id (int)
log_trade_close(state, trade_id, result, exit_price, pnl, balance_after,
                drawdown_pct, duration_mins)            → None
"""

import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from core.state_manager import StateManager

load_dotenv()

PAPER_MODE = os.environ.get("PAPER_MODE", "true").lower() == "true"
logger = logging.getLogger(__name__)


async def log_trade_open(
    state: StateManager,
    signal: dict,
    lot_size: float,
    order_result: dict,
) -> int:
    """
    Log a newly opened trade to the trades table.

    Reads current balance from the account table for balance_before.
    risk_amount = lot_size × sl_distance (matches the lot formula).

    Args:
        state:        Open StateManager instance.
        signal:       Approved signal dict (model, direction, sl_distance, etc.).
        lot_size:     Calculated lot size.
        order_result: Dict returned by order_placer.place_order().

    Returns:
        trade_id — the SQLite rowid of the new trade record.
    """
    now = datetime.now(timezone.utc)

    account = await state.get_account()
    balance_before = float(account.get("balance", 0.0)) if account else 0.0

    sl_distance = float(signal.get("sl_distance", 0.0))
    risk_amount = round(lot_size * sl_distance, 2)

    trade_id = await state.log_trade(
        date          = now.strftime("%Y-%m-%d"),
        time          = now.strftime("%H:%M:%S"),
        model         = signal.get("model",     ""),
        direction     = signal.get("direction", ""),
        entry_price   = float(order_result.get("entry_price", 0.0)),
        sl_price      = float(signal.get("sl_price", 0.0)),
        tp_price      = float(signal.get("tp_price", 0.0)),
        lot_size      = lot_size,
        sl_distance   = sl_distance,
        risk_amount   = risk_amount,
        balance_before = balance_before,
        reason        = signal.get("reason", ""),
        paper         = 1 if PAPER_MODE else 0,
    )

    logger.info(
        "Trade opened: id=%d %s %s | lot=%.2f | entry=%.2f SL=%.2f TP=%.2f",
        trade_id,
        signal.get("model"),
        signal.get("direction"),
        lot_size,
        order_result.get("entry_price", 0.0),
        signal.get("sl_price",  0.0),
        signal.get("tp_price",  0.0),
    )
    return trade_id


async def log_trade_close(
    state: StateManager,
    trade_id: int,
    result: str,
    exit_price: float,
    pnl: float,
    balance_after: float,
    drawdown_pct: float,
    duration_mins: float,
) -> None:
    """
    Update an open trade record with close details.

    Args:
        state:         Open StateManager instance.
        trade_id:      Row id returned by log_trade_open().
        result:        "WIN" or "LOSS".
        exit_price:    Actual exit price (bid/ask at close).
        pnl:           Realised P&L in USD.
        balance_after: Account balance after the trade.
        drawdown_pct:  Current drawdown % after the trade.
        duration_mins: How long the trade was open (minutes).
    """
    await state.update_trade(
        trade_id,
        result        = result,
        exit_price    = exit_price,
        pnl           = pnl,
        balance_after = balance_after,
        drawdown_pct  = round(drawdown_pct, 2),
        duration_mins = round(duration_mins, 1),
    )

    logger.info(
        "Trade closed: id=%d %s | exit=%.2f pnl=%.2f | balance→%.2f DD=%.1f%%",
        trade_id, result, exit_price, pnl, balance_after, drawdown_pct,
    )
