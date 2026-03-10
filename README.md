# HONORED — Autonomous XAUUSD Trading System

> *"The strongest don't need luck. They need a system."*

**HONORED** is a fully autonomous, multi-agent algorithmic trading system built for XAUUSD (Gold/USD) on an HFM MT5 Cents account. Five specialized agents run 24/7, each named after a Jujutsu Kaisen character — because the strongest cursed spirits don't sleep, and neither does this system.

---

## The Agents

| Agent | Character | Type | Role |
|-------|-----------|------|------|
| **GOJO** | Satoru Gojo | OpenClaw + DeepSeek | Commander — orchestration, WhatsApp comms, JARVIS-style interface |
| **NANAMI** | Kento Nanami | Pure Python | Analyst — market watching, 6-state regime detection, OU signal generation |
| **GETO** | Suguru Geto | Pure Python | Risk Manager — 11-check validator, account protection, the immune system |
| **TOJI** | Toji Fushiguro | Pure Python | Executor — MetaApi order placement, anti-martingale sizing, trade monitoring |
| **MAHORAGA** | Mahoraga | Python + LLM | Learning — performance analysis, strategy optimization, adaptation |

> GETO's validation is pure `if/else` — it cannot be reasoned around. MAHORAGA adapts but never acts unilaterally. GOJO never touches trade logic. The architecture is paranoid by design.

---

## Trading Models

### Model A — OU Grind (Directional Mean-Reversion)
- **Sessions:** London Open (07:00–10:00), NY Overlap (12:00–16:00), NY Close (19:00–21:00)
- **Regime:** BULLISH_GRIND or BEARISH_GRIND only (6-state H1 regime)
- **Logic:** Ornstein-Uhlenbeck mean-reversion with dual detrend — EMA50 primary (z=0.8, 80-bar) + EMA21 fallback (z=1.3, 40-bar). BUY in BULLISH_GRIND when z < -threshold, SELL in BEARISH_GRIND when z > threshold
- **Gates:** ADF stationary, OU fit valid, 3 ≤ half_life ≤ 50, |z| > threshold
- **Risk:** SL = 1.5×ATR clamped $6–$12, TP = SL×2, max 8 trades/session

### Model B — OU Range (Bidirectional Mean-Reversion)
- **Sessions:** NY Overlap (12:00–16:00) only — proven toxic in other sessions
- **Regime:** TIGHT_RANGE only
- **Logic:** Ornstein-Uhlenbeck with EMA50 detrend only (z=1.3). z < -1.3 → BUY, z > 1.3 → SELL. No EMA21 — proven noisy for bidirectional
- **Gates:** Same OU engine as Model A, higher z-score threshold
- **Risk:** SL = 1.5×ATR clamped $6–$12, TP = SL×2, max 8 trades/session

### Model C — Asian Breakout
- **Session:** 07:00–07:30 GMT only (30-minute window, exclusive priority)
- **Regime:** Any — filtered by H4 bias only (BUY needs h4≠BEARISH, SELL needs h4≠BULLISH)
- **Logic:** Asian session range (00:00–07:00 GMT) high/low break on M5 close; min range $3 guard
- **Risk:** SL = Asian range width clamped $5–$8, TP = SL×2, max 1 trade/day

---

## Risk Rules (Hard Stops — No Exceptions)

```
Risk per trade:        5% of current balance
Risk:Reward ratio:     1:2 fixed (TP always = SL × 2)
Open trades:           No cap — multiple trades can be open simultaneously
Max drawdown:          50% → EMERGENCY HALT (user unlock only)
Consecutive losses:    3 in a row → SOFT HALT (user "override" to resume)
News blackout:         30 min before/after high-impact events (Finnhub)
Max spread:            $4.00
Structural break:      H1 candle > 3×ATR14 → 4h cooldown
```

### Anti-Martingale Lot Sizing
```python
lot = round((balance × 5%) / sl_distance, 2)
# After each consecutive loss: lot / 2^consecutive_losses
# Floor at 0.01 lot minimum, resets on win
# balance=$20, SL=$8  → lot=0.13
# balance=$20, SL=$8, 1 loss → lot=0.06
# balance=$20, SL=$8, 2 losses → lot=0.03
```

---

## Backtest Results (6 months, realistic friction, $20 start)

```
Combined:  257 trades, 61.5% WR, $20 → $356, Sharpe 2.05, max DD 38.1%
OU_GRIND:  155 trades, 63% WR, +$237 (1.2/day)
OU_RANGE:   82 trades, 61% WR,  +$38 (0.6/day)
BREAKOUT:   20 trades, 55% WR,  +$61 (0.2/day)
Best session: NY_OVERLAP — 153 trades, 67% WR, +$229
```

Realistic friction: dynamic spread ($0.35 calm → $1.20 volatile), slippage ($0.10 → $0.30), ATR-percentile-based.

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

> **Anti-hallucination:** Status responses use `get_status_text.py` which outputs pre-formatted plain text. GOJO echoes it verbatim — no LLM reformatting, no stale data possible. If GOJO starts returning wrong data, run `bash scripts/reset_gojo_session.sh` to clear the poisoned WhatsApp session history.

> **Note:** GOJO scripts use a standalone `_load_honored_env()` parser (no python-dotenv dependency) and check both `HONORED_DB` and `HONORED_DB_PATH` env vars. The `HONORED_DB` path **must be absolute** — relative paths resolve to `~/.openclaw/workspace/` and will read the wrong DB.

---

## Stack

