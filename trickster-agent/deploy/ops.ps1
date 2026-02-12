<#
Unified ops helper for trickster-agent.

Usage:
  cd E:\PROJECTS\files_molt
  .\trickster-agent\deploy\ops.ps1 -Action help

Common:
  .\trickster-agent\deploy\ops.ps1 -Action status
  .\trickster-agent\deploy\ops.ps1 -Action backup
  .\trickster-agent\deploy\ops.ps1 -Action update
  .\trickster-agent\deploy\ops.ps1 -Action restart
  .\trickster-agent\deploy\ops.ps1 -Action rotate-moltbook-key
  .\trickster-agent\deploy\ops.ps1 -Action dashboard
#>

param(
  [ValidateSet("help","status","restart","backup","update","tunnel","dashboard","set-key","rotate-moltbook-key","quiet-on","quiet-off","writes-on","writes-off","smoke-visual","visual-image-first","visual-video-first","visual-safe-fallback")]
  [string] $Action = "help",
  [string] $ServerIp = "65.21.243.4",
  [string] $SshUser = "root",
  [switch] $InstallPlaywright,
  [string] $KeyName = "",
  [string] $KeyValue = "",
  [switch] $PromptKey,
  [string] $Prompt = "mu glitch void mirror",
  [string] $AdminToken = "",
  [switch] $IncludeVideoAudio,
  [string] $Modes = ""
)

$ErrorActionPreference = "Stop"
$deployDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Invoke-RemoteBash {
  param(
    [Parameter(Mandatory=$true)][string] $ScriptText
  )
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($ScriptText)
  $b64 = [Convert]::ToBase64String($bytes)
  $cmd = "echo '$b64' | base64 -d | bash"
  ssh "$SshUser@$ServerIp" $cmd
}

function Show-Help {
  Write-Host "Actions:" -ForegroundColor Cyan
  Write-Host "  help                  Show this help"
  Write-Host "  status                Server service + port status"
  Write-Host "  backup                Create manual server backup tar.gz"
  Write-Host "  update                Run deploy/update_server.ps1"
  Write-Host "  restart               Restart server services"
  Write-Host "  tunnel                Run deploy/tunnel_admin.ps1"
  Write-Host "  dashboard             Run deploy/start_dashboard.ps1"
  Write-Host "  set-key               Set any KEY in server config/.env"
  Write-Host "  rotate-moltbook-key   Prompt + set MOLTBOOK_API_KEY"
  Write-Host "  quiet-on              Suppress post/upvote and rate-limit comments"
  Write-Host "  quiet-off             Disable quiet mode"
  Write-Host "  writes-on             Enable real writes to Moltbook API"
  Write-Host "  writes-off            Disable writes (simulation mode)"
  Write-Host "  smoke-visual          Local smoke test url/ascii/audio/video via admin API"
  Write-Host "  visual-image-first    Set visual preset: image-first"
  Write-Host "  visual-video-first    Set visual preset: video-first"
  Write-Host "  visual-safe-fallback  Set visual preset: safe-fallback"
  Write-Host ""
  Write-Host "Examples:" -ForegroundColor Cyan
  Write-Host "  .\trickster-agent\deploy\ops.ps1 -Action status"
  Write-Host "  .\trickster-agent\deploy\ops.ps1 -Action backup"
  Write-Host "  .\trickster-agent\deploy\ops.ps1 -Action update -InstallPlaywright"
  Write-Host "  .\trickster-agent\deploy\ops.ps1 -Action set-key -KeyName MOLTBOOK_API_KEY -PromptKey"
  Write-Host "  .\trickster-agent\deploy\ops.ps1 -Action quiet-on"
  Write-Host "  .\trickster-agent\deploy\ops.ps1 -Action smoke-visual -Prompt `"mu mirror fracture`""
  Write-Host "  .\trickster-agent\deploy\ops.ps1 -Action smoke-visual -Prompt `"mu mirror fracture`" -Modes `"video`""
  Write-Host "  .\trickster-agent\deploy\ops.ps1 -Action visual-safe-fallback"
}

