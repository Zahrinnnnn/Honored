"""
agent.py — GOJO (Commander)

Telegram bot. No LLM. Pure command routing + alert delivery.

Commands:
  /status   — system snapshot (session, regime, balance, open trades, news)
  /pause    — pause trading
  /resume   — resume trading
  /override — clear halt + reset consecutive losses
  /report   — performance report (default 7 days; /report 30 for 30 days)

Alert polling: every 60s, sends pending alerts from alert_queue to Telegram.
"""

import logging
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
load_dotenv()

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from core.state_manager import StateManager

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [GOJO] %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("gojo")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID        = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
ALERT_INTERVAL = 60   # seconds between alert_queue polls


def _guard(update: Update) -> bool:
    """Only respond to the configured chat ID."""
    return update.effective_chat.id == CHAT_ID


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _guard(update):
        return

    async with StateManager() as s:
        session     = await s.get_session_info("current_session") or "—"
        regime      = await s.get_session_info("current_regime")  or "—"
        bias        = await s.get_session_info("macro_bias")       or "NEUTRAL"
        bid         = await s.get_session_info("current_bid")      or "—"
        ask         = await s.get_session_info("current_ask")      or "—"
        spread      = await s.get_session_info("current_spread")   or "—"
        news        = await s.get_session_info("minutes_to_next_news") or "—"
        cons_l      = await s.get_consecutive_losses()
        last_dec    = await s.get_trading_state("last_risk_decision") or "—"
        paused      = await s.get_system_flag("pause_flag")
        halted      = await s.get_system_flag("halt_flag")
        emhalt      = await s.get_system_flag("emergency_halt_flag")
        account     = await s.get_account()
        open_trades = await s.get_open_trades()

    bal = f"${account.get('balance', 0):.2f}"
    dd  = f"{account.get('current_dd_pct', 0):.1f}%"

    flags = []
    if paused: flags.append("PAUSED")
    if halted: flags.append("HALTED")
    if emhalt: flags.append("EMERGENCY HALT")
    flag_str = " | ".join(flags) if flags else "OK"

    lines = [
        "*HONORED STATUS*",
        f"Session: `{session}` | Regime: `{regime}` | Bias: `{bias}`",
        f"Gold: bid={bid}  ask={ask}  spread={spread}",
        f"Balance: {bal}  |  DD: {dd}",
        f"Loss streak: {cons_l}  |  News in: {news} min",
        f"System: `{flag_str}`",
        f"Last decision: `{last_dec}`",
    ]

    if open_trades:
        lines.append(f"\nOpen trades ({len(open_trades)}):")
        for t in open_trades:
            lines.append(
                f"  {t['model']} {t['direction']}  "
                f"entry={t['entry_price']}  sl={t['sl_price']}  "
                f"tp={t['tp_price']}  lot={t['lot_size']}"
            )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _guard(update):
        return
    async with StateManager() as s:
        await s.set_system_flag("pause_flag", True)
    await update.message.reply_text("Trading paused.")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _guard(update):
        return
    async with StateManager() as s:
        await s.set_system_flag("pause_flag", False)
    await update.message.reply_text("Trading resumed.")


async def cmd_override(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _guard(update):
        return
    async with StateManager() as s:
        await s.set_system_flag("halt_flag", False)
        await s.reset_consecutive_losses()
    await update.message.reply_text("Halt cleared. Consecutive losses reset to 0.")


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _guard(update):
        return

    days = 7
    if ctx.args:
        try:
            days = int(ctx.args[0])
        except ValueError:
            pass

    async with StateManager() as s:
        trades = await s.get_trades_by_period(days)

    if not trades:
        await update.message.reply_text(f"No trades in the last {days} days.")
        return

    wins   = sum(1 for t in trades if t["result"] == "WIN")
    losses = sum(1 for t in trades if t["result"] == "LOSS")
    bes    = sum(1 for t in trades if t["result"] == "BREAKEVEN")
    dec    = wins + losses
    wr     = wins / dec if dec > 0 else 0.0
    pnl    = sum(t["pnl"] or 0.0 for t in trades)

    by_model: dict = {}
    for t in trades:
        m = t["model"] or "UNKNOWN"
        by_model.setdefault(m, {"w": 0, "l": 0, "pnl": 0.0})
        if t["result"] == "WIN":
            by_model[m]["w"] += 1
        elif t["result"] == "LOSS":
            by_model[m]["l"] += 1
        by_model[m]["pnl"] += t["pnl"] or 0.0

    model_lines = []
    for m, st in by_model.items():
        d   = st["w"] + st["l"]
        mwr = st["w"] / d if d > 0 else 0.0
        model_lines.append(
            f"  {m}: {st['w']}W/{st['l']}L  {mwr:.0%} WR  ${st['pnl']:+.2f}"
        )

    lines = [
        f"*{days}-DAY REPORT*",
        f"Trades: {len(trades)}  ({wins}W / {losses}L / {bes}BE)",
        f"Win rate: {wr:.1%}",
        f"Net P&L: ${pnl:+.2f}",
        "",
        "By model:",
        *model_lines,
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Alert poller (background job)
# ---------------------------------------------------------------------------

async def _poll_alerts(ctx: ContextTypes.DEFAULT_TYPE):
    """Send pending alert_queue rows to Telegram, then mark as sent."""
    try:
        async with StateManager() as s:
            alerts = await s.get_pending_alerts()
            for alert in alerts:
                text = f"*{alert['alert_type']}*\n{alert['message']}"
                await ctx.bot.send_message(CHAT_ID, text, parse_mode="Markdown")
                await s.mark_alert_sent(alert["id"])
    except Exception:
        logger.exception("Alert poll failed")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set — exiting")
        sys.exit(1)
    if not CHAT_ID:
        logger.error("TELEGRAM_CHAT_ID not set — exiting")
        sys.exit(1)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("pause",    cmd_pause))
    app.add_handler(CommandHandler("resume",   cmd_resume))
    app.add_handler(CommandHandler("override", cmd_override))
    app.add_handler(CommandHandler("report",   cmd_report))

    app.job_queue.run_repeating(_poll_alerts, interval=ALERT_INTERVAL, first=10)

    logger.info("GOJO (Telegram) online")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
