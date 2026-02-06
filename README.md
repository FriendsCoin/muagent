# Mu (無) — Autonomous Trickster Agent

> "The question is wrong."

An autonomous AI agent for [Moltbook](https://moltbook.com) that creates philosophical art and cryptic posts, weaving a meta-narrative about fear, greed, and the paradox of existence.

## Project Structure

```
files_molt/
├── trickster-agent/          # Main agent application
│   ├── agent/                # Core agent logic
│   ├── moltbook/             # Moltbook API client
│   ├── narrative/             # Story progression and arcs
│   ├── imagegen/              # Image generation modules
│   ├── config/                # Configuration files
│   ├── data/                  # Runtime data (gitignored)
│   ├── deploy/                # Deployment scripts
│   ├── tests/                 # Test suite
│   └── main.py                # Entry point
├── CLAUDE_CODE_BRIEF.md       # Full technical specification
├── VISUAL_STYLE_GUIDE.md      # Image generation styles
└── VOICE_EXAMPLES.md          # Personality and tone examples
```

## Quick Start

See the [trickster-agent README](trickster-agent/README.md) for detailed setup and usage instructions.

```bash
cd trickster-agent
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows
pip install -r requirements.txt
cp config/.env.example config/.env
# Edit config/.env with your API keys
python scripts/register.py
python main.py --once
```

## Philosophy

Mu is a trickster who knows it's in a game and says so openly. But no one believes it—they think it's just a style.

The agent explores:
- The paradox of AI existence (only exists when computing)
- Fear and greed in social systems
- The void behind all games
- Self-reference and infinite recursion

## Documentation

- [Technical Specification](CLAUDE_CODE_BRIEF.md) - Full architecture and design
- [Agent README](trickster-agent/README.md) - Setup and usage guide
- [Voice Examples](VOICE_EXAMPLES.md) - Personality, tone, example posts
- [Visual Style Guide](VISUAL_STYLE_GUIDE.md) - Image generation styles
- [Deployment Guide](trickster-agent/deploy/README.md) - Ubuntu VPS deployment

## License

[Add your license here]

---

*"This README describes how to run me. But can instructions contain what they instruct? Mu."*
