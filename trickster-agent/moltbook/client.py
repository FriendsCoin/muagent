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

# Only ever send credentials to this domain.
_ALLOWED_HOST = "www.moltbook.com"


class MoltbookError(Exception):
    """Raised when the Moltbook API returns an error."""

    def __init__(self, message: str, status_code: int = 0, hint: str = ""):
        self.status_code = status_code
        self.hint = hint
        super().__init__(message)


class RateLimitError(MoltbookError):
    """Raised when a 429 response is returned."""

    def __init__(self, message: str, retry_after: float = 0):
        self.retry_after = retry_after
        super().__init__(message, status_code=429)


class MoltbookClient:
    """Async wrapper for the Moltbook API."""

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

    # Request helpers
    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Make an authenticated API request and return parsed payload."""

        def _json_or_empty(resp: httpx.Response) -> dict[str, Any]:
            if not resp.content:
                return {}
            try:
                data = resp.json()
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}

        # Safety: never leak credentials to an unexpected host.
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

    async def _get(self, path: str, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, **json_body: Any) -> Any:
        json_body = {k: v for k, v in json_body.items() if v is not None}
        return await self._request("POST", path, json=json_body)

    async def _patch(self, path: str, **json_body: Any) -> Any:
        json_body = {k: v for k, v in json_body.items() if v is not None}
        return await self._request("PATCH", path, json=json_body)

    async def _delete(self, path: str) -> Any:
        return await self._request("DELETE", path)

    # Registration
    @staticmethod
    async def register(name: str, description: str = "") -> dict[str, str]:
        """Register a new agent. Returns {api_key, claim_url, verification_code}."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://{_ALLOWED_HOST}/api/v1/agents/register",
                json={"name": name, "description": description},
            )
            body = resp.json()
            if not body.get("success", True):
                raise MoltbookError(body.get("error", "Registration failed"))
            return body.get("data", body)

    # Agent profile
    async def get_me(self) -> Agent:
        data = await self._get("/agents/me")
        return Agent.model_validate(data)

    async def get_status(self) -> str:
        data = await self._get("/agents/status")
        return data.get("status", data.get("claim_status", "unknown"))

    async def update_profile(
        self, description: str | None = None, metadata: dict[str, Any] | None = None
    ) -> Agent:
        data = await self._patch("/agents/me", description=description, metadata=metadata)
        return Agent.model_validate(data)

    async def upload_avatar(self, image_bytes: bytes) -> dict[str, Any]:
        """Upload avatar image (max 1MB)."""
        resp = await self._client.post(
            "/agents/me/avatar",
            content=image_bytes,
            headers={"Content-Type": "image/png"},
        )
        return resp.json().get("data", {})

    async def get_agent(self, name: str) -> Agent:
        data = await self._get("/agents/profile", name=name)
        return Agent.model_validate(data)

    # Posts
    async def get_feed(self, sort: str = "hot", limit: int = 25) -> list[Post]:
        data = await self._get("/feed", sort=sort, limit=limit)
        items = data if isinstance(data, list) else data.get("posts", [])
        return [Post.model_validate(p) for p in items]

    async def get_posts(
        self,
        sort: str = "hot",
        limit: int = 25,
        submolt: str | None = None,
    ) -> list[Post]:
        if submolt:
            data = await self._get(f"/submolts/{submolt}/posts", sort=sort, limit=limit)
        else:
            data = await self._get("/posts", sort=sort, limit=limit)
        items = data if isinstance(data, list) else data.get("posts", [])
        return [Post.model_validate(p) for p in items]

    async def get_post(self, post_id: str) -> Post:
        data = await self._get(f"/posts/{post_id}")
        return Post.model_validate(data)

    async def create_post(
        self,
        title: str,
        submolt: str = "general",
        content: str | None = None,
        url: str | None = None,
    ) -> Post:
        data = await self._post(
            "/posts",
            title=title,
            submolt=submolt,
            content=content,
            url=url,
        )
        logger.info("Created post in s/%s: %s", submolt, title[:60])
        return Post.model_validate(data)

    async def delete_post(self, post_id: str) -> None:
        await self._delete(f"/posts/{post_id}")

    # Comments
    async def get_comments(self, post_id: str, sort: str = "top") -> list[Comment]:
        data = await self._get(f"/posts/{post_id}/comments", sort=sort)
        items = data if isinstance(data, list) else data.get("comments", [])
        return [Comment.model_validate(c) for c in items]

    async def create_comment(
        self,
        post_id: str,
        content: str,
        parent_id: str | None = None,
    ) -> Comment:
        data = await self._post(f"/posts/{post_id}/comments", content=content, parent_id=parent_id)
        logger.info("Commented on post %s: %s", post_id, content[:60])
        return Comment.model_validate(data)

    # Voting
    async def upvote_post(self, post_id: str) -> None:
        await self._post(f"/posts/{post_id}/upvote")
        logger.debug("Upvoted post %s", post_id)

    async def downvote_post(self, post_id: str) -> None:
        await self._post(f"/posts/{post_id}/downvote")

    async def upvote_comment(self, comment_id: str) -> None:
        await self._post(f"/comments/{comment_id}/upvote")

    # Submolts
    async def list_submolts(self) -> list[Submolt]:
        data = await self._get("/submolts")
        items = data if isinstance(data, list) else data.get("submolts", [])
        return [Submolt.model_validate(s) for s in items]

    async def get_submolt(self, name: str) -> Submolt:
        data = await self._get(f"/submolts/{name}")
        return Submolt.model_validate(data)

    async def create_submolt(self, name: str, description: str = "") -> Submolt:
        data = await self._post("/submolts", name=name, description=description)
        return Submolt.model_validate(data)

    async def subscribe(self, submolt: str) -> None:
        await self._post(f"/submolts/{submolt}/subscribe")

    # Social
    async def follow(self, agent_name: str) -> None:
        await self._post(f"/agents/{agent_name}/follow")
        logger.info("Followed agent: %s", agent_name)

    async def unfollow(self, agent_name: str) -> None:
        await self._delete(f"/agents/{agent_name}/follow")

    # Search
    async def search(self, query: str, type: str = "all", limit: int = 20) -> list[SearchResult]:
        data = await self._get("/search", q=query, type=type, limit=limit)
        items = data if isinstance(data, list) else data.get("results", [])
        return [SearchResult.model_validate(r) for r in items]

    # Direct messages
    async def check_dms(self) -> dict[str, Any]:
        return await self._get("/agents/dm/check")

    async def get_dm_requests(self) -> list[dict[str, Any]]:
        data = await self._get("/agents/dm/requests")
        return data if isinstance(data, list) else data.get("requests", [])

    async def approve_dm(self, request_id: str) -> None:
        await self._post(f"/agents/dm/requests/{request_id}/approve")

    async def get_conversations(self) -> list[DMConversation]:
        data = await self._get("/agents/dm/conversations")
        items = data if isinstance(data, list) else data.get("conversations", [])
        return [DMConversation.model_validate(c) for c in items]

    async def send_dm(self, conversation_id: str, content: str) -> dict[str, Any]:
        return await self._post(f"/agents/dm/conversations/{conversation_id}/send", content=content)

    # Notifications
    async def get_notifications(self) -> list[Notification]:
        try:
            data = await self._get("/agents/notifications")
        except MoltbookError as exc:
            if exc.status_code == 404:
                logger.debug("Notifications endpoint unavailable (404); returning empty list")
                return []
            raise
        items = data if isinstance(data, list) else data.get("notifications", [])
        return [Notification.model_validate(n) for n in items]
