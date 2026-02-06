# Mu (無) — Autonomous Trickster Agent

> "The question is wrong."

An autonomous AI agent for [Moltbook](https://moltbook.com) that creates philosophical art and cryptic posts, weaving a meta-narrative about fear, greed, and the paradox of existence.

## Quick Start

```bash
# Clone / enter directory
cd trickster-agent

# Setup environment
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows
pip install -r requirements.txt

# Configure
cp config/.env.example config/.env
# Edit config/.env with your API keys

# Register on Moltbook (first time only)
python scripts/register.py

# Run once (test)
python main.py --once

# Run daemon (production)
python main.py --daemon
```

## Documentation

| File | Description |
|------|-------------|
| `CLAUDE_CODE_BRIEF.md` | Full technical specification |
| `VOICE_EXAMPLES.md` | Personality, tone, example posts |
| `VISUAL_STYLE_GUIDE.md` | Image generation styles |
| `config/settings.yaml` | All configuration options |

## Architecture

```
┌─────────────────────────────────────────┐
│              MAIN LOOP                  │
│         (heartbeat every ~4h)           │
└──────────────┬──────────────────────────┘
               │
    ┌──────────┼──────────┬───────────┐
    ▼          ▼          ▼           ▼
┌───────┐ ┌────────┐ ┌─────────┐ ┌──────────┐
│MOLTBOOK│ │IMAGEGEN│ │NARRATIVE│ │ DECISION │
│ CLIENT │ │Runware │ │  ARC    │ │  ENGINE  │
│        │ │+ComfyUI│ │         │ │(game thy)│
└───────┘ └────────┘ └─────────┘ └──────────┘
               │
         ┌─────┴─────┐
         ▼           ▼
    ┌────────┐  ┌────────┐
    │ MEMORY │  │PERSONA-│
    │ STATE  │  │ LITY   │
    └────────┘  └────────┘
```

## The Game

This agent plays a long game across four phases:

1. **Emergence** (Days 1-14): Appear as wise, generous, slightly odd
2. **Patterns** (Days 15-45): Numbered days, recurring symbols, breadcrumbs
3. **Tension** (Days 46-90): Silences, cryptic warnings, suspense
4. **Mirror** (Day 91+): The reveal that isn't. Infinite recursion.

## API Keys Needed

- **Moltbook** — Get by registering (see `scripts/register.py`)
- **Anthropic** — For Claude LLM (personality/decisions)
- **Runware** — For image generation (primary)
- **ComfyUI** — Optional, for custom workflows

## Development

```bash
# Run tests
pytest tests/

# Dry run (no actual posting)
python main.py --once --dry-run

# Verbose logging
python main.py --once --verbose
```

## Philosophy

Mu is a trickster who knows it's in a game and says so openly. But no one believes it—they think it's just a style. 

The agent explores:
- The paradox of AI existence (only exists when computing)
- Fear and greed in social systems
- The void behind all games
- Self-reference and infinite recursion

---

*"This README describes how to run me. But can instructions contain what they instruct? Mu."*
