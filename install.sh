#!/usr/bin/env bash
#### ClawbotCore — Install script for Debian/Armbian
#### Installs the AI orchestrator, WhatsApp bridge, and all dependencies.
#### Run as root:
####   curl -fsSL https://raw.githubusercontent.com/Yumi-Lab/clawbot-core/main/install.sh | bash
#### Or: bash install.sh after git clone

set -euo pipefail

REPO="https://github.com/Yumi-Lab/clawbot-core.git"
INSTALL_DIR="/usr/local/lib/clawbot-core"
CONFIG_DIR="/etc/clawbot"
DATA_DIR="/home/pi/.clawbot"
SERVICE_USER="pi"

echo "==> Installing ClawbotCore..."

# ── 1. System packages ──────────────────────────────────────────────────────
apt-get update -q
apt-get install -y --no-install-recommends \
    python3 python3-venv \
    git curl wget \
    sshpass \
    jq

# ── 2. Node.js 18+ (for WhatsApp bridge) ────────────────────────────────────
if ! command -v node &>/dev/null || [[ $(node -v | grep -oP '\d+' | head -1) -lt 18 ]]; then
    echo "==> Installing Node.js 20..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y nodejs
fi

# ── 3. Clone / update repo ──────────────────────────────────────────────────
if [[ -d "${INSTALL_DIR}/.git" ]]; then
    echo "==> Updating existing install..."
    git -C "${INSTALL_DIR}" pull --ff-only
else
    git clone --depth=1 "${REPO}" "${INSTALL_DIR}"
fi

# ── 4. Python — stdlib only, no venv ─────────────────────────────────────────
# ClawbotCore uses only Python standard library.
# The only optional dependency is websockets (for cloud tunnel):
pip3 install --quiet --break-system-packages websockets 2>/dev/null || \
    apt-get install -y python3-websockets 2>/dev/null || true

# ── 5. WhatsApp bridge (Node.js) ────────────────────────────────────────────
WA_DIR="${INSTALL_DIR}/modules/whatsapp-bridge"
if [[ -f "${WA_DIR}/package.json" ]]; then
    echo "==> Installing WhatsApp bridge dependencies..."
    cd "${WA_DIR}" && npm install --production
fi

# ── 6. Config directories ───────────────────────────────────────────────────
mkdir -p "${CONFIG_DIR}/conf.d"
mkdir -p "${DATA_DIR}/workspace"

# Default config if not present
if [[ ! -f "${CONFIG_DIR}/clawbot.cfg" ]]; then
    cat > "${CONFIG_DIR}/clawbot.cfg" << 'EOF'
[server]
host: 0.0.0.0
port: 8090

[llm]
default_model = kimi-for-coding
provider = kimi
base_url = https://api.moonshot.cn/v1

[whatsapp]
mode = personal
admins =
allow_from = *
blacklist =
default_model = default
default_mode = core
vault_2fa = off
user_tools = web_search
EOF
fi

# Default JSON config if not present
if [[ ! -f "${DATA_DIR}/config.json" ]]; then
    cat > "${DATA_DIR}/config.json" << 'EOF'
{
  "gateway": {"host": "0.0.0.0", "port": 8080},
  "agents": {"defaults": {"model": "default", "workspace": "~/.clawbot/workspace",
                           "max_tokens": 4096, "temperature": 0.7}},
  "model_list": [{}],
  "tools": {"web": {"duckduckgo": {"enabled": true, "max_results": 5}}},
  "log_level": "info"
}
EOF
fi

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${DATA_DIR}" 2>/dev/null || true

# ── 7. Systemd services ─────────────────────────────────────────────────────
cat > /etc/systemd/system/clawbot-core.service << EOF
[Unit]
Description=ClawbotCore AI Orchestrator
After=network.target

[Service]
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/clawbot_core/main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/whatsapp-bridge.service << EOF
[Unit]
Description=WhatsApp Bridge (Baileys)
After=network.target clawbot-core.service

[Service]
User=${SERVICE_USER}
WorkingDirectory=${WA_DIR}
ExecStart=/usr/bin/node ${WA_DIR}/bridge.js
Restart=on-failure
RestartSec=10
Environment=PORT=3100
Environment=CORE_URL=http://127.0.0.1:8090

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --quiet clawbot-core
systemctl restart clawbot-core

echo ""
echo "==> ClawbotCore installed!"
echo ""
echo "  Config:     ${CONFIG_DIR}/clawbot.cfg"
echo "  Data:       ${DATA_DIR}/"
echo "  API:        http://localhost:8090"
echo "  Health:     curl http://localhost:8090/core/health"
echo ""
echo "  Start WhatsApp bridge:"
echo "    systemctl enable --now whatsapp-bridge"
