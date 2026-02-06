#!/usr/bin/env bash
set -euo pipefail

# Full server bootstrap for Ubuntu.
# Run as root on a fresh VPS.
#
# Required env vars:
#   REPO_URL
#   MOLTBOOK_API_KEY
#   ANTHROPIC_API_KEY
#
# Optional env vars:
#   APP_USER=bot
#   APP_ROOT=/opt/trickster-agent
#   REPO_BRANCH=main
#   PROJECT_SUBDIR=trickster-agent
#   SERVICE_NAME=trickster-agent
#   RUNWARE_API_KEY=
#   COMFYUI_API_KEY=
#   VASTAI_API_KEY=
#   ADMIN_TOKEN=

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "ERROR: required env var is missing: $name" >&2
    exit 1
  fi
}

require_env "REPO_URL"
require_env "MOLTBOOK_API_KEY"
require_env "ANTHROPIC_API_KEY"

APP_USER="${APP_USER:-bot}"
APP_ROOT="${APP_ROOT:-/opt/trickster-agent}"
REPO_BRANCH="${REPO_BRANCH:-main}"
PROJECT_SUBDIR="${PROJECT_SUBDIR:-trickster-agent}"
SERVICE_NAME="${SERVICE_NAME:-trickster-agent}"
REPO_DIR="$APP_ROOT/repo"
PROJECT_DIR="$REPO_DIR/$PROJECT_SUBDIR"
ENV_FILE="$PROJECT_DIR/config/.env"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"

if [[ "$EUID" -ne 0 ]]; then
  echo "ERROR: run this script as root." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y git python3 python3-venv python3-pip ca-certificates

if ! id "$APP_USER" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "$APP_USER"
fi

mkdir -p "$APP_ROOT"
chown -R "$APP_USER:$APP_USER" "$APP_ROOT"

if ! git ls-remote --exit-code --heads "$REPO_URL" "$REPO_BRANCH" >/dev/null 2>&1; then
  echo "ERROR: branch '$REPO_BRANCH' not found in $REPO_URL" >&2
  echo "Available branches:" >&2
  git ls-remote --heads "$REPO_URL" | awk '{print $2}' | sed 's#refs/heads/##' >&2
  exit 1
fi

if [[ -d "$REPO_DIR/.git" ]]; then
  sudo -u "$APP_USER" git -C "$REPO_DIR" fetch --all --prune
  sudo -u "$APP_USER" git -C "$REPO_DIR" checkout "$REPO_BRANCH"
  sudo -u "$APP_USER" git -C "$REPO_DIR" pull --ff-only origin "$REPO_BRANCH"
else
  sudo -u "$APP_USER" git clone --branch "$REPO_BRANCH" "$REPO_URL" "$REPO_DIR"
fi

if [[ ! -f "$PROJECT_DIR/main.py" ]]; then
  echo "ERROR: project entrypoint not found: $PROJECT_DIR/main.py" >&2
  exit 1
fi

sudo -u "$APP_USER" python3 -m venv "$PROJECT_DIR/.venv"
sudo -u "$APP_USER" "$PROJECT_DIR/.venv/bin/python" -m pip install --upgrade pip
sudo -u "$APP_USER" "$PROJECT_DIR/.venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

install -d -m 755 -o "$APP_USER" -g "$APP_USER" "$(dirname "$ENV_FILE")"
cat >"$ENV_FILE" <<EOF
MOLTBOOK_API_KEY=$MOLTBOOK_API_KEY
ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
RUNWARE_API_KEY=${RUNWARE_API_KEY:-}
COMFYUI_API_KEY=${COMFYUI_API_KEY:-}
VASTAI_API_KEY=${VASTAI_API_KEY:-}
ADMIN_TOKEN=${ADMIN_TOKEN:-}
EOF
chown "$APP_USER:$APP_USER" "$ENV_FILE"
chmod 600 "$ENV_FILE"

install -d -m 755 -o "$APP_USER" -g "$APP_USER" "$PROJECT_DIR/data"

cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=Mu Trickster Agent
After=network.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/.venv/bin/python main.py --daemon
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

echo
echo "Deployment complete."
echo "Service status:"
systemctl --no-pager --full status "$SERVICE_NAME" | sed -n '1,20p'
echo
echo "Tail logs:"
echo "  journalctl -u $SERVICE_NAME -f"
