"""Configuration loading from settings.yaml and .env."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv


def load_config(
    config_dir: str | Path | None = None,
) -> dict:
    """Load settings.yaml and .env, return merged config dict."""
    if config_dir is None:
        config_dir = Path(__file__).resolve().parent.parent / "config"
    config_dir = Path(config_dir)

    # Load .env (silently skip if missing)
    env_path = config_dir / ".env"
    load_dotenv(env_path)

    # Load settings.yaml
    settings_path = config_dir / "settings.yaml"
    if not settings_path.exists():
        raise FileNotFoundError(f"Config not found: {settings_path}")

    with open(settings_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # Inject env vars into config for convenience
    cfg["_secrets"] = {
        "moltbook_api_key": os.getenv("MOLTBOOK_API_KEY", ""),
        "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
        "runware_api_key": os.getenv("RUNWARE_API_KEY", ""),
        "comfyui_api_key": os.getenv("COMFYUI_API_KEY", ""),
    }

    return cfg
