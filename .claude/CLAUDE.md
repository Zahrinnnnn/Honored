# CLAUDE.md — HONORED Autonomous XAUUSD Trading System
## Refined Architecture Document — supersedes PRD where they conflict

**Full PRD:** `.claude/HONORED_PRD.md` — read for deep detail. This file holds all corrections.

---

## Agent Roster

| Agent | Name | Runtime | Role |
|-------|------|---------|------|
| 1 | **GOJO** | OpenClaw (Node.js) | Commander — WhatsApp I/O, orchestration, JARVIS personality |
| 2 | **NANAMI** | Pure Python async | Analyst — market watching, signal generation |
| 3 | **GETO** | Pure Python async | Risk Manager — validation, halt logic |
| 4 | **TOJI** | Pure Python async | Executor — MetaApi order placement, logging |
| 5 | **MAHORAGA** | Python + LLM scheduled | Learning — performance analysis, adaptation |

---

## Architecture: How Agents Communicate

```
WhatsApp ←→ OpenClaw (Node.js daemon) ←→ GOJO (SOUL.md + tools)
                                              ↕ SQLite (honored.db)
                          NANAMI ←→ GETO ←→ TOJI ←→ MAHORAGA
                          (all Python async processes, all R/W SQLite)
```

- **OpenClaw** handles all WhatsApp I/O via Baileys. GOJO is defined as an OpenClaw agent via `SOUL.md` + `AGENTS.md` in the OpenClaw workspace.
- **GOJO** reads SQLite for state, calls Python tool scripts via subprocess for actions, monitors the `alert_queue` table via OpenClaw cron and pushes pending alerts to WhatsApp.
- **Python agents** (NANAMI, GETO, TOJI, MAHORAGA) are standalone `asyncio` processes. They communicate exclusively via SQLite — never direct calls between agents.
- **No agent calls another agent directly.** All inter-agent communication is through SQLite reads/writes.

---

## Critical Rules (Never Break)

