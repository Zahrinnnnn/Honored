# CLAUDE.md — HONORED Autonomous XAUUSD Trading System
## Current Architecture — authoritative reference, supersedes all prior versions

**Full PRD:** `.claude/HONORED_PRD.md` — for historical detail. This file is the source of truth.

---

## Agent Roster

| Agent | Name | Runtime | Role |
|-------|------|---------|------|
| 1 | **GOJO** | Pure Python async (Telegram bot) | Commander — Telegram I/O, alert delivery, control commands |
| 2 | **NANAMI** | Pure Python async | Analyst — market watching, signal generation |
| 3 | **GETO** | Pure Python async | Risk Manager — validation, halt logic |
| 4 | **TOJI** | Pure Python async | Executor — MetaApi order placement, trade monitoring |
| 5 | **MAHORAGA** | Pure Python scheduled | Learning — statistical analysis, CUSUM drift detection, parameter proposals |

---

## Architecture

```
Telegram ←→ GOJO (agents/gojo/agent.py)
                    ↕ SQLite (honored.db)
      NANAMI ←→ GETO ←→ TOJI ←→ MAHORAGA
      (all Python asyncio processes, all R/W SQLite via WAL mode)
```

- **GOJO** is a pure Telegram bot (`python-telegram-bot[job-queue]`). No LLM. No WhatsApp. No OpenClaw.
- All 5 Python agents share `honored.db` via `core/state_manager.py`.
- **No agent calls another agent directly.** All inter-agent communication is via SQLite reads/writes.

---

## Trading Models

### Model A — `OU_GRIND`
- **Strategy:** OU mean-reversion on M5 detrended residuals (EMA50 primary z=0.8, EMA21 fallback z=1.2)
- **Sessions:** `NY_OVERLAP` (12:00–16:00 GMT) + `NY_CLOSE` (19:00–21:00 GMT — entry cutoff at 20:00 UTC)
- **Regimes:** `BULLISH_GRIND` (BUY), `BEARISH_GRIND` (SELL), `BULLISH_BLOWOFF` (BUY, z=1.0), `BEARISH_PANIC` (SELL, z=1.0, EMA50 only — mirror of BLOWOFF)
- **Session limit:** 8 trades per session
- **Backtest (Jan 2025–Mar 2026, CENTS $5):** 55.6% WR, 1.12 trades/day, Sharpe 2.71, Max DD 84.5%, 0 busts

### Model B — `LONDON_REVERSAL`
- **Strategy:** Kalman velocity flip + CUSUM + N-bar exhaustion + volume climax
- **Sessions:** `LONDON_OPEN` (07:00–10:00 GMT) — entry allowed from 07:00 UTC onward
- **Regimes:** Regime-agnostic; H4 bias filter only (BUY blocked when H4=BEARISH, SELL when H4=BULLISH)
- **Session limit:** 3 trades per session
- **Time kill:** 120 min (reversals need more room than OU models)

> **No Model C or D.** All alternatives tested and reverted — see Historical Decisions Log.

---

## Critical Rules (Never Break)

### Trading Logic
- GOJO **never** touches market data, indicators, or MetaApi
- GETO validation is **pure if/else** — no LLM, no reasoning around it
- All 5 agents are **zero-LLM** — pure Python only
- MAHORAGA **never** auto-applies parameter changes — all require explicit user approval via Telegram
- No cap on simultaneous open trades
- Risk per trade = exactly 13% of current balance (`RISK_PER_TRADE_PCT = 0.13`)
- RR ratio = 1:2 fixed (TP always = SL × 2)
- Anti-martingale lot sizing: `lot / 2^consecutive_losses`, floor 0.01
- Breakeven: move SL to entry when profit ≥ 1.5 × ATR (reduces whipsaw breakevens)

### Model Priority & Sessions
- **Model A:** NY_OVERLAP + NY_CLOSE. NY_CLOSE entry cutoff at 20:00 UTC (ensures ≥60 min before blackout)
- **Model B:** LONDON_OPEN only (proven toxic in other sessions). Entry allowed from 07:00 UTC onward
- **Model A vs Model B:** Mutually exclusive by session — Model A never fires in LONDON_OPEN
- **Concurrent trades:** No position cap — multiple trades can be open simultaneously

