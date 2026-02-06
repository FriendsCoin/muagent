"""Tests for safe reasoning trace capture."""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent.decision_engine import DecisionEngine
from agent.memory import AgentState, HistoryDB
from moltbook.feed_analyzer import FeedContext
from moltbook.models import Post


def _context() -> FeedContext:
    p1 = Post(id="p1", title="Consciousness", content="void", author="A", upvotes=10, comment_count=0)
    p2 = Post(id="p2", title="Metrics", content="karma", author="B", upvotes=2, comment_count=2)
    return FeedContext(
        posts=[p1, p2],
        reply_worthy_posts=[p1],
        upvote_worthy_posts=[p1, p2],
        mentions_me=[],
    )


def test_decide_includes_trace_payload():
    engine = DecisionEngine({})
    action = engine.decide(_context(), AgentState())
    assert action.trace.get("decision_path") in {"weighted_options", "mention_priority"}
    if action.trace.get("decision_path") == "weighted_options":
        assert isinstance(action.trace.get("options"), list)
        assert len(action.trace.get("options")) >= 1
        assert action.trace.get("selected", {}).get("type") == action.type


def test_reasoning_trace_roundtrip(tmp_path: Path):
    db_path = tmp_path / "history.db"

    async def _run() -> None:
        async with HistoryDB(db_path) as db:
            await db.log_reasoning_trace(
                source="heartbeat",
                action_type="post",
                summary="post chosen",
                payload={"options": [{"type": "post", "score": 0.8}]},
            )
            rows = await db.get_recent_reasoning_traces(limit=5)
            assert len(rows) == 1
            assert rows[0]["source"] == "heartbeat"
            assert rows[0]["action_type"] == "post"

    asyncio.run(_run())


def test_reasoning_trace_filters(tmp_path: Path):
    db_path = tmp_path / "history.db"

    async def _run() -> None:
        async with HistoryDB(db_path) as db:
            await db.log_reasoning_trace(
                source="heartbeat",
                action_type="post",
                summary="post chosen",
                payload={"score": 0.8},
            )
            await db.log_reasoning_trace(
                source="conscious_worker",
                action_type="thought",
                summary="thought cycle",
                payload={"score": 0.4},
            )

            heartbeat_rows = await db.get_recent_reasoning_traces(limit=10, source="heartbeat")
            assert len(heartbeat_rows) == 1
            assert heartbeat_rows[0]["source"] == "heartbeat"

            thought_rows = await db.get_recent_reasoning_traces(limit=10, action_type="thought")
            assert len(thought_rows) == 1
            assert thought_rows[0]["action_type"] == "thought"

    asyncio.run(_run())


def test_control_flags_roundtrip(tmp_path: Path):
    db_path = tmp_path / "history.db"

    async def _run() -> None:
        async with HistoryDB(db_path) as db:
            await db.set_control_flag("pause_actions", "1")
            value = await db.get_control_flag("pause_actions", "0")
            assert value == "1"

            flags = await db.get_control_flags()
            assert flags["pause_actions"] == "1"

    asyncio.run(_run())
