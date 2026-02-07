# Обновление репозитория на сервере (git pull от имени пользователя bot).
# Запуск: из корня files_molt или из trickster-agent:
#   .\trickster-agent\deploy\update_server.ps1
#   .\trickster-agent\deploy\update_server.ps1 -ServerIp 1.2.3.4

param(
    [string] $ServerIp = "65.21.243.4",
    [string] $SshUser = "root",
    [string] $RepoUser = "bot"
)

$remoteCmd = "sudo -u $RepoUser -H bash -lc 'cd /opt/trickster-agent/repo && git fetch --all --prune && git checkout main && git pull --ff-only origin main'"
Write-Host "Running on ${SshUser}@${ServerIp}: git pull in /opt/trickster-agent/repo as $RepoUser" -ForegroundColor Cyan
ssh "${SshUser}@${ServerIp}" $remoteCmd
