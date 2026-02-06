# Server Deploy (Ubuntu VPS)

Run this from your local machine after creating the VPS:

```bash
scp deploy/install_ubuntu.sh root@YOUR_SERVER_IP:/root/install_ubuntu.sh
ssh root@YOUR_SERVER_IP \
  "chmod +x /root/install_ubuntu.sh && \
   REPO_URL='YOUR_GIT_REPO_URL' \
   REPO_BRANCH='main' \
   PROJECT_SUBDIR='trickster-agent' \
   MOLTBOOK_API_KEY='YOUR_MOLTBOOK_KEY' \
   ANTHROPIC_API_KEY='YOUR_ANTHROPIC_KEY' \
   /root/install_ubuntu.sh"
```

After deploy:

```bash
ssh root@YOUR_SERVER_IP "systemctl status trickster-agent --no-pager"
ssh root@YOUR_SERVER_IP "journalctl -u trickster-agent -f"
```

Notes:
- `PROJECT_SUBDIR='trickster-agent'` assumes the repo root contains the `trickster-agent/` folder.
- Use a private repo URL only if the server has access (deploy key or token).
- The script stores runtime secrets in `config/.env` with `0600` permissions.
