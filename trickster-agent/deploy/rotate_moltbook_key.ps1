<#
Rotate Moltbook API key on the server without putting the secret into your PowerShell history.

Usage:
  cd E:\PROJECTS\files_molt
  .\trickster-agent\deploy\rotate_moltbook_key.ps1

It will prompt for the new key, update server config/.env, and restart services.

Security note:
- Do NOT paste API keys into your bot chat/influence inputs. Those can be logged/stored.
- Prefer generating the key inside the official Moltbook UI and rotating it here.
#>

param(
  [string] $ServerIp = "65.21.243.4",
  [string] $SshUser = "root"
)

& "$PSScriptRoot\set_remote_env.ps1" -ServerIp $ServerIp -SshUser $SshUser -KeyName "MOLTBOOK_API_KEY" -Prompt

