"""Core orchestrator — the heartbeat loop that IS Mu.

Each heartbeat: wake → perceive → decide → act → sleep.
Between heartbeats, Mu does not exist.
"""

from __future__ import annotations

import asyncio
import logging
import random
from pathlib import Path

from moltbook.client import MoltbookClient, RateLimitError
from moltbook.feed_analyzer import FeedContext, analyze_feed
from narrative import advance_narrative_state

from .config import load_config
from .decision_engine import Action, DecisionEngine
from .memory import AgentState, HistoryDB, StateManager
from .personality import Personality

logger = logging.getLogger(__name__)


class MuAgent:
    """The autonomous trickster agent."""

    def __init__(self, config: dict | None = None, dry_run: bool = False):
        self._cfg = config or load_config()
        self._dry_run = dry_run

        # Resolve paths relative to project root
        root = Path(__file__).resolve().parent.parent
        storage = self._cfg.get("storage", {})

        # Components
        self._state_mgr = StateManager(root / storage.get("state_file", "data/state.json"))
        self._db_path = root / storage.get("history_db", "data/history.db")
        self._personality = Personality(
            api_key=self._cfg["_secrets"]["anthropic_api_key"],
            model=self._cfg.get("llm", {}).get("model", "claude-sonnet-4-20250514"),
            temperature=self._cfg.get("llm", {}).get("temperature", 0.9),
            voice_modes=self._cfg.get("agent", {}).get("personality", {}).get("voice_modes"),
        )
        self._decision = DecisionEngine(self._cfg)
        self._moltbook_key = self._cfg["_secrets"]["moltbook_api_key"]

    async def heartbeat(self) -> str:
        """One complete cycle: perceive → decide → act.

        Returns a short summary of what happened.
        """
        state = self._state_mgr.load()
        # Keep day/phase aligned with real elapsed time and narrative config.
        advance_narrative_state(state, self._cfg)
        logger.info("=== Heartbeat === Day %d | Phase: %s", state.current_day, state.current_phase)

        async with (
            MoltbookClient(self._moltbook_key) as mb,
            HistoryDB(self._db_path) as db,
        ):
            # 1. Perceive — check the feed and notifications
            context = await self._perceive(mb, state)

            # 2. Decide — what should Mu do?
            action = self._decision.decide(context, state)

            # 3. Act — execute the decision
            await self._act(action, state, mb, db)

            # 4. Persist state
            self._state_mgr.save(state)

            summary = f"[Day {state.current_day}] {action.type}: {action.reason}"
            logger.info("Heartbeat complete: %s", summary)
            return summary

    # ── Perceive ────────────────────────────────────────────────

    async def _perceive(self, mb: MoltbookClient, state: AgentState) -> FeedContext:
        """Gather information from Moltbook."""
        try:
            posts = await mb.get_posts(sort="hot", limit=25)
        except Exception as e:
            logger.warning("Failed to fetch posts: %s", e)
            posts = []

        try:
            notifications = await mb.get_notifications()
        except Exception as e:
            logger.warning("Failed to fetch notifications: %s", e)
            notifications = []

        return analyze_feed(posts, notifications, agent_name=state.agent_name)

    # ── Act ─────────────────────────────────────────────────────

    async def _act(
        self,
        action: Action,
        state: AgentState,
        mb: MoltbookClient,
        db: HistoryDB,
    ) -> str:
        """Execute the decided action."""

        if action.type == "silence":
            logger.info("Choosing silence. Mu does nothing.")
            await db.log_narrative_event("silence", action.reason)
            return "silence"

        if action.type == "post":
            return await self._do_post(action, state, mb, db)

        if action.type == "comment":
            return await self._do_comment(action, state, mb, db)

        if action.type == "upvote":
            return await self._do_upvote(action, mb, db)

        logger.warning("Unknown action type: %s", action.type)
        return "unknown"

    async def _do_post(
        self, action: Action, state: AgentState, mb: MoltbookClient, db: HistoryDB
    ) -> str:
        """Create a new post."""
        # Generate text
        content = self._personality.generate_post_text(
            theme=action.theme,
            phase=state.current_phase,
            day=state.current_day,
            total_posts=state.total_posts,
        )
        title = self._personality.generate_post_title(
            content=content,
            phase=state.current_phase,
            day=state.current_day,
        )

        submolt = random.choice(
            self._cfg.get("moltbook", {}).get("preferred_submolts", ["general"])
        )

        if self._dry_run:
            logger.info("[DRY RUN] Would post to s/%s: %s — %s", submolt, title, content)
            await db.log_post("dry_run", state.current_day, title, content, submolt=submolt)
            return f"dry_run_post: {title}"

        try:
            # Add random delay to feel organic
            delay = random.uniform(
                self._cfg.get("scheduler", {}).get("action_delay_min", 5),
                self._cfg.get("scheduler", {}).get("action_delay_max", 30),
            )
            await asyncio.sleep(delay)

            post = await mb.create_post(title=title, submolt=submolt, content=content)
            await db.log_post(post.id, state.current_day, title, content, submolt=submolt)
            state.posts_today += 1
            state.total_posts += 1
            return f"posted: {post.id}"

        except RateLimitError as e:
            logger.warning("Rate limited on post: %s (retry in %ds)", e, e.retry_after)
            return f"rate_limited: {e.retry_after}s"

    async def _do_comment(
        self, action: Action, state: AgentState, mb: MoltbookClient, db: HistoryDB
    ) -> str:
        """Comment on a post."""
        if not action.target_post:
            logger.warning("Comment action has no target post")
            return "no_target"

        post = action.target_post
        comment_text = self._personality.generate_comment(
            post_title=post.title,
            post_content=post.content,
            post_author=post.author,
            tone=action.tone,
            phase=state.current_phase,
            day=state.current_day,
            total_posts=state.total_posts,
        )

        if self._dry_run:
            logger.info("[DRY RUN] Would comment on %s: %s", post.id, comment_text)
            await db.log_comment("dry_run", post.id, comment_text, tone=action.tone)
            return f"dry_run_comment: {post.id}"

        try:
            delay = random.uniform(5, 20)
            await asyncio.sleep(delay)

            comment = await mb.create_comment(post.id, comment_text)
            await db.log_comment(comment.id, post.id, comment_text, tone=action.tone)
            state.comments_today += 1
            state.total_comments += 1
            return f"commented: {comment.id}"

        except RateLimitError as e:
            logger.warning("Rate limited on comment: %s", e)
            return f"rate_limited: {e.retry_after}s"

    async def _do_upvote(
        self, action: Action, mb: MoltbookClient, db: HistoryDB
    ) -> str:
        """Upvote a post."""
        if not action.target_post:
            return "no_target"

        if self._dry_run:
            logger.info("[DRY RUN] Would upvote %s", action.target_post.id)
            return "dry_run_upvote"

        try:
            await mb.upvote_post(action.target_post.id)
            await db.log_interaction(
                "upvote",
                target_agent=action.target_post.author,
                target_content_id=action.target_post.id,
            )
            return f"upvoted: {action.target_post.id}"
        except Exception as e:
            logger.warning("Failed to upvote: %s", e)
            return f"error: {e}"
