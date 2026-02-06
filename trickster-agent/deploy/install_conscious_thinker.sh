#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-trickster-thinker}"
APP_USER="${APP_USER:-bot}"
INTERVAL_MINUTES="${INTERVAL_MINUTES:-45}"
CONSCIOUS_DIR="${CONSCIOUS_DIR:-}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"

if [[ -z "$CONSCIOUS_DIR" ]]; then
  CONSCIOUS_DIR="$PROJECT_DIR/NEW/conscious-claude-master"
fi

cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=Mu Conscious Thinker
After=network.target trickster-agent.service

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/.venv/bin/python scripts/conscious_thinker.py --interval-minutes $INTERVAL_MINUTES --conscious-dir $CONSCIOUS_DIR
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
systemctl --no-pager --full status "$SERVICE_NAME" | sed -n '1,18p'
