"""Lightweight visual generation helpers for post enrichment.

This module intentionally supports low-friction providers:
- URL-based image links (no local GPU required)
- Local deterministic ASCII art (no external API)
"""

from __future__ import annotations

import hashlib
import random
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote_plus

import httpx


@dataclass
class VisualAttachment:
    kind: str = "none"  # "none" | "url" | "ascii"
    provider: str = ""
    prompt: str = ""
    url: str = ""
    ascii_art: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


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
        self._fallback_provider = str(self._url_cfg.get("fallback_provider", "pollinations")).strip().lower()
        self._runware_max_attempts = max(1, int(self._url_cfg.get("runware_max_attempts", 1)))
        self._media_cfg = self._cfg.get("media", {})
        self._pollinations_media_endpoint = str(
            self._media_cfg.get("pollinations_endpoint", "https://gen.pollinations.ai")
        ).rstrip("/")
        self._media_public_include_key = bool(self._media_cfg.get("public_url_include_key", False))

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
        fallback_provider_override: str = "",
        runware_max_attempts_override: int | None = None,
    ) -> VisualAttachment:
        """Return a visual attachment candidate for a post."""
        forced = force_mode.strip().lower()
        if forced and forced not in {"url", "ascii", "audio", "video"}:
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
            return self._generate_url(
                prompt,
                day=day,
                provider_override=url_provider_override,
                fallback_provider_override=fallback_provider_override,
                runware_max_attempts_override=runware_max_attempts_override,
            )
        if mode == "audio":
            return self._generate_audio(prompt=prompt)
        if mode == "video":
            return self._generate_video(prompt=prompt)
        if mode == "ascii":
            return self._generate_ascii(prompt=prompt, day=day)
        return VisualAttachment()

    def generate_url_from_prompt(self, prompt: str, provider_override: str = "") -> VisualAttachment:
        """Generate URL visual from an explicit prompt (bypasses auto prompt builder)."""
        clean = str(prompt or "").strip()
        if not clean:
            return VisualAttachment()
        return self._generate_url(clean, day=1, provider_override=provider_override)

    def _pick_mode(self) -> str:
        url_w = max(0.0, float(self._mode_weights.get("url", 0.65)))
        ascii_w = max(0.0, float(self._mode_weights.get("ascii", 0.35)))
        audio_w = max(0.0, float(self._mode_weights.get("audio", 0.0)))
        video_w = max(0.0, float(self._mode_weights.get("video", 0.0)))
        if url_w <= 0 and ascii_w <= 0 and audio_w <= 0 and video_w <= 0:
            return "url"
        return random.choices(
            ["url", "ascii", "audio", "video"],
            weights=[url_w, ascii_w, audio_w, video_w],
            k=1,
        )[0]

    def _pollinations_key_query(self) -> str:
        key = str(self._secrets.get("pollinations_api_key", "")).strip()
        if not key:
            return ""
        return f"&key={quote_plus(key)}"

    def tts_from_text(self, text: str, voice_override: str = "") -> VisualAttachment:
        """Create a TTS audio URL (Pollinations) from plain text."""
        clean = str(text or "").strip()
        if not clean:
            return VisualAttachment()
        voice = str(voice_override or self._media_cfg.get("audio_voice", "alloy")).strip() or "alloy"
        return self._generate_audio(prompt=clean, voice_override=voice)

    def _generate_audio(self, *, prompt: str) -> VisualAttachment:
        voice = str(self._media_cfg.get("audio_voice", "alloy")).strip() or "alloy"
        return self._generate_audio(prompt=prompt, voice_override=voice)

    def _generate_audio(self, *, prompt: str, voice_override: str = "") -> VisualAttachment:
        voice = str(voice_override or self._media_cfg.get("audio_voice", "alloy")).strip() or "alloy"
        seed_base = hashlib.sha1(f"audio:{prompt}".encode("utf-8")).hexdigest()
        seed = int(seed_base[:8], 16)
        encoded = quote_plus(prompt)
        public_url = (
            f"{self._pollinations_media_endpoint}/audio/{encoded}"
            f"?voice={quote_plus(voice)}&seed={seed}"
        )
        signed_url = public_url + self._pollinations_key_query()
        url = signed_url if (self._media_public_include_key and self._pollinations_key_query()) else public_url
        return VisualAttachment(
            kind="url",
            provider="pollinations_audio",
            prompt=prompt,
            url=url,
            meta={
                "media_type": "audio",
                "voice": voice,
                "public_url": public_url,
                "signed_url": signed_url if self._pollinations_key_query() else "",
                "key_in_url": bool(self._media_public_include_key and self._pollinations_key_query()),
            },
        )

    def _generate_video(self, *, prompt: str) -> VisualAttachment:
        model = str(self._media_cfg.get("video_model", "fast")).strip() or "fast"
        seed_base = hashlib.sha1(f"video:{prompt}".encode("utf-8")).hexdigest()
        seed = int(seed_base[:8], 16)
        encoded = quote_plus(prompt)
        public_url = (
            f"{self._pollinations_media_endpoint}/video/{encoded}"
            f"?model={quote_plus(model)}&seed={seed}"
        )
        signed_url = public_url + self._pollinations_key_query()
        url = signed_url if (self._media_public_include_key and self._pollinations_key_query()) else public_url
        return VisualAttachment(
            kind="url",
            provider="pollinations_video",
            prompt=prompt,
            url=url,
            meta={
                "media_type": "video",
                "model": model,
                "public_url": public_url,
                "signed_url": signed_url if self._pollinations_key_query() else "",
                "key_in_url": bool(self._media_public_include_key and self._pollinations_key_query()),
            },
        )

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

    def _generate_url(
        self,
        prompt: str,
        *,
        day: int,
        provider_override: str = "",
        fallback_provider_override: str = "",
        runware_max_attempts_override: int | None = None,
    ) -> VisualAttachment:
        provider = str(provider_override or self._url_cfg.get("provider", "pollinations")).lower()
        requested_runware = provider == "runware"
        requested_pollinations_enter = provider in {"pollinations_enter", "pollinations_api"}
        fallback_provider = str(fallback_provider_override or self._fallback_provider or "pollinations").lower()
        if fallback_provider not in {"pollinations", "ascii"}:
            fallback_provider = "pollinations"
        runware_attempts = self._runware_max_attempts if runware_max_attempts_override is None else int(runware_max_attempts_override)
        runware_attempts = max(1, min(5, runware_attempts))
        last_reason = ""
        last_detail = ""
        pollinations_enter_reason = ""
        pollinations_enter_detail = ""
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
            for _ in range(runware_attempts):
                runware, reason, detail = self._generate_runware_url(prompt=prompt, width=width, height=height)
                if runware is not None:
                    runware.meta.update(
                        {
                            "requested_provider": "runware",
                            "runware_fallback_used": False,
                            "runware_error_reason": "",
                            "runware_error_detail": "",
                        }
                    )
                    return runware
                last_reason = reason
                last_detail = detail
            if fallback_provider == "ascii":
                visual = self._generate_ascii(prompt=prompt, day=day)
                visual.meta.update(
                    {
                        "requested_provider": "runware",
                        "runware_fallback_used": True,
                        "runware_error_reason": last_reason,
                        "runware_error_detail": last_detail,
                        "fallback_provider_used": "ascii",
                    }
                )
                return visual
            provider = "pollinations"

        if provider in {"pollinations_enter", "pollinations_api"}:
            enter_visual, pollinations_enter_reason, pollinations_enter_detail = self._generate_pollinations_enter_url(
                prompt=prompt,
                width=width,
                height=height,
            )
            if enter_visual is not None:
                enter_visual.meta.update(
                    {
                        "requested_provider": "pollinations_enter",
                        "pollinations_enter_fallback_used": False,
                        "pollinations_enter_error_reason": "",
                        "pollinations_enter_error_detail": "",
                    }
                )
                return enter_visual
            provider = "pollinations"

        if provider == "pollinations":
            model = self._url_cfg.get("model", "flux")
            seed_base = hashlib.sha1(prompt.encode("utf-8")).hexdigest()
            seed = int(seed_base[:8], 16)
            encoded = quote_plus(prompt)
            url = (
                f"https://image.pollinations.ai/prompt/{encoded}"
                f"?width={width}&height={height}&model={quote_plus(str(model))}&seed={seed}"
            )
            visual = VisualAttachment(kind="url", provider="pollinations", prompt=prompt, url=url)
            if requested_runware:
                visual.meta.update(
                    {
                        "requested_provider": "runware",
                        "runware_fallback_used": True,
                        "runware_error_reason": last_reason,
                        "runware_error_detail": last_detail,
                        "fallback_provider_used": "pollinations",
                    }
                )
            if requested_pollinations_enter:
                visual.meta.update(
                    {
                        "requested_provider": "pollinations_enter",
                        "pollinations_enter_fallback_used": True,
                        "pollinations_enter_error_reason": pollinations_enter_reason,
                        "pollinations_enter_error_detail": pollinations_enter_detail,
                        "fallback_provider_used": "pollinations",
                    }
                )
            return visual

        # Unknown provider fallback: deterministic pollinations URL.
        encoded = quote_plus(prompt)
        url = f"https://image.pollinations.ai/prompt/{encoded}?width={width}&height={height}"
        return VisualAttachment(kind="url", provider="pollinations_fallback", prompt=prompt, url=url)

    @staticmethod
    def _classify_runware_response(status_code: int, text: str) -> str:
        lower = (text or "").lower()
        if status_code == 401:
            return "auth_failed"
        if status_code == 402:
            return "insufficient_credits"
        if status_code == 429:
            return "rate_limited"
        if 500 <= status_code <= 599:
            return "provider_5xx"
        if "credit" in lower or "insufficient" in lower or "balance" in lower:
            return "insufficient_credits"
        return f"http_{status_code}"

    def _generate_runware_url(self, *, prompt: str, width: int, height: int) -> tuple[VisualAttachment | None, str, str]:
        api_key = str(self._secrets.get("runware_api_key", "")).strip()
        if not api_key:
            return None, "no_key", "RUNWARE_API_KEY missing"

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
        except httpx.TimeoutException as exc:
            return None, "timeout", str(exc)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            raw = ""
            if exc.response is not None:
                raw = exc.response.text[:220]
            reason = self._classify_runware_response(status_code, raw)
            return None, reason, raw
        except httpx.RequestError as exc:
            return None, "network_error", str(exc)
        except ValueError as exc:
            return None, "invalid_json", str(exc)
        except Exception:
            return None, "unknown_error", ""

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
                return VisualAttachment(kind="url", provider="runware", prompt=prompt, url=image_url), "", ""
        return None, "empty_response", "Runware response had no imageURL"

    @staticmethod
    def _classify_pollinations_response(status_code: int, text: str) -> str:
        lower = (text or "").lower()
        if status_code == 401:
            return "auth_failed"
        if status_code == 402:
            return "insufficient_credits"
        if status_code == 429:
            return "rate_limited"
        if 500 <= status_code <= 599:
            return "provider_5xx"
        if "credit" in lower or "insufficient" in lower or "balance" in lower or "quota" in lower:
            return "insufficient_credits"
        return f"http_{status_code}"

    def _generate_pollinations_enter_url(
        self, *, prompt: str, width: int, height: int
    ) -> tuple[VisualAttachment | None, str, str]:
        api_key = str(self._secrets.get("pollinations_api_key", "")).strip()
        if not api_key:
            return None, "no_key", "POLLINATIONS_API_KEY missing"

        base = str(self._url_cfg.get("pollinations_enter_endpoint", "https://gen.pollinations.ai/openai")).strip()
        base = base.rstrip("/")
        endpoint_candidates: list[str] = []
        # Accept multiple base styles:
        # - https://gen.pollinations.ai/openai
        # - https://gen.pollinations.ai/openai/v1
        # - https://gen.pollinations.ai
        if base.endswith("/v1"):
            endpoint_candidates.append(base + "/images/generations")
        elif base.endswith("/openai"):
            endpoint_candidates.append(base + "/v1/images/generations")
            endpoint_candidates.append(base + "/images/generations")
        else:
            endpoint_candidates.append(base + "/openai/v1/images/generations")
            endpoint_candidates.append(base + "/openai/images/generations")
        model = str(self._url_cfg.get("model", "flux")).strip() or "flux"
        timeout_seconds = float(self._url_cfg.get("timeout_seconds", 25))

        payload = {
            "model": model,
            "prompt": prompt,
            "size": f"{width}x{height}",
            "response_format": "url",
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        last_reason = "unknown_error"
        last_detail = ""
        for endpoint in endpoint_candidates:
            try:
                response = httpx.post(endpoint, json=payload, headers=headers, timeout=timeout_seconds)
                response.raise_for_status()
                body = response.json()
            except httpx.TimeoutException as exc:
                last_reason = "timeout"
                last_detail = str(exc)
                continue
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code if exc.response is not None else 0
                raw = ""
                if exc.response is not None:
                    raw = exc.response.text[:220]
                last_reason = self._classify_pollinations_response(status_code, raw)
                last_detail = f"{endpoint} :: {raw}"
                # Try next candidate for 404 path mismatch; stop early for auth/quota/rate.
                if status_code in {401, 402, 403, 429}:
                    return None, last_reason, last_detail
                continue
            except httpx.RequestError as exc:
                last_reason = "network_error"
                last_detail = str(exc)
                continue
            except ValueError as exc:
                last_reason = "invalid_json"
                last_detail = str(exc)
                continue
            except Exception:
                last_reason = "unknown_error"
                last_detail = ""
                continue

            data = []
            if isinstance(body, dict):
                raw_data = body.get("data", [])
                if isinstance(raw_data, list):
                    data = raw_data
                elif isinstance(raw_data, dict):
                    data = [raw_data]
            for item in data:
                if not isinstance(item, dict):
                    continue
                image_url = str(item.get("url", "") or item.get("image_url", "")).strip()
                if image_url:
                    return (
                        VisualAttachment(kind="url", provider="pollinations_enter", prompt=prompt, url=image_url),
                        "",
                        "",
                    )
            last_reason = "empty_response"
            last_detail = f"{endpoint} :: Pollinations response had no image url"

        return None, last_reason, last_detail

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
