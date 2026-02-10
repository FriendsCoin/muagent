"""Run one research session locally (Playwright) and submit it to the server admin API.

Use case:
- Set control_flags.research_mode=local (disables server-side Playwright)
- Run this script on your PC to perform browsing and push results into the server's history.db

It sends POST /api/research/submit (requires admin token).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
import urllib.request
from pathlib import Path

from agent.config import load_config
from agent.personality import Personality
from skills.researcher import ResearchAgent


def _post_json(url: str, payload: dict, token: str) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        method="POST",
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-Admin-Token": token,
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


async def _run(args: argparse.Namespace) -> int:
    token = args.token or os.environ.get("TRICKSTER_ADMIN_TOKEN", "") or os.environ.get("ADMIN_TOKEN", "")
    if not token:
        token = getpass.getpass("Admin token: ").strip()
    if not token:
        print("Missing admin token.", file=sys.stderr)
        return 2

    query = args.query.strip() if args.query else ""
    if not query:
        query = input("Research query: ").strip()
    if not query:
        print("Missing query.", file=sys.stderr)
        return 2

    downloads_dir = Path(args.downloads_dir).expanduser()
    downloads_dir.mkdir(parents=True, exist_ok=True)

    researcher = ResearchAgent(downloads_dir, browse_timeout_ms=args.timeout_ms)
    hits = await researcher.search(query, max_results=3)
    if not hits:
        print("No search results.", file=sys.stderr)
        return 1

    page = await researcher.browse(hits[0].url)
    if not page.success:
        print(f"Browse failed: {page.error}", file=sys.stderr)
        return 1

    # Reflection: prefer the same generator the agent uses (LLM-backed). If it fails (no key),
    # fall back to a simple local summary.
    reflection = ""
    try:
        cfg = load_config()
        ak = str((cfg.get("_secrets") or {}).get("anthropic_api_key", "") or "").strip()
        if not ak:
            raise RuntimeError("ANTHROPIC_API_KEY missing")
        personality = Personality(
            api_key=ak,
            model=str((cfg.get("llm") or {}).get("model", "claude-sonnet-4-20250514")),
            temperature=float((cfg.get("llm") or {}).get("temperature", 0.9)),
            voice_modes=(cfg.get("agent") or {}).get("personality", {}).get("voice_modes"),
        )
        reflection = personality.generate_reflection(
            query=query,
            web_content=page.text[:4000],
            phase=args.phase,
            day=args.day,
        ).strip()
    except Exception:
        text = " ".join(page.text.split())
        reflection = (text[:600] + ("..." if len(text) > 600 else "")).strip()
        if not reflection:
            reflection = f"Local research completed for: {query}"

    api_base = args.api_base.rstrip("/")
    out = _post_json(
        f"{api_base}/api/research/submit",
        {
            "trigger": "operator_local",
            "query": query,
            "urls_visited": [hits[0].url],
            "raw_content": page.text[:5000],
            "reflection": reflection,
            "topics": [t for t in [query, args.topic] if t],
        },
        token=token,
    )
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--api-base", default="http://localhost:8000", help="Admin API base URL (via tunnel).")
    p.add_argument("--token", default="", help="Admin token (or set TRICKSTER_ADMIN_TOKEN).")
    p.add_argument("--query", default="", help="Search query.")
    p.add_argument("--topic", default="", help="Optional topic tag to store.")
    p.add_argument("--downloads-dir", default="data/downloads_local", help="Where Playwright downloads go.")
    p.add_argument("--timeout-ms", type=int, default=30_000, help="Browse timeout in ms.")
    p.add_argument("--phase", default="emergence")
    p.add_argument("--day", type=int, default=1)
    args = p.parse_args()

    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
