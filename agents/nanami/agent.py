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
  7. Dispatch Model A in BULLISH_GRIND / BEARISH_GRIND regimes (NY_OVERLAP + NY_CLOSE).
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
    MODEL_A, MODEL_B, MODEL_C, MODEL_D,
    MODEL_SESSION_LIMITS, MODEL_SESSIONS,
    NANAMI_POLL_ACTIVE, NANAMI_POLL_BLACKOUT,
    REGIME_H1_BARS_NEEDED,
    REGIME_BULLISH_GRIND, REGIME_BEARISH_GRIND,
    REGIME_BULLISH_BLOWOFF, NO_TRADE_REGIMES,
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
from agents.nanami.skills.london_reversal import generate_signal as london_reversal_signal  # noqa: E402
from agents.nanami.skills.kalman_trend import generate_signal as kalman_trend_signal  # noqa: E402
from agents.nanami.skills.intraday_mom import generate_signal as intraday_mom_signal  # noqa: E402
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

    # ── 9. Signal generation — priority: B → C → A → D ──────────────────
    signal = None

    # ── Model B: London reversal — fires in LONDON_OPEN regardless of regime ─
    if session in MODEL_SESSIONS[MODEL_B]:
        signal = await _try_model_b(state, df_m5, session, macro_bias)

    # ── Model C: Kalman trend — all sessions, Hurst > 0.55 ───────────────────
    if signal is None and session in MODEL_SESSIONS[MODEL_C]:
        signal = await _try_model_c(state, df_m5, session, macro_bias)

    # ── Model A: OU grind — GRIND + BULLISH_BLOWOFF, NY sessions ─────────────
    if signal is None and regime in (REGIME_BULLISH_GRIND, REGIME_BEARISH_GRIND, REGIME_BULLISH_BLOWOFF):
        if session in MODEL_SESSIONS[MODEL_A]:
            utc_hour = datetime.now(timezone.utc).hour
            # NY_CLOSE entry cutoff: block after 20:00 UTC — ensures ≥60 min before blackout
            if session == "NY_CLOSE" and utc_hour >= 20:
                logger.debug("NY_CLOSE entry cutoff (20:00 UTC) — skipping Model A signal")
            else:
                signal = await _try_model_a(state, df_m5, session, regime, macro_bias)

    # ── Model D: Intraday momentum — NY_OVERLAP, 14:00–16:00 UTC only ────────
    if signal is None and session in MODEL_SESSIONS[MODEL_D]:
        signal = await _try_model_d(state, df_m5, session, macro_bias)

    if regime in NO_TRADE_REGIMES and signal is None:
        logger.info("Regime=%s — no trading allowed", regime)

    # ── 10. Persist signal ───────────────────────────────────────────────
    if signal:
        signal["timestamp"] = datetime.now(timezone.utc).isoformat()
        signal["status"] = "PENDING"
        await state.set_trading_state("last_signal", json.dumps(signal))
        logger.info(
            "Signal → %s %s | entry=%.2f  SL=%.2f  TP=%.2f | %s",
            signal["model"], signal["direction"],
            signal["entry_price"], signal["sl_price"], signal["tp_price"],
            signal.get("reason", f"score={signal.get('score', '?')}"),
        )
    else:
        logger.debug("No signal this poll — session=%s regime=%s", session, regime)


# ---------------------------------------------------------------------------
# Per-model helpers
# ---------------------------------------------------------------------------

async def _try_model_b(state: StateManager, df_m5, session: str, macro_bias: str = "NEUTRAL"):
    """Check session limit, run Model B (London reversal)."""
    count = await state.get_session_trade_count(session, MODEL_B)
    if count >= MODEL_SESSION_LIMITS[MODEL_B]:
        logger.debug(
            "Model B session limit reached (%d/%d) for %s",
            count, MODEL_SESSION_LIMITS[MODEL_B], session,
        )
        return None
    return london_reversal_signal(df_m5, session, h4_bias=macro_bias)


async def _try_model_c(state: StateManager, df_m5, session: str, h4_bias: str = "NEUTRAL"):
    """Check session limit, run Model C (Kalman trend)."""
    count = await state.get_session_trade_count(session, MODEL_C)
    if count >= MODEL_SESSION_LIMITS[MODEL_C]:
        logger.debug(
            "Model C session limit reached (%d/%d) for %s",
            count, MODEL_SESSION_LIMITS[MODEL_C], session,
        )
        return None
    return kalman_trend_signal(df_m5, session, h4_bias=h4_bias)


async def _try_model_d(state: StateManager, df_m5, session: str, h4_bias: str = "NEUTRAL"):
    """Check daily limit, run Model D (intraday momentum)."""
    count = await state.get_session_trade_count(session, MODEL_D)
    if count >= MODEL_SESSION_LIMITS[MODEL_D]:
        logger.debug("Model D daily limit reached (%d/%d)", count, MODEL_SESSION_LIMITS[MODEL_D])
        return None
    return intraday_mom_signal(df_m5, session, h4_bias=h4_bias)


async def _try_model_a(state: StateManager, df_m5, session: str, regime: str, macro_bias: str = "NEUTRAL"):
    """Check session limit, run Model A (OU grind) with H4 bias gate."""
    count = await state.get_session_trade_count(session, MODEL_A)
    if count >= MODEL_SESSION_LIMITS[MODEL_A]:
        logger.debug(
            "Model A session limit reached (%d/%d) for %s",
            count, MODEL_SESSION_LIMITS[MODEL_A], session,
        )
        return None

    signal = ou_grind.generate_signal(df_m5, session, regime, macro_bias=macro_bias)
    if signal is None:
        return None

    # H4 bias gate: only trade with the macro trend
    if signal["direction"] == "BUY" and macro_bias == "BEARISH":
        logger.debug("Model A BUY blocked — H4 bias is BEARISH")
        return None
    if signal["direction"] == "SELL" and macro_bias == "BULLISH":
        logger.debug("Model A SELL blocked — H4 bias is BULLISH")
        return None

    return signal


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(run())
