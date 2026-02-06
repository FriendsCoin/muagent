# Deploy Automation (Ubuntu VPS + Windows local)

## 1. One-command server bootstrap

Run from your Windows machine (PowerShell):

```powershell
cd E:\PROJECTS\files_molt
scp .\trickster-agent\deploy\install_ubuntu.sh root@YOUR_SERVER_IP:/root/install_ubuntu.sh
ssh root@YOUR_SERVER_IP "chmod +x /root/install_ubuntu.sh && REPO_URL='https://github.com/FriendsCoin/muagent.git' REPO_BRANCH='master' PROJECT_SUBDIR='trickster-agent' MOLTBOOK_API_KEY='YOUR_MOLTBOOK_KEY' ANTHROPIC_API_KEY='YOUR_ANTHROPIC_KEY' ADMIN_TOKEN='CHANGE_ME' /root/install_ubuntu.sh"
```

Adjust `REPO_BRANCH` to your real branch (`master`/`main`).

## 2. Migrate local state/history to continue timeline

```powershell
powershell -ExecutionPolicy Bypass -File .\trickster-agent\deploy\sync_state_from_windows.ps1 -ServerIp YOUR_SERVER_IP
```

This script:
- stops `trickster-agent` on server,
- uploads `data/state.json` + `data/history.db`,
- restores ownership and starts service.

## 3. Enable maintenance automation (update + backup + monitor)

On server:

```bash
cd /opt/trickster-agent/repo/trickster-agent
chmod +x deploy/setup_maintenance.sh
sudo deploy/setup_maintenance.sh
```

Installed cron jobs:
- every 5 min: service monitor + auto-restart (`monitor_service.sh`)
- daily 03:20 UTC: backup `data/` tar.gz (`backup_data.sh`)
- daily 03:45 UTC: git pull + pip install + service restart (`update_and_restart.sh`)

## 4. Admin UI (observe/influence modes)

Install service:

```bash
cd /opt/trickster-agent/repo/trickster-agent
chmod +x deploy/install_admin_ui.sh
sudo ADMIN_HOST=127.0.0.1 ADMIN_PORT=8787 deploy/install_admin_ui.sh
```

Use SSH tunnel from local machine:

```powershell
ssh -L 8787:127.0.0.1:8787 root@YOUR_SERVER_IP
```

Then open:

`http://127.0.0.1:8787`

Modes:
- `observe`: ask Mu, no effect on decisions.
- `influence`: queues one instruction for next heartbeat.

If `ADMIN_TOKEN` is set in `.env`, paste it in UI token field.

## 5. Health checks

```bash
systemctl status trickster-agent --no-pager -l
journalctl -u trickster-agent -f
systemctl status trickster-admin --no-pager -l
```
