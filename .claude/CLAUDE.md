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
- No cap on simultaneous open trades
- Risk per trade = exactly 20% of current balance
- RR ratio = 1:2 fixed (TP always = SL × 2)
- Anti-martingale lot sizing: halve lot for each consecutive loss (lot / 2^losses), floor at 0.01

### Model Priority & Exclusivity
- **07:00–07:30 GMT**: Model C (Asian Breakout) has exclusive priority — Model A **cannot** fire in this window
- **Model A vs Model B**: Mutually exclusive by 6-state regime (A fires only in GRIND regimes, B fires only in TIGHT_RANGE)
- **Model A sessions**: NY_OVERLAP only (LONDON_OPEN 25% dWR + NY_CLOSE disabled)
- **Model B sessions**: NY_OVERLAP only (proven toxic in other sessions)
- **Concurrent trades**: No position cap — multiple trades can be open simultaneously
- **Model C**: Regime-agnostic — filtered by H4 bias only

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
-- Keys: current_session, current_regime, h4_bias, next_news_event,
--       minutes_to_next_news, asian_range_high, asian_range_low,
--       structural_break_until, current_spread, current_bid, current_ask
```

### State Access Rules
```
NANAMI:   READ session_info, system_state, session_trades | WRITE trading_state.last_signal, session_trades, session_info (regime, bias, spread, news)
GETO:     READ all | WRITE system_state (halt_flags), trading_state.last_risk_decision, alert_queue
TOJI:     READ trading_state.last_risk_decision | WRITE account, trading_state (post-trade), trades, session_trades
GOJO:     READ all | WRITE system_state (pause_flag), alert_queue.sent
MAHORAGA: READ trades, account | WRITE mahoraga_state, alert_queue
```

---

## GETO Validation Checks (ALL 11 must pass)

```python
checks = {
    "session_valid":              current_session in ALLOWED_SESSIONS,
    "model_priority_ok":          not breakout_window or signal.model == "ASIAN_BREAKOUT",
    "regime_and_bias_ok":         regime_and_bias_allows(model, direction, regime, h4_bias),
    "session_trades_within_limit": session_trade_count(model) < limit,  # A:8, B:8, C:1
    "consecutive_losses_ok":      consecutive_losses < 3,
    "drawdown_ok":                current_dd_pct < 50.0,
    "news_clear":                 minutes_to_next_news > 30,
    "spread_acceptable":          current_spread < $4.00,
    "not_paused":                 pause_flag == False,
    "not_halted":                 halt_flag == False and emergency_halt_flag == False,
    "structural_break_clear":     structural_break_until expired or empty,
}
```

`regime_and_bias_ok` validates: Model A BUY→BULLISH_GRIND, SELL→BEARISH_GRIND; Model B→TIGHT_RANGE; Model C→H4 bias filter only.
`structural_break_clear` blocks trading during 4h cooldown after single H1 candle > 3×ATR14.

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
│   │       ├── stat_tests.py
│   │       ├── htf_regime.py
│   │       ├── ou_grind.py              ← Model A (OU in GRIND regimes)
│   │       ├── ou_range.py              ← Model B (OU in TIGHT_RANGE)
│   │       └── asian_breakout.py        ← Model C (London breakout)
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
PHASE 5 ✅ COMPLETE   GOJO — Commander
PHASE 6 ✅ COMPLETE   MAHORAGA — Learning
PHASE 7 ✅ COMPLETE   Integration & Paper Trading
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

  [x] agents/nanami/skills/stat_tests.py  ← QUANT UPGRADE (new)
        rolling_hurst(): R/S std-of-differences, clips [0,1], returns 0.5 if
          len < HURST_WINDOW. No systematic bias. lags = [2,4,8,16,32,64].
        classify_hurst(): TRENDING >0.53, RANGING <0.47, UNDEFINED otherwise.
        adf_stationary(): statsmodels ADF, fail-safe (non-stationary on error).
        fit_ou(): OLS Vasicek; returns None if theta≤0 or a≥1.
          theta=-log(a)/dt, mu=b/(1-a), sigma=std(ε)/√dt, σ_eq=σ/√(2θ).
        ou_zscore(): (price - mu) / sigma_eq entry signal.
        KalmanPriceFilter: constant-velocity model, state=[price, velocity].
          R calibrated from var(diff(prices[-50:])), Q = R × 0.01.
        kalman_velocity(): returns final velocity from fitted filter.

  [x] agents/nanami/skills/indicator_engine.py  ← QUANT UPGRADE
        add_indicators(df) — 21 columns (was 18): EMA9/21/50, ema21_slope,
        RSI14, Stoch RSI (k/d), ATR14, atr_pct, ADX14, MACD(12/26/9),
        BB(20,2σ), bb_width, bb_width_pct + z_score_50, kalman_price,
        kalman_velocity (new). _MIN_ROWS = 60 guard.
        NOTE: ta returns 0.0 (not NaN) during ATR warm-up — filter with
        df["atr14"] > 0, not dropna().

  [x] agents/nanami/skills/session_detector.py
        ROBUSTNESS REWRITE: Added SessionContext frozen dataclass (atomic
        single UTC clock read — all fields consistent, no edge-case skew).
        get_session_context() is primary API; all legacy helpers delegate to it.
        _now_utc() returns full datetime (not time) — use this as patch target
        in tests, not _now_utc_time().

  [x] agents/nanami/skills/htf_regime.py  ← 6-STATE REGIME (full rewrite)
        detect_regime(df_h1): Z-score (50-bar) × ATR14 percentile (200-bar)
        → 6 states: BULLISH_GRIND, BULLISH_BLOWOFF, BEARISH_GRIND,
        BEARISH_PANIC, TIGHT_RANGE, TOXIC_CHOP. Default TIGHT_RANGE.
        check_structural_break(df_h1): True if H1 candle > 3×ATR14.
        compute_h4_bias(df_h4): retained for Model C GETO validation.

  [x] agents/nanami/skills/ou_grind.py  ← MODEL A (full rewrite)
        OU mean-reversion in directional GRIND regimes.
        Dual detrend: EMA50 primary (z=0.9, 80-bar) + EMA21 fallback (z=1.3, 40-bar).
        BUY: BULLISH_GRIND + z < -threshold.
        SELL: BEARISH_GRIND + z > threshold.
        Gates: regime, ADF stationary, OU fit, 3≤half_life≤50, z>threshold.
        SL = 1.5×ATR clamped $6–$12; TP = SL×2.

  [x] agents/nanami/skills/ou_range.py  ← MODEL B (full rewrite)
        OU mean-reversion in TIGHT_RANGE (bidirectional, EMA50 only).
        z < -1.3 → BUY, z > 1.3 → SELL.
        Same OU engine as Model A, no EMA21 (proven noisy for bidirectional).
        SL = 1.5×ATR clamped $6–$12; TP = SL×2.

  [x] agents/nanami/skills/asian_breakout.py  ← MODEL C (minor update)
        M5 close above Asian high / below Asian low.
        MODEL_C_MIN_RANGE ($3) guard — skips if range < $3.
        H4 bias filter: BUY needs h4≠BEARISH, SELL needs h4≠BULLISH.
        SL = Asian range width clamped $5–$8; TP = SL×2.

  [x] agents/nanami/agent.py
        Main asyncio loop: 60s active / 300s blackout.
        Fetches H1 (200 bars) for 6-state regime + structural break check.
        Fetches H4 for Model C H4 bias. Model dispatch by regime:
        GRIND→Model A (NY_OVERLAP only, dead zone 14:00-15:00 UTC blocked),
        TIGHT_RANGE→Model B, no-trade regimes→skip.
        Updates session_info every poll. APPROVED signal guard active.
        Writes signals as JSON to trading_state.last_signal.

  Tests:   97/97 passing (test_nanami_signals.py)

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

  [x] agents/geto/skills/trade_validator.py   ← 11 checks
        _ALLOWED_SESSIONS = set(ACTIVE_SESSIONS) | {"LONDON_BREAKOUT"}
        ValidationResult dataclass: approved, checks dict, fail_reason, signal.
        _regime_and_bias_ok(): MODEL_A→GRIND+direction, MODEL_B→TIGHT_RANGE, MODEL_C→H4 bias.
        structural_break_clear: reads structural_break_until from session_info.

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
        calculate_lot(balance, sl_distance, risk_pct, consecutive_losses)
        → lot rounded to 2dp, min 0.01.
        Anti-martingale: lot / 2^consecutive_losses, floor 0.01.
        calculate_risk_amount(balance) → USD risk per trade

  [x] agents/toji/skills/order_placer.py
        Paper: simulate fill at signal entry_price, return PAPER-<hex> order_id.
        Live:  MetaApi create_market_buy/sell_order with SL+TP set on broker.
        PAPER_MODE env var controls which path runs.

  [x] agents/toji/skills/trade_monitor.py
        check_breakeven(trade, bid, ask) → new SL or None (at +1 ATR profit).
        check_exit(trade, bid, ask, now) → "WIN"/"LOSS"/"BREAKEVEN" or None.
        BUY exits at bid (SL/TP), SELL exits at ask (SL/TP).
        OU-calibrated time kill: 2 × half_life × 5 min (fallback 60 min).
        Max duration: 4h hard cap.
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
║  PHASE 5 — GOJO (Commander)              ✅ COMPLETE  ║
╚══════════════════════════════════════════════════════╝

  [x] gojo/SOUL.md                            ← JARVIS personality (witty, dry, never robotic)
  [x] gojo/AGENTS.md                          ← command routing (status/pause/resume/override/why/report/analyze)
  [x] gojo/IDENTITY.md                        ← name, emoji, quick command reference
  [x] gojo/HEARTBEAT.md                       ← polls alert_queue every 60s; formats per alert_type → WhatsApp
  [x] gojo/openclaw.json                      ← openclaw config template: deepseek/deepseek-chat custom provider,
                                                 heartbeat 60s, sandbox off, env: DEEPSEEK_API_KEY + HONORED_DB
  [x] gojo/skills/honored-trading/SKILL.md   ← always:true skill; {baseDir} resolves to scripts/

  [x] gojo/skills/honored-trading/scripts/get_status.py
        Full system snapshot: state, gold_price (bid/ask/spread from session_info), regime,
        session, consecutive_losses, last_signal, last_decision, today P&L (wins/losses/by_model),
        open_trades details (entry/SL/TP/lot), session_counts per model, minutes_to_news,
        upcoming_news (medium+high, next 6h, with currency + affects_gold flag), asian_range.
        --alerts-only mode: returns unsent alert_queue rows + marks them sent (heartbeat path).

  [x] gojo/skills/honored-trading/scripts/get_report.py
        Trade history (--days N, default 7): win_rate, total_pnl, avg_pnl,
        best/worst trade, by_model breakdown.

  [x] gojo/skills/honored-trading/scripts/set_flag.py
        Sets pause_flag / halt_flag / emergency_halt_flag (true/false).
        --flag override: clears halt_flag + resets consecutive_losses to 0.

  [x] gojo/skills/honored-trading/scripts/get_signal_reason.py
        Returns last_signal JSON + last_risk_decision from trading_state.

  [x] gojo/skills/honored-trading/scripts/trigger_mahoraga.py
        Writes manual_trigger=true to mahoraga_state; MAHORAGA picks up on next poll.

  [x] core/news_fetcher.py — refactored to _fetch_all_events() (all impacts) +
        fetch_high_impact_events() (backward compat, used by trading agents) +
        fetch_upcoming_events() (display-only, medium+high, adds minutes_away field).

  OpenClaw deployment notes:
  - Model: deepseek/deepseek-chat via custom provider (baseUrl: https://api.deepseek.com,
    api: openai-completions, apiKey in models.providers.deepseek.apiKey)
  - Gateway auth: DEEPSEEK_API_KEY must be in models.providers.deepseek.apiKey
    (NOT in env section — that's for subprocess scripts only)
  - WhatsApp: dmPolicy=allowlist, allowFrom=[user_number], groupPolicy=allowlist
  - Workspace sync: after any script change, manually cp to ~/.openclaw/workspace/skills/...

  Commits: e1beb63 (build), bae4eba (enrich status: gold price, regime, today P&L, news detail)
  Tested:  All WhatsApp commands working end-to-end (status, pause, resume, override, why,
           report, analyze, news). Heartbeat delivering alerts. 223/223 tests still passing.

╔══════════════════════════════════════════════════════╗
║  PHASE 6 — MAHORAGA (Learning)           ✅ COMPLETE  ║
╚══════════════════════════════════════════════════════╝

  [x] agents/mahoraga/skills/performance_analyzer.py
        compute_stats() — win_rate, total_pnl, avg_pnl, best/worst trade,
        Sharpe ratio (annualised trade-level), max drawdown %, recovery factor,
        profit factor, avg duration. Plus: by_model_stats(), by_session_stats(),
        by_direction_stats(), rolling_stats(days).

  [x] agents/mahoraga/skills/model_evaluator.py
        evaluate_model() — full statistical evaluation per model.
        binomial_significance(): scipy.stats.binomtest (p-value, significant flag);
          falls back to normal approximation if scipy unavailable.
        kelly_fraction(): f* = p − q/b, capped at [0, 0.25] (half-Kelly safety).
        detect_drift(): compare recent 20 trades vs historical baseline;
          drift flagged when recent_wr < historical_wr − 0.15 (15pp drop).
        Composite score 0–100: base=win_rate×100, ±Kelly, ±drift, ±significance.
        Status: HEALTHY | OUTPERFORM | UNDERPERFORM | DRIFT | INSUFFICIENT_DATA.
        Model-specific recommendations: OU_GRIND → adjust z-score thresholds;
          OU_RANGE → adjust z-score thresholds; ASIAN_BREAKOUT → raise range $3→$5.

  [x] agents/mahoraga/skills/parameter_optimizer.py
        analyze_sl_ranges(): bucket trades by SL distance ($0-4/$4-6/$6-8/$8+);
          rank by avg P&L — identifies best SL distance range.
        analyze_session_timing(): UTC-hour buckets; identifies peak trading hours.
        analyze_direction_bias(): BUY vs SELL win rate asymmetry.
        analyze_duration_vs_outcome(): avg duration for wins vs losses.
        generate_parameter_suggestions(): produces human-readable hints.

  [x] agents/mahoraga/skills/regime_validator.py
        validate_regime_accuracy(): trade outcome as regime quality proxy;
          GOOD (≥55%), MARGINAL (40-55%), POOR (<40%), INSUFFICIENT_DATA.
        analyze_regime_stability(): flip rate, dominant regime, distribution dict.
        generate_regime_suggestions(): specific Hurst/ADF threshold adjustments.

  [x] agents/mahoraga/skills/adaptation_reporter.py
        compile_report(): full structured JSON report with generated_at, period_days,
          overall stats, by_model evaluations, regime_accuracy, recommendations list,
          param_hints, summary_counts (CRITICAL/HIGH/MEDIUM/LOW).
        format_whatsapp_summary(): JARVIS-style digest — WR, P&L, Sharpe, per-model
          status emojis, top finding + suggestion, approval reminder.
        Recommendation dataclass: priority, model, type, finding, suggestion,
          confidence_pct, expected_impact. Priority-sorted CRITICAL→LOW.

  [x] agents/mahoraga/agent.py
        60s poll. Daily 21:30 GMT + Sunday weekly 90-day deep review.
        On-demand via mahoraga_state.manual_trigger (set by trigger_mahoraga.py).
        Skips if < MIN_TRADES_FOR_ANALYSIS (30) completed trades.
        Writes last_report JSON to mahoraga_state; pushes MAHORAGA_REPORT alert.
        NEVER auto-applies — all recommendations require explicit user approval.

  [x] core/state_manager.py — added get_trades_by_period(days, paper) method.

  Commit: d7451dd (build Phase 6 + 68 tests)
  Tests:  68/68 passing (test_mahoraga_analysis.py) | 291/291 total

╔══════════════════════════════════════════════════════╗
║  PHASE 7 — Integration & Paper Trading   ✅ COMPLETE  ║
╚══════════════════════════════════════════════════════╝

  [x] Wire all agents to run concurrently
        All 4 Python agents (NANAMI/GETO/TOJI/MAHORAGA) already run as
        independent asyncio processes sharing honored.db via WAL-mode SQLite.
        supervisord.conf covers all 4 — no additional wiring needed.

  [x] DB initialization script
        scripts/init_db.py — seeds account balance, clears all flags to
        clean active state. Supports --balance and --live flags.
        Run before first paper or live session.

  [x] Integration test suite — 47/47 tests passing
        tests/test_integration.py — real SQLite (tmp_path), NO mocks.
        Covers the full cross-agent state machine:
          - Signal validation: all 11 GETO checks via real DB reads
          - Model/regime exclusivity: A→GRIND, B→TIGHT_RANGE, C→any
          - Session count limits: Model A (8/session), Model B (8/session), Model C (1/day)
          - Trade lifecycle: log_trade_open → post_trade_update → log_trade_close
          - Consecutive loss counter: increment, double, reset on win
          - Halt conditions: soft halt (3 losses) + emergency halt (50% DD)
          - Duplicate halt suppression (already-halted guard)
          - Alert queue: SOFT_HALT + EMERGENCY_HALT pushed + marked sent
          - Override flow: halt → clear flags → signal approved again
          - Emergency halt NOT cleared by override alone (requires explicit flag)
          - Signal status written to DB (APPROVED / REJECTED) as GETO does it
          - Trade monitor pure functions: check_exit (BUY/SELL SL/TP) + calculate_pnl

  [x] Total tests: 362/362 passing (315 unit + 47 integration)

  Manual verification steps (run on VPS / local with live agents):
  [ ] OpenClaw cron for MAHORAGA daily/weekly scheduled reports
  [ ] WhatsApp comms end-to-end (all GOJO commands)
  [ ] 50+ paper trades accumulated and verified
  [ ] Halt/override scenarios triggered and confirmed via WhatsApp

  Commit: see git log — "feat: Phase 7 — integration tests + init_db script"
  Tests:  47/47 new (test_integration.py) | 362/362 total

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
# Risk
RISK_PER_TRADE_PCT = 0.05        # 5% risk per trade ($1 on $20)
MAX_DRAWDOWN_PCT = 0.50
MAX_CONSECUTIVE_LOSSES = 3
NEWS_BLACKOUT_MINUTES = 30
MAX_SPREAD_DOLLARS = 4.0
# No MAX_OPEN_TRADES — uncapped

SESSIONS = {
    "LONDON_OPEN":     ("07:00", "10:00"),
    "NY_OVERLAP":      ("12:00", "16:00"),
    "NY_CLOSE":        ("19:00", "21:00"),
    "LONDON_BREAKOUT": ("07:00", "07:30"),
}

# 6-State Regime (H1)
REGIME_Z_SCORE_WINDOW = 50
REGIME_Z_SCORE_THRESHOLD = 1.0
REGIME_ATR_PERCENTILE_THRESHOLD = 75
REGIME_H1_BARS_NEEDED = 200

# Structural Break Override
STRUCTURAL_BREAK_ATR_MULT = 3.0        # single H1 candle > 3×ATR → halt
STRUCTURAL_BREAK_COOLDOWN_HOURS = 4    # hours to cool down

# OU Model Parameters (Model A + B)
OU_ZSCORE_ENTRY_THRESHOLD = 1.3    # Model B z-score (also Model A EMA21 fallback)
OU_ZSCORE_GRIND_THRESHOLD = 0.9    # Model A EMA50 z-score (intermediate — balances quality vs frequency)
OU_MIN_HALF_LIFE = 3
OU_MAX_HALF_LIFE = 50
OU_LOOKBACK = 80                   # primary detrend window (EMA50)
OU_LOOKBACK_SHORT = 40             # short detrend window (EMA21, Model A only)
OU_SL_ATR_MULT = 1.5
OU_SL_MIN = 6.0
OU_SL_MAX = 12.0
ADF_P_VALUE_THRESHOLD = 0.10

# Exit System
RR_RATIO = 2.0
BREAKEVEN_ATR_THRESHOLD = 1.0          # Move SL to entry at +1 ATR profit
OU_TIME_KILL_HALF_LIFE_MULT = 2        # OU time kill = 2 × half_life × 5 min
TIME_KILL_MINUTES = 60                 # fallback for non-OU models
MAX_TRADE_DURATION_MINUTES = 240

# Session limits
M5_MAX_TRADES_PER_SESSION = 8       # Model A
M1_MAX_TRADES_PER_SESSION = 8       # Model B
BREAKOUT_MAX_TRADES_PER_DAY = 1     # Model C

# Model names + session routing
MODEL_A = "OU_GRIND"
MODEL_B = "OU_RANGE"
MODEL_C = "ASIAN_BREAKOUT"

MODEL_SESSIONS = {
    MODEL_A: ["NY_OVERLAP"],         # LONDON_OPEN (25% dWR) + NY_CLOSE disabled
    MODEL_B: ["NY_OVERLAP"],
    MODEL_C: ["LONDON_BREAKOUT"],
}

# Dead zone — blocked inside NY_OVERLAP (US midday doldrums, historically weak)
NY_OVERLAP_DEAD_HOUR_START = 14    # 14:00 UTC
NY_OVERLAP_DEAD_HOUR_END   = 15    # 15:00 UTC
```

