"""
trade_monitor.py — TOJI skill

Monitors open positions and detects SL/TP exits.

Paper mode
    Open trades are tracked in the trades table (result IS NULL).
    Current price is fetched from MetaApi (read-only, no orders placed).
    If MetaApi is unavailable, monitoring is skipped for that cycle.

Live mode
    Open positions are polled from MetaApi.
    The broker manages SL/TP execution; TOJI detects closed positions
    by comparing MetaApi positions against our open trades in SQLite.

PnL formula
    PnL = lot_size × price_diff_USD × XAUUSD_POINT_VALUE
    CENTS (default): XAUUSD_POINT_VALUE=1  → 1 lot = $1/dollar move
    STANDARD:        XAUUSD_POINT_VALUE=100 → 1 lot = $100/dollar move
    Dollar P&L is identical for both types since lot size scales inversely.

Public API
──────────
check_exit(trade, current_bid, current_ask) → "WIN" | "LOSS" | None
calculate_pnl(trade, exit_price) → float
get_current_price(connection, symbol) → dict | None
get_live_closed_positions(connection, open_trade_ids) → list[dict]
"""

import logging
from typing import Optional

from core.constants import XAUUSD_POINT_VALUE

logger = logging.getLogger(__name__)

_SYMBOL = "XAUUSD"


# ─────────────────────────────────────────────────────────────────────────────
# Exit detection
# ─────────────────────────────────────────────────────────────────────────────

def check_exit(
    trade: dict,
    current_bid: float,
    current_ask: float,
) -> Optional[str]:
    """
    Determine if a paper trade has hit its SL or TP.

    For BUY trades:  execution at bid price.
        bid <= sl_price  → LOSS
        bid >= tp_price  → WIN

    For SELL trades: execution at ask price.
        ask >= sl_price  → LOSS
        ask <= tp_price  → WIN

    Returns:
        "WIN"  if take-profit hit,
        "LOSS" if stop-loss hit,
        None   if still open.
    """
    direction = trade.get("direction", "")
    sl = float(trade.get("sl_price", 0.0))
    tp = float(trade.get("tp_price", 0.0))

    if direction == "BUY":
        price = current_bid
        if price <= sl:
            return "LOSS"
        if price >= tp:
            return "WIN"
    elif direction == "SELL":
        price = current_ask
        if price >= sl:
            return "LOSS"
        if price <= tp:
            return "WIN"

    return None


# ─────────────────────────────────────────────────────────────────────────────
# PnL calculation
# ─────────────────────────────────────────────────────────────────────────────

def calculate_pnl(trade: dict, exit_price: float) -> float:
    """
    Calculate realised P&L in USD for a closed XAUUSD trade.

    Formula: PnL = lot_size × price_diff_USD × XAUUSD_POINT_VALUE
    Controlled by ACCOUNT_TYPE env var (CENTS=1, STANDARD=100).

    Args:
        trade:      Open trade record from the trades table.
        exit_price: Actual exit price (bid for BUY close, ask for SELL close).

    Returns:
        Signed P&L in USD (positive = profit, negative = loss).
    """
    direction   = trade.get("direction", "")
    entry_price = float(trade.get("entry_price", 0.0))
    lot_size    = float(trade.get("lot_size",    0.0))

    if direction == "BUY":
        price_diff = exit_price - entry_price
    else:
        price_diff = entry_price - exit_price

    return round(lot_size * price_diff * XAUUSD_POINT_VALUE, 2)


# ─────────────────────────────────────────────────────────────────────────────
# Price fetching (MetaApi)
# ─────────────────────────────────────────────────────────────────────────────

async def get_current_price(connection, symbol: str = _SYMBOL) -> Optional[dict]:
    """
    Fetch current bid/ask for the symbol from MetaApi.

    Returns:
        {"bid": float, "ask": float} or None on failure.
    """
    try:
        price = await connection.get_symbol_price(symbol)
        if price:
            return {
                "bid": float(price.get("bid", 0.0)),
                "ask": float(price.get("ask", 0.0)),
            }
        return None
    except Exception as exc:
        logger.warning("Failed to fetch price for %s: %s", symbol, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Live position tracking
# ─────────────────────────────────────────────────────────────────────────────

async def get_live_positions(connection) -> list:
    """
    Return currently open MetaApi positions.

    Returns list of position dicts, empty list on failure.
    """
    try:
        positions = await connection.get_positions()
        return positions or []
    except Exception as exc:
        logger.error("Failed to fetch live positions: %s", exc)
        return []


async def find_closed_trades(connection, open_trades: list) -> list:
    """
    Identify which of our open trades are no longer open in MetaApi.

    Compares our SQLite open_trades against MetaApi positions.
    Any trade whose order_id is absent from MetaApi is considered closed.

    Returns:
        List of SQLite trade dicts that are no longer in MetaApi positions.
    """
    if not open_trades:
        return []

    try:
        live_positions = await get_live_positions(connection)
        live_ids = {str(p.get("id", "")) for p in live_positions}

        closed = []
        for trade in open_trades:
            trade_order_id = str(trade.get("order_id", ""))
            if trade_order_id and trade_order_id not in live_ids:
                closed.append(trade)

        return closed
    except Exception as exc:
        logger.error("find_closed_trades error: %s", exc)
        return []
