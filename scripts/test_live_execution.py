#!/usr/bin/env python3
"""
test_live_execution.py — Manual live trade execution test

Connects to MetaApi, places a real BUY on XAUUSD with minimum lot (0.01),
waits 10 seconds, then closes it. Prints every step.

Usage (from VPS):
    cd /opt/honored
    PAPER_MODE=false python3 scripts/test_live_execution.py

WARNING: This places a REAL order on your MT5 account.
"""

import asyncio
import logging
import os
import sys
import time

# ── path setup ───────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from metaapi_cloud_sdk import MetaApi

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("live_test")

SYMBOL   = "XAUUSD"
LOT      = 0.01
HOLD_SEC = 10   # seconds to hold before closing


# ─────────────────────────────────────────────────────────────────────────────

async def main():
    token      = os.environ.get("META_API_TOKEN") or os.environ.get("METAAPI_TOKEN")
    account_id = os.environ.get("HFM_ACCOUNT_ID")  or os.environ.get("METAAPI_ACCOUNT_ID")

    if not token or not account_id:
        log.error("META_API_TOKEN / HFM_ACCOUNT_ID not set. Check your .env file.")
        sys.exit(1)

    log.info("Connecting to MetaApi…")
    api     = MetaApi(token)
    account = await api.metatrader_account_api.get_account(account_id)

    log.info("Account: %s  state=%s", account.name, account.state)

    conn = account.get_rpc_connection()
    await conn.connect()
    log.info("Waiting for terminal sync (up to 5 min)…")
    await conn.wait_synchronized(300)
    log.info("Synchronized ✓")

    # ── current price ─────────────────────────────────────────────────────────
    price = await conn.get_symbol_price(SYMBOL)
    bid   = price["bid"]
    ask   = price["ask"]
    spread = round(ask - bid, 2)
    log.info("XAUUSD  bid=%.2f  ask=%.2f  spread=$%.2f", bid, ask, spread)

    # ── account snapshot ──────────────────────────────────────────────────────
    acct_info = await conn.get_account_information()
    log.info("Balance=%.2f  Equity=%.2f  Free margin=%.2f",
             acct_info["balance"], acct_info["equity"], acct_info["freeMargin"])

    # ── place BUY ─────────────────────────────────────────────────────────────
    # Minimal SL/TP: $8 risk, $16 reward (well within limits)
    sl_price = round(ask - 8.0, 2)
    tp_price = round(ask + 16.0, 2)

    log.info("Placing BUY %s  lot=%.2f  entry≈%.2f  SL=%.2f  TP=%.2f",
             SYMBOL, LOT, ask, sl_price, tp_price)

    try:
        result = await conn.create_market_buy_order(
            SYMBOL, LOT, sl_price, tp_price,
            options={"comment": "HONORED-TEST"},
        )
    except Exception as exc:
        log.error("Order placement FAILED: %s", exc)
        await conn.close()
        sys.exit(1)

    order_id   = result.get("orderId") or result.get("positionId", "?")
    fill_price = result.get("openPrice", ask)
    log.info("Order placed ✓  id=%s  fill=%.2f", order_id, fill_price)

    # ── wait ──────────────────────────────────────────────────────────────────
    log.info("Holding for %d seconds…", HOLD_SEC)
    await asyncio.sleep(HOLD_SEC)

    # ── live P&L check ────────────────────────────────────────────────────────
    positions = await conn.get_positions()
    open_pos  = [p for p in positions if p.get("symbol") == SYMBOL]
    if open_pos:
        pos = open_pos[0]
        log.info("Position: unrealised P&L = %.2f  current price = %.5f",
                 pos.get("unrealizedProfit", 0), pos.get("currentPrice", 0))

    # ── close position ────────────────────────────────────────────────────────
    pos_id = result.get("positionId") or order_id
    log.info("Closing position %s…", pos_id)

    try:
        close_result = await conn.close_position(pos_id, options={"comment": "HONORED-TEST-CLOSE"})
        close_price  = close_result.get("closePrice", 0)
        pnl          = close_result.get("profit", "n/a")
        log.info("Position closed ✓  close_price=%.2f  P&L=%s", close_price, pnl)
    except Exception as exc:
        log.error("Close FAILED: %s", exc)
        log.error("Close manually in MT5 terminal if needed. Position ID: %s", pos_id)

    # ── final account state ───────────────────────────────────────────────────
    await asyncio.sleep(2)
    acct_after = await conn.get_account_information()
    log.info("Balance after: %.2f  (was %.2f)  diff=%.2f",
             acct_after["balance"], acct_info["balance"],
             acct_after["balance"] - acct_info["balance"])

    await conn.close()
    log.info("Done. Connection closed.")


if __name__ == "__main__":
    asyncio.run(main())
