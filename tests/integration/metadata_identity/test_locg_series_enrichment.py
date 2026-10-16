"""Confirmed, fresh release facts fill gaps without becoming an executable source."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from pullbox.core.metadata_identity import (
    IdentityEvidenceKind,
    IdentityNamespace,
    MetadataEntityKind,
    MetadataSource,
)
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models import Issue, Series
from pullbox.models.issue import IssueStatus
from pullbox.models.metadata_baseline import SeriesMetadataBaseline
from pullbox.models.metadata_identity import SeriesExternalIdentity
from pullbox.models.metadata_source import MetadataSourceConfig
from pullbox.models.publisher import Publisher
from pullbox.models.whats_new import WhatsNewReleaseCache
from pullbox.schemas.metadata_snapshot import MetadataSnapshot
from pullbox.services.metadata_baselines import load_metadata_baseline
from pullbox.services.metadata_series_refresh import SeriesRefreshError, refresh_series_from_sources
from pullbox.services.whats_new_cache_service import WhatsNewCacheService
from tests.integration.metadata_identity.test_series_adoption import (
    configured_sources,  # noqa: F401
)
from tests.integration.metadata_identity.test_series_refresh import (
    RefreshAdapter,
    refresh_registry,
    seed,
)


def release(identifier=1001):
    return {
        "locg_issue_id": identifier,
        "locg_series_id": 77,
        "series": {
            "title": "Source-only series",
            "locg_series_id": 77,
            "metron_series_id": 42,
            "start_year": 2026,
            "volume": "2",
        },
        "publisher": {"name": "Release publisher"},
        "community_rating": 4.5,
    }


async def prepare(factory, *, claim=IdentityVerificationState.VERIFIED, age=timedelta()):
    series_id, issue_id, data = await seed(factory)
    data.series.publisher = None
    data.series.year_start = None
    data.series.volume = None
    now = datetime.now(UTC)
    async with factory.begin() as session:
        series = await session.get(Series, series_id)
        series.publisher_id = None
        series.year_start = None
        # A blank canonical baseline must agree with the deliberately blank test entity.
        baseline = await session.get(SeriesMetadataBaseline, series_id)
        previous = MetadataSnapshot.model_validate_json(baseline.snapshot_json)
        snapshot = previous.model_copy(
            update={
                "values": previous.values.model_copy(
                    update={"publisher": None, "year_start": None, "volume": None}
                ),
                "origins": tuple(
                    item
                    for item in previous.origins
                    if item.field not in {"publisher", "year_start", "volume"}
                ),
            }
        )
        baseline.snapshot_json = snapshot.model_dump_json()
        if claim is not None:
            session.add(
                SeriesExternalIdentity(
                    series_id=series_id,
                    identity_namespace=IdentityNamespace.LOCG,
                    external_id="77",
                    verification_state=claim,
                    evidence_kind=IdentityEvidenceKind.USER_SELECTION,
                )
            )
        cache = await WhatsNewCacheService(now_func=lambda: now - age).upsert_upcoming(
            session, payload={"weeks": [{"issues": [release(), release(1002)]}]}
        )
        await session.flush()
        cache_id = cache.id
    return series_id, issue_id, data, cache_id


async def test_refresh_fills_confirmed_series_gaps_and_records_passive_origin(identity_probe_db):
    _, factory, _ = identity_probe_db
    series_id, issue_id, data, cache_id = await prepare(factory)
    async with factory() as session:
        adapter = RefreshAdapter(data, session=session)
        result = await refresh_series_from_sources(
            session, series_id, registry=refresh_registry(adapter)
        )
        assert result.year_start == 2026, "fresh, confirmed LOCG year never reaches refresh"
        assert (
            await session.scalar(select(Publisher.name).where(Publisher.id == result.publisher_id))
            == "Release publisher"
        )
        baseline = await load_metadata_baseline(session, MetadataEntityKind.SERIES, series_id)
        assert baseline.snapshot.values.volume == "2"
        for field in ("publisher", "year_start", "volume"):
            origin = next(item for item in baseline.snapshot.origins if item.field == field)
            assert origin.source is None and not origin.user_override
            assert origin.passive_release.locg_series_id == "77"
            assert origin.passive_release.release_ids == ("1001", "1002")
            assert (
                origin.passive_release.fetched_at
                == (await session.get(WhatsNewReleaseCache, cache_id)).fetched_at
            )
        assert result.path == "/reference/original" and result.title == "Source-only series"
        issue = await session.get(Issue, issue_id)
        assert issue.status is IssueStatus.OWNED and issue.manual_skip
        assert await session.scalar(select(func.count()).select_from(Issue)) == 1
        assert await session.scalar(select(func.count()).select_from(MetadataSourceConfig)) == len(
            MetadataSource
        )
        await session.commit()
    async with factory() as session:
        result = await refresh_series_from_sources(
            session, series_id, registry=refresh_registry(RefreshAdapter(data, session=session))
        )
        assert result.year_start == 2026
        await session.commit()


@pytest.mark.parametrize("case", ["unlinked", "disputed", "stale", "future", "malformed"])
async def test_unproven_or_unfresh_facts_never_fill_gaps(identity_probe_db, case):
    _, factory, _ = identity_probe_db
    series_id, _, data, cache_id = await prepare(
        factory,
        claim=None
        if case == "unlinked"
        else (
            IdentityVerificationState.CONFLICTED
            if case == "disputed"
            else IdentityVerificationState.VERIFIED
        ),
        age=timedelta(hours=7)
        if case == "stale"
        else (timedelta(hours=-1) if case == "future" else timedelta()),
    )
    if case == "malformed":
        async with factory.begin() as session:
            cache = await session.get(WhatsNewReleaseCache, cache_id)
            cache.payload = {
                "weeks": [
                    {
                        "issues": [
                            {
                                **release(),
                                "series": {
                                    "locg_series_id": 77,
                                    "start_year": True,
                                    "volume": {"unsafe": "not text"},
                                },
                                "publisher": {"name": ["not text"]},
                            },
                        ]
                    }
                ]
            }
    async with factory() as session:
        result = await refresh_series_from_sources(
            session, series_id, registry=refresh_registry(RefreshAdapter(data, session=session))
        )
        assert result.year_start is None and result.publisher_id is None
        await session.commit()


async def test_exact_cross_identity_conflict_stops_before_mutation(identity_probe_db):
    _, factory, _ = identity_probe_db
    series_id, _, data, cache_id = await prepare(factory)
    async with factory.begin() as session:
        cache = await session.get(WhatsNewReleaseCache, cache_id)
        payload = deepcopy(cache.payload)
        payload["weeks"][0]["issues"][0]["series"]["metron_series_id"] = 999
        cache.payload = payload
    async with factory() as session:
        with pytest.raises(SeriesRefreshError, match=r"release.*identit|identit.*release"):
            await refresh_series_from_sources(
                session,
                series_id,
                registry=refresh_registry(RefreshAdapter(data, session=session)),
            )
        await session.rollback()
    async with factory() as session:
        result = await session.get(Series, series_id)
        assert result.year_start is None and result.publisher_id is None


@pytest.mark.parametrize(
    "case", ["invalid_cross_id", "nested_series_conflict", "observed_conflict"]
)
async def test_identity_errors_cannot_be_hidden_by_variant_grouping(identity_probe_db, case):
    from pullbox.core.metadata_identity import ExternalIdentityRef

    _, factory, _ = identity_probe_db
    series_id, _, data, cache_id = await prepare(factory)
    async with factory.begin() as session:
        cache = await session.get(WhatsNewReleaseCache, cache_id)
        payload = deepcopy(cache.payload)
        item = payload["weeks"][0]["issues"][0]
        if case == "invalid_cross_id":
            item["series"]["metron_series_id"] = True
        elif case == "nested_series_conflict":
            item["series"]["locg_series_id"] = 78
        else:
            item["series"]["comicvine_series_id"] = 501
            data.series.cross_identities = [
                ExternalIdentityRef(IdentityNamespace.COMICVINE, MetadataEntityKind.SERIES, "500")
            ]
        cache.payload = payload
    async with factory() as session:
        with pytest.raises(SeriesRefreshError, match=r"[Cc]ached release.*identit"):
            await refresh_series_from_sources(
                session, series_id, registry=refresh_registry(RefreshAdapter(data, session=session))
            )
        await session.rollback()
    async with factory() as session:
        assert (await session.get(Series, series_id)).publisher_id is None


async def test_cache_change_during_provider_fetch_requires_retry(identity_probe_db):
    _, factory, _ = identity_probe_db
    series_id, _, data, cache_id = await prepare(factory)
    wait = asyncio.Event()
    async with factory() as session:
        adapter = RefreshAdapter(data, wait=wait, session=session)
        task = asyncio.create_task(
            refresh_series_from_sources(session, series_id, registry=refresh_registry(adapter))
        )
        await asyncio.wait_for(adapter.started.wait(), 5)
        async with factory.begin() as writer:
            cache = await writer.get(WhatsNewReleaseCache, cache_id)
            cache.payload = {"weeks": []}
        wait.set()
        with pytest.raises(SeriesRefreshError, match=r"release.*changed|changed.*release"):
            await task
        await session.rollback()
    async with factory() as session:
        result = await session.get(Series, series_id)
        assert result.year_start is None and result.publisher_id is None


async def test_provider_and_user_values_win_and_caller_rollback_remains_atomic(identity_probe_db):
    _, factory, _ = identity_probe_db
    series_id, _, data, _ = await prepare(factory)
    data.series.publisher = "Exact provider publisher"
    async with factory.begin() as session:
        series = await session.get(Series, series_id)
        series.year_start = 1986
    async with factory() as session:
        result = await refresh_series_from_sources(
            session, series_id, registry=refresh_registry(RefreshAdapter(data, session=session))
        )
        assert result.year_start == 1986
        assert (
            await session.scalar(select(Publisher.name).where(Publisher.id == result.publisher_id))
            == "Exact provider publisher"
        )
        await session.rollback()
    async with factory() as session:
        result = await session.get(Series, series_id)
        assert result.publisher_id is None and result.year_start == 1986


async def test_exact_provider_can_replace_passive_value_but_not_a_user_clear(identity_probe_db):
    _, factory, _ = identity_probe_db
    series_id, _, data, _ = await prepare(factory)
    async with factory() as session:
        await refresh_series_from_sources(
            session, series_id, registry=refresh_registry(RefreshAdapter(data, session=session))
        )
        await session.commit()
    data.series.publisher = "Stronger exact publisher"
    async with factory() as session:
        result = await refresh_series_from_sources(
            session, series_id, registry=refresh_registry(RefreshAdapter(data, session=session))
        )
        assert (
            await session.scalar(select(Publisher.name).where(Publisher.id == result.publisher_id))
            == "Stronger exact publisher"
        )
        await session.commit()
    async with factory.begin() as session:
        series = await session.get(Series, series_id)
        series.year_start = None
    async with factory() as session:
        result = await refresh_series_from_sources(
            session, series_id, registry=refresh_registry(RefreshAdapter(data, session=session))
        )
        assert result.year_start is None, "a deliberate clear must remain protected"
        baseline = await load_metadata_baseline(session, MetadataEntityKind.SERIES, series_id)
        origin = next(item for item in baseline.snapshot.origins if item.field == "year_start")
        assert origin.user_override and origin.passive_release is None
        await session.commit()


async def test_inconsistent_variant_context_leaves_only_the_disputed_gap_empty(identity_probe_db):
    _, factory, _ = identity_probe_db
    series_id, _, data, cache_id = await prepare(factory)
    async with factory.begin() as session:
        cache = await session.get(WhatsNewReleaseCache, cache_id)
        payload = deepcopy(cache.payload)
        payload["weeks"][0]["issues"][1]["series"]["start_year"] = 1986
        cache.payload = payload
    async with factory() as session:
        result = await refresh_series_from_sources(
            session, series_id, registry=refresh_registry(RefreshAdapter(data, session=session))
        )
        assert result.year_start is None
        assert result.publisher_id is not None
        baseline = await load_metadata_baseline(session, MetadataEntityKind.SERIES, series_id)
        assert baseline.snapshot.values.volume == "2"
        assert len(baseline.snapshot.identities) == 2
        assert not baseline.snapshot.observed_identities, "release crosswalks are not attached"
        await session.commit()


async def test_context_expiring_during_fetch_requires_retry(identity_probe_db, monkeypatch):
    from pullbox.services import metadata_series_refresh as refresh_module

    _, factory, _ = identity_probe_db
    series_id, _, data, _ = await prepare(factory)
    wait = asyncio.Event()
    expired = False

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + (timedelta(hours=7) if expired else timedelta())

    monkeypatch.setattr(refresh_module, "datetime", Clock)
    async with factory() as session:
        adapter = RefreshAdapter(data, wait=wait, session=session)
        task = asyncio.create_task(
            refresh_series_from_sources(session, series_id, registry=refresh_registry(adapter))
        )
        await asyncio.wait_for(adapter.started.wait(), 5)
        expired = True
        wait.set()
        with pytest.raises(SeriesRefreshError, match="expired during refresh"):
            await task
        await session.rollback()


async def test_unlinked_refresh_does_not_query_release_cache(identity_probe_db):
    from sqlalchemy import event

    engine, factory, _ = identity_probe_db
    series_id, _, data, _ = await prepare(factory, claim=None)
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with factory() as session:
            await refresh_series_from_sources(
                session, series_id, registry=refresh_registry(RefreshAdapter(data, session=session))
            )
            await session.commit()
        assert not any("whats_new_release_cache" in statement for statement in statements)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
