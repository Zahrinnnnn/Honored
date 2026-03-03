# HONORED — Autonomous XAUUSD Trading System

> *"The strongest don't need luck. They need a system."*

**HONORED** is a fully autonomous, multi-agent algorithmic trading system built for XAUUSD (Gold/USD) on an HFM MT5 Cents account. Five specialized agents run 24/7, each named after a Jujutsu Kaisen character — because the strongest cursed spirits don't sleep, and neither does this system.

---

## The Agents

| Agent | Character | Type | Role |
|-------|-----------|------|------|
| **GOJO** | Satoru Gojo | OpenClaw + DeepSeek | Commander — orchestration, WhatsApp comms, JARVIS-style interface |
| **NANAMI** | Kento Nanami | Pure Python | Analyst — market watching, indicator calculation, signal generation |
| **GETO** | Suguru Geto | Pure Python | Risk Manager — 10-check validator, account protection, the immune system |
| **TOJI** | Toji Fushiguro | Pure Python | Executor — MetaApi order placement, trade monitoring, logging |
| **MAHORAGA** | Mahoraga | Python + LLM | Learning — performance analysis, strategy optimization, adaptation |

> GETO's validation is pure `if/else` — it cannot be reasoned around. MAHORAGA adapts but never acts unilaterally. GOJO never touches trade logic. The architecture is paranoid by design.

---

## Trading Models

### Model A — M5 Momentum Scalp
- **Sessions:** London Open (07:00–10:00 GMT), NY Overlap (12:00–16:00 GMT)
- **Regime:** TRENDING only (Hurst > 0.53)
- **Logic:** Kalman filter velocity determines trend direction; z-score pullback entry (|z| > 1.5 on 50-bar rolling)
- **Risk:** SL 1× ATR clamped $5–$8, TP = 3× SL, max 3 trades/session

### Model B — M1 Mean Reversion Scalp
- **Sessions:** Any active session
- **Regime:** RANGING only (Hurst < 0.47)
- **Logic:** Ornstein-Uhlenbeck model fit + ADF stationarity gate; entry when |OU z-score| > 2.0 and half-life 5–30 bars
- **Risk:** SL $3–$5, TP = 3× SL, max 5 trades/session

### Model C — London Open Breakout
- **Session:** 07:00–07:30 GMT only (30-minute window)
- **Regime:** Any — breakout overrides regime check
- **Logic:** Asian session range (00:00–07:00 GMT) high/low break on M5 close; min range $3 guard (skips false breakouts)
- **Risk:** SL $6–$8, TP = 3× SL, max 1 trade/day

---

## Risk Rules (Hard Stops — No Exceptions)

```
Risk per trade:        10% of current balance
Risk:Reward ratio:     1:3 fixed (TP always = SL × 3)
Max open trades:       2 simultaneously
Max drawdown:          50% → EMERGENCY HALT (user unlock only)
Consecutive losses:    3 in a row → SOFT HALT (user "override" to resume)
News blackout:         30 min before/after high-impact events
Max spread:            $4.00
```

### Lot Calculation
```python
lot = round((balance * 0.10) / sl_distance, 2)
# balance=$20, SL=$5  → lot=0.40
# balance=$40, SL=$5  → lot=0.80  (auto-scales with growth)
```

---

## Architecture

```
WhatsApp ←→ OpenClaw (Node.js daemon) ←→ GOJO
                                              ↕ SQLite (honored.db)
                          NANAMI ←→ GETO ←→ TOJI ←→ MAHORAGA
```

- **GOJO** (OpenClaw) handles all WhatsApp I/O via Baileys — no Meta Business account needed
- **Python agents** are standalone `asyncio` processes with zero knowledge of each other
- **SQLite** is the only inter-agent communication channel — no message brokers, no HTTP calls
- **GOJO heartbeat** (every 60s) polls the `alert_queue` table and pushes pending alerts to WhatsApp
- **MAHORAGA** runs daily at 21:30 GMT and weekly on Sundays — never live, never auto-applies changes

