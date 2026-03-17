"""
agent.py — NANAMI (Analyst)

Autonomous market-watching loop. Runs continuously as a standalone
asyncio process. Responsibilities:

  1. Detect current session every poll.
  2. Fetch live OHLCV candles (M5, H1) and compute indicators.
  3. Compute 6-state regime from H1 data (Z-score × ATR percentile).
  4. Check structural break override (single H1 candle > 3×ATR → 4h halt).
  5. Update shared session_info in SQLite (session, regime, spread,
     news timing) — GETO reads these.
  6. Apply per-model session trade limits before generating any signal.
  7. Dispatch Model A in BULLISH_GRIND / BEARISH_GRIND regimes (NY_OVERLAP only).
  8. Write signals to trading_state.last_signal for GETO to validate.

Poll cadence:
  Active session: every NANAMI_POLL_ACTIVE  seconds (60s)
  Asian blackout: every NANAMI_POLL_BLACKOUT seconds (300s)
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

# Ensure project root is on the path when run as a script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

load_dotenv()

from core.constants import (  # noqa: E402
    MODEL_A,
    MODEL_SESSION_LIMITS, MODEL_SESSIONS,
    NANAMI_POLL_ACTIVE, NANAMI_POLL_BLACKOUT,
    NY_OVERLAP_DEAD_HOUR_START, NY_OVERLAP_DEAD_HOUR_END,
    REGIME_H1_BARS_NEEDED,
    REGIME_BULLISH_GRIND, REGIME_BEARISH_GRIND,
    NO_TRADE_REGIMES,
    STRUCTURAL_BREAK_COOLDOWN_HOURS,
)
from core.state_manager import StateManager  # noqa: E402
from core.news_fetcher import minutes_to_next_high_impact_event  # noqa: E402

from agents.nanami.skills import (  # noqa: E402
    market_data,
    indicator_engine,
    session_detector,
    ou_grind,
)
from agents.nanami.skills.htf_regime import (  # noqa: E402
    detect_regime,
    check_structural_break,
    compute_macro_bias,
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
        logger.debug("Outside active session windows — no signal attempt")
        return

    await state.set_session_info("current_session", session)

    # ── 1. Check structural break cooldown ──────────────────────────────
    sb_until = await state.get_session_info("structural_break_until")
    if sb_until:
        try:
            sb_time = datetime.fromisoformat(sb_until)
            if datetime.now(timezone.utc) < sb_time:
                logger.info("Structural break cooldown active until %s", sb_until)
                return
            else:
                # Cooldown expired — clear it
                await state.set_session_info("structural_break_until", "")
        except (ValueError, TypeError):
            await state.set_session_info("structural_break_until", "")

    # ── 2. Fetch M5 candles + indicators ──────────────────────────────────
    df_m5 = await market_data.get_candles("5m", count=200)
    if df_m5.empty:
        logger.warning("M5 candles unavailable — skipping poll")
        return

    df_m5 = indicator_engine.add_indicators(df_m5)

    # ── 3. Fetch H1 (200 bars for regime) ───────────────────────────────
    df_h1 = await market_data.get_candles("1h", count=REGIME_H1_BARS_NEEDED)

    # ── 4. Detect 6-state regime from H1 ────────────────────────────────
    regime = detect_regime(df_h1)
    macro_bias = compute_macro_bias(df_h1)
    await state.set_session_info("current_regime", regime)
    await state.set_session_info("macro_bias", macro_bias)

    # ── 5. Check structural break ────────────────────────────────────────
    if check_structural_break(df_h1):
        cooldown_end = (
            datetime.now(timezone.utc) + timedelta(hours=STRUCTURAL_BREAK_COOLDOWN_HOURS)
        ).isoformat()
        await state.set_session_info("structural_break_until", cooldown_end)
        await state.push_alert(
            "STRUCTURAL_BREAK",
            f"Structural break detected on H1. "
            f"Trading halted for {STRUCTURAL_BREAK_COOLDOWN_HOURS} hours.",
        )
        logger.warning("STRUCTURAL BREAK — cooldown until %s", cooldown_end)
        return

    logger.info("Session=%s  Regime=%s", session, regime)

    # ── 7. Update spread & news timing ───────────────────────────────────
    price = await market_data.get_current_price()
    await state.set_session_info("current_spread", str(price["spread"]))
    await state.set_session_info("current_bid", str(price["bid"]))
    await state.set_session_info("current_ask", str(price["ask"]))

    mins_to_news = minutes_to_next_high_impact_event()
    await state.set_session_info("minutes_to_next_news", str(round(mins_to_news, 1)))

    # ── 8. APPROVED signal guard ─────────────────────────────────────────
    last_decision = await state.get_trading_state("last_risk_decision")
    if last_decision == "APPROVED":
        logger.debug("Signal APPROVED and awaiting TOJI — skipping new signal generation")
        return

    # ── 9. Signal generation based on regime ─────────────────────────────
    signal = None

    if regime in NO_TRADE_REGIMES:
        logger.info("Regime=%s — no trading allowed", regime)

    elif regime in (REGIME_BULLISH_GRIND, REGIME_BEARISH_GRIND):
        if session in MODEL_SESSIONS[MODEL_A]:
            utc_hour = datetime.now(timezone.utc).hour
            if NY_OVERLAP_DEAD_HOUR_START <= utc_hour < NY_OVERLAP_DEAD_HOUR_END:
                logger.debug("Dead zone (%d:00 UTC) — skipping Model A signal", utc_hour)
            else:
                signal = await _try_model_a(state, df_m5, session, regime, macro_bias)

    # ── 10. Persist signal ───────────────────────────────────────────────
    if signal:
        signal["timestamp"] = datetime.now(timezone.utc).isoformat()
        signal["status"] = "PENDING"
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

async def _try_model_a(state: StateManager, df_m5, session: str, regime: str, macro_bias: str = "NEUTRAL"):
    """Check session limit, run Model A (OU grind)."""
    count = await state.get_session_trade_count(session, MODEL_A)
    if count >= MODEL_SESSION_LIMITS[MODEL_A]:
        logger.debug(
            "Model A session limit reached (%d/%d) for %s",
            count, MODEL_SESSION_LIMITS[MODEL_A], session,
        )
        return None

    return ou_grind.generate_signal(df_m5, session, regime, macro_bias=macro_bias)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(run())
