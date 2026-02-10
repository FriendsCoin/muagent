#!/usr/bin/env bash
set -euo pipefail

# Update KEY=VALUE in config/.env (create if missing) and restart services.
#
# Server-side usage:
#   KEY_NAME="MOLTBOOK_API_KEY" KEY_VALUE="moltbook_sk_..." ./deploy/set_env.sh
#   KEY_NAME="ANTHROPIC_API_KEY" KEY_VALUE="sk-ant-..." ./deploy/set_env.sh
#
# Notes:
# - Avoid putting secrets directly in shell history. Prefer running this from the
#   Windows helper script `deploy/set_remote_env.ps1`.

APP_DIR="${APP_DIR:-/opt/trickster-agent/repo/trickster-agent}"
ENV_FILE="${ENV_FILE:-$APP_DIR/config/.env}"
APP_USER="${APP_USER:-bot}"

KEY_NAME="${KEY_NAME:-}"
KEY_VALUE="${KEY_VALUE:-}"

if [[ -z "$KEY_NAME" ]]; then
  echo "KEY_NAME is required" >&2
  exit 2
fi

if [[ -z "$KEY_VALUE" ]]; then
  echo "KEY_VALUE is required" >&2
  exit 2
fi

mkdir -p "$(dirname "$ENV_FILE")"
touch "$ENV_FILE"

python3 - "$ENV_FILE" "$KEY_NAME" "$KEY_VALUE" <<'PY'
import re
import sys
from pathlib import Path

env_path = Path(sys.argv[1])
key = sys.argv[2].strip()
value = sys.argv[3]

if not re.fullmatch(r"[A-Z0-9_]+", key):
    raise SystemExit(f"Invalid KEY_NAME: {key!r}")

lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
out = []
replaced = False
pat = re.compile(rf"^\s*{re.escape(key)}\s*=")
for line in lines:
    if pat.match(line):
        out.append(f"{key}={value}")
        replaced = True
    else:
        out.append(line)
if not replaced:
    if out and out[-1].strip() != "":
        out.append("")
    out.append(f"{key}={value}")

env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
print(f"OK: set {key} in {env_path}")
PY

chown "$APP_USER:$APP_USER" "$ENV_FILE" || true
chmod 600 "$ENV_FILE" || true

if command -v systemctl >/dev/null 2>&1; then
  systemctl restart trickster-agent || true
  systemctl restart trickster-admin || true
  systemctl restart trickster-thinker || true
  systemctl restart trickster-objkt-worker || true
  echo "Service states:"
  systemctl is-active trickster-agent || true
  systemctl is-active trickster-admin || true
  systemctl is-active trickster-thinker || true
  systemctl is-active trickster-objkt-worker || true
fi