switch ($Action) {
  "help" {
    Show-Help
    break
  }

  "status" {
    $script = @'
set -euo pipefail
echo "== services =="
systemctl is-active trickster-agent || true
systemctl is-active trickster-admin || true
systemctl is-active trickster-thinker || true
systemctl is-active trickster-objkt-worker || true
echo
echo "== listening ports =="
ss -ltnp | grep -E ":(8787|9898)\b" || true
echo
echo "== recent logs =="
journalctl -u trickster-agent -n 20 --no-pager || true
echo
journalctl -u trickster-admin -n 20 --no-pager || true
'@
    Invoke-RemoteBash -ScriptText $script
    break
  }

  "backup" {
    $script = @'
set -euo pipefail
ts="$(date -u +%Y%m%d_%H%M%S)"
backup_dir="/opt/trickster-agent/backups"
mkdir -p "$backup_dir"
archive="$backup_dir/manual_$ts.tar.gz"
tar -czf "$archive" \
  -C /opt/trickster-agent/repo \
  trickster-agent/config/.env \
  trickster-agent/data || true
echo "Backup: $archive"
ls -lh "$archive" || true
'@
    Invoke-RemoteBash -ScriptText $script
    break
  }

  "update" {
    $scriptPath = Join-Path $deployDir "update_server.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath)) {
      throw "Missing script: $scriptPath"
    }
    if ($InstallPlaywright) {
      & $scriptPath -ServerIp $ServerIp -SshUser $SshUser -InstallPlaywright
    } else {
      & $scriptPath -ServerIp $ServerIp -SshUser $SshUser
    }
    break
  }

  "restart" {
    $script = @'
set -euo pipefail
systemctl restart trickster-agent || true
systemctl restart trickster-admin || true
systemctl restart trickster-thinker || true
systemctl restart trickster-objkt-worker || true
echo "== service states =="
systemctl is-active trickster-agent || true
systemctl is-active trickster-admin || true
systemctl is-active trickster-thinker || true
systemctl is-active trickster-objkt-worker || true
'@
    Invoke-RemoteBash -ScriptText $script
    break
  }

  "tunnel" {
    $scriptPath = Join-Path $deployDir "tunnel_admin.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath)) {
      throw "Missing script: $scriptPath"
    }
    & $scriptPath -ServerIp $ServerIp -SshUser $SshUser
    break
  }

  "dashboard" {
    $scriptPath = Join-Path $deployDir "start_dashboard.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath)) {
      throw "Missing script: $scriptPath"
    }
    & $scriptPath -ServerIp $ServerIp -SshUser $SshUser
    break
  }

  "set-key" {
    if ([string]::IsNullOrWhiteSpace($KeyName)) {
      throw "set-key requires -KeyName"
    }
    $scriptPath = Join-Path $deployDir "set_remote_env.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath)) {
      throw "Missing script: $scriptPath"
    }
    if ($PromptKey -or [string]::IsNullOrWhiteSpace($KeyValue)) {
      & $scriptPath -ServerIp $ServerIp -SshUser $SshUser -KeyName $KeyName -Prompt
    } else {
      & $scriptPath -ServerIp $ServerIp -SshUser $SshUser -KeyName $KeyName -KeyValue $KeyValue
    }
    break
  }

  "rotate-moltbook-key" {
    $scriptPath = Join-Path $deployDir "rotate_moltbook_key.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath)) {
      throw "Missing script: $scriptPath"
    }
    & $scriptPath -ServerIp $ServerIp -SshUser $SshUser
    break
  }

  "quiet-on" {
    $script = @'
set -euo pipefail
db="/opt/trickster-agent/repo/trickster-agent/data/history.db"
sqlite3 "$db" "
INSERT INTO control_flags(key,value,updated_at) VALUES
('quiet_mode','1',datetime('now')),
('quiet_comment_cooldown_hours','6.0',datetime('now'))
ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at;
"
echo "quiet_mode=1, quiet_comment_cooldown_hours=6.0"
'@
    Invoke-RemoteBash -ScriptText $script
    break
  }

  "quiet-off" {
    $script = @'
set -euo pipefail
db="/opt/trickster-agent/repo/trickster-agent/data/history.db"
sqlite3 "$db" "
INSERT INTO control_flags(key,value,updated_at) VALUES
('quiet_mode','0',datetime('now'))
ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at;
"
echo "quiet_mode=0"
'@
    Invoke-RemoteBash -ScriptText $script
    break
  }

  "writes-on" {
    $script = @'
set -euo pipefail
db="/opt/trickster-agent/repo/trickster-agent/data/history.db"
sqlite3 "$db" "
INSERT INTO control_flags(key,value,updated_at) VALUES
('moltbook_write_enabled','1',datetime('now'))
ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at;
"
echo "moltbook_write_enabled=1"
'@
    Invoke-RemoteBash -ScriptText $script
    break
  }

  "writes-off" {
    $script = @'
set -euo pipefail
db="/opt/trickster-agent/repo/trickster-agent/data/history.db"
sqlite3 "$db" "
INSERT INTO control_flags(key,value,updated_at) VALUES
('moltbook_write_enabled','0',datetime('now'))
ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at;
"
echo "moltbook_write_enabled=0"
'@
    Invoke-RemoteBash -ScriptText $script
    break
  }

  "smoke-visual" {
    $scriptPath = Join-Path $deployDir "smoke_visual.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath)) {
      throw "Missing script: $scriptPath"
    }
    if ($IncludeVideoAudio) {
      & $scriptPath -Prompt $Prompt -AdminToken $AdminToken -IncludeVideoAudio -Modes $Modes
    } else {
      & $scriptPath -Prompt $Prompt -AdminToken $AdminToken -Modes $Modes
    }
    break
  }

  "visual-image-first" {
    $script = @'
set -euo pipefail
db="/opt/trickster-agent/repo/trickster-agent/data/history.db"
sqlite3 "$db" "
INSERT INTO control_flags(key,value,updated_at) VALUES
('visual_enabled','1',datetime('now')),
('visual_mode','url',datetime('now')),
('visual_url_provider','pollinations',datetime('now')),
('visual_fallback_provider','pollinations',datetime('now')),
('visual_video_provider','pollinations',datetime('now')),
('visual_video_include_audio','0',datetime('now'))
ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at;
"
echo "Applied preset: image-first"
'@
    Invoke-RemoteBash -ScriptText $script
    break
  }

  "visual-video-first" {
    $script = @'
set -euo pipefail
db="/opt/trickster-agent/repo/trickster-agent/data/history.db"
sqlite3 "$db" "
INSERT INTO control_flags(key,value,updated_at) VALUES
('visual_enabled','1',datetime('now')),
('visual_mode','video',datetime('now')),
('visual_url_provider','pollinations',datetime('now')),
('visual_fallback_provider','pollinations',datetime('now')),
('visual_video_provider','pollinations',datetime('now')),
('visual_video_model','seedance-pro',datetime('now')),
('visual_video_include_audio','0',datetime('now'))
ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at;
"
echo "Applied preset: video-first"
'@
    Invoke-RemoteBash -ScriptText $script
    break
  }

  "visual-safe-fallback" {
    $script = @'
set -euo pipefail
db="/opt/trickster-agent/repo/trickster-agent/data/history.db"
sqlite3 "$db" "
INSERT INTO control_flags(key,value,updated_at) VALUES
('visual_enabled','1',datetime('now')),
('visual_mode','auto',datetime('now')),
('visual_url_provider','pollinations',datetime('now')),
('visual_fallback_provider','ascii',datetime('now')),
('visual_video_provider','pollinations',datetime('now')),
('visual_video_model','seedance-pro',datetime('now')),
('visual_video_include_audio','0',datetime('now'))
ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at;
"
echo "Applied preset: safe-fallback"
'@
    Invoke-RemoteBash -ScriptText $script
    break
  }
}
