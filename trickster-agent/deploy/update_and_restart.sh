#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-trickster-agent}"
APP_USER="${APP_USER:-bot}"
REPO_BRANCH="${REPO_BRANCH:-main}"

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO_ROOT="$(cd "$PROJECT_DIR/.." && pwd)"
VENV_PIP="$PROJECT_DIR/.venv/bin/pip"

echo "[update] repo root: $REPO_ROOT"
echo "[update] branch: $REPO_BRANCH"

runuser -u "$APP_USER" -- git -C "$REPO_ROOT" fetch origin "$REPO_BRANCH"
runuser -u "$APP_USER" -- git -C "$REPO_ROOT" checkout "$REPO_BRANCH"
runuser -u "$APP_USER" -- git -C "$REPO_ROOT" pull --ff-only origin "$REPO_BRANCH"

runuser -u "$APP_USER" -- "$VENV_PIP" install -r "$PROJECT_DIR/requirements.txt"

systemctl restart "$SERVICE_NAME"
systemctl --no-pager --full status "$SERVICE_NAME" | sed -n '1,18p'
