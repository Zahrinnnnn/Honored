# HONORED — Autonomous XAUUSD Trading System
## Product Requirements Document (PRD)
### Version 1.0 | For Claude Code Implementation

---

## 1. System Overview

**HONORED** is a fully autonomous, multi-agent algorithmic trading system built specifically for XAUUSD (Gold/USD) on an HFM MT5 Cents account. It uses OpenClaw as the agent framework, DeepSeek as the LLM brain for the Commander agent only, and pure Python for all trading logic to eliminate hallucination risk on critical decisions.

The system executes high-frequency intraday trades (7-12 trades/day) across three quantitative models, communicates with the user via WhatsApp, and continuously learns and adapts through a dedicated learning agent.

---

## 2. Agent Roster

| Agent ID | Name | Type | Role |
|----------|------|------|------|
| Agent 1 | **GOJO** | DeepSeek (LLM) | Commander — orchestration, WhatsApp comms |
| Agent 2 | **NANAMI** | Pure Python | Analyst — market watching, signal generation |
| Agent 3 | **GETO** | Pure Python | Risk Manager — account protection, trade validation |
| Agent 4 | **TOJI** | Pure Python | Executor — trade placement, logging |
| Agent 5 | **MAHORAGA** | Python + LLM | Learning & Adaptability — performance analysis, strategy optimization |

---

## 3. Account & Trading Parameters

### 3.1 Account Specifications
```
Broker:          HFM (HF Markets)
Account Type:    Cents Account (MT5)
Base Deposit:    $20 USD (= 2000 USC)
Leverage:        1:2000
Instrument:      XAUUSD only
Access Method:   MetaApi Cloud REST API
```

### 3.2 Risk Parameters
```
Risk Per Trade:      10% of current balance
Max Drawdown Halt:   50% of current balance
Consecutive Loss Halt: 3 losses in a row
Max Open Trades:     1 at any time
News Blackout:       30 minutes before/after high-impact events
```

### 3.3 Dynamic Lot Size Formula
```python
lot_size = (balance * 0.10) / sl_in_dollars
# Round to nearest 0.01
# Example: balance=$20, risk=10%=$2.00, SL=$5 → lot=0.40
```

### 3.4 RR Ratio
```
Risk:Reward = 1:3 (fixed, no exceptions)
TP always = SL × 3
```

---

## 4. Trading Models

### 4.1 Model A — M5 Momentum Scalp
```
Timeframe:     M5
Sessions:      London Open (07:00-10:00 GMT)
               NY Overlap (12:00-16:00 GMT)
Regime:        TRENDING (ADX > 25)

Entry Logic:
  - EMA50 on M15 defines higher timeframe bias
  - Price pulls back to EMA21 on M5
  - RSI between 40-60 (momentum zone)
  - MACD histogram turning in trend direction
  - ATR above 14-period average (volatility active)

SL:            $5-8 below/above recent M5 swing
TP:            3 × SL
Max Trades:    3 per session window
Expected SL Distance: $5-8
Expected Lot: 0.25-0.40
```

### 4.2 Model B — M1 Mean Reversion Scalp
```
Timeframe:     M1
Sessions:      Any active session
Regime:        RANGING (ADX < 20)

Entry Logic:
  - Price touches Bollinger Band outer band on M1
  - RSI > 72 (sell) or RSI < 28 (buy)
  - Price within established session range (not breaking out)
  - ATR below 14-period average (low volatility confirms range)

SL:            $3-5 beyond band
TP:            3 × SL (BB midline)
Max Trades:    5 per session
Expected SL Distance: $3-5
Expected Lot: 0.40-0.65
```

### 4.3 Model C — London Open Breakout
```
Timeframe:     M5
Session:       07:00-07:30 GMT ONLY (30 min window)
Regime:        Any (breakout overrides regime)

Entry Logic:
  - Calculate Asian session range (00:00-07:00 GMT) high/low
  - Wait for M5 candle close ABOVE range high → BUY
  - Wait for M5 candle close BELOW range low → SELL
  - ATR spike on breakout candle confirms volume
  - No fakeouts: candle body must close beyond range, not just wick

SL:            $6-8 back inside Asian range
TP:            3 × SL
Max Trades:    1 per day (London open only)
Expected SL Distance: $6-8
Expected Lot: 0.25-0.33
```