---

## WhatsApp Commands

| Command | What it does |
|---------|-------------|
| `status` | Gold price, balance/DD, regime, session, open trades, today P&L, upcoming news (next 6h, medium+high) |
| `report` | Last 7 days — win rate, total P&L, avg trade, best/worst, breakdown by model |
| `report 30` | Same, last 30 days |
| `pause` | Pause signal generation (NANAMI keeps watching, no new trades) |
| `resume` | Resume trading |
| `override` | Clear soft halt after 3 consecutive losses |
| `emergency override` | Clear emergency halt after 50% drawdown |
| `why` | Last signal details + GETO's validation decision |
| `analyze` | Trigger MAHORAGA performance analysis immediately |
| `help` | Show command reference |

GOJO responds in JARVIS style — confident, dry wit, never robotic.

---

## Stack

| Layer | Technology |
|-------|-----------|
| Commander (GOJO) | OpenClaw v2026.3 + DeepSeek Chat (custom provider, openai-completions compat) |
| WhatsApp | OpenClaw + Baileys (no Meta Business account needed) |
| Trading agents | Python 3.11 + asyncio |
| Indicators | `ta` library + statsmodels + scipy (Hurst, Kalman, OU) |
| Broker API | MetaApi Cloud (REST) |
| News calendar | ForexFactory XML free feed (no API key needed) |
| Shared state | SQLite (WAL mode, concurrent-safe) |
| Process mgmt (VPS) | OpenClaw → systemd, Python agents → supervisord |

---

## Project Structure

```
honored/
├── core/
│   ├── constants.py          # All thresholds, session windows, model configs
│   ├── state_manager.py      # SQLite wrapper — all DB access goes through here
│   ├── metaapi_client.py     # MetaApi connection with retry/backoff
│   └── news_fetcher.py       # ForexFactory XML calendar — blocks trades if unreachable
│
├── agents/
│   ├── nanami/               # Analyst — signals every 60s
│   ├── geto/                 # Risk Manager — 10-check validator
│   ├── toji/                 # Executor — order placement + logging
│   └── mahoraga/             # Learning — scheduled performance analysis
│
├── gojo/                     # OpenClaw workspace files
│   ├── SOUL.md               # JARVIS personality definition
│   ├── AGENTS.md             # Command routing rules
│   ├── HEARTBEAT.md          # Alert queue polling
│   └── skills/honored-trading/
│       ├── SKILL.md          # Skill definition for OpenClaw
│       └── scripts/          # Python tools GOJO calls via exec
│
├── deploy/
│   ├── setup.sh              # One-shot VPS provisioning script
│   └── supervisord.conf      # Process management for Python agents
│
└── tests/
```

---

## Setup

### Prerequisites
- Python 3.11+
- Node.js 22+
- MetaApi account + HFM MT5 Cents account
- DeepSeek API key (deepseek.com)
- No news API key needed — ForexFactory XML feed is free

### Local Development

```bash
# 1. Clone
git clone git@github.com:Zahrinnnnn/Honored.git
cd Honored

# 2. Install Python deps
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. Install OpenClaw
npm install -g openclaw@latest

# 4. Configure environment
cp .env.example .env
# Fill in: META_API_TOKEN, HFM_ACCOUNT_ID, DEEPSEEK_API_KEY
# (No FINNHUB_API_KEY needed — uses ForexFactory free XML feed)

# 5. Set up GOJO workspace
mkdir -p ~/.openclaw/workspace/skills
cp gojo/SOUL.md gojo/AGENTS.md gojo/IDENTITY.md gojo/HEARTBEAT.md ~/.openclaw/workspace/
cp -r gojo/skills/honored-trading ~/.openclaw/workspace/skills/

# 6. Link WhatsApp (scan QR in terminal)
openclaw channels login --channel whatsapp

# 7. Run in paper mode (default)
python agents/nanami/agent.py   # PAPER_MODE=true in .env
```

