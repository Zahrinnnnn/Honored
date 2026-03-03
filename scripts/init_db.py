#!/usr/bin/env python3
"""
init_db.py — Initialize the HONORED paper or live database.

Usage:
    python scripts/init_db.py                    # paper.db, $20 balance
    python scripts/init_db.py --balance 100.0    # custom starting balance
    python scripts/init_db.py --live             # initialize honored.db (live)

Creates all 8 tables via StateManager.connect(), then:
  - Seeds the account table with the starting balance
  - Resets all system flags to clean active state
  - Clears any stale trading state from previous sessions

Safe to re-run: INSERT OR IGNORE protects existing data;
only explicit UPDATE calls overwrite (balance, flags).
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from core.state_manager import StateManager


async def init(db_path: str, balance: float, paper: bool):
    sm = StateManager(db_path=db_path)
    await sm.connect()

    # ── System flags → clean active state ────────────────────────────────────
    await sm.set_system_flag("pause_flag",          False)
    await sm.set_system_flag("halt_flag",           False)
    await sm.set_system_flag("emergency_halt_flag", False)
    await sm.set_system_status("ACTIVE")

    # ── Account → seed with starting balance ─────────────────────────────────
    await sm.update_account(
        balance        = balance,
        equity         = balance,
        peak_balance   = balance,
        current_dd_pct = 0.0,
        open_positions = 0,
    )

    # ── Trading state → reset all counters ───────────────────────────────────
    await sm.set_trading_state("consecutive_losses",   "0")
    await sm.set_trading_state("total_trades_today",   "0")
    await sm.set_trading_state("last_trade_result",    "")
    await sm.set_trading_state("last_trade_timestamp", "")
    await sm.set_trading_state("last_signal",          "")
    await sm.set_trading_state("last_risk_decision",   "")

    await sm.close()

    mode = "PAPER" if paper else "LIVE"
    print(f"[init_db] ✓ {mode} database initialized: {db_path}")
    print(f"[init_db] ✓ Starting balance : ${balance:.2f}")
    print(f"[init_db] ✓ System status    : ACTIVE")
    print(f"[init_db] ✓ All flags        : cleared")


def main():
    parser = argparse.ArgumentParser(
        description="Initialize HONORED trading database with clean state."
    )
    parser.add_argument(
        "--balance", type=float, default=20.0,
        help="Starting balance in USD (default: 20.0)",
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Initialize live DB (honored.db). Default: paper.db",
    )
    args = parser.parse_args()

    paper   = not args.live
    db_path = os.environ.get(
        "HONORED_DB_PATH",
        "paper.db" if paper else "honored.db",
    )

    asyncio.run(init(db_path=db_path, balance=args.balance, paper=paper))


if __name__ == "__main__":
    main()
