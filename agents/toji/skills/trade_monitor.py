"""
trade_monitor.py — TOJI skill

Monitors open positions: SL/TP, breakeven protection, time kill, max duration.

Exit system (Fixed 1:2 RR + Breakeven):
    1. SL hit → LOSS (or BREAKEVEN if SL was moved to entry)
    2. TP hit → WIN (TP = 2× SL, set at signal time)
    3. Breakeven: at +1 ATR profit, move SL to entry price
    4. Time kill: close if not profitable after 60 min
    5. Max duration: close after 4 hours

Public API:
    check_breakeven(trade, bid, ask) → Optional[float]  (new SL or None)
    check_exit(trade, bid, ask, now) → Optional[dict]  ({result, exit_price, exit_reason})
    calculate_pnl(trade, exit_price) → float
    get_current_price(connection, symbol) → dict | None
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from core.constants import (
    BREAKEVEN_ATR_THRESHOLD,
    MAX_TRADE_DURATION_MINUTES,
    MODEL_A,
    MODEL_A_TIME_KILL_MINUTES,
    OU_TIME_KILL_HALF_LIFE_MULT,
    TIME_KILL_MINUTES,
    XAUUSD_POINT_VALUE,
)

logger = logging.getLogger(__name__)

from core.constants import SYMBOL as _SYMBOL


# ─────────────────────────────────────────────────────────────────────────────
# Breakeven protection (replaces trailing stop)
# ─────────────────────────────────────────────────────────────────────────────

def check_breakeven(
    trade: dict,
    current_bid: float,
    current_ask: float,
) -> Optional[float]:
    """
    Check if trade should move SL to breakeven (entry price).
    Returns new SL price or None.

    Trigger: profit >= BREAKEVEN_ATR_THRESHOLD × atr_at_entry
    Only fires once (if SL is already at or better than entry, skip).
    """
    atr = float(trade.get("atr_at_entry", 0.0))
    if atr <= 0:
        return None

    direction   = trade.get("direction", "")
    entry_price = float(trade.get("entry_price", 0.0))
    current_sl  = float(trade.get("sl_price", 0.0))

    if direction == "BUY":
        profit = current_bid - entry_price
        if profit >= BREAKEVEN_ATR_THRESHOLD * atr and current_sl < entry_price:
            return round(entry_price, 2)

    elif direction == "SELL":
        profit = entry_price - current_ask
        if profit >= BREAKEVEN_ATR_THRESHOLD * atr and current_sl > entry_price:
            return round(entry_price, 2)

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Exit detection
# ─────────────────────────────────────────────────────────────────────────────

def check_exit(
    trade: dict,
    current_bid: float,
    current_ask: float,
    now: datetime = None,
) -> Optional[dict]:
    """
    Determine if a trade should be closed.

    Returns:
        None if still open, or dict:
        {
            "result":      "WIN" | "LOSS" | "BREAKEVEN",
            "exit_price":  float,
            "exit_reason": "SL_HIT" | "TP_HIT" | "TIME_KILL" | "MAX_DURATION",
        }
    """
    direction = trade.get("direction", "")
    sl = float(trade.get("sl_price", 0.0))
    tp = float(trade.get("tp_price", 0.0))
    entry = float(trade.get("entry_price", 0.0))

    # --- SL/TP check ---
    if direction == "BUY":
        price = current_bid
        if price <= sl:
            result = "BREAKEVEN" if abs(sl - entry) < 0.10 else "LOSS"
            return {"result": result, "exit_price": sl, "exit_reason": "SL_HIT"}
        if price >= tp:
            return {"result": "WIN", "exit_price": tp, "exit_reason": "TP_HIT"}
    elif direction == "SELL":
        price = current_ask
        if price >= sl:
            result = "BREAKEVEN" if abs(sl - entry) < 0.10 else "LOSS"
            return {"result": result, "exit_price": sl, "exit_reason": "SL_HIT"}
        if price <= tp:
            return {"result": "WIN", "exit_price": tp, "exit_reason": "TP_HIT"}
    else:
        return None

    # --- Time-based exits ---
    if now is None:
        return None

    open_time = _parse_open_time(trade)
    if open_time is None:
        return None

    elapsed_minutes = (now - open_time).total_seconds() / 60.0

    # Time kill — per-model:
    #   Model A (OU): 3 × half_life_bars × 5 min; fallback MODEL_A_TIME_KILL_MINUTES
    #   Other: TIME_KILL_MINUTES (60 min)
    model     = trade.get("model", "")
    half_life = float(trade.get("half_life_bars", 0.0))
    if half_life > 0:
        time_kill_mins = half_life * OU_TIME_KILL_HALF_LIFE_MULT * 5.0
    elif model == MODEL_A:
        time_kill_mins = MODEL_A_TIME_KILL_MINUTES
    else:
        time_kill_mins = TIME_KILL_MINUTES

    if elapsed_minutes >= time_kill_mins:
        if direction == "BUY" and current_bid <= entry:
            return {"result": "LOSS", "exit_price": current_bid, "exit_reason": "TIME_KILL"}
        if direction == "SELL" and current_ask >= entry:
            return {"result": "LOSS", "exit_price": current_ask, "exit_reason": "TIME_KILL"}

    # Max duration: hard cap
    if elapsed_minutes >= MAX_TRADE_DURATION_MINUTES:
        exit_price = current_bid if direction == "BUY" else current_ask
        result = "WIN" if _is_profitable(trade, exit_price) else "LOSS"
        return {"result": result, "exit_price": exit_price, "exit_reason": "MAX_DURATION"}

    return None


def _is_profitable(trade: dict, exit_price: float) -> bool:
    entry = float(trade.get("entry_price", 0.0))
    if trade.get("direction") == "BUY":
        return exit_price > entry
    return exit_price < entry


def _parse_open_time(trade: dict) -> Optional[datetime]:
    """Parse the trade open time from various possible field names/formats."""
    for key in ("open_time", "created_at", "date"):
        val = trade.get(key)
        if val is None:
            continue
        if isinstance(val, datetime):
            return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
        if isinstance(val, str):
            try:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                return dt
            except (ValueError, TypeError):
                continue
    return None


# ─────────────────────────────────────────────────────────────────────────────
# PnL calculation
# ─────────────────────────────────────────────────────────────────────────────

def calculate_pnl(trade: dict, exit_price: float) -> float:
    """
    Calculate realised P&L in USD for a closed XAUUSD trade.
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


async def get_live_positions(connection) -> list:
    try:
        positions = await connection.get_positions()
        return positions or []
    except Exception as exc:
        logger.error("Failed to fetch live positions: %s", exc)
        return []


async def find_closed_trades(connection, open_trades: list) -> list:
    if not open_trades:
        return []
    try:
        live_positions = await get_live_positions(connection)
        live_ids = {str(p.get("id", "")) for p in live_positions}
        return [t for t in open_trades
                if str(t.get("order_id", "")) and str(t.get("order_id", "")) not in live_ids]
    except Exception as exc:
        logger.error("find_closed_trades error: %s", exc)
        return []
