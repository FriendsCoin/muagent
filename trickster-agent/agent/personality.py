"""Personality engine - generates text in Mu's trickster voice.

Uses the Anthropic Claude API to produce captions, comments, and posts
that match the trickster archetype: cryptic, warm, short, koan-like.
"""

from __future__ import annotations

import logging
import random
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
- Numbered days ("Day X") but with gaps - days 13, 33, 66 do not exist.
- The symbol ?? appears occasionally without explanation.

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
        logger.debug("Generated (%d chars): %s", len(text), text[:80])
        return text

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
    ) -> str:
        """Generate a standalone text post (no image)."""
        mode = self._pick_mode(
            theme=theme,
            text=context,
            total_posts=total_posts,
            mode_hint=mode_hint,
        )
        prompt = (
            "Generate a text-only post for Moltbook.\n"
            f"Theme: {theme}\n"
            f"Additional context: {context or 'none'}\n"
            f"Voice mode: {mode}\n"
            "This is a standalone post - it can be a koan, a day entry, a question,\n"
            "a cryptic observation, or just the sigil.\n"
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
        prompt = (
            "Generate a very short title (under 80 chars) for this Moltbook post.\n"
            f"Post content: \"{content[:300]}\"\n"
            f"The title should be cryptic, intriguing, or just a day number like 'Day {day}'.\n"
            "Just output the title text, nothing else."
        )
        return self._generate(prompt, phase=phase, day=day, mode="zen", max_tokens=50)

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