| Layer | Technology |
|-------|-----------|
| Commander (GOJO) | OpenClaw + DeepSeek Chat (custom provider, openai-completions compat) |
| WhatsApp | OpenClaw + Baileys (no Meta Business account needed) |
| Trading agents | Python 3.11 + asyncio |
| Indicators | `ta` library + statsmodels (ADF, OU fit, Hurst) |
| Broker API | MetaApi Cloud (REST) |
| News calendar | Finnhub free tier (API key required) |
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
│   └── news_fetcher.py       # Finnhub calendar — blocks trades if unreachable
│
├── agents/
│   ├── nanami/               # Analyst — signals every 60s
│   │   └── skills/           # market_data, indicator_engine, stat_tests,
│   │                         # htf_regime, ou_grind, ou_range, asian_breakout
│   ├── geto/                 # Risk Manager — 11-check validator
│   ├── toji/                 # Executor — anti-martingale lot sizing, order placement
│   └── mahoraga/             # Learning — scheduled performance analysis
│
├── gojo/                     # OpenClaw workspace files
│   ├── SOUL.md               # JARVIS personality definition
│   ├── AGENTS.md             # Command routing rules
│   ├── HEARTBEAT.md          # Alert queue polling
│   └── skills/honored-trading/
│       ├── SKILL.md          # Skill definition for OpenClaw
│       └── scripts/          # Python tools GOJO calls via exec
│           └── get_status_text.py  # Pre-formatted WhatsApp status (GOJO echoes verbatim)
│
├── scripts/
│   ├── init_db.py                # Initialize DB with starting balance
│   ├── backtest_per_model.py     # Combined backtester with realistic friction
│   ├── reset_gojo_session.sh     # Clear GOJO's poisoned WhatsApp session history
│   └── test_live_execution.py    # Manual live trade execution test (place + close)
│
├── deploy/
│   ├── setup.sh              # One-shot VPS provisioning script
│   └── supervisord.conf      # Process management for Python agents
│
└── tests/                    # 362 tests (315 unit + 47 integration)
```

---

## Setup

### Prerequisites
- Python 3.11+
- Node.js 22+
- MetaApi account + HFM MT5 account (demo or live)
- DeepSeek API key (deepseek.com)
- Finnhub API key (free at finnhub.io)

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
# Fill in: META_API_TOKEN, HFM_ACCOUNT_ID, DEEPSEEK_API_KEY, FINNHUB_API_KEY

# 5. Initialize paper DB
python scripts/init_db.py --balance 20.0

# 6. Set up GOJO workspace
mkdir -p ~/.openclaw/workspace/skills
cp gojo/SOUL.md gojo/AGENTS.md gojo/IDENTITY.md gojo/HEARTBEAT.md ~/.openclaw/workspace/
cp -r gojo/skills/honored-trading ~/.openclaw/workspace/skills/

# 7. Link WhatsApp (scan QR in terminal)
openclaw channels login --channel whatsapp

# 8. Run in paper mode (default)
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

## GETO Validation (all 11 checks must pass)

```
 1. session_valid               Trading session is active
 2. model_priority_ok           Model C exclusive during 07:00–07:30
 3. regime_and_bias_ok          A→GRIND+direction, B→TIGHT_RANGE, C→H4 bias
 4. session_trades_within_limit Count < limit (A:8, B:8, C:1/day)
 5. consecutive_losses_ok       Streak < 3
 6. drawdown_ok                 DD% < 50%
 7. news_clear                  Minutes to next event > 30
 8. spread_acceptable           Spread < $4.00
 9. not_paused                  pause_flag is False
10. not_halted                  halt_flag and emergency_halt_flag both False
11. structural_break_clear      No H1 candle > 3×ATR14 in last 4h
```

---

## Current Deployment

**VPS:** Hetzner CX23 — Ubuntu 22.04, Helsinki
**Status:** ✅ Live (`PAPER_MODE=false`) — real orders on HFM demo MT5
**Starting balance:** $200 USD (HFM demo, STANDARD account)
**DB:** `/opt/honored/honored.db`
**Agents:** NANAMI / GETO / TOJI / MAHORAGA under supervisord; GOJO under `openclaw-gateway.service`

```bash
# Check agents
supervisorctl status honored:*
openclaw gateway status

# Restart agents
supervisorctl restart honored:*
openclaw gateway restart

# Sync GOJO scripts after local changes
cp /opt/honored/gojo/skills/honored-trading/scripts/*.py \
   ~/.openclaw/workspace/skills/honored-trading/scripts/
```

---

## Build Status

| Phase | Agent | Status | Tests |
|-------|-------|--------|-------|
| 1 | **Foundation** | ✅ Complete | — |
| 2 | **NANAMI** | ✅ Complete | 97/97 |
| 3 | **GETO** | ✅ Complete | 72/72 |
| 4 | **TOJI** | ✅ Complete | 54/54 |
| 5 | **GOJO** | ✅ Complete | — |
| 6 | **MAHORAGA** | ✅ Complete | 68/68 |
| 7 | **Integration** | ✅ Complete | 47/47 |
| 8 | **Go Live** | ⬜ Pending | — |

**Total: 362/362 tests passing** (315 unit + 47 integration)

---

## Safety

- **LLM only in GOJO** — DeepSeek never touches trade calculations
- **GETO is pure if/else** — cannot be convinced, cannot hallucinate
- **MAHORAGA never auto-applies changes** — all recommendations require explicit user approval via WhatsApp
- **Paper mode default** — `PAPER_MODE=true` until explicitly switched off
- **News blackout enforced** — Finnhub API unreachable → all trades blocked (fail safe)
- **Emergency halt** — 50% drawdown locks the system until you personally unlock it
- **Anti-martingale** — lot halves on each consecutive loss, protecting against drawdown spirals

---

*Built with intentional paranoia. Every safety check exists because something could go wrong.*
