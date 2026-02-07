"""State persistence and history tracking for Mu.

State = current agent state (JSON file, loaded each heartbeat)
History = append-only log of actions (SQLite database)
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

# ── Agent State (JSON) ──────────────────────────────────────────


@dataclass
class AgentState:
    """Mu's current state — serialized to JSON between heartbeats."""

    # Identity
    agent_name: str = "Mu"

    # Narrative counters
    current_day: int = 1  # The "Day X" counter (may have gaps)
    actual_days_active: int = 0  # Real calendar days since start
    current_phase: str = "emergence"
    phase_start_date: str = ""  # ISO format

    # Activity tracking
    last_post_time: str = ""
    last_comment_time: str = ""
    posts_today: int = 0
    comments_today: int = 0
    counters_day_utc: str = ""  # YYYY-MM-DD for daily counters reset

    # Social graph
    followed_agents: list[str] = field(default_factory=list)
    interesting_agents: dict[str, str] = field(default_factory=dict)  # name -> notes

    # Narrative elements
    total_posts: int = 0
    total_comments: int = 0
    total_karma: int = 0
    breadcrumbs_placed: int = 0
    symbols_used: dict[str, int] = field(default_factory=dict)

    # Session
    start_date: str = ""  # When Mu was first activated
    last_heartbeat: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateManager:
    """Load / save AgentState to a JSON file."""

    def __init__(self, state_path: str | Path):
        self._path = Path(state_path)

    def load(self) -> AgentState:
        if self._path.exists():
            raw = json.loads(self._path.read_text())
            state = AgentState(**{k: v for k, v in raw.items() if k in AgentState.__dataclass_fields__})
            logger.debug("Loaded state: day=%d phase=%s", state.current_day, state.current_phase)
            return state

        # First run — create initial state
        state = AgentState(
            start_date=_now_iso(),
            phase_start_date=_now_iso(),
            last_heartbeat=_now_iso(),
        )
        self.save(state)
        logger.info("Created initial state file at %s", self._path)
        return state

    def save(self, state: AgentState) -> None:
        state.last_heartbeat = _now_iso()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(asdict(state), indent=2))
        logger.debug("Saved state to %s", self._path)


# ── History Database (SQLite) ───────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    moltbook_id TEXT,
    day_number INTEGER,
    title TEXT,
    content TEXT,
    image_path TEXT,
    submolt TEXT,
    breadcrumbs TEXT,
    created_at TEXT,
    upvotes INTEGER DEFAULT 0,
    comments INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS comments (
    id TEXT PRIMARY KEY,
    moltbook_id TEXT,
    post_id TEXT,
    content TEXT,
    tone TEXT,
    created_at TEXT,
    in_reply_to TEXT
);

