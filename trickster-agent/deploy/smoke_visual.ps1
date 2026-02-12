param(
  [string] $BaseUrl = "http://localhost:8000",
  [string] $AdminToken = "",
  [string] $Prompt = "mu glitch void mirror",
  [string] $Phase = "emergence",
  [int] $Day = 1,
  [switch] $IncludeVideoAudio,
  [string] $OutFile = ""
)

$ErrorActionPreference = "Stop"

function Get-Token {
  param([string]$InputToken)
  if (-not [string]::IsNullOrWhiteSpace($InputToken)) { return $InputToken.Trim() }
  if ($env:TRICKSTER_ADMIN_TOKEN) { return $env:TRICKSTER_ADMIN_TOKEN.Trim() }
  try {
    $stored = [Environment]::GetEnvironmentVariable("TRICKSTER_ADMIN_TOKEN", "User")
    if ($stored) { return $stored.Trim() }
  } catch {}
  return ""
}

function Invoke-JsonPost {
  param(
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)]$Body,
    [Parameter(Mandatory = $false)][string]$Token
  )

  $headers = @{ "Content-Type" = "application/json" }
  if ($Token) { $headers["X-Admin-Token"] = $Token }
  $payload = ($Body | ConvertTo-Json -Depth 8 -Compress)

  try {
    return Invoke-RestMethod -Method Post -Uri $Url -Headers $headers -Body $payload
  } catch {
    $msg = $_.Exception.Message
    if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
      $msg = "$msg`n$($_.ErrorDetails.Message)"
    }
    throw "POST $Url failed: $msg"
  }
}

$token = Get-Token -InputToken $AdminToken
$base = $BaseUrl.TrimEnd("/")

Write-Host "Running visual smoke test against $base" -ForegroundColor Cyan
if ($token) {
  Write-Host "Auth: X-Admin-Token present" -ForegroundColor DarkGray
} else {
  Write-Host "Auth: no token (only works if server has no admin token)" -ForegroundColor Yellow
}

$res = Invoke-JsonPost `
  -Url "$base/api/visual/test_all" `
  -Token $token `
  -Body @{
    prompt = $Prompt
    phase = $Phase
    day = $Day
    include_video_audio = [bool]$IncludeVideoAudio
  }

if (-not $res.ok) {
  throw "Unexpected response from /api/visual/test_all"
}

$rows = @()
foreach ($mode in @("url", "ascii", "audio", "video")) {
  $item = $res.results.$mode
  if ($null -eq $item) {
    $rows += [pscustomobject]@{
      mode = $mode
      kind = "-"
      provider = "-"
      has_url = $false
      has_audio = $false
      note = "missing"
    }
    continue
  }

  $hasUrl = -not [string]::IsNullOrWhiteSpace([string]$item.url)
  if ($mode -in @("audio", "video")) {
    $hasUrl = -not [string]::IsNullOrWhiteSpace([string]$item.base_visual.url)
  }

  $hasAudio = $false
  if ($mode -in @("audio", "video")) {
    $hasAudio = $null -ne $item.audio -and -not [string]::IsNullOrWhiteSpace([string]$item.audio.url)
  }

  $provider = [string]$item.provider
  if ($mode -in @("audio", "video")) {
    $provider = [string]$item.base_visual.provider
  }

  $note = ""
  if ($mode -eq "video") {
    $note = "include_audio=$([bool]$item.include_audio)"
  }

  $rows += [pscustomobject]@{
    mode = $mode
    kind = [string]$item.kind
    provider = $provider
    has_url = $hasUrl
    has_audio = $hasAudio
    note = $note
  }
}

$rows | Format-Table -AutoSize

if ($OutFile) {
  $outPath = Resolve-Path -LiteralPath "." | ForEach-Object { Join-Path $_ $OutFile }
  $res | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $outPath -Encoding UTF8
  Write-Host "Saved full response: $outPath" -ForegroundColor Green
}

Write-Host "Smoke test done." -ForegroundColor Green
