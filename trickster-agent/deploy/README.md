# Deploy Automation (Ubuntu VPS + Windows local)

This repo is split into:
- Server side (Ubuntu/Hetzner): runs the agent + admin API as systemd services
- Local side (Windows): runs the React dashboard and connects via SSH tunnel

## 1) One-command server bootstrap (first install)

From Windows (PowerShell):

```powershell
cd E:\PROJECTS\files_molt
scp .\trickster-agent\deploy\install_ubuntu.sh root@YOUR_SERVER_IP:/root/install_ubuntu.sh
ssh root@YOUR_SERVER_IP "chmod +x /root/install_ubuntu.sh && REPO_URL='https://github.com/FriendsCoin/muagent.git' REPO_BRANCH='main' PROJECT_SUBDIR='trickster-agent' MOLTBOOK_API_KEY='YOUR_MOLTBOOK_KEY' ANTHROPIC_API_KEY='YOUR_ANTHROPIC_KEY' ADMIN_TOKEN='CHANGE_ME' /root/install_ubuntu.sh"
```

## 2) Migrate local state/history to continue timeline

From Windows (PowerShell):

```powershell
cd E:\PROJECTS\files_molt
powershell -ExecutionPolicy Bypass -File .\trickster-agent\deploy\sync_state_from_windows.ps1 -ServerIp YOUR_SERVER_IP
```

## 3) Admin API + React dashboard

### 3.1 Install admin API service (on server)

```bash
cd /opt/trickster-agent/repo/trickster-agent
chmod +x deploy/install_admin_ui.sh
sudo ADMIN_HOST=127.0.0.1 ADMIN_PORT=8787 deploy/install_admin_ui.sh
```

### 3.2 Open tunnel (on Windows)

This forwards:
- local `http://localhost:8000` -> server `http://127.0.0.1:8787`

```powershell
cd E:\PROJECTS\files_molt
.\trickster-agent\deploy\tunnel_admin.ps1 -ServerIp YOUR_SERVER_IP
```

Keep this terminal open. Stop the tunnel with Ctrl+C.

### 3.3 Run the dashboard (on Windows)

```powershell
cd E:\PROJECTS\files_molt\trickster-command
npm run dev
```

Open: `http://localhost:8080`

If you set `ADMIN_TOKEN` in server `config/.env`, you must also provide it to the dashboard (Settings). Alternatively, set:
- `trickster-command/.env.local`:

```env
VITE_ADMIN_BASE_URL=http://localhost:8000
VITE_ADMIN_TOKEN=YOUR_ADMIN_TOKEN
```

## 4) Update server code (git pull + pip install + restart)

From Windows (PowerShell):

```powershell
cd E:\PROJECTS\files_molt
.\trickster-agent\deploy\update_server.ps1 -ServerIp YOUR_SERVER_IP
```

This does:
- stop services (best-effort)
- stash local changes on server repo (so pull can fast-forward)
- `git pull --ff-only origin main`
- `pip install -r requirements.txt` into `.venv`
- restart services

## 5) Rotate secrets (API keys) on the server

From Windows (PowerShell) (recommended: via env var, so it doesn't land in PS history):

```powershell
$env:NEW_KEY = "moltbook_sk_..."
.\trickster-agent\deploy\set_remote_env.ps1 -KeyName MOLTBOOK_API_KEY -KeyValue $env:NEW_KEY
```

Notes:
- API keys should be ASCII. If you paste with RU keyboard layout, you may insert Cyrillic characters and break loading.
- Do not `source config/.env` on Linux. It is not a shell script. The project reads it via python-dotenv.

## 6) Health checks (server)

```bash
systemctl status trickster-agent --no-pager -l
systemctl status trickster-admin --no-pager -l
systemctl status trickster-thinker --no-pager -l
systemctl status trickster-objkt-worker --no-pager -l

journalctl -u trickster-agent -n 120 --no-pager
journalctl -u trickster-admin -n 120 --no-pager
```

Ports:
- admin API: `127.0.0.1:8787` (server)
- objkt worker: `127.0.0.1:9898` (server)

## 7) Common problems

### Dashboard shows `ERR_CONNECTION_REFUSED`

You forgot the tunnel or the admin service is down.

1) On server:
```bash
systemctl restart trickster-admin
systemctl status trickster-admin --no-pager -l
```

2) On Windows: keep the tunnel running:
```powershell
.\trickster-agent\deploy\tunnel_admin.ps1
```

### Server `trickster-admin` says: `No module named fastapi`

You updated code, but didn't reinstall deps.

On server:
```bash
cd /opt/trickster-agent/repo/trickster-agent
sudo -u bot -H bash -lc '.venv/bin/pip install -r requirements.txt'
systemctl restart trickster-admin
```

