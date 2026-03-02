# -*- coding: utf-8 -*-
"""
Real end-to-end probe for NANAMI -- single poll cycle, prints every step.

Usage:
    python tests/e2e_nanami.py

Requires .env with META_API_TOKEN, HFM_ACCOUNT_ID.
PAPER_MODE=true is safe -- no orders are placed.
"""

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

SEP = "-" * 60


async def main() -> None:
    from core.state_manager import StateManager
    import core.metaapi_client as _ma_mod
    from agents.nanami.skills import (
        market_data,
        indicator_engine,
        session_detector,
        regime_detector,
        m5_momentum,
        m1_meanrev,
        london_breakout,
    )

    print(SEP)
    print("  NANAMI - real end-to-end probe")
    print(SEP)

    # 1. env check
    paper   = os.getenv("PAPER_MODE", "true").lower() != "false"
    db_path = os.getenv("HONORED_DB_PATH", "paper.db" if paper else "honored.db")

    if not os.getenv("META_API_TOKEN") or not os.getenv("HFM_ACCOUNT_ID"):
        print("MISSING: META_API_TOKEN or HFM_ACCOUNT_ID not in .env")
        sys.exit(1)

    print(f"  PAPER_MODE   : {paper}")
    print(f"  ACCOUNT_TYPE : {os.getenv('ACCOUNT_TYPE', 'CENTS')}")
    print(f"  DB           : {db_path}")
    print()

    # 2. state manager
    print("[ 1 ] Initialising SQLite...")
    state = StateManager(db_path)
    await state.connect()
    print("      OK -- tables ready")
    print()

    # 3. MetaApi connection
    print("[ 2 ] Connecting to MetaApi...")
    try:
        await _ma_mod.get_connection()
        print("      OK -- connected")
    except Exception as exc:
        print(f"      FAILED: {exc}")
        sys.exit(1)
    print()

    # 4. Session
    print("[ 3 ] Session detection...")
    ctx = session_detector.get_session_context()
    print(f"      UTC time  : {datetime.now(timezone.utc).strftime('%H:%M:%S')}")
    print(f"      Session   : {ctx.name}")
    print(f"      In session: {ctx.is_active}")
    print(f"      London BO : {ctx.is_breakout_window}")
    print()

    # 5. Current price
    print("[ 4 ] Fetching current price...")
    price_info = await market_data.get_current_price()
    bid        = price_info["bid"]
    ask        = price_info["ask"]
    spread_usd = price_info.get("spread", 0.0)
    if bid:
        print(f"      Bid    : {bid:.2f}")
        print(f"      Ask    : {ask:.2f}")
        print(f"      Spread : ${spread_usd:.3f}")
    else:
        print("      price fetch returned zeros (market may be closed)")
    print()

    # 6. OHLCV candles
    print("[ 5 ] Fetching M5 candles (300 bars)...")
    df_m5 = await market_data.get_candles("5m", 300)
    if df_m5.empty:
        print("      FAILED -- empty DataFrame. Market closed or MetaApi issue.")
        await _ma_mod.close_connection()
        sys.exit(1)
    print(f"      Got {len(df_m5)} bars  "
          f"[{df_m5['time'].iloc[0].strftime('%Y-%m-%d %H:%M')} to "
          f"{df_m5['time'].iloc[-1].strftime('%Y-%m-%d %H:%M')}]")

    print("[ 6 ] Fetching M1 candles (300 bars)...")
    df_m1 = await market_data.get_candles("1m", 300)
    if df_m1.empty:
        print("      M1 fetch returned empty -- Model B will be skipped")
    else:
        print(f"      Got {len(df_m1)} bars")
    print()

    # 7. Asian range
    print("[ 7 ] Asian range (00:00-07:00 GMT)...")
    asian_high, asian_low = await market_data.get_asian_range()
    if asian_high:
        print(f"      High : {asian_high:.2f}   Low : {asian_low:.2f}")
        print(f"      Range: ${asian_high - asian_low:.2f}")
    else:
        print("      no Asian range data (before 07:00 UTC or data gap)")
    print()

    # 8. Indicators
    print("[ 8 ] Computing M5 indicators...")
    df_m5 = indicator_engine.add_indicators(df_m5)
    last  = df_m5.iloc[-1]
    print(f"      ATR14        : {float(last.get('atr14', float('nan'))):.3f}")
    print(f"      ADX14        : {float(last.get('adx14', float('nan'))):.2f}")
    print(f"      BB width %   : {float(last.get('bb_width_pct', float('nan'))):.2f}")
    print(f"      z_score_50   : {float(last.get('z_score_50', float('nan'))):.4f}")
    print(f"      kalman_vel   : {float(last.get('kalman_velocity', float('nan'))):.6f}")
    print()

    # 9. Regime
    print("[ 9 ] Regime detection...")
    regime = regime_detector.detect_regime(df_m5)
    print(f"      Regime : {regime}")
    print()

    # 10. Model signals
    print("[ 10 ] Model A -- M5 Momentum (Kalman + z-score)...")
    sig_a = m5_momentum.generate_signal(df_m5, ctx.name, regime)
    if sig_a:
        print(f"       SIGNAL  {sig_a['direction']}")
        print(f"       entry={sig_a['entry_price']:.2f}  sl={sig_a['sl_price']:.2f}  "
              f"tp={sig_a['tp_price']:.2f}  sl_dist=${sig_a['sl_distance']:.2f}")
        print(f"       reason: {sig_a['reason']}")
    else:
        print("       no signal")
    print()

    print("[ 11 ] Model B -- M1 Mean Reversion (OU + ADF)...")
    sig_b = None
    if not df_m1.empty:
        df_m1 = indicator_engine.add_indicators(df_m1)
        sig_b = m1_meanrev.generate_signal(df_m1, ctx.name, regime)
        if sig_b:
            print(f"       SIGNAL  {sig_b['direction']}")
            print(f"       entry={sig_b['entry_price']:.2f}  sl={sig_b['sl_price']:.2f}  "
                  f"tp={sig_b['tp_price']:.2f}  sl_dist=${sig_b['sl_distance']:.2f}")
            print(f"       reason: {sig_b['reason']}")
        else:
            print("       no signal")
    else:
        print("       skipped (no M1 data)")
    print()

    print("[ 12 ] Model C -- London Breakout...")
    sig_c = london_breakout.generate_signal(df_m5, asian_high, asian_low)
    if sig_c:
        print(f"       SIGNAL  {sig_c['direction']}")
        print(f"       entry={sig_c['entry_price']:.2f}  sl={sig_c['sl_price']:.2f}  "
              f"tp={sig_c['tp_price']:.2f}  sl_dist=${sig_c['sl_distance']:.2f}")
        print(f"       reason: {sig_c['reason']}")
    else:
        print("       no signal")
    print()

    # 13. News check
    print("[ 13 ] News calendar check...")
    minutes_to_news = 999.0
    try:
        from core import news_fetcher
        minutes_to_news = news_fetcher.minutes_to_next_high_impact_event()
        news_clear      = news_fetcher.is_news_clear()
        print(f"       next high-impact event in : {minutes_to_news:.0f} min")
        print(f"       news_clear                : {news_clear}")
    except Exception as exc:
        print(f"       news fetch failed: {exc}")
    print()

    # 14. Write session_info
    print("[ 14 ] Writing session_info to SQLite...")
    await state.set_session_info("current_session",      str(ctx.name))
    await state.set_session_info("asian_range_high",     str(asian_high))
    await state.set_session_info("asian_range_low",      str(asian_low))
    await state.set_session_info("minutes_to_next_news", str(minutes_to_news))
    print("       OK")
    print()

    # Summary
    await state.close()
    await _ma_mod.close_connection()

    signal = sig_a or sig_b or sig_c
    print(SEP)
    if signal:
        print(f"  RESULT  : {signal['model']} {signal['direction']} signal generated")
        print(f"            entry={signal['entry_price']:.2f}  "
              f"sl={signal['sl_price']:.2f}  tp={signal['tp_price']:.2f}")
    else:
        print("  RESULT  : no signal this poll  (conditions not met -- normal)")
    print(f"  Regime  : {regime}")
    print(f"  Session : {ctx.name}  (in_session={ctx.is_active})")
    print(f"  Price   : bid={bid:.2f}  ask={ask:.2f}  spread=${spread_usd:.3f}")
    print(SEP)


if __name__ == "__main__":
    asyncio.run(main())