### Trading Logic
- GOJO **never** touches market data, indicators, or MetaApi
- GETO validation is **pure if/else** — no LLM, no reasoning around it
- NANAMI and TOJI are **zero-LLM** — pure Python only
- MAHORAGA **never** auto-applies parameter changes — all require explicit user approval via WhatsApp
- Max **2 open trades** simultaneously (changed from PRD's 1)
- Risk per trade = exactly 10% of current balance
- RR ratio = 1:3 fixed (TP always = SL × 3)

### Model Priority & Exclusivity
- **07:00–07:30 GMT**: Model C (London Breakout) has exclusive priority — Model A **cannot** fire in this window
- **Model A vs Model B**: Mutually exclusive by regime (A fires only TRENDING ADX>25, B fires only RANGING ADX<20)
- **Concurrent trades**: Can have 2× Model A open OR 2× Model B open — never Model A + B simultaneously (different regime)
- **Model C + Model B**: Possible (regime=any for C) — GETO enforces the 2-trade max

### Session Trade Count Tracking
BOTH NANAMI and GETO own trade count:
- **NANAMI**: Reads session counts from SQLite before generating a signal; suppresses signal if model is at its session limit
- **GETO**: Independently validates the same check as a validation rule (9th check → 10th check)
- Source of truth: `session_trades` table in SQLite, reset at start of each session window

### Risk Hard Stops
- 50% drawdown → EMERGENCY HALT (only user can unlock)
- 3 consecutive losses → SOFT HALT (user unlocks with "override")
- News blackout: 30 min before/after high-impact events
- Max spread: $4.00

### Paper Mode
- `PAPER_MODE=true` (env var, default)
- TOJI writes to `paper.db` instead of `honored.db`
- All WhatsApp messages tagged `[PAPER]` in paper mode
- NANAMI, GETO, MAHORAGA read from `paper.db` when in paper mode

---

## Shared State: SQLite

Use SQLite (`honored.db` / `paper.db`) — NOT `state.json`. All reads/writes go through `core/state_manager.py`.

### Tables

```sql
-- System flags
CREATE TABLE system_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);
-- Keys: status, pause_flag, halt_flag, emergency_halt_flag

-- Account snapshot (updated after every trade + periodically)
CREATE TABLE account (
    id INTEGER PRIMARY KEY,
    balance REAL,
    equity REAL,
    peak_balance REAL,
    current_dd_pct REAL,
    open_positions INTEGER,
    updated_at TEXT
);

-- Trading state
CREATE TABLE trading_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);
-- Keys: consecutive_losses, total_trades_today, last_trade_result,
--       last_trade_timestamp, last_signal, last_risk_decision

-- Session counters (reset each session)
CREATE TABLE session_trades (
    session TEXT,
    model TEXT,
    date TEXT,
    count INTEGER,
    PRIMARY KEY (session, model, date)
);

-- Full trade log
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT, time TEXT, model TEXT, direction TEXT,
    entry_price REAL, sl_price REAL, tp_price REAL,
    lot_size REAL, sl_distance REAL, risk_amount REAL,
    result TEXT, exit_price REAL, pnl REAL,
    balance_before REAL, balance_after REAL,
    drawdown_pct REAL, duration_mins REAL, reason TEXT,
    paper INTEGER DEFAULT 0
);

-- MAHORAGA recommendations + pending alerts for GOJO
CREATE TABLE alert_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_type TEXT,
    message TEXT,
    sent INTEGER DEFAULT 0,
    created_at TEXT
);

-- MAHORAGA state
CREATE TABLE mahoraga_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

-- Session info
CREATE TABLE session_info (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);
-- Keys: current_session, next_news_event, minutes_to_next_news,
--       asian_range_high, asian_range_low
```

### State Access Rules
```
NANAMI:   READ session_info, system_state, session_trades | WRITE trading_state.last_signal, session_trades
GETO:     READ all | WRITE system_state (halt_flags), trading_state.last_risk_decision, alert_queue
TOJI:     READ trading_state.last_risk_decision | WRITE account, trading_state (post-trade), trades, session_trades
GOJO:     READ all | WRITE system_state (pause_flag), alert_queue.sent
MAHORAGA: READ trades, account | WRITE mahoraga_state, alert_queue
```

---

## GETO Validation Checks (ALL 10 must pass)

```python
checks = {
    "session_valid":            current_session in ALLOWED_SESSIONS,
    "model_priority_ok":        not london_breakout_window() or signal.model == "LONDON_BREAKOUT",
    "regime_matches_model":     regime_valid_for_model(signal),
    "session_trades_within_limit": session_trade_count(signal.model) < model_max_trades(signal.model),
    "consecutive_losses":       state["consecutive_losses"] < 3,
    "drawdown_ok":              state["current_dd_pct"] < 50.0,
    "open_trades_ok":           state["open_positions"] < 2,          # max 2 open
    "news_clear":               minutes_to_next_high_impact_news() > 30,
    "spread_acceptable":        current_spread_dollars < 4.0,
    "not_paused":               state["pause_flag"] == False,
    "not_halted":               state["halt_flag"] == False,
}
```

Note: `no_open_trades` from PRD → replaced by `open_trades_ok` (< 2).

---

## News Calendar: Finnhub

Use Finnhub free tier (`https://finnhub.io/api/v1/calendar/economic`).
- Requires a free API key (add as `FINNHUB_API_KEY` in `.env`)
- Filter by `impact = "high"` to get only high-impact events
- Cache the calendar locally for the day; refresh at 00:00 GMT
- Fall back to blocking all trades if API is unreachable (safety default)

---

## OpenClaw / GOJO Setup

OpenClaw is installed globally (Node ≥22 required). GOJO config lives in `gojo/` inside this project and is **copied/symlinked** to `~/.openclaw/workspace/` on first setup.

### Installation
```bash
npm install -g openclaw@latest
openclaw onboard --install-daemon   # registers as system daemon
openclaw channels login --channel whatsapp   # scan QR in WA > Linked Devices
```

### openclaw.json (`~/.openclaw/openclaw.json`)
```json5
{
  agents: {
    defaults: {
      workspace: "~/.openclaw/workspace",
      model: { primary: "openai/deepseek-chat" },   // DeepSeek via OpenAI-compat
      heartbeat: { every: "60s" },                   // poll alert_queue every 60s
      sandbox: { mode: "off" }                       // trusted scripts, no Docker
    }
  },
  channels: {
    whatsapp: {
      dmPolicy: "allowlist",
      allowFrom: ["${USER_WHATSAPP_NUMBER}"],
      sendReadReceipts: false,
      ackReaction: { emoji: "⚡" }
    }
  },
  cron: { enabled: true, maxConcurrentRuns: 1 },
  tools: { profile: "full" },
  env: {
    DEEPSEEK_API_KEY: "${DEEPSEEK_API_KEY}",
    HONORED_DB: "${HONORED_DB_PATH}"
  }
}
```

### GOJO Workspace Files

```
gojo/                          ← copy to ~/.openclaw/workspace/
├── SOUL.md                    ← JARVIS personality
├── AGENTS.md                  ← routing rules + tool conventions
├── IDENTITY.md                ← name, emoji, theme
├── HEARTBEAT.md               ← runs every 60s: polls alert_queue
└── skills/
    └── honored-trading/
        ├── SKILL.md           ← main trading system skill
        └── scripts/           ← Python tools GOJO calls via exec
            ├── get_status.py
            ├── get_report.py
            ├── set_flag.py
            ├── get_signal_reason.py
            └── trigger_mahoraga.py
```

### SOUL.md (JARVIS tone)
GOJO is **JARVIS** — witty, confident, dry humor, never robotic.
- Not: `"Trade opened. XAUUSD BUY. Entry: 2345.50."`
- Yes: `"On it. Just opened a BUY on gold at $2345.50 — tight stop at $2340.50, targeting $2360.50. I'll keep watch."`
- On halt: `"Pulled the brakes. Three losses in a row is my limit — I'm not about to gamble your account away. Say 'override' when you're ready to go again."`

### SKILL.md format (honored-trading skill)
```markdown
---
name: honored-trading
description: Query and control the HONORED XAUUSD trading system — status, reports, flags, signals
metadata: {"openclaw":{"emoji":"📈","requires":{"bins":["python3"],"env":["HONORED_DB"]},"always":true}}
---
## Workflow
### Status
python3 {baseDir}/scripts/get_status.py --json
### Report
python3 {baseDir}/scripts/get_report.py --days 7 --json
### Set flag (pause/resume/override)
python3 {baseDir}/scripts/set_flag.py --flag <pause_flag|halt_flag> --value <true|false> --json
### Signal reason
python3 {baseDir}/scripts/get_signal_reason.py --json
### Trigger MAHORAGA
python3 {baseDir}/scripts/trigger_mahoraga.py --json
## Output contract
All scripts return JSON stdout. On non-zero exit, report stderr verbatim and stop.
```

### HEARTBEAT.md (alert_queue polling)
```markdown
# Heartbeat
- Run: python3 ~/.openclaw/workspace/skills/honored-trading/scripts/get_status.py --alerts-only --json
- If any unsent alerts are returned, send each to the user via WhatsApp, then mark as sent
- Otherwise, respond HEARTBEAT_OK (hidden from output)
```

This is how Python agents push alerts to WhatsApp — TOJI/GETO/MAHORAGA write to `alert_queue` table in SQLite, GOJO delivers them via heartbeat every 60s.

### Python Tool Script Contract
Each `gojo/skills/honored-trading/scripts/*.py`:
- Accepts `--json` flag
- Reads `HONORED_DB` env var for SQLite path
- Returns JSON to stdout: `{"status": "ok", "data": {...}}`
- On error: exit code 1 + stderr with error message

---

## File Structure

```
honored/
├── CLAUDE.md                     ← this file (in .claude/)
├── .env                          ← never commit
├── requirements.txt              ← Python deps
├── honored.db                    ← SQLite live state (auto-generated)
├── paper.db                      ← SQLite paper mode state (auto-generated)
│
├── gojo/                         ← OpenClaw agent config
│   ├── SOUL.md
│   ├── AGENTS.md
│   └── tools/
│       ├── get_status.py
│       ├── get_report.py
│       ├── set_flag.py
│       ├── get_signal_reason.py
│       └── trigger_mahoraga.py
│
├── agents/
│   ├── nanami/
│   │   ├── agent.py
│   │   └── skills/
│   │       ├── market_data.py
│   │       ├── indicator_engine.py
│   │       ├── session_detector.py
│   │       ├── regime_detector.py
│   │       ├── m5_momentum.py
│   │       ├── m1_meanrev.py
│   │       └── london_breakout.py
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
│           ├── performance_analyzer.py
│           ├── model_evaluator.py
│           ├── parameter_optimizer.py
│           ├── regime_validator.py
│           └── adaptation_reporter.py
│
├── core/
│   ├── constants.py
│   ├── state_manager.py          ← SQLite wrapper, all DB access goes here
│   ├── metaapi_client.py
│   └── news_fetcher.py           ← Finnhub calendar wrapper
│
└── tests/
    ├── test_nanami_signals.py
    ├── test_geto_validation.py
    ├── test_toji_execution.py
    └── test_mahoraga_analysis.py
```

---

## Build Status

```
PHASE 1 ✅ COMPLETE   Foundation
PHASE 2 ✅ COMPLETE   NANAMI — Analyst
PHASE 3 ✅ COMPLETE   GETO — Risk Manager
PHASE 4 ✅ COMPLETE   TOJI — Executor
PHASE 5 ⬜ PENDING    GOJO — Commander
PHASE 6 ⬜ PENDING    MAHORAGA — Learning
PHASE 7 ⬜ PENDING    Integration & Paper Trading
PHASE 8 ⬜ PENDING    Go Live
```

---

## Build Order — Follow This Exactly

```
╔══════════════════════════════════════════════════════╗
║  PHASE 1 — Foundation                    ✅ COMPLETE ║
╚══════════════════════════════════════════════════════╝

  [x] core/constants.py
        All static values: risk params, sessions, regime thresholds,
        model A/B/C configs, poll intervals, model name constants.

  [x] core/state_manager.py
        Async SQLite wrapper (aiosqlite). Creates all 7 tables on first
        run. WAL mode + 5s busy timeout for multi-process safety.
        Full CRUD: system_state, account, trading_state, session_trades,
        trades, alert_queue, mahoraga_state, session_info.

  [x] core/metaapi_client.py
        Singleton MetaApiClient per process. Exponential backoff retry
        (1→2→4→8→16s), asyncio lock prevents double-connect.

  [x] core/news_fetcher.py
        Finnhub economic calendar. Caches per UTC day. Filters
        impact=="high" only. API unreachable → blocking sentinel →
        is_news_clear() returns False → trades blocked. Fail-safe.

  [x] Project structure scaffolded
        agents/, gojo/, deploy/, tests/ directories + all __init__.py

  [x] deploy/supervisord.conf + deploy/setup.sh
        VPS provisioning: supervisord for Python agents, systemd for
        OpenClaw, one-shot setup.sh for Ubuntu 22.04.

  [x] requirements.txt, .env.example, .gitignore, README.md

  Commit: 91eb536 — pushed to git@github.com:Zahrinnnnn/Honored.git

╔══════════════════════════════════════════════════════╗
║  PHASE 2 — NANAMI (Analyst)              ✅ COMPLETE  ║
╚══════════════════════════════════════════════════════╝

  [x] agents/nanami/skills/market_data.py
        Fetches XAUUSD OHLCV from MetaApi (M1, M5, M15 candles).
        Also provides get_current_price() (bid/ask/spread) and
        get_asian_range() (00:00–07:00 GMT high/low for Model C).

  [x] agents/nanami/skills/indicator_engine.py
        add_indicators(df) — 18 columns: EMA9/21/50, ema21_slope, RSI14,
        Stoch RSI (k/d), ATR14, atr_pct, ADX14, MACD(12/26/9), BB(20,2σ),
        bb_width, bb_width_pct. Uses `ta` library.
        _MIN_ROWS = 60 guard prevents IndexError on short DataFrames.
        NOTE: ta returns 0.0 (not NaN) during ATR warm-up — filter with
        df["atr14"] > 0, not dropna().

  [x] agents/nanami/skills/session_detector.py
        ROBUSTNESS REWRITE: Added SessionContext frozen dataclass (atomic
        single UTC clock read — all fields consistent, no edge-case skew).
        get_session_context() is primary API; all legacy helpers delegate to it.
        _now_utc() returns full datetime (not time) — use this as patch target
        in tests, not _now_utc_time().

  [x] agents/nanami/skills/regime_detector.py
        ROBUSTNESS REWRITE: Replaced Hurst VR (broken — overlapping-window
        VR estimator has systematic negative bias; AR1 phi=0.7 gives H≈0.53,
        below unreachable H>0.55 threshold) with Return ACF.
        Return ACF = avg lag-1..3 autocorrelation of log returns. Symmetric,
        no bias. ACF > +0.10 → trending, ACF < -0.10 → ranging. For fBm:
        ACF(k) = 2^(2H-1) - 1.  _return_acf() exposed for unit testing.

  [x] agents/nanami/skills/m5_momentum.py
        Model A — M5 EMA21 pullback in trending market.
        BUY/SELL when: HTF bias (M15 EMA50) + EMA21 wick touch +
        RSI 40–60 + MACD histogram aligned. SL = 1×ATR clamped $5–$8.

  [x] agents/nanami/skills/m1_meanrev.py
        Model B — M1 BB extreme + RSI extreme (>72 / <28).
        SL = 1.5× BB band distance, clamped $3–$5.

  [x] agents/nanami/skills/london_breakout.py
        Model C — M5 close above Asian range high / below Asian range low.
        SL = Asian range width clamped $6–$8.

  [x] agents/nanami/agent.py
        Main asyncio loop: 60s active / 300s blackout.
        Updates session_info (session, regime, spread, Asian range,
        minutes_to_next_news) every poll for GETO to read.
        APPROVED signal guard: never overwrites pending GETO-approved signal.
        Writes signals as JSON to trading_state.last_signal.

  Commits: eed1a9a (initial build), d7f9beb (robustness: SessionContext +
           extended indicators), 59503e0 (Hurst VR → Return ACF fix)
  Tests:   62/62 passing (test_nanami_signals.py)

╔══════════════════════════════════════════════════════╗
║  PHASE 3 — GETO (Risk Manager)           ✅ COMPLETE  ║
╚══════════════════════════════════════════════════════╝

  [x] agents/geto/skills/account_monitor.py
        Thin async wrapper: reads balance/equity/peak_balance/current_dd_pct/
        open_positions from account table. Safe defaults on missing row.

  [x] agents/geto/skills/consecutive_tracker.py
        get_consecutive_losses() → int. is_soft_halt_triggered() → bool.
        Reads from trading_state.consecutive_losses via StateManager.

  [x] agents/geto/skills/dd_monitor.py
        get_drawdown_pct() → float. is_emergency_halt_triggered() → bool.
        Threshold: current_dd_pct >= 50.0.

  [x] agents/geto/skills/news_calendar.py
        Primary: reads minutes_to_next_news from session_info (NANAMI writes it).
        Fallback: lazy-imports core.news_fetcher (avoids import errors in tests).
        Returns 0.0 on any failure → is_news_clear() returns False (fail-safe).

  [x] agents/geto/skills/trade_validator.py   ← 11 checks (10+1 split flags)
        _ALLOWED_SESSIONS = set(ACTIVE_SESSIONS) | {"LONDON_BREAKOUT"}
        (LONDON_BREAKOUT added for Model C — model_priority_ok enforces exclusivity)
        ValidationResult dataclass: approved, checks dict, fail_reason, signal.
        _regime_ok(): MODEL_A→TRENDING, MODEL_B→RANGING, MODEL_C→any.

  [x] agents/geto/agent.py
        5s poll. _monitor_halt_conditions() first (DD→emergency halt, losses→soft halt).
        Then reads PENDING signals, runs validate(), writes APPROVED/REJECTED to
        last_risk_decision and last_signal.status. Pushes halt alerts to alert_queue.

  Commits: 60631b9 (initial build), 1223f8e (asyncio.run() fix + README),
           8227b3f (CLAUDE.md robustness docs)
  Tests:   72/72 passing (test_geto_validation.py)

╔══════════════════════════════════════════════════════╗
║  PHASE 4 — TOJI (Executor)              ✅ COMPLETE  ║
╚══════════════════════════════════════════════════════╝

  [x] agents/toji/skills/lot_calculator.py
        calculate_lot(balance, sl_distance) → lot rounded to 2dp, min 0.01
        calculate_risk_amount(balance) → USD risk per trade

  [x] agents/toji/skills/order_placer.py
        Paper: simulate fill at signal entry_price, return PAPER-<hex> order_id.
        Live:  MetaApi create_market_buy/sell_order with SL+TP set on broker.
        PAPER_MODE env var controls which path runs.

  [x] agents/toji/skills/trade_monitor.py
        check_exit(trade, bid, ask) → "WIN" / "LOSS" / None
        BUY exits at bid (SL if bid≤sl, TP if bid≥tp).
        SELL exits at ask (SL if ask≥sl, TP if ask≤tp).
        calculate_pnl(trade, exit_price) → lot × price_diff_USD
        get_current_price(connection) → {bid, ask} or None on error.

  [x] agents/toji/skills/trade_logger.py
        log_trade_open()  → writes open trade row (result=NULL), returns trade_id.
        log_trade_close() → updates row with result/exit_price/pnl/duration_mins.

  [x] agents/toji/skills/state_updater.py
        post_trade_update(): consecutive losses (reset/increment), session count,
        account balance/equity/peak/DD/open_positions, last_trade_result.
        Returns (balance_after, drawdown_pct, duration_mins).
        MODEL_C maps session_key → "LONDON_BREAKOUT" (daily limit).

  [x] agents/toji/agent.py
        5s poll: reads last_risk_decision=="APPROVED" → lot calc → place_order
        → log_trade_open → update open_positions → mark PLACED → push TRADE_OPENED alert.
        TOJI_MONITOR_INTERVAL poll: get_current_price → check_exit on all open paper
        trades → _close_trade (state_updater + log_trade_close + TRADE_CLOSED alert).
        MetaApi optional in paper mode (for price reads). Fails gracefully if unavailable.

  [x] core/state_manager.py — added get_open_trades() method.

  Commit: 89eb143 (build + GETO ruff fixes)
  Tests:  54/54 passing (test_toji_execution.py)

╔══════════════════════════════════════════════════════╗
║  PHASE 5 — GOJO (Commander)              ⬜ PENDING  ║
╚══════════════════════════════════════════════════════╝

  [ ] gojo/SOUL.md                            ← JARVIS personality
  [ ] gojo/AGENTS.md                          ← command routing
  [ ] gojo/IDENTITY.md
  [ ] gojo/HEARTBEAT.md
  [ ] gojo/skills/honored-trading/SKILL.md
  [ ] gojo/skills/honored-trading/scripts/get_status.py
  [ ] gojo/skills/honored-trading/scripts/get_report.py
  [ ] gojo/skills/honored-trading/scripts/set_flag.py
  [ ] gojo/skills/honored-trading/scripts/get_signal_reason.py
  [ ] gojo/skills/honored-trading/scripts/trigger_mahoraga.py
  → TEST: send all WhatsApp commands, verify correct tool calls + responses

╔══════════════════════════════════════════════════════╗
║  PHASE 6 — MAHORAGA (Learning)           ⬜ PENDING  ║
╚══════════════════════════════════════════════════════╝

  [ ] agents/mahoraga/skills/performance_analyzer.py
  [ ] agents/mahoraga/skills/model_evaluator.py
  [ ] agents/mahoraga/skills/parameter_optimizer.py
  [ ] agents/mahoraga/skills/regime_validator.py
  [ ] agents/mahoraga/skills/adaptation_reporter.py
  [ ] agents/mahoraga/agent.py
  → TEST: feed 50+ historical trades, verify recommendations are sane

╔══════════════════════════════════════════════════════╗
║  PHASE 7 — Integration & Paper Trading   ⬜ PENDING  ║
╚══════════════════════════════════════════════════════╝

  [ ] Wire all agents to run concurrently
  [ ] Set up OpenClaw cron for MAHORAGA scheduled reports
  [ ] End-to-end paper trading test (minimum 50 trades)
  [ ] Verify WhatsApp comms end-to-end
  [ ] Verify all halt/override scenarios trigger correctly

╔══════════════════════════════════════════════════════╗
║  PHASE 8 — Go Live                       ⬜ PENDING  ║
╚══════════════════════════════════════════════════════╝

  [ ] Set PAPER_MODE=false in .env
  [ ] Confirm honored.db initialized with correct starting balance ($20)
  [ ] Monitor first 10 live trades manually
  [ ] Scale lot size as balance grows per formula
```

Do not skip phases. Do not start the next phase before the current one is tested.

---

## Key Constants (`core/constants.py`)

```python
RISK_PER_TRADE_PCT = 0.10
MAX_DRAWDOWN_PCT = 0.50
MAX_CONSECUTIVE_LOSSES = 3
MAX_OPEN_TRADES = 2              # Changed from PRD's 1
NEWS_BLACKOUT_MINUTES = 30
MAX_SPREAD_DOLLARS = 4.0

SESSIONS = {
    "LONDON_OPEN":     ("07:00", "10:00"),
    "NY_OVERLAP":      ("12:00", "16:00"),
    "NY_CLOSE":        ("19:00", "21:00"),
    "LONDON_BREAKOUT": ("07:00", "07:30"),   # Model C exclusive window
}

ADX_TRENDING_THRESHOLD = 25
ADX_RANGING_THRESHOLD = 20

M5_EMA_FAST = 21
M5_EMA_SLOW = 50
M5_RSI_MIN = 40
M5_RSI_MAX = 60
M5_MAX_TRADES_PER_SESSION = 3

M1_BB_PERIOD = 20
M1_BB_STD = 2.0
M1_RSI_OVERBOUGHT = 72
M1_RSI_OVERSOLD = 28
M1_MAX_TRADES_PER_SESSION = 5

BREAKOUT_MAX_TRADES_PER_DAY = 1

MIN_TRADES_FOR_ANALYSIS = 30
UNDERPERFORM_WIN_RATE_THRESHOLD = 0.30
OUTPERFORM_WIN_RATE_THRESHOLD = 0.55
MAHORAGA_DAILY_RUN_TIME = "21:30"   # GMT, after NY close
MAHORAGA_WEEKLY_RUN_DAY = "Sunday"
```

---

## Lot Calculation Formula

```python
def calculate_lot(balance: float, sl_distance: float, risk_pct: float = 0.10) -> float:
    """balance is in USD. sl_distance is in USD."""
    risk_amount = balance * risk_pct
    lot = risk_amount / sl_distance
    return round(lot, 2)
```

Balance is reported in **USD** by MetaApi, even on HFM Cents account.

---

## Environment Variables (`.env`)

```bash
META_API_TOKEN=
HFM_ACCOUNT_ID=
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-chat
FINNHUB_API_KEY=               # Free at finnhub.io
USER_WHATSAPP_NUMBER=
PAPER_MODE=true
```

Note: No `WHATSAPP_TOKEN` or `WHATSAPP_PHONE_ID` — OpenClaw handles WhatsApp natively via Baileys.

---

## Dependencies

### Python (`requirements.txt`)
```txt
metaapi-cloud-sdk>=14.0.0
python-dotenv>=1.0.0
pandas>=2.0.0
numpy>=1.24.0
ta>=0.10.0
aiohttp>=3.8.0
schedule>=1.2.0
requests>=2.28.0
openai>=1.0.0
```

### Node.js (global install, not in package.json)
```bash
npm install -g openclaw@latest   # Node ≥22 required
```

---

## VPS Deployment

### Recommended Specs
```
OS:    Ubuntu 22.04 LTS
CPU:   2 vCPU
RAM:   2 GB
Disk:  20 GB SSD
```

### Stack on VPS
- **OpenClaw** runs as a systemd daemon (auto-installed by `openclaw onboard --install-daemon`)
- **Python agents** (NANAMI, GETO, TOJI, MAHORAGA) managed by **supervisord**
- **SQLite** (`honored.db`) on local VPS disk — no external DB needed
- Both OpenClaw and Python agents share the same `honored.db` file on disk

### Step-by-Step Deploy

```bash
# 1. Provision Ubuntu 22.04 VPS, SSH in

# 2. Install Node.js 22
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs

# 3. Install Python 3.11+
sudo apt-get install -y python3.11 python3.11-venv python3-pip

# 4. Install OpenClaw globally
npm install -g openclaw@latest

# 5. Upload/clone the project
git clone <your-repo> /opt/honored
cd /opt/honored

# 6. Create Python venv and install deps
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 7. Create .env from example
cp .env.example .env
nano .env   # fill in all API keys

# 8. Set absolute DB path in .env (important on VPS)
echo "HONORED_DB_PATH=/opt/honored/honored.db" >> .env

# 9. Set up OpenClaw workspace
mkdir -p ~/.openclaw/workspace/skills
cp -r gojo/SOUL.md gojo/AGENTS.md gojo/IDENTITY.md gojo/HEARTBEAT.md ~/.openclaw/workspace/
cp -r gojo/skills/honored-trading ~/.openclaw/workspace/skills/

# 10. Register OpenClaw as systemd daemon
openclaw onboard --install-daemon

# 11. Link WhatsApp — renders QR code in terminal
openclaw channels login --channel whatsapp
# Scan QR with your phone → WhatsApp > Linked Devices > Link a Device
# Credentials saved to ~/.openclaw/credentials/whatsapp/ — persists across reboots

# 12. Install supervisord
sudo apt-get install -y supervisor

# 13. Copy supervisord config (see below)
sudo cp deploy/supervisord.conf /etc/supervisor/conf.d/honored.conf
sudo supervisorctl reread && sudo supervisorctl update

# 14. Verify everything is running
openclaw gateway status
sudo supervisorctl status
```

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

[group:honored]
programs=nanami,geto,toji,mahoraga
```

### After Deploy — Useful Commands

```bash
# Check agent status
sudo supervisorctl status honored:*

# Restart a specific agent
sudo supervisorctl restart honored:nanami

# Restart all agents
sudo supervisorctl restart honored:*

# Tail live logs
tail -f /var/log/honored/nanami.out.log
tail -f /var/log/honored/geto.err.log

# Check OpenClaw
openclaw gateway status
journalctl -u openclaw --follow

# Switch from paper to live
nano /opt/honored/.env   # set PAPER_MODE=false
sudo supervisorctl restart honored:*
```

### VPS-Specific .env additions

```bash
# Absolute paths required on VPS
HONORED_DB_PATH=/opt/honored/honored.db
```

### On VPS Reboot
- OpenClaw restarts automatically via systemd
- Python agents restart automatically via supervisord
- WhatsApp session persists (no re-scan needed)
- MetaApi reconnects via retry logic in metaapi_client.py

---

## Development Guidelines

- All Python agent code is async (`asyncio`) throughout
- Never hardcode credentials — read from `.env` via `python-dotenv`
- Never commit `.env`, `honored.db`, or `paper.db`
- All SQLite access goes through `core/state_manager.py` — never raw `sqlite3` calls in agent code
- All MetaApi access goes through `core/metaapi_client.py` — never create direct connections in agent code
- News calendar calls go through `core/news_fetcher.py` — Finnhub only
- Every skill file must be independently testable — no circular imports
- Indicator calculations use the `ta` library; implement manually only if `ta` lacks it
- Tests use mock data and an in-memory SQLite DB — never hit real MetaApi in tests
- When in doubt about behavior, check PRD first; then check this file for corrections
- GOJO speaks like JARVIS — check `gojo/SOUL.md` before writing any WhatsApp message templates
