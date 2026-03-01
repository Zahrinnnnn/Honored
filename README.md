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
- **Regime:** TRENDING only (ADX > 25)
- **Logic:** EMA21 pullback on M5, RSI 40–60 momentum zone, MACD histogram confirming trend
- **Risk:** SL $5–8, TP = 3× SL, max 3 trades/session

### Model B — M1 Mean Reversion Scalp
- **Sessions:** Any active session
- **Regime:** RANGING only (ADX < 20)
- **Logic:** Bollinger Band touch + RSI extreme (>72 sell, <28 buy)
- **Risk:** SL $3–5, TP = 3× SL, max 5 trades/session

### Model C — London Open Breakout
- **Session:** 07:00–07:30 GMT only (30-minute window)
- **Regime:** Any — breakout overrides regime check
- **Logic:** Asian session range (00:00–07:00 GMT) high/low break on M5 candle close
- **Risk:** SL $6–8, TP = 3× SL, max 1 trade/day

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

```
status       → balance, drawdown, open trade, last 5 trades
pause        → pause trading (NANAMI keeps watching)
resume       → resume trading
report       → full trade log summary
why          → reasoning behind last signal
override     → reset halt flag after review
performance  → weekly/monthly stats from MAHORAGA
adapt        → trigger manual MAHORAGA analysis run
```

---

## Stack

| Layer | Technology |
|-------|-----------|
| Commander (GOJO) | OpenClaw + DeepSeek Chat (via OpenAI-compatible API) |
| Trading agents | Python 3.11 + asyncio |
| Indicators | `ta` library + pandas/numpy |
| Broker API | MetaApi Cloud (REST) |
| News calendar | Finnhub free tier |
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
- DeepSeek API key
- Finnhub free API key (finnhub.io)

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

## Build Phases

```
Phase 1 ✅  Foundation        — core/ (constants, state, MetaApi, news)
Phase 2     NANAMI            — analyst skills + agent
Phase 3     GETO              — risk validation (10 checks)
Phase 4     TOJI              — executor (paper mode first)
Phase 5     GOJO              — OpenClaw workspace + tools
Phase 6     MAHORAGA          — learning + adaptation
Phase 7     Integration       — paper trading (50+ trades)
Phase 8     Go Live           — switch PAPER_MODE=false
```

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
