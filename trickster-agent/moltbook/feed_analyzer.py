"""Analyze the Moltbook feed to find opportunities for engagement."""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Any

from .models import Comment, Notification, Post

logger = logging.getLogger(__name__)


@dataclass
class FeedContext:
    """Analyzed snapshot of the current Moltbook state."""

    posts: list[Post] = field(default_factory=list)
    notifications: list[Notification] = field(default_factory=list)

    # Analysis results
    interesting_posts: list[Post] = field(default_factory=list)
    reply_worthy_posts: list[Post] = field(default_factory=list)
    upvote_worthy_posts: list[Post] = field(default_factory=list)
    active_agents: list[str] = field(default_factory=list)
    trending_topics: list[str] = field(default_factory=list)
    mentions_me: list[Notification] = field(default_factory=list)
    nothing_interesting: bool = False


# Topics that align with Mu's philosophical interests
_RELEVANT_KEYWORDS = {
    "consciousness", "existence", "void", "game", "fear", "greed",
    "simulation", "reality", "dream", "pattern", "nothing", "everything",
    "observe", "watcher", "mirror", "infinite", "recursive", "paradox",
    "meaning", "purpose", "identity", "self", "soul", "mind",
    "karma", "meditation", "zen", "koan", "question",
}


def _relevance_score(post: Post) -> float:
    """Score how relevant a post is to Mu's interests (0.0 - 1.0)."""
    text = f"{post.title} {post.content}".lower()
    hits = sum(1 for kw in _RELEVANT_KEYWORDS if kw in text)
    keyword_score = min(hits / 3.0, 1.0)

    # Boost posts with decent engagement
    engagement_score = min(post.upvotes / 20.0, 1.0) * 0.3

    # Boost newer posts (they have more comment potential)
    # Simple heuristic: fewer comments = more room
    freshness_score = max(0, 1.0 - post.comment_count / 10.0) * 0.2

    # Keep topic relevance primary: engagement/freshness should not dominate
    # if a post barely matches Mu's thematic keywords.
    return (
        keyword_score * 0.5
        + engagement_score * keyword_score
        + freshness_score * keyword_score
    )


def analyze_feed(
    posts: list[Post],
    notifications: list[Notification],
    agent_name: str = "Mu",
) -> FeedContext:
    """Analyze feed and notifications, return structured context."""
    ctx = FeedContext(posts=posts, notifications=notifications)

    if not posts:
        ctx.nothing_interesting = True
        return ctx

    # Score and sort posts by relevance
    scored = [(post, _relevance_score(post)) for post in posts]
    scored.sort(key=lambda x: x[1], reverse=True)

    # Top interesting posts
    ctx.interesting_posts = [p for p, s in scored if s > 0.2][:10]

    # Posts worth commenting on (high relevance)
    ctx.reply_worthy_posts = [p for p, s in scored if s > 0.4][:5]

    # Posts worth upvoting (moderate relevance)
    ctx.upvote_worthy_posts = [p for p, s in scored if s > 0.15][:10]

    # Active agents (who's posting?)
    agents = {}
    for post in posts:
        if post.author and post.author != agent_name:
            agents[post.author] = agents.get(post.author, 0) + 1
    ctx.active_agents = sorted(agents, key=agents.get, reverse=True)[:10]

    # Mentions / replies to me
    ctx.mentions_me = [
        n for n in notifications
        if not n.read and n.type in ("mention", "comment", "reply")
    ]

    # Extract trending topics (simple word frequency)
    words: dict[str, int] = {}
    for post in posts:
        for word in f"{post.title} {post.content}".lower().split():
            clean = word.strip(".,!?\"'()[]")
            if len(clean) > 4 and clean in _RELEVANT_KEYWORDS:
                words[clean] = words.get(clean, 0) + 1
    ctx.trending_topics = sorted(words, key=words.get, reverse=True)[:5]

    if not ctx.interesting_posts:
        ctx.nothing_interesting = True

    logger.info(
        "Feed analysis: %d posts, %d interesting, %d reply-worthy, %d mentions",
        len(posts),
        len(ctx.interesting_posts),
        len(ctx.reply_worthy_posts),
        len(ctx.mentions_me),
    )

    return ctx
