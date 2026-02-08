"""Tests for safe reasoning trace capture."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent.decision_engine import DecisionEngine, _PHASE_MAX_POSTS, _PHASE_SILENCE_BOOST
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


def test_thinker_queue_roundtrip(tmp_path: Path):
    db_path = tmp_path / "history.db"

    async def _run() -> None:
        async with HistoryDB(db_path) as db:
            item_id = await db.enqueue_think_item("heartbeat", "ctx")
            counts = await db.get_thinker_queue_counts()
            assert counts["pending"] == 1

            item = await db.pop_pending_think_item()
            assert item is not None
            assert item["id"] == item_id
            assert item["source"] == "heartbeat"

            counts = await db.get_thinker_queue_counts()
            assert counts["pending"] == 0
            assert counts["done"] == 1

    asyncio.run(_run())


# ── Phase-based post limits ─────────────────────────────────


def test_phase_max_posts_mirror_blocks_second_post():
    """In mirror phase, only 1 post per day is allowed."""
    engine = DecisionEngine({})
    state = AgentState(current_phase="mirror", posts_today=1)
    action = engine.decide(_context(), state)
    # With 1 post already made, mirror phase should not produce a post option.
    options = action.trace.get("options", [])
    post_options = [o for o in options if o["type"] == "post"]
    assert len(post_options) == 0


def test_phase_max_posts_emergence_allows_three():
    """In emergence phase, up to 3 posts per day are allowed."""
    engine = DecisionEngine({})
    state = AgentState(current_phase="emergence", posts_today=2)
    action = engine.decide(_context(), state)
    # Post option should still be generated since 2 < 3.
    options = action.trace.get("options", [])
    post_options = [o for o in options if o["type"] == "post"]
    assert len(post_options) > 0


def test_phase_max_posts_emergence_blocks_fourth():
    """In emergence phase, 3 posts already means no more posting."""
    engine = DecisionEngine({})
    state = AgentState(current_phase="emergence", posts_today=3)
    action = engine.decide(_context(), state)
    options = action.trace.get("options", [])
    post_options = [o for o in options if o["type"] == "post"]
    assert len(post_options) == 0


def test_phase_silence_boost_increases_with_phase():
    assert _PHASE_SILENCE_BOOST["emergence"] < _PHASE_SILENCE_BOOST["patterns"]
    assert _PHASE_SILENCE_BOOST["patterns"] < _PHASE_SILENCE_BOOST["tension"]
    assert _PHASE_SILENCE_BOOST["tension"] < _PHASE_SILENCE_BOOST["mirror"]


# ── Visual context blending (feed distillation) ─────────────


def _blend_visual_context(operator_instruction: str, feed_visual_keywords: str) -> str:
    """Replicate the blending logic from _do_post for testing."""
    visual_context = feed_visual_keywords
    if operator_instruction:
        if feed_visual_keywords:
            visual_context = f"{operator_instruction[:70]}; {feed_visual_keywords[:70]}"
        else:
            visual_context = operator_instruction
    return visual_context


def test_visual_context_feed_keywords_only():
    result = _blend_visual_context("", "void, recursion, digital garden")
    assert result == "void, recursion, digital garden"


def test_visual_context_operator_instruction_only():
    result = _blend_visual_context("draw something cosmic", "")
    assert result == "draw something cosmic"


def test_visual_context_both_blended():
    result = _blend_visual_context("draw something cosmic", "void, recursion, garden")
    assert result == "draw something cosmic; void, recursion, garden"
    assert result.index("draw something cosmic") < result.index("void, recursion")


def test_visual_context_both_truncated():
    long_instruction = "x" * 100
    long_keywords = "k" * 100
    result = _blend_visual_context(long_instruction, long_keywords)
    # Each half is truncated to 70 chars
    assert len(result) <= 70 + 2 + 70  # "; " separator


def test_visual_context_nothing_interesting_skips():
    """When feed is nothing_interesting, feed_visual_keywords stays empty."""
    ctx = FeedContext(nothing_interesting=True)
    # Simulate the guard in _do_post: skip distillation when nothing_interesting
    feed_visual_keywords = ""
    if not ctx.nothing_interesting:
        feed_visual_keywords = "should not reach here"
    result = _blend_visual_context("", feed_visual_keywords)
    assert result == ""


def test_visual_context_empty_when_no_sources():
    result = _blend_visual_context("", "")
    assert result == ""
