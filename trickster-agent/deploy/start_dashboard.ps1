<#
One-button launcher (Windows):
- Opens an SSH tunnel window (localhost:8000 -> server:127.0.0.1:8787)
- Opens a Vite UI window (trickster-command: http://localhost:8080)

Usage:
  cd E:\PROJECTS\files_molt
  .\trickster-agent\deploy\start_dashboard.ps1

Notes:
- Keep the tunnel window open. Stop with Ctrl+C.
- The UI may require an admin token (set it in the UI gear Settings, or use ?token=... on the legacy page).
#>

param(
  [string] $ServerIp = "65.21.243.4",
  [string] $SshUser = "root",
  [int] $LocalApiPort = 8000,
  [int] $RemoteApiPort = 8787,
  [int] $UiPort = 8080
)

$ErrorActionPreference = "Stop"

$tunnelScript = Join-Path $PSScriptRoot "tunnel_admin.ps1"
if (-not (Test-Path -LiteralPath $tunnelScript)) {
  throw "Missing script: $tunnelScript"
}

$workspaceRoot = Resolve-Path (Join-Path $PSScriptRoot "..\\..")
$uiDir = Join-Path $workspaceRoot "trickster-command"
if (-not (Test-Path -LiteralPath $uiDir)) {
  throw "Missing UI dir: $uiDir"
}

if (-not (Get-Command "ssh" -ErrorAction SilentlyContinue)) {
  throw "ssh not found. Install Windows OpenSSH client or add it to PATH."
}
if (-not (Get-Command "npm" -ErrorAction SilentlyContinue)) {
  throw "npm not found. Install Node.js (includes npm) and reopen your terminal."
}

Write-Host "Launching tunnel + UI..." -ForegroundColor Cyan

# 1) Tunnel window (keeps running)
Start-Process -FilePath "powershell.exe" -ArgumentList @(
  "-NoExit",
  "-ExecutionPolicy", "Bypass",
  "-File", $tunnelScript,
  "-ServerIp", $ServerIp,
  "-SshUser", $SshUser,
  "-LocalPort", $LocalApiPort,
  "-RemotePort", $RemoteApiPort
)

Start-Sleep -Milliseconds 250

# 2) UI window (vite dev server)
$cmd = "Set-Location -LiteralPath '$uiDir'; npm run dev"
Start-Process -FilePath "powershell.exe" -WorkingDirectory $uiDir -ArgumentList @(
  "-NoExit",
  "-ExecutionPolicy", "Bypass",
  "-Command", $cmd
)

Write-Host ""  # spacer
Write-Host "Open UI:      http://localhost:$UiPort" -ForegroundColor Green
Write-Host "Legacy admin: http://localhost:$LocalApiPort/" -ForegroundColor Green
Write-Host ""  # spacer
Write-Host "If you see 401 Unauthorized in the UI:" -ForegroundColor Yellow
Write-Host "- Click the gear icon (Dashboard Settings) and paste the Admin Token, then Save & Reload." -ForegroundColor Yellow
Write-Host "- Or open the legacy page with token: http://localhost:$LocalApiPort/?token=YOUR_TOKEN" -ForegroundColor Yellow

