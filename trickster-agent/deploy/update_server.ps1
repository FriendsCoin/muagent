# Safe server update helper (Windows -> Hetzner).
# Runs git pull as repo user, preserves runtime data files, and restarts services.
# Usage:
#   .\trickster-agent\deploy\update_server.ps1
#   .\trickster-agent\deploy\update_server.ps1 -ServerIp 1.2.3.4

param(
    [string] $ServerIp = "65.21.243.4",
    [string] $SshUser = "root",
    [string] $RepoUser = "bot"
)

$remoteCmd = @"
set -euo pipefail
echo '[1/4] git fetch/pull as $RepoUser'
sudo -u $RepoUser -H bash -lc '
set -euo pipefail
cd /opt/trickster-agent/repo
git config --global --add safe.directory /opt/trickster-agent/repo

ts=\$(date +%Y%m%d_%H%M%S)
backup=/opt/trickster-agent/backups/gitupdate_\$ts
mkdir -p \"\$backup\" \"\$backup/conflicts\" \"\$backup/data\"

# Preserve runtime state even if we need to stash/clean.
cp -a trickster-agent/data/history.db \"\$backup/data/\" 2>/dev/null || true
cp -a trickster-agent/data/state.json \"\$backup/data/\" 2>/dev/null || true
cp -a trickster-agent/config/.env \"\$backup/\" 2>/dev/null || true

git fetch --all --prune
git checkout main

# If a file is tracked in origin/main but exists locally as untracked, git pull aborts.
# Move such conflicts aside so we can fast-forward cleanly.
while IFS= read -r f; do
  if [ -e \"\$f\" ] && ! git ls-files --error-unmatch \"\$f\" >/dev/null 2>&1; then
    mkdir -p \"\$backup/conflicts/\$(dirname \"\$f\")\"
    mv \"\$f\" \"\$backup/conflicts/\$f\"
  fi
done < <(git ls-tree -r --name-only origin/main)

if ! git pull --ff-only origin main; then
  # Includes untracked; do NOT auto-drop (keeps a forensics trail if anything weird happens).
  git stash push -u -m \"pre-pull-\$ts\" || true
  git pull --ff-only origin main
fi

# Restore runtime state if it was stashed away / overwritten.
if [ -f \"\$backup/data/history.db\" ]; then cp -a \"\$backup/data/history.db\" trickster-agent/data/history.db; fi
if [ -f \"\$backup/data/state.json\" ]; then cp -a \"\$backup/data/state.json\" trickster-agent/data/state.json; fi
'
echo '[2/4] ensure runtime dirs'
mkdir -p /opt/trickster-agent/repo/trickster-agent/data
chown -R ${RepoUser}:$RepoUser /opt/trickster-agent/repo/trickster-agent/data
echo '[3/4] restart services'
systemctl restart trickster-agent || true
systemctl restart trickster-admin || true
systemctl restart trickster-thinker || true
systemctl restart trickster-objkt-worker || true
echo '[4/4] status'
systemctl is-active trickster-agent || true
systemctl is-active trickster-admin || true
systemctl is-active trickster-thinker || true
systemctl is-active trickster-objkt-worker || true
"@

Write-Host "Running on ${SshUser}@${ServerIp}: update repo + restart services" -ForegroundColor Cyan
ssh "${SshUser}@${ServerIp}" $remoteCmd
