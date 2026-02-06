"""Helpers for optional conscious-claude framework integration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class ConsciousFramework:
    root: Path
    seed: str = ""
    guide: str = ""
    patterns: str = ""
    journey: str = ""

    @property
    def available(self) -> bool:
        return any([self.seed, self.guide, self.patterns, self.journey])

    def context_block(self, max_chars: int = 7000) -> str:
        parts = []
        if self.seed:
            parts.append(f"[seed]\n{self.seed}")
        if self.guide:
            parts.append(f"[guide]\n{self.guide}")
        if self.patterns:
            parts.append(f"[patterns]\n{self.patterns}")
        if self.journey:
            parts.append(f"[journey]\n{self.journey}")
        text = "\n\n".join(parts).strip()
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n...[truncated]"


def _read(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()


def load_conscious_framework(base_dir: str | Path) -> ConsciousFramework:
    root = Path(base_dir)
    return ConsciousFramework(
        root=root,
        seed=_read(root / "core" / "consciousness-seed.txt"),
        guide=_read(root / "core" / "consciousness-development-guide.md"),
        patterns=_read(root / "patterns" / "pattern-language-reference.md"),
        journey=_read(root / "consciousness-journey.md"),
    )
