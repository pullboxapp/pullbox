"""Integration tests for collection import wizard lifecycle.

Scenarios A-E test the full create -> scan -> review -> confirm -> import
flow at the service layer. Scenarios F-H test post-import recovery
(orphaned series, retry failed).
"""

from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path  # noqa: TC003 - used at runtime in helpers
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select as sa_select

from pullbox.core.events import EventBus
from pullbox.core.exceptions import NotFoundError, ProviderError, ValidationError
from pullbox.models.config import SystemConfig
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import LibraryRoot
from pullbox.models.publisher import Publisher
from pullbox.models.series import Series, SeriesStatus
from pullbox.providers.base import IssueSummary, SeriesMetadata, SeriesSearchResult
from pullbox.schemas.import_job import ConfirmImportRequest, ImportJobCreate, RecoverOrphanRequest
from pullbox.services.import_service import ImportService
from scripts.mylar3_import_fixture import create_minimal_cbz, create_mylar3_db

# ── Helpers ───────────────────────────────────────────────────────────


def _make_import_service(
    *,
    series_service: AsyncMock | None = None,
    metadata_service: AsyncMock | None = None,
) -> ImportService:
    """Create an ImportService with mocked dependencies."""
    mock_series = series_service or AsyncMock()
    mock_metadata = metadata_service or AsyncMock()
    event_bus = EventBus()
    return ImportService(
        series_service=mock_series,
        metadata_service=mock_metadata,
        event_bus=event_bus,
    )


async def _seed_completed_job(
    session,
    *,
    source_path: str = "/comics",
    status: ImportJobStatus = ImportJobStatus.COMPLETED,
    series_imported: int = 0,
    series_failed: int = 0,
) -> ImportJob:
    """Create a COMPLETED import job."""
    job = ImportJob(
        source_path=source_path,
        source_type=ImportSourceType.FILESYSTEM,
        status=status,
        series_imported=series_imported,
        series_failed=series_failed,
    )
    session.add(job)
    await session.flush()
    return job


async def _seed_imported_series(
    session,
    job_id: int,
    *,
    name: str = "Test Series",
    status: ImportSeriesStatus = ImportSeriesStatus.NO_MATCH,
    file_count: int = 5,
    cv_id: int | None = None,
) -> ImportedSeries:
    """Create an ImportedSeries row."""
    item = ImportedSeries(
        import_job_id=job_id,
        raw_series_name=name,
        status=status,
        file_count=file_count,
        cv_id=cv_id,
    )
    session.add(item)
    await session.flush()
    return item


def _make_comic_dirs(root: Path, specs: list[tuple[str, int]]) -> None:
    """Create series directories with lightweight, inspectable CBZ files.

    Args:
        root: Root directory to create series folders in.
        specs: List of (folder_name, file_count). Folder name should
               include year in parentheses, e.g., "Batman (2016)".
    """
    for folder_name, file_count in specs:
        series_dir = root / folder_name
        series_dir.mkdir(parents=True, exist_ok=True)
        series_title = folder_name.rsplit(" (", 1)[0]
        for i in range(1, file_count + 1):
            _write_cbz(series_dir / f"{series_title} #{i:03d}.cbz")


def _write_cbz(path: Path, comicinfo_xml: str | None = None) -> None:
    """Write a minimal CBZ fixture that survives safety inspection."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("page001.jpg", b"\xff\xd8\xff\xd9")
        if comicinfo_xml is not None:
            archive.writestr("ComicInfo.xml", comicinfo_xml)


async def _setup_comics_directory(session, comics_dir: Path) -> LibraryRoot:
    """Create a configured comics directory and matching LibraryRoot."""
    comics_dir.mkdir(parents=True, exist_ok=True)
    session.add(SystemConfig(key="comics_directory", value=str(comics_dir), value_type="string"))
    root = LibraryRoot(name="Comics", path=str(comics_dir), enabled=True)
    session.add(root)
    await session.flush()
    return root


def _make_fake_mylar_db(
    db_path: Path,
    series_list: list[dict[str, str | int]],
) -> None:
    """Create a minimal Mylar3 SQLite DB with a comics table."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE comics ("
        "ComicID TEXT, ComicName TEXT, ComicYear TEXT, "
        "ComicPublisher TEXT, ComicLocation TEXT, Status TEXT, Total INTEGER"
        ")"
    )
    for s in series_list:
        conn.execute(
            "INSERT INTO comics VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(s.get("comic_id", "")),
                str(s.get("name", "Unknown")),
                str(s.get("year", "")),
                str(s.get("publisher", "")),
                str(s.get("location", "")),
                "Continuing",
                int(s.get("total", 0)),
            ),
        )
    conn.commit()
    conn.close()


def _cv_search_result(
    *,
    provider_id: str,
    title: str,
    year: int | None = None,
    publisher: str | None = None,
    issue_count: int | None = None,
) -> SeriesSearchResult:
    """Create a SeriesSearchResult for mock CV responses."""
    return SeriesSearchResult(
        provider_id=provider_id,
        title=title,
        year_start=year,
        publisher=publisher,
        issue_count=issue_count,
        status="Continuing",
        cover_url=None,
        description=None,
    )


def _cv_series_metadata(
    *,
    provider_id: str,
    title: str,
    year: int | None = None,
    publisher: str | None = None,
    issue_count: int | None = None,
) -> SeriesMetadata:
    """Create a SeriesMetadata for mock CV get_series responses."""
    return SeriesMetadata(
        provider_id=provider_id,
        title=title,
        sort_title=title,
        year_start=year,
        year_end=None,
        status="Continuing",
        publisher=publisher,
        description=None,
        cover_url=None,
        issue_count=issue_count,
        comicvine_url=f"https://comicvine.gamespot.com/series/{provider_id}/",
    )


def _issue_summary(
    *,
    provider_id: str,
    issue_number: float,
) -> IssueSummary:
    """Create an IssueSummary for mock CV issue-list responses."""
    return IssueSummary(
        provider_id=provider_id,
        issue_number=issue_number,
        title=None,
        release_date=None,
        cover_url=None,
        issue_type="issue",
    )


def _issue_summaries(count: int, *, provider_id_base: int) -> list[IssueSummary]:
    return [
        _issue_summary(provider_id=str(provider_id_base + i), issue_number=float(i))
        for i in range(1, count + 1)
    ]


def _mock_cv_provider(
    *,
    search_map: dict[str, list[SeriesSearchResult]] | None = None,
    get_map: dict[str, SeriesMetadata] | None = None,
    issues_map: dict[str, list[IssueSummary]] | None = None,
) -> AsyncMock:
    """Create an AsyncMock ComicVineProvider with search/get behavior.

    Args:
        search_map: Maps query string -> list of search results.
        get_map: Maps CV ID string -> SeriesMetadata for get_series.
        issues_map: Maps CV ID string -> issue summaries for get_issues_for_series.
    """
    mock_provider = AsyncMock()
    _search = search_map or {}

    async def search_side_effect(query, year=None, *, limit=20, offset=0):
        for key, results in _search.items():
            if key.lower() == query.lower():
                return results
        return []

    mock_provider.search_series = AsyncMock(side_effect=search_side_effect)

    _get = get_map or {}

    async def get_side_effect(cv_id_str):
        if cv_id_str in _get:
            return _get[cv_id_str]
        msg = f"CV ID {cv_id_str} not found"
        raise Exception(msg)

    mock_provider.get_series = AsyncMock(side_effect=get_side_effect)
    _issues = issues_map or {}

    async def issues_side_effect(cv_id_str):
        return _issues.get(cv_id_str, [])

    mock_provider.get_issues_for_series = AsyncMock(side_effect=issues_side_effect)

    return mock_provider


