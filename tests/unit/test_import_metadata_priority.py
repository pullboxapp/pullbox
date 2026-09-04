"""Catalog hydration gets a bounded head start while ComicInfo still advances."""

import asyncio

import pytest

from pullbox.services.import_metadata_priority import (
    catalog_metadata_work,
    reset_import_metadata_priority,
    wait_for_comicinfo_turn,
)


@pytest.fixture(autouse=True)
def reset_priority_gate():
    reset_import_metadata_priority()


async def test_three_catalogs_advance_before_next_comicinfo_file() -> None:
    async with catalog_metadata_work(4) as catalog:
        waiting = asyncio.create_task(wait_for_comicinfo_turn())
        await asyncio.sleep(0)
        assert not waiting.done()
        await catalog.complete_one()
        await catalog.complete_one()
        assert not waiting.done()
        await catalog.complete_one()
        await asyncio.wait_for(waiting, timeout=1)


async def test_comicinfo_does_not_wait_when_catalog_queue_is_empty() -> None:
    await asyncio.wait_for(wait_for_comicinfo_turn(), timeout=1)