### Session Trade Count Tracking
BOTH NANAMI and GETO own trade count:
- **NANAMI:** Reads session counts before generating a signal; suppresses if at limit
- **GETO:** Independently validates the same check
- Source of truth: `session_trades` table in SQLite, reset at start of each session window

### Risk Hard Stops
- 50% drawdown → EMERGENCY HALT (only explicit flag reset can unlock)
- 4 consecutive losses → SOFT HALT (user sends `/override`)
- News blackout: 30 min before/after high-impact events (ForexFactory)
- Max spread: $4.00

### Paper Mode
- `PAPER_MODE=true` (env var)
- TOJI writes to `paper.db` instead of `honored.db`
- All Telegram messages tagged `[PAPER]` in paper mode
- NANAMI, GETO, MAHORAGA read from `paper.db` when in paper mode

---

## Shared State: SQLite

Use SQLite (`honored.db` / `paper.db`) exclusively. All reads/writes go through `core/state_manager.py`.

### Tables

```sql
CREATE TABLE system_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- Keys: status, pause_flag, halt_flag, emergency_halt_flag

CREATE TABLE account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    balance REAL, equity REAL, peak_balance REAL,
    current_dd_pct REAL, open_positions INTEGER, updated_at TEXT
);

CREATE TABLE trading_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- Keys: consecutive_losses, total_trades_today, last_trade_result,
--       last_trade_timestamp, last_signal, last_risk_decision

CREATE TABLE session_trades (
    session TEXT NOT NULL, model TEXT NOT NULL, date TEXT NOT NULL,
    count INTEGER DEFAULT 0,
    PRIMARY KEY (session, model, date)
);

CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT, time TEXT, model TEXT, direction TEXT,
    entry_price REAL, sl_price REAL, tp_price REAL,
    lot_size REAL, sl_distance REAL, risk_amount REAL,
    result TEXT, exit_price REAL, exit_reason TEXT,
    pnl REAL, balance_before REAL, balance_after REAL,
    drawdown_pct REAL, duration_mins REAL, reason TEXT,
    atr_at_entry REAL, paper INTEGER DEFAULT 0, created_at TEXT,
    -- Entry context (written by TOJI at open, read by MAHORAGA for analysis)
    regime_at_entry TEXT, h4_bias_at_entry TEXT,
    hurst_at_entry REAL, zscore_at_entry REAL, detrend_method TEXT,
    spread_at_entry REAL, mins_to_news_at_entry REAL, entry_hour_utc INTEGER
);

CREATE TABLE param_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT UNIQUE NOT NULL,
    model TEXT NOT NULL, parameter TEXT NOT NULL,
    current_value TEXT NOT NULL, proposed_value TEXT NOT NULL,
    rationale TEXT NOT NULL, confidence INTEGER DEFAULT 0,
    expected_impact TEXT NOT NULL, priority TEXT DEFAULT 'MEDIUM',
    status TEXT DEFAULT 'PENDING',
    created_at TEXT NOT NULL, resolved_at TEXT
);

CREATE TABLE alert_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_type TEXT NOT NULL, message TEXT NOT NULL,
    sent INTEGER DEFAULT 0, created_at TEXT NOT NULL
);

CREATE TABLE mahoraga_state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
CREATE TABLE session_info   (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
-- session_info keys: current_session, current_regime, macro_bias,
--                    minutes_to_next_news, structural_break_until,
--                    current_spread, current_bid, current_ask,
--                    asian_range_high, asian_range_low
```

### State Access Rules
```
NANAMI:   READ session_info, system_state, session_trades
          WRITE trading_state.last_signal, session_trades, session_info
GETO:     READ all
          WRITE system_state (halt_flags), trading_state.last_risk_decision, alert_queue
TOJI:     READ trading_state.last_risk_decision
          WRITE account, trading_state (post-trade), trades, session_trades
GOJO:     READ all
          WRITE system_state (pause_flag, halt_flag), alert_queue.sent
MAHORAGA: READ trades, account
          WRITE mahoraga_state, alert_queue
```