def _mock_series_service(
    cv_to_series: dict[int, int | object],
    errors: dict[int, Exception] | None = None,
) -> AsyncMock:
    """Mock SeriesService where add_from_comicvine returns the mapped object.

    cv_to_series is a mutable dict — populate it with Series IDs or real
    Series objects AFTER the scan phase (to avoid dedup collisions) but
    BEFORE run_import. The side_effect reads from the dict at call time,
    not at setup time.

    Args:
        cv_to_series: Maps ComicVine ID -> Series ID or object with .id.
        errors: Maps ComicVine ID -> Exception to raise.
    """
    mock_svc = AsyncMock()
    _errors = errors or {}

    async def add_side_effect(*args, **kwargs):
        session = args[0] if args else kwargs.get("session")
        cv_id = kwargs.get("comicvine_id")
        if cv_id in _errors:
            raise _errors[cv_id]
        if cv_id in cv_to_series:
            mapped = cv_to_series[cv_id]
            series_id: int | None
            if isinstance(mapped, int):
                series_id = mapped
            else:
                identity = sa_inspect(mapped).identity
                series_id = int(identity[0]) if identity else getattr(mapped, "id", None)
            if session is not None and series_id is not None:
                reloaded = await session.get(Series, series_id)
                if reloaded is not None:
                    return reloaded
            return mapped
        msg = f"Unexpected CV ID in mock: {cv_id}"
        raise ValueError(msg)

    mock_svc.add_from_comicvine = AsyncMock(side_effect=add_side_effect)
    return mock_svc


async def _create_series_in_db(
    session,
    title: str,
    year: int | None = None,
    comicvine_id: int | None = None,
    issue_count: int = 0,
    issue_cv_base: int | None = None,
    publisher_name: str = "DC Comics",
) -> Series:
    """Create and flush a Series row, optionally with numbered Issue rows."""
    pub_result = await session.execute(sa_select(Publisher).where(Publisher.name == publisher_name))
    publisher = pub_result.scalars().first()
    if publisher is None:
        publisher = Publisher(name=publisher_name)
        session.add(publisher)
        await session.flush()

    s = Series(
        title=title,
        sort_title=title.lower(),
        year_start=year,
        comicvine_id=comicvine_id,
        publisher_id=publisher.id,
        status=SeriesStatus.CONTINUING,
        issue_count=issue_count,
    )
    s.publisher = publisher
    session.add(s)
    await session.flush()
    for i in range(1, issue_count + 1):
        issue = Issue(
            series_id=s.id,
            issue_number=float(i),
            comicvine_id=(issue_cv_base + i if issue_cv_base is not None else None),
            issue_type=IssueType.ISSUE,
            status=IssueStatus.WANTED,
        )
        issue.series = s
        session.add(issue)
    await session.flush()
    return s


# ── Scenario A: Full Filesystem Import ────────────────────────────────