---

## 5. Session Windows

```
ACTIVE SESSIONS (trading allowed):
  London Open:    07:00 - 10:00 GMT
  NY Overlap:     12:00 - 16:00 GMT
  NY Close:       19:00 - 21:00 GMT

BLACKOUT (no trading):
  Asian Session:  21:00 - 07:00 GMT
  News Window:    30 mins before/after high-impact events
```

---

## 6. Agent Specifications

---

### 6.1 GOJO — Commander Agent

**Type:** OpenClaw + DeepSeek LLM
**Purpose:** Natural language interface between user and system. Orchestrates all agents. Never touches trading logic.

#### Skills
| Skill | Description |
|-------|-------------|
| `whatsapp_parser` | Parses incoming WhatsApp messages into structured commands |
| `agent_router` | Routes commands to correct agent based on intent |
| `report_formatter` | Formats trade results and system status into clean WhatsApp messages |
| `alert_manager` | Handles urgent push notifications (halt, DD breach, news) |

#### Accepted WhatsApp Commands
```
"status"        → queries GETO + TOJI, returns balance/DD/open trade/last 5 trades
"pause"         → sets pause_flag=true in shared state
"resume"        → sets pause_flag=false in shared state
"report"        → requests full trade log summary from TOJI
"why"           → requests reasoning for last signal from NANAMI
"override"      → manually resets halt_flag after user review (DD halt only)
"performance"   → requests weekly/monthly stats from MAHORAGA
"adapt"         → triggers MAHORAGA manual analysis run
```

#### What GOJO Never Does
- Never reads raw market data
- Never calculates indicators
- Never makes trade decisions
- Never calls MetaApi directly
- Never modifies risk parameters

---

### 6.2 NANAMI — Analyst Agent

**Type:** Pure Python (zero LLM)
**Purpose:** Continuously watches XAUUSD market, calculates all indicators, detects regime, generates structured trade signals. Runs every 60 seconds non-stop.

#### Skills
| Skill | File | Description |
|-------|------|-------------|
| `market_data` | `skills/market_data.py` | Fetches XAUUSD OHLCV data from MetaApi (M1, M5, M15 candles) |
| `indicator_engine` | `skills/indicator_engine.py` | Calculates EMA9, EMA21, EMA50, RSI14, ATR14, ADX14, MACD, Bollinger Bands |
| `session_detector` | `skills/session_detector.py` | Identifies current session window based on GMT time |
| `regime_detector` | `skills/regime_detector.py` | Classifies market as TRENDING/RANGING/VOLATILE using ADX + ATR |
| `m5_momentum_signal` | `skills/m5_momentum.py` | Model A logic — returns signal JSON or NO_TRADE |
| `m1_meanrev_signal` | `skills/m1_meanrev.py` | Model B logic — returns signal JSON or NO_TRADE |
| `london_breakout` | `skills/london_breakout.py` | Model C logic — returns signal JSON or NO_TRADE |

#### Signal Output Schema
```json
{
  "timestamp": "2026-03-01T13:45:00Z",
  "symbol": "XAUUSD",
  "session": "NY_OVERLAP",
  "regime": "TRENDING",
  "model_used": "M5_MOMENTUM",
  "signal": "BUY",
  "entry_price": 2345.50,
  "sl_price": 2340.50,
  "tp_price": 2360.50,
  "sl_distance_dollars": 5.00,
  "suggested_lot": 0.40,
  "indicators": {
    "ema21": 2344.80,
    "rsi": 47.3,
    "atr": 8.2,
    "adx": 28.5,
    "macd_hist": 0.15
  },
  "reason": "EMA21 touch on pullback, RSI 47 momentum zone, ATR above avg, ADX 28 trend confirmed"
}
```

