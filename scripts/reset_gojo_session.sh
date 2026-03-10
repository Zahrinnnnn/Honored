#!/bin/bash
# Reset GOJO's WhatsApp conversation session (clears poisoned LLM context)
# Run this when GOJO starts hallucinating stale data.

SESSION_JSON="/root/.openclaw/agents/main/sessions/sessions.json"
SESSION_KEY="agent:main:whatsapp:direct:+60169464542"

echo "Stopping openclaw..."
openclaw gateway stop 2>/dev/null || true
sleep 2

# Find and delete the session JSONL file
SESSION_FILE=$(python3 - <<'PYEOF'
import json, sys, os
session_json = "/root/.openclaw/agents/main/sessions/sessions.json"
session_key = "agent:main:whatsapp:direct:+60169464542"
try:
    s = json.load(open(session_json))
    f = s.get(session_key, {}).get("sessionFile", "")
    if f:
        print(os.path.basename(f))
except Exception as e:
    sys.stderr.write(str(e) + "\n")
PYEOF
)

if [ -n "$SESSION_FILE" ]; then
    rm -f "/root/.openclaw/agents/main/sessions/$SESSION_FILE"
    echo "Deleted session file: $SESSION_FILE"
else
    echo "No session file found (already clean)"
fi

# Remove session entry from sessions.json
python3 - <<'PYEOF'
import json
f = "/root/.openclaw/agents/main/sessions/sessions.json"
s = json.load(open(f))
removed = s.pop("agent:main:whatsapp:direct:+60169464542", None)
json.dump(s, open(f, "w"), indent=2)
print("Session entry removed" if removed else "Session entry not found (already clean)")
PYEOF

echo "Restarting openclaw..."
openclaw gateway start
sleep 5
openclaw gateway status | grep -E "Runtime|RPC probe"
echo "Done. GOJO context is clean."
