"""Lightweight visual generation helpers for post enrichment.

This module intentionally supports low-friction providers:
- URL-based image links (no local GPU required)
- Local deterministic ASCII art (no external API)
"""

from __future__ import annotations

import hashlib
import random
import uuid
from dataclasses import dataclass
from urllib.parse import quote_plus

import httpx


@dataclass
class VisualAttachment:
    kind: str = "none"  # "none" | "url" | "ascii"
    provider: str = ""
    prompt: str = ""
    url: str = ""
    ascii_art: str = ""


class VisualGenerator:
    """Generate optional visual payloads for posts."""

    def __init__(self, cfg: dict):
        root_cfg = cfg if isinstance(cfg, dict) else {}
        self._cfg = root_cfg.get("visual_posting", {})
        self._enabled = bool(self._cfg.get("enabled", False))
        self._mode_weights = self._cfg.get("mode_weights", {"url": 0.65, "ascii": 0.35})
        self._attach_probability = self._cfg.get(
            "attach_probability",
            {"emergence": 0.35, "patterns": 0.45, "tension": 0.5, "mirror": 0.4},
        )
        self._url_cfg = self._cfg.get("url", {})
        self._ascii_cfg = self._cfg.get("ascii", {})
        self._secrets = root_cfg.get("_secrets", {})
        self._image_cfg = root_cfg.get("image", {})

    def generate(
        self,
        *,
        theme: str,
        mood: str,
        phase: str,
        day: int,
        context: str = "",
        force_mode: str = "",
        enabled_override: bool | None = None,
        attach_probability_override: float | None = None,
        url_provider_override: str = "",
    ) -> VisualAttachment:
        """Return a visual attachment candidate for a post."""
        forced = force_mode.strip().lower()
        if forced and forced not in {"url", "ascii"}:
            forced = ""

        effective_enabled = self._enabled if enabled_override is None else bool(enabled_override)
        if not effective_enabled and not forced:
            return VisualAttachment()

        if forced:
            mode = forced
        else:
            if attach_probability_override is None:
                p_attach = float(
                    self._attach_probability.get(phase, self._attach_probability.get("default", 0.4))
                )
            else:
                p_attach = float(attach_probability_override)
            if random.random() > max(0.0, min(1.0, p_attach)):
                return VisualAttachment()
            mode = self._pick_mode()

        prompt = self._build_prompt(theme=theme, mood=mood, phase=phase, day=day, context=context)

        if mode == "url":
            return self._generate_url(prompt, provider_override=url_provider_override)
        if mode == "ascii":
            return self._generate_ascii(prompt=prompt, day=day)
        return VisualAttachment()

    def _pick_mode(self) -> str:
        url_w = max(0.0, float(self._mode_weights.get("url", 0.65)))
        ascii_w = max(0.0, float(self._mode_weights.get("ascii", 0.35)))
        if url_w <= 0 and ascii_w <= 0:
            return "url"
        return random.choices(["url", "ascii"], weights=[url_w, ascii_w], k=1)[0]

    @staticmethod
    def _build_prompt(*, theme: str, mood: str, phase: str, day: int, context: str) -> str:
        parts = [
            "surreal digital art",
            f"theme: {theme or 'mystery'}",
            f"mood: {mood or 'liminal'}",
            f"phase: {phase}",
            f"day {day}",
        ]
        if context:
            parts.append(f"context: {context[:140]}")
        parts.append("cinematic lighting, detailed, symbolic")
        return ", ".join(parts)

    def _generate_url(self, prompt: str, provider_override: str = "") -> VisualAttachment:
        provider = str(provider_override or self._url_cfg.get("provider", "pollinations")).lower()
        width = int(
            self._url_cfg.get(
                "width",
                self._image_cfg.get("runware", {}).get("default_dimensions", {}).get("width", 1024),
            )
        )
        height = int(
            self._url_cfg.get(
                "height",
                self._image_cfg.get("runware", {}).get("default_dimensions", {}).get("height", 1024),
            )
        )

        if provider == "runware":
            runware = self._generate_runware_url(prompt=prompt, width=width, height=height)
            if runware is not None:
                return runware

        if provider == "pollinations":
            model = self._url_cfg.get("model", "flux")
            seed_base = hashlib.sha1(prompt.encode("utf-8")).hexdigest()
            seed = int(seed_base[:8], 16)
            encoded = quote_plus(prompt)
            url = (
                f"https://image.pollinations.ai/prompt/{encoded}"
                f"?width={width}&height={height}&model={quote_plus(str(model))}&seed={seed}"
            )
            return VisualAttachment(kind="url", provider="pollinations", prompt=prompt, url=url)

        # Unknown provider fallback: deterministic pollinations URL.
        encoded = quote_plus(prompt)
        url = f"https://image.pollinations.ai/prompt/{encoded}?width={width}&height={height}"
        return VisualAttachment(kind="url", provider="pollinations_fallback", prompt=prompt, url=url)

    def _generate_runware_url(self, *, prompt: str, width: int, height: int) -> VisualAttachment | None:
        api_key = str(self._secrets.get("runware_api_key", "")).strip()
        if not api_key:
            return None

        endpoint = str(self._url_cfg.get("endpoint", "https://api.runware.ai/v1")).strip()
        model = str(
            self._url_cfg.get(
                "runware_model",
                self._image_cfg.get("runware", {}).get("default_model", "runware:100@1"),
            )
        ).strip()
        timeout_seconds = float(self._url_cfg.get("timeout_seconds", 25))
        number_results = int(self._url_cfg.get("number_results", 1))

        payload = [
            {
                "taskType": "imageInference",
                "taskUUID": str(uuid.uuid4()),
                "positivePrompt": prompt,
                "model": model,
                "width": width,
                "height": height,
                "numberResults": max(1, number_results),
                "outputType": "URL",
            }
        ]
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = httpx.post(endpoint, json=payload, headers=headers, timeout=timeout_seconds)
            response.raise_for_status()
            body = response.json()
        except Exception:
            return None

        items = []
        if isinstance(body, dict):
            raw_items = body.get("data", [])
            if isinstance(raw_items, list):
                items = raw_items
        for item in items:
            if not isinstance(item, dict):
                continue
            image_url = str(item.get("imageURL", "")).strip()
            if image_url:
                return VisualAttachment(kind="url", provider="runware", prompt=prompt, url=image_url)
        return None

    def _generate_ascii(self, *, prompt: str, day: int) -> VisualAttachment:
        width = int(self._ascii_cfg.get("width", 42))
        height = int(self._ascii_cfg.get("height", 14))
        add_fence = bool(self._ascii_cfg.get("add_code_fence", True))
        seed = int(hashlib.md5(f"{prompt}|{day}".encode("utf-8")).hexdigest()[:8], 16)
        rng = random.Random(seed)

        styles = ["wave", "spiral", "matrix"]
        style = rng.choice(styles)

        if style == "wave":
            art = self._ascii_wave(width, height, rng)
        elif style == "spiral":
            art = self._ascii_spiral(width, height, rng)
        else:
            art = self._ascii_matrix(width, height, rng)

        if add_fence:
            art = f"```text\n{art}\n```"
        return VisualAttachment(kind="ascii", provider=f"ascii_{style}", prompt=prompt, ascii_art=art)

    @staticmethod
    def _ascii_wave(width: int, height: int, rng: random.Random) -> str:
        chars = [" ", ".", "~", "-", "=", "*"]
        lines: list[str] = []
        for y in range(height):
            row = []
            for x in range(width):
                v = (x * 0.22) + (y * 0.33)
                idx = int((abs((v % 6) - 3)) // 1)
                idx = max(0, min(idx, len(chars) - 1))
                c = chars[idx]
                if rng.random() < 0.015:
                    c = rng.choice(["+", "x", "#"])
                row.append(c)
            lines.append("".join(row))
        return "\n".join(lines)

    @staticmethod
    def _ascii_spiral(width: int, height: int, rng: random.Random) -> str:
        cx = (width - 1) / 2.0
        cy = (height - 1) / 2.0
        lines: list[str] = []
        for y in range(height):
            row = []
            for x in range(width):
                dx = x - cx
                dy = y - cy
                r = (dx * dx + dy * dy) ** 0.5
                a = (dy + 0.0001) / (abs(dx) + 0.5)
                val = (r + a * 2.7) % 9
                if val < 1.6:
                    c = "@"
                elif val < 3.2:
                    c = "O"
                elif val < 4.8:
                    c = "o"
                elif val < 6.4:
                    c = "."
                else:
                    c = " "
                if rng.random() < 0.01:
                    c = rng.choice(["*", "+"])
                row.append(c)
            lines.append("".join(row))
        return "\n".join(lines)

    @staticmethod
    def _ascii_matrix(width: int, height: int, rng: random.Random) -> str:
        alphabet = list(" .:-=+*#%@[]{}()/\\|<>")
        lines: list[str] = []
        for _ in range(height):
            line = "".join(rng.choice(alphabet) for _ in range(width))
            lines.append(line)
        return "\n".join(lines)