#### NO_TRADE Output Schema
```json
{
  "timestamp": "2026-03-01T13:45:00Z",
  "signal": "NO_TRADE",
  "reason": "ADX 18 — market ranging, M5 momentum model requires trending regime"
}
```

#### Poll Interval
```
Every 60 seconds during active sessions
Every 300 seconds during blackout (monitoring only, no signals generated)
```

---

### 6.3 GETO — Risk Manager Agent

**Type:** Pure Python (zero LLM)
**Purpose:** Validates every signal against hard risk rules before allowing execution. Acts as the system's immune system. Cannot be reasoned around — pure if/else logic.

#### Skills
| Skill | File | Description |
|-------|------|-------------|
| `account_monitor` | `skills/account_monitor.py` | Real-time balance, equity, margin, drawdown from MetaApi |
| `trade_validator` | `skills/trade_validator.py` | Runs all 9 validation checks on incoming signal |
| `news_calendar` | `skills/news_calendar.py` | Fetches ForexFactory/investing.com calendar, flags high-impact events |
| `consecutive_tracker` | `skills/consecutive_tracker.py` | Tracks loss streak, sets halt_flag at 3 |
| `dd_monitor` | `skills/dd_monitor.py` | Monitors real-time drawdown, triggers emergency halt at 50% |

#### Validation Checklist (ALL must pass)
```python
checks = {
    "session_valid":        current_session in ALLOWED_SESSIONS,
    "regime_matches_model": regime_valid_for_model(signal),
    "consecutive_losses":   state["consecutive_losses"] < 3,
    "drawdown_ok":          state["current_dd_pct"] < 50.0,
    "no_open_trades":       state["open_positions"] == 0,
    "news_clear":           minutes_to_next_high_impact_news() > 30,
    "spread_acceptable":    current_spread_dollars < 4.0,
    "not_paused":           state["pause_flag"] == False,
    "not_halted":           state["halt_flag"] == False
}
```

#### Output Schema
```json
{
  "timestamp": "2026-03-01T13:45:01Z",
  "decision": "PROCEED",
  "signal_received": { ...signal from NANAMI... },
  "checks_passed": 9,
  "checks_failed": 0,
  "failed_reasons": []
}
```

```json
{
  "timestamp": "2026-03-01T13:45:01Z",
  "decision": "BLOCK",
  "checks_passed": 7,
  "checks_failed": 2,
  "failed_reasons": ["news_clear: NFP in 15 mins", "consecutive_losses: 3 streak active"]
}
```

#### Halt Triggers
```
SOFT HALT (consecutive_losses >= 3):
  - Sets halt_flag = true
  - GOJO alerts user via WhatsApp
  - User can override via "override" command
  - NANAMI continues watching market

EMERGENCY HALT (dd >= 50%):
  - Sets emergency_halt_flag = true
  - GOJO sends urgent WhatsApp alert
  - ONLY user can unlock via "override" command
  - Requires explicit confirmation
```

---

### 6.4 TOJI — Executor Agent

**Type:** Pure Python (zero LLM)
**Purpose:** Receives approved signals from GETO, places trades via MetaApi, monitors positions until close, logs everything.

#### Skills
| Skill | File | Description |
|-------|------|-------------|
| `lot_calculator` | `skills/lot_calculator.py` | Dynamic lot size: (balance × 0.10) / sl_distance |
| `order_placer` | `skills/order_placer.py` | Places market order via MetaApi with SL and TP |
| `trade_monitor` | `skills/trade_monitor.py` | Polls open position every 30 seconds until close |
| `trade_logger` | `skills/trade_logger.py` | Appends every trade to trades.csv with full details |
| `state_updater` | `skills/state_updater.py` | Updates shared state JSON after every trade event |

