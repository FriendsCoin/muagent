"""Lightweight admin UI for Mu agent.

Features:
- Live status/activity/logs view
- Ask questions in observe mode (no impact on decisions)
- Queue influence commands for next heartbeat
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import load_config
from agent.personality import Personality

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tail_lines(path: Path, max_lines: int = 200) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-max_lines:]


def _ensure_operator_table(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS operator_commands (
                id TEXT PRIMARY KEY,
                mode TEXT,
                question TEXT,
                instruction TEXT,
                status TEXT,
                created_at TEXT,
                applied_at TEXT,
                response TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


@dataclass
class AdminContext:
    cfg: dict
    state_path: Path
    db_path: Path
    log_path: Path
    admin_token: str
    _personality: Personality | None = None

    def load_state(self) -> dict:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def fetch_recent(self, table: str, limit: int = 20) -> list[dict]:
        if not self.db_path.exists():
            return []
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM {table} ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def fetch_counts(self) -> dict:
        counts = {"posts": 0, "comments": 0, "pending_operator": 0}
        if not self.db_path.exists():
            return counts
        with self._connect() as conn:
            counts["posts"] = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
            counts["comments"] = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
            counts["pending_operator"] = conn.execute(
                "SELECT COUNT(*) FROM operator_commands WHERE status = 'pending'"
            ).fetchone()[0]
        return counts

    def enqueue_influence(self, question: str, instruction: str) -> str:
        cmd_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO operator_commands (id, mode, question, instruction, status, created_at, applied_at, response) "
                "VALUES (?, 'influence', ?, ?, 'pending', ?, '', '')",
                (cmd_id, question, instruction, _now_iso()),
            )
            conn.commit()
        return cmd_id

    def personality(self) -> Personality:
        if self._personality is None:
            self._personality = Personality(
                api_key=self.cfg["_secrets"]["anthropic_api_key"],
                model=self.cfg.get("llm", {}).get("model", "claude-sonnet-4-20250514"),
                temperature=self.cfg.get("llm", {}).get("temperature", 0.9),
                voice_modes=self.cfg.get("agent", {}).get("personality", {}).get("voice_modes"),
            )
        return self._personality


class AdminHandler(BaseHTTPRequestHandler):
    ctx: AdminContext

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self) -> bool:
        token = self.ctx.admin_token.strip()
        if not token:
            return True
        supplied = self.headers.get("X-Admin-Token", "")
        return supplied == token

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._send_html(_INDEX_HTML)
            return

        if not self._auth_ok():
            self._send_json(401, {"error": "unauthorized"})
            return

        if path == "/api/status":
            state = self.ctx.load_state()
            counts = self.ctx.fetch_counts()
            self._send_json(200, {"state": state, "counts": counts})
            return

        if path == "/api/activity":
            params = parse_qs(parsed.query)
            try:
                limit = int(params.get("limit", ["20"])[0])
            except ValueError:
                limit = 20
            limit = max(1, min(100, limit))
            payload = {
                "posts": self.ctx.fetch_recent("posts", limit),
                "comments": self.ctx.fetch_recent("comments", limit),
                "narrative_events": self.ctx.fetch_recent("narrative_events", limit),
                "operator_commands": self.ctx.fetch_recent("operator_commands", limit),
            }
            self._send_json(200, payload)
            return

        if path == "/api/logs":
            params = parse_qs(parsed.query)
            try:
                lines = int(params.get("lines", ["200"])[0])
            except ValueError:
                lines = 200
            lines = max(10, min(2000, lines))
            self._send_json(200, {"lines": _tail_lines(self.ctx.log_path, lines)})
            return

        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if not self._auth_ok():
            self._send_json(401, {"error": "unauthorized"})
            return

        if self.path != "/api/chat":
            self._send_json(404, {"error": "not_found"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send_json(400, {"error": "invalid_json"})
            return

        mode = str(body.get("mode", "observe")).lower()
        question = str(body.get("question", "")).strip()
        instruction = str(body.get("instruction", "")).strip()

        if not question and not instruction:
            self._send_json(400, {"error": "question_or_instruction_required"})
            return

        state = self.ctx.load_state()
        phase = str(state.get("current_phase", "emergence"))
        day = int(state.get("current_day", 1))
        total_posts = int(state.get("total_posts", 0))
        prompt = question or instruction

        try:
            reply = self.ctx.personality().generate_dm_reply(
                from_agent="Operator",
                message=prompt,
                phase=phase,
                day=day,
                total_posts=total_posts,
                mode_hint="breach" if mode == "influence" else "",
            )
        except Exception as exc:
            self._send_json(500, {"error": f"llm_error: {exc}"})
            return

        command_id = ""
        if mode == "influence":
            command_id = self.ctx.enqueue_influence(
                question=question or prompt,
                instruction=instruction or prompt,
            )

        self._send_json(
            200,
            {
                "reply": reply,
                "mode": mode,
                "queued": bool(command_id),
                "command_id": command_id,
            },
        )


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Mu Admin</title>
  <style>
    :root {
      --bg: #0f1115;
      --card: #171a21;
      --fg: #e8edf3;
      --muted: #98a1ad;
      --accent: #5db2ff;
      --good: #76d672;
      --bad: #ff6d6d;
      --border: #2b3240;
    }
    body { margin: 0; font-family: "Segoe UI", sans-serif; background: var(--bg); color: var(--fg); }
    .wrap { max-width: 1100px; margin: 24px auto; padding: 0 16px; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
    .card { border: 1px solid var(--border); background: var(--card); border-radius: 12px; padding: 12px; }
    h1 { margin: 0 0 12px 0; font-size: 24px; }
    h2 { margin: 0 0 10px 0; font-size: 16px; color: var(--accent); }
    .mono { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; white-space: pre-wrap; }
    input, textarea, select, button {
      background: #0f131b; color: var(--fg); border: 1px solid var(--border); border-radius: 8px; padding: 8px;
    }
    textarea { width: 100%; min-height: 90px; }
    button { cursor: pointer; }
    .row { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
    .muted { color: var(--muted); font-size: 12px; }
    .ok { color: var(--good); }
    .err { color: var(--bad); }
    @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Mu Admin Console</h1>
    <div class="row">
      <input id="token" type="password" placeholder="Admin token (optional)" />
      <button onclick="refreshAll()">Refresh</button>
      <span id="health" class="muted"></span>
    </div>
    <div class="grid">
      <div class="card">
        <h2>Status</h2>
        <div id="status" class="mono">loading...</div>
      </div>
      <div class="card">
        <h2>Ask Mu</h2>
        <div class="row">
          <select id="mode">
            <option value="observe">observe (no influence)</option>
            <option value="influence">influence (next heartbeat)</option>
          </select>
        </div>
        <textarea id="question" placeholder="Your question..."></textarea>
        <textarea id="instruction" placeholder="Influence instruction (optional, for influence mode)"></textarea>
        <div class="row">
          <button onclick="sendChat()">Send</button>
          <span class="muted">Observe does not affect decisions. Influence queues one command.</span>
        </div>
        <div id="reply" class="mono"></div>
      </div>
      <div class="card">
        <h2>Recent Activity</h2>
        <div id="activity" class="mono">loading...</div>
      </div>
      <div class="card">
        <h2>Logs</h2>
        <div id="logs" class="mono">loading...</div>
      </div>
    </div>
  </div>
  <script>
    const token = () => document.getElementById('token').value.trim();
    const headers = () => token() ? {'X-Admin-Token': token(), 'Content-Type':'application/json'} : {'Content-Type':'application/json'};
    async function apiGet(path) {
      const r = await fetch(path, {headers: token()?{'X-Admin-Token':token()}:{}});
      if (!r.ok) throw new Error(await r.text());
      return await r.json();
    }
    async function refreshStatus() {
      const d = await apiGet('/api/status');
      document.getElementById('status').textContent = JSON.stringify(d, null, 2);
    }
    async function refreshActivity() {
      const d = await apiGet('/api/activity?limit=12');
      document.getElementById('activity').textContent = JSON.stringify(d, null, 2);
    }
    async function refreshLogs() {
      const d = await apiGet('/api/logs?lines=120');
      document.getElementById('logs').textContent = (d.lines || []).join('\\n');
    }
    async function sendChat() {
      const mode = document.getElementById('mode').value;
      const question = document.getElementById('question').value;
      const instruction = document.getElementById('instruction').value;
      const r = await fetch('/api/chat', {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify({mode, question, instruction}),
      });
      const d = await r.json();
      document.getElementById('reply').textContent = JSON.stringify(d, null, 2);
      await refreshAll();
    }
    async function refreshAll() {
      const h = document.getElementById('health');
      try {
        await Promise.all([refreshStatus(), refreshActivity(), refreshLogs()]);
        h.textContent = 'OK';
        h.className = 'ok';
      } catch (e) {
        h.textContent = 'ERROR: ' + e.message;
        h.className = 'err';
      }
    }
    refreshAll();
    setInterval(refreshAll, 15000);
  </script>
</body>
</html>
"""


@click.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8787, type=int, show_default=True)
@click.option("--config-dir", default=None, type=click.Path(), help="Config directory")
@click.option("--admin-token", default="", help="Require this token for /api/*")
def main(host: str, port: int, config_dir: str | None, admin_token: str) -> None:
    cfg = load_config(config_dir)
    root = Path(__file__).resolve().parent.parent
    storage = cfg.get("storage", {})
    state_path = root / storage.get("state_file", "data/state.json")
    db_path = root / storage.get("history_db", "data/history.db")
    log_path = root / storage.get("log_file", "data/agent.log")

    _ensure_operator_table(db_path)

    token = admin_token.strip()
    if not token:
        token = cfg.get("_secrets", {}).get("admin_token", "") or ""

    AdminHandler.ctx = AdminContext(
        cfg=cfg,
        state_path=state_path,
        db_path=db_path,
        log_path=log_path,
        admin_token=token,
    )

    server = ThreadingHTTPServer((host, port), AdminHandler)
    click.echo(f"Mu admin UI listening on http://{host}:{port}")
    if token:
        click.echo("Admin token enabled: set X-Admin-Token header in requests/UI.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
