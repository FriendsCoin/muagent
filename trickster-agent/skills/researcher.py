"""Research agent — headless web browsing, search, download, and shell.

Gives Mu the ability to autonomously browse the web, search DuckDuckGo,
download files, and run safe shell commands for research purposes.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import httpx

logger = logging.getLogger(__name__)


# ── Data classes ────────────────────────────────────────────────


@dataclass
class BrowseResult:
    url: str
    title: str
    text: str
    success: bool
    error: str = ""


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str


@dataclass
class DownloadResult:
    path: str
    size_bytes: int
    success: bool
    error: str = ""


@dataclass
class ShellResult:
    command: str
    stdout: str
    stderr: str
    returncode: int
    success: bool


# ── Safety constants ────────────────────────────────────────────

_ALLOWED_SCHEMES = {"http", "https"}

_ALLOWED_COMMANDS = frozenset({
    "ls", "cat", "grep", "head", "tail", "du", "df",
    "ping", "wc", "file", "stat", "uptime", "whoami",
})

_BLOCKED_SHELL_PATTERNS = re.compile(r"[|;&`$><]|\$\(|\|\||\&\&")

_FILENAME_RE = re.compile(r"^[a-zA-Z0-9_\-][a-zA-Z0-9_\-. ]{0,200}$")

_TEXT_TRUNCATE_LIMIT = 30_000


# ── ResearchAgent ───────────────────────────────────────────────


class ResearchAgent:
    """Headless browsing + shell toolkit for autonomous research."""

    def __init__(self, downloads_dir: Path, browse_timeout_ms: int = 30_000):
        self._downloads_dir = downloads_dir
        self._browse_timeout_ms = browse_timeout_ms
        downloads_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _validate_url(url: str) -> str | None:
        """Return None if URL is safe, or an error message."""
        try:
            parsed = urlparse(url)
        except Exception:
            return "invalid URL"
        if parsed.scheme not in _ALLOWED_SCHEMES:
            return f"blocked scheme: {parsed.scheme}"
        if not parsed.hostname:
            return "no hostname"
        return None

    async def browse(self, url: str, timeout_ms: int | None = None) -> BrowseResult:
        """Fetch a page with Playwright headless Chromium and extract text."""
        err = self._validate_url(url)
        if err:
            return BrowseResult(url=url, title="", text="", success=False, error=err)

        timeout = timeout_ms or self._browse_timeout_ms
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return BrowseResult(
                url=url, title="", text="", success=False,
                error="playwright not installed",
            )

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.goto(url, wait_until="networkidle", timeout=timeout)
                    title = await page.title() or ""

                    # Strip noisy elements before extracting text.
                    await page.evaluate("""
                        for (const sel of ['script','style','nav','footer','header']) {
                            document.querySelectorAll(sel).forEach(el => el.remove());
                        }
                    """)
                    text = await page.inner_text("body")
                    text = (text or "").strip()[:_TEXT_TRUNCATE_LIMIT]

                    return BrowseResult(url=url, title=title, text=text, success=True)
                finally:
                    await browser.close()
        except Exception as exc:
            logger.warning("browse(%s) failed: %s", url, exc)
            return BrowseResult(url=url, title="", text="", success=False, error=str(exc)[:500])

    async def search(self, query: str, max_results: int = 5) -> list[SearchHit]:
        """Search DuckDuckGo HTML and extract results via Playwright selectors."""
        encoded = quote_plus(query)
        search_url = f"https://html.duckduckgo.com/html/?q={encoded}"

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning("playwright not installed — search unavailable")
            return []

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.goto(search_url, wait_until="networkidle", timeout=self._browse_timeout_ms)

                    links = await page.query_selector_all(".result__a")
                    snippets = await page.query_selector_all(".result__snippet")

                    hits: list[SearchHit] = []
                    for i, link in enumerate(links[:max_results]):
                        href = await link.get_attribute("href") or ""
                        title = (await link.inner_text() or "").strip()
                        snippet = ""
                        if i < len(snippets):
                            snippet = (await snippets[i].inner_text() or "").strip()

                        # DuckDuckGo wraps URLs in a redirect — extract the real URL.
                        if "uddg=" in href:
                            from urllib.parse import parse_qs, urlparse as _urlparse
                            qs = parse_qs(_urlparse(href).query)
                            real = qs.get("uddg", [""])[0]
                            if real:
                                href = real

                        if href and title:
                            hits.append(SearchHit(title=title, url=href, snippet=snippet))

                    return hits
                finally:
                    await browser.close()
        except Exception as exc:
            logger.warning("search(%s) failed: %s", query, exc)
            return []

    async def download(
        self, url: str, filename: str, max_bytes: int = 50_000_000
    ) -> DownloadResult:
        """Stream-download a file with size limit."""
        err = self._validate_url(url)
        if err:
            return DownloadResult(path="", size_bytes=0, success=False, error=err)

        if not _FILENAME_RE.match(filename) or ".." in filename or "/" in filename:
            return DownloadResult(
                path="", size_bytes=0, success=False,
                error=f"invalid filename: {filename!r}",
            )

        dest = self._downloads_dir / filename
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    total = 0
                    with open(dest, "wb") as f:
                        async for chunk in resp.aiter_bytes(8192):
                            total += len(chunk)
                            if total > max_bytes:
                                f.close()
                                dest.unlink(missing_ok=True)
                                return DownloadResult(
                                    path="", size_bytes=total, success=False,
                                    error=f"exceeded max_bytes ({max_bytes})",
                                )
                            f.write(chunk)
            return DownloadResult(path=str(dest), size_bytes=total, success=True)
        except Exception as exc:
            dest.unlink(missing_ok=True)
            return DownloadResult(path="", size_bytes=0, success=False, error=str(exc)[:500])

    def shell(self, command: str, timeout: int = 15) -> ShellResult:
        """Run a safe, allowlisted shell command synchronously."""
        command = command.strip()
        if not command:
            return ShellResult(command=command, stdout="", stderr="empty command", returncode=1, success=False)

        # Block dangerous patterns.
        if _BLOCKED_SHELL_PATTERNS.search(command):
            return ShellResult(
                command=command, stdout="", stderr="blocked: pipes/chains/redirects not allowed",
                returncode=1, success=False,
            )

        try:
            parts = shlex.split(command)
        except ValueError as exc:
            return ShellResult(command=command, stdout="", stderr=f"parse error: {exc}", returncode=1, success=False)

        if not parts:
            return ShellResult(command=command, stdout="", stderr="empty command", returncode=1, success=False)

        binary = Path(parts[0]).name  # handle paths like /usr/bin/ls
        if binary not in _ALLOWED_COMMANDS:
            return ShellResult(
                command=command, stdout="",
                stderr=f"blocked: '{binary}' not in allowlist",
                returncode=1, success=False,
            )

        try:
            import subprocess
            proc = subprocess.run(
                parts,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return ShellResult(
                command=command,
                stdout=proc.stdout[:10_000],
                stderr=proc.stderr[:5_000],
                returncode=proc.returncode,
                success=proc.returncode == 0,
            )
        except subprocess.TimeoutExpired:
            return ShellResult(command=command, stdout="", stderr="timeout", returncode=1, success=False)
        except Exception as exc:
            return ShellResult(command=command, stdout="", stderr=str(exc)[:500], returncode=1, success=False)
