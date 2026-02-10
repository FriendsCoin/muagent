<#
Open an SSH tunnel from Windows -> Hetzner so the local dashboard can talk to the server admin API.

Default setup:
- Server admin API listens on: 127.0.0.1:8787 (server)
- Local dashboard expects:     http://localhost:8000

So we forward:
  localhost:8000 -> server:127.0.0.1:8787

Usage:
  cd E:\PROJECTS\files_molt
  .\trickster-agent\deploy\tunnel_admin.ps1

Then:
  1) In another terminal: cd .\trickster-command; npm run dev
  2) Open: http://localhost:8080

Stop the tunnel: Ctrl+C in the tunnel terminal.
#>

param(
  [string] $ServerIp = "65.21.243.4",
  [string] $SshUser = "root",
  [int] $LocalPort = 8000,
  [int] $RemotePort = 8787
)

$target = "${SshUser}@${ServerIp}"
$spec = "${LocalPort}:127.0.0.1:${RemotePort}"
Write-Host "Starting SSH tunnel: localhost:$LocalPort -> ${target}:127.0.0.1:$RemotePort" -ForegroundColor Cyan
Write-Host "Keep this window open. Stop with Ctrl+C." -ForegroundColor DarkGray

ssh -N -L $spec $target
