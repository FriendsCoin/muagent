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
sudo -u $RepoUser -H bash -lc 'cd /opt/trickster-agent/repo && git config --global --add safe.directory /opt/trickster-agent/repo && git fetch --all --prune && git checkout main && (git stash push -m pre-pull-tracked || true) && git pull --ff-only origin main && (git stash list | head -1 | grep -q pre-pull-tracked && git stash drop || true)'
echo '[2/4] ensure runtime dirs'
mkdir -p /opt/trickster-agent/repo/trickster-agent/data
chown -R $RepoUser:$RepoUser /opt/trickster-agent/repo/trickster-agent/data
echo '[3/4] restart services'
systemctl restart trickster-agent || true
systemctl restart trickster-admin || true
systemctl restart trickster-thinker || true
echo '[4/4] status'
systemctl is-active trickster-agent || true
systemctl is-active trickster-admin || true
systemctl is-active trickster-thinker || true
"@

Write-Host "Running on ${SshUser}@${ServerIp}: update repo + restart services" -ForegroundColor Cyan
ssh "${SshUser}@${ServerIp}" $remoteCmd
