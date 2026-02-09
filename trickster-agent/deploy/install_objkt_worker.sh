#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-trickster-objkt-worker}"
APP_USER="${APP_USER:-bot}"
WORKER_HOST="${OBJKT_WORKER_HOST:-127.0.0.1}"
WORKER_PORT="${OBJKT_WORKER_PORT:-9898}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"

cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=Mu Objkt Mint Worker
After=network.target trickster-agent.service

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/.venv/bin/python scripts/objkt_mint_worker.py --host $WORKER_HOST --port $WORKER_PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
systemctl --no-pager --full status "$SERVICE_NAME" | sed -n '1,18p'
