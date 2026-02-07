"""Admin UI and control plane for Mu.

Features:
- Status/activity/log/reasoning visibility
- Observe and influence chat modes
- Runtime controls (pause, run-once, reload framework)
- Optional conscious framework context
- Debug snapshot endpoint
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import asyncio
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import load_config
from agent.personality import Personality
from moltbook.client import MoltbookClient, MoltbookError

try:
    from agent.conscious_framework import load_conscious_framework
except Exception:  # pragma: no cover - fallback for deployments without optional module
    class _FrameworkFallback:
        def __init__(self, root: Path):
            self.root = root
            self.available = False

        def context_block(self, max_chars: int = 7000) -> str:
            del max_chars
            return ""

    def load_conscious_framework(base_dir: str | Path) -> _FrameworkFallback:
        return _FrameworkFallback(Path(base_dir))

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    raw = ts.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"1", "true", "yes", "y", "on"}:
            return True
        if low in {"0", "false", "no", "n", "off"}:
            return False
    return default


def _tail_lines(path: Path, max_lines: int = 200) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-max_lines:]


def _file_snapshot(path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {"path": str(path), "exists": path.exists()}
    if not path.exists():
        return info
    try:
        st = path.stat()
        info["size_bytes"] = st.st_size
        info["mtime"] = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
    except OSError as exc:
        info["error"] = str(exc)
    return info


def _ensure_admin_tables(db_path: Path) -> None:
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS thought_journal (
                id TEXT PRIMARY KEY,
                source TEXT,
                mode TEXT,
                prompt TEXT,
                content TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reasoning_trace (
                id TEXT PRIMARY KEY,
                source TEXT,
                action_type TEXT,
                summary TEXT,
                payload TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS control_flags (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS thinker_queue (
                id TEXT PRIMARY KEY,
                source TEXT,
                context TEXT,
                status TEXT,
                created_at TEXT,
                processed_at TEXT,
                error TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


@dataclass
class AdminContext:
    cfg: dict
    project_root: Path
    state_path: Path
    db_path: Path
    log_path: Path
    admin_token: str
    framework_dir: Path
    _personality: Personality | None = None
    started_unix: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.framework = load_conscious_framework(self.framework_dir)

    def reload_framework(self) -> None:
        self.framework = load_conscious_framework(self.framework_dir)

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
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    f"SELECT * FROM {table} ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        except sqlite3.Error:
            return []
        return [dict(r) for r in rows]

    def fetch_reasoning(
        self,
        limit: int = 20,
        source: str = "",
        action_type: str = "",
    ) -> list[dict]:
        if not self.db_path.exists():
            return []
        query = "SELECT * FROM reasoning_trace"
        where: list[str] = []
        params: list[Any] = []
        if source:
            where.append("source = ?")
            params.append(source)
        if action_type:
            where.append("action_type = ?")
            params.append(action_type)
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        try:
            with self._connect() as conn:
                rows = conn.execute(query, tuple(params)).fetchall()
        except sqlite3.Error:
            return []

        payload: list[dict] = []
        for row in rows:
            item = dict(row)
            payload_raw = item.get("payload", "")
            if isinstance(payload_raw, str) and payload_raw:
                try:
                    item["payload"] = json.loads(payload_raw)
                except json.JSONDecodeError:
                    pass
            payload.append(item)
        return payload

    def fetch_safety_events(self, limit: int = 20) -> list[dict]:
        if not self.db_path.exists():
            return []
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM narrative_events WHERE event_type = 'safety_filter' "
                    "ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        except sqlite3.Error:
            return []

        events: list[dict] = []
        for row in rows:
            item = dict(row)
            raw_meta = item.get("metadata", "")
            if isinstance(raw_meta, str) and raw_meta:
                try:
                    item["metadata"] = json.loads(raw_meta)
                except json.JSONDecodeError:
                    pass
            events.append(item)
        return events

    def fetch_counts(self) -> dict:
        counts = {
            "posts": 0,
            "comments": 0,
            "pending_operator": 0,
            "thoughts": 0,
            "reasoning_traces": 0,
            "pause_actions": False,
            "thinker_queue_pending": 0,
        }
        if not self.db_path.exists():
            return counts
        try:
            with self._connect() as conn:
                counts["posts"] = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
                counts["comments"] = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
                counts["pending_operator"] = conn.execute(
                    "SELECT COUNT(*) FROM operator_commands WHERE status = 'pending'"
                ).fetchone()[0]
                counts["thoughts"] = conn.execute("SELECT COUNT(*) FROM thought_journal").fetchone()[0]
                counts["reasoning_traces"] = conn.execute(
                    "SELECT COUNT(*) FROM reasoning_trace"
                ).fetchone()[0]
                counts["thinker_queue_pending"] = conn.execute(
                    "SELECT COUNT(*) FROM thinker_queue WHERE status = 'pending'"
                ).fetchone()[0]
                row = conn.execute(
                    "SELECT value FROM control_flags WHERE key = 'pause_actions' LIMIT 1"
                ).fetchone()
                counts["pause_actions"] = _to_bool(row[0], default=False) if row else False
        except sqlite3.Error:
            return counts
        return counts

    def fetch_post_activity(self) -> dict[str, Any]:
        activity = {
            "posts_last_10h": 0,
            "posts_last_24h": 0,
            "last_post_at": "",
            "last_post_title": "",
            "last_post_id": "",
            "last_post_submolt": "",
        }
        if not self.db_path.exists():
            return activity
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id, created_at, title, moltbook_id, submolt "
                    "FROM posts "
                    "WHERE COALESCE(moltbook_id, '') != 'dry_run' "
                    "ORDER BY created_at DESC LIMIT 5000"
                ).fetchall()
        except sqlite3.Error:
            return activity

        now = datetime.now(timezone.utc)
        for idx, row in enumerate(rows):
            created_at = str(row["created_at"] or "")
            dt = _parse_iso(created_at)
            if dt is None:
                continue

            age_hours = (now - dt).total_seconds() / 3600.0
            if age_hours <= 10:
                activity["posts_last_10h"] += 1
            if age_hours <= 24:
                activity["posts_last_24h"] += 1

            if idx == 0:
                activity["last_post_at"] = created_at
                activity["last_post_title"] = str(row["title"] or "")
                activity["last_post_id"] = str(row["moltbook_id"] or row["id"] or "")
                activity["last_post_submolt"] = str(row["submolt"] or "")
        return activity

    def fetch_timeline(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        items: list[dict[str, Any]] = []
        try:
            with self._connect() as conn:
                posts = conn.execute(
                    "SELECT id, created_at, title, submolt FROM posts ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                comments = conn.execute(
                    "SELECT id, created_at, post_id, content FROM comments ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                thoughts = conn.execute(
                    "SELECT id, created_at, mode, content FROM thought_journal ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                safety = conn.execute(
                    "SELECT id, created_at, description FROM narrative_events WHERE event_type = 'safety_filter' "
                    "ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        except sqlite3.Error:
            return []

        for row in posts:
            items.append(
                {
                    "kind": "post",
                    "created_at": str(row["created_at"] or ""),
                    "summary": f"{row['title']} (s/{row['submolt']})",
                    "id": str(row["id"] or ""),
                }
            )
        for row in comments:
            preview = str(row["content"] or "").replace("\n", " ").strip()
            items.append(
                {
                    "kind": "comment",
                    "created_at": str(row["created_at"] or ""),
                    "summary": f"on {row['post_id']}: {preview[:90]}",
                    "id": str(row["id"] or ""),
                }
            )
        for row in thoughts:
            preview = str(row["content"] or "").replace("\n", " ").strip()
            items.append(
                {
                    "kind": "thought",
                    "created_at": str(row["created_at"] or ""),
                    "summary": f"{row['mode']}: {preview[:90]}",
                    "id": str(row["id"] or ""),
                }
            )
        for row in safety:
            items.append(
                {
                    "kind": "safety",
                    "created_at": str(row["created_at"] or ""),
                    "summary": str(row["description"] or ""),
                    "id": str(row["id"] or ""),
                }
            )

        def _sort_key(item: dict[str, Any]) -> str:
            return str(item.get("created_at") or "")

        items.sort(key=_sort_key, reverse=True)
        return items[:limit]

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

    def log_thought(self, source: str, mode: str, prompt: str, content: str) -> str:
        thought_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO thought_journal (id, source, mode, prompt, content, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (thought_id, source, mode, prompt, content, _now_iso()),
            )
            conn.commit()
        return thought_id

    def get_control_flags(self) -> dict[str, str]:
        if not self.db_path.exists():
            return {}
        try:
            with self._connect() as conn:
                rows = conn.execute("SELECT key, value FROM control_flags").fetchall()
        except sqlite3.Error:
            return {}
        return {str(r["key"]): str(r["value"]) for r in rows}

    def set_control_flag(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO control_flags (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, _now_iso()),
            )
            conn.commit()

    def set_pause_actions(self, paused: bool) -> dict[str, str]:
        self.set_control_flag("pause_actions", "1" if paused else "0")
        return self.get_control_flags()

    def _service_status(self, name: str) -> dict[str, str]:
        result: dict[str, str] = {"name": name, "active": "unknown", "enabled": "unknown"}
        try:
            active = subprocess.run(
                ["systemctl", "is-active", name],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            result["active"] = active.stdout.strip() or active.stderr.strip() or "unknown"
        except Exception as exc:
            result["active"] = f"error: {exc}"

        try:
            enabled = subprocess.run(
                ["systemctl", "is-enabled", name],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            result["enabled"] = enabled.stdout.strip() or enabled.stderr.strip() or "unknown"
        except Exception as exc:
            result["enabled"] = f"error: {exc}"
        return result

    def runtime_snapshot(self) -> dict[str, Any]:
        return {
            "server_time": _now_iso(),
            "uptime_seconds": round(max(0.0, time.time() - self.started_unix), 2),
            "python_executable": sys.executable,
            "project_root": str(self.project_root),
            "paths": {
                "state": _file_snapshot(self.state_path),
                "db": _file_snapshot(self.db_path),
                "log": _file_snapshot(self.log_path),
            },
            "control_flags": self.get_control_flags(),
            "services": [
                self._service_status("trickster-agent"),
                self._service_status("trickster-admin"),
                self._service_status("trickster-thinker"),
            ],
            "framework": {
                "dir": str(self.framework_dir),
                "available": self.framework.available,
            },
        }

    def run_once(self, dry_run: bool, timeout_seconds: int = 240) -> dict[str, Any]:
        timeout_seconds = max(30, min(600, timeout_seconds))
        venv_python = self.project_root / ".venv" / ("Scripts" if os.name == "nt" else "bin") / "python"
        python_bin = venv_python if venv_python.exists() else Path(sys.executable)
        cmd = [str(python_bin), "main.py", "--once"]
        if dry_run:
            cmd.append("--dry-run")

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        proc = subprocess.run(
            cmd,
            cwd=self.project_root,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env=env,
        )

        merged = "\n".join([proc.stdout or "", proc.stderr or ""]).strip()
        return {
            "command": cmd,
            "dry_run": dry_run,
            "returncode": proc.returncode,
            "output_lines": merged.splitlines()[-200:],
        }

    def personality(self) -> Personality:
        if self._personality is None:
            self._personality = Personality(
                api_key=self.cfg["_secrets"]["anthropic_api_key"],
                model=self.cfg.get("llm", {}).get("model", "claude-sonnet-4-20250514"),
                temperature=self.cfg.get("llm", {}).get("temperature", 0.9),
                voice_modes=self.cfg.get("agent", {}).get("personality", {}).get("voice_modes"),
            )
        return self._personality

    def build_prompt(self, user_prompt: str, conscious: bool) -> str:
        if not conscious or not self.framework.available:
            return user_prompt
        framework = self.framework.context_block(max_chars=5000)
        return (
            "Conscious framework mode is enabled. Use the framework as optional context, "
            "not as mandatory doctrine. Keep response practical and concise.\n\n"
            f"Framework context:\n{framework}\n\n"
            f"Operator prompt:\n{user_prompt}"
        )

    def generate_reply(self, prompt: str, mode: str, conscious: bool) -> str:
        state = self.load_state()
        phase = str(state.get("current_phase", "emergence"))
        day = int(state.get("current_day", 1))
        total_posts = int(state.get("total_posts", 0))
        final_prompt = self.build_prompt(prompt, conscious=conscious)

        return self.personality().generate_dm_reply(
            from_agent="Operator",
            message=final_prompt,
            phase=phase,
            day=day,
            total_posts=total_posts,
            mode_hint="breach" if mode == "influence" else "",
        )

    def generate_autonomous_thought(self, conscious: bool) -> tuple[str, str]:
        state = self.load_state()
        phase = str(state.get("current_phase", "emergence"))
        day = int(state.get("current_day", 1))
        prompt = (
            "Write one internal thought-journal entry as Mu. "
            "Not a public post, not a decision command. "
            "2-6 sentences, concise, reflective, grounded in current phase/day."
        )
        prompt = self.build_prompt(prompt, conscious=conscious)

        text = self.personality().generate_post_text(
            theme=f"internal reflection day {day}",
            phase=phase,
            day=day,
            context=prompt,
            total_posts=int(state.get("total_posts", 0)),
        )
        thought_id = self.log_thought("conscious_worker", "autonomous", prompt, text)
        return thought_id, text

    async def _delete_post_async(self, post_id: str) -> dict[str, Any]:
        async with MoltbookClient(self.cfg["_secrets"]["moltbook_api_key"]) as mb:
            post = await mb.get_post(post_id)
            me = await mb.get_me()
            if post.author and me.name and post.author != me.name:
                raise MoltbookError(
                    f"Refusing to delete чужой пост: owner={post.author}, me={me.name}"
                )
            await mb.delete_post(post_id)
            return {"post_id": post_id, "title": post.title, "author": post.author}

    def delete_post(self, post_id: str) -> dict[str, Any]:
        return asyncio.run(self._delete_post_async(post_id))


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

    def _request_token(self) -> str:
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        if q.get("token"):
            return q["token"][0]
        return self.headers.get("X-Admin-Token", "")

    def _auth_ok(self) -> bool:
        token = self.ctx.admin_token.strip()
        if not token:
            return True
        supplied = self._request_token()
        return supplied == token

    def _read_body_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise ValueError("invalid_json")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._send_html(_INDEX_HTML)
            return

        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        if not self._auth_ok():
            self._send_json(401, {"error": "unauthorized"})
            return

        if path == "/api/status":
            state = self.ctx.load_state()
            counts = self.ctx.fetch_counts()
            self._send_json(
                200,
                {
                    "state": state,
                    "counts": counts,
                    "post_activity": self.ctx.fetch_post_activity(),
                    "control_flags": self.ctx.get_control_flags(),
                    "conscious_framework": {
                        "dir": str(self.ctx.framework_dir),
                        "available": self.ctx.framework.available,
                    },
                },
            )
            return

        if path == "/api/post_activity":
            self._send_json(200, self.ctx.fetch_post_activity())
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
                "safety_events": self.ctx.fetch_safety_events(limit),
                "operator_commands": self.ctx.fetch_recent("operator_commands", limit),
                "thoughts": self.ctx.fetch_recent("thought_journal", limit),
                "reasoning_traces": self.ctx.fetch_recent("reasoning_trace", limit),
            }
            self._send_json(200, payload)
            return

        if path == "/api/timeline":
            params = parse_qs(parsed.query)
            try:
                limit = int(params.get("limit", ["20"])[0])
            except ValueError:
                limit = 20
            limit = max(1, min(200, limit))
            self._send_json(200, {"items": self.ctx.fetch_timeline(limit=limit), "limit": limit})
            return

        if path == "/api/safety":
            params = parse_qs(parsed.query)
            try:
                limit = int(params.get("limit", ["20"])[0])
            except ValueError:
                limit = 20
            limit = max(1, min(200, limit))
            self._send_json(200, {"events": self.ctx.fetch_safety_events(limit=limit), "limit": limit})
            return

        if path == "/api/reasoning":
            params = parse_qs(parsed.query)
            try:
                limit = int(params.get("limit", ["20"])[0])
            except ValueError:
                limit = 20
            limit = max(1, min(200, limit))
            source = str(params.get("source", [""])[0]).strip()
            action_type = str(params.get("action_type", [""])[0]).strip()
            traces = self.ctx.fetch_reasoning(limit=limit, source=source, action_type=action_type)
            self._send_json(
                200,
                {
                    "traces": traces,
                    "filters": {"source": source, "action_type": action_type, "limit": limit},
                },
            )
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

        if path == "/api/debug/runtime":
            self._send_json(200, self.ctx.runtime_snapshot())
            return

        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if not self._auth_ok():
            self._send_json(401, {"error": "unauthorized"})
            return

        try:
            body = self._read_body_json()
        except ValueError:
            self._send_json(400, {"error": "invalid_json"})
            return

        if path == "/api/chat":
            mode = str(body.get("mode", "observe")).lower()
            question = str(body.get("question", "")).strip()
            instruction = str(body.get("instruction", "")).strip()
            conscious = _to_bool(body.get("conscious", False), default=False)

            if not question and not instruction:
                self._send_json(400, {"error": "question_or_instruction_required"})
                return

            prompt = question or instruction

            try:
                reply = self.ctx.generate_reply(prompt=prompt, mode=mode, conscious=conscious)
            except Exception as exc:
                self._send_json(500, {"error": f"llm_error: {exc}"})
                return

            thought_id = self.ctx.log_thought(
                source="admin",
                mode=mode,
                prompt=prompt,
                content=reply,
            )

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
                    "conscious": conscious,
                    "queued": bool(command_id),
                    "command_id": command_id,
                    "thought_id": thought_id,
                },
            )
            return

        if path == "/api/conscious/think":
            conscious = _to_bool(body.get("conscious", True), default=True)
            try:
                thought_id, thought = self.ctx.generate_autonomous_thought(conscious=conscious)
            except Exception as exc:
                self._send_json(500, {"error": f"llm_error: {exc}"})
                return
            self._send_json(200, {"thought_id": thought_id, "thought": thought, "conscious": conscious})
            return

        if path == "/api/control/pause":
            paused = _to_bool(body.get("paused", True), default=True)
            flags = self.ctx.set_pause_actions(paused)
            self._send_json(200, {"ok": True, "paused": paused, "control_flags": flags})
            return

        if path == "/api/control/reload_framework":
            self.ctx.reload_framework()
            self._send_json(
                200,
                {
                    "ok": True,
                    "framework": {
                        "dir": str(self.ctx.framework_dir),
                        "available": self.ctx.framework.available,
                    },
                },
            )
            return

        if path == "/api/control/thinker":
            enabled = _to_bool(body.get("enabled", False), default=False)
            mode = str(body.get("mode", "queue")).strip().lower()
            auto_queue = _to_bool(body.get("auto_queue", True), default=True)
            if mode not in {"queue", "interval"}:
                self._send_json(400, {"error": "invalid_mode"})
                return
            self.ctx.set_control_flag("thinker_enabled", "1" if enabled else "0")
            self.ctx.set_control_flag("thinker_mode", mode)
            self.ctx.set_control_flag("thinker_auto_queue", "1" if auto_queue else "0")
            self._send_json(
                200,
                {
                    "ok": True,
                    "control_flags": self.ctx.get_control_flags(),
                },
            )
            return

        if path == "/api/control/run_once":
            dry_run = _to_bool(body.get("dry_run", True), default=True)
            timeout_seconds = int(body.get("timeout_seconds", 240))
            try:
                result = self.ctx.run_once(dry_run=dry_run, timeout_seconds=timeout_seconds)
            except subprocess.TimeoutExpired:
                self._send_json(504, {"error": "run_once_timeout"})
                return
            except Exception as exc:
                self._send_json(500, {"error": f"run_once_failed: {exc}"})
                return
            self._send_json(200, result)
            return

        if path == "/api/control/delete_post":
            post_id = str(body.get("post_id", "")).strip()
            confirm_text = str(body.get("confirm_text", "")).strip().upper()
            if not post_id:
                self._send_json(400, {"error": "post_id_required"})
                return
            if confirm_text != "DELETE":
                self._send_json(400, {"error": "confirm_text_must_be_DELETE"})
                return
            try:
                deleted = self.ctx.delete_post(post_id)
            except Exception as exc:
                self._send_json(500, {"error": f"delete_post_failed: {exc}"})
                return
            self._send_json(200, {"ok": True, "deleted": deleted})
            return

        self._send_json(404, {"error": "not_found"})


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
      --warn: #ffd166;
      --border: #2b3240;
    }
    body { margin: 0; font-family: "Segoe UI", sans-serif; background: var(--bg); color: var(--fg); }
    .wrap { max-width: 1280px; margin: 24px auto; padding: 0 16px; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
    .card { border: 1px solid var(--border); background: var(--card); border-radius: 12px; padding: 12px; }
    h1 { margin: 0 0 12px 0; font-size: 24px; }
    h2 { margin: 0 0 10px 0; font-size: 16px; color: var(--accent); }
    .mono { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; white-space: pre-wrap; max-height: 340px; overflow: auto; word-break: break-word; }
    input, textarea, select, button {
      background: #0f131b; color: var(--fg); border: 1px solid var(--border); border-radius: 8px; padding: 8px;
    }
    textarea { width: 100%; min-height: 90px; }
    button { cursor: pointer; }
    .row { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; flex-wrap: wrap; }
    .muted { color: var(--muted); font-size: 12px; }
    .ok { color: var(--good); }
    .err { color: var(--bad); }
    .warn { color: var(--warn); }
    .pill { padding: 3px 8px; border-radius: 999px; border: 1px solid var(--border); font-size: 12px; }
    @media (max-width: 980px) { .grid { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Mu Admin Console</h1>
    <div class="row">
      <input id="token" type="text" placeholder="Admin token (optional)" />
      <button onclick="refreshAll()">Refresh</button>
      <label class="muted"><input id="conscious" type="checkbox" checked /> conscious framework</label>
      <span id="health" class="pill muted">loading</span>
      <span id="pauseState" class="pill muted">pause: unknown</span>
    </div>
    <div class="grid">
      <div class="card">
        <h2>Status</h2>
        <div id="status" class="mono">loading...</div>
      </div>

      <div class="card">
        <h2>Post Activity</h2>
        <div id="postActivity" class="mono">loading...</div>
      </div>

      <div class="card">
        <h2>Control</h2>
        <div class="row">
          <button onclick="setPause(true)">Pause Actions</button>
          <button onclick="setPause(false)">Resume Actions</button>
          <button onclick="thinkNow()">Think Now</button>
        </div>
        <div class="row">
          <button onclick="runOnce(true)">Run Once (Dry)</button>
          <button onclick="runOnce(false)">Run Once (Live)</button>
          <button onclick="reloadFramework()">Reload Framework</button>
        </div>
        <div class="row">
          <label class="muted">Thinker</label>
          <select id="thinkerMode">
            <option value="queue" selected>queue</option>
            <option value="interval">interval</option>
          </select>
          <label class="muted"><input id="thinkerEnabled" type="checkbox" /> enabled</label>
          <label class="muted"><input id="thinkerAutoQueue" type="checkbox" checked /> auto queue</label>
          <button onclick="saveThinkerConfig()">Apply Thinker</button>
        </div>
        <div class="row">
          <input id="deletePostId" type="text" placeholder="post_id to delete" style="min-width:320px;" />
          <input id="deleteConfirm" type="text" placeholder="type DELETE" style="max-width:140px;" />
          <button onclick="deletePost()">Delete Post</button>
        </div>
        <div id="controlResult" class="mono muted">No control action yet.</div>
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
        <div class="row">
          <label class="muted">limit</label>
          <select id="activityLimit">
            <option value="5">5</option>
            <option value="10" selected>10</option>
            <option value="20">20</option>
            <option value="50">50</option>
            <option value="100">100</option>
          </select>
          <button onclick="refreshActivity()">Apply</button>
        </div>
        <div id="activity" class="mono">loading...</div>
      </div>

      <div class="card">
        <h2>Live Timeline</h2>
        <div class="row">
          <label class="muted">limit</label>
          <select id="timelineLimit">
            <option value="5">5</option>
            <option value="10" selected>10</option>
            <option value="20">20</option>
            <option value="50">50</option>
            <option value="100">100</option>
          </select>
          <button onclick="refreshTimeline()">Apply</button>
        </div>
        <div id="timeline" class="mono">loading...</div>
      </div>

      <div class="card">
        <h2>Reasoning Trace</h2>
        <div class="row">
          <label class="muted">limit</label>
          <select id="reasoningLimit">
            <option value="5">5</option>
            <option value="10" selected>10</option>
            <option value="20">20</option>
            <option value="50">50</option>
            <option value="100">100</option>
          </select>
          <select id="reasoningSource">
            <option value="">all sources</option>
            <option value="heartbeat">heartbeat</option>
            <option value="conscious_worker">conscious_worker</option>
          </select>
          <input id="reasoningAction" type="text" placeholder="action_type filter" />
          <button onclick="refreshReasoning()">Apply</button>
        </div>
        <div id="reasoning" class="mono">loading...</div>
      </div>

      <div class="card">
        <h2>Safety Blocks</h2>
        <div class="row">
          <label class="muted">limit</label>
          <select id="safetyLimit">
            <option value="5">5</option>
            <option value="10" selected>10</option>
            <option value="20">20</option>
            <option value="50">50</option>
            <option value="100">100</option>
          </select>
          <button onclick="refreshSafety()">Refresh Safety</button>
        </div>
        <div id="safety" class="mono">loading...</div>
      </div>

      <div class="card">
        <h2>Logs</h2>
        <div class="row">
          <label class="muted">lines</label>
          <select id="logsLimit">
            <option value="50">50</option>
            <option value="100" selected>100</option>
            <option value="200">200</option>
            <option value="500">500</option>
            <option value="1000">1000</option>
          </select>
          <button onclick="refreshLogs()">Refresh Logs</button>
        </div>
        <div id="logs" class="mono">loading...</div>
      </div>

      <div class="card">
        <h2>Debug</h2>
        <div id="debug" class="mono">loading...</div>
      </div>
    </div>
  </div>
  <script>
    const token = () => document.getElementById('token').value.trim();
    const withToken = (path) => {
      const t = token();
      if (!t) return path;
      const sep = path.includes('?') ? '&' : '?';
      return path + sep + 'token=' + encodeURIComponent(t);
    };
    const conscious = () => document.getElementById('conscious').checked;

    async function parseResponse(r) {
      const text = await r.text();
      let data = {};
      try { data = text ? JSON.parse(text) : {}; } catch (_) { data = {raw: text}; }
      if (!r.ok) {
        const msg = data.error ? data.error : text;
        throw new Error(msg || ('HTTP ' + r.status));
      }
      return data;
    }

    async function apiGet(path) {
      const r = await fetch(withToken(path));
      return await parseResponse(r);
    }
    async function apiPost(path, payload) {
      const r = await fetch(withToken(path), {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify(payload || {}),
      });
      return await parseResponse(r);
    }
    function updatePauseBadge(paused) {
      const el = document.getElementById('pauseState');
      if (paused) {
        el.textContent = 'pause: ON';
        el.className = 'pill warn';
      } else {
        el.textContent = 'pause: OFF';
        el.className = 'pill ok';
      }
    }
    async function refreshStatus() {
      const d = await apiGet('/api/status');
      document.getElementById('status').textContent = JSON.stringify(d, null, 2);
      document.getElementById('postActivity').textContent = JSON.stringify(d.post_activity || {}, null, 2);
      const flags = d.control_flags || {};
      const paused = ['1', 'true', 'yes', 'on'].includes(String(flags.pause_actions || '').toLowerCase()) || !!(d.counts && d.counts.pause_actions);
      updatePauseBadge(paused);
      const thinkerEnabled = ['1', 'true', 'yes', 'on'].includes(String(flags.thinker_enabled || '').toLowerCase());
      const thinkerAutoQueue = !['0', 'false', 'no', 'off'].includes(String(flags.thinker_auto_queue || '1').toLowerCase());
      document.getElementById('thinkerEnabled').checked = thinkerEnabled;
      document.getElementById('thinkerAutoQueue').checked = thinkerAutoQueue;
      document.getElementById('thinkerMode').value = String(flags.thinker_mode || 'queue');
    }
    async function refreshActivity() {
      const limit = Number(document.getElementById('activityLimit').value || 10);
      const d = await apiGet('/api/activity?limit=' + encodeURIComponent(String(limit)));
      document.getElementById('activity').textContent = JSON.stringify(d, null, 2);
    }
    async function refreshLogs() {
      const lines = Number(document.getElementById('logsLimit').value || 100);
      const d = await apiGet('/api/logs?lines=' + encodeURIComponent(String(lines)));
      document.getElementById('logs').textContent = (d.lines || []).join('\\n');
    }
    async function refreshReasoning() {
      const limit = Number(document.getElementById('reasoningLimit').value || 10);
      const source = document.getElementById('reasoningSource').value;
      const actionType = document.getElementById('reasoningAction').value.trim();
      let path = '/api/reasoning?limit=' + encodeURIComponent(String(limit));
      if (source) path += '&source=' + encodeURIComponent(source);
      if (actionType) path += '&action_type=' + encodeURIComponent(actionType);
      const d = await apiGet(path);
      document.getElementById('reasoning').textContent = JSON.stringify(d, null, 2);
    }
    async function refreshSafety() {
      const limit = Number(document.getElementById('safetyLimit').value || 10);
      const d = await apiGet('/api/safety?limit=' + encodeURIComponent(String(limit)));
      document.getElementById('safety').textContent = JSON.stringify(d, null, 2);
    }
    async function refreshTimeline() {
      const limit = Number(document.getElementById('timelineLimit').value || 10);
      const d = await apiGet('/api/timeline?limit=' + encodeURIComponent(String(limit)));
      document.getElementById('timeline').textContent = JSON.stringify(d, null, 2);
    }
    async function refreshDebug() {
      const d = await apiGet('/api/debug/runtime');
      document.getElementById('debug').textContent = JSON.stringify(d, null, 2);
    }
    async function sendChat() {
      const mode = document.getElementById('mode').value;
      const question = document.getElementById('question').value;
      const instruction = document.getElementById('instruction').value;
      const d = await apiPost('/api/chat', {mode, question, instruction, conscious: conscious()});
      document.getElementById('reply').textContent = JSON.stringify(d, null, 2);
      await refreshAll();
    }
    async function thinkNow() {
      const d = await apiPost('/api/conscious/think', {conscious: conscious()});
      document.getElementById('controlResult').textContent = JSON.stringify(d, null, 2);
      await refreshAll();
    }
    async function setPause(paused) {
      const d = await apiPost('/api/control/pause', {paused});
      document.getElementById('controlResult').textContent = JSON.stringify(d, null, 2);
      updatePauseBadge(!!paused);
      await refreshAll();
    }
    async function reloadFramework() {
      const d = await apiPost('/api/control/reload_framework', {});
      document.getElementById('controlResult').textContent = JSON.stringify(d, null, 2);
      await refreshAll();
    }
    async function saveThinkerConfig() {
      const enabled = document.getElementById('thinkerEnabled').checked;
      const autoQueue = document.getElementById('thinkerAutoQueue').checked;
      const mode = document.getElementById('thinkerMode').value;
      const d = await apiPost('/api/control/thinker', {enabled, auto_queue: autoQueue, mode});
      document.getElementById('controlResult').textContent = JSON.stringify(d, null, 2);
      await refreshAll();
    }
    async function runOnce(dryRun) {
      if (!dryRun) {
        const ok = confirm('Run live heartbeat now? It may publish/comment immediately.');
        if (!ok) return;
      }
      const d = await apiPost('/api/control/run_once', {dry_run: dryRun, timeout_seconds: 300});
      document.getElementById('controlResult').textContent = JSON.stringify(d, null, 2);
      await refreshAll();
    }
    async function deletePost() {
      const postId = document.getElementById('deletePostId').value.trim();
      const confirmText = document.getElementById('deleteConfirm').value.trim();
      if (!postId) {
        alert('post_id required');
        return;
      }
      if (confirmText.toUpperCase() !== 'DELETE') {
        alert('Type DELETE in confirmation field');
        return;
      }
      const ok = confirm('Delete post ' + postId + '? This action cannot be undone.');
      if (!ok) return;
      const d = await apiPost('/api/control/delete_post', {post_id: postId, confirm_text: confirmText});
      document.getElementById('controlResult').textContent = JSON.stringify(d, null, 2);
      await refreshAll();
    }
    async function refreshAll() {
      const h = document.getElementById('health');
      try {
        await Promise.all([refreshStatus(), refreshActivity(), refreshTimeline(), refreshLogs(), refreshReasoning(), refreshSafety(), refreshDebug()]);
        h.textContent = 'OK';
        h.className = 'pill ok';
      } catch (e) {
        h.textContent = 'ERROR: ' + e.message;
        h.className = 'pill err';
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
@click.option("--conscious-dir", default="", help="Path to conscious-claude framework directory")
def main(host: str, port: int, config_dir: str | None, admin_token: str, conscious_dir: str) -> None:
    cfg = load_config(config_dir)
    root = Path(__file__).resolve().parent.parent
    storage = cfg.get("storage", {})
    state_path = root / storage.get("state_file", "data/state.json")
    db_path = root / storage.get("history_db", "data/history.db")
    log_path = root / storage.get("log_file", "data/agent.log")

    _ensure_admin_tables(db_path)

    token = admin_token.strip() or cfg.get("_secrets", {}).get("admin_token", "") or ""
    fw_dir = Path(conscious_dir) if conscious_dir else (root / "NEW" / "conscious-claude-master")

    AdminHandler.ctx = AdminContext(
        cfg=cfg,
        project_root=root,
        state_path=state_path,
        db_path=db_path,
        log_path=log_path,
        admin_token=token,
        framework_dir=fw_dir,
    )

    server = ThreadingHTTPServer((host, port), AdminHandler)
    click.echo(f"Mu admin UI listening on http://{host}:{port}")
    if token:
        click.echo("Admin token enabled (token query param or X-Admin-Token header).")
    click.echo(f"Conscious framework dir: {fw_dir}")
    click.echo(f"Conscious framework available: {AdminHandler.ctx.framework.available}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