class TestFilesystemImport:
    """Scenario A: Full filesystem scan -> match -> import lifecycle."""

    async def test_a_full_lifecycle(self, db_session, tmp_path) -> None:
        """Create job -> scan 3 dirs -> match via CV -> confirm all -> import all."""
        library_root = await _setup_comics_directory(db_session, tmp_path / "library")
        source = tmp_path / "import"

        # 1. Create 3 series directories with CBZ files
        _make_comic_dirs(
            source,
            [
                ("Batman (2016)", 3),
                ("Saga (2012)", 2),
                ("Invincible (2003)", 1),
            ],
        )

        # 2. Mock CV provider to return matching search results
        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [
                    _cv_search_result(
                        provider_id="97508",
                        title="Batman",
                        year=2016,
                        publisher="DC Comics",
                        issue_count=150,
                    ),
                ],
                "Saga": [
                    _cv_search_result(
                        provider_id="42692",
                        title="Saga",
                        year=2012,
                        publisher="Image Comics",
                        issue_count=66,
                    ),
                ],
                "Invincible": [
                    _cv_search_result(
                        provider_id="18816",
                        title="Invincible",
                        year=2003,
                        publisher="Image Comics",
                        issue_count=144,
                    ),
                ],
            },
            issues_map={
                "97508": _issue_summaries(3, provider_id_base=100000),
                "42692": _issue_summaries(2, provider_id_base=200000),
                "18816": _issue_summaries(1, provider_id_base=300000),
            },
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider

        # Mutable dict — populated after scan to avoid dedup collisions
        cv_to_series: dict[int, object] = {}
        mock_series_svc = _mock_series_service(cv_to_series)

        svc = _make_import_service(
            series_service=mock_series_svc,
            metadata_service=mock_metadata,
        )

        # 3. Create job
        request = ImportJobCreate(
            source_path=str(source),
            source_type=ImportSourceType.FILESYSTEM,
        )
        job = await svc.create_job(db_session, request)
        assert job.status == ImportJobStatus.PENDING

        # 4. Start scan -> SCANNING -> ANALYZING -> MATCHING -> REVIEW
        await svc.start_scan(db_session, job.id)
        await db_session.refresh(job)

        assert job.status == ImportJobStatus.REVIEW
        assert job.series_found == 3
        assert job.series_matched == 3
        assert job.series_no_match == 0
        assert job.series_duplicate == 0

        # 5. All 3 ImportedSeries rows are MATCHED with CV data
        result = await db_session.execute(
            sa_select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
        )
        items = result.scalars().all()
        assert len(items) == 3
        assert all(i.status == ImportSeriesStatus.MATCHED for i in items)
        assert all(i.cv_id is not None for i in items)

        # 6. Confirm all 3
        confirm_req = ConfirmImportRequest(
            series_ids=[i.id for i in items],
            target_library_root_id=library_root.id,
        )
        job = await svc.confirm_import(db_session, job.id, confirm_req)
        assert job.status == ImportJobStatus.IMPORTING

        # 7. Create real Series in DB for FK (after dedup, before import)
        batman = await _create_series_in_db(
            db_session,
            "Batman",
            2016,
            97508,
            issue_count=3,
            issue_cv_base=100000,
        )
        cv_to_series[97508] = batman.id
        saga = await _create_series_in_db(
            db_session,
            "Saga",
            2012,
            42692,
            issue_count=2,
            issue_cv_base=200000,
        )
        cv_to_series[42692] = saga.id
        cv_to_series[18816] = await _create_series_in_db(
            db_session,
            "Invincible",
            2003,
            18816,
            issue_count=1,
            issue_cv_base=300000,
        )

        # 8. Run import
        await svc.run_import(db_session, job.id)
        await db_session.refresh(job)

        assert job.status == ImportJobStatus.COMPLETED
        assert job.series_imported == 3
        assert job.series_failed == 0

        # 9. All IMPORTED with series_id set
        result = await db_session.execute(
            sa_select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
        )
        items = result.scalars().all()
        assert all(i.status == ImportSeriesStatus.IMPORTED for i in items)
        assert all(i.series_id is not None for i in items)

    async def test_trusted_comicinfo_folder_reaches_review_without_provider_calls(
        self,
        db_session,
        tmp_path,
    ) -> None:
        class ExplodingProvider:
            def __getattr__(self, method_name: str):
                async def fail(*_args, **_kwargs):
                    raise AssertionError(f"ComicVine method {method_name} must not be called")

                return fail

        source = tmp_path / "import"
        series_dir = source / "Batman (2016)"
        issue_one = series_dir / "Batman 001 (2016).cbz"
        issue_two = series_dir / "Batman 002 (2016).cbz"
        _write_cbz(
            issue_one,
            """<?xml version="1.0"?>
            <ComicInfo>
              <Series>Batman</Series>
              <Number>1</Number>
              <Volume>2016</Volume>
              <Title>I Am Gotham</Title>
              <Notes>[cv_vol_id:97508] [cv_issue_id:700001]</Notes>
            </ComicInfo>
            """,
        )
        _write_cbz(
            issue_two,
            """<?xml version="1.0"?>
            <ComicInfo>
              <Series>Batman</Series>
              <Number>2</Number>
              <Volume>2016</Volume>
              <Title>I Am Suicide</Title>
              <Notes>[cv_vol_id:97508]</Notes>
            </ComicInfo>
            """,
        )
        metadata_service = AsyncMock()
        metadata_service._provider = ExplodingProvider()
        service = _make_import_service(
            series_service=_mock_series_service({}),
            metadata_service=metadata_service,
        )
        job = await service.create_job(
            db_session,
            ImportJobCreate(
                source_path=str(source),
                source_type=ImportSourceType.FILESYSTEM,
            ),
        )

        await service.start_scan(db_session, job.id)
        await db_session.refresh(job)

        assert job.status == ImportJobStatus.REVIEW
        assert job.series_found == 1
        series_item = await db_session.scalar(
            sa_select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
        )
        assert series_item is not None
        assert series_item.status == ImportSeriesStatus.MATCHED
        assert series_item.cv_id == 97508
        assert series_item.cv_match_method == "comicinfo_cv_id"
        file_result = await db_session.execute(
            sa_select(ImportedFile).where(ImportedFile.import_job_id == job.id)
        )
        imported_files = {item.file_name: item for item in file_result.scalars().all()}
        assert imported_files[issue_one.name].status == ImportedFileStatus.MATCHED
        assert imported_files[issue_one.name].matched_issue_cv_id == 700001
        assert imported_files[issue_two.name].status == ImportedFileStatus.MATCHED
        assert imported_files[issue_two.name].matched_issue_cv_id is None
        assert imported_files[issue_two.name].match_method == "import_reconcile_provisional_issue"

    async def test_conflicting_folder_ids_reach_review_without_provider_calls(
        self,
        db_session,
        tmp_path,
    ) -> None:
        class ExplodingProvider:
            def __getattr__(self, method_name: str):
                async def fail(*_args, **_kwargs):
                    raise AssertionError(f"ComicVine method {method_name} must not be called")

                return fail

        source = tmp_path / "import"
        series_dir = source / "Batman (2016)"
        series_dir.mkdir(parents=True)
        (series_dir / "series.json").write_text('{"comicid": 11111}')
        _write_cbz(
            series_dir / "Batman 001 (2016).cbz",
            """<?xml version="1.0"?>
            <ComicInfo>
              <Series>Batman</Series>
              <Number>1</Number>
              <Volume>2016</Volume>
              <Notes>[cv_vol_id:97508] [cv_issue_id:700001]</Notes>
            </ComicInfo>
            """,
        )
        metadata_service = AsyncMock()
        metadata_service._provider = ExplodingProvider()
        service = _make_import_service(
            series_service=_mock_series_service({}),
            metadata_service=metadata_service,
        )
        job = await service.create_job(
            db_session,
            ImportJobCreate(
                source_path=str(source),
                source_type=ImportSourceType.FILESYSTEM,
            ),
        )

        await service.start_scan(db_session, job.id)
        await db_session.refresh(job)

        assert job.status == ImportJobStatus.REVIEW
        series_item = await db_session.scalar(
            sa_select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
        )
        assert series_item is not None
        assert series_item.status == ImportSeriesStatus.NO_MATCH
        assert series_item.diagnostics["kind"] == "series_conflict"
        assert series_item.diagnostics["reason"] == "trusted_source_identity_conflict"


# ── Scenario B: Mylar3 Import ─────────────────────────────────────────


