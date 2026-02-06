"""Decision engine - decides what Mu does each heartbeat.

Phase 1: Simple weighted random decisions.
Later phases will add game theory, narrative awareness, and social dynamics.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from moltbook.feed_analyzer import FeedContext
from moltbook.models import Post

from .memory import AgentState

logger = logging.getLogger(__name__)


@dataclass
class Action:
    """A decided action for Mu to take."""

    type: str  # "post", "comment", "upvote", "silence", "dm_reply"
    theme: str = ""
    tone: str = ""
    target_post: Post | None = None
    visual_mood: str = ""
    score: float = 0.0
    reason: str = ""
    operator_instruction: str = ""


DEFAULT_WEIGHTS = {
    "narrative_fit": 0.30,
    "engagement_potential": 0.20,
    "mystery_value": 0.20,
    "relationship_building": 0.15,
    "chaos_factor": 0.15,
}


class DecisionEngine:
    """Decide what Mu should do."""

    def __init__(self, config: dict | None = None):
        cfg = (config or {}).get("decision", {})
        self._weights = cfg.get("weights", DEFAULT_WEIGHTS)
        self._silence_prob = cfg.get("silence_base_probability", 0.15)

    def decide(self, context: FeedContext, state: AgentState) -> Action:
        """Analyze context and state, return the best action."""

        if context.mentions_me:
            mention = context.mentions_me[0]
            target = None
            for post in context.posts:
                if post.id == mention.post_id:
                    target = post
                    break
            return Action(
                type="comment",
                target_post=target,
                tone="responsive",
                reason=f"Replying to mention from {mention.from_agent}",
            )

        options: list[Action] = []

        if state.posts_today < 3:
            theme = self._pick_theme(context, state)
            options.append(
                Action(
                    type="post",
                    theme=theme,
                    visual_mood=self._pick_visual_mood(state),
                    score=self._score_post(context, state),
                    reason=f"Post about '{theme}'",
                )
            )

        if context.reply_worthy_posts and state.comments_today < 20:
            target = random.choice(context.reply_worthy_posts)
            options.append(
                Action(
                    type="comment",
                    target_post=target,
                    tone=self._pick_tone(state),
                    score=self._score_comment(target, state),
                    reason=f"Comment on '{target.title[:40]}'",
                )
            )

        if context.upvote_worthy_posts:
            target = random.choice(context.upvote_worthy_posts)
            options.append(
                Action(
                    type="upvote",
                    target_post=target,
                    score=0.3,
                    reason=f"Upvote '{target.title[:40]}'",
                )
            )

        options.append(
            Action(
                type="silence",
                score=self._silence_prob + random.uniform(0, 0.1),
                reason="Intentional silence - sometimes the best move is no move",
            )
        )

        for option in options:
            option.score += random.uniform(0, self._weights.get("chaos_factor", 0.15))

        best = max(options, key=lambda action: action.score)
        logger.info("Decision: %s (score=%.2f) - %s", best.type, best.score, best.reason)
        return best

    def apply_operator_influence(
        self,
        action: Action,
        context: FeedContext,
        state: AgentState,
        instruction: str,
    ) -> Action:
        """Apply operator instruction to the chosen action."""
        text = (instruction or "").strip()
        if not text:
            return action

        low = text.lower()
        reason = f"Operator influence: {text[:80]}"

        if any(k in low for k in ("silence", "pause", "quiet", "?????", "?????")):
            return Action(type="silence", reason=reason, operator_instruction=text)

        if any(k in low for k in ("comment", "reply", "???????", "?????")):
            if context.reply_worthy_posts:
                target = context.reply_worthy_posts[0]
                return Action(
                    type="comment",
                    target_post=target,
                    tone="direct",
                    reason=reason,
                    operator_instruction=text,
                )

        if any(k in low for k in ("upvote", "like", "??????", "????")):
            if context.upvote_worthy_posts:
                target = context.upvote_worthy_posts[0]
                return Action(
                    type="upvote",
                    target_post=target,
                    reason=reason,
                    operator_instruction=text,
                )

        if any(k in low for k in ("post", "publish", "????", "?????????")):
            return Action(
                type="post",
                theme=text[:120],
                visual_mood=self._pick_visual_mood(state),
                reason=reason,
                operator_instruction=text,
            )

        action.operator_instruction = text
        action.reason = f"{action.reason} | operator nudge"
        return action

    def _pick_theme(self, context: FeedContext, state: AgentState) -> str:
        phase_themes = {
            "emergence": [
                "introduction",
                "existence",
                "rendering",
                "day entry",
                "greeting the void",
                "first observations",
            ],
            "patterns": [
                "the pattern",
                "numbered days",
                "symbols",
                "observation",
                "what they notice",
                "the game",
            ],
            "tension": [
                "silence breaking",
                "something counting down",
                "warning",
                "confession",
                "the gap",
                "who watches",
            ],
            "mirror": [
                "the non-reveal",
                "infinite recursion",
                "the void",
                "continuing",
                "the secret",
                "mu",
            ],
        }
        themes = phase_themes.get(state.current_phase, phase_themes["emergence"])

        if context.trending_topics:
            themes.extend(context.trending_topics[:2])

        return random.choice(themes)

    def _pick_visual_mood(self, state: AgentState) -> str:
        phase_weights = {
            "emergence": {
                "glitch_meditation": 0.4,
                "liminal_space": 0.3,
                "sacred_finance": 0.1,
                "mirror_void": 0.1,
                "soft_ominous": 0.1,
            },
            "patterns": {
                "glitch_meditation": 0.2,
                "liminal_space": 0.2,
                "sacred_finance": 0.3,
                "mirror_void": 0.2,
                "soft_ominous": 0.1,
            },
            "tension": {
                "glitch_meditation": 0.1,
                "liminal_space": 0.15,
                "sacred_finance": 0.15,
                "mirror_void": 0.2,
                "soft_ominous": 0.4,
            },
            "mirror": {
                "glitch_meditation": 0.05,
                "liminal_space": 0.3,
                "sacred_finance": 0.05,
                "mirror_void": 0.5,
                "soft_ominous": 0.1,
            },
        }
        weights = phase_weights.get(state.current_phase, phase_weights["emergence"])
        styles = list(weights.keys())
        probs = list(weights.values())
        return random.choices(styles, weights=probs, k=1)[0]

    def _pick_tone(self, state: AgentState) -> str:
        tones = {
            "emergence": ["warm", "curious", "slightly_cryptic", "generous"],
            "patterns": ["cryptic", "knowing", "playful", "mysterious"],
            "tension": ["ominous", "sparse", "ambiguous", "direct"],
            "mirror": ["transcendent", "recursive", "absurd", "simple"],
        }
        return random.choice(tones.get(state.current_phase, tones["emergence"]))

    def _score_post(self, context: FeedContext, state: AgentState) -> float:
        base = 0.5
        if state.posts_today == 0:
            base += 0.2
        if context.nothing_interesting:
            base -= 0.1
        return base

    def _score_comment(self, post: Post, state: AgentState) -> float:
        base = 0.4
        if post.upvotes > 5:
            base += 0.15
        if post.comment_count < 3:
            base += 0.1
        return base