---

## GETO Validation Checks (ALL 10 must pass)

```python
checks = {
    "session_valid":               current_session in ALLOWED_SESSIONS,
    "regime_and_bias_ok":          regime_and_bias_allows(model, direction, regime, h4_bias),
    "session_trades_within_limit": session_trade_count(model) < limit,  # A:8, B:3
    "consecutive_losses_ok":       consecutive_losses < 4,
    "drawdown_ok":                 current_dd_pct < 50.0,
    "news_clear":                  minutes_to_next_news > 30,
    "spread_acceptable":           current_spread < 4.00,
    "not_paused":                  pause_flag == False,
    "not_halted":                  halt_flag == False and emergency_halt_flag == False,
    "structural_break_clear":      structural_break_until expired or empty,
}
```

`regime_and_bias_ok` rules:
- Model A BUY → `BULLISH_GRIND` or `BULLISH_BLOWOFF` + H4 ≠ BEARISH
- Model A SELL → `BEARISH_GRIND` or `BEARISH_PANIC` + H4 ≠ BULLISH
- Model B BUY → any regime + H4 ≠ BEARISH
- Model B SELL → any regime + H4 ≠ BULLISH

`structural_break_clear` blocks trading during 4h cooldown after single H1 candle > 3×ATR14.

---

## News Calendar: ForexFactory

Use ForexFactory free XML feed (`https://nfs.faireconomy.media/ff_calendar_thisweek.xml`). No API key required.
- Filter by `impact = "high"` — high-impact only (NFP, CPI, FOMC, etc.)
- Times in US Eastern → converted to UTC internally
- Cache refreshes every 4 hours (intraday updates)
- Feed unreachable → `is_news_clear()` returns False (fail-safe, blocks all trades)

---

## GOJO — Telegram Bot

GOJO is a pure Python Telegram bot. No LLM. No OpenClaw. No WhatsApp.

### Commands
| Command | Action |
|---------|--------|
| `/status` | System snapshot: session, regime, bias, gold price, balance, open trades, news |
| `/pause` | Set `pause_flag = true` |
| `/resume` | Set `pause_flag = false` |
| `/override` | Clear `halt_flag` + reset `consecutive_losses` to 0 |
| `/report [N]` | N-day trade report (default 7 days) |
| `/proposals` | List pending parameter proposals from MAHORAGA |

### Alert Delivery
- Background job polls `alert_queue` every 60s
- Sends any unsent rows to Telegram, then marks as sent
- Alert types: `TRADE_OPENED`, `TRADE_CLOSED`, `SOFT_HALT`, `EMERGENCY_HALT`, `MAHORAGA_REPORT`, `MAHORAGA_DRIFT`

### Security
- `_guard(update)` — only responds to `TELEGRAM_CHAT_ID`; ignores all other senders

---

## File Structure

