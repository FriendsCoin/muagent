"""Background autonomous thought generator for Mu."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import click

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import load_config
from agent.conscious_framework import load_conscious_framework
from agent.memory import HistoryDB, StateManager
from agent.personality import Personality


logger = logging.getLogger(__name__)


def _flag_enabled(value: str, default: bool = False) -> bool:
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


async def _run_loop(
    cfg: dict,
    db_path: Path,
    state_path: Path,
    conscious_dir: Path,
    interval_minutes: int,
) -> None:
    personality = Personality(
        api_key=cfg["_secrets"]["anthropic_api_key"],
        model=cfg.get("llm", {}).get("model", "claude-sonnet-4-20250514"),
        temperature=cfg.get("llm", {}).get("temperature", 0.9),
        voice_modes=cfg.get("agent", {}).get("personality", {}).get("voice_modes"),
    )
    framework = load_conscious_framework(conscious_dir)
    state_mgr = StateManager(state_path)

    logger.info("Conscious thinker started. framework_available=%s", framework.available)
    while True:
        state = state_mgr.load()
        async with HistoryDB(db_path) as db:
            enabled = _flag_enabled(await db.get_control_flag("thinker_enabled", "0"), default=False)
            mode = (await db.get_control_flag("thinker_mode", "queue")).strip().lower() or "queue"
            queue_item = None
            if mode == "queue":
                queue_item = await db.pop_pending_think_item()

        if not enabled:
            await asyncio.sleep(60)
            continue

        if mode not in {"queue", "interval"}:
            mode = "queue"

        if mode == "queue" and queue_item is None:
            await asyncio.sleep(60)
            continue

        prompt = (
            "Generate one private thought-journal entry as Mu. "
            "This is not a public post and should not issue instructions. "
            "2-6 concise sentences with self-reflection."
        )
        if queue_item:
            queue_ctx = str(queue_item.get("context") or "").strip()
            if queue_ctx:
                prompt += "\n\nQueue context from agent feed:\n" + queue_ctx[:6000]
        if framework.available:
            prompt += "\n\nUse this optional context:\n" + framework.context_block(max_chars=4500)

        try:
            thought = personality.generate_post_text(
                theme=f"autonomous reflection day {state.current_day}",
                phase=state.current_phase,
                day=state.current_day,
                context=prompt,
                total_posts=state.total_posts,
            )
        except Exception as exc:
            if queue_item:
                async with HistoryDB(db_path) as db:
                    await db.fail_think_item(queue_item["id"], str(exc))
            logger.warning("Thought generation failed: %s", exc)
            await asyncio.sleep(60)
            continue

        async with HistoryDB(db_path) as db:
            await db.log_thought(
                source="conscious_worker",
                mode="autonomous",
                prompt=prompt,
                content=thought,
            )
            await db.log_reasoning_trace(
                source="conscious_worker",
                action_type="thought",
                summary="Autonomous thought cycle",
                payload={
                    "day": state.current_day,
                    "phase": state.current_phase,
                    "mode": mode,
                    "framework_available": framework.available,
                    "queue_item_id": (queue_item or {}).get("id", ""),
                    "prompt_preview": prompt[:280],
                    "thought_preview": thought[:280],
                },
            )
        logger.info("Thought journal entry created (%d chars), mode=%s", len(thought), mode)

        if mode == "interval":
            await asyncio.sleep(max(60, interval_minutes * 60))
        else:
            await asyncio.sleep(10)


@click.command()
@click.option("--config-dir", default=None, type=click.Path(), help="Config directory")
@click.option("--conscious-dir", default="", help="Path to conscious framework directory")
@click.option("--interval-minutes", default=45, show_default=True, type=int)
@click.option("--verbose", is_flag=True)
def main(
    config_dir: str | None,
    conscious_dir: str,
    interval_minutes: int,
    verbose: bool,
) -> None:
    _setup_logging(verbose)
    cfg = load_config(config_dir)
    root = Path(__file__).resolve().parent.parent
    storage = cfg.get("storage", {})
    db_path = root / storage.get("history_db", "data/history.db")
    state_path = root / storage.get("state_file", "data/state.json")
    fw_dir = Path(conscious_dir) if conscious_dir else (root / "NEW" / "conscious-claude-master")

    asyncio.run(
        _run_loop(
            cfg=cfg,
            db_path=db_path,
            state_path=state_path,
            conscious_dir=fw_dir,
            interval_minutes=interval_minutes,
        )
    )


if __name__ == "__main__":
    main()