### VPS Deployment (Ubuntu 22.04)

```bash
# Upload project to /opt/honored, then:
bash deploy/setup.sh

# Link WhatsApp once via SSH
openclaw channels login --channel whatsapp

# Start all agents
sudo supervisorctl start honored:*
```

See `deploy/setup.sh` for the full automated provisioning script.

---

## Build Status

| Phase | Agent | Status | Tests | What's inside |
|-------|-------|--------|-------|---------------|
| 1 | **Foundation** | ✅ Complete | — | `core/` — constants, SQLite state manager, MetaApi client, ForexFactory news fetcher |
| 2 | **NANAMI** | ✅ Complete | 97/97 | Market data, indicator engine (21 cols), session/regime detection, 3 quant model signals |
| 3 | **GETO** | ✅ Complete | 72/72 | Account monitor, consecutive tracker, DD monitor, news calendar, 11-check validator, halt logic |
| 4 | **TOJI** | ✅ Complete | 54/54 | Lot calculator, paper/live order placer, trade monitor (SL/TP detection), trade logger, state updater |
| 5 | **GOJO** | ✅ Complete | — | OpenClaw + DeepSeek, SOUL.md/AGENTS.md/HEARTBEAT.md, 5 tool scripts, enriched status, ForexFactory news display |
| 6 | **MAHORAGA** | ✅ Complete | 68/68 | Binomial significance, Kelly criterion, drift detection, SL/session/regime analysis, JARVIS WhatsApp reports |
| 7 | **Integration** | ⬜ Pending | — | All agents wired, paper trading (50+ trades), halt/override scenarios verified |
| 8 | **Go Live** | ⬜ Pending | — | `PAPER_MODE=false`, live on $20 HFM Cents account |

### Phase 1 — Foundation

```
core/
├── constants.py       All static values: risk params, session windows,
│                      regime thresholds, Model A/B/C configs, poll intervals
├── state_manager.py   Async SQLite wrapper (aiosqlite, WAL mode)
│                      7 tables: system_state, account, trading_state,
│                      session_trades, trades, alert_queue, session_info
├── metaapi_client.py  Singleton connection + exponential backoff retry
└── news_fetcher.py    Finnhub calendar, daily cache, fail-safe blocking default

deploy/
├── supervisord.conf   Process management for 4 Python agents on VPS
└── setup.sh           One-shot Ubuntu 22.04 provisioning script
```

### Phase 2 — NANAMI (Analyst) — 97/97 tests

```
agents/nanami/
├── skills/
│   ├── stat_tests.py         Quant primitives: Hurst (R/S), Kalman filter, OU model,
│   │                         ADF stationarity, OU z-score
│   ├── market_data.py        XAUUSD OHLCV from MetaApi (M1/M5/M15), Asian range
│   ├── indicator_engine.py   21 indicators: EMA9/21/50, RSI14, Stoch RSI, ATR14,
│   │                         ADX14, MACD, BB(20,2σ), bb_width_pct, atr_pct,
│   │                         z_score_50, kalman_price, kalman_velocity
│   ├── session_detector.py   SessionContext (atomic clock read), 4 session windows
│   ├── regime_detector.py    ATR spike veto → Hurst vote → Return ACF vote → BB vote
│   ├── m5_momentum.py        Model A — Kalman velocity + z-score pullback entry
│   ├── m1_meanrev.py         Model B — OU+ADF 7-gate, |z|>2.0, half-life 5–30 bars
│   └── london_breakout.py    Model C — Asian range break, min $3 range guard
└── agent.py                  60s/300s async poll loop, APPROVED signal guard
```

### Phase 3 — GETO (Risk Manager) — 72/72 tests

