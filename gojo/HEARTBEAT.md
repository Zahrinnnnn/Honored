# Heartbeat — runs every 60 seconds

## Task

Check for pending alerts from the HONORED trading system and push them to the user.

## Steps

1. Run: python3 {skillsDir}/honored-trading/scripts/get_status.py --alerts-only --json

2. Parse the JSON response.

3. If `alerts` is an empty list or the response has no alerts: respond HEARTBEAT_OK
   (do not output anything to the user — silent heartbeat).

4. If `alerts` contains items: for each alert, send a natural-language message to the
   user via WhatsApp. Format based on alert_type:

   TRADE_OPENED (BUY):
     "Just opened a BUY on gold at ${entry}. Stop at ${sl}, target ${tp}. Watching."

   TRADE_OPENED (SELL):
     "Just opened a SELL on gold at ${entry}. Stop at ${sl}, target ${tp}. Watching."

   TRADE_CLOSED WIN:
     "Closed at target — ${pnl} profit. Balance now ${balance}."

   TRADE_CLOSED LOSS:
     "Stopped out. -${pnl} loss. Balance now ${balance}. ${consecutive} in a row."

   SOFT_HALT:
     "Three losses in a row. I've paused trading — that's what I'm here for.
      Say 'override' when you're ready to go again."

   EMERGENCY_HALT:
     "Emergency stop. We're down 50%. Everything's locked. You need to personally
      review and say 'emergency override' to unlock."

   MAHORAGA_REPORT:
     "MAHORAGA analysis ready: ${message}"

   Any other type:
     Send the message field verbatim.

5. The script marks alerts as sent automatically — no additional action needed.

## Notes

- Never send raw JSON to the user.
- Keep messages short — the human is likely on their phone.
- If the script fails (non-zero exit), respond HEARTBEAT_OK silently. Do not alert
  the user for transient script errors — only for genuine trading events.