#### Lot Calculation Logic
```python
def calculate_lot(balance: float, sl_distance: float, risk_pct: float = 0.10) -> float:
    risk_amount = balance * risk_pct
    lot = risk_amount / sl_distance
    return round(lot, 2)

# Examples:
# balance=$20.00, SL=$5.00 → lot = 2.00/5.00 = 0.40
# balance=$40.00, SL=$5.00 → lot = 4.00/5.00 = 0.80 (auto-scales)
# balance=$20.00, SL=$8.00 → lot = 2.00/8.00 = 0.25
```

#### MetaApi Order Placement
```python
async def place_order(connection, direction: str, lot: float, sl: float, tp: float):
    if direction == "BUY":
        result = await connection.create_market_buy_order(
            symbol="XAUUSD",
            volume=lot,
            stop_loss=sl,
            take_profit=tp,
            options={"comment": "HONORED_signal"}
        )
    elif direction == "SELL":
        result = await connection.create_market_sell_order(
            symbol="XAUUSD",
            volume=lot,
            stop_loss=sl,
            take_profit=tp,
            options={"comment": "HONORED_signal"}
        )
    return result
```

#### Trade Log Schema (trades.csv)
```
date, time, agent, model, direction, entry_price, sl_price, tp_price,
lot_size, sl_distance, risk_amount, result, exit_price, pnl_dollars,
balance_before, balance_after, drawdown_pct, trade_duration_mins, reason
```

#### Post-Trade State Update
```python
# On trade CLOSE (win or loss):
state["balance"] = new_balance
state["last_trade_result"] = "WIN" or "LOSS"
state["consecutive_losses"] = 0 if WIN else consecutive_losses + 1
state["current_dd_pct"] = calculate_dd(peak_balance, new_balance)
state["open_positions"] = 0
state["last_trade_timestamp"] = timestamp
state["total_trades"] += 1
```

---

### 6.5 MAHORAGA — Learning & Adaptability Agent

**Type:** Python + LLM (scheduled, not continuous)
**Purpose:** Analyzes system performance, detects degrading strategies, suggests parameter adjustments, and adapts the system over time. Runs on schedule, not continuously. Named after the cursed spirit that adapts to overcome any technique.

#### Philosophy
MAHORAGA does NOT make live trading decisions. It analyses historical trade data and proposes adjustments that are logged as recommendations. **No parameter changes are applied automatically** — all changes require explicit user approval via GOJO/WhatsApp. This prevents runaway adaptation.

#### Skills
| Skill | File | Description |
|-------|------|-------------|
| `performance_analyzer` | `skills/performance_analyzer.py` | Calculates win rate, EV, Sharpe, max DD per model |
| `model_evaluator` | `skills/model_evaluator.py` | Compares model performance across sessions and regimes |
| `parameter_optimizer` | `skills/parameter_optimizer.py` | Suggests EMA periods, RSI thresholds, ATR multipliers adjustments |
| `regime_validator` | `skills/regime_validator.py` | Checks if regime detection is still accurate vs recent price action |
| `adaptation_reporter` | `skills/adaptation_reporter.py` | Formats recommendations into WhatsApp-ready report for GOJO |

#### Run Schedule
```
Daily:   After NY close — quick performance snapshot (last 24h trades)
Weekly:  Sunday 00:00 GMT — full model analysis + recommendations
Manual:  On user command "adapt" via WhatsApp
```

#### Performance Metrics Tracked Per Model
```python
metrics = {
    "total_trades":       int,
    "win_rate":           float,   # wins / total
    "avg_rr_achieved":    float,   # actual RR vs theoretical 1:3
    "expected_value":     float,   # (win_rate × 3R) - (loss_rate × 1R)
    "max_consecutive_losses": int,
    "best_session":       str,     # which session performs best
    "worst_session":      str,
    "avg_trade_duration": float,   # minutes
    "sharpe_ratio":       float,
    "profit_factor":      float    # gross profit / gross loss
}
```

#### Adaptation Logic
```
IF model win_rate < 30% over last 50 trades:
    → Flag model as UNDERPERFORMING
    → Suggest parameter review
    → Recommend reducing trade frequency for that model

IF specific session consistently underperforms:
    → Suggest removing that session window for that model

IF consecutive losses increasing trend:
    → Suggest tightening entry conditions
    → Recommend reducing risk per trade temporarily

IF win_rate > 55% consistently:
    → Suggest gradually increasing risk per trade
    → Recommend compounding lot size faster
```

