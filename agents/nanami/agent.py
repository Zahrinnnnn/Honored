"""
agent.py — NANAMI (Analyst)

Autonomous market-watching loop. Runs continuously as a standalone
asyncio process. Responsibilities:

  1. Detect current session and regime every poll.
  2. Fetch live OHLCV candles (M1, M5, M15) and compute indicators.
  3. Update shared session_info in SQLite (session, regime, spread, Asian range,
     news timing) — GETO reads these without hitting the network itself.
  4. Apply per-model session trade limits before generating any signal.
  5. Write approved signals to trading_state.last_signal for GETO to validate.
  6. Respect APPROVED signal guard — never overwrite a signal GETO has approved
     but TOJI hasn't executed yet.

Poll cadence:
  • Active session: every NANAMI_POLL_ACTIVE  seconds (60s)
  • Asian blackout: every NANAMI_POLL_BLACKOUT seconds (300s)
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

# Ensure project root is on the path when run as a script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

load_dotenv()

from core.constants import (  # noqa: E402
    MODEL_A, MODEL_B, MODEL_C,
    MODEL_SESSION_LIMITS, MODEL_SESSIONS,
    NANAMI_POLL_ACTIVE, NANAMI_POLL_BLACKOUT,
)
from core.state_manager import StateManager  # noqa: E402
from core.news_fetcher import minutes_to_next_high_impact_event  # noqa: E402

from agents.nanami.skills import (  # noqa: E402
    market_data,
    indicator_engine,
    session_detector,
    regime_detector,
    m5_momentum,
    m1_meanrev,
    london_breakout,
)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [NANAMI] %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("nanami")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run():
    logger.info("NANAMI starting up — analyst process online")

    async with StateManager() as state:
        while True:
            try:
                await _poll(state)
            except Exception as exc:
                logger.exception("Unhandled error in poll cycle: %s", exc)

            # Determine sleep duration based on session
            if session_detector.is_blackout_period():
                await asyncio.sleep(NANAMI_POLL_BLACKOUT)
            else:
                await asyncio.sleep(NANAMI_POLL_ACTIVE)


async def _poll(state: StateManager):
    # ── 0. Classify session (single atomic clock read for the entire poll) ──
    ctx = session_detector.get_session_context()

    if ctx.is_blackout:
        await state.set_session_info("current_session", "ASIAN_BLACKOUT")
        logger.debug("Asian blackout — sleeping")
        return

    session = ctx.name
    if session is None:
        await state.set_session_info("current_session", "NONE")
        logger.debug(
            "Outside active session windows — no signal attempt"
        )
        return

    await state.set_session_info("current_session", session)
    is_breakout_window = ctx.is_breakout_window

    # ── 2. Fetch candles ────────────────────────────────────────────────────
    df_m5 = await market_data.get_candles("5m", count=200)
    if df_m5.empty:
        logger.warning("M5 candles unavailable — skipping poll")
        return

    df_m5 = indicator_engine.add_indicators(df_m5)

    # ── 3. Detect regime (from M5) ──────────────────────────────────────────
    regime = regime_detector.detect_regime(df_m5)
    await state.set_session_info("current_regime", regime)
    logger.info("Session=%s  Regime=%s", session, regime)

    # ── 4. Update spread & news timing in session_info ──────────────────────
    price = await market_data.get_current_price()
    await state.set_session_info("current_spread",    str(price["spread"]))
    await state.set_session_info("current_bid",       str(price["bid"]))
    await state.set_session_info("current_ask",       str(price["ask"]))

    mins_to_news = minutes_to_next_high_impact_event()
    await state.set_session_info("minutes_to_next_news", str(round(mins_to_news, 1)))

    # ── 5. Asian range (computed once per day at breakout window open) ───────
    if is_breakout_window:
        asian_high_str = await state.get_session_info("asian_range_high")
        # Recompute if missing or stale (zero means not yet set today)
        if not asian_high_str or float(asian_high_str or 0) == 0:
            a_high, a_low = await market_data.get_asian_range()
            if a_high > 0 and a_low > 0:
                await state.set_session_info("asian_range_high", str(a_high))
                await state.set_session_info("asian_range_low",  str(a_low))
                logger.info("Asian range stored: high=%.2f  low=%.2f", a_high, a_low)

    # ── 6. APPROVED signal guard ─────────────────────────────────────────────
    # Don't overwrite a signal GETO approved but TOJI hasn't executed yet.
    last_decision = await state.get_trading_state("last_risk_decision")
    if last_decision == "APPROVED":
        logger.debug("Signal APPROVED and awaiting TOJI — skipping new signal generation")
        return

    # ── 7. Signal generation ─────────────────────────────────────────────────
    signal = None

    if is_breakout_window:
        # Only Model C is allowed during 07:00–07:30
        signal = await _try_model_c(state, df_m5)

    else:
        # Model A: TRENDING regime, LONDON_OPEN or NY_OVERLAP only
        if signal is None and session in MODEL_SESSIONS[MODEL_A] and regime == "TRENDING":
            signal = await _try_model_a(state, df_m5, session, regime)

        # Model B: RANGING regime, any active session
        if signal is None and session in MODEL_SESSIONS[MODEL_B] and regime == "RANGING":
            signal = await _try_model_b(state, session, regime)

    # ── 8. Persist signal ────────────────────────────────────────────────────
    if signal:
        signal["timestamp"] = datetime.now(timezone.utc).isoformat()
        signal["status"]    = "PENDING"
        await state.set_trading_state("last_signal", json.dumps(signal))
        logger.info(
            "Signal → %s %s | entry=%.2f  SL=%.2f  TP=%.2f | %s",
            signal["model"], signal["direction"],
            signal["entry_price"], signal["sl_price"], signal["tp_price"],
            signal["reason"],
        )
    else:
        logger.debug("No signal this poll — session=%s regime=%s", session, regime)


# ---------------------------------------------------------------------------
# Per-model helpers
# ---------------------------------------------------------------------------

async def _try_model_a(
    state: StateManager,
    df_m5,
    session: str,
    regime: str,
):
    """Fetch M15 if needed, check session limit, run Model A."""
    count = await state.get_session_trade_count(session, MODEL_A)
    if count >= MODEL_SESSION_LIMITS[MODEL_A]:
        logger.debug(
            "Model A session limit reached (%d/%d) for %s",
            count, MODEL_SESSION_LIMITS[MODEL_A], session,
        )
        return None

    # Fetch M15 candles (only when Model A is eligible)
    df_m15 = await market_data.get_candles("15m", count=100)
    if df_m15.empty:
        logger.warning("M15 candles unavailable — Model A skipped")
        return None
    df_m15 = indicator_engine.add_indicators(df_m15)

    return m5_momentum.generate_signal(df_m5, df_m15, session, regime)


async def _try_model_b(
    state: StateManager,
    session: str,
    regime: str,
):
    """Fetch M1 if needed, check session limit, run Model B."""
    count = await state.get_session_trade_count(session, MODEL_B)
    if count >= MODEL_SESSION_LIMITS[MODEL_B]:
        logger.debug(
            "Model B session limit reached (%d/%d) for %s",
            count, MODEL_SESSION_LIMITS[MODEL_B], session,
        )
        return None

    # Fetch M1 candles (only when Model B is eligible)
    df_m1 = await market_data.get_candles("1m", count=300)
    if df_m1.empty:
        logger.warning("M1 candles unavailable — Model B skipped")
        return None
    df_m1 = indicator_engine.add_indicators(df_m1)

    return m1_meanrev.generate_signal(df_m1, session, regime)


async def _try_model_c(state: StateManager, df_m5):
    """Check daily limit, fetch Asian range, run Model C."""
    count = await state.get_session_trade_count("LONDON_BREAKOUT", MODEL_C)
    if count >= MODEL_SESSION_LIMITS[MODEL_C]:
        logger.debug("Model C daily limit reached (1/1)")
        return None

    asian_high_str = await state.get_session_info("asian_range_high")
    asian_low_str  = await state.get_session_info("asian_range_low")

    try:
        asian_high = float(asian_high_str or 0)
        asian_low  = float(asian_low_str  or 0)
    except ValueError:
        asian_high, asian_low = 0.0, 0.0

    if asian_high <= 0 or asian_low <= 0:
        logger.warning("Model C: Asian range not available yet — skipping")
        return None

    return london_breakout.generate_signal(df_m5, asian_high, asian_low)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(run())
