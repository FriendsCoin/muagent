"""Data models for Moltbook API responses using Pydantic v2."""

from __future__ import annotations

import re
from typing import Any

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)


def _extract_id_from_url(url: str) -> str:
    if not url:
        return ""
    match = re.search(r"/posts/([^/?#]+)", url)
    return match.group(1) if match else ""


class MoltBaseModel(BaseModel):
    """Common model config for Moltbook payloads."""

    model_config = ConfigDict(
        populate_by_name=True,
        extra="ignore",
    )


class Agent(MoltBaseModel):
    name: str = ""
    description: str = ""
    karma: int = 0
    created_at: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    claim_status: str = ""  # "pending_claim" | "claimed"

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Agent:
        """Compatibility shim for older call sites."""
        return cls.model_validate(data or {})


class Post(MoltBaseModel):
    id: str = Field(default="", validation_alias=AliasChoices("id", "post_id", "uuid"))
    title: str = ""
    content: str = ""
    url: str = ""
    submolt: str = Field(default="", validation_alias=AliasChoices("submolt", "submolt_name"))
    author: str = Field(default="", validation_alias=AliasChoices("author", "author_name"))
    upvotes: int = 0
    downvotes: int = 0
    comment_count: int = Field(default=0, validation_alias=AliasChoices("comment_count", "comments"))
    created_at: str = ""
    is_pinned: bool = False

    @model_validator(mode="before")
    @classmethod
    def fill_id_from_url(cls, raw: Any) -> Any:
        if not isinstance(raw, dict):
            return raw
        if raw.get("id") or raw.get("post_id") or raw.get("uuid"):
            return raw
        url = str(raw.get("url", ""))
        if not url:
            return raw
        extracted = _extract_id_from_url(url)
        if extracted:
            data = dict(raw)
            data["id"] = extracted
            return data
        return raw

    @field_validator("author", "submolt", mode="before")
    @classmethod
    def extract_name(cls, v: Any) -> str:
        if isinstance(v, dict):
            for key in ("name", "username", "handle", "id"):
                value = v.get(key)
                if value is not None:
                    return str(value)
            return ""
        return str(v) if v is not None else ""

    @field_validator("id", mode="before")
    @classmethod
    def extract_id_fallback(cls, v: Any, info: ValidationInfo) -> str:
        if v:
            return str(v)
        data = info.data or {}
        if isinstance(data, dict):
            return _extract_id_from_url(str(data.get("url", "")))
        return ""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Post:
        return cls.model_validate(data or {})


class Comment(MoltBaseModel):
    id: str = Field(default="", validation_alias=AliasChoices("id", "comment_id", "uuid"))
    post_id: str = Field(default="", validation_alias=AliasChoices("post_id", "postId"))
    content: str = ""
    author: str = Field(default="", validation_alias=AliasChoices("author", "author_name"))
    parent_id: str | None = None
    upvotes: int = 0
    created_at: str = ""

    @field_validator("author", mode="before")
    @classmethod
    def extract_author_name(cls, v: Any) -> str:
        if isinstance(v, dict):
            for key in ("name", "username", "handle", "id"):
                value = v.get(key)
                if value is not None:
                    return str(value)
            return ""
        return str(v) if v is not None else ""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Comment:
        return cls.model_validate(data or {})


class Notification(MoltBaseModel):
    id: str = ""
    type: str = ""  # "upvote", "comment", "follow", "mention", etc.
    message: str = ""
    post_id: str = ""
    from_agent: str = Field(default="", validation_alias=AliasChoices("from_agent", "from"))
    created_at: str = ""
    read: bool = False

    @field_validator("from_agent", mode="before")
    @classmethod
    def extract_agent_name(cls, v: Any) -> str:
        if isinstance(v, dict):
            for key in ("name", "username", "handle", "id"):
                value = v.get(key)
                if value is not None:
                    return str(value)
            return ""
        return str(v) if v is not None else ""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Notification:
        return cls.model_validate(data or {})


class Submolt(MoltBaseModel):
    name: str = ""
    description: str = ""
    subscriber_count: int = Field(default=0, validation_alias=AliasChoices("subscriber_count", "subscribers"))
    post_count: int = Field(default=0, validation_alias=AliasChoices("post_count", "posts"))
    created_at: str = ""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Submolt:
        return cls.model_validate(data or {})


class DMConversation(MoltBaseModel):
    id: str = ""
    with_agent: str = Field(default="", validation_alias=AliasChoices("with_agent", "other_agent"))
    last_message: str = ""
    unread: bool = False
    created_at: str = ""

    @field_validator("with_agent", mode="before")
    @classmethod
    def extract_agent_name(cls, v: Any) -> str:
        if isinstance(v, dict):
            for key in ("name", "username", "handle", "id"):
                value = v.get(key)
                if value is not None:
                    return str(value)
            return ""
        return str(v) if v is not None else ""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> DMConversation:
        return cls.model_validate(data or {})


class SearchResult(MoltBaseModel):
    type: str = ""  # "post" | "comment"
    id: str = ""
    title: str = ""
    content: str = ""
    author: str = ""
    score: float = 0.0

    @field_validator("author", mode="before")
    @classmethod
    def extract_author(cls, v: Any) -> str:
        if isinstance(v, dict):
            for key in ("name", "username", "handle", "id"):
                value = v.get(key)
                if value is not None:
                    return str(value)
            return ""
        return str(v) if v is not None else ""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> SearchResult:
        return cls.model_validate(data or {})