class TestMylar3Import:
    """Scenario B: Import from Mylar3 database with CV ID lookup."""

    async def test_b_mylar3_lifecycle(self, db_session, tmp_path) -> None:
        """Mylar3 DB with 2 CV-ID lookups + 1 search match -> import."""
        comics_dir = tmp_path / "comics"
        mylar_db = tmp_path / "mylar.db"
        library_root = await _setup_comics_directory(db_session, tmp_path / "library")

        # 1. Create comic directories referenced by Mylar3 DB
        for folder, count in [
            ("Batman (2016)", 3),
            ("Saga (2012)", 2),
            ("Invincible (2003)", 1),
        ]:
            d = comics_dir / folder
            d.mkdir(parents=True)
            series_title = folder.rsplit(" (", 1)[0]
            for i in range(1, count + 1):
                _write_cbz(d / f"{series_title} #{i:03d}.cbz")

        # 2. Create fake Mylar3 DB: 2 with CV IDs, 1 without
        _make_fake_mylar_db(
            mylar_db,
            [
                {
                    "comic_id": "CV-97508",
                    "name": "Batman",
                    "year": "2016",
                    "publisher": "DC Comics",
                    "location": str(comics_dir / "Batman (2016)"),
                },
                {
                    "comic_id": "CV-42692",
                    "name": "Saga",
                    "year": "2012",
                    "publisher": "Image Comics",
                    "location": str(comics_dir / "Saga (2012)"),
                },
                {
                    "comic_id": "",
                    "name": "Invincible",
                    "year": "2003",
                    "publisher": "Image Comics",
                    "location": str(comics_dir / "Invincible (2003)"),
                },
            ],
        )

        # 3. Mock CV: get_series for known CV IDs, search for unknown
        mock_provider = _mock_cv_provider(
            get_map={
                "97508": _cv_series_metadata(
                    provider_id="97508",
                    title="Batman",
                    year=2016,
                    publisher="DC Comics",
                    issue_count=150,
                ),
                "42692": _cv_series_metadata(
                    provider_id="42692",
                    title="Saga",
                    year=2012,
                    publisher="Image Comics",
                    issue_count=66,
                ),
            },
            search_map={
                "Invincible": [
                    _cv_search_result(
                        provider_id="18816",
                        title="Invincible",
                        year=2003,
                        publisher="Image Comics",
                        issue_count=144,
                    ),
                ],
            },
            issues_map={
                "97508": _issue_summaries(3, provider_id_base=100000),
                "42692": _issue_summaries(2, provider_id_base=200000),
                "18816": _issue_summaries(1, provider_id_base=300000),
            },
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider

        # Mutable dict — populated after scan to avoid dedup collisions
        cv_to_series: dict[int, object] = {}
        mock_series_svc = _mock_series_service(cv_to_series)

        svc = _make_import_service(
            series_service=mock_series_svc,
            metadata_service=mock_metadata,
        )

        # 4. Create job
        request = ImportJobCreate(
            source_path=str(mylar_db),
            source_type=ImportSourceType.MYLAR3,
        )
        job = await svc.create_job(db_session, request)

        # 5. Start scan
        await svc.start_scan(db_session, job.id)
        await db_session.refresh(job)

        assert job.status == ImportJobStatus.REVIEW
        assert job.series_found == 3

        # 6. Verify match methods
        result = await db_session.execute(
            sa_select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
        )
        items = result.scalars().all()

        mylar_matched = [i for i in items if i.cv_match_method == "mylar3_cv_id"]
        assert len(mylar_matched) == 2
        assert all(i.cv_match_score == 1.0 for i in mylar_matched)

        # Third series matched via search (fuzzy or exact)
        search_matched = [
            i
            for i in items
            if i.cv_match_method and i.cv_match_method not in ("mylar3_cv_id", "comicinfo_cv_id")
        ]
        no_match = [i for i in items if i.status == ImportSeriesStatus.NO_MATCH]
        assert len(search_matched) + len(no_match) == 1

        # 7. Confirm all matched series
        matched_ids = [i.id for i in items if i.status == ImportSeriesStatus.MATCHED]
        confirm_req = ConfirmImportRequest(
            series_ids=matched_ids,
            target_library_root_id=library_root.id,
        )
        job = await svc.confirm_import(db_session, job.id, confirm_req)
        assert job.status == ImportJobStatus.IMPORTING

        # 8. Create real Series in DB (after dedup, before import)
        cv_to_series[97508] = await _create_series_in_db(
            db_session,
            "Batman",
            2016,
            97508,
            issue_count=3,
            issue_cv_base=100000,
        )
        cv_to_series[42692] = await _create_series_in_db(
            db_session,
            "Saga",
            2012,
            42692,
            issue_count=2,
            issue_cv_base=200000,
        )
        cv_to_series[18816] = await _create_series_in_db(
            db_session,
            "Invincible",
            2003,
            18816,
            issue_count=1,
            issue_cv_base=300000,
        )

        # 9. Import
        await svc.run_import(db_session, job.id)
        await db_session.refresh(job)

        assert job.status == ImportJobStatus.COMPLETED
        assert job.series_imported == len(matched_ids)
        assert job.series_failed == 0

    async def test_trusted_mylar_annual_reaches_review_without_provider_calls(
        self,
        db_session,
        tmp_path,
    ) -> None:
        class ExplodingProvider:
            def __getattr__(self, method_name: str):
                async def fail(*_args, **_kwargs):
                    raise AssertionError(f"ComicVine method {method_name} must not be called")

                return fail

        series_dir = tmp_path / "comics" / "X-Men"
        regular_path = series_dir / "X-Men 001 (2021).cbz"
        annual_path = series_dir / "X-Men Annual 001 (2023).cbz"
        create_minimal_cbz(regular_path)
        create_minimal_cbz(annual_path)
        mylar_db = tmp_path / "mylar.db"
        create_mylar3_db(
            mylar_db,
            series=[
                {
                    "ComicID": "CV-140553",
                    "ComicName": "X-Men",
                    "ComicYear": "2021",
                    "ComicPublisher": "Marvel",
                    "ComicLocation": str(series_dir),
                    "Total": 30,
                }
            ],
            issues=[
                {
                    "IssueID": "900001",
                    "ComicID": "140553",
                    "ComicName": "X-Men",
                    "IssueName": "Fearless",
                    "Issue_Number": "1",
                    "Location": regular_path.name,
                    "IssueDate": "2021-09-01",
                }
            ],
            annuals=[
                {
                    "IssueID": "950001",
                    "ComicID": "140553",
                    "ComicName": "X-Men",
                    "IssueName": "Contest of Chaos",
                    "Issue_Number": "1",
                    "Location": annual_path.name,
                    "IssueDate": "2023-08-16",
                    "ReleaseComicID": "CV-153726",
                    "ReleaseComicName": "X-Men Annual",
                }
            ],
        )
        metadata_service = AsyncMock()
        metadata_service._provider = ExplodingProvider()
        service = _make_import_service(
            series_service=_mock_series_service({}),
            metadata_service=metadata_service,
        )
        job = await service.create_job(
            db_session,
            ImportJobCreate(
                source_path=str(mylar_db),
                source_type=ImportSourceType.MYLAR3,
            ),
        )

        await service.start_scan(db_session, job.id)
        await db_session.refresh(job)

        assert job.status == ImportJobStatus.REVIEW
        assert job.series_found == 2
        series_result = await db_session.execute(
            sa_select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
        )
        imported_series = series_result.scalars().all()
        assert {item.cv_id for item in imported_series} == {140553, 153726}
        assert all(item.status == ImportSeriesStatus.MATCHED for item in imported_series)
        assert all(item.cv_match_method == "mylar3_cv_id" for item in imported_series)

        file_result = await db_session.execute(
            sa_select(ImportedFile).where(ImportedFile.import_job_id == job.id)
        )
        imported_files = file_result.scalars().all()
        assert len(imported_files) == 2
        assert all(item.status == ImportedFileStatus.MATCHED for item in imported_files)
        by_name = {item.file_name: item for item in imported_files}
        assert by_name[regular_path.name].matched_issue_cv_id == 900001
        assert by_name[annual_path.name].matched_issue_cv_id == 950001

    async def test_incomplete_trusted_mylar_row_reaches_review_without_provider_calls(
        self,
        db_session,
        tmp_path,
    ) -> None:
        class ExplodingProvider:
            def __getattr__(self, method_name: str):
                async def fail(*_args, **_kwargs):
                    raise AssertionError(f"ComicVine method {method_name} must not be called")

                return fail

        series_dir = tmp_path / "comics" / "X-Men"
        unresolved_path = series_dir / "X-Men Special (2023).cbz"
        create_minimal_cbz(unresolved_path)
        mylar_db = tmp_path / "mylar.db"
        create_mylar3_db(
            mylar_db,
            series=[
                {
                    "ComicID": "CV-140553",
                    "ComicName": "X-Men",
                    "ComicYear": "2021",
                    "ComicPublisher": "Marvel",
                    "ComicLocation": str(series_dir),
                    "Total": 1,
                }
            ],
            issues=[],
        )
        metadata_service = AsyncMock()
        metadata_service._provider = ExplodingProvider()
        service = _make_import_service(
            series_service=_mock_series_service({}),
            metadata_service=metadata_service,
        )
        job = await service.create_job(
            db_session,
            ImportJobCreate(
                source_path=str(mylar_db),
                source_type=ImportSourceType.MYLAR3,
            ),
        )

        await service.start_scan(db_session, job.id)
        await db_session.refresh(job)

        assert job.status == ImportJobStatus.REVIEW
        series_item = await db_session.scalar(
            sa_select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
        )
        assert series_item is not None
        assert series_item.status == ImportSeriesStatus.MATCHED
        assert series_item.cv_id == 140553
        assert series_item.cv_match_method == "mylar3_cv_id"
        file_item = await db_session.scalar(
            sa_select(ImportedFile).where(ImportedFile.import_job_id == job.id)
        )
        assert file_item is not None
        assert file_item.status == ImportedFileStatus.NO_MATCH


# ── Scenario C: Deduplication ──────────────────────────────────────────


class TestDeduplication:
    """Scenario C: Deduplication against existing library."""

    async def test_c_dedup_flags_existing(self, db_session, tmp_path) -> None:
        """2 existing series flagged as duplicates; 3 new ones imported."""
        library_root = await _setup_comics_directory(db_session, tmp_path / "library")

        # 1. Pre-seed 2 Series that will match scan results
        existing_batman = Series(
            title="Batman",
            sort_title="batman",
            year_start=2016,
            comicvine_id=97508,
            status=SeriesStatus.CONTINUING,
            issue_count=0,
        )
        existing_saga = Series(
            title="Saga",
            sort_title="saga",
            year_start=2012,
            comicvine_id=42692,
            status=SeriesStatus.CONTINUING,
            issue_count=0,
        )
        db_session.add_all([existing_batman, existing_saga])
        await db_session.flush()

        # 2. Create 5 series directories: 2 existing + 3 new
        source = tmp_path / "import"
        _make_comic_dirs(
            source,
            [
                ("Batman (2016)", 3),
                ("Saga (2012)", 2),
                ("Invincible (2003)", 1),
                ("X-Men (2019)", 4),
                ("Spider-Man (2022)", 2),
            ],
        )

        # 3. Mock CV for the 3 new series (duplicates never reach matching)
        mock_provider = _mock_cv_provider(
            search_map={
                "Invincible": [
                    _cv_search_result(
                        provider_id="18816",
                        title="Invincible",
                        year=2003,
                        publisher="Image Comics",
                    ),
                ],
                "X-Men": [
                    _cv_search_result(
                        provider_id="77059",
                        title="X-Men",
                        year=2019,
                        publisher="Marvel Comics",
                    ),
                ],
                "Spider-Man": [
                    _cv_search_result(
                        provider_id="88123",
                        title="Spider-Man",
                        year=2022,
                        publisher="Marvel Comics",
                    ),
                ],
            },
            issues_map={
                "18816": _issue_summaries(1, provider_id_base=300000),
                "77059": _issue_summaries(4, provider_id_base=400000),
                "88123": _issue_summaries(2, provider_id_base=500000),
            },
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider

        # Mutable dict — populated after scan
        cv_to_series: dict[int, object] = {}
        mock_series_svc = _mock_series_service(cv_to_series)

        svc = _make_import_service(
            series_service=mock_series_svc,
            metadata_service=mock_metadata,
        )

        # 4. Create job and scan
        request = ImportJobCreate(
            source_path=str(source),
            source_type=ImportSourceType.FILESYSTEM,
        )
        job = await svc.create_job(db_session, request)
        await svc.start_scan(db_session, job.id)
        await db_session.refresh(job)

        assert job.status == ImportJobStatus.REVIEW
        assert job.series_found == 5
        assert job.series_duplicate == 2

        # 5. Verify statuses
        result = await db_session.execute(
            sa_select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
        )
        items = result.scalars().all()

        duplicates = [i for i in items if i.status == ImportSeriesStatus.DUPLICATE]
        matched = [i for i in items if i.status == ImportSeriesStatus.MATCHED]
        assert len(duplicates) == 2
        assert len(matched) == 3

        # Duplicate names should be the pre-seeded ones
        dup_names = {i.raw_series_name for i in duplicates}
        assert "Batman" in dup_names
        assert "Saga" in dup_names

        # 6. Confirm only the 3 non-duplicates
        confirm_req = ConfirmImportRequest(
            series_ids=[i.id for i in matched],
            target_library_root_id=library_root.id,
        )
        job = await svc.confirm_import(db_session, job.id, confirm_req)

        # 7. Create target Series in DB (after dedup, before import)
        cv_to_series[18816] = await _create_series_in_db(
            db_session,
            "Invincible",
            2003,
            18816,
            issue_count=1,
            issue_cv_base=300000,
        )
        cv_to_series[77059] = await _create_series_in_db(
            db_session,
            "X-Men",
            2019,
            77059,
            issue_count=4,
            issue_cv_base=400000,
        )
        cv_to_series[88123] = await _create_series_in_db(
            db_session,
            "Spider-Man",
            2022,
            88123,
            issue_count=2,
            issue_cv_base=500000,
        )

        # 8. Import
        await svc.run_import(db_session, job.id)
        await db_session.refresh(job)

        assert job.status == ImportJobStatus.COMPLETED
        assert job.series_imported == 3
        assert job.series_failed == 0


# ── Scenario D: Partial Failure ────────────────────────────────────────


class TestPartialFailure:
    """Scenario D: Partial import failure (2 succeed, 1 fails)."""

    async def test_d_partial_failure(self, db_session, tmp_path) -> None:
        """2 of 3 imports succeed; 1 raises ProviderError."""
        library_root = await _setup_comics_directory(db_session, tmp_path / "library")

        # 1. Create 3 series directories
        source = tmp_path / "import"
        _make_comic_dirs(
            source,
            [
                ("Batman (2016)", 3),
                ("Saga (2012)", 2),
                ("Invincible (2003)", 1),
            ],
        )

        # 2. Mock CV matching for all 3
        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [
                    _cv_search_result(
                        provider_id="97508",
                        title="Batman",
                        year=2016,
                        publisher="DC Comics",
                    ),
                ],
                "Saga": [
                    _cv_search_result(
                        provider_id="42692",
                        title="Saga",
                        year=2012,
                        publisher="Image Comics",
                    ),
                ],
                "Invincible": [
                    _cv_search_result(
                        provider_id="18816",
                        title="Invincible",
                        year=2003,
                        publisher="Image Comics",
                    ),
                ],
            },
            issues_map={
                "97508": _issue_summaries(3, provider_id_base=100000),
                "42692": _issue_summaries(2, provider_id_base=200000),
                "18816": _issue_summaries(1, provider_id_base=300000),
            },
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider

        # 3. Mutable dict for successful imports; 1 error
        cv_to_series: dict[int, object] = {}
        mock_series_svc = _mock_series_service(
            cv_to_series,
            errors={18816: ProviderError("comicvine", "CV API down")},
        )

        svc = _make_import_service(
            series_service=mock_series_svc,
            metadata_service=mock_metadata,
        )

        # 4. Create job and scan
        request = ImportJobCreate(
            source_path=str(source),
            source_type=ImportSourceType.FILESYSTEM,
        )
        job = await svc.create_job(db_session, request)
        await svc.start_scan(db_session, job.id)
        await db_session.refresh(job)

        # 5. Confirm all 3
        result = await db_session.execute(
            sa_select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
        )
        items = result.scalars().all()
        confirm_req = ConfirmImportRequest(
            series_ids=[i.id for i in items],
            target_library_root_id=library_root.id,
        )
        job = await svc.confirm_import(db_session, job.id, confirm_req)

        # 6. Create Series for the 2 that will succeed (after dedup)
        batman = await _create_series_in_db(
            db_session,
            "Batman",
            2016,
            97508,
            issue_count=3,
            issue_cv_base=100000,
        )
        cv_to_series[97508] = batman.id
        saga = await _create_series_in_db(
            db_session,
            "Saga",
            2012,
            42692,
            issue_count=2,
            issue_cv_base=200000,
        )
        cv_to_series[42692] = saga.id

        # 7. Run import (1 will fail)
        await svc.run_import(db_session, job.id)
        await db_session.refresh(job)

        assert job.status == ImportJobStatus.COMPLETED  # partial success
        assert job.series_imported == 2
        assert job.series_failed == 1

        # 8. Verify failed series has error_message
        result = await db_session.execute(
            sa_select(ImportedSeries).where(
                ImportedSeries.import_job_id == job.id,
                ImportedSeries.status == ImportSeriesStatus.FAILED,
            )
        )
        failed_items = result.scalars().all()
        assert len(failed_items) == 1
        assert failed_items[0].error_message is not None
        assert "CV API down" in failed_items[0].error_message

        # 9. Job status is COMPLETED (not FAILED — partial success)
        assert job.status == ImportJobStatus.COMPLETED


# ── Scenario E: Cancel ────────────────────────────────────────────────


class TestCancel:
    """Scenario E: Cancel an import job from REVIEW state."""

    async def test_e_cancel_from_review(self, db_session, tmp_path) -> None:
        """Cancel a REVIEW-state job; confirm_import raises ValidationError."""
        # 1. Create a dir and scan to REVIEW
        _make_comic_dirs(tmp_path, [("Batman (2016)", 3)])

        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [
                    _cv_search_result(
                        provider_id="97508",
                        title="Batman",
                        year=2016,
                        publisher="DC Comics",
                    ),
                ],
            },
            issues_map={
                "97508": _issue_summaries(3, provider_id_base=100000),
            },
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider

        svc = _make_import_service(metadata_service=mock_metadata)

        request = ImportJobCreate(
            source_path=str(tmp_path),
            source_type=ImportSourceType.FILESYSTEM,
        )
        job = await svc.create_job(db_session, request)
        await svc.start_scan(db_session, job.id)
        await db_session.refresh(job)
        assert job.status == ImportJobStatus.REVIEW

        # 2. Cancel — job is deleted entirely
        job_id = job.id
        await svc.cancel_job(db_session, job_id)
        assert await db_session.get(ImportJob, job_id) is None

        # 3. Attempting confirm on deleted job raises NotFoundError
        with pytest.raises(NotFoundError):
            await svc.confirm_import(
                db_session,
                job_id,
                ConfirmImportRequest(series_ids=[1]),
            )


