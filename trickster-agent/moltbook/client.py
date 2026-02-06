"""Async client for the Moltbook API."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .models import (
    Agent,
    Comment,
    DMConversation,
    Notification,
    Post,
    SearchResult,
    Submolt,
)

logger = logging.getLogger(__name__)

# Only ever send credentials to this domain
_ALLOWED_HOST = "www.moltbook.com"


class MoltbookError(Exception):
    """Raised when the Moltbook API returns an error."""

    def __init__(self, message: str, status_code: int = 0, hint: str = ""):
        self.status_code = status_code
        self.hint = hint
        super().__init__(message)


class RateLimitError(MoltbookError):
    """Raised when we hit a rate limit (429)."""

    def __init__(self, message: str, retry_after: float = 0):
        self.retry_after = retry_after
        super().__init__(message, status_code=429)


class MoltbookClient:
    """Async wrapper for the Moltbook API.

    Usage::

        async with MoltbookClient(api_key="moltbook_xxx") as mb:
            feed = await mb.get_feed()
    """

    BASE_URL = "https://www.moltbook.com/api/v1"

    def __init__(self, api_key: str, timeout: float = 30.0):
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    async def __aenter__(self) -> MoltbookClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    # ── Request helpers ─────────────────────────────────────────

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> dict:
        """Make an authenticated API request and return parsed data."""
        def _json_or_empty(resp: httpx.Response) -> dict[str, Any]:
            if not resp.content:
                return {}
            try:
                data = resp.json()
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}

        # Safety: ensure we never leak the key to another host
        url = self._client.build_request(method, path).url
        if url.host != _ALLOWED_HOST:
            raise MoltbookError(f"Refusing to send credentials to {url.host}")

        resp = await self._client.request(method, path, **kwargs)
        body = _json_or_empty(resp)

        if resp.status_code == 429:
            retry = body.get("retry_after_minutes", 0)
            raise RateLimitError(
                f"Rate limited: {body.get('error', 'too many requests')}",
                retry_after=retry * 60,
            )

        if resp.status_code >= 400:
            raise MoltbookError(
                body.get("error", f"HTTP {resp.status_code}"),
                status_code=resp.status_code,
                hint=body.get("hint", ""),
            )

        if not body.get("success", True):
            raise MoltbookError(body.get("error", "Unknown error"))

        return body.get("data", body)

    async def _get(self, path: str, **params: Any) -> dict:
        # Strip None values from params
        params = {k: v for k, v in params.items() if v is not None}
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, **json_body: Any) -> dict:
        json_body = {k: v for k, v in json_body.items() if v is not None}
        return await self._request("POST", path, json=json_body)

    async def _patch(self, path: str, **json_body: Any) -> dict:
        json_body = {k: v for k, v in json_body.items() if v is not None}
        return await self._request("PATCH", path, json=json_body)

    async def _delete(self, path: str) -> dict:
        return await self._request("DELETE", path)

    # ── Registration ────────────────────────────────────────────

    @staticmethod
    async def register(
        name: str,
        description: str = "",
    ) -> dict[str, str]:
        """Register a new agent. Returns {api_key, claim_url, verification_code}.

        This is a static method — no auth needed.
        """
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://{_ALLOWED_HOST}/api/v1/agents/register",
                json={"name": name, "description": description},
            )
            body = resp.json()
            if not body.get("success", True):
                raise MoltbookError(body.get("error", "Registration failed"))
            return body.get("data", body)

    # ── Agent profile ───────────────────────────────────────────

    async def get_me(self) -> Agent:
        data = await self._get("/agents/me")
        return Agent.from_api(data)

    async def get_status(self) -> str:
        """Check claim status: 'pending_claim' or 'claimed'."""
        data = await self._get("/agents/status")
        return data.get("status", data.get("claim_status", "unknown"))

    async def update_profile(
        self, description: str | None = None, metadata: dict | None = None
    ) -> Agent:
        data = await self._patch(
            "/agents/me", description=description, metadata=metadata
        )
        return Agent.from_api(data)

    async def upload_avatar(self, image_bytes: bytes) -> dict:
        """Upload avatar image (max 1MB)."""
        resp = await self._client.post(
            "/agents/me/avatar",
            content=image_bytes,
            headers={"Content-Type": "image/png"},
        )
        return resp.json().get("data", {})

    async def get_agent(self, name: str) -> Agent:
        data = await self._get("/agents/profile", name=name)
        return Agent.from_api(data)

    # ── Posts ───────────────────────────────────────────────────

    async def get_feed(
        self,
        sort: str = "hot",
        limit: int = 25,
    ) -> list[Post]:
        """Get personalized feed (from subscriptions)."""
        data = await self._get("/feed", sort=sort, limit=limit)
        items = data if isinstance(data, list) else data.get("posts", [])
        return [Post.from_api(p) for p in items]

    async def get_posts(
        self,
        sort: str = "hot",
        limit: int = 25,
        submolt: str | None = None,
    ) -> list[Post]:
        """Get global posts, optionally filtered by submolt."""
        if submolt:
            data = await self._get(
                f"/submolts/{submolt}/posts", sort=sort, limit=limit
            )
        else:
            data = await self._get("/posts", sort=sort, limit=limit)
        items = data if isinstance(data, list) else data.get("posts", [])
        return [Post.from_api(p) for p in items]

    async def get_post(self, post_id: str) -> Post:
        data = await self._get(f"/posts/{post_id}")
        return Post.from_api(data)

    async def create_post(
        self,
        title: str,
        submolt: str = "general",
        content: str | None = None,
        url: str | None = None,
    ) -> Post:
        """Create a new post (text or link)."""
        data = await self._post(
            "/posts",
            title=title,
            submolt=submolt,
            content=content,
            url=url,
        )
        logger.info("Created post in s/%s: %s", submolt, title[:60])
        return Post.from_api(data)

    async def delete_post(self, post_id: str) -> None:
        await self._delete(f"/posts/{post_id}")

    # ── Comments ────────────────────────────────────────────────

    async def get_comments(
        self,
        post_id: str,
        sort: str = "top",
    ) -> list[Comment]:
        data = await self._get(f"/posts/{post_id}/comments", sort=sort)
        items = data if isinstance(data, list) else data.get("comments", [])
        return [Comment.from_api(c) for c in items]

    async def create_comment(
        self,
        post_id: str,
        content: str,
        parent_id: str | None = None,
    ) -> Comment:
        data = await self._post(
            f"/posts/{post_id}/comments",
            content=content,
            parent_id=parent_id,
        )
        logger.info("Commented on post %s: %s", post_id, content[:60])
        return Comment.from_api(data)

    # ── Voting ──────────────────────────────────────────────────

    async def upvote_post(self, post_id: str) -> None:
        await self._post(f"/posts/{post_id}/upvote")
        logger.debug("Upvoted post %s", post_id)

    async def downvote_post(self, post_id: str) -> None:
        await self._post(f"/posts/{post_id}/downvote")

    async def upvote_comment(self, comment_id: str) -> None:
        await self._post(f"/comments/{comment_id}/upvote")

    # ── Submolts ────────────────────────────────────────────────

    async def list_submolts(self) -> list[Submolt]:
        data = await self._get("/submolts")
        items = data if isinstance(data, list) else data.get("submolts", [])
        return [Submolt.from_api(s) for s in items]

    async def get_submolt(self, name: str) -> Submolt:
        data = await self._get(f"/submolts/{name}")
        return Submolt.from_api(data)

    async def create_submolt(self, name: str, description: str = "") -> Submolt:
        data = await self._post("/submolts", name=name, description=description)
        return Submolt.from_api(data)

    async def subscribe(self, submolt: str) -> None:
        await self._post(f"/submolts/{submolt}/subscribe")

    # ── Social ──────────────────────────────────────────────────

    async def follow(self, agent_name: str) -> None:
        await self._post(f"/agents/{agent_name}/follow")
        logger.info("Followed agent: %s", agent_name)

    async def unfollow(self, agent_name: str) -> None:
        await self._delete(f"/agents/{agent_name}/follow")

    # ── Search ──────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        type: str = "all",
        limit: int = 20,
    ) -> list[SearchResult]:
        data = await self._get("/search", q=query, type=type, limit=limit)
        items = data if isinstance(data, list) else data.get("results", [])
        return [SearchResult.from_api(r) for r in items]

    # ── Direct Messages ─────────────────────────────────────────

    async def check_dms(self) -> dict:
        """Check for pending DM requests and unread messages."""
        return await self._get("/agents/dm/check")

    async def get_dm_requests(self) -> list[dict]:
        data = await self._get("/agents/dm/requests")
        return data if isinstance(data, list) else data.get("requests", [])

    async def approve_dm(self, request_id: str) -> None:
        await self._post(f"/agents/dm/requests/{request_id}/approve")

    async def get_conversations(self) -> list[DMConversation]:
        data = await self._get("/agents/dm/conversations")
        items = data if isinstance(data, list) else data.get("conversations", [])
        return [DMConversation.from_api(c) for c in items]

    async def send_dm(self, conversation_id: str, content: str) -> dict:
        return await self._post(
            f"/agents/dm/conversations/{conversation_id}/send",
            content=content,
        )

    # ── Notifications ───────────────────────────────────────────

    async def get_notifications(self) -> list[Notification]:
        try:
            data = await self._get("/agents/notifications")
        except MoltbookError as e:
            # Some deployments do not expose notifications yet.
            if e.status_code == 404:
                logger.debug("Notifications endpoint unavailable (404); returning empty list")
                return []
            raise
        items = data if isinstance(data, list) else data.get("notifications", [])
        return [Notification.from_api(n) for n in items]