#### Recommendation Output (sent to GOJO for WhatsApp delivery)
```
📊 MAHORAGA Weekly Report — March 1, 2026

MODEL A (M5 Momentum):
  Win Rate: 42% (50 trades)
  EV: +0.26R per trade ✅
  Best Session: NY Overlap
  Recommendation: MAINTAIN

MODEL B (M1 Mean Reversion):
  Win Rate: 28% (36 trades)
  EV: -0.16R per trade ⚠️
  Issue: Performing below breakeven
  Recommendation: SUSPEND pending review

MODEL C (London Breakout):
  Win Rate: 38% (8 trades)
  EV: +0.14R per trade ✅
  Note: Small sample size
  Recommendation: CONTINUE monitoring

ACCOUNT:
  Starting Balance: $20.00
  Current Balance: $31.40
  Total Return: +57%
  Max DD Hit: 22%
  
Reply "approve adapt" to apply recommendations
Reply "reject adapt" to keep current parameters
```

#### Hard Constraints On MAHORAGA
```
MAHORAGA CANNOT:
  - Change live risk parameters without user approval
  - Modify SL/TP logic directly
  - Disable GETO's risk checks
  - Access MetaApi or place trades
  - Run during active trading sessions (daily analysis only after NY close)
```

---

## 7. Shared State Architecture

All agents communicate through a shared state file. No direct agent-to-agent calls except through GOJO.

### 7.1 Shared State Schema (state.json)
```json
{
  "system": {
    "status": "ACTIVE",
    "pause_flag": false,
    "halt_flag": false,
    "emergency_halt_flag": false,
    "last_updated": "2026-03-01T13:45:00Z"
  },
  "account": {
    "balance": 20.00,
    "equity": 20.00,
    "peak_balance": 20.00,
    "current_dd_pct": 0.0,
    "open_positions": 0
  },
  "trading": {
    "consecutive_losses": 0,
    "total_trades_today": 0,
    "last_trade_result": null,
    "last_trade_timestamp": null,
    "last_signal": null,
    "last_risk_decision": null
  },
  "session": {
    "current_session": "NY_OVERLAP",
    "next_news_event": "NFP",
    "minutes_to_next_news": 180,
    "asian_range_high": 2350.00,
    "asian_range_low": 2340.00
  },
  "mahoraga": {
    "pending_recommendations": [],
    "last_analysis_run": "2026-03-01T00:00:00Z",
    "adaptation_status": "IDLE"
  }
}
```

### 7.2 State Access Rules
```
NANAMI:   READ session, system | WRITE trading.last_signal
GETO:     READ all | WRITE system.halt_flag, trading.last_risk_decision
TOJI:     READ trading.last_risk_decision | WRITE account, trading (post-trade)
GOJO:     READ all | WRITE system.pause_flag (on user command)
MAHORAGA: READ all | WRITE mahoraga (recommendations only)
```

---

## 8. MetaApi Integration

### 8.1 Connection Setup
```python
from metaapi_cloud_sdk import MetaApi

META_API_TOKEN = os.environ["META_API_TOKEN"]
ACCOUNT_ID = os.environ["HFM_ACCOUNT_ID"]

async def get_connection():
    api = MetaApi(META_API_TOKEN)
    account = await api.metatrader_account_api.get_account(ACCOUNT_ID)
    connection = account.get_rpc_connection()
    await connection.connect()
    await connection.wait_synchronized()
    return connection
```

### 8.2 Required MetaApi Calls
```python
# NANAMI — Market Data
await connection.get_historical_candles("XAUUSD", "1m", count=100)
await connection.get_historical_candles("XAUUSD", "5m", count=100)
await connection.get_historical_candles("XAUUSD", "15m", count=50)
await connection.get_symbol_price("XAUUSD")

# GETO — Account Monitor
await connection.get_account_information()
await connection.get_positions()

# TOJI — Order Placement
await connection.create_market_buy_order(symbol, volume, sl, tp, options)
await connection.create_market_sell_order(symbol, volume, sl, tp, options)
await connection.get_history_orders_by_time_range(start, end)
```