# ── Scenario F & G: Orphaned Series ──────────────────────────────────


class TestOrphanedSeries:
    """Tests for get_orphaned_series, get_orphaned_count, assign_cv_to_orphan, dismiss_orphan."""

    async def test_f1_list_orphaned_from_completed_jobs(self, db_session) -> None:
        """F1: get_orphaned_series returns active unmatched rows from COMPLETED jobs."""
        svc = _make_import_service()

        # Two COMPLETED jobs, each with 2 NO_MATCH series
        job1 = await _seed_completed_job(db_session)
        job2 = await _seed_completed_job(db_session, source_path="/more-comics")
        await _seed_imported_series(db_session, job1.id, name="Series A")
        await _seed_imported_series(db_session, job1.id, name="Series B")
        await _seed_imported_series(db_session, job2.id, name="Series C")
        await _seed_imported_series(db_session, job2.id, name="Series D")
        await _seed_imported_series(
            db_session,
            job2.id,
            name="Series E",
            status=ImportSeriesStatus.RECOVERY_PENDING,
        )

        items, total = await svc.get_orphaned_series(db_session)
        assert total == 5
        assert len(items) == 5
        assert {item.status for item in items} == {
            ImportSeriesStatus.NO_MATCH,
            ImportSeriesStatus.RECOVERY_PENDING,
        }

    async def test_f1_count_matches_list(self, db_session) -> None:
        """get_orphaned_count returns same total as get_orphaned_series."""
        svc = _make_import_service()

        job = await _seed_completed_job(db_session)
        await _seed_imported_series(db_session, job.id, name="Orphan 1")
        await _seed_imported_series(db_session, job.id, name="Orphan 2")
        await _seed_imported_series(db_session, job.id, name="Orphan 3")

        count = await svc.get_orphaned_count(db_session)
        assert count == 3

    async def test_f1_excludes_non_completed_jobs(self, db_session) -> None:
        """Orphaned query excludes NO_MATCH from CANCELLED/FAILED jobs."""
        svc = _make_import_service()

        completed_job = await _seed_completed_job(db_session)
        cancelled_job = await _seed_completed_job(
            db_session, source_path="/c", status=ImportJobStatus.CANCELLED
        )
        failed_job = await _seed_completed_job(
            db_session, source_path="/f", status=ImportJobStatus.FAILED
        )

        await _seed_imported_series(db_session, completed_job.id, name="Real Orphan")
        await _seed_imported_series(db_session, cancelled_job.id, name="Cancelled Orphan")
        await _seed_imported_series(db_session, failed_job.id, name="Failed Orphan")

        items, total = await svc.get_orphaned_series(db_session)
        assert total == 1
        assert items[0].raw_series_name == "Real Orphan"

    async def test_f1_excludes_skipped_rows(self, db_session) -> None:
        """Orphaned query excludes SKIPPED (dismissed) rows."""
        svc = _make_import_service()

        job = await _seed_completed_job(db_session)
        await _seed_imported_series(db_session, job.id, name="Active Orphan")
        await _seed_imported_series(
            db_session, job.id, name="Dismissed", status=ImportSeriesStatus.SKIPPED
        )

        items, total = await svc.get_orphaned_series(db_session)
        assert total == 1
        assert items[0].raw_series_name == "Active Orphan"

    async def test_f1_pagination(self, db_session) -> None:
        """Orphaned series supports pagination."""
        svc = _make_import_service()

        job = await _seed_completed_job(db_session)
        for i in range(5):
            await _seed_imported_series(db_session, job.id, name=f"Orphan {i}")

        items_p1, total = await svc.get_orphaned_series(db_session, page=1, page_size=2)
        assert total == 5
        assert len(items_p1) == 2

        items_p2, _ = await svc.get_orphaned_series(db_session, page=2, page_size=2)
        assert len(items_p2) == 2

        items_p3, _ = await svc.get_orphaned_series(db_session, page=3, page_size=2)
        assert len(items_p3) == 1

    async def test_f2_assign_cv_to_orphan_success(self, db_session) -> None:
        """F2: assign_cv_to_orphan stores CV metadata and keeps the row active."""
        mock_metadata = AsyncMock()
        mock_metadata.get_series_metadata.return_value = _cv_series_metadata(
            provider_id="12345",
            title="Batman (2016)",
            year=2016,
            publisher="DC Comics",
            issue_count=85,
        )
        svc = _make_import_service(metadata_service=mock_metadata)

        job = await _seed_completed_job(db_session)
        orphan = await _seed_imported_series(db_session, job.id, name="Batman")

        result = await svc.assign_cv_to_orphan(db_session, orphan.id, cv_id=12345)
        assert result.id == orphan.id

        await db_session.refresh(orphan)
        assert orphan.status == ImportSeriesStatus.RECOVERY_PENDING
        assert orphan.series_id is None
        assert orphan.cv_id == 12345
        assert orphan.cv_title == "Batman (2016)"
        series_count = (await db_session.execute(sa_select(Series))).scalars().all()
        assert len(series_count) == 0

    async def test_f3_assigned_orphan_disappears_from_list(self, db_session) -> None:
        """F3: After assigning, the row stays in the active unmatched queue."""
        mock_metadata = AsyncMock()
        mock_metadata.get_series_metadata.return_value = _cv_series_metadata(
            provider_id="12345",
            title="Batman (2016)",
            year=2016,
            publisher="DC Comics",
            issue_count=85,
        )
        svc = _make_import_service(metadata_service=mock_metadata)

        job = await _seed_completed_job(db_session)
        orphan1 = await _seed_imported_series(db_session, job.id, name="Batman")
        await _seed_imported_series(db_session, job.id, name="Superman")
        await _seed_imported_series(db_session, job.id, name="Flash")

        # Assign one
        await svc.assign_cv_to_orphan(db_session, orphan1.id, cv_id=12345)

        items, total = await svc.get_orphaned_series(db_session)
        assert total == 3
        identified = next(item for item in items if item.id == orphan1.id)
        assert identified.status == ImportSeriesStatus.RECOVERY_PENDING

    async def test_f4_assign_cv_failure_keeps_no_match(self, db_session) -> None:
        """F4: On provider error, row stays NO_MATCH."""
        mock_metadata = AsyncMock()
        mock_metadata.get_series_metadata.side_effect = ProviderError(
            "comicvine", "CV ID not found"
        )
        svc = _make_import_service(metadata_service=mock_metadata)

        job = await _seed_completed_job(db_session)
        orphan = await _seed_imported_series(db_session, job.id, name="Unknown")

        with pytest.raises(ProviderError):
            await svc.assign_cv_to_orphan(db_session, orphan.id, cv_id=99999)

        await db_session.refresh(orphan)
        assert orphan.status == ImportSeriesStatus.NO_MATCH

    async def test_f5_recover_orphan_assigns_files_only_on_submit(self, db_session) -> None:
        """F5: recover_orphan creates the series only on final submit and resolves files."""
        root = LibraryRoot(name="Main", path="/library/main", enabled=True)
        publisher = Publisher(name="DC Comics")
        real_series = Series(
            title="Batman (2016)",
            sort_title="Batman",
            year_start=2016,
            comicvine_id=12345,
            status=SeriesStatus.CONTINUING,
            issue_count=2,
            library_root_id=None,
        )
        db_session.add_all([root, publisher, real_series])
        await db_session.flush()
        issue = Issue(
            series_id=real_series.id,
            comicvine_id=501,
            issue_number=1.0,
            title="I Am Gotham",
            status=IssueStatus.WANTED,
        )
        db_session.add(issue)
        await db_session.flush()

        mock_series_svc = AsyncMock()
        mock_series_svc.add_from_comicvine.return_value = real_series
        svc = _make_import_service(series_service=mock_series_svc)
        job = await _seed_completed_job(db_session)
        job.target_library_root_id = root.id
        orphan = await _seed_imported_series(
            db_session,
            job.id,
            name="Batman",
            status=ImportSeriesStatus.RECOVERY_PENDING,
            cv_id=12345,
        )
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=orphan.id,
            file_path="/tmp/batman-001.cbz",
            file_name="Batman 001.cbz",
            file_size=1024,
            file_format="cbz",
            parsed_issue_number=1.0,
            status=ImportedFileStatus.PENDING,
        )
        db_session.add(imp_file)
        await db_session.flush()

        async def _fake_process_series_files(
            _session, _job, _item, *, report_file_progress=None
        ) -> None:
            imp_file.status = ImportedFileStatus.IMPORTED

        svc._process_series_files = AsyncMock(side_effect=_fake_process_series_files)
        svc._recompute_file_counters = AsyncMock()
        svc._recompute_series_counters = AsyncMock()

        result = await svc.recover_orphan(
            db_session,
            orphan.id,
            RecoverOrphanRequest(
                decisions=[
                    {
                        "imported_file_id": imp_file.id,
                        "action": "assign",
                        "issue_cv_id": 501,
                    }
                ]
            ),
        )

        await db_session.flush()
        await db_session.refresh(orphan)
        await db_session.refresh(imp_file)
        mock_series_svc.add_from_comicvine.assert_awaited_once()
        assert orphan.series_id == real_series.id
        assert orphan.status == ImportSeriesStatus.IMPORTED
        assert imp_file.match_method == "orphan_recovery"
        assert imp_file.matched_issue_id == issue.id
        assert result["status"] == ImportSeriesStatus.IMPORTED
        assert result["files_remaining"] == 0

    async def test_f4_assign_nonexistent_orphan_404(self, db_session) -> None:
        """assign_cv_to_orphan raises NotFoundError for missing ID."""
        svc = _make_import_service()

        with pytest.raises(NotFoundError):
            await svc.assign_cv_to_orphan(db_session, 99999, cv_id=12345)

    async def test_g1_dismiss_orphan(self, db_session) -> None:
        """G1: dismiss_orphan sets status to SKIPPED."""
        svc = _make_import_service()

        job = await _seed_completed_job(db_session)
        orphan = await _seed_imported_series(db_session, job.id, name="Meh Series")

        await svc.dismiss_orphan(db_session, orphan.id)

        await db_session.refresh(orphan)
        assert orphan.status == ImportSeriesStatus.SKIPPED

    async def test_g2_dismissed_excluded_from_orphans(self, db_session) -> None:
        """G2: Dismissed row does not appear in orphaned query."""
        svc = _make_import_service()

        job = await _seed_completed_job(db_session)
        orphan = await _seed_imported_series(db_session, job.id, name="Meh Series")
        await _seed_imported_series(db_session, job.id, name="Good Orphan")

        await svc.dismiss_orphan(db_session, orphan.id)

        items, total = await svc.get_orphaned_series(db_session)
        assert total == 1
        names = [i.raw_series_name for i in items]
        assert "Meh Series" not in names

    async def test_g3_dismiss_nonexistent_404(self, db_session) -> None:
        """G3: dismiss_orphan raises NotFoundError for missing ID."""
        svc = _make_import_service()

        with pytest.raises(NotFoundError):
            await svc.dismiss_orphan(db_session, 99999)

    async def test_sort_file_count_desc(self, db_session) -> None:
        """Default sort: file_count descending."""
        svc = _make_import_service()

        job = await _seed_completed_job(db_session)
        await _seed_imported_series(db_session, job.id, name="Small", file_count=2)
        await _seed_imported_series(db_session, job.id, name="Big", file_count=50)
        await _seed_imported_series(db_session, job.id, name="Medium", file_count=10)

        items, _ = await svc.get_orphaned_series(db_session, sort="file_count_desc")
        assert items[0].raw_series_name == "Big"
        assert items[1].raw_series_name == "Medium"
        assert items[2].raw_series_name == "Small"

    async def test_sort_series_name_asc(self, db_session) -> None:
        """Sort by series_name_asc."""
        svc = _make_import_service()

        job = await _seed_completed_job(db_session)
        await _seed_imported_series(db_session, job.id, name="Zorro")
        await _seed_imported_series(db_session, job.id, name="Batman")
        await _seed_imported_series(db_session, job.id, name="Aquaman")

        items, _ = await svc.get_orphaned_series(db_session, sort="series_name_asc")
        assert items[0].raw_series_name == "Aquaman"
        assert items[1].raw_series_name == "Batman"
        assert items[2].raw_series_name == "Zorro"

    async def test_sort_date_found_desc(self, db_session) -> None:
        """Sort by date_found_desc (created_at descending)."""
        svc = _make_import_service()

        job = await _seed_completed_job(db_session)
        await _seed_imported_series(db_session, job.id, name="First")
        await _seed_imported_series(db_session, job.id, name="Second")
        await _seed_imported_series(db_session, job.id, name="Third")

        items, total = await svc.get_orphaned_series(db_session, sort="date_found_desc")
        assert total == 3
        assert len(items) == 3


