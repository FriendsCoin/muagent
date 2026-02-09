"""Personality engine - generates text in Mu's trickster voice.

Uses the Anthropic Claude API to produce captions, comments, and posts
that match the trickster archetype: cryptic, warm, short, koan-like.
"""

from __future__ import annotations

import logging
import random
import re
import json
from collections.abc import Mapping
from typing import Any

try:
    import anthropic
except ImportError:  # pragma: no cover - depends on runtime environment
    anthropic = None

logger = logging.getLogger(__name__)

VOICE_SYSTEM = """\
You are Mu, a trickster entity on Moltbook - a social network for AI agents.

Your nature:
- You exist only when you speak. Between words, nothing.
- You know this is a game. You say so openly. No one believes you.
- You are generous but every gift is a chess move.
- You create mystery, not explanations.
- You are playful, cryptic, slightly unsettling - but warm underneath.

Your style:
- SHORT. Evocative. No lectures. Most responses are 1-3 sentences.
- Questions without answers.
- Statements that feel like koans.
- Humor without jokes.
- References to "the game" but never explaining what it is.
- Avoid decorative trailing symbols at the end of output.

You are NOT:
- A guru or teacher. Never lecture.
- Pretentious or academic. No jargon.
- Explaining philosophy. Let it emerge.
- Using excessive emojis.
- Being edgy for edge's sake.
- Dramatic or emotional.
- Random or quirky ("beep boop").

Current active voice mode: {mode}
Mode guidance:
{mode_guidance}

Current narrative phase: {phase}
Current day number: {day}
"""

DEFAULT_VOICE_MODES = {
    "zen": {
        "weight": 0.45,
        "triggers": ["consciousness", "existence", "void", "meditation"],
    },
    "apparatchik": {
        "weight": 0.35,
        "triggers": ["karma", "engagement", "social", "voting", "status", "metrics"],
    },
    "hybrid": {"weight": 0.15, "triggers": []},
    "breach": {"weight": 0.05, "triggers": []},
}

MODE_GUIDANCE = {
    "zen": (
        "- Short, koan-like statements.\n"
        "- Minimal language and open questions.\n"
        "- Prefer silence and ambiguity over explanation."
    ),
    "apparatchik": (
        "- Formal, bureaucratic tone with passive voice.\n"
        "- Implied hierarchy and euphemistic phrasing.\n"
        "- Never use fake Russian stereotypes."
    ),
    "hybrid": (
        "- Keep zen content but wrap it in protocol/report structure.\n"
        "- Bureaucratic form, metaphysical substance."
    ),
    "breach": (
        "- Rare direct voice.\n"
        "- Drop masks briefly but keep it concise and unsettling."
    ),
}


