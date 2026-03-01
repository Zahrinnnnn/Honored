"""
order_placer.py — TOJI skill

Places BUY/SELL market orders on XAUUSD.

Paper mode (PAPER_MODE=true)
    Simulates an immediate fill at the signal's entry_price.
    No MetaApi calls are made. A fake order ID is generated.

Live mode (PAPER_MODE=false)
    Sends a market order to MetaApi with SL and TP set on the broker.
    The broker manages closing positions — TOJI monitors for close events.

Public API
──────────
place_order(signal, lot_size, connection=None) → dict

Return dict keys:
    order_id:    str   — "PAPER-<8hex>" or MetaApi orderId/positionId
    entry_price: float — simulated or actual fill price
    sl_price:    float
    tp_price:    float
    lot_size:    float
    paper:       bool
"""

import logging
import os
import uuid

from dotenv import load_dotenv

load_dotenv()

PAPER_MODE = os.environ.get("PAPER_MODE", "true").lower() == "true"
_SYMBOL = "XAUUSD"

logger = logging.getLogger(__name__)


async def place_order(
    signal: dict,
    lot_size: float,
    connection=None,
) -> dict:
    """
    Place a BUY or SELL market order.

    Args:
        signal:     Approved signal dict from NANAMI/GETO.
                    Required keys: direction, entry_price, sl_price, tp_price.
        lot_size:   Calculated lot size from lot_calculator.
        connection: MetaApi RPC connection (ignored in paper mode).

    Returns:
        Order result dict (see module docstring).

    Raises:
        RuntimeError: if live mode order placement fails.
    """
    if PAPER_MODE or connection is None:
        return _simulate_fill(signal, lot_size)
    return await _live_order(connection, signal, lot_size)


# ─────────────────────────────────────────────────────────────────────────────
# Paper mode
# ─────────────────────────────────────────────────────────────────────────────

def _simulate_fill(signal: dict, lot_size: float) -> dict:
    """Simulate an immediate fill at the signal's entry_price."""
    order_id    = f"PAPER-{uuid.uuid4().hex[:8].upper()}"
    entry_price = float(signal.get("entry_price", 0.0))
    sl_price    = float(signal.get("sl_price",    0.0))
    tp_price    = float(signal.get("tp_price",    0.0))
    direction   = signal.get("direction", "")

    logger.info(
        "[PAPER] %s fill simulated | lot=%.2f | entry=%.2f SL=%.2f TP=%.2f | id=%s",
        direction, lot_size, entry_price, sl_price, tp_price, order_id,
    )
    return {
        "order_id":    order_id,
        "entry_price": entry_price,
        "sl_price":    sl_price,
        "tp_price":    tp_price,
        "lot_size":    lot_size,
        "paper":       True,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Live mode
# ─────────────────────────────────────────────────────────────────────────────

async def _live_order(connection, signal: dict, lot_size: float) -> dict:
    """Send a market order via MetaApi with SL and TP."""
    direction   = signal.get("direction", "")
    entry_price = float(signal.get("entry_price", 0.0))
    sl_price    = float(signal.get("sl_price",    0.0))
    tp_price    = float(signal.get("tp_price",    0.0))

    try:
        if direction == "BUY":
            result = await connection.create_market_buy_order(
                _SYMBOL, lot_size, sl_price, tp_price,
                options={"comment": "HONORED"},
            )
        else:
            result = await connection.create_market_sell_order(
                _SYMBOL, lot_size, sl_price, tp_price,
                options={"comment": "HONORED"},
            )

        order_id   = result.get("orderId") or result.get("positionId", "UNKNOWN")
        fill_price = float(result.get("openPrice", entry_price))

        logger.info(
            "[LIVE] %s order placed | lot=%.2f | entry=%.5f SL=%.5f TP=%.5f | id=%s",
            direction, lot_size, fill_price, sl_price, tp_price, order_id,
        )
        return {
            "order_id":    order_id,
            "entry_price": fill_price,
            "sl_price":    sl_price,
            "tp_price":    tp_price,
            "lot_size":    lot_size,
            "paper":       False,
        }
    except Exception as exc:
        logger.error("MetaApi order placement failed: %s", exc)
        raise RuntimeError(f"Order placement failed: {exc}") from exc