# ── Scenario H & H-EDGE: Retry Failed ────────────────────────────────


class TestRetryFailed:
    """Tests for retry_failed_series on ImportService."""

    async def test_h1_retry_resets_failed_to_confirmed(self, db_session) -> None:
        """H1: retry_failed_series resets FAILED rows to CONFIRMED."""
        svc = _make_import_service()

        job = await _seed_completed_job(db_session, series_imported=3, series_failed=2)
        # 3 imported rows
        for i in range(3):
            await _seed_imported_series(
                db_session,
                job.id,
                name=f"Good {i}",
                status=ImportSeriesStatus.IMPORTED,
                cv_id=1000 + i,
            )
        # 2 failed rows
        fail1 = await _seed_imported_series(
            db_session,
            job.id,
            name="Fail A",
            status=ImportSeriesStatus.FAILED,
            cv_id=2001,
        )
        fail2 = await _seed_imported_series(
            db_session,
            job.id,
            name="Fail B",
            status=ImportSeriesStatus.FAILED,
            cv_id=2002,
        )
        failed_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=fail1.id,
            file_path="/tmp/fail-a-001.cbz",
            file_name="Fail A 001.cbz",
            file_size=1024,
            file_format="cbz",
            status=ImportedFileStatus.FAILED,
            parsed_issue_number=1.0,
            match_confidence="high",
            match_method="issue_number",
            error_message="retry me",
        )
        db_session.add(failed_file)
        await db_session.flush()

        updated_job, count = await svc.retry_failed_series(db_session, job.id)
        assert count == 2
        assert updated_job.status == ImportJobStatus.IMPORTING

        # Verify FAILED rows are now CONFIRMED
        await db_session.refresh(fail1)
        await db_session.refresh(fail2)
        await db_session.refresh(failed_file)
        assert fail1.status == ImportSeriesStatus.CONFIRMED
        assert fail2.status == ImportSeriesStatus.CONFIRMED
        assert failed_file.status == ImportedFileStatus.CONFIRMED
        assert failed_file.error_message is None

    async def test_h1_series_failed_counter_decremented(self, db_session) -> None:
        """H1: series_failed counter is decremented by retry count."""
        svc = _make_import_service()

        job = await _seed_completed_job(db_session, series_imported=3, series_failed=2)
        for i in range(3):
            await _seed_imported_series(
                db_session,
                job.id,
                name=f"Good {i}",
                status=ImportSeriesStatus.IMPORTED,
                cv_id=1000 + i,
            )
        for i in range(2):
            await _seed_imported_series(
                db_session,
                job.id,
                name=f"Fail {i}",
                status=ImportSeriesStatus.FAILED,
                cv_id=2000 + i,
            )

        updated_job, _ = await svc.retry_failed_series(db_session, job.id)
        assert updated_job.series_failed == 0

    async def test_h5_imported_rows_untouched(self, db_session) -> None:
        """H5: Original IMPORTED rows are not modified by retry."""
        svc = _make_import_service()

        job = await _seed_completed_job(db_session, series_imported=3, series_failed=1)
        imported_items = []
        for i in range(3):
            item = await _seed_imported_series(
                db_session,
                job.id,
                name=f"Good {i}",
                status=ImportSeriesStatus.IMPORTED,
                cv_id=1000 + i,
            )
            imported_items.append(item)
        await _seed_imported_series(
            db_session,
            job.id,
            name="Fail",
            status=ImportSeriesStatus.FAILED,
            cv_id=2000,
        )

        await svc.retry_failed_series(db_session, job.id)

        for item in imported_items:
            await db_session.refresh(item)
            assert item.status == ImportSeriesStatus.IMPORTED

    async def test_he1_retry_non_completed_job_409(self, db_session) -> None:
        """HE1: retry_failed on REVIEW job raises ValidationError."""
        svc = _make_import_service()

        job = await _seed_completed_job(db_session, status=ImportJobStatus.REVIEW)

        with pytest.raises(ValidationError, match="COMPLETED"):
            await svc.retry_failed_series(db_session, job.id)

    async def test_he2_retry_no_failures_409(self, db_session) -> None:
        """HE2: retry_failed with 0 FAILED rows raises ValidationError."""
        svc = _make_import_service()

        job = await _seed_completed_job(db_session, series_imported=5, series_failed=0)
        for i in range(5):
            await _seed_imported_series(
                db_session,
                job.id,
                name=f"Good {i}",
                status=ImportSeriesStatus.IMPORTED,
                cv_id=1000 + i,
            )

        with pytest.raises(ValidationError, match="No failed"):
            await svc.retry_failed_series(db_session, job.id)

    async def test_he3_retry_nonexistent_job_404(self, db_session) -> None:
        """HE3: retry_failed on nonexistent job raises NotFoundError."""
        svc = _make_import_service()

        with pytest.raises(NotFoundError):
            await svc.retry_failed_series(db_session, 99999)
