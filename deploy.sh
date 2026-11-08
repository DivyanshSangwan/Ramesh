#!/usr/bin/env bash
# Deploy Ramesh to a remote Vultr instance via rsync and Docker Compose.
set -euo pipefail

if [ "$#" -lt 1 ]; then
    echo "Usage: ./deploy.sh <VULTR_IP> [DOMAIN]"
    echo "Example (IP only):  ./deploy.sh 149.28.xx.xx"
    echo "Example (HTTPS):    ./deploy.sh 149.28.xx.xx ramesh.example.com"
    exit 1
fi

VULTR_IP="$1"
DOMAIN="${2:-:80}"

# Load local GEMINI_API_KEY from .env if not already exported in shell.
if [ -z "${GEMINI_API_KEY:-}" ] && [ -f .env ]; then
    GEMINI_API_KEY=$(grep -E '^GEMINI_API_KEY=' .env | cut -d '=' -f2- | tr -d '"\r' || true)
fi

echo "==> 1. Syncing project files to root@${VULTR_IP}:/opt/ramesh ..."
rsync -avz --delete \
    --exclude '.venv' \
    --exclude '.git' \
    --exclude '.aws' \
    --exclude '.codex' \
    --exclude '.agents' \
    --exclude 'node_modules' \
    --exclude 'frontend/node_modules' \
    --exclude 'frontend/test-results' \
    --exclude 'frontend/playwright-report' \
    --exclude '.env' \
    --exclude 'generated_models' \
    --exclude '__pycache__' \
    ./ "root@${VULTR_IP}:/opt/ramesh/"

echo "==> 2. Building and starting containers on ${VULTR_IP} ..."
ssh "root@${VULTR_IP}" "bash -s" <<EOF
set -euo pipefail
cd /opt/ramesh

# Install Docker & Compose plugin if not already installed
if ! command -v docker >/dev/null 2>&1; then
    apt-get update
    apt-get install -y docker.io docker-compose-v2
    systemctl enable --now docker
fi

# Write server environment configuration
cat > .env <<ENVEOF
GEMINI_API_KEY=${GEMINI_API_KEY:-}
GEMINI_MODEL=gemini-3.5-flash-lite
GEMINI_ANALYSIS_MODEL=gemini-3.5-flash-lite
GEMINI_FALLBACK_MODEL=gemini-3.8-flash
DOMAIN=${DOMAIN}
ENVEOF
chmod 600 .env

# Open HTTP/HTTPS firewall ports if ufw is active
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
    ufw allow 80/tcp
    ufw allow 443/tcp
fi

# Build and start services
if docker compose version >/dev/null 2>&1; then
    docker compose up -d --build
else
    docker-compose up -d --build
fi
EOF

if [ "$DOMAIN" = ":80" ]; then
    echo "==> Deployment complete! Open: http://${VULTR_IP}"
else
    echo "==> Deployment complete! Open: https://${DOMAIN}"
fi

