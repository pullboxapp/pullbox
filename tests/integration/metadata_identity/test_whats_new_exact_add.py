"""Two operators adding the same published release share one library acquisition."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from sqlalchemy import func, select

from pullbox.core.events import EventBus
from pullbox.models import Issue, Series
from pullbox.models.metadata_source import MetadataSourceConfig
from pullbox.providers.metadata import sources
from pullbox.schemas.whats_new import WhatsNewSeriesSelection
from pullbox.services.catalog import reader as catalog_reader
from pullbox.services.whats_new_cache_service import WhatsNewCacheService
from pullbox.services.whats_new_exact_add import add_exact_release, release_offers
from pullbox.services.whats_new_grab import cache_fingerprint
from tests.integration.metadata_identity.test_series_interest import prepare
from tests.integration.metadata_identity.test_watch_release_refresh import (
    watch_refresh_enabled,  # noqa: F401
)
from tests.ui.test_whats_new_ui_routes import _issue_summary
from tests.unit.test_catalog_reader import installed_reader


async def test_concurrent_published_release_add_is_idempotent(
    identity_probe_db,
    tmp_path,
    monkeypatch,
    watch_refresh_enabled,  # noqa: F811
):
    _, factory, _ = identity_probe_db
    _, actor, _ = await prepare(factory, tmp_path / "shelf")
    catalog = installed_reader(tmp_path)
    monkeypatch.setattr(catalog_reader, "get_catalog_reader", lambda: catalog)
    monkeypatch.setattr(sources, "get_catalog_reader", lambda: catalog)
    emit = AsyncMock()
    monkeypatch.setattr(EventBus, "emit", emit)
    today = datetime.now(UTC).date()
    issue = _issue_summary()
    issue["series"].update(
        publication_state="published", publication_as_of=today.isoformat(), comicvine_series_id=10
    )
    cache = WhatsNewCacheService()
    async with factory.begin() as db:
        db.add(MetadataSourceConfig(source="comicvine_local", enabled=True, priority=1, revision=1))
        current = await cache.upsert_current_week(
            db, store_date=today, payload={"issues": [issue], "store_date": today.isoformat()}
        )
        upcoming = await cache.upsert_upcoming(db, payload={"weeks": []})
        await db.flush()
        offer = (await release_offers(db, [current, upcoming], {"180901"}))["180901"]
        selection = WhatsNewSeriesSelection(
            cache_id=current.id,
            release_id=1514020,
            fingerprint=offer.fingerprint(cache_fingerprint(current.payload), 1514020),
        )

    async def add():
        async with factory() as db:
            return await add_exact_release(db, selection, actor)

    results = await asyncio.gather(add(), add())
    assert results[0] == results[1]
    emit.assert_awaited_once()
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(Series)) == 1
        assert await db.scalar(select(func.count()).select_from(Issue)) == 1