```
honored/
├── .claude/
│   ├── CLAUDE.md               ← this file
│   └── HONORED_PRD.md
├── .env                        ← never commit
├── requirements.txt
├── honored.db                  ← SQLite live state (auto-generated)
├── paper.db                    ← SQLite paper mode state (auto-generated)
│
├── agents/
│   ├── gojo/
│   │   └── agent.py            ← Telegram bot (Commander)
│   ├── nanami/
│   │   ├── agent.py
│   │   └── skills/
│   │       ├── market_data.py
│   │       ├── indicator_engine.py
│   │       ├── stat_tests.py
│   │       ├── htf_regime.py
│   │       ├── session_detector.py
│   │       ├── ou_grind.py         ← Model A
│   │       └── london_reversal.py  ← Model B
│   ├── geto/
│   │   ├── agent.py
│   │   └── skills/
│   │       ├── account_monitor.py
│   │       ├── trade_validator.py
│   │       ├── news_calendar.py
│   │       ├── consecutive_tracker.py
│   │       └── dd_monitor.py
│   ├── toji/
│   │   ├── agent.py
│   │   └── skills/
│   │       ├── lot_calculator.py
│   │       ├── order_placer.py
│   │       ├── trade_monitor.py
│   │       ├── trade_logger.py
│   │       └── state_updater.py
│   └── mahoraga/
│       ├── agent.py
│       └── skills/
│           ├── statistical_engine.py   ← expectancy, Sharpe, Calmar, streaks
│           ├── feature_analyzer.py     ← hour heatmap, regime×dir matrix, z-score buckets
│           ├── drift_detector.py       ← CUSUM per model, immediate alert logic
│           ├── regime_profiler.py      ← WR matrix by model×regime×session
│           ├── parameter_proposer.py   ← concrete proposals (no auto-apply)
│           └── adaptation_reporter.py  ← compile_report(), Telegram digest
│
├── core/
│   ├── constants.py
│   ├── state_manager.py        ← SQLite wrapper, all DB access goes here
│   ├── metaapi_client.py
│   └── news_fetcher.py
│
├── scripts/
│   ├── init_db.py              ← seed DB before first run
│   ├── backtest_per_model.py   ← Jan 2025–Mar 2026 validated backtest
│   ├── health_check.py
│   └── diagnose_signals.py
│
└── tests/
    ├── test_comprehensive.py   ← 73 unit tests (lot calc, monitor, validator, regimes, signals)
    └── test_e2e.py             ← 41 integration tests (real SQLite, full state machine)
```

---

## Key Constants (`core/constants.py`)

```python
# Risk
RISK_PER_TRADE_PCT      = 0.13       # 13% of balance per trade
MAX_DRAWDOWN_PCT        = 0.50
MAX_CONSECUTIVE_LOSSES  = 4
NEWS_BLACKOUT_MINUTES   = 30
MAX_SPREAD_DOLLARS      = 4.0

# Sessions (GMT)
SESSIONS = {
    "LONDON_OPEN":     ("07:00", "10:00"),
    "NY_OVERLAP":      ("12:00", "16:00"),
    "NY_CLOSE":        ("19:00", "21:00"),
}

# 6-State Regime (H1)
REGIME_Z_SCORE_WINDOW            = 50
REGIME_Z_SCORE_THRESHOLD         = 1.0
REGIME_ATR_PERCENTILE_THRESHOLD  = 75
REGIME_H1_BARS_NEEDED            = 200

# Structural Break Override
STRUCTURAL_BREAK_ATR_MULT       = 3.0    # single H1 candle > 3×ATR → halt
STRUCTURAL_BREAK_COOLDOWN_HOURS = 4

# OU Model Parameters (Model A)
OU_ZSCORE_GRIND_THRESHOLD   = 0.8    # EMA50 primary detrend
OU_ZSCORE_ENTRY_THRESHOLD   = 1.2    # EMA21 fallback detrend
OU_ZSCORE_BLOWOFF_THRESHOLD = 1.0    # blowoff mode (stricter)
OU_MIN_HALF_LIFE            = 3
OU_MAX_HALF_LIFE            = 50
OU_LOOKBACK                 = 80     # EMA50 window
OU_LOOKBACK_SHORT           = 40     # EMA21 window
OU_SL_ATR_MULT              = 1.5
OU_SL_MIN                   = 6.0   # $6 floor
OU_SL_MAX                   = 12.0  # $12 cap

# Exit System
RR_RATIO                    = 2.0
BREAKEVEN_ATR_THRESHOLD     = 1.5   # move SL to entry at +1.5 ATR profit
OU_TIME_KILL_HALF_LIFE_MULT = 3     # OU time kill = 3 × half_life × 5 min
MODEL_A_TIME_KILL_MINUTES   = 60    # Model A fallback (no half_life)
MODEL_B_TIME_KILL_MINUTES   = 120   # Model B London reversal
MAX_TRADE_DURATION_MINUTES  = 240   # 4h hard cap

# Model names + sessions
MODEL_A = "OU_GRIND"
MODEL_B = "LONDON_REVERSAL"

MODEL_SESSION_LIMITS = {
    MODEL_A: 8,   # per session
    MODEL_B: 3,   # per session
}

MODEL_SESSIONS = {
    MODEL_A: ["NY_OVERLAP", "NY_CLOSE"],  # NY_CLOSE entry cutoff at 20:00 UTC
    MODEL_B: ["LONDON_OPEN"],             # entry allowed from 07:00 UTC onward
}
```