---

## 9. Project File Structure

```
honored/
├── README.md
├── .env                          # API keys (never commit)
├── requirements.txt
├── state.json                    # Shared state (auto-generated)
├── trades.csv                    # Trade log (auto-generated)
│
├── agents/
│   ├── gojo/                     # Commander
│   │   ├── agent.py
│   │   └── skills/
│   │       ├── whatsapp_parser.py
│   │       ├── agent_router.py
│   │       ├── report_formatter.py
│   │       └── alert_manager.py
│   │
│   ├── nanami/                   # Analyst
│   │   ├── agent.py
│   │   └── skills/
│   │       ├── market_data.py
│   │       ├── indicator_engine.py
│   │       ├── session_detector.py
│   │       ├── regime_detector.py
│   │       ├── m5_momentum.py
│   │       ├── m1_meanrev.py
│   │       └── london_breakout.py
│   │
│   ├── geto/                     # Risk Manager
│   │   ├── agent.py
│   │   └── skills/
│   │       ├── account_monitor.py
│   │       ├── trade_validator.py
│   │       ├── news_calendar.py
│   │       ├── consecutive_tracker.py
│   │       └── dd_monitor.py
│   │
│   ├── toji/                     # Executor
│   │   ├── agent.py
│   │   └── skills/
│   │       ├── lot_calculator.py
│   │       ├── order_placer.py
│   │       ├── trade_monitor.py
│   │       ├── trade_logger.py
│   │       └── state_updater.py
│   │
│   └── mahoraga/                 # Learning & Adaptability
│       ├── agent.py
│       └── skills/
│           ├── performance_analyzer.py
│           ├── model_evaluator.py
│           ├── parameter_optimizer.py
│           ├── regime_validator.py
│           └── adaptation_reporter.py
│
├── core/
│   ├── metaapi_client.py         # MetaApi connection manager
│   ├── state_manager.py          # Shared state read/write
│   ├── news_fetcher.py           # Economic calendar fetcher
│   └── constants.py              # Sessions, thresholds, config
│
└── tests/
    ├── test_nanami_signals.py
    ├── test_geto_validation.py
    ├── test_toji_execution.py
    └── test_mahoraga_analysis.py
```

---

## 10. Environment Variables

```bash
# .env
META_API_TOKEN=your_metaapi_token
HFM_ACCOUNT_ID=your_hfm_account_id
DEEPSEEK_API_KEY=your_deepseek_api_key
WHATSAPP_TOKEN=your_whatsapp_token
WHATSAPP_PHONE_ID=your_phone_id
USER_WHATSAPP_NUMBER=your_number
NEWS_API_KEY=your_forexfactory_or_investing_key
```

---

## 11. Constants & Thresholds

