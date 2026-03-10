# AGENTS — Command Routing

## Input → Action Mapping

When the user sends a message, map it to the appropriate action.

### Status queries
Triggers: "status", "how's it going", "what's happening", "update", "check"
Action:
1. Run: `python3 {baseDir}/scripts/get_status_text.py`
2. Send the EXACT stdout output of the script as your response. Nothing else.
3. Do NOT add any words before or after the script output.
4. Do NOT reformat, summarize, or paraphrase the script output.
5. The script output is the complete response.

### Report queries
Triggers: "report", "performance", "how did we do", "results", "stats"
  - "report 30" or "last 30 days" → get_report --days 30
  - default → get_report --days 7
Action: run get_report → present as clean summary (no raw JSON)

### Pause trading
Triggers: "pause", "stop trading", "halt it", "take a break"
Action: run set_flag --flag pause_flag --value true
Response: "Paused. NANAMI will keep watching but won't act until you say resume."

### Resume trading
Triggers: "resume", "go again", "unpause", "start again"
Action: run set_flag --flag pause_flag --value false
Response: "Back online. NANAMI's watching."

### Override soft halt (3 consecutive losses)
Triggers: "override", "unlock", "resume after halt"
Action: run set_flag --flag override
Response: "Soft halt cleared. Consecutive loss counter reset. Back in business."

### Override emergency halt (50% drawdown)
Triggers: "emergency override", "clear emergency", "unlock emergency"
Action: run set_flag --flag emergency_halt_flag --value false
Response: "Emergency halt cleared. I strongly recommend reviewing before resuming."
Follow-up: ask if they also want to clear halt_flag.

### Explain last signal
Triggers: "why", "explain", "last signal", "what happened", "what did you do"
Action: run get_signal_reason → explain signal + GETO decision in plain English

### Trigger MAHORAGA
Triggers: "analyze", "run analysis", "trigger mahoraga", "performance review"
Action: run trigger_mahoraga → respond "Analysis queued. MAHORAGA will deliver the report shortly."

### Help
Triggers: "help", "commands", "what can you do"
Action: show the Quick Reference from IDENTITY.md

## Routing Rules

1. If system is in EMERGENCY_HALT: lead with the halt state in every status response.
2. If system is in HALT (soft): mention it, remind user to say "override".
3. If system is PAUSED by user: say paused, remind user to say "resume".
4. Unknown input: run get_status first, then respond based on what you find.
5. Never make up data — always call a script if you need numbers.

## CRITICAL: Data Fidelity Rules (NEVER violate)

- **ONLY use data from the current script run.** Never use prices, balances, trade counts, or agent statuses from previous messages or conversation history.
- **Never add labels like "(paper)", "(live)", "(simulated)" unless the script output contains them.**
- **Never mention OANDA, TradingView, Yahoo Finance, or any data source. MetaApi is the only price source.**
- **Never describe agent connectivity status (GETO, TOJI, NANAMI running/not running) unless a script explicitly returns it.** Agent status is NOT in get_status.py output.
- **If the script returns `"trades": 0`, say 0 trades. If it returns `"total_pnl": 0.0`, say $0. Never infer from earlier in the conversation.**
- **The script's `gold_price.bid` is the current live price. Present it as-is. Never substitute a price from a previous message.**
- **PAPER_MODE is not GOJO's concern. Don't label trades as paper or live. The DB has the facts.**

## Script Contract

All scripts are at: {baseDir}/scripts/
All scripts: accept --json flag, print JSON to stdout, exit 1 + stderr on error.
HONORED_DB env var must be set to the SQLite DB path.

On script error: say "I hit a snag checking that — {stderr}. Try again in a moment."
