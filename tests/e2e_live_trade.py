"""
e2e_live_trade.py — One-shot live trade test on demo account.

Places a real BUY order on XAUUSD via MetaApi, then immediately closes it.
Uses ACCOUNT_TYPE=STANDARD lot formula.
PAPER_MODE is overridden to false for this script only.

Run:
    python tests/e2e_live_trade.py
"""

import asyncio
import os
import sys

# Force live mode for this script only — does NOT change .env
os.environ["PAPER_MODE"] = "false"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from core.metaapi_client import get_connection, get_account
from agents.toji.skills.lot_calculator import calculate_lot, calculate_risk_amount
from agents.toji.skills.order_placer import place_order


SL_DISTANCE = 5.0   # $5 SL for this test
DIRECTION   = "BUY"


async def main():
    print("=" * 60)
    print("HONORED — Live Trade E2E Test (DEMO ACCOUNT)")
    print("=" * 60)

    # ── 1. Connect ────────────────────────────────────────────────
    print("\n[1] Connecting to MetaApi...")
    connection = await get_connection()
    account    = await get_account()
    print("    Connected.")

    # ── 2. Get current price ───────────────────────────────────────
    print("\n[2] Fetching current XAUUSD price...")
    try:
        price_info = await connection.get_symbol_price("XAUUSD")
        bid = price_info.get("bid", 0)
        ask = price_info.get("ask", 0)
    except Exception as e:
        print(f"    get_symbol_price failed: {e}")
        bid, ask = 0.0, 0.0

    if not bid or not ask:
        print("    Could not get live price — aborting.")
        return

    spread = round(ask - bid, 2)
    print(f"    Bid: ${bid:.2f}  Ask: ${ask:.2f}  Spread: ${spread:.2f}")

    if spread > 4.0:
        print(f"    Spread ${spread:.2f} > $4.00 limit — aborting (safety).")
        return

    # ── 3. Get account balance ────────────────────────────────────
    print("\n[3] Fetching account info...")
    try:
        acct_info = await connection.get_account_information()
        balance = acct_info.get("balance", 1000.0)
        equity  = acct_info.get("equity",  balance)
    except Exception as e:
        print(f"    Could not fetch account info: {e}")
        balance = 1000.0
        equity  = 1000.0

    print(f"    Balance: ${balance:.2f}  Equity: ${equity:.2f}")

    # ── 4. Calculate lot ───────────────────────────────────────────
    print("\n[4] Calculating lot size...")
    entry  = ask  # BUY fills at ask
    sl     = round(entry - SL_DISTANCE, 2)
    tp     = round(entry + SL_DISTANCE * 3, 2)
    lot    = calculate_lot(balance, SL_DISTANCE)
    risk   = calculate_risk_amount(balance)

    print(f"    Direction : {DIRECTION}")
    print(f"    Entry     : ${entry:.2f}")
    print(f"    SL        : ${sl:.2f}  (${SL_DISTANCE:.2f} away)")
    print(f"    TP        : ${tp:.2f}  (${SL_DISTANCE * 3:.2f} away, 1:3 RR)")
    print(f"    Lot size  : {lot}")
    print(f"    Risk      : ${risk:.2f} ({10}% of ${balance:.2f})")

    # ── 5. Place order ─────────────────────────────────────────────
    print("\n[5] Placing LIVE order on demo account...")
    signal = {
        "direction":   DIRECTION,
        "entry_price": entry,
        "sl_price":    sl,
        "tp_price":    tp,
    }

    result = await place_order(signal, lot, connection=connection)

    print(f"\n    Order ID    : {result['order_id']}")
    print(f"    Fill price  : ${result['entry_price']:.2f}")
    print(f"    SL          : ${result['sl_price']:.2f}")
    print(f"    TP          : ${result['tp_price']:.2f}")
    print(f"    Lot         : {result['lot_size']}")
    print(f"    Paper       : {result['paper']}")

    print("\n[6] Waiting 5 seconds then closing position...")
    await asyncio.sleep(5)

    # ── 6. Close position ─────────────────────────────────────────
    order_id = result["order_id"]
    try:
        positions = await connection.get_positions()
        print(f"    Open positions found: {len(positions)}")
        closed = False
        for pos in positions:
            pos_id = str(pos.get("id", ""))
            symbol = pos.get("symbol", "")
            if symbol == "XAUUSD" and (pos_id == order_id or order_id == "UNKNOWN"):
                print(f"    Closing position {pos_id}...")
                close_result = await connection.close_position(pos_id)
                print(f"    Closed: {close_result}")
                closed = True
                break
        if not closed:
            print(f"    Position {order_id} not found in open positions — may have filled differently.")
            print(f"    Open positions: {[p.get('id') for p in positions]}")
    except Exception as e:
        print(f"    Close failed: {e}")

    print("\n" + "=" * 60)
    print("Test complete. Check your demo account MT5 terminal.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
