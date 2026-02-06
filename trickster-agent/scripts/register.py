"""Register Mu on Moltbook.

Usage:
    python scripts/register.py
    python scripts/register.py --name "MuAgent" --description "Mu. The question is wrong."

After registration, you'll need to verify ownership via tweet.
Save the returned API key to config/.env.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from moltbook.client import MoltbookClient, MoltbookError


@click.command()
@click.option(
    "--name",
    default="MuAgent",
    help="Agent name (letters/numbers/underscore; avoid very short names)",
)
@click.option(
    "--description",
    default="Mu. The question is wrong.",
    help="Agent description",
)
def register(name: str, description: str) -> None:
    """Register a new agent on Moltbook."""

    click.echo(f'\nRegistering agent "{name}" on Moltbook...\n')

    try:
        result = asyncio.run(MoltbookClient.register(name, description))
    except MoltbookError as e:
        click.echo(f"Registration failed: {e}", err=True)
        sys.exit(1)

    agent = result.get("agent", {}) if isinstance(result, dict) else {}
    api_key = (
        result.get("api_key", "")
        or agent.get("api_key", "")
    )
    claim_url = (
        result.get("claim_url", "")
        or agent.get("claim_url", "")
    )
    verification_code = (
        result.get("verification_code", "")
        or agent.get("verification_code", "")
    )

    click.echo("Registration successful!\n")
    click.echo(f"  API Key: {api_key}")
    click.echo(f"  Claim URL: {claim_url}")
    click.echo(f"  Verification Code: {verification_code}")
    if not api_key:
        click.echo(
            "Warning: API key not found in response. Moltbook may have changed response shape.",
            err=True,
        )
        click.echo(f"Raw response: {result}", err=True)
    click.echo()
    click.echo("Next steps:")
    click.echo("  1. Save the API key to config/.env:")
    click.echo(f'     MOLTBOOK_API_KEY={api_key}')
    click.echo(f"  2. Verify ownership at: {claim_url}")
    click.echo(f"  3. Use verification code: {verification_code}")
    click.echo()
    click.echo("Then run: python main.py --once --dry-run")


if __name__ == "__main__":
    register()
