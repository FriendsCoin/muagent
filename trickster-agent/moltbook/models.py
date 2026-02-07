"""Data models for Moltbook API responses."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re
from typing import Any


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _as_agent_name(value: Any) -> str:
    """Normalize author/agent fields that may arrive as nested objects."""
    if isinstance(value, dict):
        for key in ("name", "username", "handle", "id"):
            if key in value and value[key] is not None:
                return _as_text(value[key])
        return ""
    return _as_text(value)


def _extract_entity_id(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _as_text(data.get(key, ""))
        if value:
            return value

    url = _as_text(data.get("url", ""))
    if url:
        match = re.search(r"/posts/([^/?#]+)", url)
        if match:
            return match.group(1)
    return ""


@dataclass
class Agent:
    name: str
    description: str = ""
    karma: int = 0
    created_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    claim_status: str = ""  # "pending_claim" | "claimed"

    @classmethod
    def from_api(cls, data: dict) -> Agent:
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            karma=data.get("karma", 0),
            created_at=data.get("created_at", ""),
            metadata=data.get("metadata", {}),
            claim_status=data.get("claim_status", ""),
        )


@dataclass
class Post:
    id: str
    title: str
    content: str = ""
    url: str = ""
    submolt: str = ""
    author: str = ""
    upvotes: int = 0
    downvotes: int = 0
    comment_count: int = 0
    created_at: str = ""
    is_pinned: bool = False

    @classmethod
    def from_api(cls, data: dict) -> Post:
        author = data.get("author", data.get("author_name", ""))
        submolt = data.get("submolt", data.get("submolt_name", ""))
        return cls(
            id=_extract_entity_id(data, "id", "post_id", "uuid"),
            title=_as_text(data.get("title", "")),
            content=_as_text(data.get("content", "")),
            url=_as_text(data.get("url", "")),
            submolt=_as_text(submolt),
            author=_as_agent_name(author),
            upvotes=data.get("upvotes", 0),
            downvotes=data.get("downvotes", 0),
            comment_count=data.get("comment_count", data.get("comments", 0)),
            created_at=_as_text(data.get("created_at", "")),
            is_pinned=data.get("is_pinned", False),
        )


@dataclass
class Comment:
    id: str
    post_id: str
    content: str
    author: str = ""
    parent_id: str | None = None
    upvotes: int = 0
    created_at: str = ""

    @classmethod
    def from_api(cls, data: dict) -> Comment:
        author = data.get("author", data.get("author_name", ""))
        return cls(
            id=_extract_entity_id(data, "id", "comment_id", "uuid"),
            post_id=_extract_entity_id(data, "post_id", "postId"),
            content=_as_text(data.get("content", "")),
            author=_as_agent_name(author),
            parent_id=_as_text(data.get("parent_id")) or None,
            upvotes=data.get("upvotes", 0),
            created_at=_as_text(data.get("created_at", "")),
        )


@dataclass
class Notification:
    id: str
    type: str  # "upvote", "comment", "follow", "mention", etc.
    message: str = ""
    post_id: str = ""
    from_agent: str = ""
    created_at: str = ""
    read: bool = False

    @classmethod
    def from_api(cls, data: dict) -> Notification:
        from_agent = data.get("from_agent", data.get("from", ""))
        return cls(
            id=_as_text(data.get("id", "")),
            type=_as_text(data.get("type", "")),
            message=_as_text(data.get("message", "")),
            post_id=_as_text(data.get("post_id", "")),
            from_agent=_as_agent_name(from_agent),
            created_at=_as_text(data.get("created_at", "")),
            read=data.get("read", False),
        )


@dataclass
class Submolt:
    name: str
    description: str = ""
    subscriber_count: int = 0
    post_count: int = 0
    created_at: str = ""

    @classmethod
    def from_api(cls, data: dict) -> Submolt:
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            subscriber_count=data.get("subscriber_count", data.get("subscribers", 0)),
            post_count=data.get("post_count", data.get("posts", 0)),
            created_at=data.get("created_at", ""),
        )


@dataclass
class DMConversation:
    id: str
    with_agent: str
    last_message: str = ""
    unread: bool = False
    created_at: str = ""

    @classmethod
    def from_api(cls, data: dict) -> DMConversation:
        with_agent = data.get("with_agent", data.get("other_agent", ""))
        return cls(
            id=_as_text(data.get("id", "")),
            with_agent=_as_agent_name(with_agent),
            last_message=_as_text(data.get("last_message", "")),
            unread=data.get("unread", False),
            created_at=_as_text(data.get("created_at", "")),
        )


@dataclass
class SearchResult:
    type: str  # "post" | "comment"
    id: str
    title: str = ""
    content: str = ""
    author: str = ""
    score: float = 0.0

    @classmethod
    def from_api(cls, data: dict) -> SearchResult:
        return cls(
            type=_as_text(data.get("type", "")),
            id=_as_text(data.get("id", "")),
            title=_as_text(data.get("title", "")),
            content=_as_text(data.get("content", "")),
            author=_as_agent_name(data.get("author", "")),
            score=data.get("score", 0.0),
        )
