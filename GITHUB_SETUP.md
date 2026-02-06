# GitHub Repository Setup Guide

Your project is now initialized as a git repository with an initial commit. 

## Quick Start (Automated)

**Easiest way:** Run the automated setup script:

```powershell
cd e:\PROJECTS\files_molt
.\setup-github.ps1
```

The script will guide you through:
1. Entering your GitHub username and repository name
2. Choosing repository visibility (public/private)
3. Setting up git user configuration
4. Adding the remote and pushing to GitHub

## Manual Setup

If you prefer to do it manually, follow these steps:

## Step 1: Create a GitHub Repository

1. Go to [GitHub](https://github.com) and sign in
2. Click the "+" icon in the top right corner
3. Select "New repository"
4. Choose a repository name (e.g., `files_molt` or `mu-trickster-agent`)
5. **Do NOT** initialize with a README, .gitignore, or license (we already have these)
6. Choose Public or Private
7. Click "Create repository"

## Step 2: Update Git User Configuration (Optional)

If you want to use your own name/email for commits:

```powershell
cd e:\PROJECTS\files_molt
git config user.name "Your Name"
git config user.email "your.email@example.com"
```

Or set globally for all repositories:

```powershell
git config --global user.name "Your Name"
git config --global user.email "your.email@example.com"
```

## Step 3: Add GitHub Remote and Push

After creating the repository on GitHub, you'll see instructions. Use these commands (replace `YOUR_USERNAME` and `REPO_NAME`):

```powershell
cd e:\PROJECTS\files_molt

# Add the remote repository
git remote add origin https://github.com/YOUR_USERNAME/REPO_NAME.git

# Or if using SSH:
# git remote add origin git@github.com:YOUR_USERNAME/REPO_NAME.git

# Rename branch to main (if needed)
git branch -M main

# Push to GitHub
git push -u origin main
```

## Step 4: Verify

Visit your repository on GitHub to confirm all files are uploaded correctly.

## Additional Notes

- The `.gitignore` file is configured to exclude:
  - Python cache files (`__pycache__/`, `*.pyc`)
  - Virtual environments (`venv/`, `env/`)
  - Environment files (`.env` - but `.env.example` is included)
  - Runtime data (`data/*.db`, `data/*.log`, `data/state.json`)
  - IDE files (`.vscode/`, `.idea/`)
  - Test cache files

- If you need to remove files that were accidentally committed:
  ```powershell
  git rm --cached trickster-agent/data/history.db
  git rm --cached trickster-agent/data/state.json
  git commit -m "Remove tracked data files"
  git push
  ```

## Troubleshooting

**Authentication Issues:**
- If you get authentication errors, you may need to set up a Personal Access Token (PAT) or SSH key
- See: https://docs.github.com/en/authentication

**Branch Name:**
- If GitHub uses `main` but your local branch is `master`, rename it:
  ```powershell
  git branch -M main
  ```
