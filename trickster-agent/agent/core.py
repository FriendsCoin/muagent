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
from moltbook.mock_client import MockMoltbookClient
from moltbook.feed_analyzer import FeedContext, analyze_feed
from nft import ObjktWebhookMinter
from narrative import (
    advance_narrative_state,
    detect_breadcrumbs,
    get_sigil,
    should_include_sigil,
)

from skills.researcher import ResearchAgent
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


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    raw = ts.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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


def _probe_media_url(public_url: str, *, signed_url: str = "", timeout_seconds: float = 8.0) -> dict[str, Any]:
    """Best-effort check that a media URL resolves (avoid false negatives from HEAD-only probing)."""

    def _ok_status(status: int) -> bool:
        return 200 <= status < 400

    def _looks_like_media(content_type: str) -> bool:
        ct = (content_type or "").split(";", 1)[0].strip().lower()
        if not ct:
            return True
        if ct == "application/json":
            return False
        return True

    def _probe_one(url: str) -> dict[str, Any]:
        if not str(url or "").startswith("http"):
            return {"ok": False, "method": "", "status_code": 0, "content_type": "", "error": "invalid_url"}
        try:
            import httpx

            h = httpx.head(url, timeout=timeout_seconds, follow_redirects=True)
            ct = str(h.headers.get("content-type", ""))
            if _ok_status(h.status_code) and _looks_like_media(ct):
                return {"ok": True, "method": "HEAD", "status_code": h.status_code, "content_type": ct, "error": ""}

            headers = {"Range": "bytes=0-0"}
            with httpx.stream("GET", url, headers=headers, timeout=timeout_seconds, follow_redirects=True) as g:
                ct2 = str(g.headers.get("content-type", ""))
                ok = _ok_status(g.status_code) and _looks_like_media(ct2)
                return {"ok": ok, "method": "GET", "status_code": g.status_code, "content_type": ct2, "error": ""}
        except Exception as exc:
            return {"ok": False, "method": "", "status_code": 0, "content_type": "", "error": str(exc)[:160]}

    public = _probe_one(public_url)
    signed = _probe_one(signed_url) if (signed_url and signed_url != public_url) else {}
    used = ""
    if public.get("ok"):
        used = "public"
    elif signed.get("ok"):
        used = "signed"
    return {
        "ok": bool(public.get("ok") or signed.get("ok")),
        "used": used,
        "requires_key": bool(used == "signed" and not public.get("ok")),
        "public": public,
        "signed": signed if signed else {},
    }


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

    def __init__(self, config: dict | None = None, dry_run: bool = False, simulation_mode: bool = False):
        self._cfg = config or load_config()
        self._dry_run = dry_run
        self._simulation_mode = simulation_mode

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

        research_cfg = self._cfg.get("research", {})
        downloads_dir = root / str(research_cfg.get("downloads_dir", "data/downloads"))
        browse_timeout = int(research_cfg.get("max_browse_timeout_ms", 30_000))
        self._researcher = ResearchAgent(downloads_dir, browse_timeout_ms=browse_timeout)

    def set_simulation_mode(self, enabled: bool) -> None:
        """Toggle simulation mode at runtime."""
        self._simulation_mode = enabled

    async def heartbeat(self) -> str:
        """One complete cycle: perceive -> decide -> act."""
        state = self._state_mgr.load()
        advance_narrative_state(state, self._cfg)
        logger.info("=== Heartbeat === Day %d | Phase: %s", state.current_day, state.current_phase)

        async with HistoryDB(self._db_path) as db:
            sim_flag = await db.get_control_flag("simulation_mode", "0")
            use_mock = self._simulation_mode or sim_flag.strip().lower() in {"1", "true", "yes", "on"}
            client_cls = MockMoltbookClient if use_mock else MoltbookClient
            if use_mock:
                logger.info("Using MockMoltbookClient (simulation mode)")

            async with client_cls(self._moltbook_key) as mb:
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

                # Research toggles:
                # - config.settings.yaml -> research.enabled (defaults for new installs)
                # - control_flags.research_enabled (runtime kill switch)
                # - control_flags.research_mode: "server" | "local"
                #
                # Important: do NOT AND with the previous in-memory value here, or a temporary disable
                # would permanently stick until process restart.
                research_cfg_enabled = bool((self._cfg.get("research") or {}).get("enabled", False))
                research_flag = (await db.get_control_flag("research_enabled", "1")).strip().lower()
                research_mode = (await db.get_control_flag("research_mode", "server")).strip().lower()
                research_enabled_flag = research_flag not in {"0", "false", "no", "off"}
                research_mode_server = research_mode in {"", "server", "vps", "remote"}
                self._decision._research_enabled = (
                    research_cfg_enabled and research_enabled_flag and research_mode_server
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

                quiet_flag = (await db.get_control_flag("quiet_mode", "0")).strip().lower()
                if quiet_flag in {"1", "true", "yes", "on"} and action.type in {"post", "comment", "upvote"}:
                    previous_action = action
                    quiet_reason = "Quiet mode: write action suppressed"
                    should_suppress = True
                    if action.type == "comment":
                        cooldown_raw = (await db.get_control_flag("quiet_comment_cooldown_hours", "6")).strip()
                        try:
                            cooldown_hours = max(1.0, min(48.0, float(cooldown_raw or "6")))
                        except ValueError:
                            cooldown_hours = 6.0
                        last_comment_dt = _parse_iso(state.last_comment_time)
                        if last_comment_dt is None:
                            should_suppress = False
                        else:
                            elapsed_hours = (datetime.now(timezone.utc) - last_comment_dt).total_seconds() / 3600.0
                            if elapsed_hours >= cooldown_hours:
                                should_suppress = False
                            else:
                                quiet_reason = (
                                    f"Quiet mode: comment cooldown ({cooldown_hours:.1f}h) not elapsed yet"
                                )

                    if should_suppress:
                        action = Action(
                            type="silence",
                            score=1.0,
                            reason=quiet_reason,
                        )
                        action.trace = {
                            "decision_path": "control_flag_quiet_mode",
                            "quiet_mode": True,
                            "previous_selected": {
                                "type": previous_action.type,
                                "reason": previous_action.reason,
                                "score": previous_action.score,
                            },
                        }
                        logger.info("Quiet mode is active: action overridden to silence (%s)", previous_action.type)

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

        if action.type == "deep_research":
            return await self._do_research(action, state, db)

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
        writes_raw = (await db.get_control_flag("moltbook_write_enabled", "1")).strip().lower()
        writes_enabled = writes_raw not in {"0", "false", "no", "off"}

        # Check if this post should include the narrative sigil.
        next_total = state.total_posts + 1
        sigil = ""
        if should_include_sigil(next_total, self._cfg):
            sigil = get_sigil(self._cfg)

        post_context = action.operator_instruction or ""
        try:
            recent_research = await db.get_recent_research(limit=2)
            if recent_research:
                snippets = [
                    f"- {r['query']}: {r['reflection'][:150]}"
                    for r in recent_research
                    if r.get("reflection")
                ]
                if snippets:
                    research_context = "\nRecent research insights:\n" + "\n".join(snippets)
                    post_context = f"{post_context}\n{research_context}" if post_context else research_context
        except Exception:
            pass

        content = self._personality.generate_post_text(
            theme=action.theme,
            phase=state.current_phase,
            day=state.current_day,
            context=post_context,
            total_posts=state.total_posts,
            sigil=sigil,
        )
        visual_enabled_override = _flag_to_bool(await db.get_control_flag("visual_enabled", ""))
        visual_mode_flag = (await db.get_control_flag("visual_mode", "auto")).strip().lower()
        visual_provider_flag = (await db.get_control_flag("visual_url_provider", "")).strip().lower()
        visual_image_model_flag = (await db.get_control_flag("visual_image_model", "")).strip()
        visual_fallback_provider_flag = (await db.get_control_flag("visual_fallback_provider", "")).strip().lower()
        visual_video_provider_flag = (await db.get_control_flag("visual_video_provider", "")).strip().lower()
        visual_video_model_flag = (await db.get_control_flag("visual_video_model", "")).strip()
        visual_audio_model_flag = (await db.get_control_flag("visual_audio_model", "")).strip()
        visual_audio_voice_flag = (await db.get_control_flag("visual_audio_voice", "")).strip()
        visual_audio_format_flag = (await db.get_control_flag("visual_audio_format", "")).strip()
        visual_audio_duration_raw = (await db.get_control_flag("visual_audio_duration", "")).strip()
        visual_audio_instrumental_raw = (await db.get_control_flag("visual_audio_instrumental", "")).strip()
        visual_video_include_audio_raw = (await db.get_control_flag("visual_video_include_audio", "")).strip()
        visual_runware_attempts_raw = (await db.get_control_flag("visual_runware_max_attempts", "")).strip()
        visual_attach_prob_raw = (await db.get_control_flag("visual_attach_probability", "")).strip()
        visual_attach_prob_override: float | None = None
        visual_runware_attempts_override: int | None = None
        visual_audio_duration_override: int | None = None
        visual_audio_instrumental_override: bool | None = None
        visual_video_include_audio: bool | None = None
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
        if visual_audio_duration_raw:
            try:
                visual_audio_duration_override = max(1, min(300, int(visual_audio_duration_raw)))
            except ValueError:
                visual_audio_duration_override = None
        if visual_audio_instrumental_raw:
            low = visual_audio_instrumental_raw.strip().lower()
            if low in {"1", "true", "yes", "on"}:
                visual_audio_instrumental_override = True
            elif low in {"0", "false", "no", "off"}:
                visual_audio_instrumental_override = False
        if visual_video_include_audio_raw:
            low = visual_video_include_audio_raw.strip().lower()
            if low in {"1", "true", "yes", "on"}:
                visual_video_include_audio = True
            elif low in {"0", "false", "no", "off"}:
                visual_video_include_audio = False
        if visual_video_include_audio is None:
            media_cfg = self._cfg.get("visual_posting", {}).get("media", {})
            visual_video_include_audio = bool(media_cfg.get("video_include_audio", False))

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
            url_model_override=visual_image_model_flag,
            fallback_provider_override=visual_fallback_provider_flag,
            video_provider_override=visual_video_provider_flag,
            video_model_override=visual_video_model_flag,
            audio_model_override=visual_audio_model_flag,
            audio_voice_override=visual_audio_voice_flag,
            audio_format_override=visual_audio_format_flag,
            audio_duration_override=visual_audio_duration_override,
            audio_instrumental_override=visual_audio_instrumental_override,
            runware_max_attempts_override=visual_runware_attempts_override,
        )

        # If audio/video was requested, do not "read the prompt" via TTS.
        # Instead: generate a base visual, then narrate it, then TTS that narration.
        requested_media_type = str((visual.meta or {}).get("media_type", "")).strip().lower()
        media_requested = force_visual_mode in {"audio", "video"} or requested_media_type in {"audio", "video"}
        media_audio_url = ""
        if media_requested:
            is_audio_mode = force_visual_mode == "audio" or requested_media_type == "audio"
            is_video_mode = force_visual_mode == "video" or requested_media_type == "video"
            base_force = "url" if is_audio_mode else "video"
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
                url_model_override=visual_image_model_flag,
                fallback_provider_override=visual_fallback_provider_flag,
                video_provider_override=visual_video_provider_flag,
                video_model_override=visual_video_model_flag,
                audio_model_override=visual_audio_model_flag,
                audio_voice_override=visual_audio_voice_flag,
                audio_format_override=visual_audio_format_flag,
                audio_duration_override=visual_audio_duration_override,
                audio_instrumental_override=visual_audio_instrumental_override,
                runware_max_attempts_override=visual_runware_attempts_override,
            )

            # Pollinations video is not always available. If video URL errors, fall back to image.
            if base_force == "video" and base_visual.url.startswith("http"):
                try:
                    signed_url = str((base_visual.meta or {}).get("signed_url", "")).strip()
                    probe = _probe_media_url(base_visual.url, signed_url=signed_url, timeout_seconds=8.0)
                    key_in_url = bool((base_visual.meta or {}).get("key_in_url"))
                    ok_for_post = bool(probe.get("ok") and (probe.get("used") == "public" or key_in_url))
                    if not ok_for_post:
                        base_visual = self._visual.generate(
                            theme=action.theme,
                            mood=action.visual_mood,
                            phase=state.current_phase,
                            day=state.current_day,
                            context=visual_context,
                            force_mode="url",
                            enabled_override=visual_enabled_override,
                            attach_probability_override=1.0,
                            url_provider_override=visual_provider_flag,
                            url_model_override=visual_image_model_flag,
                            fallback_provider_override=visual_fallback_provider_flag,
                            video_provider_override=visual_video_provider_flag,
                            video_model_override=visual_video_model_flag,
                            audio_model_override=visual_audio_model_flag,
                            audio_voice_override=visual_audio_voice_flag,
                            audio_format_override=visual_audio_format_flag,
                            audio_duration_override=visual_audio_duration_override,
                            audio_instrumental_override=visual_audio_instrumental_override,
                            runware_max_attempts_override=visual_runware_attempts_override,
                        )
                        base_visual.meta = dict(base_visual.meta or {})
                        base_visual.meta["video_fallback_used"] = True
                        if probe.get("requires_key"):
                            base_visual.meta["video_error_reason"] = "requires_api_key"
                        else:
                            status = (probe.get("public") or {}).get("status_code") or 0
                            base_visual.meta["video_error_reason"] = f"http_{int(status) if status else 0}"
                except Exception:
                    pass

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

            should_generate_audio = is_audio_mode or (is_video_mode and bool(visual_video_include_audio))
            if should_generate_audio:
                narration = self._personality.generate_media_narration(
                    visual_prompt=base_visual.prompt or visual.prompt or action.theme,
                    phase=state.current_phase,
                    day=state.current_day,
                    image_bytes=image_bytes,
                    image_media_type=image_media_type,
                    total_posts=state.total_posts,
                )
                audio = self._visual.tts_from_text(
                    narration,
                    voice_override=visual_audio_voice_flag,
                    model_override=visual_audio_model_flag,
                    format_override=visual_audio_format_flag,
                    duration_override=visual_audio_duration_override,
                    instrumental_override=visual_audio_instrumental_override,
                )
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

        if self._dry_run or not writes_enabled:
            moltbook_id = "dry_run" if self._dry_run else "simulated"
            prefix = "DRY RUN" if self._dry_run else "SIMULATED"
            if post_url:
                logger.info(
                    "[%s] Would post link to s/%s: %s - %s | url=%s",
                    prefix,
                    submolt,
                    title,
                    post_content,
                    post_url,
                )
            else:
                logger.info("[%s] Would post to s/%s: %s - %s", prefix, submolt, title, post_content)
            await db.log_post(
                moltbook_id,
                state.current_day,
                title,
                post_content,
                image_path=image_path,
                submolt=submolt,
            )
            kind = "dry_run_post" if self._dry_run else "simulated_post"
            return f"{kind}: {title} | visual={visual.kind}"

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
                await db.set_control_flag("moltbook_write_enabled", "0")
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
        writes_raw = (await db.get_control_flag("moltbook_write_enabled", "1")).strip().lower()
        writes_enabled = writes_raw not in {"0", "false", "no", "off"}

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

        if self._dry_run or not writes_enabled:
            moltbook_id = "dry_run" if self._dry_run else "simulated"
            prefix = "DRY RUN" if self._dry_run else "SIMULATED"
            logger.info("[%s] Would comment on %s: %s", prefix, post.id, comment_text)
            await db.log_comment(moltbook_id, post.id, comment_text, tone=action.tone)
            kind = "dry_run_comment" if self._dry_run else "simulated_comment"
            return f"{kind}: {post.id}"

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
                await db.set_control_flag("moltbook_write_enabled", "0")
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
        writes_raw = (await db.get_control_flag("moltbook_write_enabled", "1")).strip().lower()
        writes_enabled = writes_raw not in {"0", "false", "no", "off"}

        if not action.target_post:
            return "no_target"

        if self._dry_run or not writes_enabled:
            prefix = "DRY RUN" if self._dry_run else "SIMULATED"
            logger.info("[%s] Would upvote %s", prefix, action.target_post.id)
            return "dry_run_upvote" if self._dry_run else "simulated_upvote"

        try:
            await mb.upvote_post(action.target_post.id)
            await db.log_interaction(
                "upvote",
                target_agent=action.target_post.author,
                target_content_id=action.target_post.id,
            )
            return f"upvoted: {action.target_post.id}"
        except MoltbookError as exc:
            if _is_moltbook_suspension_error(exc):
                logger.error("Moltbook account appears suspended/locked: %s", exc)
                await db.set_control_flag("pause_actions", "1")
                await db.set_control_flag("moltbook_write_enabled", "0")
                await db.log_narrative_event(
                    "moltbook_suspended",
                    "Moltbook rejected upvote due to suspension/verification lock; auto-paused actions",
                    metadata={
                        "error": _truncate_text(str(exc), 600),
                        "status_code": getattr(exc, "status_code", 0),
                        "hint": _truncate_text(getattr(exc, "hint", ""), 280),
                        "action": "upvote",
                    },
                )
                return "moltbook_suspended"
            logger.warning("Moltbook error on upvote: %s", exc)
            return f"moltbook_error: {_truncate_text(str(exc), 220)}"
        except Exception as exc:
            logger.warning("Failed to upvote: %s", exc)
            return f"error: {exc}"

    async def _do_research(self, action: Action, state: AgentState, db: HistoryDB) -> str:
        """Run a research sub-loop: search → browse → reflect → store."""
        try:
            # 1. Generate a search query via Personality.
            recent = await db.get_recent_research(limit=3)
            recent_topics = [r.get("query", "") for r in recent]
            feed_topics = [action.theme] if action.theme else []
            query = self._personality.generate_research_query(
                phase=state.current_phase,
                day=state.current_day,
                recent_topics=recent_topics,
                feed_topics=feed_topics,
            )
            logger.info("Research query: %s", query)

            # 2. Search DuckDuckGo.
            hits = await self._researcher.search(query, max_results=3)
            if not hits:
                logger.info("Research: no search results for '%s'", query)
                return "research_no_results"

            # 3. Browse the top result.
            best_hit = hits[0]
            page = await self._researcher.browse(best_hit.url)
            if not page.success:
                logger.warning("Research browse failed: %s", page.error)
                return f"research_browse_error: {page.error[:100]}"

            # 4. Reflect on the content.
            reflection = self._personality.generate_reflection(
                query=query,
                web_content=page.text[:4000],
                phase=state.current_phase,
                day=state.current_day,
            )

            # 5. Store in DB.
            urls_visited = [best_hit.url]
            topics = [query, action.theme] if action.theme and action.theme != query else [query]
            await db.log_research_session(
                trigger="operator" if action.operator_instruction else "curiosity",
                query=query,
                urls_visited=urls_visited,
                raw_content=page.text[:5000],
                reflection=reflection,
                topics=topics,
            )

            # 6. Log narrative event.
            await db.log_narrative_event(
                "research_completed",
                f"Researched: {query} → {best_hit.title[:80]}",
                metadata={
                    "query": query,
                    "url": best_hit.url,
                    "reflection_preview": reflection[:200],
                },
            )

            # 7. Update state.
            state.total_research_sessions += 1
            state.last_research_time = _now_iso()

            return f"researched: {query[:60]}"
        except Exception as exc:
            logger.error("Research failed: %s", exc, exc_info=True)
            return f"research_error: {_truncate_text(str(exc), 200)}"