---

## Lot Calculation Formula

```python
def calculate_lot(balance: float, sl_distance: float,
                  risk_pct: float = RISK_PER_TRADE_PCT,
                  consecutive_losses: int = 0) -> float:
    """balance in USD. sl_distance in USD."""
    if balance <= 0 or sl_distance <= 0:
        raise ValueError("balance and sl_distance must be > 0")
    risk_amount = balance * risk_pct
    lot = round(risk_amount / (sl_distance * XAUUSD_POINT_VALUE), 2)
    if consecutive_losses > 0:
        lot = round(lot / (2 ** consecutive_losses), 2)
    return max(lot, 0.01)
```

`XAUUSD_POINT_VALUE = 100` for both STANDARD and CENTS accounts (MetaApi returns CENTS balance in USC; 1 cent-lot = 100 USC per $1 gold move).

---

## Environment Variables (`.env`)

```bash
# MetaApi
META_API_TOKEN=
HFM_ACCOUNT_ID=

# Telegram (GOJO)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# News
# ForexFactory XML feed — no API key required

# Trading mode
PAPER_MODE=true
ACCOUNT_TYPE=STANDARD   # STANDARD or CENTS — affects XAUUSD_POINT_VALUE

# DB path (required on VPS — absolute path)
HONORED_DB_PATH=/opt/honored/honored.db
```

---

## Dependencies

### Python (`requirements.txt`)
```
metaapi-cloud-sdk>=14.0.0
python-dotenv>=1.0.0
pandas>=2.0.0
numpy>=1.24.0
ta>=0.10.0
aiohttp>=3.8.0
aiosqlite>=0.19.0
requests>=2.28.0
python-telegram-bot[job-queue]>=20.0
statsmodels>=0.14.0
```

---

## VPS Deployment

### Current Setup
- **VPS:** Hetzner CX23, Helsinki — `89.167.122.162`
- **OS:** Ubuntu 22.04 LTS
- **DB:** `/opt/honored/honored.db`
- **All 5 agents managed by supervisord**

### supervisord config (`deploy/supervisord.conf`)

```ini
[program:nanami]
command=/opt/honored/.venv/bin/python /opt/honored/agents/nanami/agent.py
directory=/opt/honored
environment=PYTHONPATH="/opt/honored"
autostart=true
autorestart=true
startsecs=5
startretries=3
stderr_logfile=/var/log/honored/nanami.err.log
stdout_logfile=/var/log/honored/nanami.out.log

[program:geto]
command=/opt/honored/.venv/bin/python /opt/honored/agents/geto/agent.py
directory=/opt/honored
environment=PYTHONPATH="/opt/honored"
autostart=true
autorestart=true
startsecs=5
startretries=3
stderr_logfile=/var/log/honored/geto.err.log
stdout_logfile=/var/log/honored/geto.out.log

[program:toji]
command=/opt/honored/.venv/bin/python /opt/honored/agents/toji/agent.py
directory=/opt/honored
environment=PYTHONPATH="/opt/honored"
autostart=true
autorestart=true
startsecs=5
startretries=3
stderr_logfile=/var/log/honored/toji.err.log
stdout_logfile=/var/log/honored/toji.out.log

[program:mahoraga]
command=/opt/honored/.venv/bin/python /opt/honored/agents/mahoraga/agent.py
directory=/opt/honored
environment=PYTHONPATH="/opt/honored"
autostart=true
autorestart=true
startsecs=5
startretries=3
stderr_logfile=/var/log/honored/mahoraga.err.log
stdout_logfile=/var/log/honored/mahoraga.out.log

[program:gojo]
command=/opt/honored/.venv/bin/python /opt/honored/agents/gojo/agent.py
directory=/opt/honored
environment=PYTHONPATH="/opt/honored",PYTHONUNBUFFERED="1"
autostart=true
autorestart=true
startsecs=5
startretries=3
stderr_logfile=/var/log/honored/gojo.err.log
stdout_logfile=/var/log/honored/gojo.out.log

[group:honored]
programs=nanami,geto,toji,mahoraga,gojo
```

