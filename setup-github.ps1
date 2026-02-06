# GitHub Repository Setup Script
# This script helps you push your project to GitHub

Write-Host "=== GitHub Repository Setup ===" -ForegroundColor Cyan
Write-Host ""

# Check if git is initialized
if (-not (Test-Path ".git")) {
    Write-Host "Error: Git repository not initialized!" -ForegroundColor Red
    exit 1
}

# Check current branch
$currentBranch = git branch --show-current
Write-Host "Current branch: $currentBranch" -ForegroundColor Yellow

# Get repository name from user
$repoName = Read-Host "Enter your GitHub repository name (e.g., 'files_molt' or 'mu-trickster-agent')"
if ([string]::IsNullOrWhiteSpace($repoName)) {
    Write-Host "Repository name cannot be empty!" -ForegroundColor Red
    exit 1
}

# Get GitHub username
$username = Read-Host "Enter your GitHub username"
if ([string]::IsNullOrWhiteSpace($username)) {
    Write-Host "Username cannot be empty!" -ForegroundColor Red
    exit 1
}

# Ask for repository visibility
Write-Host ""
Write-Host "Repository visibility:" -ForegroundColor Yellow
Write-Host "1. Public"
Write-Host "2. Private"
$visibility = Read-Host "Choose (1 or 2) [default: 1]"
if ([string]::IsNullOrWhiteSpace($visibility)) {
    $visibility = "1"
}

$isPrivate = $visibility -eq "2"
$visibilityText = if ($isPrivate) { "private" } else { "public" }

# Ask if they want to use SSH or HTTPS
Write-Host ""
Write-Host "Connection method:" -ForegroundColor Yellow
Write-Host "1. HTTPS (recommended for most users)"
Write-Host "2. SSH (requires SSH keys set up)"
$method = Read-Host "Choose (1 or 2) [default: 1]"
if ([string]::IsNullOrWhiteSpace($method)) {
    $method = "1"
}

if ($method -eq "2") {
    $remoteUrl = "git@github.com:$username/$repoName.git"
} else {
    $remoteUrl = "https://github.com/$username/$repoName.git"
}

Write-Host ""
Write-Host "=== Summary ===" -ForegroundColor Cyan
Write-Host "Repository: $username/$repoName"
Write-Host "Visibility: $visibilityText"
Write-Host "Remote URL: $remoteUrl"
Write-Host ""

$confirm = Read-Host "Continue? (y/n) [default: y]"
if ($confirm -and $confirm.ToLower() -ne "y" -and $confirm.ToLower() -ne "yes") {
    Write-Host "Cancelled." -ForegroundColor Yellow
    exit 0
}

Write-Host ""
Write-Host "=== Step 1: Create GitHub Repository ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Please create the repository on GitHub first:" -ForegroundColor Yellow
Write-Host "1. Go to: https://github.com/new" -ForegroundColor White
Write-Host "2. Repository name: $repoName" -ForegroundColor White
Write-Host "3. Visibility: $visibilityText" -ForegroundColor White
Write-Host "4. DO NOT initialize with README, .gitignore, or license" -ForegroundColor White
Write-Host "5. Click 'Create repository'" -ForegroundColor White
Write-Host ""

$ready = Read-Host "Have you created the repository on GitHub? (y/n)"
if (-not $ready -or ($ready.ToLower() -ne "y" -and $ready.ToLower() -ne "yes")) {
    Write-Host "Please create the repository first, then run this script again." -ForegroundColor Yellow
    exit 0
}

Write-Host ""
Write-Host "=== Step 2: Configure Git User (if needed) ===" -ForegroundColor Cyan
$currentName = git config user.name
$currentEmail = git config user.email

if ($currentName -and $currentEmail) {
    Write-Host "Current git user: $currentName <$currentEmail>" -ForegroundColor Green
    $changeUser = Read-Host "Change git user? (y/n) [default: n]"
    if ($changeUser -and ($changeUser.ToLower() -eq "y" -or $changeUser.ToLower() -eq "yes")) {
        $newName = Read-Host "Enter your name"
        $newEmail = Read-Host "Enter your email"
        if ($newName -and $newEmail) {
            git config user.name $newName
            git config user.email $newEmail
            Write-Host "Git user updated!" -ForegroundColor Green
        }
    }
} else {
    Write-Host "Git user not configured." -ForegroundColor Yellow
    $setUser = Read-Host "Set git user now? (y/n) [default: y]"
    if (-not $setUser -or ($setUser.ToLower() -eq "y" -or $setUser.ToLower() -eq "yes")) {
        $newName = Read-Host "Enter your name"
        $newEmail = Read-Host "Enter your email"
        if ($newName -and $newEmail) {
            git config user.name $newName
            git config user.email $newEmail
            Write-Host "Git user configured!" -ForegroundColor Green
        }
    }
}

Write-Host ""
Write-Host "=== Step 3: Add Remote and Push ===" -ForegroundColor Cyan

# Check if remote already exists
$existingRemote = git remote get-url origin 2>$null
if ($existingRemote) {
    Write-Host "Remote 'origin' already exists: $existingRemote" -ForegroundColor Yellow
    $updateRemote = Read-Host "Update to new URL? (y/n) [default: n]"
    if ($updateRemote -and ($updateRemote.ToLower() -eq "y" -or $updateRemote.ToLower() -eq "yes")) {
        git remote set-url origin $remoteUrl
        Write-Host "Remote updated!" -ForegroundColor Green
    }
} else {
    git remote add origin $remoteUrl
    Write-Host "Remote 'origin' added!" -ForegroundColor Green
}

# Rename branch to main if it's master
if ($currentBranch -eq "master") {
    Write-Host "Renaming branch from 'master' to 'main'..." -ForegroundColor Yellow
    git branch -M main
    $currentBranch = "main"
}

Write-Host ""
Write-Host "Pushing to GitHub..." -ForegroundColor Yellow
Write-Host ""

# Push to GitHub
try {
    git push -u origin $currentBranch
    Write-Host ""
    Write-Host "=== Success! ===" -ForegroundColor Green
    Write-Host "Your repository is now on GitHub:" -ForegroundColor Green
    Write-Host "https://github.com/$username/$repoName" -ForegroundColor Cyan
} catch {
    Write-Host ""
    Write-Host "=== Push Failed ===" -ForegroundColor Red
    Write-Host "You may need to authenticate with GitHub." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "For HTTPS:" -ForegroundColor Yellow
    Write-Host "- Use a Personal Access Token (PAT) as your password" -ForegroundColor White
    Write-Host "- Create one at: https://github.com/settings/tokens" -ForegroundColor White
    Write-Host "- Select 'repo' scope" -ForegroundColor White
    Write-Host ""
    Write-Host "For SSH:" -ForegroundColor Yellow
    Write-Host "- Make sure your SSH key is added to GitHub" -ForegroundColor White
    Write-Host "- Test with: ssh -T git@github.com" -ForegroundColor White
    Write-Host ""
    Write-Host "Then run: git push -u origin $currentBranch" -ForegroundColor Yellow
}
