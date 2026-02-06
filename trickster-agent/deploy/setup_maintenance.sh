#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_DIR="$PROJECT_DIR/deploy"
CRON_FILE="/etc/cron.d/trickster-agent-maintenance"

chmod +x "$DEPLOY_DIR"/update_and_restart.sh
chmod +x "$DEPLOY_DIR"/backup_data.sh
chmod +x "$DEPLOY_DIR"/monitor_service.sh

cat >"$CRON_FILE" <<EOF
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Check service every 5 minutes and restart+alert on failure
*/5 * * * * root SERVICE_NAME=trickster-agent WEBHOOK_URL="\${WEBHOOK_URL:-}" $DEPLOY_DIR/monitor_service.sh >> /var/log/trickster-agent-monitor.log 2>&1

# Daily backup at 03:20 UTC
20 3 * * * root RETENTION_DAYS=14 $DEPLOY_DIR/backup_data.sh >> /var/log/trickster-agent-backup.log 2>&1

# Daily update at 03:45 UTC
45 3 * * * root SERVICE_NAME=trickster-agent APP_USER=bot REPO_BRANCH=main $DEPLOY_DIR/update_and_restart.sh >> /var/log/trickster-agent-update.log 2>&1
EOF

chmod 644 "$CRON_FILE"
echo "Installed cron file: $CRON_FILE"
cat "$CRON_FILE"