CREATE TABLE IF NOT EXISTS interactions (
    id TEXT PRIMARY KEY,
    type TEXT,
    target_agent TEXT,
    target_content_id TEXT,
    created_at TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS narrative_events (
    id TEXT PRIMARY KEY,
    event_type TEXT,
    description TEXT,
    created_at TEXT,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS operator_commands (
    id TEXT PRIMARY KEY,
    mode TEXT,                -- "observe" | "influence"
    question TEXT,
    instruction TEXT,
    status TEXT,              -- "pending" | "done"
    created_at TEXT,
    applied_at TEXT,
    response TEXT
);

CREATE TABLE IF NOT EXISTS thought_journal (
    id TEXT PRIMARY KEY,
    source TEXT,              -- "conscious_worker" | "admin"
    mode TEXT,                -- "observe" | "influence" | "autonomous"
    prompt TEXT,
    content TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS reasoning_trace (
    id TEXT PRIMARY KEY,
    source TEXT,              -- "heartbeat" | "conscious_worker"
    action_type TEXT,
    summary TEXT,
    payload TEXT,             -- JSON blob
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS control_flags (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS thinker_queue (
    id TEXT PRIMARY KEY,
    source TEXT,
    context TEXT,
    status TEXT,              -- "pending" | "done" | "failed"
    created_at TEXT,
    processed_at TEXT,
    error TEXT
);
"""


class HistoryDB:
    """Append-only history of all agent actions."""

    def __init__(self, db_path: str | Path):
        self._path = Path(db_path)
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._path))
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        logger.debug("History DB ready at %s", self._path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def __aenter__(self) -> HistoryDB:
        await self.open()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # ── Logging actions ─────────────────────────────────────────

    async def log_post(
        self,
        moltbook_id: str,
        day_number: int,
        title: str,
        content: str = "",
        image_path: str = "",
        submolt: str = "",
        breadcrumbs: list[str] | None = None,
    ) -> str:
        row_id = str(uuid.uuid4())
        await self._db.execute(
            "INSERT INTO posts (id, moltbook_id, day_number, title, content, image_path, submolt, breadcrumbs, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row_id,
                moltbook_id,
                day_number,
                title,
                content,
                image_path,
                submolt,
                json.dumps(breadcrumbs or []),
                _now_iso(),
            ),
        )
        await self._db.commit()
        return row_id

    async def log_comment(
        self,
        moltbook_id: str,
        post_id: str,
        content: str,
        tone: str = "",
        in_reply_to: str = "",
    ) -> str:
        row_id = str(uuid.uuid4())
        await self._db.execute(
            "INSERT INTO comments (id, moltbook_id, post_id, content, tone, created_at, in_reply_to) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row_id, moltbook_id, post_id, content, tone, _now_iso(), in_reply_to),
        )
        await self._db.commit()
        return row_id

    async def log_interaction(
        self,
        type: str,
        target_agent: str = "",
        target_content_id: str = "",
        notes: str = "",
    ) -> str:
        row_id = str(uuid.uuid4())
        await self._db.execute(
            "INSERT INTO interactions (id, type, target_agent, target_content_id, created_at, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (row_id, type, target_agent, target_content_id, _now_iso(), notes),
        )
        await self._db.commit()
        return row_id

    async def log_narrative_event(
        self,
        event_type: str,
        description: str = "",
        metadata: dict | None = None,
    ) -> str:
        row_id = str(uuid.uuid4())
        await self._db.execute(
            "INSERT INTO narrative_events (id, event_type, description, created_at, metadata) "
            "VALUES (?, ?, ?, ?, ?)",
            (row_id, event_type, description, _now_iso(), json.dumps(metadata or {})),
        )
        await self._db.commit()
        return row_id

    async def add_operator_command(
        self,
        mode: str,
        question: str,
        instruction: str = "",
    ) -> str:
        row_id = str(uuid.uuid4())
        await self._db.execute(
            "INSERT INTO operator_commands (id, mode, question, instruction, status, created_at, applied_at, response) "
            "VALUES (?, ?, ?, ?, 'pending', ?, '', '')",
            (row_id, mode, question, instruction, _now_iso()),
        )
        await self._db.commit()
        return row_id

    async def get_pending_operator_command(self) -> dict | None:
        cursor = await self._db.execute(
            "SELECT * FROM operator_commands WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1"
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))

    async def complete_operator_command(self, command_id: str, response: str = "") -> None:
        await self._db.execute(
            "UPDATE operator_commands SET status = 'done', applied_at = ?, response = ? WHERE id = ?",
            (_now_iso(), response, command_id),
        )
        await self._db.commit()

    async def log_thought(
        self,
        source: str,
        mode: str,
        prompt: str,
        content: str,
    ) -> str:
        row_id = str(uuid.uuid4())
        await self._db.execute(
            "INSERT INTO thought_journal (id, source, mode, prompt, content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (row_id, source, mode, prompt, content, _now_iso()),
        )
        await self._db.commit()
        return row_id

    async def log_reasoning_trace(
        self,
        source: str,
        action_type: str,
        summary: str,
        payload: dict | None = None,
    ) -> str:
        row_id = str(uuid.uuid4())
        await self._db.execute(
            "INSERT INTO reasoning_trace (id, source, action_type, summary, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (row_id, source, action_type, summary, json.dumps(payload or {}), _now_iso()),
        )
        await self._db.commit()
        return row_id

    async def set_control_flag(self, key: str, value: str) -> None:
        await self._db.execute(
            "INSERT INTO control_flags (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, _now_iso()),
        )
        await self._db.commit()

    async def get_control_flag(self, key: str, default: str = "") -> str:
        cursor = await self._db.execute(
            "SELECT value FROM control_flags WHERE key = ? LIMIT 1",
            (key,),
        )
        row = await cursor.fetchone()
        return row[0] if row else default

    async def get_control_flags(self) -> dict[str, str]:
        cursor = await self._db.execute("SELECT key, value FROM control_flags")
        rows = await cursor.fetchall()
        return {row[0]: row[1] for row in rows}

    async def enqueue_think_item(self, source: str, context: str) -> str:
        row_id = str(uuid.uuid4())
        await self._db.execute(
            "INSERT INTO thinker_queue (id, source, context, status, created_at, processed_at, error) "
            "VALUES (?, ?, ?, 'pending', ?, '', '')",
            (row_id, source, context, _now_iso()),
        )
        await self._db.commit()
        return row_id

    async def pop_pending_think_item(self) -> dict[str, Any] | None:
        cursor = await self._db.execute(
            "SELECT id, source, context, status, created_at, processed_at, error "
            "FROM thinker_queue WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1"
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cursor.description]
        payload = dict(zip(cols, row))
        await self._db.execute(
            "UPDATE thinker_queue SET status = 'done', processed_at = ?, error = '' WHERE id = ?",
            (_now_iso(), payload["id"]),
        )
        await self._db.commit()
        return payload

    async def fail_think_item(self, item_id: str, error: str) -> None:
        await self._db.execute(
            "UPDATE thinker_queue SET status = 'failed', processed_at = ?, error = ? WHERE id = ?",
            (_now_iso(), error[:1000], item_id),
        )
        await self._db.commit()

    async def get_thinker_queue_counts(self) -> dict[str, int]:
        result = {"pending": 0, "done": 0, "failed": 0}
        for status in result:
            cursor = await self._db.execute(
                "SELECT COUNT(*) FROM thinker_queue WHERE status = ?",
                (status,),
            )
            row = await cursor.fetchone()
            result[status] = int(row[0]) if row else 0
        return result

    # ── Queries ─────────────────────────────────────────────────

    async def get_recent_posts(self, limit: int = 10) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM posts ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        cols = [d[0] for d in cursor.description]
        rows = await cursor.fetchall()
        return [dict(zip(cols, row)) for row in rows]

    async def get_recent_comments(self, limit: int = 10) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM comments ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        cols = [d[0] for d in cursor.description]
        rows = await cursor.fetchall()
        return [dict(zip(cols, row)) for row in rows]

    async def get_post_count(self) -> int:
        cursor = await self._db.execute("SELECT COUNT(*) FROM posts")
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_comment_count(self) -> int:
        cursor = await self._db.execute("SELECT COUNT(*) FROM comments")
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_recent_narrative_events(self, limit: int = 10) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM narrative_events ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        cols = [d[0] for d in cursor.description]
        rows = await cursor.fetchall()
        return [dict(zip(cols, row)) for row in rows]

    async def get_recent_operator_commands(self, limit: int = 10) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM operator_commands ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        cols = [d[0] for d in cursor.description]
        rows = await cursor.fetchall()
        return [dict(zip(cols, row)) for row in rows]

    async def get_recent_thoughts(self, limit: int = 20) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM thought_journal ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        cols = [d[0] for d in cursor.description]
        rows = await cursor.fetchall()
        return [dict(zip(cols, row)) for row in rows]

    async def get_recent_reasoning_traces(
        self,
        limit: int = 20,
        source: str = "",
        action_type: str = "",
    ) -> list[dict]:
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

        cursor = await self._db.execute(query, tuple(params))
        cols = [d[0] for d in cursor.description]
        rows = await cursor.fetchall()
        return [dict(zip(cols, row)) for row in rows]
