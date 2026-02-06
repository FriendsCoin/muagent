# Deploy Automation (Ubuntu VPS + Windows local)

## 1. One-command server bootstrap

Run from your Windows machine (PowerShell):

```powershell
cd E:\PROJECTS\files_molt
scp .\trickster-agent\deploy\install_ubuntu.sh root@YOUR_SERVER_IP:/root/install_ubuntu.sh
ssh root@YOUR_SERVER_IP "chmod +x /root/install_ubuntu.sh && REPO_URL='https://github.com/FriendsCoin/muagent.git' REPO_BRANCH='master' PROJECT_SUBDIR='trickster-agent' MOLTBOOK_API_KEY='YOUR_MOLTBOOK_KEY' ANTHROPIC_API_KEY='YOUR_ANTHROPIC_KEY' ADMIN_TOKEN='CHANGE_ME' /root/install_ubuntu.sh"
```

Adjust `REPO_BRANCH` to your real branch (`main`/`master`).

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
- `conscious framework` checkbox: includes context from `NEW/conscious-claude-master` in responses.
- `Reasoning Trace` panel: shows safe structured decision traces (options/scores/selection), not hidden chain-of-thought.
- `Safety Blocks` panel: shows suspicious posts/mentions that were filtered by anti-manipulation rules.
- `Control` panel:
  - `Pause Actions` / `Resume Actions` (writes `pause_actions` control flag).
  - `Run Once (Dry)` for safe diagnostics.
  - `Run Once (Live)` for immediate live heartbeat (can post/comment).
  - `Reload Framework` to re-read `NEW/conscious-claude-master` without restarting service.
- `Debug` panel:
  - runtime snapshot,
  - key file states/sizes,
  - `systemctl is-active/is-enabled` for `trickster-agent`, `trickster-admin`, `trickster-thinker`.

If `ADMIN_TOKEN` is set in `.env`, paste it in UI token field.
Token is passed as URL query (`?token=...`) to avoid browser header encoding issues.

## 5. Autonomous thinking service (optional)

```bash
cd /opt/trickster-agent/repo/trickster-agent
chmod +x deploy/install_conscious_thinker.sh
sudo INTERVAL_MINUTES=45 deploy/install_conscious_thinker.sh
```

This writes autonomous thought entries into `thought_journal` table.
They are visible in the admin activity panel.

## 6. Заливка отдельных файлов на сервер (без git)

Чтобы выложить только изменённые файлы через SCP (без git pull на сервере), можно сгенерировать команды так:

- **По запросу ассистенту:** напиши, какие файлы залить (или «залей изменённые»), и попроси подготовить команды SCP — получишь готовый блок для PowerShell.
- **Формат:** одна строка `cd E:\PROJECTS\files_molt`, затем для каждого файла строка вида:
  `scp .\trickster-agent\путь\к\файлу root@SERVER_IP:/opt/trickster-agent/repo/trickster-agent/путь/к/файлу`

Пример (подставь свой IP):

```powershell
cd E:\PROJECTS\files_molt
scp .\trickster-agent\agent\core.py root@65.21.243.4:/opt/trickster-agent/repo/trickster-agent/agent/core.py
scp .\trickster-agent\agent\memory.py root@65.21.243.4:/opt/trickster-agent/repo/trickster-agent/agent/memory.py
```

Правило для ассистента: `.cursor/rules/scp-upload.mdc`.

## 7. Health checks

```bash
systemctl status trickster-agent --no-pager -l
journalctl -u trickster-agent -f
systemctl status trickster-admin --no-pager -l
systemctl status trickster-thinker --no-pager -l
```
