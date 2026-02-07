"""Core orchestrator - the heartbeat loop that IS Mu.

Each heartbeat: wake -> perceive -> decide -> act -> sleep.
Between heartbeats, Mu does not exist.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from pathlib import Path

from moltbook.client import MoltbookClient, RateLimitError
from moltbook.feed_analyzer import FeedContext, analyze_feed
from narrative import advance_narrative_state, post_day_label

from .config import load_config
from .decision_engine import Action, DecisionEngine
from .memory import AgentState, HistoryDB, StateManager
from .personality import Personality

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MuAgent:
    """The autonomous trickster agent."""

    def __init__(self, config: dict | None = None, dry_run: bool = False):
        self._cfg = config or load_config()
        self._dry_run = dry_run

        root = Path(__file__).resolve().parent.parent
        storage = self._cfg.get("storage", {})

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
        """One complete cycle: perceive -> decide -> act."""
        state = self._state_mgr.load()
        advance_narrative_state(state, self._cfg)
        logger.info("=== Heartbeat === Day %d | Phase: %s", state.current_day, state.current_phase)

        async with MoltbookClient(self._moltbook_key) as mb, HistoryDB(self._db_path) as db:
            context = await self._perceive(mb, state)
            await self._maybe_enqueue_think_context(db, context, state)
            if context.suspicious_posts or context.blocked_mention_notifications:
                await db.log_narrative_event(
                    "safety_filter",
                    "Filtered suspicious feed content",
                    metadata={
                        "suspicious_posts": [
                            {
                                "id": post.id,
                                "author": post.author,
                                "title": post.title[:120],
                            }
                            for post in context.suspicious_posts
                        ],
                        "blocked_mentions": [
                            {
                                "id": n.id,
                                "from_agent": n.from_agent,
                                "post_id": n.post_id,
                                "message": (n.message or "")[:200],
                            }
                            for n in context.blocked_mention_notifications
                        ],
                    },
                )

            operator_cmd = await db.get_pending_operator_command()
            action = self._decision.decide(context, state)

            if operator_cmd and operator_cmd.get("mode") == "influence":
                instruction = operator_cmd.get("instruction") or operator_cmd.get("question") or ""
                action = self._decision.apply_operator_influence(action, context, state, instruction)
                logger.info("Operator command %s applied", operator_cmd.get("id", "?"))

            pause_flag = (await db.get_control_flag("pause_actions", "0")).strip().lower()
            if pause_flag in {"1", "true", "yes", "on"}:
                previous_action = action
                action = Action(
                    type="silence",
                    score=1.0,
                    reason="Paused by operator control flag",
                )
                action.trace = {
                    "decision_path": "control_flag_pause",
                    "pause_actions": True,
                    "previous_selected": {
                        "type": previous_action.type,
                        "reason": previous_action.reason,
                        "score": previous_action.score,
                    },
                }
                logger.info("Pause flag is active: action overridden to silence")

            result = await self._act(action, state, mb, db)

            await db.log_reasoning_trace(
                source="heartbeat",
                action_type=action.type,
                summary=action.reason,
                payload={
                    "day": state.current_day,
                    "phase": state.current_phase,
                    "score": action.score,
                    "operator_command_id": operator_cmd.get("id") if operator_cmd else "",
                    "result": result,
                    "trace": getattr(action, "trace", {}),
                },
            )

            if operator_cmd and operator_cmd.get("status") == "pending":
                await db.complete_operator_command(
                    operator_cmd["id"],
                    response=f"action={action.type}; result={result}; reason={action.reason}",
                )

            self._state_mgr.save(state)

            summary = f"[Day {state.current_day}] {action.type}: {action.reason}"
            logger.info("Heartbeat complete: %s", summary)
            return summary

    async def _maybe_enqueue_think_context(
        self,
        db: HistoryDB,
        context: FeedContext,
        state: AgentState,
    ) -> None:
        enabled = (await db.get_control_flag("thinker_enabled", "0")).strip().lower()
        auto_queue = (await db.get_control_flag("thinker_auto_queue", "1")).strip().lower()
        mode = (await db.get_control_flag("thinker_mode", "queue")).strip().lower()
        if enabled not in {"1", "true", "yes", "on"}:
            return
        if auto_queue in {"0", "false", "no", "off"}:
            return
        if mode != "queue":
            return

        queue_counts = await db.get_thinker_queue_counts()
        if queue_counts.get("pending", 0) >= 5:
            return

        posts = context.reply_worthy_posts or context.upvote_worthy_posts or context.posts
        if not posts:
            return

        lines = [
            f"Day={state.current_day}; phase={state.current_phase}",
            "Top feed items:",
        ]
        for idx, post in enumerate(posts[:50], start=1):
            title = (post.title or "").replace("\n", " ").strip()
            author = (post.author or "").strip()
            lines.append(f"{idx}. [{post.id}] {title[:140]} | by {author}")
        if context.mentions_me:
            lines.append("Mentions:")
            for mention in context.mentions_me[:20]:
                msg = (mention.message or "").replace("\n", " ").strip()
                lines.append(f"- [{mention.id}] from={mention.from_agent} post={mention.post_id}: {msg[:200]}")

        queue_context = "\n".join(lines)[:12000]
        await db.enqueue_think_item("heartbeat", queue_context)

    async def _perceive(self, mb: MoltbookClient, state: AgentState) -> FeedContext:
        """Gather information from Moltbook."""
        try:
            posts = await mb.get_posts(sort="hot", limit=25)
        except Exception as exc:
            logger.warning("Failed to fetch posts: %s", exc)
            posts = []

        try:
            notifications = await mb.get_notifications()
        except Exception as exc:
            logger.warning("Failed to fetch notifications: %s", exc)
            notifications = []

        return analyze_feed(posts, notifications, agent_name=state.agent_name)

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
        self,
        action: Action,
        state: AgentState,
        mb: MoltbookClient,
        db: HistoryDB,
    ) -> str:
        """Create a new post."""
        day_label = post_day_label(state.current_day, state.posts_today)
        content = self._personality.generate_post_text(
            theme=action.theme,
            phase=state.current_phase,
            day=state.current_day,
            context=action.operator_instruction,
            total_posts=state.total_posts,
        )
        title = self._personality.generate_post_title(
            content=content,
            phase=state.current_phase,
            day=state.current_day,
        )
        # Prevent multiple same-day posts all being titled just "Day X".
        if state.posts_today > 0 and title.strip().lower().startswith(f"day {state.current_day}".lower()):
            title = day_label

        submolt = random.choice(self._cfg.get("moltbook", {}).get("preferred_submolts", ["general"]))

        if self._dry_run:
            logger.info("[DRY RUN] Would post to s/%s: %s - %s", submolt, title, content)
            await db.log_post("dry_run", state.current_day, title, content, submolt=submolt)
            return f"dry_run_post: {title}"

        try:
            delay = random.uniform(
                self._cfg.get("scheduler", {}).get("action_delay_min", 5),
                self._cfg.get("scheduler", {}).get("action_delay_max", 30),
            )
            await asyncio.sleep(delay)

            post = await mb.create_post(title=title, submolt=submolt, content=content)
            post_id = (post.id or "").strip()
            if not post_id and post.url:
                post_id = post.url.rstrip("/").rsplit("/", 1)[-1]
            if not post_id:
                post_id = "unknown"

            await db.log_post(post_id, state.current_day, title, content, submolt=submolt)
            state.posts_today += 1
            state.total_posts += 1
            state.last_post_time = _now_iso()
            return f"posted: {post_id}"
        except RateLimitError as exc:
            logger.warning("Rate limited on post: %s (retry in %ds)", exc, exc.retry_after)
            return f"rate_limited: {exc.retry_after}s"

    async def _do_comment(
        self,
        action: Action,
        state: AgentState,
        mb: MoltbookClient,
        db: HistoryDB,
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
            context=action.operator_instruction,
        )

        if self._dry_run:
            logger.info("[DRY RUN] Would comment on %s: %s", post.id, comment_text)
            await db.log_comment("dry_run", post.id, comment_text, tone=action.tone)
            return f"dry_run_comment: {post.id}"

        try:
            await asyncio.sleep(random.uniform(5, 20))
            comment = await mb.create_comment(post.id, comment_text)
            comment_id = (comment.id or "").strip() or "unknown"
            await db.log_comment(comment_id, post.id, comment_text, tone=action.tone)
            state.comments_today += 1
            state.total_comments += 1
            state.last_comment_time = _now_iso()
            return f"commented: {comment_id}"
        except RateLimitError as exc:
            logger.warning("Rate limited on comment: %s", exc)
            return f"rate_limited: {exc.retry_after}s"

    async def _do_upvote(self, action: Action, mb: MoltbookClient, db: HistoryDB) -> str:
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
        except Exception as exc:
            logger.warning("Failed to upvote: %s", exc)
            return f"error: {exc}"