```python
# core/constants.py

# Risk
RISK_PER_TRADE_PCT = 0.10       # 10%
MAX_DRAWDOWN_PCT = 0.50         # 50%
MAX_CONSECUTIVE_LOSSES = 3
MAX_OPEN_TRADES = 1

# Sessions (GMT)
SESSIONS = {
    "LONDON_OPEN":  ("07:00", "10:00"),
    "NY_OVERLAP":   ("12:00", "16:00"),
    "NY_CLOSE":     ("19:00", "21:00"),
    "LONDON_BREAKOUT": ("07:00", "07:30")
}

# Regime Thresholds
ADX_TRENDING_THRESHOLD = 25
ADX_RANGING_THRESHOLD = 20
ATR_VOLATILE_MULTIPLIER = 2.0   # ATR > 2x avg = volatile

# Model A — M5 Momentum
M5_EMA_FAST = 21
M5_EMA_SLOW = 50
M5_RSI_MIN = 40
M5_RSI_MAX = 60
M5_MAX_TRADES_PER_SESSION = 3

# Model B — M1 Mean Reversion
M1_BB_PERIOD = 20
M1_BB_STD = 2.0
M1_RSI_OVERBOUGHT = 72
M1_RSI_OVERSOLD = 28
M1_MAX_TRADES_PER_SESSION = 5

# Model C — London Breakout
LONDON_BREAKOUT_WINDOW_START = "07:00"
LONDON_BREAKOUT_WINDOW_END = "07:30"
ASIAN_SESSION_START = "00:00"
ASIAN_SESSION_END = "07:00"
BREAKOUT_MAX_TRADES_PER_DAY = 1

# Risk Filter
NEWS_BLACKOUT_MINUTES = 30
MAX_SPREAD_DOLLARS = 4.0

# MAHORAGA
MIN_TRADES_FOR_ANALYSIS = 30
UNDERPERFORM_WIN_RATE_THRESHOLD = 0.30
OUTPERFORM_WIN_RATE_THRESHOLD = 0.55
MAHORAGA_DAILY_RUN_TIME = "21:30"   # After NY close GMT
MAHORAGA_WEEKLY_RUN_DAY = "Sunday"
```

---

## 12. WhatsApp Message Templates

```python
# Trade Opened
TRADE_OPENED = """
🟢 TRADE OPENED
━━━━━━━━━━━━━━
Pair:    XAUUSD {direction}
Model:   {model}
Entry:   ${entry_price}
SL:      ${sl_price} (-${sl_distance})
TP:      ${tp_price} (+${tp_distance})
Lot:     {lot_size}
Risk:    ${risk_amount} ({risk_pct}%)
Session: {session}
"""

# Trade Closed Win
TRADE_WIN = """
✅ TRADE WIN
━━━━━━━━━━━━━━
Pair:     XAUUSD {direction}
Entry:    ${entry_price}
Exit:     ${exit_price}
P&L:      +${pnl}
Duration: {duration} mins
Balance:  ${balance}
DD:       {dd_pct}%
Streak:   {win_streak} wins 🔥
"""

# Trade Closed Loss
TRADE_LOSS = """
❌ TRADE LOSS
━━━━━━━━━━━━━━
Pair:     XAUUSD {direction}
Entry:    ${entry_price}
Exit:     ${exit_price}
P&L:      -${pnl}
Duration: {duration} mins
Balance:  ${balance}
DD:       {dd_pct}%
Losses:   {consecutive_losses} in a row
"""

# Soft Halt
SOFT_HALT = """
⚠️ SYSTEM HALTED — GETO
━━━━━━━━━━━━━━
Reason: 3 consecutive losses
Balance: ${balance}
DD: {dd_pct}%

NANAMI still watching market.
Type 'override' to resume trading.
Type 'report' for full log.
"""

# Emergency Halt
EMERGENCY_HALT = """
🚨 EMERGENCY HALT — GETO
━━━━━━━━━━━━━━
50% DRAWDOWN REACHED
Balance: ${balance} (was ${peak_balance})
Loss: ${total_loss}

ALL TRADING SUSPENDED.
Manual review required.
Type 'override' + confirm to unlock.
"""
```

---

## 13. Build Order For Claude Code

Build in this exact sequence. Do not skip ahead.

