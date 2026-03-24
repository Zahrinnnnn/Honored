# HONORED — Autonomous XAUUSD Trading System

Fully automated gold trading system running on MetaApi (HFM MT5).
Five async Python agents, zero-LLM, SQLite shared state, Telegram interface.

---

## Agents

| Agent | Role |
|-------|------|
| **GOJO** | Telegram bot — command interface, alert delivery |
| **NANAMI** | Analyst — market data, signal generation (Model A + C) |
| **GETO** | Risk Manager — 11-check signal validator, halt logic |
| **TOJI** | Executor — lot sizing, order placement, trade monitoring |
| **MAHORAGA** | Learning — CUSUM drift detection, statistical analysis, parameter proposals |

---

## Trading Models

### Model A — OU_GRIND (Active)
Mean-reversion on M5 detrended residuals using Ornstein-Uhlenbeck process.
- **Sessions:** NY_OVERLAP (12:00–16:00 UTC), NY_CLOSE (19:00–21:00 UTC, entry cutoff 20:00 UTC)
- **Regimes:** BULLISH_GRIND (BUY), BEARISH_GRIND (SELL), BULLISH_BLOWOFF (BUY), BEARISH_PANIC (SELL)
- **Session cap:** 15 trades
- **Time kill:** Dynamic — 3 × half_life × 5 min (fallback 60 min)
- **Risk:** 15% of balance per trade

### Model B — LONDON_REVERSAL (Disabled)
Kalman velocity flip + CUSUM + N-bar exhaustion + volume climax. Disabled — net -$21k drag in 2025 backtest due to anti-martingale cross-contamination at peak balance.

### Model C — LONDON_TREND (Active)
Asian range breakout + Kalman velocity continuation. London Phase 1 institutional flow.
- **Session:** LONDON_OPEN (07:00–09:00 UTC entry window only)
- **Regimes:** BULLISH_GRIND (BUY), BEARISH_GRIND (SELL) only — no BLOWOFF/PANIC
- **Session cap:** 4 trades
- **Time kill:** Fixed 150 min
- **Risk:** 5% of balance per trade (isolated counter — Model C losses don't affect Model A lot sizing)
- **SL:** max(asian_range / 3, 1.5 × ATR), clamped $5–$20

**Backtest results (Model A + C, 15% + 5% risk, $500 start):**
- 2025: $500 → $157,342 | 58.3% WR | 0.79 trades/day | Sharpe 1.72 | 1 bust (March)
- 2026 (Jan–Mar): $500 → $4,953 | 67.9% WR | 0.62 trades/day | Sharpe 2.76 | 0 busts

---

## Risk Rules

| Parameter | Value |
|-----------|-------|
| Risk per trade (Model A) | 15% of balance |
| Risk per trade (Model C) | 5% of balance (isolated) |
| RR ratio | 1:2 fixed |
| Model A SL | 1.5 × ATR14, clamped $6–$12 |
| Model C SL | max(asian_range/3, 1.5×ATR), clamped $5–$20 |
| Anti-martingale | lot ÷ 2^consecutive_losses (per model, isolated) |
| Breakeven | Move SL to entry at +1.5 × ATR profit |
| Max simultaneous trades | 10 (across all models) |
| 4 consecutive losses | Soft halt — `/override` to resume |
| 50% drawdown | Emergency halt — manual flag reset |
| News blackout | 30 min before/after high-impact events |
| Max spread | $4.00 |

---

## GETO Validation — 11 Checks

All 11 must pass before any trade is placed:

1. `session_valid` — active trading session
2. `regime_and_bias_ok` — regime + H4 bias allows direction
3. `session_trades_within_limit` — count < model session cap
4. `consecutive_losses_ok` — streak < 4
5. `drawdown_ok` — DD < 50%
6. `news_clear` — > 30 min to next high-impact event
7. `spread_acceptable` — spread < $4.00
8. `not_paused` — pause flag is False
9. `not_halted` — halt + emergency halt both False
10. `structural_break_clear` — no active 4h cooldown
11. `position_cap_ok` — open trades < 10

---

## Quick Start

```bash
# 1. Install dependencies
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Fill in: META_API_TOKEN, HFM_ACCOUNT_ID, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
#          PAPER_MODE, ACCOUNT_TYPE, HONORED_DB_PATH

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
- Runs **CUSUM drift detection** per model — fires `MAHORAGA_DRIFT` alert immediately if win rate degrades
- Generates concrete parameter proposals stored in `param_proposals` table

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
│   │       ├── london_reversal.py  Model B (disabled)
│   │       └── london_trend.py     Model C
│   ├── geto/agent.py               Risk Manager
│   │   └── skills/
│   │       ├── trade_validator.py  11-check validator
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
│   └── mahoraga/agent.py           Learning (scheduled)
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

## Key Constants

```python
RISK_PER_TRADE_PCT          = 0.15   # Model A — 15% per trade
MODEL_C_RISK_PCT            = 0.05   # Model C — 5% per trade (isolated)
MAX_SIMULTANEOUS_TRADES     = 10     # hard cap across all models
MAX_CONSECUTIVE_LOSSES      = 4      # soft halt trigger
M5_MAX_TRADES_PER_SESSION   = 15     # Model A session cap
LONDON_TREND_MAX_TRADES_PER_SESSION = 4   # Model C session cap
OU_ZSCORE_GRIND_THRESHOLD   = 0.9   # EMA50 primary z-score
OU_ZSCORE_ENTRY_THRESHOLD   = 1.3   # EMA21 fallback z-score
LONDON_TREND_TIME_KILL_MINUTES = 150
MAX_TRADE_DURATION_MINUTES  = 240
```

---

## Environment Variables

```bash
META_API_TOKEN=
HFM_ACCOUNT_ID=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
PAPER_MODE=true              # false for live
ACCOUNT_TYPE=STANDARD        # STANDARD or CENTS
HONORED_DB_PATH=             # absolute path on VPS
# News: ForexFactory XML feed — no API key required
```

---

## Architecture Notes

- All inter-agent communication is via SQLite (WAL mode, no direct calls)
- `honored.db` for live, `paper.db` for paper mode (separate files, same schema)
- TOJI uses lazy MetaApi init to prevent dual-subscription conflict with NANAMI
- MAHORAGA is zero-LLM — pure statistics, deterministic, no API costs
- Model C uses isolated consecutive_losses counter — losses don't shrink Model A lots
- Simultaneous trades build up within a session (NANAMI polls every 60s, 1 signal per tick)
