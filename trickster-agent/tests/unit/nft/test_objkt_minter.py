from __future__ import annotations

from nft import ObjktWebhookMinter


def test_mint_returns_not_configured_when_webhook_missing():
    minter = ObjktWebhookMinter({"nft": {"objkt": {}}, "_secrets": {}})
    res = minter.mint(
        {
            "id": "d1",
            "title": "Mu Artifact",
            "description": "desc",
            "image_url": "https://example.com/img.png",
            "tags": ["mu"],
        }
    )
    assert res.ok is False
    assert "not configured" in res.message


def test_mint_success_parses_response(monkeypatch):
    class _Resp:
        content = b'{"ok": true}'

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {
                "ok": True,
                "message": "minted",
                "tx_hash": "ooABC",
                "token_id": "123",
                "token_url": "https://objkt.com/tokens/123",
            }

    def _fake_post(*args, **kwargs):
        return _Resp()

    monkeypatch.setattr("nft.objkt_minter.httpx.post", _fake_post)
    minter = ObjktWebhookMinter(
        {
            "nft": {"objkt": {"mint_webhook_url": "https://mint.local/hook", "timeout_seconds": 5}},
            "_secrets": {"objkt_webhook_token": "tok"},
        }
    )
    res = minter.mint(
        {
            "id": "d1",
            "title": "Mu Artifact",
            "description": "desc",
            "image_url": "https://example.com/img.png",
            "tags": ["mu"],
        }
    )
    assert res.ok is True
    assert res.tx_hash == "ooABC"
    assert res.token_id == "123"

