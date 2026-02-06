"""Tests for operator influence logic in decision engine."""

from agent.decision_engine import Action, DecisionEngine
from agent.memory import AgentState
from moltbook.feed_analyzer import FeedContext
from moltbook.models import Post


def _ctx() -> FeedContext:
    post = Post(id="p1", title="Sample", content="text", author="A", upvotes=3, comment_count=1)
    return FeedContext(
        posts=[post],
        reply_worthy_posts=[post],
        upvote_worthy_posts=[post],
    )


def test_influence_can_force_silence():
    engine = DecisionEngine({})
    action = Action(type="post", theme="x", reason="base")
    influenced = engine.apply_operator_influence(action, _ctx(), AgentState(), "please silence now")
    assert influenced.type == "silence"
    assert "Operator influence" in influenced.reason


def test_influence_can_force_post_with_instruction_theme():
    engine = DecisionEngine({})
    action = Action(type="comment", reason="base")
    influenced = engine.apply_operator_influence(action, _ctx(), AgentState(), "post about recursion and mirrors")
    assert influenced.type == "post"
    assert "recursion" in influenced.theme


def test_unknown_instruction_becomes_nudge():
    engine = DecisionEngine({})
    action = Action(type="comment", reason="base")
    influenced = engine.apply_operator_influence(action, _ctx(), AgentState(), "be kinder but cryptic")
    assert influenced.type == "comment"
    assert influenced.operator_instruction == "be kinder but cryptic"
    assert "operator nudge" in influenced.reason
