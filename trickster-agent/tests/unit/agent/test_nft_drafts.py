from __future__ import annotations

import asyncio
from pathlib import Path

from agent.memory import HistoryDB


def test_nft_draft_roundtrip(tmp_path: Path):
    db_path = tmp_path / "history.db"

    async def _run() -> None:
        async with HistoryDB(db_path) as db:
            draft_id = await db.create_nft_draft(
                source_post_id="p-row-1",
                source_moltbook_id="mb-1",
                image_url="https://example.com/image.png",
                title="Mu Artifact",
                description="desc",
                tags=["mu", "ai-art"],
                edition_size=1,
                royalty_bps=500,
                metadata={"phase": "emergence"},
            )

            assert await db.has_nft_draft_for_source("mb-1") is True

            draft = await db.get_nft_draft(draft_id)
            assert draft is not None
            assert draft["status"] == "draft"

            await db.set_nft_draft_status(
                draft_id,
                status="minted",
                tx_hash="ooTX",
                token_id="42",
                mint_url="https://objkt.com/tokens/42",
            )
            updated = await db.get_nft_draft(draft_id)
            assert updated is not None
            assert updated["status"] == "minted"
            assert updated["mint_tx_hash"] == "ooTX"

            rows = await db.get_recent_nft_drafts(limit=5)
            assert len(rows) == 1
            assert rows[0]["id"] == draft_id

    asyncio.run(_run())

