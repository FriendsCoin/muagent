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
import base64
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
            "nft_drafts": 0,
            "nft_minted": 0,
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
        fallback_provider = str(flags.get("visual_fallback_provider", "")).strip().lower()
        if not fallback_provider:
            fallback_provider = str(
                self.cfg.get("visual_posting", {}).get("url", {}).get("fallback_provider", "pollinations")
            )
        if fallback_provider not in {"pollinations", "ascii"}:
            fallback_provider = "pollinations"

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
            "fallback_provider": fallback_provider,
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
                "runware_key_present": runware_key_present,
                "runware_endpoint": str(visual_cfg.get("url", {}).get("endpoint", "https://api.runware.ai/v1")),
                "runware_max_attempts": int(visual_cfg.get("url", {}).get("runware_max_attempts", 1)),
                "pollinations_key_present": bool(str(secrets.get("pollinations_api_key", "")).strip()),
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
    ) -> dict[str, Any]:
        generator = VisualGenerator(self.cfg)
        if mode in {"audio", "video"}:
            return self.test_media_narration(prompt=prompt, mode=mode, phase=phase, day=day)

        force_mode = mode if mode in {"url", "ascii"} else ""
        effective = self._visual_effective_flags()
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
            fallback_provider_override=str(effective.get("fallback_provider") or ""),
            runware_max_attempts_override=effective.get("runware_max_attempts_override"),
        )
        payload = {
            "kind": visual.kind,
            "provider": visual.provider,
            "prompt": visual.prompt,
            "url": visual.url,
            "ascii_art": visual.ascii_art,
            "meta": visual.meta,
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
    ) -> dict[str, Any]:
        """Generate an image/video + narrated audio (TTS) instead of reading the prompt verbatim."""
        generator = VisualGenerator(self.cfg)
        effective = self._visual_effective_flags()

        base_force = "url" if mode == "audio" else "video"
        base_visual = generator.generate(
            theme=prompt[:80] or "mystery",
            mood="soft_ominous",
            phase=phase or "emergence",
            day=max(1, int(day)),
            context=prompt,
            force_mode=base_force,
            enabled_override=effective["enabled"],
            attach_probability_override=1.0,
            url_provider_override=str(effective["url_provider"] or ""),
            fallback_provider_override=str(effective.get("fallback_provider") or ""),
            runware_max_attempts_override=effective.get("runware_max_attempts_override"),
        )

        # Pollinations video is not always available. If the generated URL 404s,
        # fall back to a standard image URL so the pipeline still works.
        if base_force == "video" and base_visual.url.startswith("http"):
            try:
                import httpx

                resp = httpx.head(base_visual.url, timeout=8, follow_redirects=True)
                if resp.status_code >= 400:
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
                        fallback_provider_override=str(effective.get("fallback_provider") or ""),
                        runware_max_attempts_override=effective.get("runware_max_attempts_override"),
                    )
                    base_visual.meta = dict(base_visual.meta or {})
                    base_visual.meta["video_fallback_used"] = True
                    base_visual.meta["video_error_reason"] = f"http_{resp.status_code}"
            except Exception:
                # Best-effort only; keep original base_visual if probing fails.
                pass

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

        narration = self.personality().generate_media_narration(
            visual_prompt=base_visual.prompt or prompt,
            phase=phase or "emergence",
            day=max(1, int(day)),
            image_bytes=image_bytes,
            image_media_type=image_media_type,
        )
        audio = generator.tts_from_text(narration)

        return {
            "mode": mode,
            "phase": phase,
            "day": day,
            "base_visual": {
                "kind": base_visual.kind,
                "provider": base_visual.provider,
                "prompt": base_visual.prompt,
                "url": base_visual.url,
                "meta": base_visual.meta,
            },
            "narration": narration,
            "audio": {
                "kind": audio.kind,
                "provider": audio.provider,
                "prompt": audio.prompt,
                "url": audio.url,
                "meta": audio.meta,
            },
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

        if path == "/api/visual/status":
            params = parse_qs(parsed.query)
            try:
                limit = int(params.get("limit", ["20"])[0])
            except ValueError:
                limit = 20
            limit = max(1, min(200, limit))
            self._send_json(200, self.ctx.fetch_visual_status(limit=limit))
            return

        if path == "/api/visual/why":
            params = parse_qs(parsed.query)
            try:
                limit = int(params.get("limit", ["20"])[0])
            except ValueError:
                limit = 20
            limit = max(1, min(200, limit))
            self._send_json(200, self.ctx.fetch_visual_why(limit=limit))
            return

        if path == "/api/nft/status":
            params = parse_qs(parsed.query)
            try:
                limit = int(params.get("limit", ["20"])[0])
            except ValueError:
                limit = 20
            limit = max(1, min(200, limit))
            self._send_json(200, self.ctx.fetch_nft_status(limit=limit))
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

        if path == "/api/control/moltbook":
            writes_enabled = _to_bool(body.get("writes_enabled", True), default=True)
            self.ctx.set_control_flag("moltbook_write_enabled", "1" if writes_enabled else "0")
            self._send_json(200, {"ok": True, "control_flags": self.ctx.get_control_flags()})
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

        if path == "/api/control/visual":
            enabled = _to_bool(body.get("enabled", True), default=True)
            mode = str(body.get("mode", "auto")).strip().lower()
            provider = str(body.get("url_provider", "")).strip().lower()
            fallback_provider = str(body.get("fallback_provider", "")).strip().lower()
            runware_attempts_raw = str(body.get("runware_max_attempts", "")).strip()
            attach_probability_raw = str(body.get("attach_probability", "")).strip()
            if mode not in {"auto", "url", "ascii", "audio", "video", "off"}:
                self._send_json(400, {"error": "invalid_mode"})
                return
            if provider and provider not in {"pollinations", "pollinations_enter", "runware"}:
                self._send_json(400, {"error": "invalid_provider"})
                return
            if fallback_provider and fallback_provider not in {"pollinations", "ascii"}:
                self._send_json(400, {"error": "invalid_fallback_provider"})
                return

            self.ctx.set_control_flag("visual_enabled", "1" if enabled else "0")
            self.ctx.set_control_flag("visual_mode", mode)
            if provider:
                self.ctx.set_control_flag("visual_url_provider", provider)
            if fallback_provider:
                self.ctx.set_control_flag("visual_fallback_provider", fallback_provider)

            if runware_attempts_raw:
                try:
                    attempts = max(1, min(5, int(runware_attempts_raw)))
                except ValueError:
                    self._send_json(400, {"error": "invalid_runware_max_attempts"})
                    return
                self.ctx.set_control_flag("visual_runware_max_attempts", str(attempts))
            else:
                self.ctx.set_control_flag("visual_runware_max_attempts", "")

            if attach_probability_raw:
                try:
                    prob = max(0.0, min(1.0, float(attach_probability_raw)))
                except ValueError:
                    self._send_json(400, {"error": "invalid_attach_probability"})
                    return
                self.ctx.set_control_flag("visual_attach_probability", f"{prob:.3f}")
            else:
                self.ctx.set_control_flag("visual_attach_probability", "")

            self._send_json(
                200,
                {
                    "ok": True,
                    "control_flags": self.ctx.get_control_flags(),
                    "visual_status": self.ctx.fetch_visual_status(limit=10),
                },
            )
            return

        if path == "/api/visual/test":
            prompt = str(body.get("prompt", "")).strip()
            mode = str(body.get("mode", "auto")).strip().lower()
            phase = str(body.get("phase", "emergence")).strip().lower() or "emergence"
            try:
                day = int(body.get("day", 1))
            except (TypeError, ValueError):
                day = 1
            if mode not in {"auto", "url", "ascii", "audio", "video"}:
                self._send_json(400, {"error": "invalid_mode"})
                return
            payload = self.ctx.test_visual_generation(
                prompt=prompt or "mu glitch consciousness",
                mode=mode,
                phase=phase,
                day=max(1, day),
            )
            self._send_json(200, payload)
            return

        if path == "/api/control/nft":
            enabled = _to_bool(body.get("enabled", False), default=False)
            mode = str(body.get("mode", "draft")).strip().lower()
            auto_draft = _to_bool(body.get("auto_draft", True), default=True)
            if mode not in {"off", "draft", "manual", "auto"}:
                self._send_json(400, {"error": "invalid_mode"})
                return
            self.ctx.set_control_flag("nft_enabled", "1" if enabled else "0")
            self.ctx.set_control_flag("nft_mode", mode)
            self.ctx.set_control_flag("nft_auto_draft", "1" if auto_draft else "0")
            self._send_json(
                200,
                {
                    "ok": True,
                    "control_flags": self.ctx.get_control_flags(),
                    "nft_status": self.ctx.fetch_nft_status(limit=10),
                },
            )
            return

        if path == "/api/nft/draft":
            post_ref = str(body.get("post_id", "")).strip()
            try:
                result = self.ctx.create_nft_draft_from_post(post_ref=post_ref)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except Exception as exc:
                self._send_json(500, {"error": f"draft_failed: {exc}"})
                return
            self._send_json(200, result)
            return

        if path == "/api/nft/mint":
            draft_id = str(body.get("draft_id", "")).strip()
            confirm_text = str(body.get("confirm_text", "")).strip().upper()
            if not draft_id:
                self._send_json(400, {"error": "draft_id_required"})
                return
            if confirm_text != "MINT":
                self._send_json(400, {"error": "confirm_text_must_be_MINT"})
                return
            try:
                result = self.ctx.mint_nft_draft(draft_id)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except Exception as exc:
                self._send_json(500, {"error": f"mint_failed: {exc}"})
                return
            self._send_json(200, result)
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
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; align-items: start; }
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
    .tabs { display: flex; gap: 8px; margin: 10px 0 14px 0; flex-wrap: wrap; }
    .tab-btn { border-radius: 999px; padding: 6px 12px; }
    .tab-btn.active { border-color: var(--accent); color: var(--accent); }
    .tab-card { display: block; }
    .kv { display: grid; grid-template-columns: 180px 1fr; gap: 6px 10px; font-size: 12px; }
    .kv b { color: var(--muted); font-weight: 600; }
    details { border-top: 1px dashed var(--border); padding-top: 8px; margin-top: 8px; }
    summary { cursor: pointer; color: var(--muted); font-size: 12px; }
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
      <span id="runwareState" class="pill muted">runware: unknown</span>
      <span id="nftState" class="pill muted">nft: unknown</span>
    </div>
    <div class="tabs">
      <button id="tab-dashboard" class="tab-btn active" onclick="showTab('dashboard')">Dashboard</button>
      <button id="tab-operations" class="tab-btn" onclick="showTab('operations')">Operations</button>
      <button id="tab-content" class="tab-btn" onclick="showTab('content')">Content</button>
      <button id="tab-nft" class="tab-btn" onclick="showTab('nft')">NFT</button>
      <button id="tab-system" class="tab-btn" onclick="showTab('system')">System</button>
    </div>
    <div class="grid">
      <div class="card tab-card" data-tab="dashboard">
        <h2>Status</h2>
        <div id="statusSummary" class="kv"></div>
        <details>
          <summary>Raw JSON</summary>
          <div id="status" class="mono">loading...</div>
        </details>
      </div>

      <div class="card tab-card" data-tab="dashboard">
        <h2>Post Activity</h2>
        <div id="postActivitySummary" class="kv"></div>
        <details>
          <summary>Raw JSON</summary>
          <div id="postActivity" class="mono">loading...</div>
        </details>
      </div>

      <div class="card tab-card" data-tab="content">
        <h2>Visual Generation</h2>
        <div class="row">
          <label class="muted"><input id="visualEnabled" type="checkbox" checked /> enabled</label>
          <label class="muted">mode</label>
          <select id="visualMode">
            <option value="auto" selected>auto</option>
            <option value="url">url</option>
            <option value="ascii">ascii</option>
            <option value="audio">audio</option>
            <option value="video">video</option>
            <option value="off">off</option>
          </select>
          <label class="muted">provider</label>
          <select id="visualProvider">
            <option value="pollinations" selected>pollinations</option>
            <option value="pollinations_enter">pollinations_enter</option>
            <option value="runware">runware</option>
          </select>
          <label class="muted">fallback</label>
          <select id="visualFallbackProvider">
            <option value="pollinations" selected>pollinations</option>
            <option value="ascii">ascii</option>
          </select>
          <label class="muted">runware tries</label>
          <input id="visualRunwareAttempts" type="number" min="1" max="5" step="1" placeholder="cfg" style="width:72px;" />
          <label class="muted">attach p</label>
          <input id="visualAttachProbability" type="number" min="0" max="1" step="0.05" placeholder="auto" style="width:90px;" />
          <button onclick="saveVisualConfig()">Apply Visual</button>
          <label class="muted">history</label>
          <select id="visualLimit">
            <option value="5">5</option>
            <option value="10">10</option>
            <option value="20" selected>20</option>
            <option value="50">50</option>
            <option value="100">100</option>
          </select>
          <button onclick="refreshVisualStatus(); refreshVisualWhy();">Refresh Visual</button>
        </div>
        <div class="row">
          <input id="visualTestPrompt" type="text" placeholder="test prompt (optional)" style="min-width:280px;" />
          <select id="visualTestMode">
            <option value="auto" selected>auto test</option>
            <option value="url">force url</option>
            <option value="ascii">force ascii</option>
            <option value="audio">force audio</option>
            <option value="video">force video</option>
          </select>
          <button onclick="testVisual()">Test Visual</button>
          <button onclick="testAudio()">Test Audio</button>
          <button onclick="testVideo()">Test Video</button>
        </div>
        <div id="visualSummary" class="kv"></div>
        <div id="visualResult" class="mono muted">No visual test yet.</div>
        <details>
          <summary>Raw JSON</summary>
          <div id="visualStatus" class="mono">loading...</div>
        </details>
      </div>

      <div class="card tab-card" data-tab="content">
        <h2>Why This Visual</h2>
        <div class="row">
          <label class="muted">limit</label>
          <select id="visualWhyLimit">
            <option value="5">5</option>
            <option value="10">10</option>
            <option value="20" selected>20</option>
            <option value="50">50</option>
            <option value="100">100</option>
          </select>
          <button onclick="refreshVisualWhy()">Refresh Why</button>
        </div>
        <div id="visualWhy" class="mono">loading...</div>
      </div>

      <div class="card tab-card" data-tab="nft">
        <h2>NFT / objkt</h2>
        <div class="row">
          <label class="muted"><input id="nftEnabled" type="checkbox" /> enabled</label>
          <label class="muted">mode</label>
          <select id="nftMode">
            <option value="off">off</option>
            <option value="draft" selected>draft</option>
            <option value="manual">manual</option>
            <option value="auto">auto</option>
          </select>
          <label class="muted"><input id="nftAutoDraft" type="checkbox" checked /> auto draft</label>
          <button onclick="saveNftConfig()">Apply NFT</button>
          <label class="muted">limit</label>
          <select id="nftLimit">
            <option value="5">5</option>
            <option value="10">10</option>
            <option value="20" selected>20</option>
            <option value="50">50</option>
            <option value="100">100</option>
          </select>
          <button onclick="refreshNftStatus()">Refresh NFT</button>
        </div>
        <div class="row">
          <input id="nftPostRef" type="text" placeholder="post_id or moltbook_id (empty = latest visual post)" style="min-width:360px;" />
          <button onclick="createNftDraft()">Create Draft</button>
        </div>
        <div class="row">
          <input id="nftDraftId" type="text" placeholder="draft_id to mint" style="min-width:320px;" />
          <input id="nftMintConfirm" type="text" placeholder="type MINT" style="max-width:140px;" />
          <button onclick="mintNftDraft()">Mint Draft</button>
        </div>
        <div id="nftSummary" class="kv"></div>
        <div id="nftResult" class="mono muted">No NFT action yet.</div>
        <details>
          <summary>Raw JSON</summary>
          <div id="nftStatus" class="mono">loading...</div>
        </details>
      </div>

      <div class="card tab-card" data-tab="operations">
        <h2>Control</h2>
        <div class="row">
          <button onclick="setPause(true)">Pause Actions</button>
          <button onclick="setPause(false)">Resume Actions</button>
          <button onclick="thinkNow()">Think Now</button>
        </div>
        <div class="row">
          <label class="muted"><input id="moltbookWritesEnabled" type="checkbox" checked /> Moltbook writes</label>
          <button onclick="saveMoltbookConfig()">Apply Moltbook</button>
          <span class="muted">When off: simulate actions locally (no Moltbook API writes).</span>
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

      <div class="card tab-card" data-tab="operations">
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
      <div class="card tab-card" data-tab="dashboard">
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

      <div class="card tab-card" data-tab="dashboard">
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

      <div class="card tab-card" data-tab="operations">
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

      <div class="card tab-card" data-tab="dashboard">
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

      <div class="card tab-card" data-tab="content">
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

      <div class="card tab-card" data-tab="system">
        <h2>Debug</h2>
        <div id="debugSummary" class="kv"></div>
        <details>
          <summary>Raw JSON</summary>
          <div id="debug" class="mono">loading...</div>
        </details>
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
    const currentTabKey = 'mu_admin_tab_v2';

    function showTab(tab) {
      const allowed = new Set(['dashboard', 'operations', 'content', 'nft', 'system']);
      if (!allowed.has(tab)) tab = 'dashboard';
      const cards = document.querySelectorAll('.tab-card');
      cards.forEach((card) => {
        const cardTab = card.getAttribute('data-tab') || 'dashboard';
        card.style.display = (cardTab === tab) ? 'block' : 'none';
      });
      document.querySelectorAll('.tab-btn').forEach((btn) => btn.classList.remove('active'));
      const active = document.getElementById('tab-' + tab);
      if (active) active.classList.add('active');
      try { localStorage.setItem(currentTabKey, tab); } catch (_) {}
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }

    function setKv(targetId, items) {
      const el = document.getElementById(targetId);
      if (!el) return;
      const rows = [];
      for (const [k, v] of items) {
        rows.push('<b>' + escapeHtml(k) + '</b><span>' + escapeHtml(v) + '</span>');
      }
      el.innerHTML = rows.join('');
    }

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
    function updateRunwareBadge(runtime) {
      const el = document.getElementById('runwareState');
      const state = String((runtime || {}).state || 'unknown');
      const message = String((runtime || {}).message || '');
      if (state === 'ok') {
        el.textContent = 'runware: OK';
        el.className = 'pill ok';
      } else if (state === 'fallback' || state === 'mixed') {
        el.textContent = 'runware: fallback';
        el.className = 'pill warn';
      } else if (state === 'no_key') {
        el.textContent = 'runware: no key';
        el.className = 'pill err';
      } else {
        el.textContent = 'runware: unknown';
        el.className = 'pill muted';
      }
      if (message) {
        el.title = message;
      }
    }
    function updateNftBadge(statusPayload) {
      const el = document.getElementById('nftState');
      const effective = (statusPayload || {}).effective || {};
      const objkt = (statusPayload || {}).objkt || {};
      const enabled = !!effective.enabled;
      const mode = String(effective.mode || 'off');
      const webhookConfigured = !!objkt.webhook_configured;
      if (!enabled || mode === 'off') {
        el.textContent = 'nft: OFF';
        el.className = 'pill muted';
        return;
      }
      if (!webhookConfigured && (mode === 'manual' || mode === 'auto')) {
        el.textContent = 'nft: no webhook';
        el.className = 'pill err';
        return;
      }
      if (mode === 'auto') {
        el.textContent = 'nft: AUTO';
        el.className = 'pill warn';
        return;
      }
      if (mode === 'manual') {
        el.textContent = 'nft: MANUAL';
        el.className = 'pill ok';
        return;
      }
      el.textContent = 'nft: DRAFT';
      el.className = 'pill ok';
    }
    async function refreshStatus() {
      const d = await apiGet('/api/status');
      document.getElementById('status').textContent = JSON.stringify(d, null, 2);
      document.getElementById('postActivity').textContent = JSON.stringify(d.post_activity || {}, null, 2);
      const s = d.state || {};
      const c = d.counts || {};
      setKv('statusSummary', [
        ['agent', String(s.agent_name || 'Mu')],
        ['day / phase', String((s.current_day || '?') + ' / ' + (s.current_phase || '?'))],
        ['last heartbeat', String(s.last_heartbeat || '-')],
        ['posts today', String(s.posts_today || 0)],
        ['comments today', String(s.comments_today || 0)],
        ['pending operator', String(c.pending_operator || 0)],
        ['reasoning traces', String(c.reasoning_traces || 0)],
        ['pause actions', String(!!c.pause_actions)],
      ]);
      const pa = d.post_activity || {};
      setKv('postActivitySummary', [
        ['posts (10h)', String(pa.posts_last_10h || 0)],
        ['posts (24h)', String(pa.posts_last_24h || 0)],
        ['last post at', String(pa.last_post_at || '-')],
        ['last title', String(pa.last_post_title || '-')],
        ['last submolt', String(pa.last_post_submolt || '-')],
      ]);
      const flags = d.control_flags || {};
      const paused = ['1', 'true', 'yes', 'on'].includes(String(flags.pause_actions || '').toLowerCase()) || !!(d.counts && d.counts.pause_actions);
      updatePauseBadge(paused);
      const writesEnabled = !['0', 'false', 'no', 'off'].includes(String(flags.moltbook_write_enabled || '1').toLowerCase());
      const mw = document.getElementById('moltbookWritesEnabled');
      if (mw) mw.checked = writesEnabled;
      const thinkerEnabled = ['1', 'true', 'yes', 'on'].includes(String(flags.thinker_enabled || '').toLowerCase());
      const thinkerAutoQueue = !['0', 'false', 'no', 'off'].includes(String(flags.thinker_auto_queue || '1').toLowerCase());
      document.getElementById('thinkerEnabled').checked = thinkerEnabled;
      document.getElementById('thinkerAutoQueue').checked = thinkerAutoQueue;
      document.getElementById('thinkerMode').value = String(flags.thinker_mode || 'queue');
      const nftEnabled = ['1', 'true', 'yes', 'on'].includes(String(flags.nft_enabled || '').toLowerCase());
      const nftAutoDraft = !['0', 'false', 'no', 'off'].includes(String(flags.nft_auto_draft || '1').toLowerCase());
      document.getElementById('nftEnabled').checked = nftEnabled;
      document.getElementById('nftAutoDraft').checked = nftAutoDraft;
      document.getElementById('nftMode').value = String(flags.nft_mode || 'draft');
    }
    async function refreshActivity() {
      const limit = Number(document.getElementById('activityLimit').value || 10);
      const d = await apiGet('/api/activity?limit=' + encodeURIComponent(String(limit)));
      document.getElementById('activity').textContent = JSON.stringify(d, null, 2);
    }
    async function refreshVisualStatus() {
      const limit = Number(document.getElementById('visualLimit').value || 20);
      const d = await apiGet('/api/visual/status?limit=' + encodeURIComponent(String(limit)));
      document.getElementById('visualStatus').textContent = JSON.stringify(d, null, 2);
      const effective = d.effective || {};
      const runtime = d.runware_runtime || {};
      setKv('visualSummary', [
        ['enabled', String(!!effective.enabled)],
        ['mode', String(effective.mode || 'auto')],
        ['provider', String(effective.url_provider || 'pollinations')],
        ['fallback', String(effective.fallback_provider || ((d.providers || {}).fallback_provider || 'pollinations'))],
        ['pollinations key', String(!!((d.providers || {}).pollinations_key_present))],
        ['runware tries', String(
          (effective.runware_max_attempts_override !== null && effective.runware_max_attempts_override !== undefined)
            ? effective.runware_max_attempts_override
            : ((d.providers || {}).runware_max_attempts || 1)
        )],
        ['runware state', String(runtime.state || 'unknown')],
        ['runware msg', String(runtime.message || '-')],
        ['recent visual posts', String((d.recent_visual_posts || []).length)],
      ]);
      document.getElementById('visualEnabled').checked = !!effective.enabled;
      document.getElementById('visualMode').value = String(effective.mode || 'auto');
      document.getElementById('visualProvider').value = String(effective.url_provider || 'pollinations');
      document.getElementById('visualFallbackProvider').value = String(
        effective.fallback_provider || ((d.providers || {}).fallback_provider || 'pollinations')
      );
      const runwareAttempts = effective.runware_max_attempts_override;
      document.getElementById('visualRunwareAttempts').value =
        (runwareAttempts === null || runwareAttempts === undefined) ? '' : String(runwareAttempts);
      const p = effective.attach_probability_override;
      document.getElementById('visualAttachProbability').value = (p === null || p === undefined) ? '' : String(p);
      updateRunwareBadge(runtime);
    }
    async function refreshVisualWhy() {
      const limit = Number(document.getElementById('visualWhyLimit').value || 20);
      const d = await apiGet('/api/visual/why?limit=' + encodeURIComponent(String(limit)));
      document.getElementById('visualWhy').textContent = JSON.stringify(d, null, 2);
    }
    async function refreshNftStatus() {
      const limit = Number(document.getElementById('nftLimit').value || 20);
      const d = await apiGet('/api/nft/status?limit=' + encodeURIComponent(String(limit)));
      document.getElementById('nftStatus').textContent = JSON.stringify(d, null, 2);
      const effective = d.effective || {};
      const objkt = d.objkt || {};
      const counts = d.counts || {};
      setKv('nftSummary', [
        ['enabled', String(!!effective.enabled)],
        ['mode', String(effective.mode || 'draft')],
        ['auto draft', String(!!effective.auto_draft)],
        ['webhook configured', String(!!objkt.webhook_configured)],
        ['token present', String(!!objkt.token_present)],
        ['draft/minting/minted/sim/failed', String((counts.draft || 0) + '/' + (counts.minting || 0) + '/' + (counts.minted || 0) + '/' + (counts.simulated || 0) + '/' + (counts.failed || 0))],
      ]);
      document.getElementById('nftEnabled').checked = !!effective.enabled;
      document.getElementById('nftMode').value = String(effective.mode || 'draft');
      document.getElementById('nftAutoDraft').checked = !!effective.auto_draft;
      updateNftBadge(d);
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
      const services = Array.isArray(d.services) ? d.services : [];
      const active = services.filter((s) => String(s.active || '') === 'active').length;
      setKv('debugSummary', [
        ['server time', String(d.server_time || '-')],
        ['uptime (sec)', String(d.uptime_seconds || 0)],
        ['project root', String(d.project_root || '-')],
        ['services active', String(active + '/' + services.length)],
        ['framework available', String(!!((d.framework || {}).available))],
      ]);
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
    async function saveMoltbookConfig() {
      const writesEnabled = document.getElementById('moltbookWritesEnabled').checked;
      const d = await apiPost('/api/control/moltbook', {writes_enabled: writesEnabled});
      document.getElementById('controlResult').textContent = JSON.stringify(d, null, 2);
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
    async function saveVisualConfig() {
      const enabled = document.getElementById('visualEnabled').checked;
      const mode = document.getElementById('visualMode').value;
      const urlProvider = document.getElementById('visualProvider').value;
      const fallbackProvider = document.getElementById('visualFallbackProvider').value;
      const runwareAttempts = document.getElementById('visualRunwareAttempts').value.trim();
      const attachProbability = document.getElementById('visualAttachProbability').value.trim();
      const payload = {
        enabled,
        mode,
        url_provider: urlProvider,
        fallback_provider: fallbackProvider,
        runware_max_attempts: runwareAttempts,
        attach_probability: attachProbability,
      };
      const d = await apiPost('/api/control/visual', payload);
      document.getElementById('visualResult').textContent = JSON.stringify(d, null, 2);
      await refreshAll();
    }
    async function runVisualTest(modeOverride='') {
      const prompt = document.getElementById('visualTestPrompt').value.trim();
      const mode = modeOverride || document.getElementById('visualTestMode').value;
      const status = await apiGet('/api/status');
      const state = status.state || {};
      const payload = {
        prompt: prompt || 'mu glitch void mirror',
        mode,
        phase: String(state.current_phase || 'emergence'),
        day: Number(state.current_day || 1),
      };
      const d = await apiPost('/api/visual/test', payload);
      document.getElementById('visualResult').textContent = JSON.stringify(d, null, 2);
      await refreshVisualStatus();
      await refreshVisualWhy();
    }
    async function testVisual() { await runVisualTest(''); }
    async function testAudio() { await runVisualTest('audio'); }
    async function testVideo() { await runVisualTest('video'); }
    async function saveNftConfig() {
      const enabled = document.getElementById('nftEnabled').checked;
      const mode = document.getElementById('nftMode').value;
      const autoDraft = document.getElementById('nftAutoDraft').checked;
      const d = await apiPost('/api/control/nft', {enabled, mode, auto_draft: autoDraft});
      document.getElementById('nftResult').textContent = JSON.stringify(d, null, 2);
      await refreshNftStatus();
      await refreshStatus();
    }
    async function createNftDraft() {
      const postRef = document.getElementById('nftPostRef').value.trim();
      const d = await apiPost('/api/nft/draft', {post_id: postRef});
      document.getElementById('nftResult').textContent = JSON.stringify(d, null, 2);
      const draftId = (((d || {}).draft || {}).id || '');
      if (draftId) document.getElementById('nftDraftId').value = draftId;
      await refreshNftStatus();
    }
    async function mintNftDraft() {
      const draftId = document.getElementById('nftDraftId').value.trim();
      const confirmText = document.getElementById('nftMintConfirm').value.trim().toUpperCase();
      if (!draftId) {
        alert('draft_id required');
        return;
      }
      if (confirmText !== 'MINT') {
        alert('Type MINT in confirmation field');
        return;
      }
      const ok = confirm('Mint draft ' + draftId + '? This will trigger external mint webhook.');
      if (!ok) return;
      const d = await apiPost('/api/nft/mint', {draft_id: draftId, confirm_text: confirmText});
      document.getElementById('nftResult').textContent = JSON.stringify(d, null, 2);
      await refreshNftStatus();
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
        await Promise.all([refreshStatus(), refreshVisualStatus(), refreshVisualWhy(), refreshNftStatus(), refreshActivity(), refreshTimeline(), refreshLogs(), refreshReasoning(), refreshSafety(), refreshDebug()]);
        h.textContent = 'OK';
        h.className = 'pill ok';
      } catch (e) {
        h.textContent = 'ERROR: ' + e.message;
        h.className = 'pill err';
      }
    }
    const savedTab = (() => { try { return localStorage.getItem(currentTabKey) || 'dashboard'; } catch (_) { return 'dashboard'; } })();
    showTab(savedTab);
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
