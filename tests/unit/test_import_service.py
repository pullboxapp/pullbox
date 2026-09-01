"""Unit tests for ImportService class."""

from __future__ import annotations

import os
import sqlite3
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullbox.config import get_settings
from pullbox.core.collection_scanner import DiscoveredSeries, ScanInventory
from pullbox.core.exceptions import JobCancelledError, JobPausedError, ValidationError
from pullbox.core.source_metadata import MetadataSignal, SourceMetadataExtractor
from pullbox.models import Base
from pullbox.models.config import SystemConfig
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportFileHandlingMode,
    ImportJob,
    ImportJobAction,
    ImportJobActionStatus,
    ImportJobLog,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.publisher import Publisher
from pullbox.models.series import Series, SeriesType
from pullbox.providers.base import IssueSummary
from pullbox.schemas.import_job import (
    ConfirmImportRequest,
    ImportJobCreate,
    ImportProgressEvent,
    ImportReconcileDecision,
    ImportReconcileRequest,
    OrphanRecoveryDecision,
    RecoverOrphanRequest,
)
from pullbox.services import library_root_management
from pullbox.services.comicvine_persistent_cache import PersistentComicVineCacheProvider
from pullbox.services.import_service import ComicVineMatchEvaluation, ImportService
from pullbox.services.import_story_arc_placement_completion import (
    ImportStoryArcPlacementCompletionOutcome,
    ImportStoryArcPlacementCompletionState,
    ImportStoryArcPlacementCounts,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_discovered(
    *,
    name: str = "Batman",
    year: int | None = 2016,
    publisher: str | None = None,
    file_count: int = 5,
    mylar3_cv_id: int | None = None,
    folder_cv_id: int | None = None,
    comicinfo_cv_id: int | None = None,
    source_folder: str = "/comics/Batman (2016)",
) -> DiscoveredSeries:
    return DiscoveredSeries(
        raw_series_name=name,
        raw_year=year,
        raw_publisher=publisher,
        file_count=file_count,
        sample_paths=[f"{source_folder}/issue{i}.cbz" for i in range(min(file_count, 3))],
        source_folder=source_folder,
        source_folder_relative=name,
        mylar3_cv_id=mylar3_cv_id,
        folder_cv_id=folder_cv_id,
        comicinfo_cv_id=comicinfo_cv_id,
    )


def _make_service(
    *,
    series_service: AsyncMock | None = None,
    metadata_service: AsyncMock | None = None,
    event_bus: AsyncMock | None = None,
) -> ImportService:
    """Create an ImportService with mocked dependencies."""
    return ImportService(
        series_service=series_service or AsyncMock(),
        metadata_service=metadata_service or AsyncMock(),
        event_bus=event_bus or AsyncMock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "schedule_sync", "schedule_enrichment"),
    [
        (ImportStoryArcPlacementCompletionState.PENDING, True, False),
        (ImportStoryArcPlacementCompletionState.COMPLETED, False, True),
    ],
)
async def test_run_import_resumes_only_the_story_arc_placement_finalizer(
    db_session: AsyncSession,
    state: ImportStoryArcPlacementCompletionState,
    schedule_sync: bool,
    schedule_enrichment: bool,
) -> None:
    job = ImportJob(
        source_path="/imports/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.IMPORTING,
        progress_snapshot={"mode": "import", "phase": "story_arc_placements"},
    )
    db_session.add(job)
    await db_session.flush()
    outcome = ImportStoryArcPlacementCompletionOutcome(
        job_id=job.id,
        state=state,
        counts=ImportStoryArcPlacementCounts(queued=1),
    )
    service = _make_service()

    with (
        patch(
            "pullbox.services.import_service.finalize_import_story_arc_placements",
            new=AsyncMock(return_value=outcome),
        ) as finalize,
        patch(
            "pullbox.services.import_service.execute_import_job",
            new=AsyncMock(),
        ) as execute,
    ):
        result = await service.run_import(db_session, job.id)

    finalize.assert_awaited_once_with(db_session, job.id)
    execute.assert_not_awaited()
    assert result.schedule_story_arc_sync is schedule_sync
    assert result.schedule_comicinfo_enrichment is schedule_enrichment


async def test_scan_metadata_provider_skips_persistent_cache_for_in_memory_sqlite(
    async_engine,
) -> None:
    provider = AsyncMock()
    metadata_service = AsyncMock()
    metadata_service._provider = provider
    service = _make_service(metadata_service=metadata_service)
    session_factory = async_sessionmaker(async_engine, expire_on_commit=False)

    async with session_factory() as session:
        cached_provider = service._build_scan_metadata_provider(session)

    assert cached_provider._provider is provider


async def test_scan_metadata_provider_keeps_persistent_cache_for_file_sqlite(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pullbox.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    provider = AsyncMock()
    metadata_service = AsyncMock()
    metadata_service._provider = provider
    service = _make_service(metadata_service=metadata_service)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as session:
            cached_provider = service._build_scan_metadata_provider(session)
    finally:
        await engine.dispose()

    assert isinstance(cached_provider._provider, PersistentComicVineCacheProvider)


def _matched_evaluation(match: dict[str, object]) -> ComicVineMatchEvaluation:
    """Wrap a successful ComicVine match in the evaluated result contract."""
    return ComicVineMatchEvaluation(match=match, diagnostics={})


def _no_match_evaluation(
    *,
    raw_name: str = "Unknown Series",
    raw_year: int | None = None,
    top_candidates: list[dict[str, object]] | None = None,
) -> ComicVineMatchEvaluation:
    """Wrap a no-match result in the evaluated result contract."""
    return ComicVineMatchEvaluation(
        match=None,
        diagnostics={
            "kind": "series_no_match",
            "reason": "below_threshold",
            "raw_name": raw_name,
            "raw_year": raw_year,
            "normalized_query": raw_name.lower(),
            "threshold": 0.7,
            "top_candidates": top_candidates or [],
        },
    )


async def _create_job_row(
    session: AsyncSession,
    *,
    source_path: str = "/tmp/comics",
    source_type: ImportSourceType = ImportSourceType.FILESYSTEM,
    status: ImportJobStatus = ImportJobStatus.PENDING,
    file_handling_mode: ImportFileHandlingMode = ImportFileHandlingMode.MANAGED_COPY,
) -> ImportJob:
    """Insert an ImportJob directly for test setup."""
    target_library_root_id: int | None = None
    if file_handling_mode == ImportFileHandlingMode.MANAGED_COPY:
        root = await session.scalar(
            select(LibraryRoot).where(LibraryRoot.is_default_managed_destination.is_(True))
        )
        if root is None:
            root_path = get_settings().library_root
            root_path.mkdir(parents=True, exist_ok=True)
            root = LibraryRoot(
                name="Test managed root",
                path=str(root_path),
                enabled=True,
                allow_referenced_registrations=True,
                allow_managed_writes=True,
                is_default_managed_destination=True,
            )
            session.add(root)
            await session.flush()
        target_library_root_id = root.id
    job = ImportJob(
        source_path=source_path,
        source_type=source_type,
        status=status,
        file_handling_mode=file_handling_mode,
        target_library_root_id=target_library_root_id,
    )
    session.add(job)
    await session.flush()
    return job


async def _create_imported_series(
    session: AsyncSession,
    job: ImportJob,
    *,
    name: str = "Batman",
    year: int | None = 2016,
    status: ImportSeriesStatus = ImportSeriesStatus.MATCHED,
    cv_id: int | None = 97508,
    cv_match_score: float | None = 0.95,
    cv_match_method: str | None = "exact_title_year",
    selected_for_import: bool = False,
) -> ImportedSeries:
    """Insert an ImportedSeries for test setup."""
    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name=name,
        raw_year=year,
        status=status,
        file_count=5,
        cv_id=cv_id,
        cv_match_score=cv_match_score,
        cv_match_method=cv_match_method,
        selected_for_import=selected_for_import,
    )
    session.add(item)
    await session.flush()
    return item


async def _create_imported_file(
    session: AsyncSession,
    job: ImportJob,
    item: ImportedSeries,
    *,
    file_name: str = "Batman 001.cbz",
    status: ImportedFileStatus = ImportedFileStatus.MATCHED,
) -> ImportedFile:
    """Insert an importable file row for tests that exercise Step 4 execution."""
    imp_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=item.id,
        file_path=f"/tmp/comics/{file_name}",
        file_name=file_name,
        file_size=1024,
        file_format=file_name.rsplit(".", 1)[-1],
        parsed_issue_number=1.0,
        status=status,
    )
    session.add(imp_file)
    await session.flush()
    return imp_file


