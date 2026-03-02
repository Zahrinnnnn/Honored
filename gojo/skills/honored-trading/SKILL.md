---
name: honored-trading
description: Query and control the HONORED XAUUSD autonomous trading system — status, reports, pause/resume, halt override, signal explanation, MAHORAGA analysis
metadata: {"openclaw":{"emoji":"📈","requires":{"bins":["python3"],"env":["HONORED_DB"]},"always":true}}
---

## Overview

HONORED is an autonomous XAUUSD (gold) trading system with 4 Python agents:
- NANAMI: analyst (generates signals)
- GETO: risk manager (validates signals)
- TOJI: executor (places and monitors trades)
- MAHORAGA: learning (performance analysis)

All state is in SQLite at the path in the HONORED_DB env var.

## Workflow

### Get system status
```
python3 {baseDir}/scripts/get_status.py --json
```
Returns: system flags (paused/halted/emergency), account balance, open positions,
drawdown, consecutive losses, current session, regime, last signal summary, minutes to news.

### Get status + pending alerts (heartbeat use)
```
python3 {baseDir}/scripts/get_status.py --alerts-only --json
```
Returns: list of unsent alert_queue rows. Marks them sent.

### Get trade performance report
```
python3 {baseDir}/scripts/get_report.py --days 7 --json
python3 {baseDir}/scripts/get_report.py --days 30 --json
```
Returns: trade count, win rate, total PnL, avg PnL, by-model breakdown.

### Set system flag
```
python3 {baseDir}/scripts/set_flag.py --flag pause_flag --value true --json
python3 {baseDir}/scripts/set_flag.py --flag pause_flag --value false --json
python3 {baseDir}/scripts/set_flag.py --flag halt_flag --value false --json
python3 {baseDir}/scripts/set_flag.py --flag emergency_halt_flag --value false --json
python3 {baseDir}/scripts/set_flag.py --flag override --json
```
`override` clears halt_flag + resets consecutive_losses to 0.

### Explain last signal
```
python3 {baseDir}/scripts/get_signal_reason.py --json
```
Returns: last signal details (model, direction, entry/sl/tp, reason) + GETO decision.

### Trigger MAHORAGA analysis
```
python3 {baseDir}/scripts/trigger_mahoraga.py --json
```
Queues a manual MAHORAGA analysis run. MAHORAGA picks it up on its next cycle.

## Output Contract

All scripts:
- Print a single JSON object to stdout
- Exit 0 on success, exit 1 on error
- On error: print error message to stderr, stdout may be empty

Success format:
```json
{"status": "ok", "data": {...}}
```

Error format (stderr):
```
Error: <description>
```
