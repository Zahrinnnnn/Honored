#!/bin/bash
# HONORED VPS Setup Script
# Run as root on fresh Ubuntu 22.04
# Usage: bash deploy/setup.sh

set -e

PROJECT_DIR="/opt/honored"
LOG_DIR="/var/log/honored"

echo "=== Installing Node.js 22 ==="
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt-get install -y nodejs

echo "=== Installing Python 3.11 ==="
apt-get install -y python3.11 python3.11-venv python3-pip supervisor

echo "=== Installing OpenClaw ==="
npm install -g openclaw@latest

echo "=== Setting up project ==="
mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR"

echo "=== Creating Python venv ==="
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

echo "=== Setting up OpenClaw workspace ==="
mkdir -p ~/.openclaw/workspace/skills
cp gojo/SOUL.md gojo/AGENTS.md gojo/IDENTITY.md gojo/HEARTBEAT.md ~/.openclaw/workspace/
cp -r gojo/skills/honored-trading ~/.openclaw/workspace/skills/

echo "=== Registering OpenClaw daemon ==="
openclaw onboard --install-daemon

echo "=== Installing supervisord config ==="
cp deploy/supervisord.conf /etc/supervisor/conf.d/honored.conf
supervisorctl reread
supervisorctl update

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env: nano $PROJECT_DIR/.env"
echo "  2. Link WhatsApp: openclaw channels login --channel whatsapp"
echo "  3. Start agents: supervisorctl start honored:*"
echo "  4. Check status: supervisorctl status honored:*"
echo "  5. Check OpenClaw: openclaw gateway status"
