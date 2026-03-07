#!/usr/bin/env python3
"""
fetch_history_metaapi.py — Download XAUUSD M5 + M1 historical data from MetaApi.

Fetches actual HFM MT5 broker prices via paginated get_historical_candles().
Saves two CSVs consumed by backtest.py:

    data/XAUUSD_M5.csv   — 5-minute candles  (signals, regime, exits)
    data/XAUUSD_M1.csv   — 1-minute candles  (Model B OU mean-reversion)

History depth is broker-dependent. HFM MT5 typically stores:
    M5 → 2+ years     M1 → 1 year

Script stops naturally when HFM runs out of data.
Each page retries up to 3 times (network resilience).

Requires META_API_TOKEN and HFM_ACCOUNT_ID in .env.

Usage:
    python scripts/fetch_history_metaapi.py
    python scripts/fetch_history_metaapi.py --days 365
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

# Force UTF-8 output on Windows (cp1252 terminals choke on em-dashes etc.)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from core.metaapi_client import get_account, close_connection

SYMBOL      = "XAUUSD"
BATCH_SIZE  = 1000    # candles per request
MAX_RETRIES = 3       # retries per page on network failure
RETRY_DELAY = 3.0     # seconds between retries

_BAR_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}


def _parse_time(t) -> datetime | None:
    """Parse a MetaApi timestamp (datetime or ISO string) to UTC datetime."""
    if t is None:
        return None
    if isinstance(t, str):
        try:
            return datetime.fromisoformat(t.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(t, datetime):
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return None


async def _fetch_timeframe(account, timeframe: str, days: int, output: str) -> bool:
    """
    Paginate backwards from now to collect `days` calendar days of data.
    Returns True on success, False on unrecoverable error.
    """
    bar_mins     = _BAR_MINUTES[timeframe]
    target_start = datetime.now(timezone.utc) - timedelta(days=days)

    print(f"\n--- {SYMBOL} {timeframe.upper()} ({days}d) ---")
    print(f"  Target start : {target_start:%Y-%m-%d %H:%M UTC}")

    all_candles: list[dict] = []
    end_time = datetime.now(timezone.utc)
    page = 0

    while True:
        page += 1
        batch = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                batch = await account.get_historical_candles(
                    SYMBOL, timeframe, end_time, BATCH_SIZE
                )
                break
            except Exception as exc:
                if attempt < MAX_RETRIES:
                    print(f"\n  [page {page}] retry {attempt}/{MAX_RETRIES}: {exc}")
                    await asyncio.sleep(RETRY_DELAY)
                else:
                    print(f"\n  [page {page}] FAILED after {MAX_RETRIES} retries: {exc}",
                          file=sys.stderr)
                    return False

        if not batch:
            print(f"\n  [page {page}] empty — broker data exhausted")
            break

        all_candles.extend(batch)

        oldest_time = None
        newest_time = None
        for c in batch:
            t = _parse_time(c.get("time"))
            if t:
                if oldest_time is None or t < oldest_time:
                    oldest_time = t
                if newest_time is None or t > newest_time:
                    newest_time = t

        print(
            f"  [page {page:>3}] {len(batch):>5} bars  |"
            f"  {oldest_time:%Y-%m-%d %H:%M} to {newest_time:%Y-%m-%d %H:%M}"
            f"  (total {len(all_candles):,})",
            end="\r",
        )

        if oldest_time is None or oldest_time <= target_start:
            print()
            break

        end_time = oldest_time - timedelta(minutes=bar_mins)

    print()

    if not all_candles:
        print(f"  ERROR: No {timeframe} candles returned.", file=sys.stderr)
        return False

    rows = []
    for c in all_candles:
        t = _parse_time(c.get("time"))
        if t is None:
            continue
        rows.append({
            "time":   t,
            "open":   float(c.get("open",  0.0)),
            "high":   float(c.get("high",  0.0)),
            "low":    float(c.get("low",   0.0)),
            "close":  float(c.get("close", 0.0)),
            "volume": int(c.get("tickVolume", c.get("volume", 0))),
        })

    df = (
        pd.DataFrame(rows)
        .sort_values("time")
        .drop_duplicates(subset="time")
        .reset_index(drop=True)
    )
    df = df[df["time"] >= target_start]
    df = df[df["close"] > 0]
    df = df.set_index("time")
    df.index = pd.to_datetime(df.index, utc=True)

    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    df.to_csv(output)

    print(f"  Saved {len(df):>7,} bars  |  "
          f"{df.index[0]:%Y-%m-%d %H:%M UTC}  to  {df.index[-1]:%Y-%m-%d %H:%M UTC}")
    print(f"  Output: {output}")
    return True


_DEFAULT_TIMEFRAMES = {
    "1m":  "data/XAUUSD_M1.csv",
    "5m":  "data/XAUUSD_M5.csv",
    "15m": "data/XAUUSD_M15.csv",
    "1h":  "data/XAUUSD_H1.csv",
    "4h":  "data/XAUUSD_H4.csv",
    "1d":  "data/XAUUSD_D1.csv",
}


async def run(days: int, timeframes: dict[str, str]) -> int:
    print("Connecting to MetaApi ...")
    try:
        account = await get_account()
    except Exception as exc:
        print(f"ERROR: MetaApi connection failed: {exc}", file=sys.stderr)
        print("Check META_API_TOKEN and HFM_ACCOUNT_ID in .env", file=sys.stderr)
        return 1

    print("Connected.")

    failed = []
    for tf, out_path in timeframes.items():
        ok = await _fetch_timeframe(account, tf, days, out_path)
        if not ok:
            failed.append(tf)

    await close_connection()

    if failed:
        print(f"\nFetch incomplete — failed: {', '.join(failed)}", file=sys.stderr)
        return 1

    print("\nAll data fetched successfully.")
    return 0


def main():
    p = argparse.ArgumentParser(
        description="Download XAUUSD historical data from MetaApi."
    )
    p.add_argument(
        "--days", type=int, default=183,
        help="Calendar days to attempt (default: 183 = ~6 months)."
    )
    p.add_argument(
        "--timeframes", nargs="+",
        default=list(_DEFAULT_TIMEFRAMES.keys()),
        choices=list(_DEFAULT_TIMEFRAMES.keys()),
        help="Timeframes to fetch (default: all). Example: --timeframes 15m 1h 4h 1d"
    )
    a = p.parse_args()
    tf_map = {tf: _DEFAULT_TIMEFRAMES[tf] for tf in a.timeframes}
    sys.exit(asyncio.run(run(a.days, tf_map)))


if __name__ == "__main__":
    main()