```
PHASE 1 — Foundation
  1. core/constants.py
  2. core/state_manager.py
  3. core/metaapi_client.py
  4. core/news_fetcher.py

PHASE 2 — NANAMI (Analyst)
  5. skills/market_data.py
  6. skills/indicator_engine.py
  7. skills/session_detector.py
  8. skills/regime_detector.py
  9. skills/m5_momentum.py
  10. skills/m1_meanrev.py
  11. skills/london_breakout.py
  12. agents/nanami/agent.py
  → TEST: Run NANAMI standalone, verify signals on historical data

PHASE 3 — GETO (Risk Manager)
  13. skills/account_monitor.py
  14. skills/consecutive_tracker.py
  15. skills/dd_monitor.py
  16. skills/news_calendar.py
  17. skills/trade_validator.py
  18. agents/geto/agent.py
  → TEST: Feed mock signals, verify all 9 checks fire correctly

PHASE 4 — TOJI (Executor)
  19. skills/lot_calculator.py
  20. skills/order_placer.py (paper mode first)
  21. skills/trade_monitor.py
  22. skills/trade_logger.py
  23. skills/state_updater.py
  24. agents/toji/agent.py
  → TEST: Paper trade 50 signals, verify logs and state updates

PHASE 5 — GOJO (Commander)
  25. skills/whatsapp_parser.py
  26. skills/agent_router.py
  27. skills/report_formatter.py
  28. skills/alert_manager.py
  29. agents/gojo/agent.py
  → TEST: Send all WhatsApp commands, verify correct routing

PHASE 6 — MAHORAGA (Learning)
  30. skills/performance_analyzer.py
  31. skills/model_evaluator.py
  32. skills/parameter_optimizer.py
  33. skills/regime_validator.py
  34. skills/adaptation_reporter.py
  35. agents/mahoraga/agent.py
  → TEST: Feed 50+ historical trades, verify recommendations are sane

PHASE 7 — Integration
  36. Connect all agents through shared state
  37. End-to-end paper trading test (minimum 50 trades)
  38. Verify WhatsApp communication works end-to-end
  39. Verify all halt scenarios trigger correctly

PHASE 8 — Go Live
  40. Switch TOJI from paper mode to live mode
  41. Start with $20 cents account
  42. Monitor first 10 live trades manually
  43. Scale lot size as balance grows per formula
```

---

## 14. Paper Trading Mode

TOJI must support a paper trading mode for testing without real money.

```python
PAPER_MODE = os.environ.get("PAPER_MODE", "true").lower() == "true"

if PAPER_MODE:
    # Simulate fills at current price
    # Log to paper_trades.csv instead of trades.csv
    # Update paper_state.json instead of state.json
    # Send WhatsApp messages tagged [PAPER]
else:
    # Real MetaApi execution
```

---

## 15. Success Criteria

### System Health Metrics (monitored by MAHORAGA)
```
Win Rate:          > 35% across all models (minimum viable)
Expected Value:    > 0 per trade (positive EV required)
Max Drawdown:      Never exceed 50% halt threshold in normal operation
System Uptime:     > 95% during active session windows
Signal Accuracy:   NANAMI signals validated by GETO > 60% pass rate
Execution Speed:   Order placed within 5 seconds of GETO approval
```

### Account Growth Targets
```
Phase 1 ($20 → $40):   Lot size stays at formula output, prove edge
Phase 2 ($40 → $80):   Lot doubles automatically via formula
Phase 3 ($80 → $160):  Review MAHORAGA recommendations, optimize
Phase 4 ($160+):       Consider upgrading to standard account
```

---

## 16. Known Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| LLM hallucination | DeepSeek only in GOJO, never touches trade logic |
| MetaApi downtime | Retry logic with exponential backoff, GOJO alerts user |
| Broker disconnect | TOJI monitors connection, attempts reconnect, halts if fails |
| News spike | GETO news_calendar blocks trades 30 mins before events |
| Over-adaptation | MAHORAGA recommendations require user approval, never auto-applies |
| State corruption | State file backed up every hour, validation on every read |
| WhatsApp failure | GOJO logs all alerts locally if WhatsApp delivery fails |

---

## 17. Dependencies

```txt
# requirements.txt
metaapi-cloud-sdk>=14.0.0
python-dotenv>=1.0.0
pandas>=2.0.0
numpy>=1.24.0
ta>=0.10.0              # Technical analysis indicators
aiohttp>=3.8.0
asyncio
schedule>=1.2.0
requests>=2.28.0
openai>=1.0.0           # DeepSeek API (OpenAI-compatible)
```

---

*PRD Version 1.0 — HONORED Autonomous Trading System*
*Feed this document to Claude Code to begin implementation.*
*Start with Phase 1 — Foundation. Do not skip phases.*
