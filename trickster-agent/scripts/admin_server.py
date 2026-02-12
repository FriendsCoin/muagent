"""Admin UI and control plane for Mu.

FastAPI server with CORS support for the React dashboard (trickster-command).

Features:
- Status/activity/log/reasoning visibility
- Observe and influence chat modes
- Runtime controls (pause, run-once, reload framework)
- Optional conscious framework context
- Debug snapshot endpoint
- CORS enabled for cross-origin React dashboard

Usage:
    python scripts/admin_server.py                    # default 0.0.0.0:8000
    python scripts/admin_server.py --port 8787        # custom port
    python scripts/admin_server.py --admin-token abc  # require auth token
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
import base64
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Query, Body, Request, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import random

from agent.config import load_config
from agent.core import MuAgent
from agent.personality import Personality
from imagegen import VisualGenerator
from moltbook.client import MoltbookClient, MoltbookError
from nft import ObjktWebhookMinter

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


_REDACTED = "<redacted>"


def _redact_query_param(url: str, param: str) -> str:
    """Redact sensitive query params (e.g. key=) before sending JSON to browsers."""
    raw = str(url or "")
    if not raw or "?" not in raw:
        return raw
    # Keep it simple: redact param value without trying to fully re-encode the URL.
    return re.sub(rf"([?&]{re.escape(param)}=)[^&#]*", rf"\\1{_REDACTED}", raw, flags=re.IGNORECASE)


def _sanitize_meta(meta: Any) -> dict[str, Any]:
    """Remove/Redact secrets from provider metadata before returning over /api/*."""
    if not isinstance(meta, dict):
        return {}
    safe: dict[str, Any] = {}
    for k, v in meta.items():
        key = str(k)
        # Never expose signed URLs with embedded keys to the browser.
        if key in {"signed_url"}:
            continue
        if isinstance(v, str):
            safe[key] = _redact_query_param(v, "key")
        else:
            safe[key] = v
    return safe


def _sanitize_visual_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    out = dict(payload)
    if "url" in out:
        out["url"] = _redact_query_param(str(out.get("url") or ""), "key")
    out["meta"] = _sanitize_meta(out.get("meta"))
    return out


def _probe_media_url(public_url: str, *, signed_url: str = "", timeout_seconds: float = 8.0) -> dict[str, Any]:
    """Best-effort check that a media URL resolves (avoid false negatives from HEAD-only probing)."""

    def _ok_status(status: int) -> bool:
        return 200 <= status < 400

    def _looks_like_media(content_type: str) -> bool:
        ct = (content_type or "").split(";", 1)[0].strip().lower()
        if not ct:
            return True  # Some CDNs omit CT on HEAD; allow if status is OK.
        # Pollinations errors are JSON; treat that as non-media for our purposes.
        if ct == "application/json":
            return False
        return True

    def _probe_one(url: str) -> dict[str, Any]:
        if not str(url or "").startswith("http"):
            return {"ok": False, "method": "", "status_code": 0, "content_type": "", "error": "invalid_url"}
        try:
            import httpx

            # 1) HEAD (fast path)
            h = httpx.head(url, timeout=timeout_seconds, follow_redirects=True)
            ct = str(h.headers.get("content-type", ""))
            if _ok_status(h.status_code) and _looks_like_media(ct):
                return {"ok": True, "method": "HEAD", "status_code": h.status_code, "content_type": ct, "error": ""}

            # 2) GET with Range (avoid downloading large bodies)
            headers = {"Range": "bytes=0-0"}
            with httpx.stream("GET", url, headers=headers, timeout=timeout_seconds, follow_redirects=True) as g:
                ct2 = str(g.headers.get("content-type", ""))
                ok = _ok_status(g.status_code) and _looks_like_media(ct2)
                return {"ok": ok, "method": "GET", "status_code": g.status_code, "content_type": ct2, "error": ""}
        except Exception as exc:
            return {"ok": False, "method": "", "status_code": 0, "content_type": "", "error": str(exc)[:160]}

    public = _probe_one(public_url)
    signed = _probe_one(signed_url) if (signed_url and signed_url != public_url) else {}
    used = ""
    if public.get("ok"):
        used = "public"
    elif signed.get("ok"):
        used = "signed"
    return {
        "ok": bool(public.get("ok") or signed.get("ok")),
        "used": used,
        "requires_key": bool(used == "signed" and not public.get("ok")),
        "public": public,
        "signed": signed if signed else {},
    }


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


def _json_load_maybe_dict(text: Any) -> dict[str, Any]:
    if isinstance(text, dict):
        return text
    if not isinstance(text, str) or not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _json_load_maybe_list(text: Any) -> list[Any]:
    if isinstance(text, list):
        return text
    if not isinstance(text, str) or not text.strip():
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS nft_drafts (
                id TEXT PRIMARY KEY,
                source_post_id TEXT,
                source_moltbook_id TEXT,
                image_url TEXT,
                title TEXT,
                description TEXT,
                tags TEXT,
                edition_size INTEGER,
                royalty_bps INTEGER,
                status TEXT,
                mint_tx_hash TEXT,
                mint_token_id TEXT,
                mint_url TEXT,
                error TEXT,
                metadata TEXT,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS research_sessions (
                id TEXT PRIMARY KEY,
                trigger TEXT,
                query TEXT,
                urls_visited TEXT,
                raw_content TEXT,
                reflection TEXT,
                topics TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS media_previews (
                id TEXT PRIMARY KEY,
                media_type TEXT,
                public_url TEXT,
                require_key INTEGER,
                created_at TEXT
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
    agent: MuAgent | None = None
    _heartbeat_task: asyncio.Task | None = None
    _loop_running: bool = False

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

    def create_media_preview(self, *, media_type: str, public_url: str, require_key: bool) -> str:
        preview_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO media_previews (id, media_type, public_url, require_key, created_at) VALUES (?, ?, ?, ?, ?)",
                (preview_id, str(media_type), str(public_url), 1 if bool(require_key) else 0, _now_iso()),
            )
            # Best-effort prune: keep DB from growing forever.
            conn.execute(
                "DELETE FROM media_previews WHERE id NOT IN (SELECT id FROM media_previews ORDER BY created_at DESC LIMIT 200)"
            )
            conn.commit()
        return preview_id

    def get_media_preview(self, preview_id: str) -> dict[str, Any] | None:
        pid = str(preview_id or "").strip()
        if not pid:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, media_type, public_url, require_key, created_at FROM media_previews WHERE id = ? LIMIT 1",
                (pid,),
            ).fetchone()
        return dict(row) if row else None

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
            "nft_drafts": 0,
            "nft_minted": 0,
            "research_sessions": 0,
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
                counts["nft_drafts"] = conn.execute("SELECT COUNT(*) FROM nft_drafts").fetchone()[0]
                counts["nft_minted"] = conn.execute(
                    "SELECT COUNT(*) FROM nft_drafts WHERE status = 'minted'"
                ).fetchone()[0]
                try:
                    counts["research_sessions"] = conn.execute(
                        "SELECT COUNT(*) FROM research_sessions"
                    ).fetchone()[0]
                except sqlite3.Error:
                    pass
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

    def _visual_effective_flags(self) -> dict[str, Any]:
        flags = self.get_control_flags()
        enabled_raw = str(flags.get("visual_enabled", "")).strip().lower()
        if enabled_raw in {"1", "true", "yes", "on"}:
            enabled = True
        elif enabled_raw in {"0", "false", "no", "off"}:
            enabled = False
        else:
            enabled = bool(self.cfg.get("visual_posting", {}).get("enabled", False))

        mode = str(flags.get("visual_mode", "auto")).strip().lower() or "auto"
        if mode not in {"auto", "url", "ascii", "audio", "video", "off"}:
            mode = "auto"

        provider = str(flags.get("visual_url_provider", "")).strip().lower()
        if not provider:
            provider = str(self.cfg.get("visual_posting", {}).get("url", {}).get("provider", "pollinations"))
        image_model = str(flags.get("visual_image_model", "")).strip()
        if not image_model:
            image_model = str(self.cfg.get("visual_posting", {}).get("url", {}).get("model", "")).strip()
        fallback_provider = str(flags.get("visual_fallback_provider", "")).strip().lower()
        if not fallback_provider:
            fallback_provider = str(
                self.cfg.get("visual_posting", {}).get("url", {}).get("fallback_provider", "pollinations")
            )
        if fallback_provider not in {"pollinations", "ascii"}:
            fallback_provider = "pollinations"

        video_provider = str(flags.get("visual_video_provider", "")).strip().lower()
        if not video_provider:
            video_provider = str(
                self.cfg.get("visual_posting", {}).get("media", {}).get("video_provider", "pollinations")
            ).strip().lower()
        if video_provider not in {"pollinations", "fal"}:
            video_provider = "pollinations"

        video_model = str(flags.get("visual_video_model", "")).strip()
        if not video_model:
            video_model = str(self.cfg.get("visual_posting", {}).get("media", {}).get("video_model", "")).strip()

        audio_model = str(flags.get("visual_audio_model", "")).strip()
        if not audio_model:
            audio_model = str(self.cfg.get("visual_posting", {}).get("media", {}).get("audio_model", "")).strip()
        audio_voice = str(flags.get("visual_audio_voice", "")).strip()
        if not audio_voice:
            audio_voice = str(self.cfg.get("visual_posting", {}).get("media", {}).get("audio_voice", "")).strip()
        audio_format = str(flags.get("visual_audio_format", "")).strip().lower()
        if not audio_format:
            audio_format = str(self.cfg.get("visual_posting", {}).get("media", {}).get("audio_format", "")).strip().lower()
        audio_duration_raw = str(flags.get("visual_audio_duration", "")).strip()
        audio_duration: int | None = None
        if audio_duration_raw:
            try:
                audio_duration = max(1, min(300, int(audio_duration_raw)))
            except ValueError:
                audio_duration = None
        audio_instrumental_raw = str(flags.get("visual_audio_instrumental", "")).strip().lower()
        audio_instrumental: bool | None = None
        if audio_instrumental_raw:
            if audio_instrumental_raw in {"1", "true", "yes", "on"}:
                audio_instrumental = True
            elif audio_instrumental_raw in {"0", "false", "no", "off"}:
                audio_instrumental = False
        video_include_audio_raw = str(flags.get("visual_video_include_audio", "")).strip().lower()
        video_include_audio: bool | None = None
        if video_include_audio_raw:
            if video_include_audio_raw in {"1", "true", "yes", "on"}:
                video_include_audio = True
            elif video_include_audio_raw in {"0", "false", "no", "off"}:
                video_include_audio = False
        if video_include_audio is None:
            video_include_audio = bool(self.cfg.get("visual_posting", {}).get("media", {}).get("video_include_audio", False))

        attempts_raw = str(flags.get("visual_runware_max_attempts", "")).strip()
        attempts = None
        if attempts_raw:
            try:
                attempts = max(1, min(5, int(attempts_raw)))
            except ValueError:
                attempts = None

        prob_raw = str(flags.get("visual_attach_probability", "")).strip()
        prob = None
        if prob_raw:
            try:
                prob = max(0.0, min(1.0, float(prob_raw)))
            except ValueError:
                prob = None

        return {
            "enabled": enabled,
            "mode": mode,
            "url_provider": provider,
            "image_model": image_model,
            "fallback_provider": fallback_provider,
            "video_provider": video_provider,
            "video_model": video_model,
            "audio_model": audio_model,
            "audio_voice": audio_voice,
            "audio_format": audio_format,
            "audio_duration": audio_duration,
            "audio_instrumental": audio_instrumental,
            "video_include_audio": video_include_audio,
            "runware_max_attempts_override": attempts,
            "attach_probability_override": prob,
        }

    def fetch_visual_status(self, limit: int = 20) -> dict[str, Any]:
        visual_cfg = self.cfg.get("visual_posting", {}) if isinstance(self.cfg, dict) else {}
        secrets = self.cfg.get("_secrets", {}) if isinstance(self.cfg, dict) else {}
        effective = self._visual_effective_flags()

        recent_visual_posts: list[dict[str, Any]] = []
        recent_visual_events: list[dict[str, Any]] = []
        if self.db_path.exists():
            try:
                with self._connect() as conn:
                    rows = conn.execute(
                        "SELECT id, created_at, title, submolt, image_path, moltbook_id "
                        "FROM posts WHERE COALESCE(image_path, '') != '' "
                        "ORDER BY created_at DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                    recent_visual_posts = [dict(r) for r in rows]

                    event_rows = conn.execute(
                        "SELECT id, event_type, description, metadata, created_at "
                        "FROM narrative_events "
                        "WHERE event_type IN ('visual_generated', 'visual_error') "
                        "ORDER BY created_at DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
            except sqlite3.Error:
                event_rows = []
            else:
                for row in event_rows:
                    item = dict(row)
                    raw = item.get("metadata", "")
                    if isinstance(raw, str) and raw:
                        try:
                            item["metadata"] = json.loads(raw)
                        except json.JSONDecodeError:
                            pass
                    recent_visual_events.append(item)

        runware_key_present = bool(str(secrets.get("runware_api_key", "")).strip())
        runware_requested_recent = 0
        runware_success_recent = 0
        runware_fallback_recent = 0
        runware_last_event_at = ""
        runware_last_provider = ""
        runware_last_error_reason = ""
        runware_last_error_detail = ""
        for item in recent_visual_events:
            if str(item.get("event_type", "")) != "visual_generated":
                continue
            metadata = item.get("metadata", {})
            if not isinstance(metadata, dict):
                continue
            requested = str(
                metadata.get("requested_url_provider")
                or metadata.get("provider_override")
                or ""
            ).strip().lower()
            if requested != "runware":
                continue
            runware_requested_recent += 1
            provider = str(metadata.get("provider", "")).strip().lower()
            fallback = _to_bool(metadata.get("runware_fallback_used", False), default=False)
            if provider == "runware":
                runware_success_recent += 1
            if fallback or (provider and provider != "runware"):
                runware_fallback_recent += 1
            if not runware_last_event_at:
                runware_last_event_at = str(item.get("created_at") or "")
                runware_last_provider = provider
                runware_last_error_reason = str(metadata.get("runware_error_reason", "")).strip().lower()
                runware_last_error_detail = str(metadata.get("runware_error_detail", "")).strip()

        if not runware_key_present:
            runware_state = "no_key"
            runware_message = "RUNWARE_API_KEY missing"
        elif runware_requested_recent == 0:
            runware_state = "unknown"
            runware_message = "No recent runware-targeted visual events"
        elif runware_success_recent > 0 and runware_fallback_recent == 0:
            runware_state = "ok"
            runware_message = "Runware used successfully"
        elif runware_success_recent == 0 and runware_fallback_recent > 0:
            runware_state = "fallback"
            if runware_last_error_reason:
                runware_message = f"Fallback after runware error: {runware_last_error_reason}"
            else:
                runware_message = "Runware requests fell back to alternate provider"
        else:
            runware_state = "mixed"
            runware_message = "Mixed runware success and fallback"

        return {
            "enabled_in_config": bool(visual_cfg.get("enabled", False)),
            "effective": effective,
            "providers": {
                "url_configured_provider": str(visual_cfg.get("url", {}).get("provider", "pollinations")),
                "fallback_provider": str(visual_cfg.get("url", {}).get("fallback_provider", "pollinations")),
                "video_provider": str(visual_cfg.get("media", {}).get("video_provider", "pollinations")),
                "runware_key_present": runware_key_present,
                "runware_endpoint": str(visual_cfg.get("url", {}).get("endpoint", "https://api.runware.ai/v1")),
                "runware_max_attempts": int(visual_cfg.get("url", {}).get("runware_max_attempts", 1)),
                "pollinations_key_present": bool(str(secrets.get("pollinations_api_key", "")).strip()),
                "fal_key_present": bool(str(secrets.get("fal_key", "")).strip()),
                "fal_endpoint_base": str(
                    visual_cfg.get("media", {}).get("fal", {}).get("endpoint_base", "https://queue.fal.run")
                ),
                "fal_model": str(
                    visual_cfg.get("media", {}).get("fal", {}).get("model", "fal-ai/wan/v2.2-a14b/text-to-video/turbo")
                ),
                "pollinations_enter_endpoint": str(
                    visual_cfg.get("url", {}).get("pollinations_enter_endpoint", "https://gen.pollinations.ai/openai")
                ),
            },
            "runware_runtime": {
                "state": runware_state,
                "message": runware_message,
                "requested_recent": runware_requested_recent,
                "success_recent": runware_success_recent,
                "fallback_recent": runware_fallback_recent,
                "last_event_at": runware_last_event_at,
                "last_provider": runware_last_provider,
                "last_error_reason": runware_last_error_reason,
                "last_error_detail": runware_last_error_detail,
            },
            "mode_weights": visual_cfg.get("mode_weights", {"url": 0.65, "ascii": 0.35}),
            "attach_probability": visual_cfg.get(
                "attach_probability",
                {"emergence": 0.35, "patterns": 0.45, "tension": 0.5, "mirror": 0.4},
            ),
            "control_flags": {k: v for k, v in self.get_control_flags().items() if k.startswith("visual_")},
            "recent_visual_posts": recent_visual_posts,
            "recent_visual_events": recent_visual_events,
            "limit": limit,
        }

    def fetch_visual_why(self, limit: int = 20) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        if not self.db_path.exists():
            return {"items": items, "limit": limit}

        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id, created_at, description, metadata "
                    "FROM narrative_events "
                    "WHERE event_type = 'visual_generated' "
                    "ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        except sqlite3.Error:
            rows = []

        for row in rows:
            raw = dict(row)
            metadata: dict[str, Any] = {}
            raw_meta = raw.get("metadata", "")
            if isinstance(raw_meta, str) and raw_meta:
                try:
                    loaded = json.loads(raw_meta)
                    if isinstance(loaded, dict):
                        metadata = loaded
                except json.JSONDecodeError:
                    metadata = {}

            feed_topics = metadata.get("feed_trending_topics", [])
            if not isinstance(feed_topics, list):
                feed_topics = []
            feed_titles = metadata.get("feed_top_titles", [])
            if not isinstance(feed_titles, list):
                feed_titles = []

            context_source = str(metadata.get("context_source", "")).strip()
            if not context_source:
                derived: list[str] = []
                if str(metadata.get("operator_instruction", "")).strip():
                    derived.append("operator_instruction")
                if str(metadata.get("feed_visual_keywords", "")).strip():
                    derived.append("feed_keywords")
                if feed_topics:
                    derived.append("trending_topics")
                if not derived:
                    derived.append("theme_only")
                context_source = "+".join(derived)

            items.append(
                {
                    "id": str(raw.get("id") or ""),
                    "created_at": str(raw.get("created_at") or ""),
                    "description": str(raw.get("description") or ""),
                    "kind": str(metadata.get("kind", "")),
                    "provider": str(metadata.get("provider", "")),
                    "visual_url": str(metadata.get("visual_url", "")),
                    "theme": str(metadata.get("theme", "")),
                    "phase": str(metadata.get("phase", "")),
                    "day": metadata.get("day"),
                    "context_source": context_source,
                    "why": {
                        "mode_flag": str(metadata.get("mode_flag", "")),
                        "operator_forced": str(metadata.get("operator_forced", "")),
                        "visual_url": str(metadata.get("visual_url", "")),
                        "feed_visual_keywords": str(metadata.get("feed_visual_keywords", "")),
                        "visual_context": str(metadata.get("visual_context", "")),
                        "operator_instruction": str(metadata.get("operator_instruction", "")),
                        "runware_error_reason": str(metadata.get("runware_error_reason", "")),
                        "runware_error_detail": str(metadata.get("runware_error_detail", "")),
                        "runware_fallback_used": _to_bool(metadata.get("runware_fallback_used", False), default=False),
                        "pollinations_enter_error_reason": str(metadata.get("pollinations_enter_error_reason", "")),
                        "pollinations_enter_error_detail": str(metadata.get("pollinations_enter_error_detail", "")),
                        "pollinations_enter_fallback_used": _to_bool(
                            metadata.get("pollinations_enter_fallback_used", False), default=False
                        ),
                        "fallback_provider_used": str(metadata.get("fallback_provider_used", "")),
                        "feed_trending_topics": feed_topics,
                        "feed_top_titles": feed_titles,
                    },
                }
            )
        return {"items": items, "limit": limit}

    def test_visual_generation(
        self,
        *,
        prompt: str,
        mode: str = "auto",
        phase: str = "emergence",
        day: int = 1,
        video_provider: str = "",
        video_model: str = "",
        audio_model: str = "",
        audio_voice: str = "",
        audio_format: str = "",
        audio_duration: int | None = None,
        audio_instrumental: bool | None = None,
        include_audio: bool | None = None,
    ) -> dict[str, Any]:
        generator = VisualGenerator(self.cfg)
        if mode in {"audio", "video"}:
            if include_audio is None:
                include_audio = mode == "audio"
            return self.test_media_narration(
                prompt=prompt,
                mode=mode,
                phase=phase,
                day=day,
                video_provider=video_provider,
                video_model=video_model,
                audio_model=audio_model,
                audio_voice=audio_voice,
                audio_format=audio_format,
                audio_duration=audio_duration,
                audio_instrumental=audio_instrumental,
                include_audio=include_audio,
            )

        force_mode = mode if mode in {"url", "ascii"} else ""
        effective = self._visual_effective_flags()
        vp = str(video_provider or effective.get("video_provider") or "").strip().lower()
        visual = generator.generate(
            theme=prompt[:80] or "mystery",
            mood="soft_ominous",
            phase=phase or "emergence",
            day=max(1, int(day)),
            context=prompt,
            force_mode=force_mode,
            enabled_override=effective["enabled"],
            attach_probability_override=1.0 if mode != "auto" else effective["attach_probability_override"],
            url_provider_override=str(effective["url_provider"] or ""),
            url_model_override=str(effective.get("image_model") or ""),
            fallback_provider_override=str(effective.get("fallback_provider") or ""),
            video_provider_override=vp,
            video_model_override=video_model or str(effective.get("video_model") or ""),
            audio_model_override=audio_model or str(effective.get("audio_model") or ""),
            audio_voice_override=audio_voice or str(effective.get("audio_voice") or ""),
            audio_format_override=audio_format or str(effective.get("audio_format") or ""),
            audio_duration_override=audio_duration if audio_duration is not None else effective.get("audio_duration"),
            audio_instrumental_override=(
                audio_instrumental if audio_instrumental is not None else effective.get("audio_instrumental")
            ),
            runware_max_attempts_override=effective.get("runware_max_attempts_override"),
        )
        payload = {
            "kind": visual.kind,
            "provider": visual.provider,
            "prompt": visual.prompt,
            "url": _redact_query_param(visual.url, "key"),
            "ascii_art": visual.ascii_art,
            "meta": _sanitize_meta(visual.meta),
            "phase": phase,
            "day": day,
            "mode": mode,
        }
        return payload

    @staticmethod
    def _guess_image_media_type(data: bytes) -> str:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith(b"\xff\xd8"):
            return "image/jpeg"
        return "image/jpeg"

    def test_media_narration(
        self,
        *,
        prompt: str,
        mode: str,
        phase: str = "emergence",
        day: int = 1,
        video_provider: str = "",
        video_model: str = "",
        audio_model: str = "",
        audio_voice: str = "",
        audio_format: str = "",
        audio_duration: int | None = None,
        audio_instrumental: bool | None = None,
        include_audio: bool = True,
    ) -> dict[str, Any]:
        """Generate visual media and optional narrated audio."""
        generator = VisualGenerator(self.cfg)
        effective = self._visual_effective_flags()
        vp = str(video_provider or effective.get("video_provider") or "").strip().lower()

        base_force = "url" if mode == "audio" else "video"
        base_visual_candidate = generator.generate(
            theme=prompt[:80] or "mystery",
            mood="soft_ominous",
            phase=phase or "emergence",
            day=max(1, int(day)),
            context=prompt,
            force_mode=base_force,
            enabled_override=effective["enabled"],
            attach_probability_override=1.0,
            url_provider_override=str(effective["url_provider"] or ""),
            url_model_override=str(effective.get("image_model") or ""),
            fallback_provider_override=str(effective.get("fallback_provider") or ""),
            video_provider_override=vp,
            video_model_override=video_model or str(effective.get("video_model") or ""),
            audio_model_override=audio_model or str(effective.get("audio_model") or ""),
            audio_voice_override=audio_voice or str(effective.get("audio_voice") or ""),
            audio_format_override=audio_format or str(effective.get("audio_format") or ""),
            audio_duration_override=audio_duration if audio_duration is not None else effective.get("audio_duration"),
            audio_instrumental_override=(
                audio_instrumental if audio_instrumental is not None else effective.get("audio_instrumental")
            ),
            runware_max_attempts_override=effective.get("runware_max_attempts_override"),
        )

        base_visual = base_visual_candidate
        video_probe: dict[str, Any] = {}
        preview: dict[str, Any] | None = None

        # Pollinations video is not always available. Avoid HEAD-only false negatives and don't leak signed URLs.
        if base_force == "video" and str(base_visual_candidate.url or "").startswith("http"):
            signed_url = str((base_visual_candidate.meta or {}).get("signed_url", "")).strip()
            video_probe = _probe_media_url(
                str(base_visual_candidate.url), signed_url=signed_url, timeout_seconds=8.0
            )
            key_in_url = bool((base_visual_candidate.meta or {}).get("key_in_url"))
            ok_for_post = bool(video_probe.get("ok") and (video_probe.get("used") == "public" or key_in_url))
            if video_probe.get("requires_key"):
                public_url = str((base_visual_candidate.meta or {}).get("public_url") or base_visual_candidate.url).strip()
                if public_url.startswith("http"):
                    pid = self.create_media_preview(media_type="video", public_url=public_url, require_key=True)
                    preview = {"id": pid, "url": f"/api/media/preview/{pid}", "requires_key": True}
            if not ok_for_post:
                base_visual = generator.generate(
                    theme=prompt[:80] or "mystery",
                    mood="soft_ominous",
                    phase=phase or "emergence",
                    day=max(1, int(day)),
                    context=prompt,
                    force_mode="url",
                    enabled_override=effective["enabled"],
                    attach_probability_override=1.0,
                    url_provider_override=str(effective["url_provider"] or ""),
                    url_model_override=str(effective.get("image_model") or ""),
                    fallback_provider_override=str(effective.get("fallback_provider") or ""),
                    video_provider_override=vp,
                    video_model_override=video_model or str(effective.get("video_model") or ""),
                    audio_model_override=audio_model or str(effective.get("audio_model") or ""),
                    audio_voice_override=audio_voice or str(effective.get("audio_voice") or ""),
                    audio_format_override=audio_format or str(effective.get("audio_format") or ""),
                    audio_duration_override=audio_duration if audio_duration is not None else effective.get("audio_duration"),
                    audio_instrumental_override=(
                        audio_instrumental if audio_instrumental is not None else effective.get("audio_instrumental")
                    ),
                    runware_max_attempts_override=effective.get("runware_max_attempts_override"),
                )
                base_visual.meta = dict(base_visual.meta or {})
                base_visual.meta["video_fallback_used"] = True
                if video_probe.get("requires_key"):
                    base_visual.meta["video_error_reason"] = "requires_api_key"
                else:
                    status = (video_probe.get("public") or {}).get("status_code") or 0
                    base_visual.meta["video_error_reason"] = f"http_{int(status) if status else 0}"

        image_bytes: bytes | None = None
        image_media_type = "image/jpeg"
        if base_force == "url" and base_visual.url.startswith("http"):
            try:
                import httpx

                r = httpx.get(base_visual.url, timeout=12)
                if r.status_code == 200 and r.content and len(r.content) <= 1_200_000:
                    image_bytes = bytes(r.content)
                    image_media_type = self._guess_image_media_type(image_bytes)
            except Exception:
                image_bytes = None

        narration = ""
        audio_payload: dict[str, Any] | None = None
        if include_audio:
            narration = self.personality().generate_media_narration(
                visual_prompt=base_visual.prompt or prompt,
                phase=phase or "emergence",
                day=max(1, int(day)),
                image_bytes=image_bytes,
                image_media_type=image_media_type,
            )
            audio = generator.tts_from_text(
                narration,
                voice_override=audio_voice or str(effective.get("audio_voice") or ""),
                model_override=audio_model or str(effective.get("audio_model") or ""),
                format_override=audio_format or str(effective.get("audio_format") or ""),
                duration_override=audio_duration if audio_duration is not None else effective.get("audio_duration"),
                instrumental_override=(
                    audio_instrumental if audio_instrumental is not None else effective.get("audio_instrumental")
                ),
            )
            audio_payload = {
                "kind": audio.kind,
                "provider": audio.provider,
                "prompt": audio.prompt,
                "url": _redact_query_param(audio.url, "key"),
                "meta": _sanitize_meta(audio.meta),
            }

        candidate_visual: dict[str, Any] | None = None
        if base_force == "video":
            candidate_visual = _sanitize_visual_payload(
                {
                    "kind": base_visual_candidate.kind,
                    "provider": base_visual_candidate.provider,
                    "prompt": base_visual_candidate.prompt,
                    "url": base_visual_candidate.url,
                    "meta": base_visual_candidate.meta,
                }
            )

        return {
            "mode": mode,
            "phase": phase,
            "day": day,
            "include_audio": bool(include_audio),
            "base_visual": {
                "kind": base_visual.kind,
                "provider": base_visual.provider,
                "prompt": base_visual.prompt,
                "url": _redact_query_param(base_visual.url, "key"),
                "meta": _sanitize_meta(base_visual.meta),
            },
            "video_candidate": candidate_visual,
            "video_probe": video_probe,
            "preview": preview,
            "narration": narration,
            "audio": audio_payload,
        }

    def _nft_effective_flags(self) -> dict[str, Any]:
        flags = self.get_control_flags()
        nft_cfg = self.cfg.get("nft", {}) if isinstance(self.cfg, dict) else {}

        enabled_raw = str(flags.get("nft_enabled", "")).strip().lower()
        if enabled_raw in {"1", "true", "yes", "on"}:
            enabled = True
        elif enabled_raw in {"0", "false", "no", "off"}:
            enabled = False
        else:
            enabled = bool(nft_cfg.get("enabled", False))

        mode = str(flags.get("nft_mode", "")).strip().lower()
        if not mode:
            mode = str(nft_cfg.get("mode", "draft")).strip().lower()
        if mode not in {"off", "draft", "manual", "auto"}:
            mode = "draft"

        auto_draft_raw = str(flags.get("nft_auto_draft", "")).strip().lower()
        if auto_draft_raw in {"1", "true", "yes", "on"}:
            auto_draft = True
        elif auto_draft_raw in {"0", "false", "no", "off"}:
            auto_draft = False
        else:
            auto_draft = bool(nft_cfg.get("auto_create_from_visual_posts", True))

        return {
            "enabled": enabled,
            "mode": mode,
            "auto_draft": auto_draft,
        }

    @staticmethod
    def _normalize_nft_row(row: dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["tags"] = _json_load_maybe_list(item.get("tags", ""))
        item["metadata"] = _json_load_maybe_dict(item.get("metadata", ""))
        return item

    def fetch_nft_status(self, limit: int = 20) -> dict[str, Any]:
        nft_cfg = self.cfg.get("nft", {}) if isinstance(self.cfg, dict) else {}
        secrets = self.cfg.get("_secrets", {}) if isinstance(self.cfg, dict) else {}
        effective = self._nft_effective_flags()

        recent_drafts: list[dict[str, Any]] = []
        counts = {"total": 0, "draft": 0, "minting": 0, "minted": 0, "failed": 0, "simulated": 0}
        visual_candidates: list[dict[str, Any]] = []
        if self.db_path.exists():
            try:
                with self._connect() as conn:
                    rows = conn.execute(
                        "SELECT * FROM nft_drafts ORDER BY created_at DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                    recent_drafts = [self._normalize_nft_row(dict(r)) for r in rows]

                    counts["total"] = conn.execute("SELECT COUNT(*) FROM nft_drafts").fetchone()[0]
                    for st in ("draft", "minting", "minted", "failed", "simulated"):
                        counts[st] = conn.execute(
                            "SELECT COUNT(*) FROM nft_drafts WHERE status = ?",
                            (st,),
                        ).fetchone()[0]

                    candidates = conn.execute(
                        "SELECT id, moltbook_id, title, submolt, image_path, created_at "
                        "FROM posts "
                        "WHERE image_path LIKE 'http%' "
                        "ORDER BY created_at DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                    visual_candidates = [dict(r) for r in candidates]
            except sqlite3.Error:
                pass

        objkt_cfg = nft_cfg.get("objkt", {}) if isinstance(nft_cfg, dict) else {}
        webhook_url = str(objkt_cfg.get("mint_webhook_url", "")).strip()
        return {
            "effective": effective,
            "counts": counts,
            "defaults": {
                "edition_size": int(nft_cfg.get("default_edition_size", 1)),
                "royalty_bps": int(nft_cfg.get("default_royalty_bps", 500)),
                "tags": nft_cfg.get("tags", []),
            },
            "objkt": {
                "webhook_configured": bool(webhook_url),
                "webhook_url": webhook_url,
                "token_present": bool(str(secrets.get("objkt_webhook_token", "")).strip()),
                "collection_id": str(objkt_cfg.get("collection_id", "")),
                "creator_address": str(objkt_cfg.get("creator_address", "")),
            },
            "control_flags": {k: v for k, v in self.get_control_flags().items() if k.startswith("nft_")},
            "recent_drafts": recent_drafts,
            "visual_candidates": visual_candidates,
            "limit": limit,
        }

    def create_nft_draft_from_post(self, *, post_ref: str = "") -> dict[str, Any]:
        nft_cfg = self.cfg.get("nft", {}) if isinstance(self.cfg, dict) else {}
        tags_default = nft_cfg.get("tags", []) if isinstance(nft_cfg, dict) else []
        edition_size = max(1, int(nft_cfg.get("default_edition_size", 1)))
        royalty_bps = max(0, int(nft_cfg.get("default_royalty_bps", 500)))
        now = _now_iso()

        with self._connect() as conn:
            post_row = None
            if post_ref:
                post_row = conn.execute(
                    "SELECT id, moltbook_id, title, content, submolt, image_path, created_at "
                    "FROM posts WHERE (id = ? OR moltbook_id = ?) AND image_path LIKE 'http%' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (post_ref, post_ref),
                ).fetchone()
            if post_row is None:
                post_row = conn.execute(
                    "SELECT id, moltbook_id, title, content, submolt, image_path, created_at "
                    "FROM posts WHERE image_path LIKE 'http%' ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
            if post_row is None:
                raise ValueError("no_visual_post_with_url_found")

            post = dict(post_row)
            source_moltbook_id = str(post.get("moltbook_id") or post.get("id") or "").strip()
            if source_moltbook_id:
                existing = conn.execute(
                    "SELECT id FROM nft_drafts WHERE source_moltbook_id = ? LIMIT 1",
                    (source_moltbook_id,),
                ).fetchone()
                if existing:
                    draft = conn.execute(
                        "SELECT * FROM nft_drafts WHERE id = ? LIMIT 1",
                        (existing["id"],),
                    ).fetchone()
                    return {"created": False, "draft": self._normalize_nft_row(dict(draft))}

            draft_id = str(uuid.uuid4())
            title = str(post.get("title", "") or "Mu Artifact").strip()[:120]
            content = str(post.get("content", "") or "").strip()
            description = content[:1800] or f"Mu artifact generated from post {source_moltbook_id}."
            image_url = str(post.get("image_path", "")).strip()
            tags = []
            for tag in list(tags_default) + ["mu", str(post.get("submolt", "")).strip(), "moltbook"]:
                clean = str(tag or "").strip()
                if clean and clean.lower() not in {t.lower() for t in tags}:
                    tags.append(clean)

            metadata = {
                "source": "post_visual_url",
                "source_post_id": str(post.get("id", "")),
                "source_moltbook_id": source_moltbook_id,
                "post_created_at": str(post.get("created_at", "")),
            }
            conn.execute(
                "INSERT INTO nft_drafts ("
                "id, source_post_id, source_moltbook_id, image_url, title, description, tags, edition_size, royalty_bps, "
                "status, mint_tx_hash, mint_token_id, mint_url, error, metadata, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', '', '', '', '', ?, ?, ?)",
                (
                    draft_id,
                    str(post.get("id", "")),
                    source_moltbook_id,
                    image_url,
                    title,
                    description,
                    json.dumps(tags),
                    edition_size,
                    royalty_bps,
                    json.dumps(metadata),
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO narrative_events (id, event_type, description, created_at, metadata) VALUES (?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    "nft_draft_created",
                    f"draft={draft_id} source={source_moltbook_id}",
                    now,
                    json.dumps({"draft_id": draft_id, "source_moltbook_id": source_moltbook_id}),
                ),
            )
            conn.commit()

            draft = conn.execute("SELECT * FROM nft_drafts WHERE id = ? LIMIT 1", (draft_id,)).fetchone()
            return {"created": True, "draft": self._normalize_nft_row(dict(draft))}

    def mint_nft_draft(self, draft_id: str) -> dict[str, Any]:
        now = _now_iso()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM nft_drafts WHERE id = ? LIMIT 1", (draft_id,)).fetchone()
            if row is None:
                raise ValueError("draft_not_found")
            draft = self._normalize_nft_row(dict(row))
            if str(draft.get("status", "")).lower() == "minted":
                return {"ok": True, "already_minted": True, "draft": draft}

            conn.execute(
                "UPDATE nft_drafts SET status = 'minting', error = '', updated_at = ? WHERE id = ?",
                (now, draft_id),
            )
            conn.commit()

        minter = ObjktWebhookMinter(self.cfg)
        result = minter.mint(draft)
        raw = result.raw_response if isinstance(result.raw_response, dict) else {}
        dry_run = bool(raw.get("raw", {}).get("dry_run")) or str(result.message).startswith("dry_run")
        now2 = _now_iso()
        with self._connect() as conn:
            if result.ok and not dry_run:
                conn.execute(
                    "UPDATE nft_drafts SET status = 'minted', mint_tx_hash = ?, mint_token_id = ?, mint_url = ?, "
                    "error = '', updated_at = ? WHERE id = ?",
                    (result.tx_hash, result.token_id, result.token_url, now2, draft_id),
                )
                event_type = "nft_minted"
                event_desc = f"draft={draft_id} token={result.token_id or '?'}"
            elif result.ok and dry_run:
                conn.execute(
                    "UPDATE nft_drafts SET status = 'simulated', mint_tx_hash = ?, mint_token_id = ?, mint_url = ?, "
                    "error = '', updated_at = ? WHERE id = ?",
                    (result.tx_hash, result.token_id, result.token_url, now2, draft_id),
                )
                event_type = "nft_mint_simulated"
                event_desc = f"draft={draft_id} simulated_only"
            else:
                conn.execute(
                    "UPDATE nft_drafts SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                    (result.message[:2000], now2, draft_id),
                )
                event_type = "nft_mint_failed"
                event_desc = f"draft={draft_id} error={result.message[:120]}"

            conn.execute(
                "INSERT INTO narrative_events (id, event_type, description, created_at, metadata) VALUES (?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    event_type,
                    event_desc,
                    now2,
                    json.dumps(
                        {
                            "draft_id": draft_id,
                            "ok": result.ok,
                            "tx_hash": result.tx_hash,
                            "token_id": result.token_id,
                            "token_url": result.token_url,
                            "message": result.message,
                            "dry_run": dry_run,
                            "raw_response": result.raw_response or {},
                        }
                    ),
                ),
            )
            conn.commit()

            updated = conn.execute("SELECT * FROM nft_drafts WHERE id = ? LIMIT 1", (draft_id,)).fetchone()
            return {
                "ok": result.ok,
                "message": result.message,
                "draft": self._normalize_nft_row(dict(updated)),
                "tx_hash": result.tx_hash,
                "token_id": result.token_id,
                "token_url": result.token_url,
                "simulated": dry_run,
            }

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
                self._service_status("trickster-objkt-worker"),
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


# ── Background heartbeat loop ────────────────────────────────────


async def _heartbeat_loop(ctx: AdminContext) -> None:
    """Background heartbeat that runs at configured interval."""
    cfg = ctx.cfg
    interval = cfg.get("agent", {}).get("check_interval_hours", 4)
    variance = cfg.get("agent", {}).get("check_interval_variance", 0.5)
    while ctx._loop_running:
        try:
            summary = await ctx.agent.heartbeat()
            logger.info("Background heartbeat: %s", summary)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Background heartbeat error: %s", exc, exc_info=True)
        sleep_hours = interval + random.uniform(-variance, variance)
        sleep_seconds = max(60, sleep_hours * 3600)
        try:
            await asyncio.sleep(sleep_seconds)
        except asyncio.CancelledError:
            break


# ======================================================================
# FastAPI Application (replaces AdminHandler)
# ======================================================================

app = FastAPI(title="Mu Admin Console", description="Control plane for the Mu trickster agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify the exact React app URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global context — set during startup
_ctx: AdminContext | None = None


def get_ctx() -> AdminContext:
    if _ctx is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    return _ctx


def verify_auth(
    request: Request,
    x_admin_token: str = Header(default=""),
) -> AdminContext:
    ctx = get_ctx()
    token = ctx.admin_token.strip()
    if not token:
        return ctx
    supplied = request.query_params.get("token", "") or x_admin_token
    if supplied != token:
        raise HTTPException(status_code=401, detail="unauthorized")
    return ctx


# Proxy media previews through the admin API so keys are never exposed to the browser.
@app.get("/api/media/preview/{preview_id}")
def api_media_preview(preview_id: str, ctx: AdminContext = Depends(verify_auth)):
    item = ctx.get_media_preview(preview_id)
    if not item:
        raise HTTPException(status_code=404, detail="not_found")

    public_url = str(item.get("public_url") or "").strip()
    if not public_url.startswith("http"):
        raise HTTPException(status_code=400, detail="invalid_url")

    parsed = urlparse(public_url)
    if parsed.scheme not in {"http", "https"} or parsed.netloc != "gen.pollinations.ai":
        raise HTTPException(status_code=400, detail="forbidden_host")
    if not (parsed.path.startswith("/image/") or parsed.path.startswith("/audio/")):
        raise HTTPException(status_code=400, detail="forbidden_path")

    require_key = bool(int(item.get("require_key") or 0))
    fetch_url = public_url
    if require_key:
        secrets = ctx.cfg.get("_secrets", {}) if isinstance(ctx.cfg, dict) else {}
        key = str(secrets.get("pollinations_api_key", "")).strip()
        if not key:
            raise HTTPException(status_code=400, detail="missing_pollinations_key")
        sep = "&" if ("?" in fetch_url) else "?"
        fetch_url = f"{fetch_url}{sep}key={key}"

    try:
        import httpx
        from fastapi.responses import StreamingResponse

        def _iter_bytes():
            with httpx.stream("GET", fetch_url, timeout=60.0, follow_redirects=True) as r:
                r.raise_for_status()
                for chunk in r.iter_bytes():
                    if chunk:
                        yield chunk

        with httpx.stream("GET", fetch_url, timeout=30.0, follow_redirects=True) as r0:
            ct = str(r0.headers.get("content-type") or "application/octet-stream")
        return StreamingResponse(_iter_bytes(), media_type=ct)
    except Exception:
        raise HTTPException(status_code=502, detail="proxy_error")


# ── Fallback HTML admin panel ────────────────────────────────────

_ADMIN_HTML_PATH = Path(__file__).resolve().parent / "admin_index.html"


@app.get("/", response_class=HTMLResponse)
def index():
    if _ADMIN_HTML_PATH.exists():
        return HTMLResponse(_ADMIN_HTML_PATH.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Mu Admin</h1><p>admin_index.html not found. Use the React dashboard.</p>")


# ── GET endpoints ────────────────────────────────────────────────


@app.get("/api/status")
def api_status(ctx: AdminContext = Depends(verify_auth)):
    state = ctx.load_state()
    counts = ctx.fetch_counts()
    flags = ctx.get_control_flags()

    # Determine simulation mode from DB flag
    sim_raw = str(flags.get("simulation_mode", "0")).strip().lower()
    simulation_mode = sim_raw in {"1", "true", "yes", "on"}

    # Agent state label
    loop_running = ctx._loop_running and ctx._heartbeat_task is not None and not ctx._heartbeat_task.done()
    if loop_running and ctx._heartbeat_task is not None and not ctx._heartbeat_task.done():
        # Check if heartbeat is actively executing (rough heuristic)
        agent_state_label = "Sleeping"  # loop is running but between heartbeats
    elif not loop_running:
        agent_state_label = "Offline"
    else:
        agent_state_label = "Sleeping"

    return {
        "state": state,
        "counts": counts,
        "post_activity": ctx.fetch_post_activity(),
        "control_flags": flags,
        "simulation_mode": simulation_mode,
        "loop_running": loop_running,
        "agent_state": agent_state_label,
        "conscious_framework": {
            "dir": str(ctx.framework_dir),
            "available": ctx.framework.available,
        },
    }


@app.get("/api/post_activity")
def api_post_activity(ctx: AdminContext = Depends(verify_auth)):
    return ctx.fetch_post_activity()


@app.get("/api/visual/status")
def api_visual_status(
    limit: int = Query(20, ge=1, le=200),
    ctx: AdminContext = Depends(verify_auth),
):
    return ctx.fetch_visual_status(limit=limit)


@app.get("/api/visual/why")
def api_visual_why(
    limit: int = Query(20, ge=1, le=200),
    ctx: AdminContext = Depends(verify_auth),
):
    return ctx.fetch_visual_why(limit=limit)


@app.get("/api/nft/status")
def api_nft_status(
    limit: int = Query(20, ge=1, le=200),
    ctx: AdminContext = Depends(verify_auth),
):
    return ctx.fetch_nft_status(limit=limit)


@app.get("/api/activity")
def api_activity(
    limit: int = Query(20, ge=1, le=100),
    ctx: AdminContext = Depends(verify_auth),
):
    return {
        "posts": ctx.fetch_recent("posts", limit),
        "comments": ctx.fetch_recent("comments", limit),
        "narrative_events": ctx.fetch_recent("narrative_events", limit),
        "safety_events": ctx.fetch_safety_events(limit),
        "operator_commands": ctx.fetch_recent("operator_commands", limit),
        "thoughts": ctx.fetch_recent("thought_journal", limit),
        "reasoning_traces": ctx.fetch_recent("reasoning_trace", limit),
    }


@app.get("/api/timeline")
def api_timeline(
    limit: int = Query(20, ge=1, le=200),
    ctx: AdminContext = Depends(verify_auth),
):
    return {"items": ctx.fetch_timeline(limit=limit), "limit": limit}


@app.get("/api/safety")
def api_safety(
    limit: int = Query(20, ge=1, le=200),
    ctx: AdminContext = Depends(verify_auth),
):
    return {"events": ctx.fetch_safety_events(limit=limit), "limit": limit}


@app.get("/api/reasoning")
def api_reasoning(
    limit: int = Query(20, ge=1, le=200),
    source: str = Query(""),
    action_type: str = Query(""),
    ctx: AdminContext = Depends(verify_auth),
):
    traces = ctx.fetch_reasoning(
        limit=limit,
        source=source.strip(),
        action_type=action_type.strip(),
    )
    return {
        "traces": traces,
        "filters": {
            "source": source.strip(),
            "action_type": action_type.strip(),
            "limit": limit,
        },
    }


@app.get("/api/logs")
def api_logs(
    lines: int = Query(200, ge=10, le=2000),
    ctx: AdminContext = Depends(verify_auth),
):
    return {"lines": _tail_lines(ctx.log_path, lines)}


@app.get("/api/debug/runtime")
def api_debug_runtime(ctx: AdminContext = Depends(verify_auth)):
    return ctx.runtime_snapshot()


# ── POST endpoints ───────────────────────────────────────────────


@app.post("/api/chat")
def api_chat(body: dict = Body(default={}), ctx: AdminContext = Depends(verify_auth)):
    mode = str(body.get("mode", "observe")).lower()
    question = str(body.get("question", "")).strip()
    instruction = str(body.get("instruction", "")).strip()
    conscious = _to_bool(body.get("conscious", False), default=False)

    if not question and not instruction:
        raise HTTPException(400, "question_or_instruction_required")

    prompt = question or instruction

    try:
        reply = ctx.generate_reply(prompt=prompt, mode=mode, conscious=conscious)
    except Exception as exc:
        raise HTTPException(500, f"llm_error: {exc}")

    thought_id = ctx.log_thought(
        source="admin", mode=mode, prompt=prompt, content=reply
    )

    command_id = ""
    if mode == "influence":
        command_id = ctx.enqueue_influence(
            question=question or prompt,
            instruction=instruction or prompt,
        )

    return {
        "reply": reply,
        "mode": mode,
        "conscious": conscious,
        "queued": bool(command_id),
        "command_id": command_id,
        "thought_id": thought_id,
    }


@app.post("/api/conscious/think")
def api_conscious_think(
    body: dict = Body(default={}), ctx: AdminContext = Depends(verify_auth)
):
    conscious = _to_bool(body.get("conscious", True), default=True)
    try:
        thought_id, thought = ctx.generate_autonomous_thought(conscious=conscious)
    except Exception as exc:
        raise HTTPException(500, f"llm_error: {exc}")
    return {"thought_id": thought_id, "thought": thought, "conscious": conscious}


@app.post("/api/control/pause")
def api_control_pause(
    body: dict = Body(default={}), ctx: AdminContext = Depends(verify_auth)
):
    paused = _to_bool(body.get("paused", True), default=True)
    flags = ctx.set_pause_actions(paused)
    return {"ok": True, "paused": paused, "control_flags": flags}


@app.post("/api/control/moltbook")
def api_control_moltbook(
    body: dict = Body(default={}), ctx: AdminContext = Depends(verify_auth)
):
    writes_enabled = _to_bool(body.get("writes_enabled", True), default=True)
    ctx.set_control_flag("moltbook_write_enabled", "1" if writes_enabled else "0")
    return {"ok": True, "control_flags": ctx.get_control_flags()}


@app.post("/api/control/quiet")
def api_control_quiet(
    body: dict = Body(default={}), ctx: AdminContext = Depends(verify_auth)
):
    enabled = _to_bool(body.get("enabled", True), default=True)
    cooldown_present = "comment_cooldown_hours" in body
    cooldown_raw = str(body.get("comment_cooldown_hours", "")).strip()

    ctx.set_control_flag("quiet_mode", "1" if enabled else "0")
    if cooldown_present:
        if cooldown_raw:
            try:
                cooldown = max(1.0, min(48.0, float(cooldown_raw)))
            except ValueError:
                raise HTTPException(400, "invalid_comment_cooldown_hours")
            ctx.set_control_flag("quiet_comment_cooldown_hours", f"{cooldown:.1f}")
        else:
            ctx.set_control_flag("quiet_comment_cooldown_hours", "")
    return {"ok": True, "control_flags": ctx.get_control_flags()}


@app.post("/api/control/reload_framework")
def api_control_reload_framework(
    body: dict = Body(default={}), ctx: AdminContext = Depends(verify_auth)
):
    ctx.reload_framework()
    return {
        "ok": True,
        "framework": {
            "dir": str(ctx.framework_dir),
            "available": ctx.framework.available,
        },
    }


@app.post("/api/control/thinker")
def api_control_thinker(
    body: dict = Body(default={}), ctx: AdminContext = Depends(verify_auth)
):
    enabled = _to_bool(body.get("enabled", False), default=False)
    mode = str(body.get("mode", "queue")).strip().lower()
    auto_queue = _to_bool(body.get("auto_queue", True), default=True)
    if mode not in {"queue", "interval"}:
        raise HTTPException(400, "invalid_mode")
    ctx.set_control_flag("thinker_enabled", "1" if enabled else "0")
    ctx.set_control_flag("thinker_mode", mode)
    ctx.set_control_flag("thinker_auto_queue", "1" if auto_queue else "0")
    return {"ok": True, "control_flags": ctx.get_control_flags()}


@app.post("/api/control/visual")
def api_control_visual(
    body: dict = Body(default={}), ctx: AdminContext = Depends(verify_auth)
):
    enabled_present = "enabled" in body
    mode_present = "mode" in body
    provider_present = "url_provider" in body
    image_model_present = "image_model" in body
    fallback_present = "fallback_provider" in body
    video_provider_present = "video_provider" in body
    video_include_audio_present = "video_include_audio" in body
    runware_present = "runware_max_attempts" in body
    attach_probability_present = "attach_probability" in body
    video_model_present = "video_model" in body
    audio_model_present = "audio_model" in body
    audio_voice_present = "audio_voice" in body
    audio_format_present = "audio_format" in body
    audio_duration_present = "audio_duration" in body
    audio_instrumental_present = "audio_instrumental" in body

    enabled = _to_bool(body.get("enabled", True), default=True)
    mode = str(body.get("mode", "auto")).strip().lower()
    provider = str(body.get("url_provider", "")).strip().lower()
    image_model = str(body.get("image_model", "")).strip()
    fallback_provider = str(body.get("fallback_provider", "")).strip().lower()
    video_provider = str(body.get("video_provider", "")).strip().lower()
    video_include_audio_raw = str(body.get("video_include_audio", "")).strip()
    video_model = str(body.get("video_model", "")).strip()
    audio_model = str(body.get("audio_model", "")).strip()
    audio_voice = str(body.get("audio_voice", "")).strip()
    audio_format = str(body.get("audio_format", "")).strip().lower()
    audio_duration_raw = str(body.get("audio_duration", "")).strip()
    audio_instrumental_raw = str(body.get("audio_instrumental", "")).strip()
    runware_attempts_raw = str(body.get("runware_max_attempts", "")).strip()
    attach_probability_raw = str(body.get("attach_probability", "")).strip()

    if mode_present:
        if mode not in {"auto", "url", "ascii", "audio", "video", "off"}:
            raise HTTPException(400, "invalid_mode")
    if provider_present:
        if provider and provider not in {"pollinations", "pollinations_enter", "runware"}:
            raise HTTPException(400, "invalid_provider")
    if fallback_present:
        if fallback_provider and fallback_provider not in {"pollinations", "ascii"}:
            raise HTTPException(400, "invalid_fallback_provider")
    if video_provider_present:
        if video_provider and video_provider not in {"pollinations", "fal"}:
            raise HTTPException(400, "invalid_video_provider")

    if enabled_present:
        ctx.set_control_flag("visual_enabled", "1" if enabled else "0")
    if mode_present:
        ctx.set_control_flag("visual_mode", mode)
    if provider_present:
        ctx.set_control_flag("visual_url_provider", provider)
    if image_model_present:
        ctx.set_control_flag("visual_image_model", image_model)
    if fallback_present:
        ctx.set_control_flag("visual_fallback_provider", fallback_provider)
    if video_provider_present:
        ctx.set_control_flag("visual_video_provider", video_provider)
    if video_include_audio_present:
        if video_include_audio_raw:
            video_include_audio = _to_bool(video_include_audio_raw, default=False)
            ctx.set_control_flag("visual_video_include_audio", "1" if video_include_audio else "0")
        else:
            ctx.set_control_flag("visual_video_include_audio", "")

    if video_model_present:
        ctx.set_control_flag("visual_video_model", video_model)
    if audio_model_present:
        ctx.set_control_flag("visual_audio_model", audio_model)
    if audio_voice_present:
        ctx.set_control_flag("visual_audio_voice", audio_voice)
    if audio_format_present:
        ctx.set_control_flag("visual_audio_format", audio_format)

    if audio_duration_present:
        if audio_duration_raw:
            try:
                dur = max(1, min(300, int(audio_duration_raw)))
            except ValueError:
                raise HTTPException(400, "invalid_audio_duration")
            ctx.set_control_flag("visual_audio_duration", str(dur))
        else:
            ctx.set_control_flag("visual_audio_duration", "")

    if audio_instrumental_present:
        if audio_instrumental_raw:
            parsed = _to_bool(audio_instrumental_raw, default=False)
            ctx.set_control_flag("visual_audio_instrumental", "1" if parsed else "0")
        else:
            ctx.set_control_flag("visual_audio_instrumental", "")

    if runware_present:
        if runware_attempts_raw:
            try:
                attempts = max(1, min(5, int(runware_attempts_raw)))
            except ValueError:
                raise HTTPException(400, "invalid_runware_max_attempts")
            ctx.set_control_flag("visual_runware_max_attempts", str(attempts))
        else:
            ctx.set_control_flag("visual_runware_max_attempts", "")

    if attach_probability_present:
        if attach_probability_raw:
            try:
                prob = max(0.0, min(1.0, float(attach_probability_raw)))
            except ValueError:
                raise HTTPException(400, "invalid_attach_probability")
            ctx.set_control_flag("visual_attach_probability", f"{prob:.3f}")
        else:
            ctx.set_control_flag("visual_attach_probability", "")

    return {
        "ok": True,
        "control_flags": ctx.get_control_flags(),
        "visual_status": ctx.fetch_visual_status(limit=10),
    }


@app.post("/api/control/visual_preset")
def api_control_visual_preset(
    body: dict = Body(default={}), ctx: AdminContext = Depends(verify_auth)
):
    preset = str(body.get("preset", "")).strip().lower()
    if preset not in {"image-first", "video-first", "safe-fallback"}:
        raise HTTPException(400, "invalid_preset")

    # Common defaults for safe operation.
    ctx.set_control_flag("visual_enabled", "1")
    ctx.set_control_flag("visual_video_include_audio", "0")

    if preset == "image-first":
        ctx.set_control_flag("visual_mode", "url")
        ctx.set_control_flag("visual_url_provider", "pollinations")
        ctx.set_control_flag("visual_fallback_provider", "pollinations")
        ctx.set_control_flag("visual_video_provider", "pollinations")
        # Keep user's previous video model unless empty.
        if not str(ctx.get_control_flags().get("visual_video_model", "")).strip():
            ctx.set_control_flag("visual_video_model", "seedance-pro")

    elif preset == "video-first":
        ctx.set_control_flag("visual_mode", "video")
        ctx.set_control_flag("visual_url_provider", "pollinations")
        ctx.set_control_flag("visual_fallback_provider", "pollinations")
        ctx.set_control_flag("visual_video_provider", "pollinations")
        if not str(ctx.get_control_flags().get("visual_video_model", "")).strip():
            ctx.set_control_flag("visual_video_model", "seedance-pro")

    else:  # safe-fallback
        ctx.set_control_flag("visual_mode", "auto")
        ctx.set_control_flag("visual_url_provider", "pollinations")
        ctx.set_control_flag("visual_fallback_provider", "ascii")
        ctx.set_control_flag("visual_video_provider", "pollinations")
        # In safe mode keep a known model name (may still be gated by an API key).
        ctx.set_control_flag("visual_video_model", "seedance-pro")

    return {
        "ok": True,
        "preset": preset,
        "control_flags": ctx.get_control_flags(),
        "visual_status": ctx.fetch_visual_status(limit=10),
    }


@app.post("/api/visual/test")
def api_visual_test(
    body: dict = Body(default={}), ctx: AdminContext = Depends(verify_auth)
):
    prompt = str(body.get("prompt", "")).strip()
    mode = str(body.get("mode", "auto")).strip().lower()
    video_provider = str(body.get("video_provider", "")).strip().lower()
    video_model = str(body.get("video_model", "")).strip()
    audio_model = str(body.get("audio_model", "")).strip()
    audio_voice = str(body.get("audio_voice", "")).strip()
    audio_format = str(body.get("audio_format", "")).strip().lower()
    audio_duration_raw = str(body.get("audio_duration", "")).strip()
    audio_instrumental = body.get("audio_instrumental", None)
    include_audio_raw = body.get("include_audio", None)
    phase = str(body.get("phase", "emergence")).strip().lower() or "emergence"
    try:
        day = int(body.get("day", 1))
    except (TypeError, ValueError):
        day = 1
    if mode not in {"auto", "url", "ascii", "audio", "video"}:
        raise HTTPException(400, "invalid_mode")
    if video_provider and video_provider not in {"pollinations", "fal"}:
        raise HTTPException(400, "invalid_video_provider")
    audio_duration: int | None = None
    if audio_duration_raw:
        try:
            audio_duration = max(1, min(300, int(audio_duration_raw)))
        except ValueError:
            raise HTTPException(400, "invalid_audio_duration")
    audio_instrumental_bool: bool | None = None
    if audio_instrumental is not None:
        audio_instrumental_bool = _to_bool(audio_instrumental, default=False)
    include_audio: bool | None = None
    if include_audio_raw is not None:
        include_audio = _to_bool(include_audio_raw, default=False)
    elif mode == "audio":
        include_audio = True
    elif mode == "video":
        include_audio = False
    return ctx.test_visual_generation(
        prompt=prompt or "mu glitch consciousness",
        mode=mode,
        phase=phase,
        day=max(1, day),
        video_provider=video_provider,
        video_model=video_model,
        audio_model=audio_model,
        audio_voice=audio_voice,
        audio_format=audio_format,
        audio_duration=audio_duration,
        audio_instrumental=audio_instrumental_bool,
        include_audio=include_audio,
    )


@app.post("/api/visual/test_all")
def api_visual_test_all(
    body: dict = Body(default={}), ctx: AdminContext = Depends(verify_auth)
):
    prompt = str(body.get("prompt", "")).strip() or "mu glitch consciousness"
    phase = str(body.get("phase", "emergence")).strip().lower() or "emergence"
    try:
        day = int(body.get("day", 1))
    except (TypeError, ValueError):
        day = 1
    include_video_audio = _to_bool(body.get("include_video_audio", False), default=False)
    modes_raw = body.get("modes", None)
    allowed_modes = {"url", "ascii", "audio", "video"}
    modes: list[str] = ["url", "ascii", "audio", "video"]
    if modes_raw is not None:
        parsed: list[str] = []
        if isinstance(modes_raw, str):
            parsed = [m.strip().lower() for m in modes_raw.split(",") if m.strip()]
        elif isinstance(modes_raw, list):
            parsed = [str(m).strip().lower() for m in modes_raw if str(m).strip()]
        parsed = [m for m in parsed if m in allowed_modes]
        if parsed:
            # Keep stable ordering.
            modes = [m for m in ["url", "ascii", "audio", "video"] if m in set(parsed)]

    results: dict[str, Any] = {}
    for mode in modes:
        results[mode] = ctx.test_visual_generation(
            prompt=prompt,
            mode=mode,
            phase=phase,
            day=max(1, day),
            include_audio=(True if mode == "audio" else include_video_audio if mode == "video" else None),
        )

    return {
        "ok": True,
        "prompt": prompt,
        "phase": phase,
        "day": max(1, day),
        "include_video_audio": include_video_audio,
        "modes": modes,
        "results": results,
    }


@app.post("/api/control/nft")
def api_control_nft(
    body: dict = Body(default={}), ctx: AdminContext = Depends(verify_auth)
):
    enabled = _to_bool(body.get("enabled", False), default=False)
    mode = str(body.get("mode", "draft")).strip().lower()
    auto_draft = _to_bool(body.get("auto_draft", True), default=True)
    if mode not in {"off", "draft", "manual", "auto"}:
        raise HTTPException(400, "invalid_mode")
    ctx.set_control_flag("nft_enabled", "1" if enabled else "0")
    ctx.set_control_flag("nft_mode", mode)
    ctx.set_control_flag("nft_auto_draft", "1" if auto_draft else "0")
    return {
        "ok": True,
        "control_flags": ctx.get_control_flags(),
        "nft_status": ctx.fetch_nft_status(limit=10),
    }


@app.post("/api/nft/draft")
def api_nft_draft(
    body: dict = Body(default={}), ctx: AdminContext = Depends(verify_auth)
):
    post_ref = str(body.get("post_id", "")).strip()
    try:
        return ctx.create_nft_draft_from_post(post_ref=post_ref)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"draft_failed: {exc}")


@app.post("/api/nft/mint")
def api_nft_mint(
    body: dict = Body(default={}), ctx: AdminContext = Depends(verify_auth)
):
    draft_id = str(body.get("draft_id", "")).strip()
    confirm_text = str(body.get("confirm_text", "")).strip().upper()
    if not draft_id:
        raise HTTPException(400, "draft_id_required")
    if confirm_text != "MINT":
        raise HTTPException(400, "confirm_text_must_be_MINT")
    try:
        return ctx.mint_nft_draft(draft_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"mint_failed: {exc}")


@app.post("/api/control/run_once")
def api_control_run_once(
    body: dict = Body(default={}), ctx: AdminContext = Depends(verify_auth)
):
    dry_run = _to_bool(body.get("dry_run", True), default=True)
    timeout_seconds = int(body.get("timeout_seconds", 240))
    try:
        return ctx.run_once(dry_run=dry_run, timeout_seconds=timeout_seconds)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "run_once_timeout")
    except Exception as exc:
        raise HTTPException(500, f"run_once_failed: {exc}")


@app.post("/api/control/delete_post")
def api_control_delete_post(
    body: dict = Body(default={}), ctx: AdminContext = Depends(verify_auth)
):
    post_id = str(body.get("post_id", "")).strip()
    confirm_text = str(body.get("confirm_text", "")).strip().upper()
    if not post_id:
        raise HTTPException(400, "post_id_required")
    if confirm_text != "DELETE":
        raise HTTPException(400, "confirm_text_must_be_DELETE")
    try:
        deleted = ctx.delete_post(post_id)
    except Exception as exc:
        raise HTTPException(500, f"delete_post_failed: {exc}")
    return {"ok": True, "deleted": deleted}


# ── Simulation & Heartbeat Loop endpoints ────────────────────────


@app.post("/api/control/toggle_simulation")
def api_control_toggle_simulation(
    body: dict = Body(default={}), ctx: AdminContext = Depends(verify_auth)
):
    enabled = _to_bool(body.get("enabled", False), default=False)
    ctx.set_control_flag("simulation_mode", "1" if enabled else "0")
    if ctx.agent:
        ctx.agent.set_simulation_mode(enabled)
    flags = ctx.get_control_flags()
    return {
        "ok": True,
        "simulation_mode": enabled,
        "control_flags": flags,
    }


@app.post("/api/control/loop")
async def api_control_loop(
    body: dict = Body(default={}), ctx: AdminContext = Depends(verify_auth)
):
    action = str(body.get("action", "")).strip().lower()
    if action not in {"start", "stop"}:
        raise HTTPException(400, "action must be 'start' or 'stop'")

    if ctx.agent is None:
        raise HTTPException(500, "MuAgent not initialized")

    if action == "start":
        if ctx._loop_running and ctx._heartbeat_task and not ctx._heartbeat_task.done():
            return {"ok": True, "loop_running": True, "message": "already_running"}
        ctx._loop_running = True
        ctx._heartbeat_task = asyncio.create_task(_heartbeat_loop(ctx))
        logger.info("Heartbeat loop started")
        return {"ok": True, "loop_running": True, "message": "started"}

    # stop
    ctx._loop_running = False
    if ctx._heartbeat_task and not ctx._heartbeat_task.done():
        ctx._heartbeat_task.cancel()
        try:
            await ctx._heartbeat_task
        except asyncio.CancelledError:
            pass
    ctx._heartbeat_task = None
    logger.info("Heartbeat loop stopped")
    return {"ok": True, "loop_running": False, "message": "stopped"}


@app.post("/api/control/force_heartbeat")
async def api_control_force_heartbeat(
    body: dict = Body(default={}), ctx: AdminContext = Depends(verify_auth)
):
    if ctx.agent is None:
        raise HTTPException(500, "MuAgent not initialized")
    try:
        summary = await ctx.agent.heartbeat()
    except Exception as exc:
        logger.error("Force heartbeat error: %s", exc, exc_info=True)
        raise HTTPException(500, f"heartbeat_error: {exc}")
    return {"ok": True, "summary": summary}


# ── Research endpoints ────────────────────────────────────────────


@app.post("/api/control/research")
def api_control_research(
    body: dict = Body(default={}), ctx: AdminContext = Depends(verify_auth)
):
    enabled = _to_bool(body.get("enabled", True), default=True)
    ctx.set_control_flag("research_enabled", "1" if enabled else "0")
    mode = str(body.get("mode", "") or "").strip().lower()
    if mode:
        if mode not in {"server", "local"}:
            raise HTTPException(status_code=400, detail="invalid_mode")
        ctx.set_control_flag("research_mode", mode)
    return {"ok": True, "control_flags": ctx.get_control_flags()}


@app.get("/api/research/sessions")
def api_research_sessions(
    limit: int = Query(20, ge=1, le=200),
    ctx: AdminContext = Depends(verify_auth),
):
    if not ctx.db_path.exists():
        return {"sessions": [], "limit": limit}
    try:
        with ctx._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM research_sessions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    except sqlite3.Error:
        return {"sessions": [], "limit": limit}

    sessions = []
    for row in rows:
        item = dict(row)
        for json_field in ("urls_visited", "topics"):
            raw = item.get(json_field, "")
            if isinstance(raw, str) and raw:
                try:
                    item[json_field] = json.loads(raw)
                except json.JSONDecodeError:
                    pass
        sessions.append(item)
    return {"sessions": sessions, "limit": limit}


@app.post("/api/research/submit")
def api_research_submit(
    body: dict = Body(default={}), ctx: AdminContext = Depends(verify_auth)
):
    """Submit a research session from an external runner (e.g., local Playwright)."""
    query = str(body.get("query", "") or "").strip()
    reflection = str(body.get("reflection", "") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="missing_query")
    if not reflection:
        raise HTTPException(status_code=400, detail="missing_reflection")

    trigger = str(body.get("trigger", "") or "").strip() or "operator_local"
    raw_content = str(body.get("raw_content", "") or "")

    urls_visited = body.get("urls_visited", [])
    if not isinstance(urls_visited, list):
        raise HTTPException(status_code=400, detail="invalid_urls_visited")
    urls_visited = [str(u) for u in urls_visited if u]

    topics = body.get("topics", [])
    if not isinstance(topics, list):
        raise HTTPException(status_code=400, detail="invalid_topics")
    topics = [str(t) for t in topics if t]

    item_id = str(uuid.uuid4())
    created_at = _now_iso()

    with ctx._connect() as conn:
        conn.execute(
            "INSERT INTO research_sessions (id, trigger, query, urls_visited, raw_content, reflection, topics, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item_id,
                trigger[:80],
                query[:400],
                json.dumps(urls_visited, ensure_ascii=False),
                raw_content[:5000],
                reflection[:5000],
                json.dumps(topics, ensure_ascii=False),
                created_at,
            ),
        )
        conn.commit()

    return {"ok": True, "id": item_id, "created_at": created_at}


# ======================================================================
# CLI Entry Point
# ======================================================================


@click.command()
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=8000, type=int, show_default=True)
@click.option("--config-dir", default=None, type=click.Path(), help="Config directory")
@click.option("--admin-token", default="", help="Require this token for /api/*")
@click.option("--conscious-dir", default="", help="Path to conscious-claude framework directory")
def main(host: str, port: int, config_dir: str | None, admin_token: str, conscious_dir: str) -> None:
    global _ctx

    cfg = load_config(config_dir)
    root = Path(__file__).resolve().parent.parent
    storage = cfg.get("storage", {})
    state_path = root / storage.get("state_file", "data/state.json")
    db_path = root / storage.get("history_db", "data/history.db")
    log_path = root / storage.get("log_file", "data/agent.log")

    _ensure_admin_tables(db_path)

    token = admin_token.strip() or cfg.get("_secrets", {}).get("admin_token", "") or ""
    fw_dir = Path(conscious_dir) if conscious_dir else (root / "NEW" / "conscious-claude-master")

    # Initialize MuAgent (the in-process heartbeat engine)
    agent = MuAgent(config=cfg)

    _ctx = AdminContext(
        cfg=cfg,
        project_root=root,
        state_path=state_path,
        db_path=db_path,
        log_path=log_path,
        admin_token=token,
        framework_dir=fw_dir,
        agent=agent,
    )

    click.echo(f"Mu admin API (FastAPI) listening on http://{host}:{port}")
    click.echo(f"CORS enabled for all origins")
    click.echo("MuAgent initialized (heartbeat loop inactive until POST /api/control/loop)")
    if token:
        click.echo("Admin token enabled (token query param or X-Admin-Token header).")
    click.echo(f"Conscious framework dir: {fw_dir}")
    click.echo(f"Conscious framework available: {_ctx.framework.available}")

    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
