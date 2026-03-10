"""
agent.py — TOJI (Executor)

Autonomous execution loop. Runs as a standalone asyncio process.

Responsibilities
────────────────
1. Poll trading_state.last_risk_decision every 5 seconds.
2. On "APPROVED": calculate lot → place order (paper or live) → log open trade
   → increment open_positions → mark signal as "PLACED".
3. Monitor open paper trades every TOJI_MONITOR_INTERVAL seconds:
   - Fetch current bid/ask from MetaApi.
   - Call check_exit() for each open paper trade.
   - On SL/TP hit: calculate P&L, call post_trade_update(), log close,
     push WhatsApp alert via alert_queue.
4. In live mode: broker manages SL/TP; TOJI detects closed positions
   by polling MetaApi positions.

Paper mode (PAPER_MODE=true, default)
   - Order placement is simulated (no MetaApi orders placed).
   - MetaApi is still connected for reading current price (trade monitoring).
   - If MetaApi is unavailable: trade monitoring skipped; orders still logged.

Signal lifecycle
────────────────
   NANAMI:  last_signal.status = "PENDING"
   GETO:    last_risk_decision = "APPROVED", last_signal.status = "APPROVED"
   TOJI:    reads "APPROVED" → places order → sets "PLACED" → NANAMI can fire again
"""

import asyncio
import json
import logging
import os
import sys
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
load_dotenv()

from core.constants import RISK_PER_TRADE_PCT, TOJI_MONITOR_INTERVAL  # noqa: E402
from core.state_manager import StateManager, PAPER_MODE                # noqa: E402
from agents.toji.skills.lot_calculator import calculate_lot            # noqa: E402
from agents.toji.skills.order_placer import place_order                # noqa: E402
from datetime import datetime, timezone                                  # noqa: E402
from agents.toji.skills.trade_monitor import (                         # noqa: E402
    check_exit,
    check_breakeven,
    calculate_pnl,
    get_current_price,
)
from agents.toji.skills.trade_logger import log_trade_open, log_trade_close  # noqa: E402
from agents.toji.skills.state_updater import post_trade_update               # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [TOJI] %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("toji")

_POLL_INTERVAL = 5   # seconds between signal checks


# ─────────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────────

async def run():
    logger.info("TOJI starting up — executor online (paper=%s)", PAPER_MODE)

    # Connection is lazy: TOJI does NOT connect MetaApi at startup.
    # NANAMI holds the persistent subscription; TOJI connects only when placing a live order.
    # Trade monitoring reads price from SQLite session_info (written by NANAMI every 60s).
    connection = None

    last_monitor = 0.0

    async with StateManager() as state:
        while True:
            try:
                await _poll_signals(state, connection)

                now = asyncio.get_event_loop().time()
                if now - last_monitor >= TOJI_MONITOR_INTERVAL:
                    await _poll_positions(state, connection)
                    last_monitor = now

            except Exception as exc:
                logger.exception("Unhandled error in TOJI poll: %s", exc)

            await asyncio.sleep(_POLL_INTERVAL)


# ─────────────────────────────────────────────────────────────────────────────
# Signal processing
# ─────────────────────────────────────────────────────────────────────────────

async def _poll_signals(state: StateManager, connection):
    """Check for an APPROVED signal and execute it."""
    decision = await state.get_trading_state("last_risk_decision")
    if decision != "APPROVED":
        return

    raw = await state.get_trading_state("last_signal")
    if not raw:
        return

    try:
        signal = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("last_signal is not valid JSON — skipping")
        return

    if signal.get("status") != "APPROVED":
        return

    await _execute_signal(state, signal, connection)