```
agents/geto/
├── skills/
│   ├── account_monitor.py      Reads balance/DD%/open positions from SQLite
│   ├── consecutive_tracker.py  Reads loss streak, detects soft halt threshold
│   ├── dd_monitor.py           Reads drawdown%, detects 50% emergency halt
│   ├── news_calendar.py        Minutes-to-news from session_info (NANAMI writes);
│   │                           falls back to Finnhub; 0.0 on failure (fail-safe)
│   └── trade_validator.py      11 checks — returns ValidationResult (pure if/else)
└── agent.py                    5s poll, validates PENDING signals, monitors halt
                                conditions, writes APPROVED/REJECTED to SQLite,
                                pushes SOFT_HALT / EMERGENCY_HALT alerts to queue
```

**GETO validation checks (all 11 must pass):**
```
1.  session_valid              live session is a trading session (incl. LONDON_BREAKOUT)
2.  model_priority_ok          Model C exclusive during 07:00–07:30 breakout window
3.  regime_matches_model       TRENDING↔Model A, RANGING↔Model B, any↔Model C
4.  session_trades_within_limit count < per-session max (A:3, B:5, C:1/day)
5.  consecutive_losses_ok      streak < 3
6.  drawdown_ok                DD% < 50%
7.  open_trades_ok             open positions < 2
8.  news_clear                 minutes to next event > 30
9.  spread_acceptable          spread < $4.00
10. not_paused                 pause_flag is False
11. not_halted                 halt_flag and emergency_halt_flag are both False
```

### Phase 4 — TOJI (Executor) — 54/54 tests

```
agents/toji/
├── skills/
│   ├── lot_calculator.py   lot = round((balance × 10%) / sl_distance, 2); min 0.01
│   ├── order_placer.py     Paper: simulated fill at signal entry_price (PAPER-<id>)
│   │                       Live:  MetaApi create_market_buy/sell_order with SL+TP
│   ├── trade_monitor.py    check_exit(trade, bid, ask) → WIN/LOSS/None
│   │                       calculate_pnl(trade, exit_price) → USD P&L
│   │                       get_current_price(connection) → {bid, ask}
│   ├── trade_logger.py     log_trade_open() → trade_id; log_trade_close() updates row
│   └── state_updater.py    post_trade_update(): consecutive losses, session count,
│                           account balance/DD/open_positions, trading_state keys
└── agent.py                5s poll: APPROVED → lot calc → place order → log open
                            → mark PLACED; TOJI_MONITOR_INTERVAL poll: check SL/TP
                            → log close → push TRADE_OPENED/TRADE_CLOSED alerts
```

**PnL formula:** `pnl = lot_size × price_diff_USD`
(derives from `lot = risk_amount / sl_distance`; 1 lot = $1/dollar on HFM Cents)

### Phase 5 — GOJO (Commander) ✅ Complete

```
gojo/
├── SOUL.md                        JARVIS personality — witty, dry, confident, never robotic
├── AGENTS.md                      Command routing (status/report/pause/resume/override/why/analyze)
├── IDENTITY.md                    Name, emoji, command quick-reference
├── HEARTBEAT.md                   Polls alert_queue every 60s; formats TRADE_OPENED/CLOSED/HALT
│                                  alerts into JARVIS-style messages → WhatsApp
├── openclaw.json                  Config template (deepseek/deepseek-chat custom provider)
└── skills/honored-trading/
    ├── SKILL.md                   always:true skill; {baseDir} → script dir
    └── scripts/
        ├── get_status.py          Gold price (bid/ask/spread), balance, regime, session,
        │                          today P&L/wins/losses by model, open trade details,
        │                          session counts per model, upcoming news (medium+high,
        │                          next 6h, currency + affects_gold flag) → JSON
        ├── get_report.py          Trade history (--days N, default 7): win_rate, total_pnl,
        │                          avg_pnl, best/worst trade, by_model breakdown → JSON
        ├── set_flag.py            pause_flag / halt_flag / emergency_halt_flag toggle;
        │                          --flag override clears halt + resets consecutive_losses
        ├── get_signal_reason.py   Last signal + GETO validation decision → JSON
        └── trigger_mahoraga.py    Sets manual_trigger in mahoraga_state → JSON
```

