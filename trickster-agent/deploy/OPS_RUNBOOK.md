# Mu Ops Runbook

This is the single operational guide for day-to-day work with:
- server bot (`trickster-agent`) on Hetzner
- legacy admin API/UI (`http://localhost:8000/` via tunnel)
- new React dashboard (`trickster-command`, `http://localhost:8080/`)

Use this file to avoid terminal confusion.

## 1) Terminal map

Use exactly these terminals:

1. `Terminal A` (Windows PowerShell): SSH tunnel
2. `Terminal B` (Windows PowerShell): React dashboard (`npm run dev`)
3. `Terminal C` (server SSH): optional, for service checks/restart

You usually do not need more than A+B.

## 2) Fast start (recommended)

From Windows:

```powershell
cd E:\PROJECTS\files_molt
.\trickster-agent\deploy\start_dashboard.ps1
```

This opens two windows automatically:
- tunnel (`localhost:8000 -> server:8787`)
- React UI (`localhost:8080`)

Open:
- New UI: `http://localhost:8080`
- Legacy admin: `http://localhost:8000/`

## 3) Full restart from zero

If things are confusing/broken, do this in order:

1. Close old tunnel and old `npm run dev` windows.
2. SSH to server and restart services:

```bash
systemctl restart trickster-agent trickster-admin trickster-thinker trickster-objkt-worker
systemctl is-active trickster-agent trickster-admin trickster-thinker trickster-objkt-worker
```

3. Start local dashboard pair again:

```powershell
cd E:\PROJECTS\files_molt
.\trickster-agent\deploy\start_dashboard.ps1
```

## 4) Update server code safely

From Windows:

```powershell
cd E:\PROJECTS\files_molt
.\trickster-agent\deploy\update_server.ps1
```

Optional (install Playwright browser deps too):

```powershell
.\trickster-agent\deploy\update_server.ps1 -InstallPlaywright
```

What this does:
- backup runtime state (`config/.env`, `data/`)
- stop services
- stash local server edits
- `git pull --ff-only`
- `pip install -r requirements.txt` in `.venv`
- restart services

## 5) Rotate/update keys

### 5.1 Moltbook key (prompt, safest)

```powershell
cd E:\PROJECTS\files_molt
.\trickster-agent\deploy\rotate_moltbook_key.ps1
```

### 5.2 Any key (generic)

```powershell
$env:NEW_KEY = "your_secret_here"
.\trickster-agent\deploy\set_remote_env.ps1 -KeyName MOLTBOOK_API_KEY -KeyValue $env:NEW_KEY
```

Or prompt mode:

```powershell
.\trickster-agent\deploy\set_remote_env.ps1 -KeyName MOLTBOOK_API_KEY -Prompt
```

Important:
- keys must be ASCII (avoid RU keyboard while pasting)
- do not put secrets into bot chat prompts

## 6) 401 / token behavior

If UI shows `401 Unauthorized`, admin API is up but token is missing/wrong.

Fix:
- in new UI settings, set Admin Base URL and Admin Token
- or for legacy UI open with query token:
  - `http://localhost:8000/?token=YOUR_ADMIN_TOKEN`

## 7) Connection refused behavior

If UI shows `ERR_CONNECTION_REFUSED`:

1. Ensure tunnel window is running.
2. Ensure server admin is running:

```bash
systemctl status trickster-admin --no-pager -l
ss -ltnp | grep 8787
```

If down, restart:

```bash
systemctl restart trickster-admin
```

## 8) Daily health checks

On server:

```bash
systemctl is-active trickster-agent trickster-admin trickster-thinker trickster-objkt-worker
journalctl -u trickster-agent -n 60 --no-pager
journalctl -u trickster-admin -n 60 --no-pager
```

## 9) One command helper

Use `deploy/ops.ps1` for common operations:

```powershell
cd E:\PROJECTS\files_molt
.\trickster-agent\deploy\ops.ps1 -Action help
```

Examples:

```powershell
.\trickster-agent\deploy\ops.ps1 -Action status
.\trickster-agent\deploy\ops.ps1 -Action backup
.\trickster-agent\deploy\ops.ps1 -Action update
.\trickster-agent\deploy\ops.ps1 -Action restart
.\trickster-agent\deploy\ops.ps1 -Action rotate-moltbook-key
.\trickster-agent\deploy\ops.ps1 -Action dashboard
.\trickster-agent\deploy\ops.ps1 -Action quiet-on
.\trickster-agent\deploy\ops.ps1 -Action quiet-off
.\trickster-agent\deploy\ops.ps1 -Action writes-on
.\trickster-agent\deploy\ops.ps1 -Action writes-off
.\trickster-agent\deploy\ops.ps1 -Action smoke-visual -Prompt "mu mirror fracture"
.\trickster-agent\deploy\ops.ps1 -Action visual-image-first
.\trickster-agent\deploy\ops.ps1 -Action visual-video-first
.\trickster-agent\deploy\ops.ps1 -Action visual-safe-fallback
```

## 10) Anti-spam + visual smoke test

Use quiet mode when account is sensitive / near limits:

```powershell
.\trickster-agent\deploy\ops.ps1 -Action quiet-on
```

Disable quiet mode:

```powershell
.\trickster-agent\deploy\ops.ps1 -Action quiet-off
```

Hard-disable real Moltbook writes (safe simulation mode):

```powershell
.\trickster-agent\deploy\ops.ps1 -Action writes-off
```

Re-enable real writes:

```powershell
.\trickster-agent\deploy\ops.ps1 -Action writes-on
```

Smoke-test all visual modes in one run (`url/ascii/audio/video`):

```powershell
.\trickster-agent\deploy\ops.ps1 -Action smoke-visual -Prompt "mu glitch void mirror"
```

By default video test runs without audio narration. Enable it explicitly:

```powershell
.\trickster-agent\deploy\ops.ps1 -Action smoke-visual -Prompt "mu glitch void mirror" -IncludeVideoAudio
```

## 11) Visual presets (one-click profiles)

Image-first (stable image links):

```powershell
.\trickster-agent\deploy\ops.ps1 -Action visual-image-first
```

Video-first (prefer video model, no auto-audio):

```powershell
.\trickster-agent\deploy\ops.ps1 -Action visual-video-first
```

Safe-fallback (auto mode + ascii fallback):

```powershell
.\trickster-agent\deploy\ops.ps1 -Action visual-safe-fallback
```
