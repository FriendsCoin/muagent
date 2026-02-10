<# 
Safe server update helper (Windows -> Hetzner).

What it does on the server:
- Stops services (best-effort)
- Stashes any local tracked/untracked changes (so git pull can fast-forward)
- Pulls `origin/main`
- Installs Python deps into `.venv` from `requirements.txt`
- Restarts services (best-effort)

Usage:
  cd E:\PROJECTS\files_molt
  .\trickster-agent\deploy\update_server.ps1
  .\trickster-agent\deploy\update_server.ps1 -ServerIp 1.2.3.4
#>

param(
    [string] $ServerIp = "65.21.243.4",
    [string] $SshUser = "root",
    [string] $RepoUser = "bot",
    [string] $RepoRoot = "/opt/trickster-agent/repo",
    [string] $ProjectDir = "/opt/trickster-agent/repo/trickster-agent"
)

# We ship a bash script via base64 to avoid any quoting/escaping issues with ssh/PowerShell.
$bashScript = @'
set -euo pipefail
REPO_USER="__REPO_USER__"
REPO_ROOT="__REPO_ROOT__"
PROJECT_DIR="__PROJECT_DIR__"
BRANCH="main"

ts="$(date -u +%Y%m%d_%H%M%S)"
backup_root="/opt/trickster-agent/backups"
backup="$backup_root/update_$ts"

mkdir -p "$backup_root" "$backup"

echo "[0/5] stop services (best-effort)"
systemctl stop trickster-agent || true
systemctl stop trickster-admin || true
systemctl stop trickster-thinker || true
systemctl stop trickster-objkt-worker || true

echo "[1/5] backup runtime state (best-effort) -> $backup"
cp -a "$PROJECT_DIR/config/.env" "$backup/" 2>/dev/null || true
cp -a "$PROJECT_DIR/data" "$backup/" 2>/dev/null || true

echo "[2/5] git pull (stash-first) as $REPO_USER in $REPO_ROOT"
sudo -u "$REPO_USER" -H bash -s <<'EOS'
set -euo pipefail
REPO_ROOT="__REPO_ROOT__"
BRANCH="main"
cd "$REPO_ROOT"
git config --global --add safe.directory "$REPO_ROOT" || true
git fetch --all --prune
git checkout "$BRANCH"
# Avoid pull failures due to local edits (incl. accidental tracked runtime files).
git stash push -u -m "auto-pre-pull-__TS__" || true
git pull --ff-only origin "$BRANCH"
EOS

echo "[3/5] ensure venv deps (fastapi/uvicorn/etc)"
if [ ! -x "$PROJECT_DIR/.venv/bin/pip" ]; then
  echo "  .venv missing; creating"
  sudo -u "$REPO_USER" -H bash -lc "cd '$PROJECT_DIR' && python3 -m venv .venv"
fi
sudo -u "$REPO_USER" -H bash -lc "cd '$PROJECT_DIR' && .venv/bin/pip install -U pip && .venv/bin/pip install -r requirements.txt"

echo "[4/5] restore runtime state (best-effort)"
mkdir -p "$PROJECT_DIR/data"
if [ -d "$backup/data" ]; then
  cp -a "$backup/data/." "$PROJECT_DIR/data/" 2>/dev/null || true
fi
if [ -f "$backup/.env" ]; then
  cp -a "$backup/.env" "$PROJECT_DIR/config/.env" 2>/dev/null || true
fi
chown -R "$REPO_USER:$REPO_USER" "$PROJECT_DIR/data" || true
chown "$REPO_USER:$REPO_USER" "$PROJECT_DIR/config/.env" 2>/dev/null || true

echo "[5/5] restart services (best-effort)"
systemctl restart trickster-agent || true
systemctl restart trickster-admin || true
systemctl restart trickster-thinker || true
systemctl restart trickster-objkt-worker || true

echo "Service states:"
systemctl is-active trickster-agent || true
systemctl is-active trickster-admin || true
systemctl is-active trickster-thinker || true
systemctl is-active trickster-objkt-worker || true

echo "Listening ports:"
ss -ltnp | grep -E ':(8787|9898)\\b' || true
'@

$bashScript = $bashScript.Replace("__REPO_USER__", $RepoUser)
$bashScript = $bashScript.Replace("__REPO_ROOT__", $RepoRoot)
$bashScript = $bashScript.Replace("__PROJECT_DIR__", $ProjectDir)
$bashScript = $bashScript.Replace("__TS__", (Get-Date).ToUniversalTime().ToString("yyyyMMdd_HHmmss"))
$bytes = [System.Text.Encoding]::UTF8.GetBytes($bashScript)
$b64 = [Convert]::ToBase64String($bytes)
$remoteCmd = "echo '$b64' | base64 -d | bash"

Write-Host "Running on ${SshUser}@${ServerIp}: update repo + restart services" -ForegroundColor Cyan
ssh "${SshUser}@${ServerIp}" $remoteCmd
