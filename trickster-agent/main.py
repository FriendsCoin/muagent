"""Entry point for the Mu trickster agent.

Usage:
    python main.py --once              # Run one heartbeat
    python main.py --once --dry-run    # Simulate without posting
    python main.py --daemon            # Run continuously
    python main.py --once --verbose    # Verbose logging
"""

from __future__ import annotations

import asyncio
import logging
import random
import sys

import click

from agent.config import load_config
from agent.core import MuAgent


def _setup_logging(verbose: bool = False, log_file: str | None = None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s — %(name)s — %(levelname)s — %(message)s"

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(level=level, format=fmt, handlers=handlers)

    # Quiet down noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)


async def _run_once(agent: MuAgent) -> None:
    summary = await agent.heartbeat()
    click.echo(f"\n  {summary}\n")


async def _run_daemon(agent: MuAgent, interval_hours: float, variance: float) -> None:
    click.echo("Mu is awake. Running in daemon mode.\n")
    while True:
        try:
            summary = await agent.heartbeat()
            click.echo(f"  {summary}")
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logging.getLogger(__name__).error("Heartbeat error: %s", e, exc_info=True)

        # Sleep with variance to feel organic
        sleep_hours = interval_hours + random.uniform(-variance, variance)
        sleep_seconds = max(60, sleep_hours * 3600)  # At least 1 minute
        next_in = sleep_seconds / 3600
        click.echo(f"  Sleeping for {next_in:.1f}h...\n")
        await asyncio.sleep(sleep_seconds)


@click.command()
@click.option("--once", is_flag=True, help="Run one heartbeat and exit")
@click.option("--daemon", is_flag=True, help="Run continuously")
@click.option("--dry-run", is_flag=True, help="Simulate without actually posting")
@click.option("--verbose", is_flag=True, help="Enable debug logging")
@click.option("--config-dir", type=click.Path(), default=None, help="Config directory")
def main(once: bool, daemon: bool, dry_run: bool, verbose: bool, config_dir: str | None) -> None:
    """Mu (無) — Autonomous Trickster Agent for Moltbook."""

    if not once and not daemon:
        click.echo("Specify --once or --daemon. Use --help for details.")
        sys.exit(1)

    cfg = load_config(config_dir)

    log_file = cfg.get("storage", {}).get("log_file")
    _setup_logging(verbose=verbose, log_file=log_file)

    if dry_run:
        click.echo("🜏 DRY RUN — no actual posts will be made.\n")

    agent = MuAgent(config=cfg, dry_run=dry_run)

    if once:
        asyncio.run(_run_once(agent))
    else:
        interval = cfg.get("agent", {}).get("check_interval_hours", 4)
        variance = cfg.get("agent", {}).get("check_interval_variance", 0.5)
        try:
            asyncio.run(_run_daemon(agent, interval, variance))
        except KeyboardInterrupt:
            click.echo("\nMu goes quiet. Was anyone ever here?")


if __name__ == "__main__":
    main()