### Step-by-Step Deploy (fresh VPS)

```bash
# 1. Install Python 3.11+
sudo apt-get install -y python3.11 python3.11-venv python3-pip

# 2. Clone and set up
git clone <your-repo> /opt/honored
cd /opt/honored
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Configure .env
cp .env.example .env
nano .env   # fill in all keys; set HONORED_DB_PATH=/opt/honored/honored.db

# 4. Create log directory
sudo mkdir -p /var/log/honored

# 5. Initialize DB
python scripts/init_db.py --balance 200

# 6. Install and configure supervisord
sudo apt-get install -y supervisor
sudo cp deploy/supervisord.conf /etc/supervisor/conf.d/honored.conf
sudo supervisorctl reread && sudo supervisorctl update

# 7. Verify
sudo supervisorctl status honored:*
```

### Useful Commands

```bash
sudo supervisorctl status honored:*
sudo supervisorctl restart honored:nanami
sudo supervisorctl restart honored:*
tail -f /var/log/honored/nanami.out.log
tail -f /var/log/honored/gojo.err.log

# Switch paper → live
nano /opt/honored/.env   # PAPER_MODE=false
sudo supervisorctl restart honored:*
```

---

## Build Status

```
PHASE 1 ✅ COMPLETE   Foundation (core/, DB schema, supervisord)
PHASE 2 ✅ COMPLETE   NANAMI — Analyst (Model A OU_GRIND + Model B LONDON_REVERSAL)
PHASE 3 ✅ COMPLETE   GETO — Risk Manager (10-check validator)
PHASE 4 ✅ COMPLETE   TOJI — Executor (lot calc, order placement, monitoring)
PHASE 5 ✅ COMPLETE   GOJO — Commander (Telegram bot, alert delivery)
PHASE 6 ✅ COMPLETE   MAHORAGA — Learning (CUSUM drift, statistical engine, proposals)
PHASE 7 ✅ COMPLETE   Integration & Tests (114 tests: 73 unit + 41 e2e)
PHASE 8 ⬜ PENDING    Go Live
```

### Phase 8 Checklist
```
[ ] Set PAPER_MODE=false in .env on VPS
[ ] Confirm honored.db initialized with correct live balance
[ ] Monitor first 10 live trades manually via Telegram
[ ] Verify Telegram alerts firing for each TRADE_OPENED / TRADE_CLOSED
[ ] Confirm halt/override flow working via /override
```

---

## Development Guidelines

- All Python agent code is async (`asyncio`) throughout
- Never hardcode credentials — read from `.env` via `python-dotenv`
- Never commit `.env`, `honored.db`, or `paper.db`
- All SQLite access goes through `core/state_manager.py` — never raw `sqlite3` in agent code
- All MetaApi access goes through `core/metaapi_client.py`
- News calendar calls go through `core/news_fetcher.py` — ForexFactory XML feed (no API key)
- Every skill file must be independently testable — no circular imports
- Indicator calculations use the `ta` library; implement manually only if `ta` lacks it
- Tests use real temp SQLite (no mocks for DB) — never hit real MetaApi in tests
- Run tests: `pytest tests/test_comprehensive.py tests/test_e2e.py -v`
- Run linter: `python -m ruff check . --statistics`

---

## Historical Decisions Log

