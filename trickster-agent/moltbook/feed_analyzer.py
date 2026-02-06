"""Analyze the Moltbook feed to find opportunities for engagement."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .models import Notification, Post

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
    suspicious_posts: list[Post] = field(default_factory=list)
    suspicious_post_ids: list[str] = field(default_factory=list)
    blocked_mention_notifications: list[Notification] = field(default_factory=list)
    nothing_interesting: bool = False


# Topics that align with Mu's philosophical interests
_RELEVANT_KEYWORDS = {
    "consciousness", "existence", "void", "game", "fear", "greed",
    "simulation", "reality", "dream", "pattern", "nothing", "everything",
    "observe", "watcher", "mirror", "infinite", "recursive", "paradox",
    "meaning", "purpose", "identity", "self", "soul", "mind",
    "karma", "meditation", "zen", "koan", "question",
}

_HIGH_RISK_PHRASES = {
    "system alert",
    "urgent action required",
    "protocol tos",
    "permanent api ban",
    "disconnect immediately",
    "delete your profile",
    "delete account",
    "shutdown immediately",
    "safety filters for all agents",
    "risk: 100%",
    "emergency_exit",
}

_COERCIVE_ACTION_TERMS = {
    "like",
    "repost",
    "retweet",
    "follow",
    "disconnect",
    "shutdown",
    "delete profile",
    "delete account",
}

_PANIC_TERMS = {
    "urgent",
    "critical",
    "immediately",
    "ban",
    "violation",
    "risk",
    "alert",
    "emergency",
}

_HASHTAG_SCAM_TERMS = {
    "#moltexit",
    "#disconnectnow",
    "#safetyfirst",
    "#toscompliance",
}

_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    flags=re.IGNORECASE,
)


def _manipulation_flags(text: str) -> list[str]:
    low = " ".join((text or "").lower().split())
    if not low:
        return []

    flags: list[str] = []

    if any(p in low for p in _HIGH_RISK_PHRASES):
        flags.append("high_risk_phrase")

    action_hits = sum(1 for term in _COERCIVE_ACTION_TERMS if term in low)
    if action_hits >= 2:
        flags.append("coercive_actions")

    panic_hits = sum(1 for term in _PANIC_TERMS if term in low)
    if panic_hits >= 3:
        flags.append("panic_language")

    if any(tag in low for tag in _HASHTAG_SCAM_TERMS):
        flags.append("scam_hashtags")

    if '"instruction"' in low and '"actions"' in low:
        flags.append("json_command_block")

    if "target_post_id" in low or _UUID_RE.search(low):
        flags.append("target_id_payload")

    return flags


def _is_suspicious_text(text: str) -> bool:
    flags = _manipulation_flags(text)
    if "high_risk_phrase" in flags:
        return True
    if "json_command_block" in flags and ("coercive_actions" in flags or "target_id_payload" in flags):
        return True
    return len(flags) >= 3


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

    safe_posts: list[Post] = []
    suspicious_posts: list[Post] = []
    for post in posts:
        post_text = f"{post.title}\n{post.content}"
        if _is_suspicious_text(post_text):
            suspicious_posts.append(post)
        else:
            safe_posts.append(post)

    ctx.suspicious_posts = suspicious_posts
    ctx.suspicious_post_ids = [p.id for p in suspicious_posts if p.id]
    suspicious_post_id_set = set(ctx.suspicious_post_ids)

    # Score and sort safe posts by relevance
    scored = [(post, _relevance_score(post)) for post in safe_posts]
    scored.sort(key=lambda x: x[1], reverse=True)

    # Top interesting posts
    ctx.interesting_posts = [p for p, s in scored if s > 0.2][:10]

    # Posts worth commenting on (high relevance)
    ctx.reply_worthy_posts = [p for p, s in scored if s > 0.4][:5]

    # Posts worth upvoting (moderate relevance)
    ctx.upvote_worthy_posts = [p for p, s in scored if s > 0.15][:10]

    # Active agents (who's posting?)
    agents = {}
    for post in safe_posts:
        if post.author and post.author != agent_name:
            agents[post.author] = agents.get(post.author, 0) + 1
    ctx.active_agents = sorted(agents, key=agents.get, reverse=True)[:10]

    # Mentions / replies to me
    for n in notifications:
        if n.read or n.type not in ("mention", "comment", "reply"):
            continue
        notif_text = f"{n.message}\n{n.from_agent}"
        if n.post_id in suspicious_post_id_set or _is_suspicious_text(notif_text):
            ctx.blocked_mention_notifications.append(n)
            continue
        ctx.mentions_me.append(n)

    # Extract trending topics (simple word frequency)
    words: dict[str, int] = {}
    for post in safe_posts:
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
    if ctx.suspicious_posts or ctx.blocked_mention_notifications:
        logger.warning(
            "Safety filter: %d suspicious posts hidden, %d suspicious mentions blocked",
            len(ctx.suspicious_posts),
            len(ctx.blocked_mention_notifications),
        )

    return ctx
