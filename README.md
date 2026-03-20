# HONORED — Autonomous XAUUSD Trading System

Fully automated gold trading system running on MetaApi (HFM MT5).
Five async Python agents, zero-LLM, SQLite shared state, Telegram interface.

---

## Agents

| Agent | Role |
|-------|------|
| **GOJO** | Telegram bot — command interface, alert delivery |
| **NANAMI** | Analyst — market data, signal generation (Model A + B) |
| **GETO** | Risk Manager — 10-check signal validator, halt logic |
| **TOJI** | Executor — lot sizing, order placement, trade monitoring |
| **MAHORAGA** | Learning — CUSUM drift detection, statistical analysis, parameter proposals |

---

## Trading Models

### Model A — OU_GRIND
Mean-reversion on M5 detrended residuals using Ornstein-Uhlenbeck process.
- **Sessions:** NY_OVERLAP (12:00–16:00 UTC), NY_CLOSE (19:00–20:00 UTC)
- **Regimes:** BULLISH_GRIND (BUY), BEARISH_GRIND (SELL), BULLISH_BLOWOFF (BUY)
- **Session cap:** 8 trades
- **Backtest:** 62.2% WR, 0.61 trades/day, Sharpe 6.44 (Jan 2025 – Mar 2026)

### Model B — LONDON_REVERSAL
Fakeout/reversal model using Kalman velocity flip + CUSUM + N-bar exhaustion + volume climax.
- **Session:** LONDON_OPEN (08:00–10:00 UTC, entry blocked before 08:00)
- **Session cap:** 2 trades
- **Minimum score:** 3 points (Kalman flip mandatory + at least 1 confirmation)

---

## Risk Rules

| Parameter | Value |
|-----------|-------|
| Risk per trade | 20% of balance |
| RR ratio | 1:2 fixed |
| SL | 1.5 × ATR14, clamped $6–$12 |
| Anti-martingale | lot ÷ 2^consecutive_losses |
| Breakeven | Move SL to entry at +1.5 × ATR profit |
| 3 consecutive losses | Soft halt — `/override` to resume |
| 50% drawdown | Emergency halt — manual flag reset |
| News blackout | 30 min before/after high-impact events |
| Max spread | $4.00 |

---

## Quick Start

```bash
# 1. Install dependencies
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Fill in: META_API_TOKEN, HFM_ACCOUNT_ID, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
#          FINNHUB_API_KEY, PAPER_MODE, ACCOUNT_TYPE, HONORED_DB_PATH

# 3. Initialise database
python scripts/init_db.py --balance 200

# 4. Run all agents (development)
python agents/gojo/agent.py &
python agents/nanami/agent.py &
python agents/geto/agent.py &
python agents/toji/agent.py &
python agents/mahoraga/agent.py &

# 5. Tests
pytest tests/ -v
```

---

## VPS Deployment (supervisord)

```bash
sudo cp deploy/supervisord.conf /etc/supervisor/conf.d/honored.conf
sudo supervisorctl reread && sudo supervisorctl update
sudo supervisorctl status honored:*
```

Logs: `/var/log/honored/*.out.log` and `*.err.log`

---

## Telegram Commands

| Command | Action |
|---------|--------|
| `/status` | System snapshot: session, regime, balance, open trades, news |
| `/pause` | Pause trading (GETO blocks all new signals) |
| `/resume` | Resume trading |
| `/override` | Clear halt + reset consecutive losses |
| `/report [N]` | N-day trade report (default 7) |
| `/proposals` | List pending MAHORAGA parameter proposals |

---

## MAHORAGA — Learning Agent

Runs on a schedule (daily 21:30 GMT, weekly Sunday) and on a micro-trigger (every 5 trades).

**What it does:**
- Computes expectancy, Sharpe, Calmar, profit factor, streak stats per model
- Slices performance by: UTC hour, regime, direction, session, z-score bucket, H4 bias, detrend method
- Runs **CUSUM drift detection** per model — fires `MAHORAGA_DRIFT` alert immediately if win rate degrades significantly
- Generates concrete parameter proposals (z-score thresholds, session limits, dead hours) stored in `param_proposals` table

**What it never does:**
- Auto-apply any parameter change — user reads `/proposals` and updates `core/constants.py` manually

---

## File Structure

```
honored/
├── agents/
│   ├── gojo/agent.py               Telegram bot
│   ├── nanami/agent.py             Analyst (60s loop)
│   │   └── skills/
│   │       ├── market_data.py
│   │       ├── indicator_engine.py
│   │       ├── stat_tests.py
│   │       ├── htf_regime.py
│   │       ├── session_detector.py
│   │       ├── ou_grind.py         Model A
│   │       └── london_reversal.py  Model B
│   ├── geto/agent.py               Risk Manager
│   │   └── skills/
│   │       ├── trade_validator.py  10-check validator
│   │       ├── news_calendar.py
│   │       ├── account_monitor.py
│   │       ├── consecutive_tracker.py
│   │       └── dd_monitor.py
│   ├── toji/agent.py               Executor
│   │   └── skills/
│   │       ├── lot_calculator.py
│   │       ├── order_placer.py
│   │       ├── trade_monitor.py
│   │       ├── trade_logger.py
│   │       └── state_updater.py
│   └── mahoraga/agent.py           Learning (60s poll)
│       └── skills/
│           ├── statistical_engine.py
│           ├── feature_analyzer.py
│           ├── drift_detector.py
│           ├── regime_profiler.py
│           ├── parameter_proposer.py
│           └── adaptation_reporter.py
├── core/
│   ├── constants.py                All tunable parameters
│   ├── state_manager.py            SQLite wrapper (all DB access here)
│   ├── metaapi_client.py
│   └── news_fetcher.py
├── scripts/
│   ├── init_db.py                  Seed DB before first run
│   ├── backtest_per_model.py       Validated backtest Jan 2025–Mar 2026
│   ├── health_check.py
│   └── diagnose_signals.py
└── tests/
    ├── test_comprehensive.py       73 unit tests
    └── test_e2e.py                 41 integration tests (real SQLite)
```

---

## Environment Variables

```bash
META_API_TOKEN=
HFM_ACCOUNT_ID=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
FINNHUB_API_KEY=
PAPER_MODE=true              # false for live
ACCOUNT_TYPE=STANDARD        # STANDARD or CENTS
HONORED_DB_PATH=             # absolute path on VPS
```

---

## Architecture Notes

- All inter-agent communication is via SQLite (WAL mode, no direct calls)
- `honored.db` for live, `paper.db` for paper mode (separate files, same schema)
- TOJI uses lazy MetaApi init to prevent dual-subscription conflict with NANAMI
- MAHORAGA is zero-LLM — pure statistics, deterministic, no API costs
