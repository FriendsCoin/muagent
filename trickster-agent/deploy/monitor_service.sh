#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-trickster-agent}"
WEBHOOK_URL="${WEBHOOK_URL:-}"
HOSTNAME_SHORT="$(hostname -s)"

if systemctl is-active --quiet "$SERVICE_NAME"; then
  exit 0
fi

msg="[$HOSTNAME_SHORT] $SERVICE_NAME is down on $(date -u +%Y-%m-%dT%H:%M:%SZ). Restarting."
echo "$msg"
systemctl restart "$SERVICE_NAME"

if [[ -n "$WEBHOOK_URL" ]]; then
  payload="$(printf '{"text":"%s"}' "$msg")"
  curl -sS -X POST -H "Content-Type: application/json" -d "$payload" "$WEBHOOK_URL" >/dev/null || true
fi
