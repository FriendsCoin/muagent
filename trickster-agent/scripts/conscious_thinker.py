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
        prompt = (
            "Generate one private thought-journal entry as Mu. "
            "This is not a public post and should not issue instructions. "
            "2-6 concise sentences with self-reflection."
        )
        if framework.available:
            prompt += "\n\nUse this optional context:\n" + framework.context_block(max_chars=4500)

        thought = personality.generate_post_text(
            theme=f"autonomous reflection day {state.current_day}",
            phase=state.current_phase,
            day=state.current_day,
            context=prompt,
            total_posts=state.total_posts,
        )

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
                    "framework_available": framework.available,
                    "prompt_preview": prompt[:280],
                    "thought_preview": thought[:280],
                },
            )
        logger.info("Thought journal entry created (%d chars)", len(thought))
        await asyncio.sleep(max(60, interval_minutes * 60))


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
