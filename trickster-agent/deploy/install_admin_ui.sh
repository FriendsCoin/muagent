#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-trickster-admin}"
APP_USER="${APP_USER:-bot}"
ADMIN_HOST="${ADMIN_HOST:-127.0.0.1}"
ADMIN_PORT="${ADMIN_PORT:-8787}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"

cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=Mu Admin UI
After=network.target trickster-agent.service

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/.venv/bin/python scripts/admin_server.py --host $ADMIN_HOST --port $ADMIN_PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
systemctl --no-pager --full status "$SERVICE_NAME" | sed -n '1,18p'
