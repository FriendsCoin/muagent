"""Quick smoke test for ResearchAgent."""

import asyncio
import os
import shutil
from pathlib import Path

# WSL1 workaround — must be set before playwright import
if not os.environ.get("PLAYWRIGHT_NODEJS_PATH"):
    node = shutil.which("node")
    if node:
        os.environ["PLAYWRIGHT_NODEJS_PATH"] = node

from skills.researcher import ResearchAgent

passed = 0
failed = 0


def check(name: str, ok: bool, detail: str = ""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


async def main():
    agent = ResearchAgent(Path("data/downloads_test"))

    # ── URL validation (no Chromium needed) ──────────────────

    print("\n--- URL validation ---")
    r = await agent.browse("file:///etc/passwd")
    check("block file:// scheme", not r.success and "blocked" in r.error)

    r = await agent.browse("javascript:alert(1)")
    check("block javascript: scheme", not r.success and "blocked" in r.error)

    r = await agent.browse("data:text/html,<h1>hi</h1>")
    check("block data: scheme", not r.success and "blocked" in r.error)

    r = await agent.browse("")
    check("block empty URL", not r.success)

    # ── Browse (graceful fail without system libs) ───────────

    print("\n--- browse (may fail on WSL without deps) ---")
    r = await agent.browse("https://example.com")
    if r.success:
        check("browse example.com", "Example" in r.title, f"title={r.title!r}")
    else:
        print(f"  SKIP  browse example.com (expected on WSL1 without libs): {r.error[:80]}")

    # ── Download ─────────────────────────────────────────────

    print("\n--- download ---")
    dl = await agent.download("https://example.com", "test_example.html")
    check("download example.com", dl.success and dl.size_bytes > 0, f"err={dl.error}")

    dl = await agent.download("https://example.com", "../../etc/passwd")
    check("block path traversal filename", not dl.success and "invalid" in dl.error.lower())

    dl = await agent.download("https://example.com", "a/b/c.txt")
    check("block slash in filename", not dl.success)

    dl = await agent.download("file:///etc/passwd", "test.txt")
    check("block file:// in download", not dl.success and "blocked" in dl.error)

    dl = await agent.download("https://example.com", "ok_file.html", max_bytes=10)
    check("enforce max_bytes limit", not dl.success and "max_bytes" in dl.error.lower())

    # ── Shell ────────────────────────────────────────────────

    print("\n--- shell ---")
    sh = agent.shell("whoami")
    check("whoami (allowed)", sh.success and sh.returncode == 0)

    sh = agent.shell("uptime")
    check("uptime (allowed)", sh.success)

    sh = agent.shell("ls data/downloads_test")
    check("ls downloads dir", sh.success)

    sh = agent.shell("rm -rf /")
    check("block rm (not in allowlist)", not sh.success and "allowlist" in sh.stderr.lower())

    sh = agent.shell("sudo reboot")
    check("block sudo (not in allowlist)", not sh.success)

    sh = agent.shell("python3 -c 'import os; os.system(\"rm -rf /\")'")
    check("block python3 (not in allowlist)", not sh.success)

    sh = agent.shell("ls | grep x")
    check("block pipe", not sh.success and "pipe" in sh.stderr.lower())

    sh = agent.shell("ls && whoami")
    check("block && chain", not sh.success)

    sh = agent.shell("ls > /tmp/out")
    check("block redirect", not sh.success)

    sh = agent.shell("cat $(whoami)")
    check("block $() subshell", not sh.success)

    sh = agent.shell("cat `whoami`")
    check("block backtick subshell", not sh.success)

    sh = agent.shell("")
    check("block empty command", not sh.success)

    # ── Cleanup ──────────────────────────────────────────────

    shutil.rmtree("data/downloads_test", ignore_errors=True)

    print(f"\n{'='*40}")
    print(f"  {passed} passed, {failed} failed")
    if failed:
        print("  Some tests FAILED")
        raise SystemExit(1)
    else:
        print("  All tests passed")


if __name__ == "__main__":
    asyncio.run(main())
