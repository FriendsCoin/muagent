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
  [string] $KeyValue = "",
  [switch] $Prompt
)

if ($KeyName -notmatch '^[A-Z0-9_]+$') {
  throw "Invalid KeyName: $KeyName"
}

function ConvertFrom-SecureStringPlain {
  param([Parameter(Mandatory=$true)][System.Security.SecureString] $Secure)
  $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
  try {
    return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
  } finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
  }
}

if ($Prompt -or [string]::IsNullOrWhiteSpace($KeyValue)) {
  $sec = Read-Host -AsSecureString -Prompt "Enter value for $KeyName"
  $KeyValue = ConvertFrom-SecureStringPlain -Secure $sec
}

if ([string]::IsNullOrWhiteSpace($KeyValue)) {
  throw "KeyValue is empty (use -KeyValue or -Prompt)"
}

# Send a small bash runner via base64 to avoid quoting issues.
$bash = @"
set -euo pipefail
APP_DIR='$AppDir'
APP_USER='$AppUser'
KEY_NAME='$KeyName'
KEY_VALUE='$KeyValue'

ENV_FILE="`$APP_DIR/config/.env"
mkdir -p "`$(dirname "`$ENV_FILE")"
touch "`$ENV_FILE"

python3 - "`$ENV_FILE" "`$KEY_NAME" "`$KEY_VALUE" <<'PY'
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

chown "`$APP_USER:`$APP_USER" "`$ENV_FILE" || true
chmod 600 "`$ENV_FILE" || true

systemctl restart trickster-agent || true
systemctl restart trickster-admin || true
systemctl restart trickster-thinker || true
systemctl restart trickster-objkt-worker || true

echo "Service states:"
systemctl is-active trickster-agent || true
systemctl is-active trickster-admin || true
systemctl is-active trickster-thinker || true
systemctl is-active trickster-objkt-worker || true
"@

$bytes = [System.Text.Encoding]::UTF8.GetBytes($bash)
$b64 = [Convert]::ToBase64String($bytes)
$remoteCmd = "echo '$b64' | base64 -d | bash"

Write-Host "Updating $KeyName on $SshUser@$ServerIp ($AppDir) and restarting services..." -ForegroundColor Cyan
ssh "$SshUser@$ServerIp" $remoteCmd
