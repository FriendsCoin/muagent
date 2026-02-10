<# 
Windows helper: update KEY=VALUE in the bot's server config/.env via SSH and restart services.

Usage (recommended: pass the secret via environment variable so it doesn't land in PS history):
  $env:NEW_KEY="moltbook_sk_..."
  .\trickster-agent\deploy\set_remote_env.ps1 -KeyName MOLTBOOK_API_KEY -KeyValue $env:NEW_KEY

Or inline (less safe, can land in shell history):
  .\trickster-agent\deploy\set_remote_env.ps1 -KeyName MOLTBOOK_API_KEY -KeyValue "moltbook_sk_..."

Defaults assume your Hetzner host + standard install path.
#>

param(
  [string] $ServerIp = "65.21.243.4",
  [string] $SshUser = "root",
  [string] $AppDir = "/opt/trickster-agent/repo/trickster-agent",
  [string] $AppUser = "bot",
  [Parameter(Mandatory=$true)][string] $KeyName,
  [Parameter(Mandatory=$true)][string] $KeyValue
)

if ($KeyName -notmatch '^[A-Z0-9_]+$') {
  throw "Invalid KeyName: $KeyName"
}

if ([string]::IsNullOrWhiteSpace($KeyValue)) {
  throw "KeyValue is empty"
}

# Send a small bash runner via base64 to avoid quoting issues.
$bash = @"
set -euo pipefail
APP_DIR='$AppDir'
APP_USER='$AppUser'
KEY_NAME='$KeyName'
KEY_VALUE='$KeyValue'
cd `"$APP_DIR`"
chmod +x deploy/set_env.sh
APP_DIR=`"$APP_DIR`" APP_USER=`"$APP_USER`" KEY_NAME=`"$KEY_NAME`" KEY_VALUE=`"$KEY_VALUE`" ./deploy/set_env.sh
"@

$bytes = [System.Text.Encoding]::UTF8.GetBytes($bash)
$b64 = [Convert]::ToBase64String($bytes)
$remoteCmd = "echo '$b64' | base64 -d | bash"

Write-Host "Updating $KeyName on $SshUser@$ServerIp ($AppDir) and restarting services..." -ForegroundColor Cyan
ssh "$SshUser@$ServerIp" $remoteCmd

