# Commit and push updates to GitHub
# Run from project root: .\git-push-updates.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Check for changes
$status = git status --porcelain
if (-not $status) {
    Write-Host "Nothing to commit. Working tree clean." -ForegroundColor Green
    exit 0
}

Write-Host "Changes to be committed:" -ForegroundColor Cyan
git status -s
Write-Host ""

# Optional: custom message as first argument
$msg = $args[0]
if (-not $msg) {
    $msg = Read-Host "Commit message (or press Enter for 'Updates')"
    if ([string]::IsNullOrWhiteSpace($msg)) {
        $msg = "Updates"
    }
}

git add -A
git commit -m $msg
git push

Write-Host "Done. Pushed to GitHub." -ForegroundColor Green
