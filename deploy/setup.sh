#!/bin/bash
# HONORED — One-Shot VPS Setup Script
# Run as root on fresh Ubuntu 22.04
#
# Usage:
#   wget -O setup.sh https://raw.githubusercontent.com/Zahrinnnnn/Honored/master/deploy/setup.sh
#   bash setup.sh

set -e

PROJECT_DIR="/opt/honored"
LOG_DIR="/var/log/honored"
REPO="https://github.com/Zahrinnnnn/Honored.git"

# ─── 1. System packages ───────────────────────────────────────────────────────
echo ""
echo "=== [1/7] Installing system packages ==="
apt-get update -qq
apt-get install -y git curl supervisor nano

# Node.js 22
curl -fsSL https://deb.nodesource.com/setup_22.x | bash - > /dev/null
apt-get install -y nodejs

# Python 3.11
apt-get install -y python3.11 python3.11-venv python3-pip

echo "  Node: $(node --version)  |  Python: $(python3.11 --version)"

# ─── 2. Clone / update repo ───────────────────────────────────────────────────
echo ""
echo "=== [2/7] Cloning repo ==="
if [ -d "$PROJECT_DIR/.git" ]; then
    echo "  Repo already exists — pulling latest..."
    git -C "$PROJECT_DIR" pull origin master
else
    git clone "$REPO" "$PROJECT_DIR"
fi
cd "$PROJECT_DIR"

# ─── 3. Python virtual environment ───────────────────────────────────────────
echo ""
echo "=== [3/7] Setting up Python venv ==="
python3.11 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
echo "  Python deps installed."

# ─── 4. .env file ─────────────────────────────────────────────────────────────
echo ""
echo "=== [4/7] Creating .env ==="
if [ -f "$PROJECT_DIR/.env" ]; then
    echo "  .env already exists — skipping. Edit manually: nano $PROJECT_DIR/.env"
else
    cat > "$PROJECT_DIR/.env" << 'ENVEOF'
# MetaApi — get from app.metaapi.cloud
META_API_TOKEN=
HFM_ACCOUNT_ID=

# DeepSeek — get from platform.deepseek.com (for GOJO/WhatsApp brain)
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-chat

# Your WhatsApp number (must match openclaw.json allowFrom)
USER_WHATSAPP_NUMBER=+60169464542

# Trading mode — KEEP TRUE until you manually verify paper trades work
PAPER_MODE=true

# Account type — HFM Cents account = CENTS (MetaAPI reports balance in USD)
ACCOUNT_TYPE=CENTS

# Absolute DB paths (required on VPS)
HONORED_DB_PATH=/opt/honored/honored.db
ENVEOF
    echo "  Created. FILL IN your API keys now:"
    echo ""
    nano "$PROJECT_DIR/.env"
fi

# ─── 5. Log directories ───────────────────────────────────────────────────────
echo ""
echo "=== [5/7] Creating log directories ==="
mkdir -p "$LOG_DIR"
echo "  Logs will be in: $LOG_DIR"

# ─── 6. OpenClaw + GOJO workspace ────────────────────────────────────────────
echo ""
echo "=== [6/7] Setting up OpenClaw + GOJO ==="
npm install -g openclaw@latest --silent

mkdir -p ~/.openclaw/workspace/skills

# Copy GOJO workspace files
cp "$PROJECT_DIR/gojo/SOUL.md"      ~/.openclaw/workspace/
cp "$PROJECT_DIR/gojo/AGENTS.md"    ~/.openclaw/workspace/
cp "$PROJECT_DIR/gojo/IDENTITY.md"  ~/.openclaw/workspace/
cp "$PROJECT_DIR/gojo/HEARTBEAT.md" ~/.openclaw/workspace/
cp -r "$PROJECT_DIR/gojo/skills/honored-trading" ~/.openclaw/workspace/skills/

# Inject env vars into openclaw.json (replace ${VAR} placeholders)
source "$PROJECT_DIR/.env" 2>/dev/null || true
DEEPSEEK_KEY=$(grep "^DEEPSEEK_API_KEY=" "$PROJECT_DIR/.env" | cut -d= -f2)
USER_WA=$(grep "^USER_WHATSAPP_NUMBER=" "$PROJECT_DIR/.env" | cut -d= -f2)
DB_PATH=$(grep "^HONORED_DB_PATH=" "$PROJECT_DIR/.env" | cut -d= -f2)
DB_PATH="${DB_PATH:-/opt/honored/honored.db}"

# Write openclaw.json with values substituted
cat > ~/.openclaw/openclaw.json << OCEOF
{
  "models": {
    "providers": {
      "deepseek": {
        "baseUrl": "https://api.deepseek.com",
        "api": "openai-completions",
        "apiKey": "${DEEPSEEK_KEY}",
        "models": [
          {
            "id": "deepseek-chat",
            "name": "DeepSeek Chat",
            "contextWindow": 64000,
            "maxTokens": 8192
          }
        ]
      }
    }
  },
  "agents": {
    "defaults": {
      "workspace": "~/.openclaw/workspace",
      "model": { "primary": "deepseek/deepseek-chat" },
      "heartbeat": { "every": "60s" },
      "sandbox": { "mode": "off" }
    }
  },
  "channels": {
    "whatsapp": {
      "dmPolicy": "allowlist",
      "allowFrom": ["${USER_WA}"],
      "sendReadReceipts": false,
      "ackReaction": { "emoji": "⚡" }
    }
  },
  "cron": { "enabled": true, "maxConcurrentRuns": 1 },
  "tools": { "profile": "full" },
  "env": {
    "DEEPSEEK_API_KEY": "${DEEPSEEK_KEY}",
    "HONORED_DB": "${DB_PATH}"
  }
}
OCEOF

echo "  OpenClaw installed. GOJO workspace synced."

# Register OpenClaw as systemd daemon
openclaw onboard --install-daemon || echo "  (Daemon already registered)"

# ─── 7. Supervisord + DB init ────────────────────────────────────────────────
echo ""
echo "=== [7/7] Setting up supervisord + initializing DB ==="
cp "$PROJECT_DIR/deploy/supervisord.conf" /etc/supervisor/conf.d/honored.conf
supervisorctl reread
supervisorctl update

# Initialize paper DB with $20 balance
"$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/init_db.py" --balance 20.0
echo "  Paper DB initialized with \$20."

# ─── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════"
echo "  HONORED setup complete!"
echo "════════════════════════════════════════════════════════"
echo ""
echo "  NEXT STEPS (in order):"
echo ""
echo "  1. Verify .env has all API keys:"
echo "     nano $PROJECT_DIR/.env"
echo ""
echo "  2. Link WhatsApp to GOJO (requires phone):"
echo "     openclaw channels login --channel whatsapp"
echo "     → Scan the QR in WhatsApp > Linked Devices"
echo ""
echo "  3. Start all trading agents:"
echo "     supervisorctl start honored:*"
echo "     supervisorctl status honored:*"
echo ""
echo "  4. Tail logs to verify:"
echo "     tail -f /var/log/honored/nanami.out.log"
echo "     tail -f /var/log/honored/toji.out.log"
echo ""
echo "  5. WhatsApp GOJO: 'status' — should reply with live gold price"
echo ""
echo "  MONITORING:"
echo "     supervisorctl status honored:*"
echo "     openclaw gateway status"
echo ""