All scripts: read `HONORED_DB` env var, return `{"status":"ok","data":{...}}`, exit 1 on error.
GOJO uses DeepSeek Chat via OpenClaw's custom provider (openai-completions compat).
Model auth: `apiKey` in `models.providers.deepseek` — NOT in the `env` section (that's subprocess only).

**Deployment:** `openclaw gateway run` with `DEEPSEEK_API_KEY` set; workspace files at
`~/.openclaw/workspace/`; scripts must be manually synced after changes (`cp gojo/skills/... ~/.openclaw/workspace/skills/...`).

### Phase 6 — MAHORAGA (Learning) ✅ Complete

```
agents/mahoraga/
├── skills/
│   ├── performance_analyzer.py   Sharpe ratio, max drawdown, recovery factor,
│   │                             profit factor; by_model/session/direction/rolling breakdowns
│   ├── model_evaluator.py        scipy binomtest significance + normal approx fallback;
│   │                             Kelly criterion f* = p − q/b (capped half-Kelly);
│   │                             drift detection: recent 20 vs historical (Δ≥15% → DRIFT);
│   │                             composite 0–100 health score; model-specific suggestions
│   ├── parameter_optimizer.py    SL distance bucket analysis ($0-4/$4-6/$6-8/$8+);
│   │                             session timing heatmap (UTC-hour); BUY vs SELL bias;
│   │                             duration vs outcome; parameter suggestion generator
│   ├── regime_validator.py       Regime accuracy proxy via trade outcomes (GOOD/MARGINAL/POOR);
│   │                             stability analysis (flip rate, dominant regime);
│   │                             Hurst/ADF threshold adjustment suggestions
│   └── adaptation_reporter.py    compile_report() → full JSON report stored in mahoraga_state;
│                                 format_whatsapp_summary() → JARVIS digest with per-model
│                                 emojis, top finding, confidence %, expected impact;
│                                 Recommendations: CRITICAL | HIGH | MEDIUM | LOW
└── agent.py                      60s poll; daily 21:30 GMT + Sunday weekly 90-day review;
                                  on-demand via manual_trigger; skips if < 30 trades;
                                  writes last_report to mahoraga_state; pushes MAHORAGA_REPORT;
                                  NEVER auto-applies — all changes require user approval
```

**State manager:** `get_trades_by_period(days, paper)` method added to `core/state_manager.py`.

Sample recommendations MAHORAGA generates:
- 🔴 `CRITICAL`: drift detected — recent 20 trades at 32% WR vs historical 58%. Consider pausing Model B.
- ⚠️ `HIGH`: M5_MOMENTUM win rate 27% (p=0.02, significant). Raise z-score threshold 1.5 → 2.0.
- 📊 `MEDIUM`: Kelly fraction ≤ 0 on M1_MEANREV — no positive edge at current win rate. Review entry criteria.
- ✅ `LOW`: SL $4–6 range → 68% WR vs $6–8 → 41% WR. Tighter SLs performing better.

---

## Safety

- **LLM only in GOJO** — DeepSeek never touches trade calculations
- **GETO is pure if/else** — cannot be convinced, cannot hallucinate
- **MAHORAGA never auto-applies changes** — all recommendations require explicit user approval via WhatsApp
- **Paper mode default** — `PAPER_MODE=true` until explicitly switched off
- **News blackout enforced** — Finnhub API unreachable → all trades blocked (fail safe)
- **Emergency halt** — 50% drawdown locks the system until you personally unlock it

---

*Built with intentional paranoia. Every safety check exists because something could go wrong.*