---

## Lot Calculation Formula

```python
def calculate_lot(balance: float, sl_distance: float, risk_pct: float = 0.20,
                  consecutive_losses: int = 0) -> float:
    """balance is in USD. sl_distance is in USD."""
    risk_amount = balance * risk_pct
    lot = round(risk_amount / sl_distance, 2)
    if consecutive_losses > 0:
        lot = round(lot / (2 ** consecutive_losses), 2)
    return max(lot, 0.01)
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

### GOJO Script Critical Lessons (learned in production)

1. **python-dotenv is not in system python3** — OpenClaw runs scripts with system python3 (`/usr/bin/python3`), not the project venv. `from dotenv import load_dotenv` silently fails. All GOJO scripts use a standalone `_load_honored_env()` function that reads `/opt/honored/.env` directly via stdlib `open()`.

2. **HONORED_DB must be an absolute path** — Relative paths in `openclaw.json` env section resolve to `~/.openclaw/workspace/`, creating the wrong DB. Always set `"HONORED_DB": "/opt/honored/honored.db"` (absolute).

3. **`_load_honored_env()` does not override existing env vars** — It skips any key already in `os.environ`. So if openclaw passes `HONORED_DB` via its env section, the .env file cannot override it. Get the path right in `openclaw.json` first.

4. **Workspace sync is manual** — After any local script change, copy to VPS and sync:
   ```bash
   # From local machine:
   scp gojo/skills/honored-trading/scripts/*.py root@<VPS>:/opt/honored/gojo/skills/honored-trading/scripts/
   # On VPS:
   cp /opt/honored/gojo/skills/honored-trading/scripts/*.py ~/.openclaw/workspace/skills/honored-trading/scripts/
   openclaw gateway restart
   ```

5. **GOJO has tools:full** — DeepSeek can run arbitrary shell commands on the VPS. This is required for `set_flag.py` etc. but means GOJO can explore and self-repair during debugging.

6. **Validate sync** — `grep -l "_load_honored_env" ~/.openclaw/workspace/skills/honored-trading/scripts/*.py` — should list all 5 scripts.

### Current Deployment State (as of March 2026)
- **VPS:** Hetzner CX23, Helsinki, 89.167.122.162
- **Mode:** Paper (`PAPER_MODE=true`)
- **Starting balance:** $200 USD (STANDARD account)
- **DB:** `/opt/honored/honored.db`
- **Status:** All agents running, GOJO on WhatsApp

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
