"""Objkt mint adapter.

This module intentionally uses a webhook contract instead of wallet key handling.
Real on-chain signing should live in a dedicated minter worker/service.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class MintResult:
    ok: bool
    message: str
    tx_hash: str = ""
    token_id: str = ""
    token_url: str = ""
    raw_response: dict[str, Any] | None = None


class ObjktWebhookMinter:
    """Mint NFT drafts by calling an external mint webhook."""

    def __init__(self, cfg: dict):
        root = cfg if isinstance(cfg, dict) else {}
        nft_cfg = root.get("nft", {}) if isinstance(root, dict) else {}
        objkt_cfg = nft_cfg.get("objkt", {}) if isinstance(nft_cfg, dict) else {}
        self._secrets = root.get("_secrets", {}) if isinstance(root, dict) else {}

        self._endpoint = str(objkt_cfg.get("mint_webhook_url", "")).strip()
        self._timeout = float(objkt_cfg.get("timeout_seconds", 20))
        self._collection_id = str(objkt_cfg.get("collection_id", "")).strip()
        self._creator_address = str(objkt_cfg.get("creator_address", "")).strip()

    @property
    def endpoint(self) -> str:
        return self._endpoint

    def is_configured(self) -> bool:
        return bool(self._endpoint)

    def mint(self, draft: dict[str, Any]) -> MintResult:
        if not self._endpoint:
            return MintResult(
                ok=False,
                message="mint_webhook_url is not configured",
            )

        token = str(self._secrets.get("objkt_webhook_token", "")).strip()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        payload = {
            "title": str(draft.get("title", "")).strip(),
            "description": str(draft.get("description", "")).strip(),
            "image_url": str(draft.get("image_url", "")).strip(),
            "tags": draft.get("tags", []),
            "edition_size": int(draft.get("edition_size", 1) or 1),
            "royalty_bps": int(draft.get("royalty_bps", 500) or 500),
            "collection_id": str(draft.get("collection_id", "")).strip() or self._collection_id,
            "creator_address": str(draft.get("creator_address", "")).strip() or self._creator_address,
            "external_ref": str(draft.get("id", "")).strip(),
            "metadata": draft.get("metadata", {}),
        }

        try:
            response = httpx.post(
                self._endpoint,
                headers=headers,
                json=payload,
                timeout=self._timeout,
            )
            response.raise_for_status()
            body = response.json() if response.content else {}
        except Exception as exc:
            return MintResult(ok=False, message=f"mint webhook request failed: {exc}")

        if not isinstance(body, dict):
            return MintResult(ok=False, message="mint webhook response is not a JSON object")

        ok = bool(body.get("ok", False))
        tx_hash = str(body.get("tx_hash", "")).strip()
        token_id = str(body.get("token_id", "")).strip()
        token_url = str(body.get("token_url", "")).strip()
        message = str(body.get("message", "")).strip() or ("minted" if ok else "mint failed")
        return MintResult(
            ok=ok,
            message=message,
            tx_hash=tx_hash,
            token_id=token_id,
            token_url=token_url,
            raw_response=body,
        )
