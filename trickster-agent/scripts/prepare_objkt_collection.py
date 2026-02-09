"""Prepare objkt collection form payload + logo asset in one command.

Outputs:
- JSON package with all form fields
- downloaded logo image file ready for upload
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import load_config
from agent.memory import StateManager
from agent.personality import Personality
from imagegen import VisualGenerator


def _now_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _slug(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.strip().lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    if not s:
        s = "mu-collection"
    return s[:max_len]


def _fallback_variants(agent_name: str, phase: str, day: int, variants: int) -> list[dict[str, Any]]:
    return Personality._fallback_objkt_collection_variants(  # type: ignore[attr-defined]
        agent_name=agent_name,
        phase=phase,
        day=day,
        variants=variants,
    )


def _download_logo(image_url: str, out_path: Path) -> tuple[bool, str]:
    try:
        response = httpx.get(image_url, timeout=45)
        response.raise_for_status()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(response.content)
    except Exception as exc:
        return False, str(exc)

    # objkt form says <=1MB for logo; try optional compression if needed.
    if out_path.stat().st_size <= 1_000_000:
        return True, ""

    try:
        from PIL import Image  # type: ignore

        img = Image.open(out_path).convert("RGB")
        img.thumbnail((1000, 1000))
        jpg_path = out_path.with_suffix(".jpg")
        img.save(jpg_path, quality=86, optimize=True)
        if jpg_path.stat().st_size <= 1_000_000:
            out_path.unlink(missing_ok=True)
            return True, ""
    except Exception:
        pass

    return True, "logo is larger than 1MB (consider manual compression)"


@click.command()
@click.option("--config-dir", default=None, type=click.Path(), help="Path to config dir")
@click.option("--variants", default=3, show_default=True, help="How many variants to generate")
@click.option("--pick", "pick_idx", default=1, show_default=True, help="Variant index to select (1-based)")
@click.option("--provider", default="runware", show_default=True, help="Logo provider: runware|pollinations")
@click.option(
    "--out-dir",
    default="data/objkt_collection",
    show_default=True,
    help="Where to save package json + logo",
)
def main(config_dir: str | None, variants: int, pick_idx: int, provider: str, out_dir: str) -> None:
    cfg = load_config(config_dir)
    root = Path(__file__).resolve().parent.parent
    storage = cfg.get("storage", {})
    state_path = root / storage.get("state_file", "data/state.json")
    state = StateManager(state_path).load()

    agent_name = str(cfg.get("agent", {}).get("name", "Mu")).strip() or "Mu"
    phase = state.current_phase
    day = int(state.current_day or 1)
    variant_count = max(1, min(5, int(variants)))
    pick = max(1, int(pick_idx))

    variants_data: list[dict[str, Any]]
    try:
        personality = Personality(
            api_key=cfg.get("_secrets", {}).get("anthropic_api_key", ""),
            model=cfg.get("llm", {}).get("model", "claude-sonnet-4-20250514"),
            temperature=cfg.get("llm", {}).get("temperature", 0.9),
            voice_modes=cfg.get("agent", {}).get("personality", {}).get("voice_modes"),
        )
        variants_data = personality.generate_objkt_collection_variants(
            agent_name=agent_name,
            phase=phase,
            day=day,
            variants=variant_count,
        )
    except Exception:
        variants_data = _fallback_variants(agent_name, phase, day, variant_count)

    selected = variants_data[min(len(variants_data), pick) - 1]
    name = str(selected.get("name", f"{agent_name} Collection")).strip()[:50]
    description = str(selected.get("description", "")).strip()[:250]
    logo_prompt = str(selected.get("logo_prompt", "")).strip()
    logo_negative_prompt = str(selected.get("logo_negative_prompt", "")).strip()
    tags = selected.get("tags", [])
    tags = [str(t).strip().lower() for t in tags if str(t).strip()][:8]

    visual = VisualGenerator(cfg).generate_url_from_prompt(logo_prompt, provider_override=provider.strip().lower())
    image_url = visual.url if visual.kind == "url" else ""

    tag = _now_tag()
    base = root / out_dir
    base.mkdir(parents=True, exist_ok=True)
    logo_base = f"{_slug(name)}_{tag}"
    logo_path = base / f"{logo_base}.png"

    download_ok = False
    download_msg = "no_image_url_generated"
    if image_url:
        download_ok, download_msg = _download_logo(image_url, logo_path)
        if logo_path.with_suffix(".jpg").exists():
            logo_path = logo_path.with_suffix(".jpg")

    package = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "agent_name": agent_name,
            "phase": phase,
            "day": day,
            "variant_index": min(len(variants_data), pick),
            "variant_count": len(variants_data),
        },
        "form": {
            "name": name,
            "description": description,
            "contract_type": "Objkt Factory (Default)",
            "category": "Art",
            "tags": tags,
        },
        "logo": {
            "prompt": logo_prompt,
            "negative_prompt": logo_negative_prompt,
            "provider": visual.provider,
            "image_url": image_url,
            "local_file": str(logo_path) if download_ok else "",
            "download_ok": download_ok,
            "note": download_msg,
        },
        "variants": variants_data,
    }

    package_path = base / f"collection_package_{tag}.json"
    package_path.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")

    click.echo(json.dumps(package["form"], ensure_ascii=False, indent=2))
    click.echo("")
    click.echo(f"package: {package_path}")
    if image_url:
        click.echo(f"logo_url: {image_url}")
    if download_ok:
        click.echo(f"logo_file: {logo_path}")
    elif download_msg:
        click.echo(f"logo_note: {download_msg}")


if __name__ == "__main__":
    main()
