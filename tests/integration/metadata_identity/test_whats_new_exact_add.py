"""Two operators adding the same published release share one library acquisition."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from sqlalchemy import event, func, select

from pullbox.core.events import EventBus
from pullbox.models import Issue, IssueStatus, Series, SeriesExternalIdentity
from pullbox.models.metadata_source import MetadataSourceConfig
from pullbox.providers.metadata import sources
from pullbox.schemas.whats_new import WhatsNewSeriesSelection, WhatsNewTrackSelection
from pullbox.services.catalog import reader as catalog_reader
from pullbox.services.whats_new_actions import release_series_evidence
from pullbox.services.whats_new_cache_service import WhatsNewCacheService
from pullbox.services.whats_new_exact_add import (
    add_exact_release,
    current_release_evidence,
    release_offers,
)
from pullbox.services.whats_new_grab import cache_fingerprint
from pullbox.services.whats_new_library_matches import native_library_matches
from pullbox.services.whats_new_track import track_release_series
from tests.integration.metadata_identity.test_series_interest import prepare
from tests.integration.metadata_identity.test_watch_release_refresh import (
    watch_refresh_enabled,  # noqa: F401
)
from tests.ui.test_whats_new_ui_routes import _issue_summary
from tests.unit.test_catalog_reader import installed_reader


async def test_two_track_clicks_claim_only_one_issue_transition(identity_probe_db):
    _, factory, _ = identity_probe_db
    today = datetime.now(UTC).date()
    cache = WhatsNewCacheService()
    release = _issue_summary()
    release["series"]["gcd_series_id"] = 82
    async with factory.begin() as db:
        current = await cache.upsert_current_week(
            db, store_date=today, payload={"store_date": today.isoformat(), "issues": [release]}
        )
        await cache.upsert_upcoming(db, payload={"weeks": []})
        owner = Series(title="Native", sort_title="native", monitored=False)
        db.add(owner)
        await db.flush()
        issue = Issue(
            series_id=owner.id, issue_number=1.0, status=IssueStatus.SKIPPED, manual_skip=False
        )
        db.add_all(
            [
                issue,
                SeriesExternalIdentity(
                    series_id=owner.id,
                    identity_namespace="gcd",
                    external_id="82",
                    verification_state="verified",
                    evidence_kind="user_selection",
                ),
            ]
        )
        await db.flush()
        selection = WhatsNewTrackSelection(
            cache_id=current.id,
            release_id=release["locg_issue_id"],
            fingerprint=cache_fingerprint(current.payload),
            expected_series_id=owner.id,
        )

    async def click():
        async with factory.begin() as db:
            return await track_release_series(db, selection)

    first, second = await asyncio.gather(click(), click())
    assert first + second == [(issue.id, owner.id)]
    async with factory() as db:
        assert (await db.get(Series, owner.id)).monitored
        assert (await db.get(Issue, issue.id)).status == IssueStatus.WANTED


async def test_native_series_badges_for_one_hundred_releases_take_one_read(identity_probe_db):
    engine, factory, _ = identity_probe_db
    rows = []
    async with factory.begin() as db:
        series = [
            Series(title=f"S{i}", sort_title=f"s{i}", monitored=i % 2 == 0) for i in range(100)
        ]
        db.add_all(series)
        await db.flush()
        for i, owner in enumerate(series, 1):
            ns = ("comicvine", "metron", "gcd")[i % 3]
            db.add(
                SeriesExternalIdentity(
                    series_id=owner.id,
                    identity_namespace=ns,
                    external_id=str(i),
                    verification_state="verified",
                    evidence_kind="user_selection",
                )
            )
            rows.append(
                {
                    "locg_issue_id": i + 200,
                    "series": {"locg_series_id": i + 100, f"{ns}_series_id": i},
                }
            )
    evidence = release_series_evidence({"issues": rows}, as_of=datetime.now(UTC).date())
    statements = []

    def record(conn, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        async with factory() as db:
            matches = await native_library_matches(db, evidence, set(evidence.claims))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
    assert len(statements) == 1 and statements[0].lstrip().upper().startswith("SELECT")
    assert {key: (badge.id, badge.monitored) for key, badge in matches.items()} == {
        str(i + 100): (owner.id, owner.monitored) for i, owner in enumerate(series, 1)
    }


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
        evidence = await current_release_evidence([current, upcoming])
        offer = (await release_offers(db, evidence, {"180901"}))["180901"]
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