def _write_cbz_archive(path: Path, comicinfo_xml: str) -> None:
    """Create a minimal CBZ archive with ComicInfo.xml."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("ComicInfo.xml", comicinfo_xml)
        archive.writestr("pages/page001.jpg", b"fake image")


def _write_cb7_archive(path: Path, comicinfo_xml: str) -> None:
    """Create a minimal CB7 archive with ComicInfo.xml."""
    import py7zr

    path.parent.mkdir(parents=True, exist_ok=True)
    payload_dir = path.parent / f"{path.stem}-payload"
    payload_dir.mkdir()
    comicinfo_path = payload_dir / "ComicInfo.xml"
    comicinfo_path.write_text(comicinfo_xml)
    page_path = payload_dir / "page001.jpg"
    page_path.write_bytes(b"fake image")

    with py7zr.SevenZipFile(path, "w") as archive:
        archive.write(comicinfo_path, "ComicInfo.xml")
        archive.write(page_path, "page001.jpg")


# ── Fixtures ─────────────────────────────────────────────────────────────


async def _create_target_series(session: AsyncSession) -> Series:
    """Create a real Series row that can be referenced by FK."""
    target = Series(
        title="Imported Target",
        sort_title="imported target",
        year_start=2020,
        comicvine_id=99999,
    )
    session.add(target)
    await session.flush()
    return target


@pytest.fixture
def mock_series_service() -> AsyncMock:
    svc = AsyncMock()
    # add_from_comicvine return value must be set per-test when DB FK is needed
    return svc


@pytest.fixture
def mock_metadata_service() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_event_bus() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def service(
    mock_series_service: AsyncMock,
    mock_metadata_service: AsyncMock,
    mock_event_bus: AsyncMock,
) -> ImportService:
    return ImportService(
        series_service=mock_series_service,
        metadata_service=mock_metadata_service,
        event_bus=mock_event_bus,
    )


@pytest.fixture
async def configured_managed_root(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> LibraryRoot:
    """Model the configured-root invariant for direct service tests."""
    root = await db_session.scalar(
        select(LibraryRoot).where(LibraryRoot.is_default_managed_destination.is_(True))
    )
    if root is None:
        root_path = tmp_path / "configured-managed-root"
        root_path.mkdir()
        root = LibraryRoot(
            name="Configured managed root",
            path=str(root_path),
            enabled=True,
            allow_referenced_registrations=True,
            allow_managed_writes=True,
            is_default_managed_destination=True,
        )
        db_session.add(root)
        await db_session.flush()
    monkeypatch.setattr(
        library_root_management.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=100 * 1024**3),
    )
    return root


# ── Test: create_job ─────────────────────────────────────────────────────


class TestCreateJob:
    """Test create_job() — creates ImportJob row with PENDING status."""

    @pytest.fixture(autouse=True)
    async def _configured_managed_root(
        self,
        configured_managed_root: LibraryRoot,
    ) -> None:
        _ = configured_managed_root

    @pytest.mark.asyncio
    async def test_create_job_pending(
        self, db_session: AsyncSession, service: ImportService, tmp_path: object
    ) -> None:
        """create_job() creates a PENDING job using the global search-on-add policy."""
        source = str(tmp_path)
        db_session.add(SystemConfig(key="search_on_add_default", value="true", value_type="bool"))
        await db_session.flush()
        request = ImportJobCreate(
            source_path=source,
            source_type=ImportSourceType.FILESYSTEM,
            monitored=True,
        )
        job = await service.create_job(db_session, request)

        assert job.id is not None
        assert job.status == ImportJobStatus.PENDING
        assert job.source_path == source
        assert job.source_type == ImportSourceType.FILESYSTEM
        assert job.monitored is True
        assert job.search_on_add is True

    @pytest.mark.asyncio
    async def test_create_job_with_mylar3_path_map(
        self,
        db_session: AsyncSession,
        service: ImportService,
        tmp_path: Path,
        configured_managed_root: LibraryRoot,
    ) -> None:
        """Mylar3 path map is persisted to the job."""
        source = tmp_path / "mylar.db"
        mapped_root = tmp_path / "mapped-comics"
        (mapped_root / "Batman").mkdir(parents=True)
        connection = sqlite3.connect(source)
        connection.execute("CREATE TABLE comics (ComicLocation TEXT)")
        connection.execute("INSERT INTO comics VALUES ('/comics/Batman')")
        connection.commit()
        connection.close()
        mapped_library_root = LibraryRoot(
            name="Mapped comics",
            path=str(mapped_root),
            enabled=True,
            is_default_managed_destination=False,
        )
        db_session.add(mapped_library_root)
        await db_session.flush()
        request = ImportJobCreate(
            source_path=str(source),
            source_type=ImportSourceType.MYLAR3,
            mylar3_path_map={"/comics": str(mapped_root)},
            mylar3_path_map_confirmed=True,
            target_library_root_id=configured_managed_root.id,
        )
        job = await service.create_job(db_session, request)

        assert job.mylar3_path_map == {"/comics": str(mapped_root)}
        assert job.mylar3_path_map_confirmed is True

    @pytest.mark.asyncio
    async def test_create_job_persists_selected_file_paths_and_forces_monitored(
        self, db_session: AsyncSession, service: ImportService, tmp_path: object
    ) -> None:
        """Explicit file imports keep their file list.

        Global search-on-add should still force monitoring on.
        """
        first = tmp_path / "Batman 001.cbz"
        second = tmp_path / "Batman 002.cbz"
        first.write_text("a")
        second.write_text("b")
        db_session.add(SystemConfig(key="search_on_add_default", value="true", value_type="bool"))
        await db_session.flush()

        request = ImportJobCreate(
            source_path=str(tmp_path),
            file_paths=[str(first), str(second)],
            source_type=ImportSourceType.FILESYSTEM,
            monitored=False,
        )

        job = await service.create_job(db_session, request)

        assert job.selected_file_paths == [str(first.resolve()), str(second.resolve())]
        assert job.monitored is True
        assert job.search_on_add is True


class TestLogicalSeriesConsolidation:
    """Repeated import buckets that point at the same target should merge physically."""

    @pytest.fixture(autouse=True)
    async def _configured_managed_root(
        self,
        configured_managed_root: LibraryRoot,
    ) -> None:
        _ = configured_managed_root

    @pytest.mark.asyncio
    async def test_consolidates_matched_series_by_cv_id_and_title(
        self,
        db_session: AsyncSession,
    ) -> None:
        job = await _create_job_row(db_session, status=ImportJobStatus.MATCHING)
        svc = _make_service()

        first = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Abattoir",
            raw_year=2010,
            status=ImportSeriesStatus.MATCHED,
            cv_id=36339,
            cv_match_score=0.94,
            source_folder="/imports/set-a/Abattoir",
            sample_paths=["/imports/set-a/Abattoir/Abattoir 001.cbz"],
            file_count=1,
            files_total=1,
        )
        second = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Abattoir",
            raw_year=2010,
            status=ImportSeriesStatus.MATCHED,
            cv_id=36339,
            cv_match_score=0.93,
            source_folder="/imports/set-b/Abattoir",
            sample_paths=["/imports/set-b/Abattoir/Abattoir 002.cbz"],
            file_count=1,
            files_total=1,
        )
        db_session.add_all([first, second])
        await db_session.flush()

        db_session.add_all(
            [
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=first.id,
                    file_path="/imports/set-a/Abattoir/Abattoir 001.cbz",
                    file_name="Abattoir 001.cbz",
                    file_size=1024,
                    file_format="cbz",
                ),
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=second.id,
                    file_path="/imports/set-b/Abattoir/Abattoir 002.cbz",
                    file_name="Abattoir 002.cbz",
                    file_size=2048,
                    file_format="cbz",
                ),
            ]
        )
        await db_session.flush()

        await svc._consolidate_logical_series_groups(db_session, job)

        merged_rows = list(
            (
                await db_session.execute(
                    select(ImportedSeries)
                    .where(ImportedSeries.import_job_id == job.id)
                    .order_by(ImportedSeries.id.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(merged_rows) == 1
        merged = merged_rows[0]
        assert merged.file_count == 2
        assert merged.files_total == 2
        assert set(merged.diagnostics["source_folders"]) == {
            "/imports/set-a/Abattoir",
            "/imports/set-b/Abattoir",
        }

        file_rows = list(
            (
                await db_session.execute(
                    select(ImportedFile).where(ImportedFile.import_job_id == job.id)
                )
            )
            .scalars()
            .all()
        )
        assert {item.import_series_id for item in file_rows} == {merged.id}

    @pytest.mark.asyncio
    async def test_consolidates_matched_series_with_scanner_suffix_by_cv_id(
        self,
        db_session: AsyncSession,
    ) -> None:
        job = await _create_job_row(db_session, status=ImportJobStatus.MATCHING)
        svc = _make_service()

        first = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Necronomicon",
            raw_year=2008,
            status=ImportSeriesStatus.MATCHED,
            cv_id=22863,
            cv_title="Necronomicon",
            cv_match_score=1.0,
            cv_match_method="exact_title_year",
            source_folder="/imports/test7",
            sample_paths=["/imports/test7/Necronomicon 001.cbz"],
            file_count=1,
            files_total=1,
        )
        second = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Necronomicon (TanCombs)",
            raw_year=2008,
            status=ImportSeriesStatus.MATCHED,
            cv_id=22863,
            cv_title="Necronomicon",
            cv_match_score=0.8891,
            cv_match_method="fuzzy_title",
            source_folder="/imports/test7",
            sample_paths=["/imports/test7/Necronomicon 004.cbz"],
            file_count=1,
            files_total=1,
        )
        db_session.add_all([first, second])
        await db_session.flush()

        db_session.add_all(
            [
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=first.id,
                    file_path="/imports/test7/Necronomicon 001.cbz",
                    file_name="Necronomicon 001.cbz",
                    file_size=1024,
                    file_format="cbz",
                ),
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=second.id,
                    file_path="/imports/test7/Necronomicon 004.cbz",
                    file_name="Necronomicon 004.cbz",
                    file_size=2048,
                    file_format="cbz",
                ),
            ]
        )
        await db_session.flush()

        await svc._consolidate_logical_series_groups(db_session, job)

        merged_rows = list(
            (
                await db_session.execute(
                    select(ImportedSeries)
                    .where(ImportedSeries.import_job_id == job.id)
                    .order_by(ImportedSeries.id.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(merged_rows) == 1
        merged = merged_rows[0]
        assert merged.raw_series_name == "Necronomicon"
        assert merged.file_count == 2
        assert merged.files_total == 2
        assert set(merged.diagnostics["merged_from_series_ids"]) == {first.id, second.id}

        file_rows = list(
            (
                await db_session.execute(
                    select(ImportedFile).where(ImportedFile.import_job_id == job.id)
                )
            )
            .scalars()
            .all()
        )
        assert {item.import_series_id for item in file_rows} == {merged.id}

    @pytest.mark.asyncio
    async def test_does_not_consolidate_same_cv_when_suffix_is_series_type(
        self,
        db_session: AsyncSession,
    ) -> None:
        job = await _create_job_row(db_session, status=ImportJobStatus.MATCHING)
        svc = _make_service()

        first = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Batman",
            raw_year=2025,
            status=ImportSeriesStatus.MATCHED,
            cv_id=12345,
            cv_title="Batman",
            cv_match_score=1.0,
            cv_match_method="exact_title_year",
            source_folder="/imports/test/Batman",
            file_count=1,
            files_total=1,
        )
        second = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Batman Annual",
            raw_year=2025,
            status=ImportSeriesStatus.MATCHED,
            cv_id=12345,
            cv_title="Batman",
            cv_match_score=0.88,
            cv_match_method="fuzzy_title",
            source_folder="/imports/test/Batman Annual",
            file_count=1,
            files_total=1,
        )
        db_session.add_all([first, second])
        await db_session.flush()

        await svc._consolidate_logical_series_groups(db_session, job)

        remaining_rows = list(
            (
                await db_session.execute(
                    select(ImportedSeries)
                    .where(ImportedSeries.import_job_id == job.id)
                    .order_by(ImportedSeries.raw_series_name.asc())
                )
            )
            .scalars()
            .all()
        )
        assert [row.raw_series_name for row in remaining_rows] == [
            "Batman",
            "Batman Annual",
        ]

    @pytest.mark.asyncio
    async def test_consolidates_duplicate_series_by_existing_series_id(
        self,
        db_session: AsyncSession,
    ) -> None:
        existing_series = Series(
            title="Absolute Martian Manhunter",
            sort_title="absolute martian manhunter",
            year_start=2025,
            comicvine_id=111111,
        )
        db_session.add(existing_series)
        await db_session.flush()

        job = await _create_job_row(db_session, status=ImportJobStatus.MATCHING)
        svc = _make_service()

        first = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Absolute Martian Manhunter",
            raw_year=2025,
            status=ImportSeriesStatus.DUPLICATE,
            series_id=existing_series.id,
            source_folder="/imports/a/Absolute Martian Manhunter",
            file_count=1,
            files_total=1,
        )
        second = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Absolute Martian Manhunter",
            raw_year=2025,
            status=ImportSeriesStatus.DUPLICATE,
            series_id=existing_series.id,
            source_folder="/imports/b/Absolute Martian Manhunter",
            file_count=1,
            files_total=1,
        )
        db_session.add_all([first, second])
        await db_session.flush()

        db_session.add_all(
            [
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=first.id,
                    file_path="/imports/a/Absolute Martian Manhunter/001.cbz",
                    file_name="Absolute Martian Manhunter 001.cbz",
                    file_size=1024,
                    file_format="cbz",
                ),
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=second.id,
                    file_path="/imports/b/Absolute Martian Manhunter/002.cbz",
                    file_name="Absolute Martian Manhunter 002.cbz",
                    file_size=2048,
                    file_format="cbz",
                ),
            ]
        )
        await db_session.flush()

        await svc._consolidate_logical_series_groups(db_session, job)

        merged_rows = list(
            (
                await db_session.execute(
                    select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(merged_rows) == 1
        merged = merged_rows[0]
        assert merged.series_id == existing_series.id
        assert merged.file_count == 2
        assert set(merged.diagnostics["source_folders"]) == {
            "/imports/a/Absolute Martian Manhunter",
            "/imports/b/Absolute Martian Manhunter",
        }

    @pytest.mark.asyncio
    async def test_create_job_rejects_existing_active_import(
        self, db_session: AsyncSession, service: ImportService, tmp_path: object
    ) -> None:
        """Only one import job may be active, including review-state jobs."""
        await _create_job_row(db_session, status=ImportJobStatus.REVIEW)

        request = ImportJobCreate(
            source_path=str(tmp_path),
            source_type=ImportSourceType.FILESYSTEM,
        )

        with pytest.raises(ValidationError, match="Only one import can be active"):
            await service.create_job(db_session, request)

    @pytest.mark.asyncio
    async def test_create_job_persists_advanced_options(
        self, db_session: AsyncSession, service: ImportService, tmp_path: object
    ) -> None:
        """Advanced scan options are persisted to the job."""
        source = str(tmp_path)
        request = ImportJobCreate(
            source_path=source,
            source_type=ImportSourceType.FILESYSTEM,
            cv_match_threshold=0.85,
            min_files_per_series=3,
            file_formats="cbz, cbr",
        )
        job = await service.create_job(db_session, request)

        assert job.cv_match_threshold == 0.85
        assert job.min_files_per_series == 3
        assert job.file_formats == "cbz, cbr"

    @pytest.mark.asyncio
    async def test_create_job_advanced_options_defaults(
        self, db_session: AsyncSession, service: ImportService, tmp_path: object
    ) -> None:
        """Advanced scan options use defaults when not specified."""
        source = str(tmp_path)
        request = ImportJobCreate(
            source_path=source,
            source_type=ImportSourceType.FILESYSTEM,
        )
        job = await service.create_job(db_session, request)

        assert job.cv_match_threshold == 0.70
        assert job.min_files_per_series == 1
        assert job.file_formats is None


# ── Test: start_scan ─────────────────────────────────────────────────────


class TestStartScan:
    """Test start_scan() — transitions job through SCANNING → ANALYZING → MATCHING → REVIEW."""

    @pytest.mark.asyncio
    async def test_scan_filesystem_persists_results(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Filesystem scan discovers series and persists ImportedSeries rows."""
        job = await _create_job_row(db_session, source_path="/tmp/comics")

        discovered = [
            _make_discovered(name="Batman", year=2016),
            _make_discovered(name="Saga", year=2012, source_folder="/comics/Saga (2012)"),
        ]

        async def mock_scan(root):
            for d in discovered:
                yield d

        with (
            patch("pullbox.services.import_service.CollectionScanner") as mock_scanner,
            patch(
                "pullbox.services.import_service.evaluate_comicvine_match",
                return_value=_no_match_evaluation(raw_name="Batman", raw_year=2016),
            ),
        ):
            mock_scanner.return_value.inventory = AsyncMock(
                return_value=ScanInventory(directory_count=2, file_count=10)
            )
            mock_scanner.return_value.scan = mock_scan
            await service.start_scan(db_session, job.id)

        await db_session.refresh(job, ["series"])
        assert job.status == ImportJobStatus.REVIEW
        assert job.series_found == 2
        assert len(job.series) == 2
        assert job.scan_started_at is not None
        assert job.scan_completed_at is not None

    @pytest.mark.asyncio
    async def test_scan_mylar3_uses_reader(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Mylar3 source uses Mylar3Reader.read_series()."""
        job = await _create_job_row(
            db_session,
            source_path="/tmp/mylar.db",
            source_type=ImportSourceType.MYLAR3,
        )
        job.mylar3_path_map_confirmed = True

        discovered = [
            _make_discovered(name="Batman", year=2016, mylar3_cv_id=97508),
        ]

        with (
            patch("pullbox.services.import_service.Mylar3Reader") as mock_reader,
            patch(
                "pullbox.services.import_service.evaluate_comicvine_match",
                return_value=_no_match_evaluation(raw_name="Batman", raw_year=2016),
            ),
        ):
            mock_reader.return_value.read_series = AsyncMock(return_value=discovered)
            await service.start_scan(db_session, job.id)

        await db_session.refresh(job, ["series"])
        assert job.status == ImportJobStatus.REVIEW
        assert job.series_found == 1

    @pytest.mark.asyncio
    async def test_scan_progress_callback(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Progress callback is invoked during scan."""
        job = await _create_job_row(db_session)
        progress_events: list[object] = []

        async def capture_progress(event: object) -> None:
            progress_events.append(event)

        async def mock_scan(root):
            yield _make_discovered()

        with (
            patch("pullbox.services.import_service.CollectionScanner") as mock_scanner,
            patch(
                "pullbox.services.import_service.evaluate_comicvine_match",
                return_value=_no_match_evaluation(raw_name="Batman", raw_year=2016),
            ),
        ):
            mock_scanner.return_value.inventory = AsyncMock(
                return_value=ScanInventory(directory_count=1, file_count=5)
            )
            mock_scanner.return_value.scan = mock_scan
            await service.start_scan(db_session, job.id, progress_callback=capture_progress)

        assert len(progress_events) > 0

    @pytest.mark.asyncio
    async def test_scan_persists_live_scan_totals_and_series_count(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Filesystem scans persist live totals so other sessions can observe progress."""
        job = await _create_job_row(db_session)
        progress_events: list[object] = []

        async def capture_progress(event: object) -> None:
            progress_events.append(event)

        class ScannerDouble:
            def __init__(self, *args, progress_callback=None, **kwargs) -> None:
                self._progress_callback = progress_callback

            async def inventory(self, root):
                return ScanInventory(directory_count=3, file_count=12)

            async def scan(self, root):
                if self._progress_callback is not None:
                    await self._progress_callback(12, 3)
                yield _make_discovered()

        with (
            patch("pullbox.services.import_service.CollectionScanner", ScannerDouble),
            patch(
                "pullbox.services.import_service.evaluate_comicvine_match",
                return_value=_no_match_evaluation(raw_name="Batman", raw_year=2016),
            ),
        ):
            await service.start_scan(db_session, job.id, progress_callback=capture_progress)

        assert job.scan_total_files == 12
        assert job.scan_total_dirs == 3
        assert job.series_found == 1
        assert any(
            getattr(event, "scan_total_files", None) == 12
            and getattr(event, "scan_total_dirs", None) == 3
            for event in progress_events
        )

    @pytest.mark.asyncio
    async def test_real_collection_scan_with_progress_callback_avoids_flush_reentrancy(
        self,
        db_session: AsyncSession,
        service: ImportService,
        tmp_path: Path,
    ) -> None:
        """Real filesystem scans can stream progress without re-entering session flushes."""
        for series_name in ("Batman", "Saga"):
            folder = tmp_path / f"{series_name} (2016)"
            folder.mkdir(parents=True, exist_ok=True)
            archive_path = folder / f"{series_name} 001.cbz"
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("page-001.jpg", b"scan-test")

        job = await _create_job_row(db_session, source_path=str(tmp_path))
        progress_events: list[object] = []

        async def capture_progress(event: object) -> None:
            progress_events.append(event)

        with patch(
            "pullbox.services.import_service.evaluate_comicvine_match",
            return_value=_no_match_evaluation(raw_name="Batman", raw_year=2016),
        ):
            await service.start_scan(db_session, job.id, progress_callback=capture_progress)

        assert job.status == ImportJobStatus.REVIEW
        assert job.series_found == 2
        assert progress_events

    @pytest.mark.asyncio
    async def test_scan_failure_marks_job_failed(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """If scan raises, job transitions to FAILED and re-raises the original error."""
        job = await _create_job_row(db_session)

        async def failing_scan(root):
            raise RuntimeError("Disk error")
            yield  # unreachable, but makes this a generator

        with patch("pullbox.services.import_service.CollectionScanner") as mock_scanner:
            mock_scanner.return_value.inventory = AsyncMock(
                return_value=ScanInventory(directory_count=0, file_count=0)
            )
            mock_scanner.return_value.scan = failing_scan
            with pytest.raises(RuntimeError, match="Disk error"):
                await service.start_scan(db_session, job.id)

        assert job.status == ImportJobStatus.FAILED
        assert "Disk error" in (job.error_message or "")

    @pytest.mark.asyncio
    async def test_scan_failure_preserves_original_flush_error_message(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Failure cleanup should not mask the original session/flush exception."""
        job = await _create_job_row(db_session)

        async def failing_scan(root):
            raise InvalidRequestError("Session is already flushing")
            yield  # unreachable, but makes this a generator

        with patch("pullbox.services.import_service.CollectionScanner") as mock_scanner:
            mock_scanner.return_value.inventory = AsyncMock(
                return_value=ScanInventory(directory_count=0, file_count=0)
            )
            mock_scanner.return_value.scan = failing_scan
            with pytest.raises(InvalidRequestError, match="Session is already flushing"):
                await service.start_scan(db_session, job.id)

        assert job.status == ImportJobStatus.FAILED
        assert job.error_message == "Session is already flushing"

    @pytest.mark.asyncio
    async def test_scan_debug_slow_mode_applies_phase_and_item_delays(
        self,
        db_session: AsyncSession,
        mock_series_service: AsyncMock,
        mock_metadata_service: AsyncMock,
        mock_event_bus: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Dev slow mode delays scan-phase and per-item progress updates."""
        monkeypatch.setenv("PULLBOX_IMPORT_DEBUG_SLOW_MODE", "true")
        monkeypatch.setenv("PULLBOX_IMPORT_DEBUG_PHASE_DELAY_SECONDS", "0.01")
        monkeypatch.setenv("PULLBOX_IMPORT_DEBUG_ITEM_DELAY_SECONDS", "0.01")
        get_settings.cache_clear()

        try:
            svc = ImportService(
                series_service=mock_series_service,
                metadata_service=mock_metadata_service,
                event_bus=mock_event_bus,
            )
            svc._maybe_slow_phase_delay = AsyncMock()
            svc._maybe_slow_item_delay = AsyncMock()

            job = await _create_job_row(db_session)

            async def capture_progress(_event: object) -> None:
                return None

            async def mock_scan(root):
                yield _make_discovered()

            with (
                patch("pullbox.services.import_service.CollectionScanner") as mock_scanner,
                patch(
                    "pullbox.services.import_service.evaluate_comicvine_match",
                    return_value=_no_match_evaluation(raw_name="Batman", raw_year=2016),
                ),
            ):
                mock_scanner.return_value.inventory = AsyncMock(
                    return_value=ScanInventory(directory_count=1, file_count=5)
                )
                mock_scanner.return_value.scan = mock_scan
                await svc.start_scan(db_session, job.id, progress_callback=capture_progress)

            assert svc._maybe_slow_phase_delay.await_count == 5
            assert svc._maybe_slow_item_delay.await_count == 1
        finally:
            get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_scan_commits_scanning_state_for_concurrent_observers(
        self,
        async_engine: object,
        db_session: AsyncSession,
        mock_series_service: AsyncMock,
        mock_metadata_service: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """The active scan phase is committed early so concurrent cancel can see it."""
        svc = ImportService(
            series_service=mock_series_service,
            metadata_service=mock_metadata_service,
            event_bus=mock_event_bus,
        )
        job = await _create_job_row(db_session)
        observer_factory = async_sessionmaker(async_engine, expire_on_commit=False)
        observed_statuses: list[ImportJobStatus] = []

        async def capture_progress(_event: object) -> None:
            if observed_statuses:
                return
            async with observer_factory() as observer_session:
                observed = await observer_session.get(ImportJob, job.id)
                assert observed is not None
                observed_statuses.append(observed.status)

        async def mock_scan(root):
            yield _make_discovered()

        with (
            patch("pullbox.services.import_service.CollectionScanner") as mock_scanner,
            patch(
                "pullbox.services.import_service.evaluate_comicvine_match",
                return_value=_no_match_evaluation(raw_name="Batman", raw_year=2016),
            ),
        ):
            mock_scanner.return_value.inventory = AsyncMock(
                return_value=ScanInventory(directory_count=1, file_count=5)
            )
            mock_scanner.return_value.scan = mock_scan
            await svc.start_scan(db_session, job.id, progress_callback=capture_progress)

        assert observed_statuses
        assert observed_statuses[0] == ImportJobStatus.SCANNING


class TestResumeScanPhase:
    """Test phase-aware recovery for interrupted Step 2 jobs."""

    @pytest.mark.asyncio
    async def test_analyzing_resume_does_not_restart_scan(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """ANALYZING resumes duplicate analysis directly and continues forward."""
        job = await _create_job_row(db_session, status=ImportJobStatus.ANALYZING)
        job.progress_snapshot = {
            "status": ImportJobStatus.ANALYZING.value,
            "mode": "scan",
            "phase": "analyzing",
        }
        await db_session.commit()

        service._metadata_service = None
        service.start_scan = AsyncMock()
        service._deduplicate_series = AsyncMock()
        service._run_matching = AsyncMock()
        service._consolidate_logical_series_groups = AsyncMock()
        service._run_file_matching = AsyncMock()
        progress_events: list[ImportProgressEvent] = []

        async def capture_progress(event: ImportProgressEvent) -> None:
            progress_events.append(event)

        await service.resume_scan_phase(
            db_session,
            job.id,
            progress_callback=capture_progress,
        )

        await db_session.refresh(job)
        assert job.status == ImportJobStatus.REVIEW
        service.start_scan.assert_not_called()
        service._deduplicate_series.assert_awaited_once()
        service._run_matching.assert_awaited_once()
        service._consolidate_logical_series_groups.assert_awaited_once()
        service._run_file_matching.assert_awaited_once()
        assert [event.phase for event in progress_events] == [
            "analyzing",
            "matching",
            "file_matching",
            "review",
        ]

    @pytest.mark.asyncio
    async def test_file_matching_resume_does_not_restart_scan(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """FILE_MATCHING resumes file matching directly and moves to review."""
        job = await _create_job_row(db_session, status=ImportJobStatus.FILE_MATCHING)
        job.progress_snapshot = {
            "status": ImportJobStatus.FILE_MATCHING.value,
            "mode": "scan",
            "phase": "file_matching",
        }
        await db_session.commit()

        service._metadata_service = None
        service._run_file_matching = AsyncMock()
        progress_events: list[ImportProgressEvent] = []

        async def capture_progress(event: ImportProgressEvent) -> None:
            progress_events.append(event)

        await service.resume_scan_phase(
            db_session,
            job.id,
            progress_callback=capture_progress,
        )

        await db_session.refresh(job)
        assert job.status == ImportJobStatus.REVIEW
        service._run_file_matching.assert_awaited_once()
        assert [event.phase for event in progress_events] == ["file_matching", "review"]

    @pytest.mark.asyncio
    async def test_matching_resume_finishes_matching_then_file_matching(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """MATCHING resumes series matching before continuing to file matching."""
        job = await _create_job_row(db_session, status=ImportJobStatus.MATCHING)
        job.progress_snapshot = {
            "status": ImportJobStatus.MATCHING.value,
            "mode": "scan",
            "phase": "matching",
        }
        await db_session.commit()

        service._metadata_service = None
        service._run_matching = AsyncMock()
        service._consolidate_logical_series_groups = AsyncMock()
        service._run_file_matching = AsyncMock()
        progress_events: list[ImportProgressEvent] = []

        async def capture_progress(event: ImportProgressEvent) -> None:
            progress_events.append(event)

        await service.resume_scan_phase(
            db_session,
            job.id,
            progress_callback=capture_progress,
        )

        await db_session.refresh(job)
        assert job.status == ImportJobStatus.REVIEW
        assert job.match_completed_at is not None
        service._run_matching.assert_awaited_once()
        service._consolidate_logical_series_groups.assert_awaited_once()
        service._run_file_matching.assert_awaited_once()
        assert [event.phase for event in progress_events] == [
            "matching",
            "file_matching",
            "review",
        ]


class TestDebugSlowMode:
    """Test env-gated debug delays used by import progress screens."""

    @pytest.mark.asyncio
    async def test_phase_delay_respects_env(
        self,
        mock_series_service: AsyncMock,
        mock_metadata_service: AsyncMock,
        mock_event_bus: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Phase delay sleeps only when slow mode is enabled."""
        monkeypatch.setenv("PULLBOX_IMPORT_DEBUG_SLOW_MODE", "true")
        monkeypatch.setenv("PULLBOX_IMPORT_DEBUG_PHASE_DELAY_SECONDS", "0.25")
        get_settings.cache_clear()

        try:
            svc = ImportService(
                series_service=mock_series_service,
                metadata_service=mock_metadata_service,
                event_bus=mock_event_bus,
            )
            sleep_mock = AsyncMock()
            with patch("pullbox.services.import_service.asyncio.sleep", sleep_mock):
                await svc._maybe_slow_phase_delay()
            sleep_mock.assert_awaited_once_with(0.25)
        finally:
            get_settings.cache_clear()


# ── Test: _run_analysis (deduplication) ──────────────────────────────────


class TestRunAnalysis:
    """Test deduplication during analysis phase."""

    @pytest.mark.asyncio
    async def test_deduplication_tags_duplicates(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Series matching existing library entries are tagged DUPLICATE."""
        # Create an existing series in the library
        existing = Series(
            title="Batman",
            sort_title="batman",
            year_start=2016,
            comicvine_id=97508,
        )
        session = db_session
        session.add(existing)
        await session.flush()

        job = await _create_job_row(session)

        # Add a discovered series that matches
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Batman",
            raw_year=2016,
            status=ImportSeriesStatus.PENDING,
            file_count=5,
        )
        session.add(item)
        await session.flush()

        await service._deduplicate_series(session, job)

        await session.refresh(item)
        assert item.status == ImportSeriesStatus.DUPLICATE
        assert item.cv_match_score == 1.0

    @pytest.mark.asyncio
    async def test_deduplication_uses_issue_target_when_file_year_is_release_year(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Ongoing exact-title series should dedupe even when file year is an issue year."""
        existing = Series(
            title="Absolute Wonder Woman",
            sort_title="absolute wonder woman",
            year_start=2024,
            comicvine_id=160511,
            issue_count=20,
        )
        db_session.add(existing)
        await db_session.flush()
        db_session.add(
            Issue(
                series_id=existing.id,
                comicvine_id=1160262,
                issue_number=18.0,
                title="Season of the Witch, Part 3 of 5",
                release_date=date(2026, 5, 1),
                status=IssueStatus.SKIPPED,
            )
        )
        await db_session.flush()

        job = await _create_job_row(db_session)
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Absolute Wonder Woman",
            raw_year=2026,
            status=ImportSeriesStatus.PENDING,
            file_count=1,
            diagnostics={"source_issue_type": IssueType.ISSUE.value},
        )
        db_session.add(item)
        await db_session.flush()
        imp_file = await _create_imported_file(
            db_session,
            job,
            item,
            file_name="Absolute Wonder Woman 018 (2026) (Digital).cbz",
            status=ImportedFileStatus.PENDING,
        )
        imp_file.parsed_issue_number = 18.0
        imp_file.parsed_year = 2026
        await db_session.flush()

        await service._deduplicate_series(db_session, job)

        await db_session.refresh(item)
        assert item.status == ImportSeriesStatus.DUPLICATE
        assert item.series_id == existing.id
        assert item.cv_id == 160511
        assert item.cv_match_score == 1.0

    @pytest.mark.asyncio
    async def test_deduplication_rejects_exact_title_issue_target_with_conflicting_year(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Issue-target fallback must not collapse onto an older same-title volume."""
        existing = Series(
            title="Wonder Woman",
            sort_title="wonder woman",
            year_start=2016,
            comicvine_id=9001,
            issue_count=83,
        )
        db_session.add(existing)
        await db_session.flush()
        db_session.add(
            Issue(
                series_id=existing.id,
                comicvine_id=900118,
                issue_number=18.0,
                title="Older volume issue",
                release_date=date(2017, 9, 1),
                status=IssueStatus.SKIPPED,
            )
        )
        await db_session.flush()

        job = await _create_job_row(db_session)
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Wonder Woman",
            raw_year=2025,
            status=ImportSeriesStatus.PENDING,
            file_count=1,
            diagnostics={"source_issue_type": IssueType.ISSUE.value},
        )
        db_session.add(item)
        await db_session.flush()
        imp_file = await _create_imported_file(
            db_session,
            job,
            item,
            file_name="Wonder Woman 018 (2025) (Digital).cbz",
            status=ImportedFileStatus.PENDING,
        )
        imp_file.parsed_issue_number = 18.0
        imp_file.parsed_year = 2025
        await db_session.flush()

        await service._deduplicate_series(db_session, job)

        await db_session.refresh(item)
        assert item.status == ImportSeriesStatus.PENDING
        assert item.series_id is None

    @pytest.mark.asyncio
    async def test_deduplication_cv_id_match(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Mylar3 CV ID matching existing Series.comicvine_id → DUPLICATE."""
        existing = Series(
            title="Batman",
            sort_title="batman",
            year_start=2016,
            comicvine_id=97508,
        )
        session = db_session
        session.add(existing)
        await session.flush()

        job = await _create_job_row(session)
        # This item has a mylar3_cv_id that doesn't match existing name but has same cv_id
        # The raw_series_name is stored on ImportedSeries, not mylar3_cv_id directly
        # We need to use source_folder or sample_paths metadata for mylar3_cv_id
        # Actually, looking at the spec, we store cv_id during scan from mylar3_cv_id
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Batman Rebirth",
            raw_year=2016,
            status=ImportSeriesStatus.PENDING,
            file_count=5,
            cv_id=97508,  # From Mylar3 CV ID — stored during scan
        )
        session.add(item)
        await session.flush()

        await service._deduplicate_series(session, job)

        await session.refresh(item)
        assert item.status == ImportSeriesStatus.DUPLICATE

    @pytest.mark.asyncio
    async def test_deduplication_skips_collection_library_match_for_standard_source(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Standard issue imports should not collapse onto same-title TPB library series."""
        existing = Series(
            title="Absolute Martian Manhunter",
            sort_title="absolute martian manhunter",
            year_start=2025,
            comicvine_id=168590,
            series_type=SeriesType.TPB,
        )
        db_session.add(existing)
        await db_session.flush()

        job = await _create_job_row(db_session)
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Absolute Martian Manhunter",
            raw_year=2025,
            status=ImportSeriesStatus.PENDING,
            file_count=10,
            diagnostics={"source_issue_type": IssueType.ISSUE.value},
        )
        db_session.add(item)
        await db_session.flush()

        await service._deduplicate_series(db_session, job)

        await db_session.refresh(item)
        assert item.status == ImportSeriesStatus.PENDING
        assert item.series_id is None

    @pytest.mark.asyncio
    async def test_deduplication_skips_subtitle_spinoff_for_base_series(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """A base-series import should not dedupe onto a subtitle/spinoff library series."""
        existing = Series(
            title="Teenage Mutant Ninja Turtles: Shredder",
            sort_title="teenage mutant ninja turtles shredder",
            year_start=2025,
            comicvine_id=166458,
            issue_count=8,
            series_type=SeriesType.STANDARD,
        )
        db_session.add(existing)
        await db_session.flush()

        job = await _create_job_row(db_session)
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Teenage Mutant Ninja Turtles",
            raw_year=2024,
            status=ImportSeriesStatus.PENDING,
            file_count=1,
            diagnostics={"source_issue_type": IssueType.ISSUE.value},
        )
        db_session.add(item)
        await db_session.flush()

        await service._deduplicate_series(db_session, job)

        await db_session.refresh(item)
        assert item.status == ImportSeriesStatus.PENDING
        assert item.series_id is None
        assert item.cv_id is None

    @pytest.mark.asyncio
    async def test_deduplication_skips_expanded_import_title_for_base_series(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Sampler/anthology-like title expansions should not dedupe onto the base series."""
        existing = Series(
            title="Seven Wives",
            sort_title="seven wives",
            year_start=2026,
            comicvine_id=172012,
            issue_count=1,
            series_type=SeriesType.STANDARD,
        )
        db_session.add(existing)
        await db_session.flush()

        job = await _create_job_row(db_session)
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Seven Wives IDW Crime Sampler",
            raw_year=2026,
            status=ImportSeriesStatus.PENDING,
            file_count=1,
            diagnostics={"source_issue_type": IssueType.ISSUE.value},
        )
        db_session.add(item)
        await db_session.flush()

        await service._deduplicate_series(db_session, job)

        await db_session.refresh(item)
        assert item.status == ImportSeriesStatus.PENDING
        assert item.series_id is None
        assert item.cv_id is None

    @pytest.mark.asyncio
    async def test_deduplication_keeps_matching_collection_library_series(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Collection imports with matching type signals still dedupe against the library."""
        publisher = Publisher(name="DC Comics", comicvine_id=10)
        db_session.add(publisher)
        await db_session.flush()
        existing = Series(
            title="Absolute Martian Manhunter",
            sort_title="absolute martian manhunter",
            year_start=2025,
            comicvine_id=168590,
            publisher_id=publisher.id,
            issue_count=6,
            comicvine_url="https://comicvine.gamespot.com/absolute-martian-manhunter/4050-168590/",
            series_type=SeriesType.TPB,
        )
        db_session.add(existing)
        await db_session.flush()

        job = await _create_job_row(db_session)
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Absolute Martian Manhunter",
            raw_year=2025,
            status=ImportSeriesStatus.PENDING,
            file_count=1,
            diagnostics={"source_issue_type": IssueType.TPB.value},
        )
        db_session.add(item)
        await db_session.flush()

        await service._deduplicate_series(db_session, job)

        await db_session.refresh(item)
        assert item.status == ImportSeriesStatus.DUPLICATE
        assert item.series_id == existing.id
        assert item.cv_id == 168590
        assert item.cv_title == "Absolute Martian Manhunter"
        assert item.cv_year == 2025
        assert item.cv_publisher == "DC Comics"
        assert item.cv_issue_count == 6
        assert item.cv_match_score == 1.0
        assert (
            item.cv_url == "https://comicvine.gamespot.com/absolute-martian-manhunter/4050-168590/"
        )

    @pytest.mark.asyncio
    async def test_non_duplicate_stays_pending(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Non-duplicate series stays PENDING after deduplication."""
        job = await _create_job_row(db_session)
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Saga",
            raw_year=2012,
            status=ImportSeriesStatus.PENDING,
            file_count=10,
        )
        db_session.add(item)
        await db_session.flush()

        await service._deduplicate_series(db_session, job)

        await db_session.refresh(item)
        assert item.status == ImportSeriesStatus.PENDING


# ── Test: _run_matching ──────────────────────────────────────────────────


class TestRunMatching:
    """Test CV matching during the matching phase."""

    @pytest.mark.asyncio
    async def test_match_updates_cv_fields(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Successful CV match populates cv_* fields and sets MATCHED."""
        job = await _create_job_row(db_session, source_type=ImportSourceType.MYLAR3)
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Batman",
            raw_year=2016,
            status=ImportSeriesStatus.PENDING,
            file_count=5,
        )
        db_session.add(item)
        await db_session.flush()

        cv_result = {
            "cv_id": 97508,
            "cv_title": "Batman",
            "cv_year": 2016,
            "cv_publisher": "DC Comics",
            "cv_issue_count": 85,
            "cv_url": "https://comicvine.gamespot.com/batman/4050-97508/",
            "cv_match_score": 0.98,
            "cv_match_method": "exact_title_year",
        }

        with patch(
            "pullbox.services.import_service.evaluate_comicvine_match",
            return_value=_matched_evaluation(cv_result),
        ):
            await service._run_matching(db_session, job)

        await db_session.refresh(item)
        assert item.status == ImportSeriesStatus.MATCHED
        assert item.cv_id == 97508
        assert item.cv_title == "Batman"
        assert item.cv_match_score == 0.98

    @pytest.mark.asyncio
    async def test_match_reclassifies_existing_library_series_as_duplicate(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """A post-match CV-id hit against the library should become DUPLICATE, not MATCHED."""
        existing = Series(
            title="About Betty's Boob",
            sort_title="about betty's boob",
            year_start=2018,
            comicvine_id=111396,
            series_type=SeriesType.GRAPHIC_NOVEL,
        )
        db_session.add(existing)
        await db_session.flush()

        job = await _create_job_row(db_session, source_type=ImportSourceType.MYLAR3)
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="About Betty's Boob",
            raw_year=2018,
            status=ImportSeriesStatus.PENDING,
            file_count=1,
        )
        db_session.add(item)
        await db_session.flush()

        cv_result = {
            "cv_id": 111396,
            "cv_title": "About Betty's Boob",
            "cv_year": 2018,
            "cv_publisher": "Archaia",
            "cv_issue_count": 1,
            "cv_url": "https://comicvine.gamespot.com/about-bettys-boob/4050-111396/",
            "cv_match_score": 1.0,
            "cv_match_method": "exact_title_year",
        }

        with patch(
            "pullbox.services.import_service.evaluate_comicvine_match",
            return_value=_matched_evaluation(cv_result),
        ):
            await service._run_matching(db_session, job)

        await db_session.refresh(item)
        assert item.status == ImportSeriesStatus.DUPLICATE
        assert item.series_id == existing.id
        assert item.cv_id == 111396
        assert item.diagnostics["duplicate_reason"] == "cv_id"

    @pytest.mark.asyncio
    async def test_no_match_sets_no_match(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """No CV match sets status to NO_MATCH."""
        job = await _create_job_row(db_session, source_type=ImportSourceType.MYLAR3)
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Unknown Series",
            raw_year=None,
            status=ImportSeriesStatus.PENDING,
            file_count=3,
        )
        db_session.add(item)
        await db_session.flush()
        db_session.add_all(
            [
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=item.id,
                    file_path="/tmp/unknown-series-1.cbz",
                    file_name="Unknown Series 001.cbz",
                    file_size=1024,
                    file_format="cbz",
                    parsed_series="Unknown Series",
                    parsed_issue_number=1.0,
                    status=ImportedFileStatus.PENDING,
                ),
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=item.id,
                    file_path="/tmp/unknown-series-2.cbz",
                    file_name="Unknown Series 002.cbz",
                    file_size=2048,
                    file_format="cbz",
                    parsed_series="Unknown Series",
                    parsed_issue_number=2.0,
                    status=ImportedFileStatus.PENDING,
                ),
            ]
        )
        await db_session.flush()

        with patch(
            "pullbox.services.import_service.evaluate_comicvine_match",
            return_value=_no_match_evaluation(
                raw_name="Unknown Series",
                top_candidates=[
                    {
                        "title": "Known Series",
                        "score_pct": 64,
                        "rejection_reasons": ["Below 70% threshold"],
                    }
                ],
            ),
        ):
            await service._run_matching(db_session, job)

        await db_session.refresh(item)
        assert item.status == ImportSeriesStatus.NO_MATCH
        assert item.diagnostics["kind"] == "series_no_match"
        assert item.diagnostics["top_candidates"][0]["title"] == "Known Series"
        file_rows = (
            (
                await db_session.execute(
                    select(ImportedFile).where(ImportedFile.import_series_id == item.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(file_rows) == 2
        assert all(f.status == ImportedFileStatus.NO_MATCH for f in file_rows)
        assert all(
            dict(f.diagnostics or {}).get("rejection_reason")
            == "Series could not be matched to ComicVine during import review."
            for f in file_rows
        )

        logs = (
            (
                await db_session.execute(
                    select(ImportJobLog).where(ImportJobLog.import_job_id == job.id)
                )
            )
            .scalars()
            .all()
        )
        detail_log = next(log for log in logs if log.event == "import_series_no_match_detail")
        assert detail_log.data["diagnostics"]["kind"] == "series_no_match"


# ── Test: confirm_import ─────────────────────────────────────────────────


class TestConfirmImport:
    """Test confirm_import() — validates and transitions to IMPORTING."""

    @pytest.fixture(autouse=True)
    async def default_managed_root(
        self,
        db_session: AsyncSession,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> LibraryRoot:
        """Give direct review-row fixtures the startup-managed root invariant."""
        root = await db_session.scalar(
            select(LibraryRoot).where(LibraryRoot.is_default_managed_destination.is_(True))
        )
        if root is None:
            root_path = tmp_path / "confirm-managed-root"
            root_path.mkdir()
            root = LibraryRoot(
                name="Confirm managed root",
                path=str(root_path),
                enabled=True,
                allow_referenced_registrations=True,
                allow_managed_writes=True,
                is_default_managed_destination=True,
            )
            db_session.add(root)
            await db_session.flush()
        monkeypatch.setattr(
            library_root_management.shutil,
            "disk_usage",
            lambda _path: SimpleNamespace(free=100 * 1024**3),
        )
        return root

    @pytest.mark.asyncio
    async def test_confirm_valid(self, db_session: AsyncSession, service: ImportService) -> None:
        """Confirm with valid series IDs transitions to IMPORTING."""
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        item = await _create_imported_series(db_session, job, selected_for_import=True)

        request = ConfirmImportRequest(series_ids=[item.id])
        updated_job = await service.confirm_import(db_session, job.id, request)

        assert updated_job.status == ImportJobStatus.IMPORTING
        assert updated_job.progress_snapshot["managed_copy_capacity"]["reserve_bytes"] == 1024**3
        await db_session.refresh(item)
        assert item.status == ImportSeriesStatus.CONFIRMED

    @pytest.mark.asyncio
    async def test_confirm_in_place_split_series_requires_future_destination(
        self,
        db_session: AsyncSession,
        service: ImportService,
        default_managed_root: LibraryRoot,
        tmp_path: Path,
    ) -> None:
        archive_path = tmp_path / "confirm-archive-root"
        archive_path.mkdir()
        archive_root = LibraryRoot(
            name="Confirm archive root",
            path=str(archive_path),
            enabled=True,
            allow_referenced_registrations=True,
            allow_managed_writes=True,
        )
        db_session.add(archive_root)
        await db_session.flush()
        job = await _create_job_row(
            db_session,
            source_path=str(tmp_path),
            source_type=ImportSourceType.MYLAR3,
            status=ImportJobStatus.REVIEW,
            file_handling_mode=ImportFileHandlingMode.IN_PLACE,
        )
        item = await _create_imported_series(
            db_session,
            job,
            selected_for_import=True,
        )
        main_file = await _create_imported_file(
            db_session,
            job,
            item,
            file_name="Batman 001.cbz",
        )
        archive_file = await _create_imported_file(
            db_session,
            job,
            item,
            file_name="Batman 002.cbz",
        )
        main_path = Path(default_managed_root.path) / main_file.file_name
        archive_file_path = archive_path / archive_file.file_name
        main_path.write_bytes(b"main")
        archive_file_path.write_bytes(b"archive")
        main_file.file_path = str(main_path)
        archive_file.file_path = str(archive_file_path)
        await db_session.flush()

        with pytest.raises(ValidationError, match="preferred managed destination"):
            await service.confirm_import(
                db_session,
                job.id,
                ConfirmImportRequest(series_ids=[]),
            )

        assert job.status == ImportJobStatus.REVIEW
        assert item.status == ImportSeriesStatus.MATCHED
        assert item.selected_for_import is True
        assert main_file.status == ImportedFileStatus.MATCHED
        assert archive_file.status == ImportedFileStatus.MATCHED

        confirmed_job = await service.confirm_import(
            db_session,
            job.id,
            ConfirmImportRequest(
                series_ids=[],
                target_library_root_id=default_managed_root.id,
            ),
        )

        assert confirmed_job.status == ImportJobStatus.IMPORTING
        assert confirmed_job.target_library_root_id == default_managed_root.id
        assert item.status == ImportSeriesStatus.CONFIRMED
        assert main_file.status == ImportedFileStatus.CONFIRMED
        assert archive_file.status == ImportedFileStatus.CONFIRMED
        assert main_path.read_bytes() == b"main"
        assert archive_file_path.read_bytes() == b"archive"

    @pytest.mark.asyncio
    async def test_confirm_blocks_insufficient_managed_capacity_and_stays_in_review(
        self,
        db_session: AsyncSession,
        service: ImportService,
        default_managed_root: LibraryRoot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        job.target_library_root_id = default_managed_root.id
        item = await _create_imported_series(db_session, job, selected_for_import=True)
        imp_file = await _create_imported_file(db_session, job, item)
        imp_file.file_size = 20 * 1024**3
        monkeypatch.setattr(
            library_root_management.shutil,
            "disk_usage",
            lambda _path: SimpleNamespace(free=22 * 1024**3 - 1),
        )

        with pytest.raises(ValidationError, match="free space"):
            await service.confirm_import(
                db_session,
                job.id,
                ConfirmImportRequest(series_ids=[item.id]),
            )

        await db_session.refresh(item)
        await db_session.refresh(imp_file)
        assert job.status == ImportJobStatus.REVIEW
        assert item.status == ImportSeriesStatus.MATCHED
        assert item.selected_for_import is True
        assert imp_file.status == ImportedFileStatus.MATCHED
        snapshot = job.progress_snapshot["managed_copy_capacity"]
        assert snapshot["status"] == "insufficient"
        assert snapshot["stage"] == "confirmation"

    @pytest.mark.asyncio
    async def test_confirm_blocks_unknown_managed_capacity_and_stays_in_review(
        self,
        db_session: AsyncSession,
        service: ImportService,
        default_managed_root: LibraryRoot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        job.target_library_root_id = default_managed_root.id
        item = await _create_imported_series(db_session, job, selected_for_import=True)
        await _create_imported_file(db_session, job, item)

        def unknown_capacity(_path: Path) -> None:
            raise OSError("capacity unavailable")

        monkeypatch.setattr(
            library_root_management.shutil,
            "disk_usage",
            unknown_capacity,
        )

        with pytest.raises(ValidationError, match="could not be determined"):
            await service.confirm_import(
                db_session,
                job.id,
                ConfirmImportRequest(series_ids=[item.id]),
            )

        assert job.status == ImportJobStatus.REVIEW
        snapshot = job.progress_snapshot["managed_copy_capacity"]
        assert snapshot["status"] == "unknown"
        assert snapshot["free_bytes"] is None

    @pytest.mark.asyncio
    async def test_confirm_persists_sanitized_managed_capacity_snapshot(
        self,
        db_session: AsyncSession,
        service: ImportService,
        default_managed_root: LibraryRoot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        job.target_library_root_id = default_managed_root.id
        item = await _create_imported_series(db_session, job, selected_for_import=True)
        imp_file = await _create_imported_file(db_session, job, item)
        imp_file.file_size = 20 * 1024**3
        monkeypatch.setattr(
            library_root_management.shutil,
            "disk_usage",
            lambda _path: SimpleNamespace(free=22 * 1024**3),
        )

        updated_job = await service.confirm_import(
            db_session,
            job.id,
            ConfirmImportRequest(series_ids=[item.id]),
        )

        snapshot = updated_job.progress_snapshot["managed_copy_capacity"]
        assert snapshot == {
            "schema_version": 1,
            "stage": "confirmation",
            "target_library_root_id": default_managed_root.id,
            "selected_source_bytes": 20 * 1024**3,
            "reserve_bytes": 2 * 1024**3,
            "required_bytes": 22 * 1024**3,
            "free_bytes": 22 * 1024**3,
            "status": "ready",
        }
        assert default_managed_root.path not in str(snapshot)

    @pytest.mark.asyncio
    async def test_confirm_wrong_state_raises(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """confirm_import on non-REVIEW job raises ValidationError."""
        job = await _create_job_row(db_session, status=ImportJobStatus.SCANNING)

        request = ConfirmImportRequest(series_ids=[1])
        with pytest.raises(ValidationError, match="REVIEW"):
            await service.confirm_import(db_session, job.id, request)

    @pytest.mark.asyncio
    async def test_confirm_ignores_foreign_client_series_ids_when_nothing_is_selected(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Foreign browser ids no longer hijack confirm once selection is server-owned."""
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        other_job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        other_item = await _create_imported_series(db_session, other_job)

        request = ConfirmImportRequest(series_ids=[other_item.id])
        with pytest.raises(ValidationError, match="Select at least one matched series"):
            await service.confirm_import(db_session, job.id, request)

    @pytest.mark.asyncio
    async def test_confirm_ignores_non_matched_client_series_ids_when_nothing_is_selected(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Stale client ids no longer fail confirm; they are ignored unless selected server-side."""
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        no_match_item = await _create_imported_series(
            db_session,
            job,
            status=ImportSeriesStatus.NO_MATCH,
            cv_id=None,
            cv_match_score=None,
            cv_match_method=None,
        )

        request = ConfirmImportRequest(series_ids=[no_match_item.id])
        with pytest.raises(ValidationError, match="Select at least one matched series"):
            await service.confirm_import(db_session, job.id, request)

    @pytest.mark.asyncio
    async def test_confirm_uses_server_selection_even_when_client_posts_stale_ids(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Confirm should honor durable server selection over stale browser ids."""
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        selected_item = await _create_imported_series(
            db_session,
            job,
            selected_for_import=True,
        )
        request = ConfirmImportRequest(series_ids=[99999])

        updated_job = await service.confirm_import(db_session, job.id, request)

        assert updated_job.status == ImportJobStatus.IMPORTING
        await db_session.refresh(selected_item)
        assert selected_item.status == ImportSeriesStatus.CONFIRMED
        assert selected_item.selected_for_import is False

    @pytest.mark.asyncio
    async def test_confirm_applies_global_search_on_add_and_monitored_override(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Confirm uses global search-on-add and still honors monitoring overrides."""
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        item = await _create_imported_series(db_session, job)
        db_session.add(SystemConfig(key="search_on_add_default", value="true", value_type="bool"))
        await db_session.flush()

        request = ConfirmImportRequest(
            series_ids=[item.id],
            monitored=True,
        )
        updated_job = await service.confirm_import(db_session, job.id, request)

        assert updated_job.monitored is True
        assert updated_job.search_on_add is True

    @pytest.mark.asyncio
    async def test_confirm_rejects_conflicting_search_on_add_override(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Deprecated search_on_add overrides that conflict with global policy are rejected."""
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        item = await _create_imported_series(db_session, job)
        db_session.add(SystemConfig(key="search_on_add_default", value="false", value_type="bool"))
        await db_session.flush()

        with pytest.raises(
            ValidationError,
            match="Search on add is now controlled by the global import policy",
        ):
            await service.confirm_import(
                db_session,
                job.id,
                ConfirmImportRequest(series_ids=[item.id], search_on_add=True),
            )

    @pytest.mark.asyncio
    async def test_confirm_persists_transfer_and_convert_settings(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Media-management transfer and conversion settings are captured on confirmation."""
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        item = await _create_imported_series(db_session, job)
        db_session.add(
            SystemConfig(key="post_processing_method", value="copy", value_type="string")
        )
        db_session.add(
            SystemConfig(
                key="convert_to_preferred_format_on_import",
                value="true",
                value_type="bool",
            )
        )
        await db_session.flush()

        updated_job = await service.confirm_import(
            db_session,
            job.id,
            ConfirmImportRequest(series_ids=[item.id]),
        )

        assert updated_job.transfer_method == "copy"
        assert updated_job.convert_to_preferred_format is True
        assert updated_job.effective_transfer_method == "copy"
        assert updated_job.effective_import_strategy == "standard"
        assert updated_job.source_preserved is True

    @pytest.mark.asyncio
    async def test_confirm_persists_torrent_import_strategy_for_auditability(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Import jobs capture the configured torrent strategy even before runtime use."""
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        item = await _create_imported_series(db_session, job)
        db_session.add(
            SystemConfig(
                key="torrent_import_strategy",
                value="seed_safe",
                value_type="string",
            )
        )
        await db_session.flush()

        updated_job = await service.confirm_import(
            db_session,
            job.id,
            ConfirmImportRequest(series_ids=[item.id]),
        )

        assert updated_job.torrent_import_strategy == "seed_safe"
        assert updated_job.effective_import_strategy == "standard"

    @pytest.mark.asyncio
    async def test_confirm_persists_embedded_comicinfo_update_setting(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """The global embedded ComicInfo policy is captured on confirmation."""
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        item = await _create_imported_series(db_session, job)
        db_session.add(
            SystemConfig(key="post_processing_method", value="copy", value_type="string")
        )
        db_session.add(
            SystemConfig(
                key="update_embedded_comicinfo_from_match_on_import",
                value="true",
                value_type="bool",
            )
        )
        await db_session.flush()

        updated_job = await service.confirm_import(
            db_session,
            job.id,
            ConfirmImportRequest(
                series_ids=[item.id],
                update_embedded_comicinfo_from_match=True,
            ),
        )

        assert updated_job.update_embedded_comicinfo_from_match is True

    @pytest.mark.asyncio
    async def test_confirm_rejects_embedded_comicinfo_update_when_not_moving_to_library(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Deprecated move_to_library=false overrides are rejected."""
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        item = await _create_imported_series(db_session, job)

        with pytest.raises(ValidationError, match="no longer supported"):
            await service.confirm_import(
                db_session,
                job.id,
                ConfirmImportRequest(
                    series_ids=[item.id],
                    move_to_library=False,
                    update_embedded_comicinfo_from_match=True,
                ),
            )

    @pytest.mark.asyncio
    async def test_confirm_allows_embedded_comicinfo_update_with_source_preserving_transfer(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Collection imports keep source-safe effective copy for metadata rewrites."""
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        item = await _create_imported_series(db_session, job)
        db_session.add(
            SystemConfig(key="post_processing_method", value="hardlink", value_type="string")
        )
        db_session.add(
            SystemConfig(
                key="update_embedded_comicinfo_from_match_on_import",
                value="true",
                value_type="bool",
            )
        )
        await db_session.flush()

        await service.confirm_import(
            db_session,
            job.id,
            ConfirmImportRequest(
                series_ids=[item.id],
                update_embedded_comicinfo_from_match=True,
            ),
        )

        assert job.transfer_method == "hardlink"
        assert job.effective_transfer_method == "copy"
        assert job.source_preserved is True
        assert job.update_embedded_comicinfo_from_match is True

    @pytest.mark.asyncio
    async def test_confirm_duplicate_file_only_selection_allowed(
        self,
        db_session: AsyncSession,
        service: ImportService,
    ) -> None:
        """Duplicate-series file selections can enter IMPORTING without new-series ids."""
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        target_series = await _create_target_series(db_session)
        duplicate_item = await _create_imported_series(
            db_session,
            job,
            status=ImportSeriesStatus.DUPLICATE,
            cv_match_method="comicinfo_cv_id",
        )
        duplicate_item.series_id = target_series.id
        db_session.add(
            ImportedFile(
                import_job_id=job.id,
                import_series_id=duplicate_item.id,
                file_path="/tmp/Batman 001.cbz",
                file_name="Batman 001.cbz",
                file_size=1024,
                file_format="cbz",
                parsed_issue_number=1.0,
                status=ImportedFileStatus.MATCHED,
                include_in_import=True,
            )
        )
        await db_session.flush()

        updated_job = await service.confirm_import(
            db_session,
            job.id,
            ConfirmImportRequest(series_ids=[]),
        )

        assert updated_job.status == ImportJobStatus.IMPORTING
        await db_session.refresh(duplicate_item)
        assert duplicate_item.status == ImportSeriesStatus.DUPLICATE

    @pytest.mark.asyncio
    async def test_duplicate_manual_override_blocked_when_no_importable_targets(
        self,
        db_session: AsyncSession,
        service: ImportService,
    ) -> None:
        """Fully owned duplicate series should not allow manual reassignment in review."""
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        target_series = await _create_target_series(db_session)
        target_issue = Issue(
            series_id=target_series.id,
            issue_number=1.0,
            comicvine_id=456001,
            title="Issue 1",
            status=IssueStatus.OWNED,
        )
        db_session.add(target_issue)
        await db_session.flush()
        root = LibraryRoot(name="Main", path="/library/main")
        db_session.add(root)
        await db_session.flush()
        db_session.add(
            LibraryFile(
                file_path="/library/main/owned.cbz",
                file_name="owned.cbz",
                file_size=2048,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(UTC),
                match_confidence=MatchConfidence.HIGH,
                issue_id=target_issue.id,
                library_root_id=root.id,
            )
        )
        duplicate_item = await _create_imported_series(
            db_session,
            job,
            status=ImportSeriesStatus.DUPLICATE,
            cv_match_method="comicinfo_cv_id",
        )
        duplicate_item.series_id = target_series.id
        duplicate_item.diagnostics = {
            "kind": "duplicate_series",
            "actionable_duplicate_merge": False,
            "fully_owned_series": True,
        }
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=duplicate_item.id,
            file_path="/tmp/redundant.cbz",
            file_name="redundant.cbz",
            file_size=1024,
            file_format="cbz",
            status=ImportedFileStatus.NO_MATCH,
        )
        db_session.add(imp_file)
        await db_session.flush()

        with pytest.raises(ValidationError, match="manual reassignment is disabled"):
            await service.override_file_match(db_session, job.id, imp_file.id, target_issue.id)

    @pytest.mark.asyncio
    async def test_unmatch_duplicate_series_returns_row_to_series_review(
        self,
        db_session: AsyncSession,
        service: ImportService,
    ) -> None:
        """Users can reject an incorrect existing-library duplicate match in Step 3."""
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        existing = Series(
            title="Seven Wives",
            sort_title="seven wives",
            year_start=2026,
            comicvine_id=172012,
        )
        db_session.add(existing)
        await db_session.flush()
        existing_issue = Issue(
            series_id=existing.id,
            issue_number=1.0,
            comicvine_id=1167184,
            status=IssueStatus.WANTED,
        )
        db_session.add(existing_issue)
        await db_session.flush()
        duplicate_item = await _create_imported_series(
            db_session,
            job,
            name="Seven Wives IDW Crime Sampler",
            year=2026,
            status=ImportSeriesStatus.DUPLICATE,
            cv_id=172012,
            cv_match_score=0.95,
            cv_match_method=None,
        )
        duplicate_item.series_id = existing.id
        duplicate_item.cv_title = "Seven Wives"
        duplicate_item.cv_year = 2026
        duplicate_item.cv_issue_count = 1
        duplicate_item.selected_for_import = True
        duplicate_item.diagnostics = {
            "kind": "duplicate_series",
            "duplicate_reason": "name_year",
            "existing_series_id": existing.id,
            "existing_series_title": "Seven Wives",
            "existing_series_year": 2026,
            "duplicate_match_score": 0.95,
        }
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=duplicate_item.id,
            file_path="/tmp/Seven Wives IDW Crime Sampler.cbz",
            file_name="Seven Wives IDW Crime Sampler.cbz",
            file_size=1024,
            file_format="cbz",
            parsed_series="Seven Wives IDW Crime Sampler",
            status=ImportedFileStatus.MATCHED,
            include_in_import=True,
            matched_issue_id=existing_issue.id,
            match_confidence="medium",
            match_method="single_issue_series",
            diagnostics={"kind": "duplicate_series_file", "target_state": "missing"},
        )
        db_session.add(imp_file)
        await db_session.flush()

        updated = await service.unmatch_duplicate_series(db_session, job.id, duplicate_item.id)

        await db_session.refresh(job)
        await db_session.refresh(duplicate_item)
        await db_session.refresh(imp_file)
        assert updated.id == duplicate_item.id
        assert duplicate_item.status == ImportSeriesStatus.NO_MATCH
        assert duplicate_item.series_id is None
        assert duplicate_item.cv_id is None
        assert duplicate_item.cv_title is None
        assert duplicate_item.selected_for_import is False
        assert duplicate_item.diagnostics["kind"] == "series_no_match"
        assert duplicate_item.diagnostics["reason"] == "duplicate_unmatched_by_user"
        assert duplicate_item.diagnostics["previous_duplicate"]["existing_series_id"] == existing.id
        assert imp_file.status == ImportedFileStatus.NO_MATCH
        assert imp_file.include_in_import is False
        assert imp_file.matched_issue_id is None
        assert imp_file.match_method is None
        assert imp_file.diagnostics["kind"] == "file_no_match"
        assert job.series_duplicate == 0
        assert job.series_no_match == 1
        assert job.total_files_no_match == 1

    @pytest.mark.asyncio
    async def test_confirm_allows_convert_with_source_preserving_transfer(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Collection imports keep source-safe effective copy for archive conversion."""
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        item = await _create_imported_series(db_session, job)
        db_session.add(
            SystemConfig(key="post_processing_method", value="hardlink", value_type="string")
        )
        db_session.add(
            SystemConfig(
                key="convert_to_preferred_format_on_import",
                value="true",
                value_type="bool",
            )
        )
        await db_session.flush()

        await service.confirm_import(
            db_session,
            job.id,
            ConfirmImportRequest(series_ids=[item.id]),
        )

        assert job.transfer_method == "hardlink"
        assert job.effective_transfer_method == "copy"
        assert job.source_preserved is True
        assert job.convert_to_preferred_format is True


class TestMetadataRepair:
    """Repair embedded ComicInfo metadata for imported files."""

    @pytest.mark.asyncio
    async def test_cached_comicinfo_payload_enriches_issue_and_uses_archive_page_count(
        self,
        db_session: AsyncSession,
        service: ImportService,
        tmp_path: Path,
    ) -> None:
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        publisher = Publisher(name="Marvel", comicvine_id=31)
        db_session.add(publisher)
        await db_session.flush()
        library_series = Series(
            title="Bring On the Bad Guys: Doom",
            sort_title="bring on the bad guys doom",
            year_start=2025,
            comicvine_id=165083,
            issue_count=1,
            publisher_id=publisher.id,
            series_type=SeriesType.STANDARD,
        )
        db_session.add(library_series)
        await db_session.flush()
        issue = Issue(
            series_id=library_series.id,
            issue_number=1.0,
            comicvine_id=1116296,
            title="Old embedded title",
            page_count=99,
            issue_type=IssueType.ISSUE,
            status=IssueStatus.WANTED,
        )
        db_session.add(issue)
        await db_session.flush()

        archive_path = tmp_path / "doom.cbz"
        other_archive_path = tmp_path / "doom-other.cbz"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("001.jpg", b"page one")
            archive.writestr("002.png", b"page two")
            archive.writestr("ComicInfo.xml", b"<ComicInfo />")
        with zipfile.ZipFile(other_archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("001.jpg", b"page one")

        async def _fetch_issue(
            session: AsyncSession,
            comicvine_issue_id: int,
        ) -> Issue:
            assert session is db_session
            assert comicvine_issue_id == 1116296
            issue.title = None
            issue.description = "Doom builds the Soul Forge."
            issue.release_date = date(2025, 8, 1)
            issue.comicvine_url = (
                "https://comicvine.gamespot.com/bring-on-the-bad-guys-doom-1/4000-1116296/"
            )
            issue.page_count = 30
            return issue

        service._metadata_service.fetch_issue = AsyncMock(side_effect=_fetch_issue)

        payload = await service._build_cached_comicinfo_payload_for_issue(
            db_session,
            job,
            issue,
            source_path=archive_path,
        )
        cached_payload = await service._build_cached_comicinfo_payload_for_issue(
            db_session,
            job,
            issue,
            source_path=archive_path,
        )
        other_payload = await service._build_cached_comicinfo_payload_for_issue(
            db_session,
            job,
            issue,
            source_path=other_archive_path,
        )

        assert service._metadata_service.fetch_issue.await_count == 2
        assert cached_payload is payload
        assert payload["Series"] == "Bring On the Bad Guys: Doom"
        assert payload["Title"] is None
        assert payload["Summary"] == "Doom builds the Soul Forge."
        assert payload["Year"] == 2025
        assert payload["Month"] == 8
        assert payload["Day"] == 1
        assert payload["PageCount"] == 2
        assert payload["Web"] == (
            "https://comicvine.gamespot.com/bring-on-the-bad-guys-doom-1/4000-1116296/"
        )
        assert payload["Notes"] == "[cv_vol_id:165083] [cv_issue_id:1116296]"
        assert other_payload["PageCount"] == 1
        assert issue.page_count == 30
        timings = service._import_runtime_cache(job.id).comicinfo_payload_timings
        assert (
            timings[(issue.id, str(archive_path), False)]["comicvine_issue_fetch_status"]
            == "fetched"
        )
        assert timings[(issue.id, str(archive_path), False)]["archive_page_count"] == 2
        assert timings[(issue.id, str(other_archive_path), False)]["archive_page_count"] == 1
        assert "comicinfo_payload_duration_ms" in timings[(issue.id, str(archive_path), False)]

    @pytest.mark.asyncio
    async def test_cached_comicinfo_payload_can_defer_cold_issue_enrichment(
        self,
        db_session: AsyncSession,
        service: ImportService,
        tmp_path: Path,
    ) -> None:
        """Step 4 can write known ComicInfo fields without waiting on ComicVine."""
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        publisher = Publisher(name="Boom", comicvine_id=42)
        db_session.add(publisher)
        await db_session.flush()
        library_series = Series(
            title="King Dracula",
            sort_title="king dracula",
            year_start=2024,
            comicvine_id=171911,
            issue_count=4,
            publisher_id=publisher.id,
            series_type=SeriesType.STANDARD,
        )
        db_session.add(library_series)
        await db_session.flush()
        issue = Issue(
            series_id=library_series.id,
            issue_number=4.0,
            comicvine_id=1234567,
            title="",
            issue_type=IssueType.ISSUE,
            status=IssueStatus.WANTED,
        )
        db_session.add(issue)
        await db_session.flush()

        archive_path = tmp_path / "king-dracula-004.cbz"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("001.jpg", b"page one")
            archive.writestr("002.jpg", b"page two")

        async def _fetch_issue(
            session: AsyncSession,
            comicvine_issue_id: int,
        ) -> Issue:
            assert session is db_session
            assert comicvine_issue_id == 1234567
            issue.description = "A newly enriched issue summary."
            issue.release_date = date(2026, 6, 17)
            issue.comicvine_url = "https://comicvine.gamespot.com/king-dracula-4/4000-1234567/"
            return issue

        service._metadata_service.fetch_issue = AsyncMock(side_effect=_fetch_issue)

        deferred_payload = await service._build_cached_comicinfo_payload_for_issue(
            db_session,
            job,
            issue,
            source_path=archive_path,
            defer_issue_enrichment=True,
        )

        assert service._metadata_service.fetch_issue.await_count == 0
        assert deferred_payload["Series"] == "King Dracula"
        assert deferred_payload["Number"] == "4"
        assert deferred_payload["Summary"] is None
        assert deferred_payload["PageCount"] == 2
        assert deferred_payload["Notes"] == "[cv_vol_id:171911] [cv_issue_id:1234567]"

        enriched_payload = await service._build_cached_comicinfo_payload_for_issue(
            db_session,
            job,
            issue,
            source_path=archive_path,
        )

        assert service._metadata_service.fetch_issue.await_count == 1
        assert enriched_payload["Summary"] == "A newly enriched issue summary."
        timings = service._import_runtime_cache(job.id).comicinfo_payload_timings
        assert timings[(issue.id, str(archive_path), True)]["comicvine_issue_fetch_status"] == (
            "deferred"
        )
        assert timings[(issue.id, str(archive_path), False)]["comicvine_issue_fetch_status"] == (
            "fetched"
        )

    @pytest.mark.asyncio
    async def test_repair_metadata_rejects_in_place_file_without_mutation(
        self,
        db_session: AsyncSession,
        service: ImportService,
        tmp_path: Path,
    ) -> None:
        job = await _create_job_row(
            db_session,
            status=ImportJobStatus.REVIEW,
            file_handling_mode=ImportFileHandlingMode.IN_PLACE,
        )
        imported_series = await _create_imported_series(db_session, job)
        archive_path = tmp_path / "Referenced.cbz"
        original = b"user-owned comic"
        archive_path.write_bytes(original)
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imported_series.id,
            file_path=str(archive_path),
            file_name=archive_path.name,
            file_size=len(original),
            file_format="cbz",
            status=ImportedFileStatus.NO_MATCH,
        )
        db_session.add(imp_file)
        await db_session.flush()

        with pytest.raises(ValidationError, match="In-place import files cannot"):
            await service.repair_file_metadata(db_session, job.id, imp_file.id)

        assert archive_path.read_bytes() == original

    @pytest.mark.asyncio
    async def test_repair_cbz_metadata_in_place(
        self,
        db_session: AsyncSession,
        service: ImportService,
        tmp_path: Path,
    ) -> None:
        """CBZ files are repaired in place when a target issue is known."""
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        imported_series = await _create_imported_series(db_session, job)
        library_series = Series(
            title="Chicken Devil",
            sort_title="chicken devil",
            year_start=2021,
            comicvine_id=139451,
            comicvine_url="https://comicvine.gamespot.com/chicken-devil/4050-139451/",
            series_type=SeriesType.STANDARD,
        )
        db_session.add(library_series)
        await db_session.flush()
        issue = Issue(
            series_id=library_series.id,
            issue_number=4.0,
            comicvine_id=905404,
            title="The Chicken is in the Details",
            comicvine_url=(
                "https://comicvine.gamespot.com/"
                "chicken-devil-4-the-chicken-is-in-the-details/4000-905404/"
            ),
            release_date=date(2022, 4, 13),
            issue_type=IssueType.ISSUE,
            status=IssueStatus.WANTED,
        )
        db_session.add(issue)
        await db_session.flush()

        archive_path = tmp_path / "Chicken Devil 004 (2022).cbz"
        _write_cbz_archive(
            archive_path,
            """<?xml version="1.0"?>
            <ComicInfo>
              <Series>Chicken Devils</Series>
              <Number>4</Number>
              <Volume>2022</Volume>
              <Year>2023</Year>
              <Title>The Chickens Made Me Do IT</Title>
              <Web>https://comicvine.gamespot.com/chicken-devils-4-the-chickens-made-me-do-it/4000-996957/</Web>
            </ComicInfo>
            """,
        )
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imported_series.id,
            file_path=str(archive_path),
            file_name=archive_path.name,
            file_size=archive_path.stat().st_size,
            file_format="cbz",
            parsed_series="Chicken Devil",
            parsed_issue_number=4.0,
            parsed_year=2022,
            has_comicinfo=True,
            status=ImportedFileStatus.MATCHED,
            matched_issue_id=issue.id,
            matched_issue_cv_id=issue.comicvine_id,
            match_method="manual_override",
            match_confidence="high",
        )
        db_session.add(imp_file)
        await db_session.flush()

        repaired = await service.repair_file_metadata(db_session, job.id, imp_file.id)

        assert repaired.file_path == str(archive_path)
        assert repaired.file_format == "cbz"
        assert repaired.matched_issue_id == issue.id
        comicinfo = (
            SourceMetadataExtractor().from_archive_path(archive_path).diagnostics["comicinfo"]
        )
        assert comicinfo["series"] == "Chicken Devil"
        assert comicinfo["number"] == "4"
        assert comicinfo["year"] == 2022
        assert comicinfo["title"] == "The Chicken is in the Details"
        assert comicinfo["web"] == issue.comicvine_url
        assert dict(repaired.diagnostics).get("metadata_repaired") is True
        assert dict(repaired.diagnostics).get("metadata_repair_mode") == "in_place"

    @pytest.mark.asyncio
    async def test_repair_cb7_creates_repaired_cbz_copy(
        self,
        db_session: AsyncSession,
        service: ImportService,
        tmp_path: Path,
    ) -> None:
        """Non-CBZ archives are normalized to a repaired CBZ copy."""
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        imported_series = await _create_imported_series(db_session, job)
        library_series = Series(
            title="Chicken Devil",
            sort_title="chicken devil",
            year_start=2021,
            comicvine_id=139451,
            comicvine_url="https://comicvine.gamespot.com/chicken-devil/4050-139451/",
            series_type=SeriesType.STANDARD,
        )
        db_session.add(library_series)
        await db_session.flush()
        issue = Issue(
            series_id=library_series.id,
            issue_number=4.0,
            comicvine_id=905404,
            title="The Chicken is in the Details",
            comicvine_url=(
                "https://comicvine.gamespot.com/"
                "chicken-devil-4-the-chicken-is-in-the-details/4000-905404/"
            ),
            release_date=date(2022, 4, 13),
            issue_type=IssueType.ISSUE,
            status=IssueStatus.WANTED,
        )
        db_session.add(issue)
        await db_session.flush()

        archive_path = tmp_path / "Chicken Devil 004 (2022).cb7"
        _write_cb7_archive(
            archive_path,
            """<?xml version="1.0"?>
            <ComicInfo>
              <Series>Chicken Devils</Series>
              <Number>4</Number>
              <Volume>2022</Volume>
              <Year>2023</Year>
              <Title>The Chickens Made Me Do IT</Title>
              <Web>https://comicvine.gamespot.com/chicken-devils-4-the-chickens-made-me-do-it/4000-996957/</Web>
            </ComicInfo>
            """,
        )
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imported_series.id,
            file_path=str(archive_path),
            file_name=archive_path.name,
            file_size=archive_path.stat().st_size,
            file_format="cb7",
            parsed_series="Chicken Devil",
            parsed_issue_number=4.0,
            parsed_year=2022,
            has_comicinfo=True,
            status=ImportedFileStatus.NO_MATCH,
        )
        db_session.add(imp_file)
        await db_session.flush()

        repaired = await service.repair_file_metadata(
            db_session,
            job.id,
            imp_file.id,
            issue_id=issue.id,
        )

        repaired_path = Path(repaired.file_path)
        assert repaired_path != archive_path
        assert repaired_path.suffix.lower() == ".cbz"
        assert repaired_path.exists()
        assert archive_path.exists()
        assert repaired.file_format == "cbz"
        assert repaired.file_name == repaired_path.name
        assert repaired.matched_issue_id == issue.id
        comicinfo = (
            SourceMetadataExtractor().from_archive_path(repaired_path).diagnostics["comicinfo"]
        )
        assert comicinfo["series"] == "Chicken Devil"
        assert comicinfo["number"] == "4"
        assert comicinfo["year"] == 2022
        assert comicinfo["title"] == "The Chicken is in the Details"
        assert comicinfo["web"] == issue.comicvine_url
        assert dict(repaired.diagnostics).get("metadata_repaired") is True
        assert dict(repaired.diagnostics).get("metadata_repair_mode") == "normalized_to_cbz"

    @pytest.mark.asyncio
    async def test_prepare_import_file_normalizes_to_cbz_when_enabled(
        self,
        db_session: AsyncSession,
        service: ImportService,
        tmp_path: Path,
    ) -> None:
        """Import normalization targets CBZ regardless of preferred search format."""
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        job.move_to_library = True
        job.transfer_method = "copy"
        job.convert_to_preferred_format = True
        imported_series = await _create_imported_series(db_session, job)
        source_path = tmp_path / "normalize-me.cb7"
        source_path.write_bytes(b"placeholder")
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imported_series.id,
            file_path=str(source_path),
            file_name=source_path.name,
            file_size=source_path.stat().st_size,
            file_format="cb7",
            status=ImportedFileStatus.MATCHED,
        )
        db_session.add(imp_file)
        db_session.add(SystemConfig(key="preferred_format", value="cbr", value_type="string"))
        await db_session.flush()

        converted_path = tmp_path / "converted.cbz"
        with patch(
            "pullbox.services.import_file_interruptible_ops.default_convert_file_interruptible",
            new=AsyncMock(return_value=converted_path),
        ) as convert_mock:
            prepared = await service._prepare_import_file(db_session, job, imp_file)

        convert_mock.assert_awaited_once()
        assert convert_mock.await_args.args[1] == "cbz"
        assert prepared.registration_source == converted_path
        assert prepared.converted is True

    @pytest.mark.asyncio
    async def test_prepare_import_file_normalizes_to_cbz_when_metadata_update_enabled(
        self,
        db_session: AsyncSession,
        service: ImportService,
        tmp_path: Path,
    ) -> None:
        """Updating embedded ComicInfo on import normalizes non-CBZ artifacts to CBZ."""
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        job.move_to_library = True
        job.transfer_method = "copy"
        job.convert_to_preferred_format = False
        job.update_embedded_comicinfo_from_match = True
        imported_series = await _create_imported_series(db_session, job)
        source_path = tmp_path / "update-metadata.cb7"
        source_path.write_bytes(b"placeholder")
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imported_series.id,
            file_path=str(source_path),
            file_name=source_path.name,
            file_size=source_path.stat().st_size,
            file_format="cb7",
            status=ImportedFileStatus.MATCHED,
        )
        db_session.add(imp_file)
        await db_session.flush()

        converted_path = tmp_path / "metadata-updated.cbz"
        with patch(
            "pullbox.services.import_file_interruptible_ops.default_convert_file_interruptible",
            new=AsyncMock(return_value=converted_path),
        ) as convert_mock:
            prepared = await service._prepare_import_file(db_session, job, imp_file)

        convert_mock.assert_awaited_once()
        assert convert_mock.await_args.args[1] == "cbz"
        assert prepared.registration_source == converted_path
        assert prepared.converted is True

    @pytest.mark.asyncio
    async def test_prepare_import_file_forwards_progress_callback_to_interruptible_converter(
        self,
        db_session: AsyncSession,
        service: ImportService,
        tmp_path: Path,
    ) -> None:
        """The Step 4 prep adapter accepts and forwards file-progress callbacks."""
        job = await _create_job_row(db_session, status=ImportJobStatus.IMPORTING)
        job.move_to_library = True
        job.transfer_method = "copy"
        job.convert_to_preferred_format = True
        imported_series = await _create_imported_series(db_session, job)
        source_path = tmp_path / "progress-me.pdf"
        source_path.write_bytes(b"placeholder")
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imported_series.id,
            file_path=str(source_path),
            file_name=source_path.name,
            file_size=source_path.stat().st_size,
            file_format="pdf",
            status=ImportedFileStatus.MATCHED,
        )
        db_session.add(imp_file)
        await db_session.flush()

        progress_callback = AsyncMock()
        converted_path = tmp_path / "progress-me.cbz"
        with patch.object(
            service,
            "_convert_import_file_interruptible",
            new=AsyncMock(return_value=converted_path),
        ) as convert_mock:
            prepared = await service._prepare_import_file(
                db_session,
                job,
                imp_file,
                progress_callback=progress_callback,
            )

        convert_mock.assert_awaited_once()
        assert convert_mock.await_args.kwargs["progress_callback"] is progress_callback
        assert prepared.registration_source == converted_path
        assert prepared.converted is True

    @pytest.mark.asyncio
    async def test_register_import_library_file_adapters_accept_progress_callbacks(
        self,
        db_session: AsyncSession,
        service: ImportService,
        tmp_path: Path,
    ) -> None:
        """Register-time converter and ComicInfo adapters accept progress callbacks."""
        publisher = Publisher(name="Image")
        db_session.add(publisher)
        await db_session.flush()
        series = Series(
            title="Progress Test",
            sort_title="progress test",
            year_start=2024,
            comicvine_id=424242,
            publisher_id=publisher.id,
        )
        db_session.add(series)
        await db_session.flush()
        issue = Issue(
            series_id=series.id,
            issue_number=1.0,
            comicvine_id=777001,
            title="One",
        )
        db_session.add(issue)
        await db_session.flush()

        job = await _create_job_row(db_session, status=ImportJobStatus.IMPORTING)
        source_path = tmp_path / "adapter-test.pdf"
        source_path.write_bytes(b"placeholder")
        transfer_progress_callback = AsyncMock()
        comicinfo_progress_callback = AsyncMock()
        converted_path = tmp_path / "adapter-test.cbz"
        transfer_library_path = tmp_path / "transfer-library.cbz"
        materialized_library_path = tmp_path / "materialized-library.cbz"

        async def fake_register_library_file(
            session: AsyncSession,
            source_path_arg: Path,
            issue_arg: Issue,
            confidence: MatchConfidence,
            **kwargs: object,
        ) -> str:
            await kwargs["converter"](
                source_path_arg,
                "cbz",
                destination=tmp_path,
                progress_callback=transfer_progress_callback,
            )
            await kwargs["artifact_transfer"](
                source_path_arg,
                transfer_library_path,
                "move",
                transfer_progress_callback=transfer_progress_callback,
            )
            await kwargs["comicinfo_materializer"](
                source_path_arg,
                materialized_library_path,
                {"Series": "Progress Test"},
                transfer_method="move",
                progress_callback=comicinfo_progress_callback,
            )
            await kwargs["comicinfo_embedder"](
                materialized_library_path,
                {"Series": "Progress Test"},
                progress_callback=comicinfo_progress_callback,
            )
            return "ok"

        async def fake_transfer(
            _session: AsyncSession,
            _job: ImportJob,
            _source_path: Path,
            target_path: Path,
            _transfer_method: str,
            *,
            transfer_progress_callback=None,
        ) -> Path:
            target_path.write_bytes(b"transferred")
            if transfer_progress_callback is not None:
                await transfer_progress_callback("transferring", 1, 1, "bytes")
            return target_path

        async def fake_materialize_with_comicinfo(
            _session: AsyncSession,
            _job: ImportJob,
            _source_path: Path,
            target_path: Path,
            _payload: dict[str, object],
            *,
            transfer_method: str,
            temp_path: Path | None = None,
            progress_callback=None,
        ) -> bool:
            assert temp_path is not None
            assert transfer_method == "move"
            target_path.write_bytes(b"materialized")
            if progress_callback is not None:
                await progress_callback("rewriting", 1, 1, "entries")
            return True

        with (
            patch(
                "pullbox.services.import_service.register_library_file",
                new=AsyncMock(side_effect=fake_register_library_file),
            ) as register_mock,
            patch.object(
                service,
                "_convert_import_file_interruptible",
                new=AsyncMock(return_value=converted_path),
            ) as convert_mock,
            patch.object(
                service,
                "_transfer_import_artifact_interruptible",
                new=AsyncMock(side_effect=fake_transfer),
            ) as transfer_mock,
            patch.object(
                service,
                "_materialize_import_cbz_with_comicinfo_interruptible",
                new=AsyncMock(side_effect=fake_materialize_with_comicinfo),
            ) as materialize_mock,
            patch.object(
                service,
                "_embed_import_comicinfo_interruptible",
                new=AsyncMock(return_value=True),
            ) as embed_mock,
            patch.object(service, "_log_event", new=AsyncMock()) as log_event_mock,
        ):
            result = await service._register_import_library_file(
                db_session,
                job,
                source_path,
                issue,
                MatchConfidence.HIGH,
                comicinfo_progress_callback=comicinfo_progress_callback,
            )

        assert result == "ok"
        register_mock.assert_awaited_once()
        assert register_mock.await_args.kwargs["source_scan_root"] == Path(job.source_path)
        assert register_mock.await_args.kwargs["strict_import_target"] is True
        convert_mock.assert_awaited_once()
        assert convert_mock.await_args.kwargs["progress_callback"] is transfer_progress_callback
        transfer_mock.assert_awaited_once()
        materialize_mock.assert_awaited_once()
        embed_mock.assert_awaited_once()
        assert embed_mock.await_args.kwargs["progress_callback"] is comicinfo_progress_callback
        logged_events = [call.args[3] for call in log_event_mock.await_args_list]
        assert logged_events == [
            "import_file_transfer_timed",
            "import_file_cbz_comicinfo_materialize_timed",
            "import_file_comicinfo_rewrite_timed",
        ]
        assert log_event_mock.await_args_list[0].kwargs["target_size_bytes"] == len(b"transferred")
        assert log_event_mock.await_args_list[1].kwargs["target_size_bytes"] == len(b"materialized")
        assert log_event_mock.await_args_list[2].kwargs["changed"] is True

    @pytest.mark.asyncio
    async def test_register_import_library_file_durably_completes_placement_signature(
        self,
        db_session: AsyncSession,
        service: ImportService,
        tmp_path: Path,
    ) -> None:
        from pullbox.core.library_file_ownership import build_managed_placement_signature

        publisher = Publisher(name="Image")
        series = Series(
            title="Placement Test",
            sort_title="placement test",
            year_start=2024,
            publisher=publisher,
        )
        issue = Issue(series=series, issue_number=1.0, title="One")
        db_session.add_all([publisher, series, issue])
        await db_session.flush()
        job = await _create_job_row(db_session, status=ImportJobStatus.IMPORTING)
        source_path = tmp_path / "incoming.cbz"
        source_path.write_bytes(b"source comic")
        stable_source_path = tmp_path / "incoming.cbr"
        stable_source_path.write_bytes(b"original comic")
        destination_path = tmp_path / "library" / "Placement Test 001.cbz"
        destination_path.parent.mkdir()

        async def fake_register_library_file(
            _session: AsyncSession,
            source_path_arg: Path,
            _issue_arg: Issue,
            _confidence: MatchConfidence,
            **kwargs: object,
        ) -> str:
            placement_started = kwargs["placement_started_callback"]
            placement_completed = kwargs["placement_completed_callback"]
            temp_paths = kwargs["placement_temp_paths"](
                source_path_arg,
                destination_path,
            )
            await placement_started(
                artifact_source_path=source_path_arg,
                target_path=destination_path,
                transfer_method="copy",
                series_folder_created=True,
                series_folder_path=destination_path.parent,
                created_directory_paths=(destination_path.parent,),
                directory_ownership_boundary_path=destination_path.parent.parent,
                temp_paths=temp_paths,
            )
            destination_path.write_bytes(b"placed comic")
            await placement_completed(
                target_path=destination_path,
                destination_signature=build_managed_placement_signature(destination_path),
            )
            return "ok"

        with (
            patch(
                "pullbox.services.import_service.register_library_file",
                new=AsyncMock(side_effect=fake_register_library_file),
            ),
            patch.object(service, "_log_import_file_timing_events", new=AsyncMock()),
        ):
            result = await service._register_import_library_file(
                db_session,
                job,
                source_path,
                issue,
                MatchConfidence.HIGH,
                recovery_original_source_path=stable_source_path,
            )

        assert result == "ok"
        action = await db_session.scalar(
            select(ImportJobAction).where(
                ImportJobAction.import_job_id == job.id,
                ImportJobAction.action_type == "library_file_placement_started",
            )
        )
        assert action is not None
        assert action.payload["original_source_path"] == str(stable_source_path)
        assert action.payload["artifact_source_path"] == str(source_path)
        assert action.payload["placement_completed"] is True
        assert action.payload["destination_signature"] == build_managed_placement_signature(
            destination_path
        )
        assert action.payload["temp_paths"]
        assert action.payload["created_directory_paths"] == [str(destination_path.parent)]
        assert action.payload["directory_ownership_boundary_path"] == str(
            destination_path.parent.parent
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tamper_destination", "converted_source", "recovery_transfer_method"),
        [
            (False, False, "move"),
            (True, False, "move"),
            (False, True, "copy"),
            (False, True, "move"),
        ],
    )
    async def test_register_import_library_file_recovers_same_job_completed_placement(
        self,
        db_session: AsyncSession,
        service: ImportService,
        tmp_path: Path,
        tamper_destination: bool,
        converted_source: bool,
        recovery_transfer_method: str,
    ) -> None:
        from pullbox.core.library_file_ownership import build_managed_placement_signature
        from pullbox.models.library import LibraryFileStorageMode

        root_path = tmp_path / "library"
        root_path.mkdir()
        root = LibraryRoot(
            name="Recovery root",
            path=str(root_path),
            enabled=True,
            allow_managed_writes=True,
            is_default_managed_destination=True,
        )
        publisher = Publisher(name="Image")
        series = Series(
            title="Placement Recovery",
            sort_title="placement recovery",
            year_start=2024,
            publisher=publisher,
            library_root=root,
        )
        issue = Issue(series=series, issue_number=1.0, title="One")
        db_session.add_all([root, publisher, series, issue])
        await db_session.flush()
        job = ImportJob(
            source_path=str(tmp_path / "incoming"),
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.IMPORTING,
            file_handling_mode=ImportFileHandlingMode.MANAGED_COPY,
            target_library_root_id=root.id,
        )
        db_session.add(job)
        await db_session.flush()
        imported_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name=series.title,
            status=ImportSeriesStatus.CONFIRMED,
            file_count=1,
            series_id=series.id,
        )
        db_session.add(imported_series)
        await db_session.flush()
        source_path = (
            tmp_path
            / "incoming"
            / ("Placement Recovery 001.cbr" if converted_source else "Placement Recovery 001.cbz")
        )
        registration_source_path = source_path
        artifact_source_path = source_path
        if converted_source:
            source_path.parent.mkdir()
            source_path.write_bytes(b"original cbr")
            artifact_source_path = tmp_path / "old-work" / "Placement Recovery 001.cbz"
            registration_source_path = tmp_path / "new-work" / "Placement Recovery 001.cbz"
            registration_source_path.parent.mkdir()
            registration_source_path.write_bytes(b"new converted temp")
        imported_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imported_series.id,
            file_path=str(source_path),
            file_name=source_path.name,
            file_size=12,
            file_format="cbz",
            parsed_issue_number=1.0,
            matched_issue_id=issue.id,
            status=ImportedFileStatus.CONFIRMED,
        )
        db_session.add(imported_file)
        await db_session.flush()
        destination_path = root_path / "Placement Recovery (2024)" / registration_source_path.name
        destination_path.parent.mkdir()
        destination_path.write_bytes(b"placed comic")
        destination_signature = build_managed_placement_signature(destination_path)
        placement_action = ImportJobAction(
            import_job_id=job.id,
            sequence_no=1,
            phase="import",
            action_type="library_file_placement_started",
            status=ImportJobActionStatus.COMPLETED,
            payload={
                "imported_file_id": imported_file.id,
                "issue_id": issue.id,
                "destination_path": str(destination_path),
                "original_source_path": str(source_path),
                "artifact_source_path": str(artifact_source_path),
                "transfer_method": recovery_transfer_method,
                "created_series_folder": True,
                "created_series_folder_path": str(destination_path.parent),
                "temp_paths": [],
                "placement_completed": True,
                "destination_signature": destination_signature,
            },
        )
        db_session.add(placement_action)
        await db_session.commit()

        if tamper_destination:
            placed_stat = destination_path.stat()
            destination_path.write_bytes(b"other comic!")
            os.utime(
                destination_path,
                ns=(placed_stat.st_atime_ns, placed_stat.st_mtime_ns),
            )

        async def attempt_recovery():  # type: ignore[no-untyped-def]
            return await service._register_import_library_file(
                db_session,
                job,
                registration_source_path,
                issue,
                MatchConfidence.HIGH,
                recovery_imported_file_id=imported_file.id,
                recovery_original_source_path=source_path,
                move_to_library=True,
                storage_mode=LibraryFileStorageMode.MANAGED,
                library_root_id=root.id,
                transfer_method=recovery_transfer_method,
                normalize_to_cbz=False,
                update_embedded_comicinfo_from_match=False,
                loaded_issue=issue,
                source_scan_root=Path(job.source_path),
            )

        if tamper_destination:
            with pytest.raises(FileNotFoundError, match="Source file not found"):
                await attempt_recovery()
            assert destination_path.read_bytes() == b"other comic!"
            assert await db_session.scalar(select(LibraryFile.id)) is None
            return

        result = await attempt_recovery()

        library_file = result.library_file
        assert library_file.file_path == str(destination_path)
        assert library_file.storage_mode == LibraryFileStorageMode.MANAGED
        assert library_file.source_signature == destination_signature
        assert destination_path.read_bytes() == b"placed comic"
        if converted_source:
            assert source_path.read_bytes() == b"original cbr"
            assert registration_source_path.read_bytes() == b"new converted temp"
        else:
            assert not source_path.exists()

    @pytest.mark.asyncio
    async def test_mylar_managed_import_rejects_exact_source_destination(
        self,
        db_session: AsyncSession,
        service: ImportService,
        tmp_path: Path,
    ) -> None:
        from pullbox.core.exceptions import ConfigurationError

        series_folder = tmp_path / "existing-mylar-library" / "Batman (2016)"
        series_folder.mkdir(parents=True)
        source_path = series_folder / "Batman 001.cbz"
        source_path.write_bytes(b"source comic")
        database = tmp_path / "mylar.db"
        database.write_bytes(b"database")
        root = LibraryRoot(name="Existing Mylar", path=str(tmp_path / "existing-mylar-library"))
        series = Series(
            title="Batman",
            sort_title="batman",
            year_start=2016,
            path=str(series_folder),
            library_root=root,
        )
        issue = Issue(series=series, issue_number=1.0, title="One")
        db_session.add_all([root, series, issue])
        await db_session.flush()
        job = await _create_job_row(db_session, status=ImportJobStatus.IMPORTING)
        job.source_type = ImportSourceType.MYLAR3
        job.source_path = str(database)
        await db_session.flush()

        with pytest.raises(ConfigurationError, match="same file"):
            await service._register_import_library_file(
                db_session,
                job,
                source_path,
                issue,
                MatchConfidence.HIGH,
                move_to_library=True,
                library_root_id=root.id,
                transfer_method="copy",
                rename=False,
                normalize_to_cbz=False,
                update_embedded_comicinfo_from_match=False,
                loaded_issue=issue,
                source_scan_root=series_folder,
            )

        assert source_path.read_bytes() == b"source comic"
        actions = list(
            await db_session.scalars(
                select(ImportJobAction).where(ImportJobAction.import_job_id == job.id)
            )
        )
        assert actions == []


# ── Test: run_import ─────────────────────────────────────────────────────


class TestRunImport:
    """Test run_import() — adds confirmed series to Pullbox."""

    @pytest.mark.asyncio
    async def test_import_confirmed_series(
        self,
        db_session: AsyncSession,
        mock_series_service: AsyncMock,
        mock_metadata_service: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """Confirmed series calls add_from_comicvine and sets IMPORTED."""
        target = await _create_target_series(db_session)
        mock_series_service.add_from_comicvine.return_value = target

        svc = ImportService(
            series_service=mock_series_service,
            metadata_service=mock_metadata_service,
            event_bus=mock_event_bus,
        )
        svc._process_series_files = AsyncMock(return_value=(1, 0))
        job = await _create_job_row(db_session, status=ImportJobStatus.IMPORTING)
        item = await _create_imported_series(db_session, job, status=ImportSeriesStatus.CONFIRMED)
        await _create_imported_file(db_session, job, item)

        await svc.run_import(db_session, job.id)

        await db_session.refresh(item)
        assert item.status == ImportSeriesStatus.IMPORTED
        assert item.series_id == target.id
        assert job.status == ImportJobStatus.COMPLETED
        assert job.series_imported == 1
        mock_series_service.add_from_comicvine.assert_called_once()

    @pytest.mark.asyncio
    async def test_import_partial_failure(
        self,
        db_session: AsyncSession,
        mock_metadata_service: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """One series fails, others succeed — job still COMPLETED."""
        target = await _create_target_series(db_session)
        target_id = target.id
        mock_ss = AsyncMock()

        async def add_from_comicvine(
            session: AsyncSession,
            comicvine_id: int,
            **_kwargs: object,
        ) -> Series:
            if comicvine_id == 111:
                raise Exception("CV API down")
            resolved = await session.get(Series, target_id)
            assert resolved is not None
            return resolved

        mock_ss.add_from_comicvine.side_effect = add_from_comicvine

        svc = ImportService(
            series_service=mock_ss,
            metadata_service=mock_metadata_service,
            event_bus=mock_event_bus,
        )
        svc._process_series_files = AsyncMock(return_value=(1, 0))
        job = await _create_job_row(db_session, status=ImportJobStatus.IMPORTING)
        fail_item = await _create_imported_series(
            db_session, job, name="Failing", status=ImportSeriesStatus.CONFIRMED, cv_id=111
        )
        await _create_imported_file(db_session, job, fail_item, file_name="Failing 001.cbz")
        good_item = await _create_imported_series(
            db_session, job, name="Good", status=ImportSeriesStatus.CONFIRMED, cv_id=222
        )
        await _create_imported_file(db_session, job, good_item, file_name="Good 001.cbz")

        await svc.run_import(db_session, job.id)

        await db_session.refresh(fail_item)
        await db_session.refresh(good_item)
        assert fail_item.status == ImportSeriesStatus.FAILED
        assert fail_item.error_message is not None
        assert good_item.status == ImportSeriesStatus.IMPORTED
        assert good_item.series_id == target.id
        assert job.status == ImportJobStatus.COMPLETED
        assert job.series_imported == 1
        assert job.series_failed == 1

    @pytest.mark.asyncio
    async def test_import_uses_user_selected_cv_id(
        self,
        db_session: AsyncSession,
        mock_series_service: AsyncMock,
        mock_metadata_service: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """When user_selected_cv_id is set, it takes priority over cv_id."""
        target = await _create_target_series(db_session)
        mock_series_service.add_from_comicvine.return_value = target

        svc = ImportService(
            series_service=mock_series_service,
            metadata_service=mock_metadata_service,
            event_bus=mock_event_bus,
        )
        svc._process_series_files = AsyncMock(return_value=(1, 0))
        job = await _create_job_row(db_session, status=ImportJobStatus.IMPORTING)
        item = await _create_imported_series(
            db_session, job, status=ImportSeriesStatus.CONFIRMED, cv_id=111
        )
        item.user_selected_cv_id = 999
        await _create_imported_file(db_session, job, item)
        await db_session.flush()

        await svc.run_import(db_session, job.id)

        call_args = mock_series_service.add_from_comicvine.call_args
        cv_id_used = call_args.kwargs.get("comicvine_id") or call_args[1].get("comicvine_id")
        assert cv_id_used == 999

    @pytest.mark.asyncio
    async def test_import_reused_existing_series_does_not_record_series_created_action(
        self,
        db_session: AsyncSession,
        mock_series_service: AsyncMock,
        mock_metadata_service: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        existing = await _create_target_series(db_session)
        existing.comicvine_id = 424242
        await db_session.flush()
        mock_series_service.add_from_comicvine.return_value = existing

        svc = ImportService(
            series_service=mock_series_service,
            metadata_service=mock_metadata_service,
            event_bus=mock_event_bus,
        )
        job = await _create_job_row(db_session, status=ImportJobStatus.IMPORTING)
        item = await _create_imported_series(
            db_session,
            job,
            status=ImportSeriesStatus.CONFIRMED,
            cv_id=424242,
        )
        await _create_imported_file(db_session, job, item)

        await svc.run_import(db_session, job.id)

        actions = list(
            (
                await db_session.execute(
                    select(ImportJobAction).where(ImportJobAction.import_job_id == job.id)
                )
            ).scalars()
        )
        assert actions == []

    @pytest.mark.asyncio
    async def test_import_debug_slow_mode_applies_item_delay(
        self,
        db_session: AsyncSession,
        mock_series_service: AsyncMock,
        mock_metadata_service: AsyncMock,
        mock_event_bus: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Dev slow mode delays import progress updates between series."""
        monkeypatch.setenv("PULLBOX_IMPORT_DEBUG_SLOW_MODE", "true")
        monkeypatch.setenv("PULLBOX_IMPORT_DEBUG_ITEM_DELAY_SECONDS", "0.01")
        get_settings.cache_clear()

        try:
            target = await _create_target_series(db_session)
            mock_series_service.add_from_comicvine.return_value = target

            svc = ImportService(
                series_service=mock_series_service,
                metadata_service=mock_metadata_service,
                event_bus=mock_event_bus,
            )
            svc._maybe_slow_item_delay = AsyncMock()
            svc._process_series_files = AsyncMock(return_value=(1, 0))

            job = await _create_job_row(db_session, status=ImportJobStatus.IMPORTING)
            item = await _create_imported_series(
                db_session, job, status=ImportSeriesStatus.CONFIRMED
            )
            await _create_imported_file(db_session, job, item)

            async def capture_progress(_event: object) -> None:
                return None

            await svc.run_import(db_session, job.id, progress_callback=capture_progress)

            svc._maybe_slow_item_delay.assert_awaited_once()
        finally:
            get_settings.cache_clear()


# ── Test: cancel_job ─────────────────────────────────────────────────────


class TestCancelJob:
    """Test delete/discard semantics for import jobs."""

    @pytest.mark.asyncio
    async def test_cancel_pending(self, db_session: AsyncSession, service: ImportService) -> None:
        """PENDING job is deleted on cancel."""
        job = await _create_job_row(db_session, status=ImportJobStatus.PENDING)
        job_id = job.id
        await service.cancel_job(db_session, job_id)
        assert await db_session.get(ImportJob, job_id) is None

    @pytest.mark.asyncio
    async def test_cancel_review(self, db_session: AsyncSession, service: ImportService) -> None:
        """REVIEW job is deleted on cancel."""
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        job_id = job.id
        await service.cancel_job(db_session, job_id)
        assert await db_session.get(ImportJob, job_id) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        [
            ImportJobStatus.FAILED,
            ImportJobStatus.COMPLETED,
            ImportJobStatus.CANCELLED,
        ],
    )
    async def test_cancel_terminal_job_deletes_history_row(
        self,
        db_session: AsyncSession,
        service: ImportService,
        status: ImportJobStatus,
    ) -> None:
        """Finished import jobs can be deleted from history."""
        job = await _create_job_row(db_session, status=status)
        job_id = job.id
        await service.cancel_job(db_session, job_id)
        assert await db_session.get(ImportJob, job_id) is None

    @pytest.mark.asyncio
    async def test_cancel_scanning_raises_validation_error(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Active scan jobs must use the explicit cancel control instead of DELETE semantics."""
        job = await _create_job_row(db_session, status=ImportJobStatus.SCANNING)

        with pytest.raises(ValidationError, match="pause/cancel/rollback"):
            await service.cancel_job(db_session, job.id)

    @pytest.mark.asyncio
    async def test_cancel_importing_raises(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """IMPORTING job cannot be deleted through cancel_job()."""
        job = await _create_job_row(db_session, status=ImportJobStatus.IMPORTING)
        with pytest.raises(ValidationError, match="pause/cancel/rollback"):
            await service.cancel_job(db_session, job.id)

    @pytest.mark.asyncio
    async def test_cancel_paused_import_rolls_back_before_delete(
        self,
        db_session: AsyncSession,
        service: ImportService,
    ) -> None:
        """Deleting a paused import cleans up imported state before removing history."""
        job = await _create_job_row(db_session, status=ImportJobStatus.PAUSED)
        job.import_started_at = datetime.now(UTC)
        await db_session.flush()

        service.rollback_import = AsyncMock()  # type: ignore[method-assign]

        result = await service.cancel_job(db_session, job.id)

        assert result == "deleted"
        service.rollback_import.assert_awaited_once_with(db_session, job.id)
        assert await db_session.get(ImportJob, job.id) is None

    @pytest.mark.asyncio
    async def test_cancel_paused_import_preserves_job_while_story_arc_rollback_is_deferred(
        self,
        db_session: AsyncSession,
        service: ImportService,
    ) -> None:
        """A live placement fence must survive until cooperative rollback resumes."""
        job = await _create_job_row(db_session, status=ImportJobStatus.PAUSED)
        job.import_started_at = datetime.now(UTC)
        await db_session.flush()
        service.rollback_import = AsyncMock(return_value=False)  # type: ignore[method-assign]

        result = await service.cancel_job(db_session, job.id)

        assert result == "rollback_pending"
        service.rollback_import.assert_awaited_once_with(db_session, job.id)
        persisted = await db_session.get(ImportJob, job.id)
        assert persisted is not None
        assert persisted.status == ImportJobStatus.ROLLING_BACK
        assert persisted.control_request == ImportControlRequest.CANCEL

    @pytest.mark.asyncio
    async def test_cancel_paused_import_preserves_incomplete_rollback_evidence(
        self,
        db_session: AsyncSession,
        service: ImportService,
    ) -> None:
        job = await _create_job_row(db_session, status=ImportJobStatus.PAUSED)
        job.import_started_at = datetime.now(UTC)
        await db_session.flush()

        async def incomplete_rollback(session: AsyncSession, job_id: int) -> bool:
            current = await session.get(ImportJob, job_id)
            assert current is not None
            current.status = ImportJobStatus.FAILED
            current.progress_snapshot = {
                "mode": "rollback",
                "phase": "rollback_incomplete",
                "rollback_manual_recovery_count": 1,
            }
            return True

        service.rollback_import = incomplete_rollback  # type: ignore[method-assign]

        result = await service.cancel_job(db_session, job.id)

        assert result == "rollback_incomplete"
        persisted = await db_session.get(ImportJob, job.id)
        assert persisted is job
        assert persisted.status == ImportJobStatus.FAILED
        assert persisted.progress_snapshot["rollback_manual_recovery_count"] == 1


class TestRequestCancel:
    """Test cooperative active-job cancellation semantics."""

    @pytest.mark.asyncio
    async def test_request_cancel_scanning_sets_cancel_request_and_logs_event(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Running scan jobs store a durable cancel request for the runner."""
        job = await _create_job_row(db_session, status=ImportJobStatus.SCANNING)

        await service.request_cancel(db_session, job.id)
        await db_session.refresh(job)

        assert job.status == ImportJobStatus.SCANNING
        assert job.control_request == ImportControlRequest.CANCEL
        assert job.error_message == "Import cancelled by user."

        logs = (
            await db_session.execute(
                select(ImportJobLog).where(ImportJobLog.import_job_id == job.id)
            )
        ).scalars()
        log = logs.one()
        assert log.event == "import_cancel_requested"
        assert log.message == "Import cancelled by user."

    @pytest.mark.asyncio
    async def test_request_cancel_importing_sets_cancel_request(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Import execution uses the same cooperative cancel request."""
        job = await _create_job_row(db_session, status=ImportJobStatus.IMPORTING)
        job.import_started_at = datetime.now(UTC)
        await db_session.flush()

        await service.request_cancel(db_session, job.id)
        await db_session.refresh(job)

        assert job.status == ImportJobStatus.CANCELLING
        assert job.control_request == ImportControlRequest.CANCEL

    @pytest.mark.asyncio
    async def test_request_cancel_paused_import_starts_rollback(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Paused imports cancel by entering rollback rather than restoring review."""
        job = await _create_job_row(db_session, status=ImportJobStatus.PAUSED)
        job.import_started_at = datetime.now(UTC)
        await db_session.flush()

        await service.request_cancel(db_session, job.id)
        await db_session.refresh(job)

        assert job.status == ImportJobStatus.ROLLING_BACK
        assert job.control_request == ImportControlRequest.CANCEL
        assert job.progress_snapshot["mode"] == "rollback"
        assert job.progress_snapshot["phase"] == "queued"

    @pytest.mark.asyncio
    async def test_request_cancel_review_raises(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Review jobs still use discard/delete rather than active-job cancellation."""
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)

        with pytest.raises(ValidationError, match="Cannot cancel"):
            await service.request_cancel(db_session, job.id)


class TestPauseResumeRollback:
    """Test durable lifecycle controls beyond cancel/discard."""

    @pytest.mark.asyncio
    async def test_pause_job_marks_job_paused_and_logs_event(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Pause transitions the durable job state to paused immediately."""
        job = await _create_job_row(db_session, status=ImportJobStatus.MATCHING)

        updated = await service.pause_job(db_session, job.id)
        await db_session.refresh(job)

        assert updated.status == ImportJobStatus.PAUSED
        assert updated.control_request == ImportControlRequest.NONE
        assert job.status == ImportJobStatus.PAUSED
        assert job.control_request == ImportControlRequest.NONE
        logs = (
            await db_session.execute(
                select(ImportJobLog).where(ImportJobLog.import_job_id == job.id)
            )
        ).scalars()
        log = logs.one()
        assert log.event == "import_paused"
        assert log.message == "Scan is paused."

    @pytest.mark.asyncio
    async def test_control_poll_uses_fresh_session_for_cancel_request(
        self,
        service: ImportService,
    ) -> None:
        """Interruptible archive work should poll through a fresh session."""
        with TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "pullbox-control-check.db"
            engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)

                session_factory = async_sessionmaker(engine, expire_on_commit=False)
                async with session_factory() as primary_session:
                    job = await _create_job_row(primary_session, status=ImportJobStatus.IMPORTING)
                    await primary_session.commit()

                    # Start a read transaction before the control request lands.
                    await primary_session.refresh(job)

                    async with session_factory() as control_session:
                        target = await control_session.get(ImportJob, job.id)
                        assert target is not None
                        target.control_request = ImportControlRequest.CANCEL
                        await control_session.commit()

                    with pytest.raises(JobCancelledError):
                        await service._raise_if_job_cancelled_immediately(
                            primary_session,
                            job.id,
                        )
            finally:
                await engine.dispose()

    @pytest.mark.asyncio
    async def test_control_poll_uses_fresh_session_for_pause_request(
        self,
        db_session: AsyncSession,
        service: ImportService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """In-memory test sessions fall back to the active session safely."""
        seen_sessions: list[object] = []

        async def fake_raise_if_job_cancelled(session_obj, _job_id):
            seen_sessions.append(session_obj)
            raise JobPausedError("paused")

        monkeypatch.setattr(
            "pullbox.services.import_service.raise_if_job_cancelled",
            fake_raise_if_job_cancelled,
        )

        with pytest.raises(JobPausedError):
            await service._raise_if_job_cancelled_immediately(db_session, 456)

        assert seen_sessions == [db_session]

    @pytest.mark.asyncio
    async def test_resume_job_uses_snapshot_phase(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Paused jobs resume into the last durable phase from the snapshot."""
        job = await _create_job_row(db_session, status=ImportJobStatus.PAUSED)
        job.import_started_at = datetime.now(UTC)
        job.progress_snapshot = {"phase": "importing", "status": "paused"}
        await db_session.flush()

        updated = await service.resume_job(db_session, job.id)

        assert updated.status == ImportJobStatus.IMPORTING

    @pytest.mark.asyncio
    async def test_request_rollback_marks_job_and_logs_event(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Executed imports can enter rollback mode for reversible cleanup."""
        job = await _create_job_row(db_session, status=ImportJobStatus.COMPLETED)
        job.import_started_at = datetime.now(UTC)
        job.import_completed_at = datetime.now(UTC)
        await db_session.flush()

        updated = await service.request_rollback(db_session, job.id)
        await db_session.refresh(job)

        assert updated.status == ImportJobStatus.ROLLING_BACK
        assert job.status == ImportJobStatus.ROLLING_BACK
        logs = (
            await db_session.execute(
                select(ImportJobLog).where(ImportJobLog.import_job_id == job.id)
            )
        ).scalars()
        assert logs.one().event == "import_rollback_requested"

    @pytest.mark.asyncio
    async def test_request_rollback_requires_started_import(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Rollback is only available once execution has actually started."""
        job = await _create_job_row(db_session, status=ImportJobStatus.COMPLETED)

        with pytest.raises(ValidationError, match="only available after import execution"):
            await service.request_rollback(db_session, job.id)


class TestRetryJob:
    """Test fresh retry semantics for cancelled and rolled-back jobs."""

    @pytest.mark.asyncio
    async def test_retry_job_creates_fresh_new_import_job(
        self,
        db_session: AsyncSession,
        service: ImportService,
        tmp_path: Path,
    ) -> None:
        """Retry creates a new PENDING job instead of reviving the historical one."""
        source_dir = tmp_path / "retry-source"
        source_dir.mkdir()

        original = await _create_job_row(
            db_session,
            source_path=str(source_dir),
            status=ImportJobStatus.CANCELLED,
        )
        original.target_library_root_id = None
        original.monitored = True
        original.search_on_add = True
        original.cv_match_threshold = 0.82
        original.min_files_per_series = 3
        original.file_formats = "cbz, pdf"
        original.convert_to_preferred_format = True
        await db_session.flush()

        retried = await service.retry_job(db_session, original.id)

        assert retried.id != original.id
        assert retried.status == ImportJobStatus.PENDING
        assert retried.source_path == str(source_dir.resolve())
        assert retried.monitored is True
        assert retried.search_on_add is True
        assert retried.cv_match_threshold == pytest.approx(0.82)
        assert retried.min_files_per_series == 3
        assert retried.file_formats == "cbz, pdf"
        assert retried.convert_to_preferred_format is True
        assert retried.progress_snapshot["status"] == ImportJobStatus.PENDING.value
        assert retried.progress_snapshot["mode"] == "scan"
        assert retried.series_found == 0

    @pytest.mark.asyncio
    async def test_retry_job_rejects_non_terminal_history_row(
        self,
        db_session: AsyncSession,
        service: ImportService,
        tmp_path: Path,
    ) -> None:
        source_dir = tmp_path / "retry-source-active"
        source_dir.mkdir()
        job = await _create_job_row(
            db_session,
            source_path=str(source_dir),
            status=ImportJobStatus.COMPLETED,
        )

        with pytest.raises(ValidationError, match="Only cancelled or rolled-back jobs"):
            await service.retry_job(db_session, job.id)

    @pytest.mark.asyncio
    async def test_rollback_import_replays_actions_and_marks_rolled_back(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Rollback execution keeps action ordering, progress, logs, and terminal state stable."""
        job = await _create_job_row(db_session, status=ImportJobStatus.ROLLING_BACK)
        job.import_started_at = datetime.now(UTC)
        job.error_message = "stale failure"
        job.progress_snapshot = {"phase": "rollback", "progress": 10}
        first = ImportJobAction(
            import_job_id=job.id,
            sequence_no=1,
            phase="importing",
            action_type="noop_first",
            status=ImportJobActionStatus.COMPLETED,
            payload={"label": "first"},
        )
        second = ImportJobAction(
            import_job_id=job.id,
            sequence_no=2,
            phase="importing",
            action_type="noop_second",
            status=ImportJobActionStatus.COMPLETED,
            payload={"label": "second"},
        )
        skipped = ImportJobAction(
            import_job_id=job.id,
            sequence_no=3,
            phase="importing",
            action_type="noop_failed",
            status=ImportJobActionStatus.ROLLED_BACK,
            payload={"label": "failed"},
        )
        db_session.add_all([first, second, skipped])
        await db_session.flush()

        rolled_back_sequences: list[int] = []

        async def rollback_action(_session: AsyncSession, action: ImportJobAction) -> None:
            rolled_back_sequences.append(action.sequence_no)

        progress_events: list[int] = []

        async def progress_callback(event: ImportProgressEvent) -> None:
            progress_events.append(event.progress)

        service._rollback_action = rollback_action  # type: ignore[method-assign]

        await service.rollback_import(
            db_session,
            job.id,
            progress_callback=progress_callback,
        )
        await db_session.refresh(job)

        assert rolled_back_sequences == [2, 1]
        assert progress_events == [50, 100]
        assert job.status == ImportJobStatus.ROLLED_BACK
        assert job.error_message is None
        assert job.progress_snapshot == {}
        logs = list(
            (
                await db_session.execute(
                    select(ImportJobLog)
                    .where(ImportJobLog.import_job_id == job.id)
                    .order_by(ImportJobLog.id)
                )
            ).scalars()
        )
        assert [log.event for log in logs] == [
            "import_rollback_started",
            "import_rollback_action_started",
            "import_rollback_action_completed",
            "import_rollback_action_started",
            "import_rollback_action_completed",
            "import_rollback_completed",
        ]
        assert logs[0].data["action_count"] == 2
        assert logs[1].data["sequence_no"] == 2
        assert logs[2].data["sequence_no"] == 2
        assert logs[3].data["sequence_no"] == 1
        assert logs[4].data["sequence_no"] == 1
        assert logs[5].data["action_count"] == 2

    @pytest.mark.asyncio
    async def test_rollback_import_recomputes_series_counters_after_restoring_review_state(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        job = await _create_job_row(db_session, status=ImportJobStatus.ROLLING_BACK)
        job.import_started_at = datetime.now(UTC)
        job.series_found = 2
        job.series_imported = 1
        job.series_failed = 1

        imported_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Imported Series",
            status=ImportSeriesStatus.IMPORTED,
            file_count=1,
        )
        no_match_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Still No Match",
            status=ImportSeriesStatus.NO_MATCH,
            file_count=1,
        )
        db_session.add_all([imported_series, no_match_series])
        await db_session.flush()

        await service.rollback_import(db_session, job.id)
        await db_session.refresh(job)
        await db_session.refresh(imported_series)
        await db_session.refresh(no_match_series)

        assert job.status == ImportJobStatus.ROLLED_BACK
        assert imported_series.status == ImportSeriesStatus.MATCHED
        assert no_match_series.status == ImportSeriesStatus.NO_MATCH
        assert job.series_found == 2
        assert job.series_imported == 0
        assert job.series_failed == 0
        assert job.series_no_match == 1


class TestSeriesSourceMetadata:
    """Test series-level source metadata reconstruction for matching."""

    @pytest.mark.asyncio
    async def test_matching_series_restores_file_level_comicinfo_signal(
        self,
        db_session: AsyncSession,
        service: ImportService,
    ) -> None:
        """Series matching metadata should preserve ComicInfo provenance from persisted files."""
        job = await _create_job_row(db_session)
        series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Chicken Devils",
            raw_year=2022,
            source_folder="/tmp/test",
            file_count=1,
            files_total=1,
            diagnostics={},
        )
        db_session.add(series)
        await db_session.flush()

        db_session.add(
            ImportedFile(
                import_job_id=job.id,
                import_series_id=series.id,
                file_path="/Users/adam/Downloads/test/Chicken Devil 004 (2022).cb7",
                file_name="Chicken Devil 004 (2022).cb7",
                file_format="cb7",
                parsed_series="Chicken Devils",
                parsed_issue_number=4.0,
                parsed_year=2022,
                has_comicinfo=True,
                diagnostics={
                    "metadata_signals": {
                        "series_name": MetadataSignal.COMICINFO.value,
                        "year": MetadataSignal.COMICINFO.value,
                    },
                    "source_metadata": {
                        "comicinfo": {
                            "series": "Chicken Devils",
                            "number": "4",
                            "volume": "2022",
                            "year": 2023,
                            "title": "The Chickens Made Me Do IT",
                            "web": (
                                "https://comicvine.gamespot.com/"
                                "chicken-devils-4-the-chickens-made-me-do-it/4000-996957/"
                            ),
                        }
                    },
                },
            )
        )
        await db_session.flush()

        metadata = await service._source_metadata_for_matching_series(db_session, series)

        assert metadata.signals["series_name"] == MetadataSignal.COMICINFO
        assert metadata.signals["year"] == MetadataSignal.COMICINFO
        assert metadata.diagnostics["comicinfo"]["series"] == "Chicken Devils"
        alternates = metadata.diagnostics["alternate_release_candidates"]
        assert isinstance(alternates, list)
        assert alternates[0]["series_name"] == "Chicken Devil"


# ── Test: get_preview ────────────────────────────────────────────────────


class TestGetPreview:
    """Test get_preview() — returns paginated ImportedSeries."""

    @pytest.mark.asyncio
    async def test_preview_returns_paginated(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """get_preview returns items with pagination."""
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        for i in range(5):
            await _create_imported_series(db_session, job, name=f"Series {i}")

        result = await service.get_preview(db_session, job.id, page=1, page_size=3)

        assert result.total == 5
        assert len(result.items) == 3
        assert result.page == 1
        assert result.page_size == 3

    @pytest.mark.asyncio
    async def test_preview_filters_by_status(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """get_preview can filter by status."""
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        await _create_imported_series(
            db_session,
            job,
            name="Matched",
            status=ImportSeriesStatus.MATCHED,
        )
        await _create_imported_series(
            db_session,
            job,
            name="Dup",
            status=ImportSeriesStatus.DUPLICATE,
        )

        result = await service.get_preview(
            db_session, job.id, status_filter=[ImportSeriesStatus.MATCHED]
        )

        assert result.total == 1
        assert result.items[0].raw_series_name == "Matched"


# ── Test: reconcile_import_series ───────────────────────────────────────


class TestImportReconciliation:
    """Review-time issue reconciliation for matched ComicVine series."""

    @staticmethod
    async def _seed_reconcile_row(
        session: AsyncSession,
    ) -> tuple[ImportJob, ImportedSeries, ImportedFile]:
        job = await _create_job_row(session, status=ImportJobStatus.REVIEW)
        item = await _create_imported_series(
            session,
            job,
            name="Powers 009",
            year=2026,
            status=ImportSeriesStatus.NO_MATCH,
            cv_id=166903,
            cv_match_score=1.0,
            cv_match_method="user_override",
            selected_for_import=False,
        )
        item.user_selected_cv_id = 166903
        item.cv_title = "Powers 25"
        item.cv_year = 2025
        item.cv_issue_count = 9
        item.files_total = 1
        item.files_no_match = 1
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=item.id,
            file_path="/tmp/Powers 25 009 (2026).cbz",
            file_name="Powers 25 009 (2026).cbz",
            file_size=1024,
            file_format="cbz",
            parsed_series="Powers 009",
            parsed_issue_number=25.0,
            parsed_year=2026,
            status=ImportedFileStatus.NO_MATCH,
        )
        session.add(imp_file)
        await session.flush()
        return job, item, imp_file

    @staticmethod
    def _metadata_with_powers_issue() -> AsyncMock:
        metadata_service = AsyncMock()
        metadata_service.get_issue_summaries_for_series = AsyncMock(
            return_value=[
                IssueSummary(
                    provider_id="1167175",
                    issue_number=9.0,
                    title="Issue 9",
                    release_date=None,
                    cover_url=None,
                    issue_type="issue",
                )
            ]
        )
        return metadata_service

    @pytest.mark.asyncio
    async def test_reconcile_assigns_issue_and_makes_series_importable(
        self,
        db_session: AsyncSession,
        mock_series_service: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """Assigning an issue makes the row importable without selecting it."""
        job, item, imp_file = await self._seed_reconcile_row(db_session)
        svc = _make_service(
            series_service=mock_series_service,
            metadata_service=self._metadata_with_powers_issue(),
            event_bus=mock_event_bus,
        )

        updated = await svc.reconcile_import_series(
            db_session,
            job.id,
            item.id,
            ImportReconcileRequest(
                decisions=[
                    ImportReconcileDecision(
                        imported_file_id=imp_file.id,
                        action="assign",
                        issue_cv_id=1167175,
                    )
                ]
            ),
        )

        await db_session.refresh(imp_file)
        assert updated.status == ImportSeriesStatus.MATCHED
        assert updated.selected_for_import is False
        assert updated.files_matched == 1
        assert updated.files_no_match == 0
        assert imp_file.status == ImportedFileStatus.MATCHED
        assert imp_file.matched_issue_cv_id == 1167175
        assert imp_file.match_confidence == "manual"
        assert imp_file.match_method == "import_reconcile"
        assert imp_file.diagnostics["target_issue_summary"] == {
            "provider_id": "1167175",
            "issue_number": 9.0,
            "title": "Issue 9",
            "release_date": None,
            "cover_url": None,
            "issue_type": "issue",
        }

    @pytest.mark.asyncio
    async def test_reconcile_assigns_existing_series_issue_with_local_issue_type(
        self,
        db_session: AsyncSession,
        mock_series_service: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """Existing-library issue choices should save the same diagnostics as CV choices."""
        job, item, imp_file = await self._seed_reconcile_row(db_session)
        library_series = Series(
            title="Thor",
            sort_title="thor",
            year_start=2025,
            comicvine_id=170000,
            issue_count=1,
        )
        db_session.add(library_series)
        await db_session.flush()
        local_issue = Issue(
            series_id=library_series.id,
            comicvine_id=1200001,
            issue_number=1.0,
            title="Issue 1",
            release_date=None,
            cover_url="https://comicvine.gamespot.com/a/uploads/scale_small/thor.jpg",
            issue_type=IssueType.ISSUE,
            status=IssueStatus.SKIPPED,
            metadata_source="comicvine",
        )
        db_session.add(local_issue)
        item.raw_series_name = "Thor"
        item.cv_title = "Thor"
        item.cv_id = 170000
        item.user_selected_cv_id = None
        item.series_id = library_series.id
        imp_file.file_name = "Thor 001 (2025).cbz"
        imp_file.parsed_series = "Thor"
        imp_file.parsed_issue_number = 1.0
        await db_session.flush()

        svc = _make_service(
            series_service=mock_series_service,
            metadata_service=AsyncMock(),
            event_bus=mock_event_bus,
        )

        updated = await svc.reconcile_import_series(
            db_session,
            job.id,
            item.id,
            ImportReconcileRequest(
                decisions=[
                    ImportReconcileDecision(
                        imported_file_id=imp_file.id,
                        action="assign",
                        issue_cv_id=1200001,
                    )
                ]
            ),
        )

        await db_session.refresh(imp_file)
        assert updated.status == ImportSeriesStatus.MATCHED
        assert imp_file.status == ImportedFileStatus.MATCHED
        assert imp_file.matched_issue_id == local_issue.id
        assert imp_file.diagnostics["target_issue_summary"] == {
            "provider_id": "1200001",
            "issue_number": 1.0,
            "title": "Issue 1",
            "release_date": None,
            "cover_url": "https://comicvine.gamespot.com/a/uploads/scale_small/thor.jpg",
            "issue_type": "issue",
        }

    @pytest.mark.asyncio
    async def test_reconcile_context_prefers_archive_page_issue_hint(
        self,
        db_session: AsyncSession,
        mock_series_service: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """Archive page-name hints should preselect the likely issue during review."""
        job, item, imp_file = await self._seed_reconcile_row(db_session)
        item.cv_title = "Hello Darkness"
        item.cv_id = 159025
        item.user_selected_cv_id = 159025
        imp_file.file_name = "Hello Darkness 020 (2026).cbz"
        imp_file.parsed_series = "Hello Darkness"
        imp_file.parsed_issue_number = 20.0
        imp_file.diagnostics = {
            "kind": "metadata_conflict",
            "conflict_type": "archive_entry_issue_number_mismatch",
            "archive_entry_issue_hint": {
                "series_name": "Hello Darkness",
                "issue_number": 21.0,
                "year": 2026,
                "confidence": "strong",
            },
        }
        await db_session.flush()

        metadata_service = AsyncMock()
        metadata_service.get_issue_summaries_for_series = AsyncMock(
            return_value=[
                IssueSummary(
                    provider_id="1162482",
                    issue_number=20.0,
                    title="Away Message",
                    release_date=None,
                    cover_url=None,
                    issue_type="issue",
                ),
                IssueSummary(
                    provider_id="1166406",
                    issue_number=21.0,
                    title="Leading the Witness",
                    release_date=None,
                    cover_url=None,
                    issue_type="issue",
                ),
            ]
        )
        svc = _make_service(
            series_service=mock_series_service,
            metadata_service=metadata_service,
            event_bus=mock_event_bus,
        )

        context = await svc.get_import_reconcile_context(db_session, job.id, item.id)

        file_row = context["files"][0]
        assert file_row["archive_entry_issue_number"] == 21.0
        assert file_row["suggested_issue_cv_id"] == 1166406
        assert file_row["suggested_issue_label"] == "#21 - Leading the Witness"

    @pytest.mark.asyncio
    async def test_reconcile_context_offers_provisional_issue_for_missing_provider_target(
        self,
        db_session: AsyncSession,
        mock_series_service: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """A known series with a missing ComicVine issue can be reconciled locally."""
        job, item, imp_file = await self._seed_reconcile_row(db_session)
        item.raw_series_name = "King Dracula"
        item.raw_year = 2026
        item.cv_title = "King Dracula"
        item.cv_id = 169964
        item.user_selected_cv_id = 169964
        item.cv_year = 2025
        item.cv_issue_count = 3
        imp_file.file_name = "King Dracula 04 (of 04) (2026).cbr"
        imp_file.parsed_series = "King Dracula"
        imp_file.parsed_issue_number = 4.0
        imp_file.parsed_year = 2026
        imp_file.diagnostics = {
            "kind": "provider_issue_target_missing",
            "target_state": "no_provider_issue_target",
            "target_series_cv_id": 169964,
            "requested_issue_number": 4.0,
            "source_metadata": {
                "archive_entry_issue_hint": {
                    "series_name": "King Dracula",
                    "issue_number": 4.0,
                    "confidence": "strong",
                }
            },
        }
        await db_session.flush()

        metadata_service = AsyncMock()
        metadata_service.get_issue_summaries_for_series = AsyncMock(
            return_value=[
                IssueSummary("1100001", 1.0, "Issue 1", None, None, "issue"),
                IssueSummary("1100002", 2.0, "Issue 2", None, None, "issue"),
                IssueSummary("1100003", 3.0, "Issue 3", None, None, "issue"),
            ]
        )
        svc = _make_service(
            series_service=mock_series_service,
            metadata_service=metadata_service,
            event_bus=mock_event_bus,
        )

        context = await svc.get_import_reconcile_context(db_session, job.id, item.id)

        file_row = context["files"][0]
        assert file_row["suggested_issue_cv_id"] is None
        assert file_row["can_create_provisional_issue"] is True
        assert file_row["provisional_issue_number"] == 4.0
        assert file_row["provisional_issue_label"] == "#4"

    @pytest.mark.asyncio
    async def test_reconcile_context_offers_provisional_issue_from_minimal_separator_filename(
        self,
        db_session: AsyncSession,
        mock_series_service: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """Manual series matches can recover issue numbers from dc_connect_72 style names."""
        job, item, imp_file = await self._seed_reconcile_row(db_session)
        item.raw_series_name = "dc_connect_72"
        item.raw_year = None
        item.cv_title = "DC Connect"
        item.cv_id = 127365
        item.user_selected_cv_id = 127365
        item.cv_year = 2020
        item.cv_issue_count = 71
        imp_file.file_path = "/tmp/dc_connect_72.pdf"
        imp_file.file_name = "dc_connect_72.pdf"
        imp_file.file_format = "pdf"
        imp_file.parsed_series = "dc_connect_72"
        imp_file.parsed_issue_number = None
        imp_file.parsed_year = None
        imp_file.diagnostics = {
            "kind": "provider_issue_target_missing",
            "target_state": "no_provider_issue_target",
            "target_series_cv_id": 127365,
            "source_metadata": {
                "filename_parse": {
                    "series_name": "dc_connect_72",
                    "issue_number": None,
                    "year": None,
                    "volume": None,
                    "issue_type": "issue",
                },
                "has_comicinfo": False,
            },
        }
        await db_session.flush()

        metadata_service = AsyncMock()
        metadata_service.get_issue_summaries_for_series = AsyncMock(
            return_value=[
                IssueSummary("900001", 1.0, "Issue 1", None, None, "issue"),
                IssueSummary("900071", 71.0, "Issue 71", None, None, "issue"),
            ]
        )
        svc = _make_service(
            series_service=mock_series_service,
            metadata_service=metadata_service,
            event_bus=mock_event_bus,
        )

        context = await svc.get_import_reconcile_context(db_session, job.id, item.id)

        file_row = context["files"][0]
        assert file_row["parsed_issue_number"] is None
        assert file_row["suggested_issue_cv_id"] is None
        assert file_row["can_create_provisional_issue"] is True
        assert file_row["provisional_issue_number"] == 72.0
        assert file_row["provisional_issue_label"] == "#72"

    @pytest.mark.asyncio
    async def test_reconcile_provisional_issue_marks_row_importable(
        self,
        db_session: AsyncSession,
        mock_series_service: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """A manual provisional issue decision stays unselected until user selection."""
        job, item, imp_file = await self._seed_reconcile_row(db_session)
        item.raw_series_name = "King Dracula"
        item.raw_year = 2026
        item.cv_title = "King Dracula"
        item.cv_id = 169964
        item.user_selected_cv_id = 169964
        item.cv_year = 2025
        item.cv_issue_count = 3
        imp_file.file_name = "King Dracula 04 (of 04) (2026).cbr"
        imp_file.parsed_series = "King Dracula"
        imp_file.parsed_issue_number = 4.0
        imp_file.parsed_year = 2026
        imp_file.diagnostics = {
            "kind": "provider_issue_target_missing",
            "target_state": "no_provider_issue_target",
            "target_series_cv_id": 169964,
            "requested_issue_number": 4.0,
        }
        await db_session.flush()

        metadata_service = AsyncMock()
        metadata_service.get_issue_summaries_for_series = AsyncMock(
            return_value=[
                IssueSummary("1100001", 1.0, "Issue 1", None, None, "issue"),
                IssueSummary("1100002", 2.0, "Issue 2", None, None, "issue"),
                IssueSummary("1100003", 3.0, "Issue 3", None, None, "issue"),
            ]
        )
        svc = _make_service(
            series_service=mock_series_service,
            metadata_service=metadata_service,
            event_bus=mock_event_bus,
        )

        updated = await svc.reconcile_import_series(
            db_session,
            job.id,
            item.id,
            ImportReconcileRequest(
                decisions=[
                    {
                        "imported_file_id": imp_file.id,
                        "action": "provisional",
                        "provisional_issue_number": 4.0,
                    }
                ]
            ),
        )

        await db_session.refresh(imp_file)
        assert updated.status == ImportSeriesStatus.MATCHED
        assert updated.selected_for_import is False
        assert updated.files_matched == 1
        assert updated.files_no_match == 0
        assert imp_file.status == ImportedFileStatus.MATCHED
        assert imp_file.matched_issue_id is None
        assert imp_file.matched_issue_cv_id is None
        assert imp_file.match_confidence == "manual"
        assert imp_file.match_method == "import_reconcile_provisional_issue"
        assert imp_file.diagnostics["kind"] == "provider_missing_issue_placeholder"
        assert imp_file.diagnostics["target_issue_number"] == 4.0
        assert imp_file.diagnostics["target_issue_type"] == IssueType.ISSUE.value

    @pytest.mark.asyncio
    async def test_reconcile_provisional_issue_keeps_duplicate_row_importable(
        self,
        db_session: AsyncSession,
        mock_series_service: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """Existing-library provisional decisions remain duplicate/file-selected imports."""
        job, item, imp_file = await self._seed_reconcile_row(db_session)
        library_series = Series(
            title="DC Connect",
            sort_title="dc connect",
            year_start=2020,
            comicvine_id=127365,
            issue_count=72,
        )
        db_session.add(library_series)
        await db_session.flush()
        db_session.add_all(
            [
                Issue(
                    series_id=library_series.id,
                    issue_number=71.0,
                    comicvine_id=1162715,
                    status=IssueStatus.SKIPPED,
                    metadata_source="comicvine",
                    issue_type=IssueType.ISSUE,
                ),
                Issue(
                    series_id=library_series.id,
                    issue_number=72.0,
                    comicvine_id=None,
                    status=IssueStatus.SKIPPED,
                    metadata_source="provisional_import",
                    issue_type=IssueType.ISSUE,
                ),
            ]
        )
        item.status = ImportSeriesStatus.DUPLICATE
        item.raw_series_name = "dc connect"
        item.raw_year = None
        item.cv_title = "DC Connect"
        item.cv_id = 127365
        item.user_selected_cv_id = None
        item.cv_year = 2020
        item.cv_issue_count = 71
        item.series_id = library_series.id
        item.files_matched = 0
        item.files_no_match = 1
        item.selected_for_import = False
        item.diagnostics = {
            "kind": "duplicate_series",
            "duplicate_reason": "name_year",
            "existing_series_id": library_series.id,
            "actionable_duplicate_merge": True,
            "has_importable_files": False,
            "no_match_files": 1,
        }
        imp_file.file_path = "/tmp/dc_connect_73.pdf"
        imp_file.file_name = "dc_connect_73.pdf"
        imp_file.file_format = "pdf"
        imp_file.parsed_series = "dc connect"
        imp_file.parsed_issue_number = 73.0
        imp_file.parsed_year = None
        imp_file.diagnostics = {
            "kind": "duplicate_series_file",
            "target_state": "no_match",
        }
        await db_session.flush()

        svc = _make_service(
            series_service=mock_series_service,
            metadata_service=AsyncMock(),
            event_bus=mock_event_bus,
        )

        context = await svc.get_import_reconcile_context(db_session, job.id, item.id)
        file_row = context["files"][0]
        assert file_row["can_create_provisional_issue"] is True
        assert file_row["provisional_issue_number"] == 73.0

        updated = await svc.reconcile_import_series(
            db_session,
            job.id,
            item.id,
            ImportReconcileRequest(
                decisions=[
                    {
                        "imported_file_id": imp_file.id,
                        "action": "provisional",
                        "provisional_issue_number": 73.0,
                    }
                ]
            ),
        )

        await db_session.refresh(imp_file)
        assert updated.status == ImportSeriesStatus.DUPLICATE
        assert updated.series_id == library_series.id
        assert updated.selected_for_import is False
        assert updated.files_matched == 1
        assert updated.files_no_match == 0
        assert imp_file.status == ImportedFileStatus.MATCHED
        assert imp_file.include_in_import is False
        assert imp_file.match_method == "import_reconcile_provisional_issue"
        assert imp_file.diagnostics["target_issue_number"] == 73.0
        assert imp_file.diagnostics["target_series_id"] == library_series.id

    @pytest.mark.asyncio
    async def test_reconcile_skip_only_marks_series_skipped(
        self,
        db_session: AsyncSession,
        mock_series_service: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """Skipping every unresolved file removes a non-importable row from Step 4."""
        job, item, imp_file = await self._seed_reconcile_row(db_session)
        svc = _make_service(
            series_service=mock_series_service,
            metadata_service=self._metadata_with_powers_issue(),
            event_bus=mock_event_bus,
        )

        updated = await svc.reconcile_import_series(
            db_session,
            job.id,
            item.id,
            ImportReconcileRequest(
                decisions=[
                    ImportReconcileDecision(
                        imported_file_id=imp_file.id,
                        action="skip",
                    )
                ]
            ),
        )

        await db_session.refresh(imp_file)
        assert updated.status == ImportSeriesStatus.SKIPPED
        assert updated.selected_for_import is False
        assert updated.files_matched == 0
        assert updated.files_no_match == 0
        assert imp_file.status == ImportedFileStatus.SKIPPED
        assert imp_file.match_method == "import_reconcile_skip"

    @pytest.mark.asyncio
    async def test_reconcile_rejects_issue_outside_selected_series(
        self,
        db_session: AsyncSession,
        mock_series_service: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """A Step 3 issue assignment must belong to the selected ComicVine series."""
        job, item, imp_file = await self._seed_reconcile_row(db_session)
        svc = _make_service(
            series_service=mock_series_service,
            metadata_service=self._metadata_with_powers_issue(),
            event_bus=mock_event_bus,
        )

        with pytest.raises(ValidationError, match="not available for this series"):
            await svc.reconcile_import_series(
                db_session,
                job.id,
                item.id,
                ImportReconcileRequest(
                    decisions=[
                        ImportReconcileDecision(
                            imported_file_id=imp_file.id,
                            action="assign",
                            issue_cv_id=999999,
                        )
                    ]
                ),
            )

        await db_session.refresh(imp_file)
        assert imp_file.status == ImportedFileStatus.NO_MATCH


# ── Test: override_cv_id ────────────────────────────────────────────────


class TestOverrideCvId:
    """Test override_cv_id() — user manually sets a CV ID."""

    @pytest.mark.asyncio
    async def test_override_sets_user_cv_id(
        self,
        db_session: AsyncSession,
        mock_series_service: AsyncMock,
        mock_metadata_service: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """override_cv_id fetches CV metadata and sets user_selected_cv_id."""
        from pullbox.providers.base import SeriesMetadata

        meta = SeriesMetadata(
            provider_id="12345",
            title="Batman",
            sort_title="batman",
            year_start=2016,
            year_end=None,
            status="Ended",
            publisher="DC Comics",
            description=None,
            cover_url=None,
            issue_count=85,
            comicvine_url="https://comicvine.gamespot.com/batman/4050-12345/",
        )
        mock_metadata_service._provider.get_series = AsyncMock(return_value=meta)

        svc = ImportService(
            series_service=mock_series_service,
            metadata_service=mock_metadata_service,
            event_bus=mock_event_bus,
        )
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        item = await _create_imported_series(
            db_session, job, status=ImportSeriesStatus.NO_MATCH, cv_id=None
        )
        item.diagnostics = {
            "kind": "series_no_match",
            "top_candidates": [{"title": "Almost Batman"}],
        }
        await db_session.flush()

        updated = await svc.override_cv_id(db_session, item.id, 12345)

        assert updated.user_selected_cv_id == 12345
        assert updated.cv_title == "Batman"
        assert updated.cv_publisher == "DC Comics"
        assert updated.status == ImportSeriesStatus.MATCHED
        assert updated.cv_match_method == "user_override"
        assert updated.diagnostics == {}

    @pytest.mark.asyncio
    async def test_override_recomputes_pending_file_matches(
        self,
        db_session: AsyncSession,
        mock_series_service: AsyncMock,
        mock_metadata_service: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """Manual series override should rerun file matching for that series."""
        from pullbox.providers.base import IssueSummary, SeriesMetadata

        meta = SeriesMetadata(
            provider_id="162966",
            title="Absolute Martian Manhunter",
            sort_title="absolute martian manhunter",
            year_start=2025,
            year_end=None,
            status="Ended",
            publisher="DC Comics",
            description=None,
            cover_url=None,
            issue_count=10,
            comicvine_url="https://comicvine.gamespot.com/absolute-martian-manhunter/4050-162966/",
        )
        mock_metadata_service._provider.get_series = AsyncMock(return_value=meta)
        mock_metadata_service._provider.get_issues_for_series = AsyncMock(
            return_value=[
                IssueSummary(
                    provider_id="1100110",
                    issue_number=1.0,
                    title="Issue 1",
                    release_date=None,
                    cover_url=None,
                    issue_type="issue",
                )
            ]
        )

        svc = ImportService(
            series_service=mock_series_service,
            metadata_service=mock_metadata_service,
            event_bus=mock_event_bus,
        )
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        item = await _create_imported_series(
            db_session, job, name="Absolute Martian Manhunter", status=ImportSeriesStatus.NO_MATCH
        )
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=item.id,
            file_path="/tmp/Absolute Martian Manhunter 001.cbz",
            file_name="Absolute Martian Manhunter 001.cbz",
            file_size=1024,
            file_format="cbz",
            parsed_series="Absolute Martian Manhunter",
            parsed_issue_number=1.0,
            status=ImportedFileStatus.NO_MATCH,
        )
        db_session.add(imp_file)
        await db_session.flush()

        updated = await svc.override_cv_id(db_session, item.id, 162966)

        await db_session.refresh(updated)
        await db_session.refresh(imp_file)
        assert updated.status == ImportSeriesStatus.MATCHED
        assert imp_file.status == ImportedFileStatus.MATCHED
        assert imp_file.matched_issue_cv_id == 1100110
        assert imp_file.match_method == "issue_number"

    @pytest.mark.asyncio
    async def test_override_reconsolidates_matching_logical_group(
        self,
        db_session: AsyncSession,
        mock_series_service: AsyncMock,
        mock_metadata_service: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """Manual series override should merge back into the matching logical series group."""
        from pullbox.providers.base import IssueSummary, SeriesMetadata

        meta = SeriesMetadata(
            provider_id="139451",
            title="Chicken Devil",
            sort_title="chicken devil",
            year_start=2021,
            year_end=None,
            status="Ended",
            publisher="AfterShock Comics",
            description=None,
            cover_url=None,
            issue_count=4,
            comicvine_url="https://comicvine.gamespot.com/chicken-devil/4050-139451/",
        )
        mock_metadata_service._provider.get_series = AsyncMock(return_value=meta)
        mock_metadata_service._provider.get_issues_for_series = AsyncMock(
            return_value=[
                IssueSummary(
                    provider_id="905404",
                    issue_number=4.0,
                    title="The Chicken is in the Details",
                    release_date=None,
                    cover_url=None,
                    issue_type="issue",
                )
            ]
        )

        svc = ImportService(
            series_service=mock_series_service,
            metadata_service=mock_metadata_service,
            event_bus=mock_event_bus,
        )
        job = await _create_job_row(db_session, status=ImportJobStatus.REVIEW)
        canonical = await _create_imported_series(
            db_session,
            job,
            name="Chicken Devil",
            year=2021,
            status=ImportSeriesStatus.MATCHED,
            cv_id=139451,
            cv_match_score=0.95,
            cv_match_method="exact_title_year",
        )
        override_target = await _create_imported_series(
            db_session,
            job,
            name="Chicken Devils",
            year=2022,
            status=ImportSeriesStatus.NO_MATCH,
            cv_id=None,
            cv_match_score=None,
            cv_match_method=None,
        )
        canonical_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=canonical.id,
            file_path="/tmp/Chicken Devil 004 (2022).cbz",
            file_name="Chicken Devil 004 (2022).cbz",
            file_size=1024,
            file_format="cbz",
            parsed_series="Chicken Devil",
            parsed_issue_number=4.0,
            parsed_year=2022,
            comicvine_issue_id=905404,
            status=ImportedFileStatus.CONFLICT,
            conflict_group_id=41,
        )
        override_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=override_target.id,
            file_path="/tmp/Chicken Devil 004 (2022).cb7",
            file_name="Chicken Devil 004 (2022).cb7",
            file_size=2048,
            file_format="cb7",
            parsed_series="Chicken Devil",
            parsed_issue_number=4.0,
            parsed_year=2022,
            status=ImportedFileStatus.NO_MATCH,
        )
        unrelated = await _create_imported_series(
            db_session,
            job,
            name="Abattoir",
            year=2010,
            status=ImportSeriesStatus.MATCHED,
            cv_id=36339,
            cv_match_score=0.95,
            cv_match_method="exact_title_year",
        )
        unrelated_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=unrelated.id,
            file_path="/tmp/Abattoir 005.cbz",
            file_name="Abattoir 005.cbz",
            file_size=512,
            file_format="cbz",
            parsed_series="Abattoir",
            parsed_issue_number=5.0,
            parsed_year=2010,
            status=ImportedFileStatus.CONFLICT,
            conflict_group_id=41,
        )
        db_session.add_all([canonical_file, override_file, unrelated_file])
        canonical.files_total = 1
        override_target.files_total = 1
        await db_session.flush()

        updated = await svc.override_cv_id(db_session, override_target.id, 139451)

        await db_session.flush()
        await db_session.refresh(canonical)
        await db_session.refresh(canonical_file)
        await db_session.refresh(override_file)
        merged_series = await db_session.get(ImportedSeries, override_target.id)

        assert merged_series is None
        assert updated.id == canonical.id
        assert canonical.files_total == 2
        assert override_file.import_series_id == canonical.id
        assert unrelated_file.conflict_group_id == 41
        assert canonical_file.status in {
            ImportedFileStatus.CONFLICT,
            ImportedFileStatus.DUPLICATE_FILE,
            ImportedFileStatus.MATCHED,
        }
        assert override_file.status in {
            ImportedFileStatus.CONFLICT,
            ImportedFileStatus.DUPLICATE_FILE,
            ImportedFileStatus.MATCHED,
        }
        if canonical_file.status == ImportedFileStatus.CONFLICT:
            assert canonical_file.conflict_group_id is not None
            assert canonical_file.conflict_group_id > 41
        if override_file.status == ImportedFileStatus.CONFLICT:
            assert override_file.conflict_group_id is not None
            assert override_file.conflict_group_id > 41


# ── Test: _log_event ─────────────────────────────────────────────────────


class TestLogEvent:
    """Test _log_event() writes to import detail logs, summaries, and the DB."""

    @pytest.mark.asyncio
    async def test_log_persists_to_db(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """_log_event writes an ImportJobLog row."""
        job = await _create_job_row(db_session)

        await service._log_event(
            db_session,
            job.id,
            "INFO",
            "test_event",
            message="Test message",
            extra_key="extra_value",
        )
        await db_session.flush()

        result = await db_session.execute(
            select(ImportJobLog).where(ImportJobLog.import_job_id == job.id)
        )
        logs = result.scalars().all()
        assert len(logs) == 1
        assert logs[0].event == "test_event"
        assert logs[0].level == "INFO"
        assert logs[0].message == "Test message"
        assert logs[0].data["extra_key"] == "extra_value"

    @pytest.mark.asyncio
    async def test_log_event_sanitizes_persisted_payload(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Persisted import job logs should redact sensitive values."""
        job = await _create_job_row(db_session)

        await service._log_event(
            db_session,
            job.id,
            "INFO",
            "test_secret_event",
            message="Using postgres://pullbox:secretpass@db.internal/pullbox",
            api_key="abc123",
            url="http://user:pass@example.com/api?token=secret",
        )
        await db_session.flush()

        result = await db_session.execute(
            select(ImportJobLog).where(ImportJobLog.import_job_id == job.id)
        )
        log = result.scalar_one()
        assert "***REDACTED***" in (log.message or "")
        assert log.data["api_key"] == "***REDACTED***"
        assert log.data["url"] == "http://***REDACTED***@example.com/api?token=***REDACTED***"

    @pytest.mark.asyncio
    async def test_log_event_only_emits_root_summary_for_lifecycle_events(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Non-summary import detail should not hit the root/operator logger."""
        job = await _create_job_row(db_session)
        detail_bound = MagicMock()
        summary_bound = MagicMock()

        with (
            patch("pullbox.services.import_service.import_detail_logger") as mock_detail,
            patch("pullbox.services.import_service.logger") as mock_root,
        ):
            mock_detail.bind.return_value = detail_bound
            mock_root.bind.return_value = summary_bound

            await service._log_event(
                db_session,
                job.id,
                "INFO",
                "import_file_placed",
                message="Placed one file.",
                file_path="/tmp/example.cbz",
            )

        detail_bound.info.assert_called_once()
        summary_bound.info.assert_not_called()

    @pytest.mark.asyncio
    async def test_log_event_emits_root_summary_for_lifecycle_events(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Summary import events should be mirrored to the root/operator logger."""
        job = await _create_job_row(db_session)
        detail_bound = MagicMock()
        summary_bound = MagicMock()

        with (
            patch("pullbox.services.import_service.import_detail_logger") as mock_detail,
            patch("pullbox.services.import_service.logger") as mock_root,
        ):
            mock_detail.bind.return_value = detail_bound
            mock_root.bind.return_value = summary_bound

            await service._log_event(
                db_session,
                job.id,
                "INFO",
                "import_scan_completed",
                message="Scan complete.",
                series_found=12,
            )

        detail_bound.info.assert_called_once()
        summary_bound.info.assert_called_once()

    @pytest.mark.asyncio
    async def test_log_event_publishes_live_sse_payload(
        self, db_session: AsyncSession, service: ImportService
    ) -> None:
        """Live import logs should also publish onto the import SSE channel."""
        job = await _create_job_row(db_session)

        with patch(
            "pullbox.services.import_service.publish",
            new_callable=AsyncMock,
        ) as mock_publish:
            await service._log_event(
                db_session,
                job.id,
                "INFO",
                "import_file_placed",
                message="Placed one file.",
                file_path="/tmp/example.cbz",
            )

        mock_publish.assert_awaited_once()
        args = mock_publish.await_args.args
        assert args[0] == f"import:{job.id}"
        assert args[1] == "log"
        payload = args[2]
        assert payload["job_id"] == job.id
        assert payload["event"] == "import_file_placed"
        assert payload["message"] == "Placed one file."
        assert payload["data"]["file_path"] == "/tmp/example.cbz"
        assert payload["stream_token"]


class TestOrphanRecovery:
    """Tests for unmatched-series recovery imports."""

    @pytest.mark.asyncio
    async def test_recover_orphan_records_series_created_action_for_new_series(
        self,
        db_session: AsyncSession,
        service: ImportService,
    ) -> None:
        root = LibraryRoot(name="Main", path="/library/main")
        db_session.add(root)
        await db_session.flush()

        job = await _create_job_row(
            db_session,
            source_path="/imports",
            status=ImportJobStatus.COMPLETED,
        )
        job.target_library_root_id = root.id
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Henchgirl expanded Edition Comic",
            raw_year=2020,
            status=ImportSeriesStatus.RECOVERY_PENDING,
            file_count=1,
            cv_id=130322,
            user_selected_cv_id=130322,
            cv_title="Henchgirl",
        )
        db_session.add(item)
        await db_session.flush()
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=item.id,
            file_path="/imports/Henchgirl.expanded.Edition.2020.Hybrid.Comic.eBook-BitBook.pdf",
            file_name="Henchgirl.expanded.Edition.2020.Hybrid.Comic.eBook-BitBook.pdf",
            file_size=123,
            file_format="pdf",
            status=ImportedFileStatus.NO_MATCH,
            diagnostics={"kind": "orphan_recovery"},
        )
        db_session.add(imp_file)
        await db_session.flush()

        async def add_from_comicvine(
            session: AsyncSession,
            comicvine_id: int,
            library_root_id: int | None = None,
            *,
            search_on_add: bool = False,
        ) -> Series:
            created = Series(
                title="Henchgirl",
                sort_title="henchgirl",
                year_start=2020,
                comicvine_id=comicvine_id,
                series_type=SeriesType.TPB,
                path="/library/main/Henchgirl (2020) [TPB]",
                library_root_id=library_root_id,
                monitored=search_on_add,
            )
            session.add(created)
            await session.flush()
            session.add(
                Issue(
                    comicvine_id=799319,
                    series_id=created.id,
                    issue_number=1.0,
                    title="Expanded Edition",
                    issue_type=IssueType.TPB,
                    status=IssueStatus.SKIPPED,
                )
            )
            await session.flush()
            return created

        service._series_service.add_from_comicvine.side_effect = add_from_comicvine

        async def mark_imported(
            session: AsyncSession,
            current_job: ImportJob,
            current_item: ImportedSeries,
            *,
            duplicate_mode: bool = False,
            series_id_override: int | None = None,
            report_file_progress=None,
        ) -> tuple[int, int]:
            del current_job, duplicate_mode, series_id_override, report_file_progress
            result = await session.execute(
                select(ImportedFile).where(ImportedFile.import_series_id == current_item.id)
            )
            target_file = result.scalar_one()
            target_file.status = ImportedFileStatus.IMPORTED
            target_file.matched_issue_cv_id = 799319
            return 1, 0

        service._process_series_files = mark_imported

        payload = await service.recover_orphan(
            db_session,
            item.id,
            RecoverOrphanRequest(
                decisions=[
                    OrphanRecoveryDecision(
                        imported_file_id=imp_file.id,
                        action="assign",
                        issue_cv_id=799319,
                    )
                ]
            ),
        )

        action_result = await db_session.execute(
            select(ImportJobAction)
            .where(ImportJobAction.import_job_id == job.id)
            .order_by(ImportJobAction.sequence_no.asc())
        )
        actions = action_result.scalars().all()

        assert payload["status"] == ImportSeriesStatus.IMPORTED
        assert len(actions) == 1
        assert actions[0].action_type == "series_created"
        assert actions[0].payload["series_id"] == item.series_id
        assert actions[0].payload["import_series_id"] == item.id
        assert actions[0].payload["series_ownership_snapshot"] == {
            "schema_version": 1,
            "comicvine_id": 130322,
            "monitored": False,
            "status_override": None,
            "alternate_names": [],
            "parent_series_id": None,
            "preferred_library_root_id": None,
        }
        assert list(actions[0].payload["issue_ownership_snapshot"].values()) == [
            {"status": "skipped", "manual_skip": False}
        ]


# ── Test: Full end-to-end flow ───────────────────────────────────────────


class TestEndToEnd:
    """Full lifecycle: scan 5 series, 2 duplicates, match 3, import 3."""

    @pytest.mark.asyncio
    async def test_full_lifecycle(
        self,
        db_session: AsyncSession,
        mock_series_service: AsyncMock,
        mock_metadata_service: AsyncMock,
        mock_event_bus: AsyncMock,
    ) -> None:
        """End-to-end with mocked CV: 5 discovered, 2 duplicates, 3 imported."""
        # Set up 2 existing series in the library
        for title, year, cvid in [("Batman", 2016, 97508), ("Saga", 2012, 55555)]:
            existing = Series(
                title=title,
                sort_title=title.lower(),
                year_start=year,
                comicvine_id=cvid,
            )
            db_session.add(existing)
        await db_session.flush()

        async def add_from_comicvine(
            session: AsyncSession,
            comicvine_id: int,
            **_kwargs: object,
        ) -> Series:
            target = Series(
                title=f"Imported {comicvine_id}",
                sort_title=f"imported {comicvine_id}",
                year_start=2000,
                comicvine_id=comicvine_id,
            )
            session.add(target)
            await session.flush()
            return target

        mock_series_service.add_from_comicvine.side_effect = add_from_comicvine

        svc = ImportService(
            series_service=mock_series_service,
            metadata_service=mock_metadata_service,
            event_bus=mock_event_bus,
        )
        svc._process_series_files = AsyncMock(return_value=(1, 0))

        # Create the job
        job = await _create_job_row(db_session, source_path="/tmp/comics")

        # 5 discovered series: 2 will be duplicates, 3 new
        discovered = [
            _make_discovered(name="Batman", year=2016),  # duplicate
            _make_discovered(name="Saga", year=2012, source_folder="/comics/Saga"),  # duplicate
            _make_discovered(
                name="Invincible",
                year=2003,
                folder_cv_id=11111,
                source_folder="/comics/Invincible",
            ),
            _make_discovered(
                name="Spawn",
                year=1992,
                folder_cv_id=22222,
                source_folder="/comics/Spawn",
            ),
            _make_discovered(
                name="Hellboy",
                year=1994,
                folder_cv_id=33333,
                source_folder="/comics/Hellboy",
            ),
        ]

        async def mock_scan(root):
            for d in discovered:
                yield d

        cv_ids_by_name = {
            "Invincible": 11111,
            "Spawn": 22222,
            "Hellboy": 33333,
        }

        async def evaluate_match(**kwargs: object) -> ComicVineMatchEvaluation:
            raw_name = str(kwargs["raw_name"])
            cv_id = cv_ids_by_name[raw_name]
            return _matched_evaluation(
                {
                    "cv_id": cv_id,
                    "cv_title": raw_name,
                    "cv_year": 2000,
                    "cv_publisher": "Test Pub",
                    "cv_issue_count": 50,
                    "cv_url": None,
                    "cv_match_score": 0.95,
                    "cv_match_method": "exact_title_year",
                }
            )

        with (
            patch("pullbox.services.import_service.CollectionScanner") as mock_scanner,
            patch(
                "pullbox.services.import_service.evaluate_comicvine_match",
                side_effect=evaluate_match,
            ),
        ):
            mock_scanner.return_value.inventory = AsyncMock(
                return_value=ScanInventory(directory_count=5, file_count=25)
            )
            mock_scanner.return_value.scan = mock_scan
            await svc.start_scan(db_session, job.id)

        # Verify scan results
        assert job.status == ImportJobStatus.REVIEW
        assert job.series_found == 5
        assert job.series_duplicate == 2
        assert job.series_matched == 3

        # Confirm the 3 non-duplicate series
        await db_session.refresh(job, ["series"])
        confirmable = [
            s.id
            for s in job.series
            if s.status in (ImportSeriesStatus.MATCHED, ImportSeriesStatus.NO_MATCH)
        ]
        assert len(confirmable) == 3
        for series in job.series:
            if series.status == ImportSeriesStatus.MATCHED:
                await _create_imported_file(
                    db_session,
                    job,
                    series,
                    file_name=f"{series.raw_series_name} 001.cbz",
                )

        request = ConfirmImportRequest(series_ids=confirmable)
        await svc.confirm_import(db_session, job.id, request)
        assert job.status == ImportJobStatus.IMPORTING

        # Run the import
        await svc.run_import(db_session, job.id)

        assert job.status == ImportJobStatus.COMPLETED
        assert job.series_imported == 3
        assert job.import_started_at is not None
        assert job.import_completed_at is not None
