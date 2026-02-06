param(
    [Parameter(Mandatory = $true)]
    [string]$ServerIp,

    [string]$SshUser = "root",

    [string]$RemoteProjectDir = "/opt/trickster-agent/repo/trickster-agent",

    [string]$LocalProjectDir = ""
)

if (-not $LocalProjectDir) {
    $LocalProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

$localState = Join-Path $LocalProjectDir "data\state.json"
$localHistory = Join-Path $LocalProjectDir "data\history.db"
$target = "$SshUser@$ServerIp"
$remoteData = "$RemoteProjectDir/data"

if (-not (Test-Path $localState)) {
    throw "Local state.json not found: $localState"
}
if (-not (Test-Path $localHistory)) {
    throw "Local history.db not found: $localHistory"
}

Write-Host "Stopping remote service..."
ssh $target "systemctl stop trickster-agent"

Write-Host "Uploading state/history..."
scp $localState "${target}:$remoteData/state.json"
scp $localHistory "${target}:$remoteData/history.db"

Write-Host "Fixing ownership and restarting..."
ssh $target "chown bot:bot $remoteData/state.json $remoteData/history.db && systemctl start trickster-agent && systemctl status trickster-agent --no-pager"

Write-Host "Done."
