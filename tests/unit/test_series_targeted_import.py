"""Unit tests for targeted-first series import metadata behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from pullbox.core.events import EventBus, SeriesAdded
from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.core.naming import classify_series_type
from pullbox.models.config import SystemConfig
from pullbox.models.import_job import (
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import LibraryRoot
from pullbox.models.publisher import Publisher
from pullbox.models.series import IssueCatalogState, Series, SeriesStatus, SeriesType
from pullbox.providers.base import IssueSummary, SeriesMetadata
from pullbox.services import series_service as series_service_module
from pullbox.services.series_service import SeriesService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _fake_upsert_series(
    session: AsyncSession,
    cv_id: int,
    meta: SeriesMetadata,
) -> Series:
    publisher: Publisher | None = None
    if meta.publisher:
        publisher = await session.scalar(select(Publisher).where(Publisher.name == meta.publisher))
        if publisher is None:
            publisher = Publisher(name=meta.publisher)
            session.add(publisher)
            await session.flush()

    existing = await session.scalar(select(Series).where(Series.comicvine_id == cv_id))
    if existing is not None:
        existing.title = meta.title
        existing.sort_title = meta.sort_title or meta.title
        existing.year_start = meta.year_start
        existing.issue_count = meta.issue_count or 0
        existing.comicvine_url = meta.comicvine_url
        existing.publisher_id = publisher.id if publisher is not None else None
        existing.series_type = SeriesType(
            classify_series_type(
                meta.title,
                description=meta.description,
                issue_count=meta.issue_count or 0,
                year_start=meta.year_start,
            )
        )
        await session.flush()
        return existing

    series = Series(
        comicvine_id=cv_id,
        title=meta.title,
        sort_title=meta.sort_title or meta.title,
        year_start=meta.year_start,
        status=SeriesStatus.CONTINUING,
        issue_count=meta.issue_count or 0,
        comicvine_url=meta.comicvine_url,
        publisher_id=publisher.id if publisher is not None else None,
        series_type=SeriesType(
            classify_series_type(
                meta.title,
                description=meta.description,
                issue_count=meta.issue_count or 0,
                year_start=meta.year_start,
            )
        ),
    )
    session.add(series)
    await session.flush()
    return series


async def _seed_folder_naming_config(session: AsyncSession, template: str) -> None:
    for key, value, value_type in (
        ("series_folder_template", template, "string"),
        ("replace_illegal_characters", "true", "bool"),
        ("colon_replacement", "dash", "string"),
    ):
        session.add(SystemConfig(key=key, value=value, value_type=value_type))
    await session.flush()


async def _fake_upsert_issue_summaries(
    session: AsyncSession,
    series: Series,
    summaries: list[IssueSummary],
    *,
    infer_series_type_from_summaries: bool = False,
) -> list[Issue]:
    del infer_series_type_from_summaries
    created: list[Issue] = []
    for summary in summaries:
        issue = Issue(
            series_id=series.id,
            comicvine_id=int(summary.provider_id),
            issue_number=summary.issue_number,
            title=summary.title,
            status=IssueStatus.SKIPPED,
        )
        session.add(issue)
        created.append(issue)
    await session.flush()
    return created


@pytest.mark.asyncio
async def test_targeted_import_creates_partial_catalog_without_emitting_series_added(
    db_session: AsyncSession,
) -> None:
    metadata = MagicMock()
    metadata.upsert_series_metadata = AsyncMock(side_effect=_fake_upsert_series)
    metadata.classify_and_link_series = AsyncMock()
    metadata.upsert_issue_summaries = AsyncMock(side_effect=_fake_upsert_issue_summaries)
    metadata.infer_series_status = AsyncMock()
    event_bus = EventBus()
    emitted: list[SeriesAdded] = []
    event_bus.subscribe(SeriesAdded, emitted.append)
    service = SeriesService(metadata_service=metadata, event_bus=event_bus)
    import_series = ImportedSeries(
        raw_series_name="King Dracula",
        cv_id=166904,
        cv_title="King Dracula",
        cv_year=2025,
        cv_publisher="Dynamite",
        cv_issue_count=3,
        cv_url="https://comicvine.gamespot.com/king-dracula/4050-166904/",
    )

    series = await service.add_from_import_review_targeted(
        db_session,
        import_series=import_series,
        library_root_id=None,
        search_on_add=True,
        issue_summaries=[
            IssueSummary(
                provider_id="120004",
                issue_number=4.0,
                title="Issue 4",
                release_date=None,
                cover_url=None,
                issue_type="issue",
            )
        ],
    )

    assert series.monitored is True
    assert series.issue_catalog_state == IssueCatalogState.HYDRATING
    assert series.issue_catalog_error is None
    assert emitted == []
    issues = (await db_session.scalars(select(Issue).where(Issue.series_id == series.id))).all()
    assert [(issue.issue_number, issue.comicvine_id) for issue in issues] == [(4.0, 120004)]


@pytest.mark.asyncio
async def test_targeted_import_caches_discovered_local_series_cover(
    db_session: AsyncSession,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_cover = tmp_path / "mylar" / "cover.jpg"
    local_cover.parent.mkdir()
    local_cover.write_bytes(b"cover")
    job = ImportJob(
        source_path=str(tmp_path / "mylar.db"),
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.IMPORTING,
    )
    db_session.add(job)
    await db_session.flush()
    metadata = MagicMock()
    metadata.upsert_series_metadata = AsyncMock(side_effect=_fake_upsert_series)
    metadata.upsert_issue_summaries = AsyncMock(side_effect=_fake_upsert_issue_summaries)
    cache_local_cover = AsyncMock()
    monkeypatch.setattr(
        series_service_module,
        "cache_imported_series_cover",
        cache_local_cover,
    )
    service = SeriesService(metadata_service=metadata, event_bus=EventBus())
    import_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Batman",
        cv_id=47050,
        cv_title="Batman",
        source_folder=str(local_cover.parent),
        diagnostics={"kind": "series_match"},
    )

    series = await service.add_from_import_review_targeted(
        db_session,
        import_series=import_series,
        issue_summaries=[],
    )

    cache_local_cover.assert_awaited_once_with(db_session, series, local_cover)


@pytest.mark.asyncio
async def test_targeted_import_uses_cached_metadata_for_type_aware_folder_name(
    db_session: AsyncSession,
    tmp_path,
) -> None:
    """Targeted placement honors all folder tokens without a cold metadata fetch."""
    root = LibraryRoot(name="Comics", path=str(tmp_path), enabled=True)
    db_session.add(root)
    await db_session.flush()
    await _seed_folder_naming_config(
        db_session,
        "{Publisher} - {Series} ({Year}) [{Type}] [cv-{ComicVineId}]",
    )

    class CachedMetadata:
        async def get_cached_series_metadata(self, comicvine_id: int) -> SeriesMetadata | None:
            assert comicvine_id == 111396
            return SeriesMetadata(
                provider_id="111396",
                title="About Betty's Boob",
                sort_title="About Betty's Boob",
                year_start=2018,
                year_end=None,
                status="ended",
                publisher="Archaia",
                description="A hardcover graphic novel.",
                cover_url=None,
                issue_count=1,
                comicvine_url="https://comicvine.gamespot.com/about-bettys-boob/4050-111396/",
            )

    metadata = CachedMetadata()
    metadata.upsert_series_metadata = AsyncMock(side_effect=_fake_upsert_series)
    metadata.upsert_issue_summaries = AsyncMock(side_effect=_fake_upsert_issue_summaries)
    service = SeriesService(metadata_service=metadata, event_bus=EventBus())
    import_series = ImportedSeries(
        raw_series_name="About Betty's Boob",
        cv_id=111396,
        cv_title="About Betty's Boob",
        cv_year=2018,
        cv_publisher="Archaia",
        cv_issue_count=1,
        cv_url="https://comicvine.gamespot.com/about-bettys-boob/4050-111396/",
    )

    series = await service.add_from_import_review_targeted(
        db_session,
        import_series=import_series,
        library_root_id=root.id,
        issue_summaries=[],
    )

    assert series.series_type == SeriesType.HARDCOVER
    assert series.path == str(tmp_path / "Archaia - About Betty's Boob (2018) [HC] [cv-111396]")


@pytest.mark.asyncio
async def test_targeted_import_uses_explicit_single_one_shot_as_folder_hint(
    db_session: AsyncSession,
    tmp_path,
) -> None:
    """A source one-shot hint names a one-issue folder without reclassifying its series."""
    root = LibraryRoot(name="Comics", path=str(tmp_path), enabled=True)
    db_session.add(root)
    await db_session.flush()
    await _seed_folder_naming_config(db_session, "{Series} ({Year}) [{Type}]")

    metadata = MagicMock()
    metadata.upsert_series_metadata = AsyncMock(side_effect=_fake_upsert_series)
    metadata.upsert_issue_summaries = AsyncMock(side_effect=_fake_upsert_issue_summaries)
    service = SeriesService(metadata_service=metadata, event_bus=EventBus())
    import_series = ImportedSeries(
        raw_series_name="Black Mass Rising",
        cv_id=144914,
        cv_title="Black Mass Rising",
        cv_year=2022,
        cv_publisher="TKO Studios",
        cv_issue_count=1,
        cv_url="https://comicvine.gamespot.com/black-mass-rising/4050-144914/",
    )

    series = await service.add_from_import_review_targeted(
        db_session,
        import_series=import_series,
        library_root_id=root.id,
        issue_summaries=[
            IssueSummary(
                provider_id="945536",
                issue_number=1.0,
                title=None,
                release_date=None,
                cover_url=None,
                issue_type="one_shot",
            )
        ],
    )

    assert series.series_type == SeriesType.STANDARD
    assert series.path == str(tmp_path / "Black Mass Rising (2022) [One-Shot]")


@pytest.mark.asyncio
async def test_hydrate_series_catalog_marks_complete_and_emits_series_added(
    db_session: AsyncSession,
) -> None:
    metadata = MagicMock()
    metadata.upsert_series_metadata = AsyncMock(side_effect=_fake_upsert_series)
    metadata.classify_and_link_series = AsyncMock()
    metadata.upsert_issue_summaries = AsyncMock(side_effect=_fake_upsert_issue_summaries)
    metadata.infer_series_status = AsyncMock()
    event_bus = EventBus()
    emitted: list[SeriesAdded] = []
    event_bus.subscribe(SeriesAdded, emitted.append)
    service = SeriesService(metadata_service=metadata, event_bus=event_bus)
    series = Series(
        comicvine_id=166904,
        title="King Dracula",
        sort_title="king dracula",
        monitored=True,
        issue_catalog_state=IssueCatalogState.HYDRATING,
    )
    db_session.add(series)
    await db_session.flush()
    metadata.get_series_metadata = AsyncMock(
        return_value=SeriesMetadata(
            provider_id="166904",
            title="King Dracula",
            sort_title="King Dracula",
            year_start=2025,
            year_end=None,
            status="continuing",
            publisher="Dynamite",
            description=None,
            cover_url=None,
            issue_count=4,
            comicvine_url="https://comicvine.gamespot.com/king-dracula/4050-166904/",
        )
    )
    metadata.get_issue_summaries_for_series = AsyncMock(
        return_value=[
            IssueSummary(
                provider_id="120001",
                issue_number=1.0,
                title="Issue 1",
                release_date=None,
                cover_url=None,
                issue_type="issue",
            )
        ]
    )

    await service.hydrate_series_catalog(db_session, series.id, search_on_add=True)
    await db_session.refresh(series)

    assert series.issue_catalog_state == IssueCatalogState.COMPLETE
    assert series.issue_catalog_last_synced_at is not None
    assert series.issue_catalog_error is None
    assert emitted == [SeriesAdded(series_id=series.id, comicvine_id=166904)]


@pytest.mark.asyncio
async def test_hydrate_series_catalog_routes_future_folder_work_to_preferred_root(
    db_session: AsyncSession,
) -> None:
    metadata = MagicMock()
    service = SeriesService(metadata_service=metadata, event_bus=EventBus())
    current_root = LibraryRoot(
        name="Existing",
        path="/existing",
        enabled=True,
        allow_managed_writes=False,
    )
    preferred_root = LibraryRoot(name="Future", path="/future", enabled=True)
    db_session.add_all([current_root, preferred_root])
    await db_session.flush()
    series = Series(
        comicvine_id=166905,
        title="Future Root",
        sort_title="future root",
        library_root_id=current_root.id,
        preferred_library_root_id=preferred_root.id,
        issue_catalog_state=IssueCatalogState.HYDRATING,
    )
    db_session.add(series)
    await db_session.flush()
    service.prefetch_comicvine_bundle = AsyncMock(return_value=(MagicMock(), []))  # type: ignore[method-assign]
    add_prefetched = AsyncMock(return_value=series)
    service.add_from_comicvine_prefetched = add_prefetched  # type: ignore[method-assign]

    hydrated = await service.hydrate_series_catalog(db_session, series.id)

    assert hydrated is series
    assert add_prefetched.await_args.kwargs["library_root_id"] == preferred_root.id


@pytest.mark.asyncio
async def test_targeted_import_requires_a_comicvine_id(db_session: AsyncSession) -> None:
    metadata = MagicMock()
    service = SeriesService(metadata_service=metadata, event_bus=EventBus())
    import_series = ImportedSeries(raw_series_name="Unknown Candidate")

    with pytest.raises(ValidationError, match="ComicVine ID is required"):
        await service.add_from_import_review_targeted(
            db_session,
            import_series=import_series,
            issue_summaries=[],
        )


@pytest.mark.asyncio
async def test_hydrate_series_catalog_validates_series_before_fetching(
    db_session: AsyncSession,
) -> None:
    metadata = MagicMock()
    metadata.get_series_metadata = AsyncMock()
    metadata.get_issue_summaries_for_series = AsyncMock()
    service = SeriesService(metadata_service=metadata, event_bus=EventBus())
    local_series = Series(
        title="Local Only",
        sort_title="local only",
        issue_catalog_state=IssueCatalogState.PARTIAL,
    )
    db_session.add(local_series)
    await db_session.flush()

    with pytest.raises(NotFoundError):
        await service.hydrate_series_catalog(db_session, 999999)
    with pytest.raises(ValidationError, match="no ComicVine ID"):
        await service.hydrate_series_catalog(db_session, local_series.id)

    assert metadata.get_series_metadata.await_count == 0
    assert metadata.get_issue_summaries_for_series.await_count == 0


@pytest.mark.asyncio
async def test_hydrate_series_catalog_marks_failed_when_provider_fetch_fails(
    db_session: AsyncSession,
) -> None:
    metadata = MagicMock()
    metadata.get_series_metadata = AsyncMock(side_effect=RuntimeError("ComicVine unavailable"))
    metadata.get_issue_summaries_for_series = AsyncMock(return_value=[])
    service = SeriesService(metadata_service=metadata, event_bus=EventBus())
    series = Series(
        comicvine_id=166904,
        title="King Dracula",
        sort_title="king dracula",
        issue_catalog_state=IssueCatalogState.PARTIAL,
        issue_catalog_error="old error",
    )
    db_session.add(series)
    await db_session.flush()

    with pytest.raises(RuntimeError, match="ComicVine unavailable"):
        await service.hydrate_series_catalog(db_session, series.id)
    await db_session.refresh(series)

    assert series.issue_catalog_state == IssueCatalogState.FAILED
    assert series.issue_catalog_error == "ComicVine unavailable"
    assert series.issue_catalog_last_synced_at is None
    assert series.issue_catalog_last_checked_at is None