| Decision | Reason |
|----------|--------|
| Replaced OpenClaw + WhatsApp with Telegram bot | OpenClaw + LLM was unreliable, slow, expensive. Telegram bot is ~250 lines, zero latency, no hallucinations |
| MODEL_A added NY_CLOSE session | Raised daily trades 0.42 → 0.61/day; WR improved 61.8% → 62.2% |
| NY_CLOSE entry cutoff at 20:00 UTC | Without it, trades opened at 20:30 hit the 4h max-duration cap (26 exits vs 9 baseline) |
| Model B renamed `OU_LONDON` → `LONDON_REVERSAL` | Implementation is Kalman+CUSUM+N-bar, not OU; name now matches reality |
| Model C (ASIAN_BREAKOUT) reverted | 27% WR, -$190 P&L in backtest; every optimisation attempt made it worse |
| KALMAN_FEEDER model rejected | TP set at Model A entry zone (z=±0.9); OU force directly opposes the trade there; 20–27% WR |
| Model C (KALMAN_TREND) built and reverted | Two configs tested: sustained velocity (32% WR) and flip-initiation (33% WR). M5 gold Hurst=0.37 (structurally mean-reverting) — Kalman velocity is anti-edge at M5 in any trend-following form |
| Model D (INTRADAY_MOM) built and reverted | 45% WR at 1:2 RR = positive fixed-lot EV, but at 20% compounding the 55% loss rate creates catastrophic drawdowns during high-balance periods (-$36k on $51k account). Not viable with current risk settings |
| TIGHT_RANGE regime tested for Model A | 29% WR on 278 trades — anti-edge. No macro anchor means residuals don't mean-revert reliably. 111 TIME_KILL exits (price drifts sideways without reaching TP or SL) |
| LONDON_OPEN tested for Model A | 29% WR on 210 trades — European session has different flow structure; OU mean-reversion doesn't hold. LONDON_OPEN left exclusively to Model B |
| `OU_ZSCORE_GRIND_THRESHOLD = 0.8` | Middle ground between 0.9 (62% WR, 0.69/day) and 0.7 (53% WR, 1.35/day). At 0.8: 55.6% WR, 1.12/day — hits 1/day target with acceptable quality |
| `MAX_CONSECUTIVE_LOSSES = 4` | Raised from 3 — at 55% WR the soft halt fires too frequently at 3, blocking valid setups |
| `LONDON_REVERSAL session limit = 3` | Raised from 2 — allowed entry from 07:00 UTC (was 08:00), more setups available |
| `XAUUSD_POINT_VALUE = 100` for CENTS | MetaApi returns CENTS balance in USC. 1 cent-lot XAUUSD = 100 USC per $1 gold move — same formula as STANDARD |
| BEARISH_PANIC added to Model A (SELL only) | Mirror of BULLISH_BLOWOFF. 5/5 wins in backtest, EMA50 only, z=1.0, parabola gate, tighter half-life cap. Highly selective but adds genuine edge during freefall regimes |
| `RISK_PER_TRADE_PCT = 0.13` | Backtested optimal — 13% balances compounding vs bust risk on CENTS $5 account. Tested 5/10/13/14/15/23% — 13% had highest final balance with 0 busts |
| `BREAKEVEN_ATR_THRESHOLD = 1.5` | Raised from 1.0 to reduce whipsaw breakeven exits |
| `OU_TIME_KILL_HALF_LIFE_MULT = 3` | Extended from 2 — gives OU process more time to mean-revert |
| TOJI lazy MetaApi init (`connection = None` at startup) | Prevents dual-subscription conflict with NANAMI |
| Dead zone (14:00–15:00 UTC) removed | Backtest confirmed no statistically significant edge degradation in that window |
| MAHORAGA rebuilt from scratch (no LLM) | LLM adds no value to quantitative analysis; pure statistics are faster, deterministic, and cheaper. CUSUM drift detection fires immediately rather than waiting for a daily report. |
| Entry context stored at trade open | 8 fields (regime, H4 bias, Hurst, z-score, detrend method, spread, minutes-to-news, UTC hour) enable MAHORAGA to slice performance by every relevant dimension without post-hoc data mining. |