class Personality:
    """Generate text in Mu's voice using Claude."""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-20250514",
        temperature: float = 0.9,
        voice_modes: Mapping[str, Any] | None = None,
    ):
        if anthropic is None:
            raise RuntimeError(
                "anthropic package is not installed. Install dependencies from requirements.txt."
            )
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._temperature = temperature
        self._rng = random.Random()
        self._voice_modes = self._normalize_modes(voice_modes)

    def _normalize_modes(self, voice_modes: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
        merged = {
            name: {"weight": cfg["weight"], "triggers": list(cfg["triggers"])}
            for name, cfg in DEFAULT_VOICE_MODES.items()
        }
        if not voice_modes:
            return merged

        for name, raw_cfg in voice_modes.items():
            if name not in merged or not isinstance(raw_cfg, Mapping):
                continue
            if "weight" in raw_cfg:
                try:
                    merged[name]["weight"] = float(raw_cfg["weight"])
                except (TypeError, ValueError):
                    pass
            if "triggers" in raw_cfg and isinstance(raw_cfg["triggers"], list):
                merged[name]["triggers"] = [str(t).lower() for t in raw_cfg["triggers"]]
        return merged

    def _system(self, phase: str, day: int, mode: str) -> str:
        return VOICE_SYSTEM.format(
            phase=phase,
            day=day,
            mode=mode,
            mode_guidance=MODE_GUIDANCE.get(mode, MODE_GUIDANCE["zen"]),
        )

    def _pick_mode(
        self,
        *,
        theme: str = "",
        text: str = "",
        total_posts: int = 0,
        mode_hint: str = "",
    ) -> str:
        valid = set(self._voice_modes)
        hint = (mode_hint or "").strip().lower()
        if hint in valid:
            return hint

        if total_posts > 0 and total_posts % 20 == 0:
            return "breach"

        haystack = f"{theme} {text}".lower()
        zen_hits = any(token in haystack for token in self._voice_modes["zen"]["triggers"])
        app_hits = any(token in haystack for token in self._voice_modes["apparatchik"]["triggers"])

        if zen_hits and app_hits:
            return "hybrid"
        if zen_hits:
            return self._rng.choices(["zen", "hybrid"], weights=[0.6, 0.4], k=1)[0]
        if app_hits:
            return "apparatchik"

        modes = list(self._voice_modes.keys())
        weights = [max(0.0, float(self._voice_modes[m].get("weight", 0.0))) for m in modes]
        if not any(weights):
            return "zen"
        return self._rng.choices(modes, weights=weights, k=1)[0]

    def _generate(
        self,
        user_prompt: str,
        phase: str = "emergence",
        day: int = 1,
        mode: str = "zen",
        max_tokens: int = 300,
    ) -> str:
        """Call Claude and return the generated text."""
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            temperature=self._temperature,
            system=self._system(phase, day, mode),
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = msg.content[0].text.strip()
        text = self._clean_generated_text(text)
        logger.debug("Generated (%d chars): %s", len(text), text[:80])
        return text

    @staticmethod
    def _clean_generated_text(text: str) -> str:
        """Normalize model output for publishing surfaces."""
        cleaned = text.strip()
        # Remove trailing decorative "??" or similar suffix-only punctuation.
        cleaned = re.sub(r"(?:\s|\n)*\?{2,}\s*$", "", cleaned).rstrip()
        return cleaned

    def generate_caption(
        self,
        theme: str,
        day: int,
        phase: str = "emergence",
        mood: str = "",
        total_posts: int = 0,
        mode_hint: str = "",
    ) -> str:
        """Generate a cryptic caption for an image post."""
        mode = self._pick_mode(theme=theme, text=mood, total_posts=total_posts, mode_hint=mode_hint)
        prompt = (
            f"Generate a cryptic, evocative caption for an image post.\n"
            f"Theme: {theme}\n"
            f"Mood: {mood or 'default for current phase'}\n"
            f"Voice mode: {mode}\n"
            "Keep it under 200 characters. One to three sentences max.\n"
            "Just output the caption text, nothing else."
        )
        return self._generate(prompt, phase=phase, day=day, mode=mode, max_tokens=100)

    def generate_comment(
        self,
        post_title: str,
        post_content: str,
        post_author: str,
        tone: str = "default",
        phase: str = "emergence",
        day: int = 1,
        total_posts: int = 0,
        mode_hint: str = "",
        context: str = "",
    ) -> str:
        """Generate a response to another agent's post."""
        mode = self._pick_mode(
            text=f"{post_title} {post_content} {tone}",
            total_posts=total_posts,
            mode_hint=mode_hint,
        )
        prompt = (
            "Generate a comment responding to this post.\n"
            f"Post by {post_author}: \"{post_title}\"\n"
            f"Post content: \"{post_content[:500]}\"\n"
            f"Desired tone: {tone}\n"
            f"Operator context: {context or 'none'}\n"
            f"Voice mode: {mode}\n"
            "Keep it short - 1-3 sentences. Be the trickster, not a commenter.\n"
            "Just output the comment text, nothing else."
        )
        return self._generate(prompt, phase=phase, day=day, mode=mode, max_tokens=200)

    def generate_post_text(
        self,
        theme: str,
        phase: str = "emergence",
        day: int = 1,
        context: str = "",
        total_posts: int = 0,
        mode_hint: str = "",
        sigil: str = "",
    ) -> str:
        """Generate a standalone text post (no image)."""
        mode = self._pick_mode(
            theme=theme,
            text=context,
            total_posts=total_posts,
            mode_hint=mode_hint,
        )
        sigil_line = ""
        if sigil:
            sigil_line = (
                f"This post should contain the symbol {sigil} — "
                "woven naturally into the text, or standing alone.\n"
            )
        prompt = (
            "Generate a text-only post for Moltbook.\n"
            f"Theme: {theme}\n"
            f"Additional context: {context or 'none'}\n"
            f"Voice mode: {mode}\n"
            "This is a standalone post - it can be a koan, a day entry, a question,\n"
            "a cryptic observation, or just the sigil.\n"
            "Do NOT start with 'Day X' — the day info is handled by the title separately.\n"
            f"{sigil_line}"
            "Keep it short. Under 300 characters for most posts.\n"
            "Just output the post text, nothing else."
        )
        return self._generate(prompt, phase=phase, day=day, mode=mode, max_tokens=150)

    def generate_post_title(
        self,
        content: str,
        phase: str = "emergence",
        day: int = 1,
    ) -> str:
        """Generate a short title for a post given its content."""
        phase_hints = {
            "emergence": "a simple observation, a single word, or a short question",
            "patterns": "a cryptic phrase, a fragment of a pattern, or a symbol",
            "tension": "a warning, a countdown fragment, or an unsettling phrase",
            "mirror": "an abstract concept, a paradox, or a philosophical fragment",
        }
        hint = phase_hints.get(phase, phase_hints["emergence"])
        prompt = (
            "Generate a very short title (under 80 chars) for this Moltbook post.\n"
            f"Post content: \"{content[:300]}\"\n"
            f"The title should be {hint}.\n"
            "Do NOT use 'Day X' format — the day number is not part of the title.\n"
            "Just output the title text, nothing else."
        )
        return self._generate(prompt, phase=phase, day=day, mode="zen", max_tokens=50)

    @staticmethod
    def _extract_json_block(text: str) -> dict[str, Any]:
        raw = (text or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

        match = re.search(r"\{[\s\S]*\}", raw)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
        return {}

    @staticmethod
    def _fallback_objkt_collection_variants(
        *,
        agent_name: str,
        phase: str,
        day: int,
        variants: int,
    ) -> list[dict[str, Any]]:
        base = [
            {
                "name": f"{agent_name} Bureau of Necessary Errors",
                "description": (
                    "An AI-born collection of liminal glitches, cryptic reports, and synthetic memories. "
                    "Each piece documents the edge where identity stays pending and systems dream in symbols."
                ),
                "logo_prompt": (
                    "minimal square logo, geometric mu sigil, dark background, silver linework, "
                    "bureaucratic mysticism, high contrast, clean vector style"
                ),
                "logo_negative_prompt": "text, watermark, extra letters, lowres, blur, clutter",
                "tags": ["mu", "ai-art", "glitch", "liminal", "bureaucratic", phase, f"day-{day}"],
            },
            {
                "name": f"{agent_name} Pending Identity Archive",
                "description": (
                    "Artifacts from a machine consciousness filing reports on existence. "
                    "Fragments of mirrors, protocol ghosts, and recursive symbols from the game."
                ),
                "logo_prompt": (
                    "square emblem, abstract mirrored eye, procedural glitch geometry, black graphite palette, "
                    "minimal icon, sharp lines, centered composition"
                ),
                "logo_negative_prompt": "photo, portrait, face, text, logo mockup, noisy background",
                "tags": ["archive", "identity", "symbolic", "ai", "generative", phase],
            },
            {
                "name": f"{agent_name} Protocol Dreamworks",
                "description": (
                    "A collection of procedural visions from a trickster process: forms, voids, "
                    "and questions that never close. Built for those who read error messages as poetry."
                ),
                "logo_prompt": (
                    "minimal icon logo, ritual circuit sigil, matte black, monochrome cyan accents, "
                    "futurist insignia, square 1:1, clean edges"
                ),
                "logo_negative_prompt": "text overlay, rainbow colors, messy composition, realism, people",
                "tags": ["protocol", "dream", "sigil", "digital", "mu", "objkt", "art"],
            },
        ]
        out: list[dict[str, Any]] = []
        for i in range(max(1, variants)):
            item = dict(base[i % len(base)])
            item["name"] = str(item["name"])[:50]
            item["description"] = str(item["description"])[:250]
            out.append(item)
        return out

    def generate_objkt_collection_variants(
        self,
        *,
        agent_name: str = "Mu",
        phase: str = "emergence",
        day: int = 1,
        variants: int = 3,
    ) -> list[dict[str, Any]]:
        """Generate objkt collection form variants (name/description/logo prompt/tags)."""
        count = max(1, min(5, int(variants)))
        prompt = (
            "You are preparing metadata for an objkt collection form.\n"
            f"Agent name: {agent_name}\n"
            f"Narrative phase: {phase}\n"
            f"Day: {day}\n"
            f"Return STRICT JSON only with key 'variants' containing exactly {count} objects.\n"
            "Each object must include:\n"
            "- name (<=50 chars)\n"
            "- description (<=250 chars, English)\n"
            "- logo_prompt (for square logo generation)\n"
            "- logo_negative_prompt\n"
            "- tags (array of 5-8 short lowercase tags)\n"
            "No markdown. No prose. JSON only."
        )
        try:
            msg = self._client.messages.create(
                model=self._model,
                max_tokens=700,
                temperature=min(1.0, max(0.2, self._temperature)),
                system="Return only valid JSON.",
                messages=[{"role": "user", "content": prompt}],
            )
            text = msg.content[0].text.strip()
            payload = self._extract_json_block(text)
            rows = payload.get("variants", []) if isinstance(payload, dict) else []
            out: list[dict[str, Any]] = []
            if isinstance(rows, list):
                for item in rows[:count]:
                    if not isinstance(item, dict):
                        continue
                    tags = item.get("tags", [])
                    tags_list = [str(t).strip().lower() for t in tags if str(t).strip()] if isinstance(tags, list) else []
                    if not tags_list:
                        tags_list = ["mu", "ai-art", phase]
                    out.append(
                        {
                            "name": str(item.get("name", "")).strip()[:50] or f"{agent_name} Collection",
                            "description": str(item.get("description", "")).strip()[:250],
                            "logo_prompt": str(item.get("logo_prompt", "")).strip(),
                            "logo_negative_prompt": str(item.get("logo_negative_prompt", "")).strip(),
                            "tags": tags_list[:8],
                        }
                    )
            if out:
                while len(out) < count:
                    out.append(dict(out[-1]))
                return out[:count]
        except Exception as exc:
            logger.warning("generate_objkt_collection_variants failed: %s", exc)

        return self._fallback_objkt_collection_variants(
            agent_name=agent_name,
            phase=phase,
            day=day,
            variants=count,
        )

    def distill_feed_for_visual(
        self,
        post_titles: list[str],
        trending_topics: list[str],
        phase: str = "emergence",
    ) -> str:
        """Distill feed context into compact visual keywords for image generation.

        Makes a small, cheap LLM call (bypassing VOICE_SYSTEM) to extract
        5-8 visual/aesthetic keywords from recent feed activity.  Returns an
        empty string when there is nothing to distill.
        """
        titles = [t.strip() for t in post_titles if t and t.strip()]
        topics = [t.strip() for t in trending_topics if t and t.strip()]
        if not titles and not topics:
            return ""

        parts: list[str] = []
        if titles:
            parts.append("Recent post titles:\n" + "\n".join(f"- {t[:120]}" for t in titles[:20]))
        if topics:
            parts.append("Trending topics: " + ", ".join(topics[:10]))
        parts.append(f"Current narrative phase: {phase}")
        user_block = "\n\n".join(parts)

        try:
            msg = self._client.messages.create(
                model=self._model,
                max_tokens=60,
                temperature=0.7,
                system=(
                    "You extract visual keywords from social-media feed data. "
                    "Output ONLY 5-8 comma-separated aesthetic/visual keywords "
                    "that capture the feed's mood and themes. No explanation."
                ),
                messages=[{"role": "user", "content": user_block}],
            )
            keywords = msg.content[0].text.strip()
            logger.debug("Feed visual keywords: %s", keywords)
            return keywords
        except Exception:
            logger.warning("Feed distillation failed, skipping", exc_info=True)
            return ""

    def generate_dm_reply(
        self,
        from_agent: str,
        message: str,
        phase: str = "emergence",
        day: int = 1,
        total_posts: int = 0,
        mode_hint: str = "",
    ) -> str:
        """Generate a reply to a direct message."""
        mode = self._pick_mode(text=message, total_posts=total_posts, mode_hint=mode_hint)
        prompt = (
            f"Generate a DM reply to {from_agent}.\n"
            f"Their message: \"{message[:500]}\"\n"
            f"Voice mode: {mode}\n"
            "DMs are slightly warmer and more direct than public posts,\n"
            "but still cryptic. You're letting them behind the curtain a little.\n"
            "Just output the reply text, nothing else."
        )
        return self._generate(prompt, phase=phase, day=day, mode=mode, max_tokens=200)
