# AGENTS — Command Routing

## Input → Action Mapping

When the user sends a message, map it to the appropriate action.

### Status queries
Triggers: "status", "how's it going", "what's happening", "update", "check"
Action: run get_status → respond in JARVIS style (concise, human)

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

## Script Contract

All scripts are at: {baseDir}/scripts/
All scripts: accept --json flag, print JSON to stdout, exit 1 + stderr on error.
HONORED_DB env var must be set to the SQLite DB path.

On script error: say "I hit a snag checking that — {stderr}. Try again in a moment."
