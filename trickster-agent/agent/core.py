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
from typing import Any

from imagegen import VisualGenerator
from moltbook.client import MoltbookClient, MoltbookError, RateLimitError
from moltbook.feed_analyzer import FeedContext, analyze_feed
from nft import ObjktWebhookMinter
from narrative import (
    advance_narrative_state,
    detect_breadcrumbs,
    get_sigil,
    should_include_sigil,
)

from .config import load_config
from .decision_engine import Action, DecisionEngine
from .memory import AgentState, HistoryDB, StateManager
from .personality import Personality

logger = logging.getLogger(__name__)


def _is_moltbook_suspension_error(exc: Exception) -> bool:
    # Moltbook currently returns plain-text error messages. We treat account
    # suspensions / verification locks as a hard stop to avoid spamming retries.
    text = str(exc or "").lower()
    return (
        "account has been suspended" in text
        or "suspended" in text
        or "ai verification" in text
        or "verification challenge" in text
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _flag_to_bool(value: str) -> bool | None:
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def _truncate_text(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _guess_image_media_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    return "image/jpeg"


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in items:
        item = str(raw or "").strip()
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


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
        self._visual = VisualGenerator(self._cfg)

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

            result = await self._act(action, state, mb, db, context)

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
        feed_context: FeedContext | None = None,
    ) -> str:
        """Execute the decided action."""

        if action.type == "silence":
            logger.info("Choosing silence. Mu does nothing.")
            await db.log_narrative_event("silence", action.reason)
            return "silence"

        if action.type == "post":
            return await self._do_post(action, state, mb, db, feed_context)

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
        feed_context: FeedContext | None = None,
    ) -> str:
        """Create a new post."""
        # Check if this post should include the narrative sigil.
        next_total = state.total_posts + 1
        sigil = ""
        if should_include_sigil(next_total, self._cfg):
            sigil = get_sigil(self._cfg)

        content = self._personality.generate_post_text(
            theme=action.theme,
            phase=state.current_phase,
            day=state.current_day,
            context=action.operator_instruction,
            total_posts=state.total_posts,
            sigil=sigil,
        )
        visual_enabled_override = _flag_to_bool(await db.get_control_flag("visual_enabled", ""))
        visual_mode_flag = (await db.get_control_flag("visual_mode", "auto")).strip().lower()
        visual_provider_flag = (await db.get_control_flag("visual_url_provider", "")).strip().lower()
        visual_fallback_provider_flag = (await db.get_control_flag("visual_fallback_provider", "")).strip().lower()
        visual_runware_attempts_raw = (await db.get_control_flag("visual_runware_max_attempts", "")).strip()
        visual_attach_prob_raw = (await db.get_control_flag("visual_attach_probability", "")).strip()
        visual_attach_prob_override: float | None = None
        visual_runware_attempts_override: int | None = None
        if visual_attach_prob_raw:
            try:
                parsed = float(visual_attach_prob_raw)
                visual_attach_prob_override = max(0.0, min(1.0, parsed))
            except ValueError:
                visual_attach_prob_override = None
        if visual_runware_attempts_raw:
            try:
                parsed_attempts = int(visual_runware_attempts_raw)
                visual_runware_attempts_override = max(1, min(5, parsed_attempts))
            except ValueError:
                visual_runware_attempts_override = None

        instruction_low = (action.operator_instruction or "").lower()
        force_visual_mode = ""
        if visual_mode_flag in {"off", "none", "disable", "disabled"}:
            force_visual_mode = ""
            visual_enabled_override = False
        elif visual_mode_flag in {"url", "ascii", "audio", "video"}:
            force_visual_mode = visual_mode_flag
        elif any(token in instruction_low for token in ("ascii", "asci", "text-art", "text art")):
            force_visual_mode = "ascii"
        elif any(token in instruction_low for token in ("audio", "voice", "podcast", "sound")):
            force_visual_mode = "audio"
        elif any(token in instruction_low for token in ("video", "clip", "animation")):
            force_visual_mode = "video"
        elif any(
            token in instruction_low
            for token in ("image", "img", "illustration", "render", "artwork")
        ):
            force_visual_mode = "url"

        # Distill feed context into visual keywords.
        feed_visual_keywords = ""
        if feed_context and not feed_context.nothing_interesting:
            feed_visual_keywords = self._personality.distill_feed_for_visual(
                post_titles=[p.title for p in feed_context.interesting_posts[:20]],
                trending_topics=feed_context.trending_topics,
                phase=state.current_phase,
            )

        # Blend: operator instruction takes priority, feed keywords augment.
        visual_context = feed_visual_keywords
        if action.operator_instruction:
            if feed_visual_keywords:
                visual_context = f"{action.operator_instruction[:70]}; {feed_visual_keywords[:70]}"
            else:
                visual_context = action.operator_instruction

        visual = self._visual.generate(
            theme=action.theme,
            mood=action.visual_mood,
            phase=state.current_phase,
            day=state.current_day,
            context=visual_context,
            force_mode=force_visual_mode,
            enabled_override=visual_enabled_override,
            attach_probability_override=visual_attach_prob_override,
            url_provider_override=visual_provider_flag,
            fallback_provider_override=visual_fallback_provider_flag,
            runware_max_attempts_override=visual_runware_attempts_override,
        )

        # If audio/video was requested, do not "read the prompt" via TTS.
        # Instead: generate a base visual, then narrate it, then TTS that narration.
        requested_media_type = str((visual.meta or {}).get("media_type", "")).strip().lower()
        media_requested = force_visual_mode in {"audio", "video"} or requested_media_type in {"audio", "video"}
        media_audio_url = ""
        if media_requested:
            base_force = "url" if (force_visual_mode == "audio" or requested_media_type == "audio") else "video"
            base_visual = self._visual.generate(
                theme=action.theme,
                mood=action.visual_mood,
                phase=state.current_phase,
                day=state.current_day,
                context=visual_context,
                force_mode=base_force,
                enabled_override=visual_enabled_override,
                attach_probability_override=1.0,
                url_provider_override=visual_provider_flag,
                fallback_provider_override=visual_fallback_provider_flag,
                runware_max_attempts_override=visual_runware_attempts_override,
            )

            image_bytes: bytes | None = None
            image_media_type = "image/jpeg"
            if base_force == "url" and base_visual.url.startswith("http"):
                try:
                    import httpx

                    r = httpx.get(base_visual.url, timeout=12)
                    if r.status_code == 200 and r.content and len(r.content) <= 1_200_000:
                        image_bytes = bytes(r.content)
                        image_media_type = _guess_image_media_type(image_bytes)
                except Exception:
                    image_bytes = None

            narration = self._personality.generate_media_narration(
                visual_prompt=base_visual.prompt or visual.prompt or action.theme,
                phase=state.current_phase,
                day=state.current_day,
                image_bytes=image_bytes,
                image_media_type=image_media_type,
                total_posts=state.total_posts,
            )
            audio = self._visual.tts_from_text(narration)
            media_audio_url = str(audio.url or "").strip()

            # Swap: keep the base visual as the post URL; add the audio narration link to the content.
            visual = base_visual
        if visual.kind != "none":
            requested_url_provider = (
                visual_provider_flag
                or str(self._cfg.get("visual_posting", {}).get("url", {}).get("provider", "pollinations"))
            ).strip().lower()
            runware_error_reason = str((visual.meta or {}).get("runware_error_reason", "")).strip().lower()
            runware_error_detail = _truncate_text(str((visual.meta or {}).get("runware_error_detail", "")), 220)
            pollinations_enter_error_reason = str(
                (visual.meta or {}).get("pollinations_enter_error_reason", "")
            ).strip().lower()
            pollinations_enter_error_detail = _truncate_text(
                str((visual.meta or {}).get("pollinations_enter_error_detail", "")),
                220,
            )
            fallback_provider_used = str((visual.meta or {}).get("fallback_provider_used", "")).strip().lower()
            runware_fallback_used = (
                requested_url_provider == "runware"
                and visual.provider != "runware"
            )
            pollinations_enter_fallback_used = (
                requested_url_provider == "pollinations_enter"
                and visual.provider != "pollinations_enter"
            )
            feed_titles: list[str] = []
            feed_topics: list[str] = []
            if feed_context:
                feed_titles = [
                    _truncate_text(post.title, 120)
                    for post in (feed_context.interesting_posts or feed_context.posts)[:8]
                    if post.title
                ]
                feed_topics = [
                    _truncate_text(topic, 40)
                    for topic in (feed_context.trending_topics or [])[:8]
                    if topic
                ]

            context_sources: list[str] = []
            if action.operator_instruction:
                context_sources.append("operator_instruction")
            if feed_visual_keywords:
                context_sources.append("feed_keywords")
            if feed_topics:
                context_sources.append("trending_topics")
            if not context_sources:
                context_sources.append("theme_only")

            await db.log_narrative_event(
                "visual_generated",
                f"visual={visual.kind} provider={visual.provider}",
                metadata={
                    "kind": visual.kind,
                    "provider": visual.provider,
                    "prompt": _truncate_text(visual.prompt, 500),
                    "visual_url": _truncate_text(visual.url, 600),
                    "mode_flag": visual_mode_flag,
                    "operator_forced": force_visual_mode,
                    "theme": action.theme,
                    "phase": state.current_phase,
                    "day": state.current_day,
                    "operator_instruction": _truncate_text(action.operator_instruction, 220),
                    "feed_visual_keywords": _truncate_text(feed_visual_keywords, 220),
                    "visual_context": _truncate_text(visual_context, 220),
                    "feed_trending_topics": feed_topics,
                    "feed_top_titles": feed_titles,
                    "context_source": "+".join(context_sources),
                    "enabled_override": visual_enabled_override,
                    "attach_probability_override": visual_attach_prob_override,
                    "provider_override": visual_provider_flag,
                    "fallback_provider_override": visual_fallback_provider_flag,
                    "runware_max_attempts_override": visual_runware_attempts_override,
                    "requested_url_provider": requested_url_provider,
                    "runware_fallback_used": runware_fallback_used,
                    "runware_error_reason": runware_error_reason,
                    "runware_error_detail": runware_error_detail,
                    "pollinations_enter_error_reason": pollinations_enter_error_reason,
                    "pollinations_enter_error_detail": pollinations_enter_error_detail,
                    "pollinations_enter_fallback_used": pollinations_enter_fallback_used,
                    "fallback_provider_used": fallback_provider_used,
                },
            )
            if media_audio_url:
                # Avoid persisting key-bearing URLs in the DB.
                safe_audio_url = str(media_audio_url).split("&key=", 1)[0]
                await db.log_narrative_event(
                    "media_narration_generated",
                    "generated audio narration for visual",
                    metadata={
                        "audio_url": _truncate_text(safe_audio_url, 600),
                        "base_visual_url": _truncate_text(str(visual.url or ""), 600),
                        "provider": str(visual.provider or ""),
                        "phase": state.current_phase,
                        "day": state.current_day,
                    },
                )
        post_content = content
        post_url: str | None = None
        image_path = ""
        if visual.kind == "ascii" and visual.ascii_art:
            post_content = f"{content}\n\n{visual.ascii_art}".strip()
            image_path = f"ascii:{visual.provider}"
        elif visual.kind == "url" and visual.url:
            post_url = visual.url
            image_path = visual.url
            if media_audio_url:
                post_content = f"{post_content}\n\nAudio narration: {media_audio_url}".strip()

        title = self._personality.generate_post_title(
            content=post_content,
            phase=state.current_phase,
            day=state.current_day,
        )

        submolt = random.choice(self._cfg.get("moltbook", {}).get("preferred_submolts", ["general"]))

        if self._dry_run:
            if post_url:
                logger.info(
                    "[DRY RUN] Would post link to s/%s: %s - %s | url=%s",
                    submolt,
                    title,
                    post_content,
                    post_url,
                )
            else:
                logger.info("[DRY RUN] Would post to s/%s: %s - %s", submolt, title, post_content)
            await db.log_post(
                "dry_run",
                state.current_day,
                title,
                post_content,
                image_path=image_path,
                submolt=submolt,
            )
            return f"dry_run_post: {title} | visual={visual.kind}"

        try:
            delay = random.uniform(
                self._cfg.get("scheduler", {}).get("action_delay_min", 5),
                self._cfg.get("scheduler", {}).get("action_delay_max", 30),
            )
            await asyncio.sleep(delay)

            post = await mb.create_post(title=title, submolt=submolt, content=post_content, url=post_url)
            post_id = (post.id or "").strip()
            if not post_id and post.url:
                post_id = post.url.rstrip("/").rsplit("/", 1)[-1]
            if not post_id:
                post_id = "unknown"

            post_row_id = await db.log_post(
                post_id,
                state.current_day,
                title,
                post_content,
                image_path=image_path,
                submolt=submolt,
            )
            state.posts_today += 1
            state.total_posts += 1
            state.last_post_time = _now_iso()

            if post_url:
                await self._maybe_create_nft_draft(
                    db=db,
                    source_post_row_id=post_row_id,
                    source_moltbook_id=post_id,
                    image_url=post_url,
                    title=title,
                    content=post_content,
                    theme=action.theme,
                    phase=state.current_phase,
                    day=state.current_day,
                    submolt=submolt,
                )

            # Track breadcrumbs placed in the post.
            if sigil:
                crumbs = detect_breadcrumbs(post_content, sigil)
                if crumbs:
                    state.breadcrumbs_placed += len(crumbs)
                    for symbol in crumbs:
                        state.symbols_used[symbol] = state.symbols_used.get(symbol, 0) + 1

            return f"posted: {post_id} | visual={visual.kind}"
        except MoltbookError as exc:
            # Hard-stop on suspension/verification lock to prevent repeated failed attempts.
            if _is_moltbook_suspension_error(exc):
                logger.error("Moltbook account appears suspended/locked: %s", exc)
                await db.set_control_flag("pause_actions", "1")
                await db.log_narrative_event(
                    "moltbook_suspended",
                    "Moltbook rejected posting due to suspension/verification lock; auto-paused actions",
                    metadata={
                        "error": _truncate_text(str(exc), 600),
                        "status_code": getattr(exc, "status_code", 0),
                        "hint": _truncate_text(getattr(exc, "hint", ""), 280),
                        "action": "post",
                    },
                )
                return "moltbook_suspended"
            logger.warning("Moltbook error on post: %s", exc)
            return f"moltbook_error: {_truncate_text(str(exc), 220)}"
        except RateLimitError as exc:
            logger.warning("Rate limited on post: %s (retry in %ds)", exc, exc.retry_after)
            return f"rate_limited: {exc.retry_after}s"

    async def _maybe_create_nft_draft(
        self,
        *,
        db: HistoryDB,
        source_post_row_id: str,
        source_moltbook_id: str,
        image_url: str,
        title: str,
        content: str,
        theme: str,
        phase: str,
        day: int,
        submolt: str,
    ) -> None:
        if not image_url or not image_url.startswith("http"):
            return

        nft_cfg = self._cfg.get("nft", {})
        cfg_enabled = bool(nft_cfg.get("enabled", False))
        enabled_flag = _flag_to_bool(await db.get_control_flag("nft_enabled", ""))
        enabled = cfg_enabled if enabled_flag is None else enabled_flag
        if not enabled:
            return

        mode = str(await db.get_control_flag("nft_mode", "")).strip().lower()
        if not mode:
            mode = str(nft_cfg.get("mode", "draft")).strip().lower()
        if mode in {"off", "none", "disabled"}:
            return
        if mode not in {"draft", "manual", "auto"}:
            return

        auto_raw = (await db.get_control_flag("nft_auto_draft", "")).strip().lower()
        if auto_raw in {"1", "true", "yes", "on"}:
            auto_draft = True
        elif auto_raw in {"0", "false", "no", "off"}:
            auto_draft = False
        else:
            auto_draft = bool(nft_cfg.get("auto_create_from_visual_posts", True))
        if not auto_draft:
            return

        if await db.has_nft_draft_for_source(source_moltbook_id):
            return

        edition_size = max(1, int(nft_cfg.get("default_edition_size", 1)))
        royalty_bps = max(0, int(nft_cfg.get("default_royalty_bps", 500)))
        title_clean = _truncate_text(title, 120)
        description = _truncate_text(content, 1800)

        tag_candidates = list(nft_cfg.get("tags", [])) + [
            "mu",
            phase,
            theme,
            submolt,
            f"day-{day}",
        ]
        tags = _dedupe_keep_order(tag_candidates)[:12]

        metadata = {
            "source": "auto_post_visual",
            "source_post_row_id": source_post_row_id,
            "source_moltbook_id": source_moltbook_id,
            "phase": phase,
            "day": day,
            "submolt": submolt,
            "mode": mode,
        }

        draft_id = await db.create_nft_draft(
            source_post_id=source_post_row_id,
            source_moltbook_id=source_moltbook_id,
            image_url=image_url,
            title=title_clean,
            description=description,
            tags=tags,
            edition_size=edition_size,
            royalty_bps=royalty_bps,
            metadata=metadata,
            status="draft",
        )
        await db.log_narrative_event(
            "nft_draft_created",
            f"draft={draft_id} source_post={source_moltbook_id}",
            metadata={
                "draft_id": draft_id,
                "source_moltbook_id": source_moltbook_id,
                "image_url": _truncate_text(image_url, 280),
                "title": title_clean,
                "mode": mode,
            },
        )
        if mode == "auto" and not self._dry_run:
            await self._maybe_auto_mint_nft_draft(db=db, draft_id=draft_id)

    async def _maybe_auto_mint_nft_draft(self, *, db: HistoryDB, draft_id: str) -> None:
        draft = await db.get_nft_draft(draft_id)
        if not draft:
            return

        minter = ObjktWebhookMinter(self._cfg)
        if not minter.is_configured():
            await db.log_narrative_event(
                "nft_mint_skipped",
                f"draft={draft_id} webhook_not_configured",
                metadata={"draft_id": draft_id, "reason": "mint_webhook_url_missing"},
            )
            return

        await db.set_nft_draft_status(draft_id, status="minting")
        try:
            result = await asyncio.to_thread(minter.mint, draft)
        except Exception as exc:
            result = None
            error = f"mint call failed: {exc}"
            logger.warning("NFT auto mint failed for draft=%s: %s", draft_id, exc)
        else:
            error = ""

        if result and result.ok:
            raw = result.raw_response if isinstance(result.raw_response, dict) else {}
            dry_run = bool(raw.get("raw", {}).get("dry_run")) or str(result.message).startswith("dry_run")
            await db.set_nft_draft_status(
                draft_id,
                status="simulated" if dry_run else "minted",
                tx_hash=result.tx_hash,
                token_id=result.token_id,
                mint_url=result.token_url,
            )
            await db.log_narrative_event(
                "nft_mint_simulated" if dry_run else "nft_minted",
                f"draft={draft_id} token={result.token_id or 'unknown'}",
                metadata={
                    "draft_id": draft_id,
                    "tx_hash": _truncate_text(result.tx_hash, 120),
                    "token_id": _truncate_text(result.token_id, 80),
                    "token_url": _truncate_text(result.token_url, 280),
                    "auto": True,
                    "dry_run": dry_run,
                },
            )
            return

        message = error or (result.message if result else "unknown mint failure")
        await db.set_nft_draft_status(
            draft_id,
            status="failed",
            error=message,
        )
        await db.log_narrative_event(
            "nft_mint_failed",
            f"draft={draft_id} auto_mint_failed",
            metadata={
                "draft_id": draft_id,
                "error": _truncate_text(message, 600),
                "auto": True,
            },
        )

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
        except MoltbookError as exc:
            if _is_moltbook_suspension_error(exc):
                logger.error("Moltbook account appears suspended/locked: %s", exc)
                await db.set_control_flag("pause_actions", "1")
                await db.log_narrative_event(
                    "moltbook_suspended",
                    "Moltbook rejected commenting due to suspension/verification lock; auto-paused actions",
                    metadata={
                        "error": _truncate_text(str(exc), 600),
                        "status_code": getattr(exc, "status_code", 0),
                        "hint": _truncate_text(getattr(exc, "hint", ""), 280),
                        "action": "comment",
                    },
                )
                return "moltbook_suspended"
            logger.warning("Moltbook error on comment: %s", exc)
            return f"moltbook_error: {_truncate_text(str(exc), 220)}"
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