async def _execute_signal(state: StateManager, signal: dict, connection):
    """Execute a single approved signal: lot calc → order → log → state update."""
    model     = signal.get("model",     "")
    direction = signal.get("direction", "")

    # ── 1. Read account balance for lot calculation ─────────────────────────
    account = await state.get_account()
    balance = float(account.get("balance", 0.0)) if account else 0.0

    if balance <= 0:
        logger.error("Cannot execute trade: balance=%.2f", balance)
        return

    # ── 2. Lot calculation (anti-martingale: halve on consecutive losses) ──
    sl_distance = float(signal.get("sl_distance", 0.0))
    if sl_distance <= 0:
        logger.error("Cannot execute trade: invalid sl_distance=%.2f", sl_distance)
        return

    consec = await state.get_consecutive_losses()
    lot_size = calculate_lot(balance, sl_distance, RISK_PER_TRADE_PCT, consec)
    logger.info(
        "Executing %s %s | balance=%.2f sl=%.2f consec=%d → lot=%.2f",
        model, direction, balance, sl_distance, consec, lot_size,
    )

    # ── 3. Place order ──────────────────────────────────────────────────────
    # In live mode: connect to MetaApi now (lazily, after NANAMI is already subscribed).
    live_connection = connection
    if not PAPER_MODE and live_connection is None:
        live_connection = await _get_connection()

    try:
        order_result = await place_order(signal, lot_size, live_connection)
    except Exception as exc:
        logger.error("Order placement failed: %s — signal dropped", exc)
        await state.set_trading_state("last_risk_decision", "ERROR")
        signal["status"] = "ERROR"
        await state.set_trading_state("last_signal", json.dumps(signal))
        return

    # ── 4. Log open trade ───────────────────────────────────────────────────
    trade_id = await log_trade_open(state, signal, lot_size, order_result)

    # ── 5. Increment open_positions ─────────────────────────────────────────
    open_pos = int(account.get("open_positions", 0)) + 1
    await state.update_account(open_positions=open_pos)

    # ── 6. Mark signal as PLACED (clears queue for NANAMI + GETO) ───────────
    await state.set_trading_state("last_risk_decision", "PLACED")
    signal["status"] = "PLACED"
    await state.set_trading_state("last_signal", json.dumps(signal))

    # ── 7. Push alert to GOJO ───────────────────────────────────────────────
    paper_tag = "[PAPER] " if PAPER_MODE else ""
    await state.push_alert(
        "TRADE_OPENED",
        f"{paper_tag}Trade opened: {model} {direction} "
        f"| lot={lot_size:.2f} entry={order_result['entry_price']:.2f} "
        f"SL={signal.get('sl_price', 0):.2f} TP={signal.get('tp_price', 0):.2f} "
        f"| id={trade_id}",
    )

    logger.info(
        "Signal PLACED: trade_id=%d %s %s | order_id=%s",
        trade_id, model, direction, order_result.get("order_id"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Position monitoring (paper mode)
# ─────────────────────────────────────────────────────────────────────────────

async def _poll_positions(state: StateManager, connection):
    """Check all open trades: breakeven protection, then check exits."""
    open_trades = await state.get_open_trades()
    if not open_trades:
        return

    # Prefer price from SQLite session_info (NANAMI writes bid/ask every 60s).
    # This avoids a second concurrent MetaApi subscription competing with NANAMI.
    bid = float(await state.get_session_info("current_bid") or 0)
    ask = float(await state.get_session_info("current_ask") or 0)

    if bid <= 0 or ask <= 0:
        # Fallback to MetaApi if NANAMI hasn't written price yet
        if connection is None:
            logger.debug("No price available — skipping trade monitor cycle")
            return
        price = await get_current_price(connection)
        if not price:
            logger.warning("Could not fetch price — skipping trade monitor cycle")
            return
        bid = price["bid"]
        ask = price["ask"]
    now = datetime.now(timezone.utc)

    for trade in open_trades:
        # 1. Check breakeven protection
        new_sl = check_breakeven(trade, bid, ask)
        if new_sl is not None:
            await state.update_trade(trade["id"], sl_price=new_sl)
            trade["sl_price"] = new_sl  # update in-memory for exit check
            logger.info("Breakeven activated: id=%d → SL=%.2f", trade["id"], new_sl)

        # 2. Check for exit
        exit_info = check_exit(trade, bid, ask, now)
        if exit_info is None:
            continue

        await _close_trade(state, trade, exit_info, bid, ask)


async def _close_trade(state: StateManager, trade: dict, exit_info: dict,
                       bid: float, ask: float):
    """Close a trade: calculate P&L, update logs and state, push alert."""
    trade_id    = trade["id"]
    direction   = trade.get("direction", "")
    result      = exit_info["result"]
    exit_price  = exit_info["exit_price"]
    exit_reason = exit_info.get("exit_reason", "")

    # BREAKEVEN counts as WIN for state tracking (no loss streak increment)
    state_result = "WIN" if result == "BREAKEVEN" else result

    pnl = calculate_pnl(trade, exit_price)
    balance_before = float(trade.get("balance_before", 0.0))

    balance_after, drawdown_pct, duration_mins = await post_trade_update(
        state         = state,
        result        = state_result,
        pnl           = pnl,
        balance_before = balance_before,
        open_time     = trade.get("created_at"),
        session       = trade.get("reason", ""),
        model         = trade.get("model", ""),
    )

    await log_trade_close(
        state         = state,
        trade_id      = trade_id,
        result        = result,
        exit_price    = exit_price,
        pnl           = pnl,
        balance_after = balance_after,
        drawdown_pct  = drawdown_pct,
        duration_mins = duration_mins,
        exit_reason   = exit_reason,
    )

    paper_tag = "[PAPER] " if PAPER_MODE else ""
    sign = "+" if pnl >= 0 else ""
    await state.push_alert(
        "TRADE_CLOSED",
        f"{paper_tag}Trade closed: {trade.get('model')} {direction} {result} "
        f"({exit_reason}) | PnL={sign}{pnl:.2f} exit={exit_price:.2f} "
        f"| balance→{balance_after:.2f} DD={drawdown_pct:.1f}%",
    )

    logger.info(
        "Trade closed: id=%d %s %s (%s) | PnL=%+.2f | balance→%.2f",
        trade_id, trade.get("model"), result, exit_reason, pnl, balance_after,
    )


# ─────────────────────────────────────────────────────────────────────────────
# MetaApi connection
# ─────────────────────────────────────────────────────────────────────────────

async def _get_connection():
    """
    Get MetaApi connection.
    In paper mode: optional (for price reading).
    In live mode: required (raises if unavailable).
    """
    try:
        from core.metaapi_client import get_connection
        conn = await get_connection()
        logger.info("MetaApi connected")
        return conn
    except Exception as exc:
        if PAPER_MODE:
            logger.warning(
                "MetaApi unavailable (paper mode) — trade monitoring will be skipped: %s", exc
            )
            return None
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    asyncio.run(run())
