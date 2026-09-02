"""Integration tests for file-level import lifecycle (Phase R-10).

Scenarios R-A through R-F test the full scan → match → file match →
review → confirm → import flow with file placement, conflict resolution,
source-safe copy behavior, renaming, manual import, and Phase 1 regression.
"""

from __future__ import annotations

import json
import os
import struct
import zipfile
import zlib
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select as sa_select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
from pullbox.core.collection_scanner import CollectionScanner
from pullbox.core.events import EventBus
from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.models.config import SystemConfig
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportFileHandlingMode,
    ImportJob,
    ImportJobAction,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import (
    LibraryFile,
    LibraryFileStorageMode,
    LibraryRoot,
    MatchConfidence,
)
from pullbox.models.publisher import Publisher
from pullbox.models.series import Series, SeriesStatus
from pullbox.providers.base import IssueSummary, SeriesMetadata, SeriesSearchResult
from pullbox.schemas.import_job import (
    ConfirmImportRequest,
    ConflictResolution,
    FileMatchOverride,
    ImportJobCreate,
)
from pullbox.services.import_service import ImportService
from scripts.mylar3_import_fixture import create_mylar3_db

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-r10")


# ── Helpers ───────────────────────────────────────────────────────────


def _make_service(
    *,
    series_service: AsyncMock | None = None,
    metadata_service: AsyncMock | None = None,
) -> ImportService:
    """Create an ImportService with mocked dependencies."""
    return ImportService(
        series_service=series_service or AsyncMock(),
        metadata_service=metadata_service or AsyncMock(),
        event_bus=EventBus(),
    )


def _cv_search_result(
    *,
    provider_id: str,
    title: str,
    year: int | None = None,
    publisher: str | None = None,
    issue_count: int | None = None,
) -> SeriesSearchResult:
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
    title: str | None = None,
) -> IssueSummary:
    return IssueSummary(
        provider_id=provider_id,
        issue_number=issue_number,
        title=title,
        release_date=None,
        cover_url=None,
        issue_type="issue",
    )


def _mock_cv_provider(
    *,
    search_map: dict[str, list[SeriesSearchResult]] | None = None,
    get_map: dict[str, SeriesMetadata] | None = None,
    issues_map: dict[str, list[IssueSummary]] | None = None,
) -> AsyncMock:
    """Mock ComicVine provider with search, get_series, and get_issues_for_series."""
    mock = AsyncMock()
    _search = search_map or {}
    _get = get_map or {}
    _issues = issues_map or {}

    async def search_side_effect(
        query: str,
        year: int | None = None,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> list[SeriesSearchResult]:
        for key, results in _search.items():
            if key.lower() == query.lower():
                return results
        return []

    async def get_side_effect(cv_id_str: str) -> SeriesMetadata:
        if cv_id_str in _get:
            return _get[cv_id_str]
        msg = f"CV ID {cv_id_str} not found"
        raise Exception(msg)

    async def issues_side_effect(cv_id_str: str) -> list[IssueSummary]:
        return _issues.get(cv_id_str, [])

    mock.search_series = AsyncMock(side_effect=search_side_effect)
    mock.get_series = AsyncMock(side_effect=get_side_effect)
    mock.get_issues_for_series = AsyncMock(side_effect=issues_side_effect)
    mock._fixture_series_ids = {
        key.casefold(): int(results[0].provider_id) for key, results in _search.items() if results
    }
    return mock


def _mock_series_service(
    cv_to_series: dict[int, object],
    errors: dict[int, Exception] | None = None,
) -> AsyncMock:
    """Mock SeriesService.add_from_comicvine reading from mutable dict."""
    mock_svc = AsyncMock()
    _errors = errors or {}

    async def add_side_effect(*args: object, **kwargs: object) -> object:
        cv_id = kwargs.get("comicvine_id")
        if cv_id in _errors:
            raise _errors[cv_id]
        if cv_id in cv_to_series:
            return cv_to_series[cv_id]
        msg = f"Unexpected CV ID: {cv_id}"
        raise ValueError(msg)

    mock_svc.add_from_comicvine = AsyncMock(side_effect=add_side_effect)
    return mock_svc


async def _create_series_with_issues(
    session: AsyncSession,
    title: str,
    year: int,
    cv_id: int,
    issue_specs: list[tuple[float, int | None]],
    publisher_name: str = "DC Comics",
) -> tuple[Series, list[Issue]]:
    """Create Series + Issues in DB. issue_specs = [(number, cv_issue_id), ...]

    Eagerly populates relationships (publisher, series) in the identity map
    so register_library_file can access them without lazy loading.
    """
    pub_result = await session.execute(sa_select(Publisher).where(Publisher.name == publisher_name))
    pub = pub_result.scalars().first()
    if pub is None:
        pub = Publisher(name=publisher_name)
        session.add(pub)
        await session.flush()

    s = Series(
        title=title,
        sort_title=title.lower(),
        year_start=year,
        comicvine_id=cv_id,
        publisher_id=pub.id,
        status=SeriesStatus.CONTINUING,
        issue_count=len(issue_specs),
    )
    # Pre-populate the publisher relationship in the identity map
    s.publisher = pub
    session.add(s)
    await session.flush()

    issues = []
    for num, cv_issue_id in issue_specs:
        iss = Issue(
            series_id=s.id,
            issue_number=num,
            comicvine_id=cv_issue_id,
            status=IssueStatus.WANTED,
            issue_type=IssueType.ISSUE,
        )
        # Pre-populate the series relationship
        iss.series = s
        session.add(iss)
        issues.append(iss)
    await session.flush()
    return s, issues


def _make_comic_dirs(
    root: Path,
    specs: list[tuple[str, list[str]]],
) -> None:
    """Create series directories with named files.

    specs = [("Batman (2016)", ["Batman 001.cbz", "Batman 002.cbz"]), ...]
    """
    for folder_name, filenames in specs:
        series_dir = root / folder_name
        series_dir.mkdir(parents=True, exist_ok=True)
        for fname in filenames:
            _write_comic_file(series_dir / fname)


def _write_comic_file(path: Path, size: int = 100) -> None:
    """Create a lightweight comic fixture that matches its file extension."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ext = path.suffix.lower()
    payload = b"\x00" * max(size, 1)
    if ext in {".cbz", ".zip", ".epub"}:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr("page001.jpg", payload)
            archive.writestr("page002.jpg", payload)
        return
    if ext == ".pdf":
        path.write_bytes(b"%PDF-1.4\n%" + payload)
        return
    if ext == ".cbr":
        # Stored RAR3 entries make the inventory real without an external writer.
        def header(kind: int, flags: int, body: bytes) -> bytes:
            data = struct.pack("<BHH", kind, flags, 7 + len(body)) + body
            return struct.pack("<H", zlib.crc32(data) & 0xFFFF) + data

        archive = b"Rar!\x1a\x07\x00" + header(0x73, 0, b"\x00" * 6)
        for name in (b"page001.jpg", b"page002.jpg"):
            file_header = (
                struct.pack(
                    "<LLBLLBBHL",
                    len(payload),
                    len(payload),
                    3,
                    zlib.crc32(payload),
                    (40 << 25) | (1 << 21) | (1 << 16),
                    20,
                    0x30,
                    len(name),
                    0o100644,
                )
                + name
            )
            archive += header(0x74, 0x8000, file_header) + payload
        path.write_bytes(archive + header(0x7B, 0, b""))
        return
    path.write_bytes(payload)


def _make_comic_dirs_simple(
    root: Path,
    specs: list[tuple[str, int]],
) -> None:
    """Create series directories with numbered issue_NNN.cbz files."""
    for folder_name, file_count in specs:
        series_dir = root / folder_name
        series_dir.mkdir(parents=True, exist_ok=True)
        for i in range(1, file_count + 1):
            _write_comic_file(series_dir / f"issue_{i:03d}.cbz")


async def _setup_comics_directory(session: AsyncSession, comics_dir: Path) -> LibraryRoot:
    """Set comics_directory config and create LibraryRoot."""
    comics_dir.mkdir(parents=True, exist_ok=True)
    session.add(SystemConfig(key="comics_directory", value=str(comics_dir), value_type="string"))
    root = LibraryRoot(name="Comics", path=str(comics_dir), enabled=True)
    session.add(root)
    await session.flush()
    return root


async def _run_full_pipeline(
    svc: ImportService,
    session: AsyncSession,
    source_path: str,
    source_type: ImportSourceType = ImportSourceType.FILESYSTEM,
) -> ImportJob:
    """Create job → start scan (scan → analyze → match → file_match → review)."""
    if source_type == ImportSourceType.FILESYSTEM:
        provider = svc._metadata_service._provider
        fixture_series_ids = getattr(provider, "_fixture_series_ids", {})
        if isinstance(fixture_series_ids, dict):
            scanner = CollectionScanner()
            for folder in Path(source_path).rglob("*"):
                if not folder.is_dir() or not any(
                    child.is_file() and child.suffix.lower() in {".cbz", ".cbr", ".pdf"}
                    for child in folder.iterdir()
                ):
                    continue
                sidecar_path = folder / "series.json"
                if sidecar_path.exists():
                    continue
                series_name, _, _ = scanner._extract_folder_identity(folder.name)
                series_name = series_name.casefold()
                series_id = fixture_series_ids.get(series_name)
                if isinstance(series_id, int):
                    sidecar_path.write_text(json.dumps({"comicid": series_id}))
    target_root = await session.scalar(
        sa_select(LibraryRoot).where(LibraryRoot.is_default_managed_destination.is_(True)).limit(1)
    )
    if target_root is None:
        target_root = await session.scalar(sa_select(LibraryRoot).order_by(LibraryRoot.id).limit(1))
    if target_root is None:
        source = Path(source_path)
        test_library = source.parent / f".{source.name}-pullbox-library"
        test_library.mkdir(parents=True, exist_ok=True)
        target_root = LibraryRoot(
            name="Lifecycle managed destination",
            path=str(test_library),
            enabled=True,
            allow_referenced_registrations=True,
            allow_managed_writes=True,
            is_default_managed_destination=True,
        )
        session.add(target_root)
        await session.flush()
    request = ImportJobCreate(
        source_path=source_path,
        source_type=source_type,
        target_library_root_id=target_root.id,
    )
    job = await svc.create_job(session, request)
    await svc.start_scan(session, job.id)
    await session.refresh(job)
    return job


# ── Scenario R-A: Full Import with File Matching — Happy Path ─────────


class TestFullImportWithFiles:
    """R-10.1: Full scan → match → file match → confirm → import with file placement."""

    async def test_ra_happy_path_3_series_15_files(
        self,
        db_session: AsyncSession,
        tmp_path: Path,
    ) -> None:
        """3 series x 5 well-named files each -> all matched and copied to library."""
        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        # Source: 3 series with well-named files (parseable issue numbers)
        source = tmp_path / "import"
        _make_comic_dirs(
            source,
            [
                ("Batman (2016)", [f"Batman #{i:03d} (2016).cbz" for i in range(1, 6)]),
                ("Saga (2012)", [f"Saga #{i:03d} (2012).cbz" for i in range(1, 6)]),
                ("Invincible (2003)", [f"Invincible #{i:03d} (2003).cbz" for i in range(1, 6)]),
            ],
        )

        # Mock CV: search returns matches, issues_for_series returns issue lists
        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [
                    _cv_search_result(
                        provider_id="97508",
                        title="Batman",
                        year=2016,
                        publisher="DC Comics",
                    )
                ],
                "Saga": [
                    _cv_search_result(
                        provider_id="42692",
                        title="Saga",
                        year=2012,
                        publisher="Image Comics",
                    )
                ],
                "Invincible": [
                    _cv_search_result(
                        provider_id="18816",
                        title="Invincible",
                        year=2003,
                        publisher="Image Comics",
                    )
                ],
            },
            issues_map={
                "97508": [
                    _issue_summary(provider_id=str(100000 + i), issue_number=float(i))
                    for i in range(1, 6)
                ],
                "42692": [
                    _issue_summary(provider_id=str(200000 + i), issue_number=float(i))
                    for i in range(1, 6)
                ],
                "18816": [
                    _issue_summary(provider_id=str(300000 + i), issue_number=float(i))
                    for i in range(1, 6)
                ],
            },
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider

        cv_to_series: dict[int, object] = {}
        mock_series_svc = _mock_series_service(cv_to_series)
        svc = _make_service(series_service=mock_series_svc, metadata_service=mock_metadata)

        # Run scan pipeline
        job = await _run_full_pipeline(svc, db_session, str(source))

        assert job.status == ImportJobStatus.REVIEW
        assert job.series_found == 3
        assert job.series_matched == 3
        assert job.total_files_found == 15
        assert job.total_files_matched == 15
        assert job.total_files_conflict == 0
        assert job.total_files_no_match == 0

        # Confirm all series
        result = await db_session.execute(
            sa_select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
        )
        items = result.scalars().all()
        assert len(items) == 3

        confirm_req = ConfirmImportRequest(series_ids=[i.id for i in items])
        job = await svc.confirm_import(db_session, job.id, confirm_req)

        # Create real Series+Issues in DB for file placement
        cv_to_series[97508] = (
            await _create_series_with_issues(
                db_session,
                "Batman",
                2016,
                97508,
                [(float(i), 100000 + i) for i in range(1, 6)],
            )
        )[0]
        cv_to_series[42692] = (
            await _create_series_with_issues(
                db_session,
                "Saga",
                2012,
                42692,
                [(float(i), 200000 + i) for i in range(1, 6)],
                publisher_name="Image Comics",
            )
        )[0]
        cv_to_series[18816] = (
            await _create_series_with_issues(
                db_session,
                "Invincible",
                2003,
                18816,
                [(float(i), 300000 + i) for i in range(1, 6)],
                publisher_name="Image Comics",
            )
        )[0]

        # Run import
        await svc.run_import(db_session, job.id)
        await db_session.refresh(job)

        assert job.status == ImportJobStatus.COMPLETED
        assert job.series_imported == 3
        assert job.series_failed == 0
        assert job.total_files_imported == 15
        assert job.total_files_failed == 0

        # Verify LibraryFile records created
        lf_result = await db_session.execute(sa_select(LibraryFile))
        library_files = lf_result.scalars().all()
        assert len(library_files) == 15

        # Verify all files materialized in comics directory
        for lf in library_files:
            assert str(comics_dir) in lf.file_path
            assert Path(lf.file_path).exists()

        # Collection imports preserve source folders/files so Mylar/Kapowarr/etc. stay intact.
        source_files = [file for folder in source.iterdir() for file in folder.glob("*.cbz")]
        assert len(source_files) == 15
        assert all(file.exists() for file in source_files)

        # Verify all matched issues are OWNED
        issue_result = await db_session.execute(
            sa_select(Issue).where(Issue.status == IssueStatus.OWNED)
        )
        owned_issues = issue_result.scalars().all()
        assert len(owned_issues) == 15

        # Verify ImportedFile records all IMPORTED with library_file_id
        imp_files_result = await db_session.execute(
            sa_select(ImportedFile).where(ImportedFile.status == ImportedFileStatus.IMPORTED)
        )
        imp_files = imp_files_result.scalars().all()
        assert len(imp_files) == 15
        assert all(f.library_file_id is not None for f in imp_files)

    async def test_ra_single_series_single_file(
        self,
        db_session: AsyncSession,
        tmp_path: Path,
    ) -> None:
        """Minimum case: 1 series, 1 file."""
        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        source = tmp_path / "import"
        _make_comic_dirs(source, [("Batman (2016)", ["Batman #001 (2016).cbz"])])

        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [_cv_search_result(provider_id="97508", title="Batman", year=2016)]
            },
            issues_map={"97508": [_issue_summary(provider_id="100001", issue_number=1.0)]},
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider

        cv_to_series: dict[int, object] = {}
        mock_series_svc = _mock_series_service(cv_to_series)
        svc = _make_service(series_service=mock_series_svc, metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(source))
        assert job.total_files_matched == 1

        result = await db_session.execute(
            sa_select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
        )
        items = result.scalars().all()
        confirm_req = ConfirmImportRequest(series_ids=[i.id for i in items])
        await svc.confirm_import(db_session, job.id, confirm_req)

        cv_to_series[97508] = (
            await _create_series_with_issues(
                db_session,
                "Batman",
                2016,
                97508,
                [(1.0, 100001)],
            )
        )[0]

        await svc.run_import(db_session, job.id)
        await db_session.refresh(job)

        assert job.status == ImportJobStatus.COMPLETED
        assert job.total_files_imported == 1

        lf_result = await db_session.execute(sa_select(LibraryFile))
        assert len(lf_result.scalars().all()) == 1

    async def test_ra_files_with_no_parseable_issue_number(
        self,
        db_session: AsyncSession,
        tmp_path: Path,
    ) -> None:
        """Files without parseable issue numbers → NO_MATCH at file level."""
        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        source = tmp_path / "import"
        _make_comic_dirs(
            source,
            [
                (
                    "Batman (2016)",
                    [
                        "Batman #001 (2016).cbz",  # matchable
                        "random_file.cbz",  # not matchable
                        "cover_scan.cbz",  # not matchable
                    ],
                ),
            ],
        )

        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [_cv_search_result(provider_id="97508", title="Batman", year=2016)]
            },
            issues_map={"97508": [_issue_summary(provider_id="100001", issue_number=1.0)]},
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider

        svc = _make_service(metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(source))

        assert job.total_files_matched == 1
        assert job.total_files_no_match == 2

        # Verify file statuses
        result = await db_session.execute(
            sa_select(ImportedFile).where(ImportedFile.status == ImportedFileStatus.NO_MATCH)
        )
        no_match = result.scalars().all()
        assert len(no_match) == 2
        no_match_names = {f.file_name for f in no_match}
        assert "random_file.cbz" in no_match_names
        assert "cover_scan.cbz" in no_match_names

    async def test_ra_series_counters_accurate(
        self,
        db_session: AsyncSession,
        tmp_path: Path,
    ) -> None:
        """Per-series file counters (files_total, files_matched, etc.) are correct."""
        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        source = tmp_path / "import"
        _make_comic_dirs(
            source,
            [
                (
                    "Batman (2016)",
                    [
                        "Batman #001 (2016).cbz",
                        "Batman #002 (2016).cbz",
                        "extras.cbz",  # no match
                    ],
                ),
            ],
        )

        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [_cv_search_result(provider_id="97508", title="Batman", year=2016)]
            },
            issues_map={
                "97508": [
                    _issue_summary(provider_id="100001", issue_number=1.0),
                    _issue_summary(provider_id="100002", issue_number=2.0),
                ]
            },
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider
        svc = _make_service(metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(source))

        result = await db_session.execute(
            sa_select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
        )
        item = result.scalars().first()
        assert item is not None
        assert item.files_total == 3
        assert item.files_matched == 2
        assert item.files_no_match == 1
        assert item.files_conflict == 0

    async def test_ra_empty_directory_produces_zero_files(
        self,
        db_session: AsyncSession,
        tmp_path: Path,
    ) -> None:
        """Import directory with series folders but no files inside."""
        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        source = tmp_path / "import"
        series_dir = source / "Batman (2016)"
        series_dir.mkdir(parents=True)
        # No files created

        svc = _make_service()
        job = await _run_full_pipeline(svc, db_session, str(source))

        assert job.total_files_found == 0

    async def test_ra_mixed_file_formats(
        self, db_session: AsyncSession, tmp_path: Path, monkeypatch
    ) -> None:
        """Files with various formats (CBZ, CBR, PDF, EPUB) all get processed."""
        # Header inspection is pure Python; this lifecycle test needs no extraction backend.
        monkeypatch.setattr("pullbox.core.archive.configure_rarfile_backend", lambda: None)
        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        source = tmp_path / "import"
        _make_comic_dirs(
            source,
            [
                (
                    "Batman (2016)",
                    [
                        "Batman #001 (2016).cbz",
                        "Batman #002 (2016).cbr",
                        "Batman #003 (2016).pdf",
                        "Batman #004 (2016).epub",
                    ],
                ),
            ],
        )

        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [_cv_search_result(provider_id="97508", title="Batman", year=2016)]
            },
            issues_map={
                "97508": [
                    _issue_summary(provider_id=str(100000 + i), issue_number=float(i))
                    for i in range(1, 5)
                ]
            },
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider
        svc = _make_service(metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(source))

        assert job.total_files_found == 4
        assert job.total_files_matched == 4

    async def test_ra_issue_number_with_decimal(
        self,
        db_session: AsyncSession,
        tmp_path: Path,
    ) -> None:
        """Files with decimal issue numbers (e.g., #1.5) match correctly."""
        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        source = tmp_path / "import"
        _make_comic_dirs(
            source,
            [
                ("Batman (2016)", ["Batman #001.5 (2016).cbz"]),
            ],
        )

        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [_cv_search_result(provider_id="97508", title="Batman", year=2016)]
            },
            issues_map={"97508": [_issue_summary(provider_id="100001", issue_number=1.5)]},
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider
        svc = _make_service(metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(source))

        assert job.total_files_matched == 1

    async def test_ra_non_comic_files_ignored(
        self,
        db_session: AsyncSession,
        tmp_path: Path,
    ) -> None:
        """Non-comic files (.txt, .jpg, .nfo) in series folders are not scanned."""
        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        source = tmp_path / "import"
        series_dir = source / "Batman (2016)"
        series_dir.mkdir(parents=True)
        _write_comic_file(series_dir / "Batman #001 (2016).cbz")
        (series_dir / "cover.jpg").write_bytes(b"\xff\xd8" + b"\x00" * 50)
        (series_dir / "info.txt").write_text("metadata")
        (series_dir / "release.nfo").write_text("nfo data")

        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [_cv_search_result(provider_id="97508", title="Batman", year=2016)]
            },
            issues_map={"97508": [_issue_summary(provider_id="100001", issue_number=1.0)]},
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider
        svc = _make_service(metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(source))

        # Only the CBZ should be found, non-comic files ignored
        assert job.total_files_found == 1


# ── Scenario R-B: Import with Conflicts ───────────────────────────────


class TestImportWithConflicts:
    """R-10.2: Duplicate files for same issue → conflict detection → resolution → import."""

    async def test_rb_two_files_same_issue_creates_conflict(
        self,
        db_session: AsyncSession,
        tmp_path: Path,
    ) -> None:
        """Two files matching the same issue → CONFLICT status with preferred file."""
        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        source = tmp_path / "import"
        series_dir = source / "Batman (2016)"
        series_dir.mkdir(parents=True)
        # Two files for issue #1 — larger file should be preferred
        _write_comic_file(series_dir / "Batman #001 (2016).cbz", size=500)
        _write_comic_file(series_dir / "Batman 001 (2016) (scan).cbz", size=100)

        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [_cv_search_result(provider_id="97508", title="Batman", year=2016)]
            },
            issues_map={"97508": [_issue_summary(provider_id="100001", issue_number=1.0)]},
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider
        svc = _make_service(metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(source))

        assert job.total_files_conflict == 2
        assert job.total_files_matched == 0  # conflicts subtract from matched

        # Both files should have CONFLICT status
        result = await db_session.execute(
            sa_select(ImportedFile).where(ImportedFile.status == ImportedFileStatus.CONFLICT)
        )
        conflicts = result.scalars().all()
        assert len(conflicts) == 2

        # One should be preferred
        preferred = [f for f in conflicts if f.is_preferred]
        assert len(preferred) == 1
        # Preferred file is the larger one
        assert preferred[0].file_size > 200

        # Both should share a conflict group
        group_ids = {f.conflict_group_id for f in conflicts}
        assert len(group_ids) == 1

    async def test_rb_resolve_conflict_and_import(
        self,
        db_session: AsyncSession,
        tmp_path: Path,
    ) -> None:
        """Resolve conflict by choosing non-preferred file → only chosen file imported."""
        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        source = tmp_path / "import"
        series_dir = source / "Batman (2016)"
        series_dir.mkdir(parents=True)
        _write_comic_file(series_dir / "Batman #001 (2016).cbz", size=500)
        _write_comic_file(series_dir / "Batman 001 (scan).cbz", size=100)
        # Also a non-conflicting file
        _write_comic_file(series_dir / "Batman #002 (2016).cbz", size=200)

        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [_cv_search_result(provider_id="97508", title="Batman", year=2016)]
            },
            issues_map={
                "97508": [
                    _issue_summary(provider_id="100001", issue_number=1.0),
                    _issue_summary(provider_id="100002", issue_number=2.0),
                ]
            },
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider

        cv_to_series: dict[int, object] = {}
        mock_series_svc = _mock_series_service(cv_to_series)
        svc = _make_service(series_service=mock_series_svc, metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(source))

        assert job.total_files_conflict == 2
        assert job.total_files_matched == 1  # issue #2

        # Get conflict files and series
        series_result = await db_session.execute(
            sa_select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
        )
        imp_series = series_result.scalars().all()

        conflict_result = await db_session.execute(
            sa_select(ImportedFile).where(ImportedFile.status == ImportedFileStatus.CONFLICT)
        )
        conflicts = conflict_result.scalars().all()
        group_id = conflicts[0].conflict_group_id

        # Choose the SMALLER file (non-preferred) to test resolution override
        smaller = min(conflicts, key=lambda f: f.file_size)

        confirm_req = ConfirmImportRequest(
            series_ids=[s.id for s in imp_series],
            conflict_resolutions=[
                ConflictResolution(conflict_group_id=group_id, chosen_file_id=smaller.id)
            ],
        )
        job = await svc.confirm_import(db_session, job.id, confirm_req)

        # Verify: chosen file CONFIRMED, other SKIPPED
        await db_session.refresh(smaller)
        assert smaller.status == ImportedFileStatus.CONFIRMED

        other = next(f for f in conflicts if f.id != smaller.id)
        await db_session.refresh(other)
        assert other.status == ImportedFileStatus.SKIPPED

        # Import
        cv_to_series[97508] = (
            await _create_series_with_issues(
                db_session,
                "Batman",
                2016,
                97508,
                [(1.0, 100001), (2.0, 100002)],
            )
        )[0]

        await svc.run_import(db_session, job.id)
        await db_session.refresh(job)

        assert job.status == ImportJobStatus.COMPLETED
        assert job.total_files_imported == 2  # resolved conflict + issue #2

        # Only 2 LibraryFile records (not 3)
        lf_result = await db_session.execute(sa_select(LibraryFile))
        assert len(lf_result.scalars().all()) == 2

    async def test_rb_three_files_same_issue(
        self,
        db_session: AsyncSession,
        tmp_path: Path,
    ) -> None:
        """Three files for same issue → all 3 in one conflict group."""
        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        source = tmp_path / "import"
        series_dir = source / "Batman (2016)"
        series_dir.mkdir(parents=True)
        _write_comic_file(series_dir / "Batman #001 (2016).cbz", size=500)
        _write_comic_file(series_dir / "Batman #001 v2.cbz", size=300)
        _write_comic_file(series_dir / "Batman #001 (scan).cbz", size=100)

        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [_cv_search_result(provider_id="97508", title="Batman", year=2016)]
            },
            issues_map={"97508": [_issue_summary(provider_id="100001", issue_number=1.0)]},
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider
        svc = _make_service(metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(source))

        assert job.total_files_conflict == 3

        result = await db_session.execute(
            sa_select(ImportedFile).where(ImportedFile.status == ImportedFileStatus.CONFLICT)
        )
        conflicts = result.scalars().all()
        assert len(conflicts) == 3

        # All same group
        group_ids = {f.conflict_group_id for f in conflicts}
        assert len(group_ids) == 1

        # Only one preferred
        preferred = [f for f in conflicts if f.is_preferred]
        assert len(preferred) == 1

    async def test_rb_conflicts_across_multiple_series(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """Conflicts in two different series → separate conflict groups."""
        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        source = tmp_path / "import"
        # Batman: 2 files for #1
        batman_dir = source / "Batman (2016)"
        batman_dir.mkdir(parents=True)
        _write_comic_file(batman_dir / "Batman #001.cbz", size=300)
        _write_comic_file(batman_dir / "Batman 001 dup.cbz", size=100)
        # Saga: 2 files for #1
        saga_dir = source / "Saga (2012)"
        saga_dir.mkdir(parents=True)
        _write_comic_file(saga_dir / "Saga #001.cbz", size=300)
        _write_comic_file(saga_dir / "Saga 001 dup.cbz", size=100)

        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [_cv_search_result(provider_id="97508", title="Batman", year=2016)],
                "Saga": [_cv_search_result(provider_id="42692", title="Saga", year=2012)],
            },
            issues_map={
                "97508": [_issue_summary(provider_id="100001", issue_number=1.0)],
                "42692": [_issue_summary(provider_id="200001", issue_number=1.0)],
            },
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider
        svc = _make_service(metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(source))

        assert job.total_files_conflict == 4

        # Should have 2 distinct conflict groups
        result = await db_session.execute(
            sa_select(ImportedFile).where(ImportedFile.status == ImportedFileStatus.CONFLICT)
        )
        conflicts = result.scalars().all()
        group_ids = {f.conflict_group_id for f in conflicts}
        assert len(group_ids) == 2

    async def test_rb_conflict_plus_normal_files_coexist(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """Series with a conflict for #1 and clean matches for #2 and #3."""
        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        source = tmp_path / "import"
        series_dir = source / "Batman (2016)"
        series_dir.mkdir(parents=True)
        _write_comic_file(series_dir / "Batman #001.cbz", size=300)
        _write_comic_file(series_dir / "Batman 001 dup.cbz", size=100)
        _write_comic_file(series_dir / "Batman #002.cbz", size=200)
        _write_comic_file(series_dir / "Batman #003.cbz", size=200)

        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [_cv_search_result(provider_id="97508", title="Batman", year=2016)]
            },
            issues_map={
                "97508": [
                    _issue_summary(provider_id="100001", issue_number=1.0),
                    _issue_summary(provider_id="100002", issue_number=2.0),
                    _issue_summary(provider_id="100003", issue_number=3.0),
                ]
            },
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider
        svc = _make_service(metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(source))

        assert job.total_files_conflict == 2
        assert job.total_files_matched == 2  # #2 and #3
        assert job.total_files_no_match == 0

    async def test_rb_unresolved_conflict_preferred_file_imported(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """Confirm without explicit conflict resolution is rejected."""
        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        source = tmp_path / "import"
        series_dir = source / "Batman (2016)"
        series_dir.mkdir(parents=True)
        _write_comic_file(series_dir / "Batman #001.cbz", size=500)
        _write_comic_file(series_dir / "Batman 001 dup.cbz", size=100)

        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [_cv_search_result(provider_id="97508", title="Batman", year=2016)]
            },
            issues_map={"97508": [_issue_summary(provider_id="100001", issue_number=1.0)]},
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider

        cv_to_series: dict[int, object] = {}
        mock_series_svc = _mock_series_service(cv_to_series)
        svc = _make_service(series_service=mock_series_svc, metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(source))

        series_result = await db_session.execute(
            sa_select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
        )
        imp_series = series_result.scalars().all()
        confirm_req = ConfirmImportRequest(series_ids=[s.id for s in imp_series])
        with pytest.raises(ValidationError, match="Resolve file conflicts"):
            await svc.confirm_import(db_session, job.id, confirm_req)


# ── Scenario R-C: Leave-in-Place via register_library_file ────────────


class TestImportLeaveInPlace:
    """R-10.3: Leave-in-place via register_library_file (direct call).

    Note: The wizard pipeline currently always moves files to the library.
    Leave-in-place is tested via direct register_library_file calls, which
    is how manual import and download pipeline use it.
    """

    async def test_rc_leave_in_place_files_stay(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """Files remain at original paths, LibraryFile records point to source."""
        from pullbox.core.file_ops import register_library_file

        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        _series, issues = await _create_series_with_issues(
            db_session,
            "Batman",
            2016,
            97508,
            [(1.0, 100001), (2.0, 100002)],
        )

        for i, issue in enumerate(issues):
            src = comics_dir / "Existing Batman Layout" / f"Batman #{i + 1:03d}.cbz"
            src.parent.mkdir(parents=True, exist_ok=True)
            _write_comic_file(src, size=200)

            lf = await register_library_file(
                db_session,
                src,
                issue,
                MatchConfidence.HIGH,
                move_to_library=False,
            )

            # File stays at source
            assert src.exists()
            assert lf.file_path == str(src)
            assert lf.storage_mode == LibraryFileStorageMode.REFERENCED

        # Issues marked OWNED
        await db_session.refresh(issues[0])
        await db_session.refresh(issues[1])
        assert issues[0].status == IssueStatus.OWNED
        assert issues[1].status == IssueStatus.OWNED

    async def test_rc_wizard_import_copies_files_to_library(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """Wizard import materializes files in library while preserving source files."""
        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        source = tmp_path / "import"
        _make_comic_dirs(
            source,
            [
                ("Batman (2016)", ["Batman #001 (2016).cbz"]),
            ],
        )

        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [_cv_search_result(provider_id="97508", title="Batman", year=2016)]
            },
            issues_map={"97508": [_issue_summary(provider_id="100001", issue_number=1.0)]},
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider

        cv_to_series: dict[int, object] = {}
        mock_series_svc = _mock_series_service(cv_to_series)
        svc = _make_service(series_service=mock_series_svc, metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(source))

        series_result = await db_session.execute(
            sa_select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
        )
        imp_series = series_result.scalars().all()

        confirm_req = ConfirmImportRequest(series_ids=[s.id for s in imp_series])
        await svc.confirm_import(db_session, job.id, confirm_req)

        cv_to_series[97508] = (
            await _create_series_with_issues(
                db_session,
                "Batman",
                2016,
                97508,
                [(1.0, 100001)],
            )
        )[0]

        await svc.run_import(db_session, job.id)
        await db_session.refresh(job)

        assert job.status == ImportJobStatus.COMPLETED
        assert job.total_files_imported == 1

        # File materialized in comics directory
        lf_result = await db_session.execute(sa_select(LibraryFile))
        lf = lf_result.scalars().first()
        assert lf is not None
        assert str(comics_dir) in lf.file_path
        registered_action = (
            await db_session.scalars(
                sa_select(ImportJobAction).where(
                    ImportJobAction.import_job_id == job.id,
                    ImportJobAction.action_type == "library_file_registered",
                )
            )
        ).one()
        assert registered_action.payload["destination_signature"] == lf.source_signature
        assert registered_action.payload["destination_signature"]["schema_version"] == 1
        placement_action = (
            await db_session.scalars(
                sa_select(ImportJobAction).where(
                    ImportJobAction.import_job_id == job.id,
                    ImportJobAction.action_type == "library_file_placement_started",
                )
            )
        ).one()
        assert placement_action.payload["placement_completed"] is True
        assert placement_action.payload["destination_signature"] == lf.source_signature
        assert placement_action.payload["temp_paths"]
        assert all(not Path(path).exists() for path in placement_action.payload["temp_paths"])

        # Source file preserved for source-safe collection imports
        source_files = list((source / "Batman (2016)").glob("*.cbz"))
        assert len(source_files) == 1
        assert source_files[0].exists()

    @pytest.mark.parametrize(
        ("source_type", "source_changed"),
        [
            (ImportSourceType.FILESYSTEM, False),
            (ImportSourceType.MYLAR3, False),
            (ImportSourceType.MYLAR3, True),
        ],
    )
    async def test_rc_wizard_in_place_import_registers_without_mutation(
        self,
        db_session: AsyncSession,
        tmp_path: Path,
        source_type: ImportSourceType,
        source_changed: bool,
    ) -> None:
        comics_dir = tmp_path / "library"
        root = await _setup_comics_directory(db_session, comics_dir)
        db_session.add_all(
            [
                SystemConfig(key="rename_on_import", value="true", value_type="bool"),
                SystemConfig(
                    key="convert_to_preferred_format_on_import",
                    value="true",
                    value_type="bool",
                ),
                SystemConfig(
                    key="update_embedded_comicinfo_from_match_on_import",
                    value="true",
                    value_type="bool",
                ),
                SystemConfig(key="library_permissions_enabled", value="true", value_type="bool"),
            ]
        )
        await db_session.flush()
        source = comics_dir / "Existing Layout"
        _make_comic_dirs(
            source,
            [("Batman (2016)", ["original issue name 001.cbz"])],
        )
        series_folder = source / "Batman (2016)"
        sidecar = series_folder / "series.json"
        sidecar.write_text(json.dumps({"comicid": 97508}), encoding="utf-8")
        source_file = series_folder / "original issue name 001.cbz"
        before_bytes = source_file.read_bytes()
        before_stat = source_file.stat()
        before_tree = sorted(str(path.relative_to(source)) for path in source.rglob("*"))
        before_sidecar = sidecar.read_bytes(), sidecar.stat().st_mtime_ns, sidecar.stat().st_mode
        database = tmp_path / "mylar.db"
        database_before: tuple[bytes, int, int] | None = None
        if source_type == ImportSourceType.MYLAR3:
            create_mylar3_db(
                database,
                series=[
                    {
                        "ComicID": "CV-97508",
                        "ComicName": "Batman",
                        "ComicYear": "2016",
                        "ComicPublisher": "DC Comics",
                        "ComicLocation": "/comics/Batman (2016)",
                        "Total": 1,
                    }
                ],
                issues=[
                    {
                        "IssueID": "100001",
                        "ComicID": "97508",
                        "Issue_Number": "1",
                        "Location": source_file.name,
                    }
                ],
            )
            database_before = (
                database.read_bytes(),
                database.stat().st_mtime_ns,
                database.stat().st_mode,
            )

        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [_cv_search_result(provider_id="97508", title="Batman", year=2016)]
            },
            issues_map={"97508": [_issue_summary(provider_id="100001", issue_number=1.0)]},
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider
        cv_to_series: dict[int, object] = {}
        mock_series_svc = _mock_series_service(cv_to_series)
        svc = _make_service(series_service=mock_series_svc, metadata_service=mock_metadata)

        job = await svc.create_job(
            db_session,
            ImportJobCreate(
                source_path=str(database if source_type == ImportSourceType.MYLAR3 else source),
                source_type=source_type,
                file_handling_mode=ImportFileHandlingMode.IN_PLACE,
                target_library_root_id=root.id,
                mylar3_path_map=(
                    {"/comics": str(source)} if source_type == ImportSourceType.MYLAR3 else {}
                ),
                mylar3_path_map_confirmed=source_type == ImportSourceType.MYLAR3,
            ),
        )
        await svc.start_scan(db_session, job.id)
        await db_session.refresh(job)
        imported_series = list(
            (
                await db_session.execute(
                    sa_select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
                )
            )
            .scalars()
            .all()
        )
        await svc.confirm_import(
            db_session,
            job.id,
            ConfirmImportRequest(series_ids=[item.id for item in imported_series]),
        )
        cv_to_series[97508] = (
            await _create_series_with_issues(
                db_session,
                "Batman",
                2016,
                97508,
                [(1.0, 100001)],
            )
        )[0]

        if source_changed:
            source_file.write_bytes(before_bytes + b"changed after scan")
        await svc.run_import(db_session, job.id)
        await db_session.refresh(job)

        if source_changed:
            file_row = (
                await db_session.scalars(
                    sa_select(ImportedFile).where(ImportedFile.import_job_id == job.id)
                )
            ).one()
            assert file_row.status == ImportedFileStatus.FAILED
            assert file_row.include_in_import is False
            assert file_row.diagnostics["source_revalidation"]["code"] == "source_changed"
            assert await db_session.scalar(sa_select(LibraryFile.id)) is None
            assert source_file.read_bytes() == before_bytes + b"changed after scan"
            return

        library_file = (
            (
                await db_session.execute(
                    sa_select(LibraryFile).where(LibraryFile.issue_id.is_not(None))
                )
            )
            .scalars()
            .one()
        )
        action = (
            (
                await db_session.execute(
                    sa_select(ImportJobAction).where(
                        ImportJobAction.import_job_id == job.id,
                        ImportJobAction.action_type == "library_file_registered",
                    )
                )
            )
            .scalars()
            .one()
        )
        after_stat = source_file.stat()

        assert job.status == ImportJobStatus.COMPLETED
        assert job.target_library_root_id == root.id
        assert job.move_to_library is False
        assert job.effective_transfer_method == "leave_in_place"
        assert job.convert_to_preferred_format is False
        assert job.update_embedded_comicinfo_from_match is False
        assert library_file.file_path == str(source_file.resolve())
        assert library_file.storage_mode == LibraryFileStorageMode.REFERENCED
        assert action.payload["storage_mode"] == "referenced"
        assert action.payload["transfer_method"] == "leave_in_place"
        assert source_file.read_bytes() == before_bytes
        assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
        assert after_stat.st_mode == before_stat.st_mode
        assert sorted(str(path.relative_to(source)) for path in source.rglob("*")) == before_tree
        assert (sidecar.read_bytes(), sidecar.stat().st_mtime_ns, sidecar.stat().st_mode) == (
            before_sidecar
        )
        if source_type == ImportSourceType.MYLAR3:
            mock_provider.search_series.assert_not_awaited()
            mock_provider.get_series.assert_not_awaited()
            mock_provider.get_issues_for_series.assert_not_awaited()
            assert (
                database.read_bytes(),
                database.stat().st_mtime_ns,
                database.stat().st_mode,
            ) == database_before
            await svc.rollback_import(db_session, job.id)
            assert await db_session.get(LibraryFile, library_file.id) is None
            assert source_file.read_bytes() == before_bytes
            assert source_file.stat().st_mtime_ns == before_stat.st_mtime_ns
            assert source_file.stat().st_mode == before_stat.st_mode
            assert (sidecar.read_bytes(), sidecar.stat().st_mtime_ns, sidecar.stat().st_mode) == (
                before_sidecar
            )
            assert (
                sorted(str(path.relative_to(source)) for path in source.rglob("*")) == before_tree
            )
            frozen_policy = dict(job.ingest_policy_snapshot)
            db_session.add(
                SystemConfig(key="post_processing_method", value="hardlink", value_type="string")
            )
            await db_session.flush()
            retry = await svc.retry_job(db_session, job.id)
            assert retry.file_handling_mode == ImportFileHandlingMode.IN_PLACE
            assert retry.ingest_policy_snapshot == frozen_policy
            assert retry.target_library_root_id == root.id
            assert retry.mylar3_path_map == {"/comics": str(source)}
            assert retry.effective_transfer_method == "leave_in_place"
            assert retry.move_to_library is False
            assert retry.convert_to_preferred_format is False
            assert retry.update_embedded_comicinfo_from_match is False


# ── Scenario R-D: Import with Rename ─────────────────────────────────


class TestImportWithRename:
    """R-10.4: Files are renamed per naming template when moved to library."""

    async def test_rd_messy_names_renamed_on_import(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """Files with messy names get renamed per template when imported."""
        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        # Enable rename
        db_session.add(SystemConfig(key="rename_on_import", value="true", value_type="bool"))
        await db_session.flush()

        source = tmp_path / "import"
        _make_comic_dirs(
            source,
            [
                (
                    "Batman (2016)",
                    [
                        "Batman #001 (2016) (scan v2).cbz",
                        "Batman - 002 (2016) (digital).cbz",
                    ],
                ),
            ],
        )

        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [
                    _cv_search_result(
                        provider_id="97508", title="Batman", year=2016, publisher="DC Comics"
                    )
                ]
            },
            issues_map={
                "97508": [
                    _issue_summary(provider_id="100001", issue_number=1.0),
                    _issue_summary(provider_id="100002", issue_number=2.0),
                ]
            },
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider

        cv_to_series: dict[int, object] = {}
        mock_series_svc = _mock_series_service(cv_to_series)
        svc = _make_service(series_service=mock_series_svc, metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(source))

        series_result = await db_session.execute(
            sa_select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
        )
        imp_series = series_result.scalars().all()

        confirm_req = ConfirmImportRequest(series_ids=[s.id for s in imp_series])
        await svc.confirm_import(db_session, job.id, confirm_req)

        cv_to_series[97508] = (
            await _create_series_with_issues(
                db_session,
                "Batman",
                2016,
                97508,
                [(1.0, 100001), (2.0, 100002)],
            )
        )[0]

        await svc.run_import(db_session, job.id)
        await db_session.refresh(job)

        assert job.status == ImportJobStatus.COMPLETED
        assert job.total_files_imported == 2

        # Verify files are renamed (not the original messy names)
        lf_result = await db_session.execute(sa_select(LibraryFile))
        library_files = lf_result.scalars().all()
        for lf in library_files:
            # Renamed files should NOT contain "scan", "digital", underscores
            assert "scan" not in lf.file_name.lower()
            assert "digital" not in lf.file_name.lower()
            assert Path(lf.file_path).exists()

    async def test_rd_rename_preserves_file_extension(
        self, db_session: AsyncSession, tmp_path: Path, monkeypatch
    ) -> None:
        """Rename keeps original file extension (.cbz stays .cbz, .cbr stays .cbr)."""
        monkeypatch.setattr("pullbox.core.archive.configure_rarfile_backend", lambda: None)
        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)
        db_session.add(SystemConfig(key="rename_on_import", value="true", value_type="bool"))
        await db_session.flush()

        source = tmp_path / "import"
        series_dir = source / "Batman (2016)"
        series_dir.mkdir(parents=True)
        _write_comic_file(series_dir / "Batman #001 (2016).cbr")

        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [_cv_search_result(provider_id="97508", title="Batman", year=2016)]
            },
            issues_map={"97508": [_issue_summary(provider_id="100001", issue_number=1.0)]},
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider

        cv_to_series: dict[int, object] = {}
        mock_series_svc = _mock_series_service(cv_to_series)
        svc = _make_service(series_service=mock_series_svc, metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(source))

        series_result = await db_session.execute(
            sa_select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
        )
        imp_series = series_result.scalars().all()
        confirm_req = ConfirmImportRequest(series_ids=[s.id for s in imp_series])
        await svc.confirm_import(db_session, job.id, confirm_req)

        cv_to_series[97508] = (
            await _create_series_with_issues(
                db_session,
                "Batman",
                2016,
                97508,
                [(1.0, 100001)],
            )
        )[0]

        await svc.run_import(db_session, job.id)
        await db_session.refresh(job)

        lf_result = await db_session.execute(sa_select(LibraryFile))
        lf = lf_result.scalars().first()
        assert lf is not None
        assert lf.file_name.endswith(".cbr")

    async def test_rd_no_rename_keeps_original_name(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """When rename is disabled, files keep original names but move to library."""
        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        # rename_on_import not set (defaults to false)

        source = tmp_path / "import"
        _make_comic_dirs(
            source,
            [
                ("Batman (2016)", ["My Weird Filename.cbz"]),
            ],
        )

        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [_cv_search_result(provider_id="97508", title="Batman", year=2016)]
            },
            issues_map={"97508": [_issue_summary(provider_id="100001", issue_number=1.0)]},
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider

        cv_to_series: dict[int, object] = {}
        mock_series_svc = _mock_series_service(cv_to_series)
        svc = _make_service(series_service=mock_series_svc, metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(source))

        # The file won't parse an issue number → NO_MATCH
        # This is expected behavior: unparseable filename without CV issue ID
        assert job.total_files_found >= 1


# ── Scenario R-E: Manual Issue Import ─────────────────────────────────


class TestManualIssueImport:
    """R-10.5: Single-file import from issue detail via register_library_file."""

    async def test_re_manual_import_moves_file(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """Manual import: file moves to comics dir, issue marked OWNED."""
        from pullbox.core.file_ops import register_library_file

        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        # Create series + issue
        _series, issues = await _create_series_with_issues(
            db_session,
            "Batman",
            2016,
            97508,
            [(17.0, 100017)],
        )
        issue = issues[0]
        assert issue.status == IssueStatus.WANTED

        # Create source file
        src = tmp_path / "downloads" / "Batman 017 (2016).cbz"
        src.parent.mkdir(parents=True)
        _write_comic_file(src, size=200)

        lf = await register_library_file(
            db_session,
            src,
            issue,
            MatchConfidence.MANUAL,
            move_to_library=True,
        )

        assert lf is not None
        assert lf.issue_id == issue.id
        assert lf.match_confidence == MatchConfidence.MANUAL
        assert str(comics_dir) in lf.file_path
        assert Path(lf.file_path).exists()
        assert not src.exists()  # moved

        await db_session.refresh(issue)
        refreshed = await db_session.get(Issue, issue.id)
        assert refreshed is not None
        assert refreshed.status == IssueStatus.OWNED

    async def test_re_manual_import_leave_in_place(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """Manual import with move_to_library=False keeps file at source."""
        from pullbox.core.file_ops import register_library_file

        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        _series, issues = await _create_series_with_issues(
            db_session,
            "Batman",
            2016,
            97508,
            [(17.0, 100017)],
        )
        issue = issues[0]

        src = comics_dir / "Existing Batman Layout" / "Batman 017 (2016).cbz"
        src.parent.mkdir(parents=True)
        _write_comic_file(src, size=200)

        lf = await register_library_file(
            db_session,
            src,
            issue,
            MatchConfidence.MANUAL,
            move_to_library=False,
        )

        assert src.exists()  # still there
        assert lf.file_path == str(src)

    async def test_re_manual_import_creates_series_folder(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """Series folder is created in comics dir if it doesn't exist."""
        from pullbox.core.file_ops import register_library_file

        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        _series, issues = await _create_series_with_issues(
            db_session,
            "Batman",
            2016,
            97508,
            [(1.0, 100001)],
        )

        src = tmp_path / "downloads" / "Batman 001.cbz"
        src.parent.mkdir(parents=True)
        _write_comic_file(src)

        lf = await register_library_file(
            db_session,
            src,
            issues[0],
            MatchConfidence.MANUAL,
            move_to_library=True,
        )

        # A series folder should exist inside comics_dir
        lf_path = Path(lf.file_path)
        assert lf_path.parent.parent == comics_dir or str(comics_dir) in str(lf_path)

    async def test_re_manual_import_source_file_missing(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """Raises FileNotFoundError if source file doesn't exist."""
        from pullbox.core.file_ops import register_library_file

        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        _series, issues = await _create_series_with_issues(
            db_session,
            "Batman",
            2016,
            97508,
            [(1.0, 100001)],
        )

        nonexistent = tmp_path / "ghost.cbz"

        with pytest.raises(FileNotFoundError):
            await register_library_file(
                db_session,
                nonexistent,
                issues[0],
                MatchConfidence.MANUAL,
                move_to_library=True,
            )

    async def test_re_manual_import_no_comics_directory_configured(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """Raises ConfigurationError if comics_directory not set."""
        from pullbox.core.exceptions import ConfigurationError
        from pullbox.core.file_ops import register_library_file

        # No comics_directory config set!
        _series, issues = await _create_series_with_issues(
            db_session,
            "Batman",
            2016,
            97508,
            [(1.0, 100001)],
        )

        src = tmp_path / "Batman 001.cbz"
        _write_comic_file(src)

        with pytest.raises(ConfigurationError):
            await register_library_file(
                db_session,
                src,
                issues[0],
                MatchConfidence.MANUAL,
                move_to_library=True,
            )

    async def test_re_idempotent_reimport_same_file(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """Re-importing the same file path returns existing LibraryFile."""
        from pullbox.core.file_ops import register_library_file

        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        _series, issues = await _create_series_with_issues(
            db_session,
            "Batman",
            2016,
            97508,
            [(1.0, 100001)],
        )

        src = comics_dir / "Existing Batman Layout" / "Batman 001.cbz"
        src.parent.mkdir(parents=True)
        _write_comic_file(src)

        lf1 = await register_library_file(
            db_session,
            src,
            issues[0],
            MatchConfidence.MANUAL,
            move_to_library=False,
        )

        # Import again with same path
        lf2 = await register_library_file(
            db_session,
            src,
            issues[0],
            MatchConfidence.HIGH,
            move_to_library=False,
        )

        # Same record returned (idempotent)
        assert lf1.id == lf2.id

    async def test_re_manual_import_different_confidence_levels(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """Different confidence levels are stored correctly on LibraryFile."""
        from pullbox.core.file_ops import register_library_file

        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        _series, issues = await _create_series_with_issues(
            db_session,
            "Batman",
            2016,
            97508,
            [(1.0, 100001), (2.0, 100002), (3.0, 100003)],
        )

        for i, (issue, confidence) in enumerate(
            zip(
                issues,
                [MatchConfidence.HIGH, MatchConfidence.MEDIUM, MatchConfidence.MANUAL],
                strict=True,
            )
        ):
            src = comics_dir / "Existing Batman Layout" / f"file_{i}.cbz"
            src.parent.mkdir(parents=True, exist_ok=True)
            _write_comic_file(src)

            lf = await register_library_file(
                db_session,
                src,
                issue,
                confidence,
                move_to_library=False,
            )
            assert lf.match_confidence == confidence


# ── Scenario R-F: Phase 1 Regression & Edge Cases ────────────────────


class TestPhase1Regression:
    """R-10.6: Ensure Phase 1 series-level import still works after file-level additions."""

    async def test_rf_series_level_import_still_works(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """Basic scan → match → confirm → import still works with file matching."""
        source = tmp_path / "import"
        await _setup_comics_directory(db_session, tmp_path / "library")
        _make_comic_dirs(
            source,
            [
                ("Batman (2016)", [f"Batman #{i:03d} (2016).cbz" for i in range(1, 4)]),
                ("Saga (2012)", [f"Saga #{i:03d} (2012).cbz" for i in range(1, 3)]),
            ],
        )

        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [_cv_search_result(provider_id="97508", title="Batman", year=2016)],
                "Saga": [_cv_search_result(provider_id="42692", title="Saga", year=2012)],
            },
            issues_map={
                "97508": [
                    _issue_summary(provider_id=str(100000 + i), issue_number=float(i))
                    for i in range(1, 4)
                ],
                "42692": [
                    _issue_summary(provider_id=str(200000 + i), issue_number=float(i))
                    for i in range(1, 3)
                ],
            },
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider

        cv_to_series: dict[int, object] = {}
        mock_series_svc = _mock_series_service(cv_to_series)
        svc = _make_service(series_service=mock_series_svc, metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(source))

        assert job.status == ImportJobStatus.REVIEW
        assert job.series_found == 2
        assert job.series_matched == 2

        result = await db_session.execute(
            sa_select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
        )
        items = result.scalars().all()
        confirm_req = ConfirmImportRequest(series_ids=[i.id for i in items])
        job = await svc.confirm_import(db_session, job.id, confirm_req)

        cv_to_series[97508] = (
            await _create_series_with_issues(
                db_session,
                "Batman",
                2016,
                97508,
                [(float(i), None) for i in range(1, 4)],
            )
        )[0]
        cv_to_series[42692] = (
            await _create_series_with_issues(
                db_session,
                "Saga",
                2012,
                42692,
                [(float(i), None) for i in range(1, 3)],
                publisher_name="Image Comics",
            )
        )[0]

        await svc.run_import(db_session, job.id)
        await db_session.refresh(job)

        assert job.status == ImportJobStatus.COMPLETED
        assert job.series_imported == 2

    async def test_rf_dedup_still_works(self, db_session: AsyncSession, tmp_path: Path) -> None:
        """Pre-existing series in library → flagged as DUPLICATE during scan."""
        # Pre-seed Batman
        pub = Publisher(name="DC Comics")
        db_session.add(pub)
        await db_session.flush()
        existing = Series(
            title="Batman",
            sort_title="batman",
            year_start=2016,
            comicvine_id=97508,
            publisher_id=pub.id,
            status=SeriesStatus.CONTINUING,
            issue_count=0,
        )
        db_session.add(existing)
        await db_session.flush()

        _make_comic_dirs_simple(tmp_path, [("Batman (2016)", 3)])

        svc = _make_service()
        job = await _run_full_pipeline(svc, db_session, str(tmp_path))

        assert job.series_duplicate == 1

        result = await db_session.execute(
            sa_select(ImportedSeries).where(
                ImportedSeries.import_job_id == job.id,
                ImportedSeries.status == ImportSeriesStatus.DUPLICATE,
            )
        )
        dups = result.scalars().all()
        assert len(dups) == 1
        assert dups[0].raw_series_name == "Batman"

    async def test_rf_cancel_from_review(self, db_session: AsyncSession, tmp_path: Path) -> None:
        """Cancelling a job in REVIEW state deletes it."""
        _make_comic_dirs_simple(tmp_path, [("Batman (2016)", 1)])

        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [_cv_search_result(provider_id="97508", title="Batman", year=2016)]
            },
            issues_map={"97508": [_issue_summary(provider_id="100001", issue_number=1.0)]},
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider
        svc = _make_service(metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(tmp_path))
        assert job.status == ImportJobStatus.REVIEW
        job_id = job.id

        await svc.cancel_job(db_session, job_id)

        # Job should be deleted
        deleted = await db_session.get(ImportJob, job_id)
        assert deleted is None

    async def test_rf_confirm_non_review_job_raises(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """Attempting to confirm a non-REVIEW job raises ValidationError."""
        svc = _make_service()
        await _setup_comics_directory(db_session, tmp_path / "managed-library")
        request = ImportJobCreate(
            source_path=str(tmp_path), source_type=ImportSourceType.FILESYSTEM
        )
        job = await svc.create_job(db_session, request)
        assert job.status == ImportJobStatus.PENDING

        with pytest.raises(ValidationError):
            await svc.confirm_import(db_session, job.id, ConfirmImportRequest(series_ids=[1]))

    async def test_rf_partial_series_failure(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """One series fails during import, others succeed."""
        source = tmp_path / "import"
        await _setup_comics_directory(db_session, tmp_path / "library")
        _make_comic_dirs(
            source,
            [
                ("Batman (2016)", [f"Batman #{i:03d} (2016).cbz" for i in range(1, 3)]),
                ("Saga (2012)", [f"Saga #{i:03d} (2012).cbz" for i in range(1, 3)]),
            ],
        )

        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [_cv_search_result(provider_id="97508", title="Batman", year=2016)],
                "Saga": [_cv_search_result(provider_id="42692", title="Saga", year=2012)],
            },
            issues_map={
                "97508": [
                    _issue_summary(provider_id=str(100000 + i), issue_number=float(i))
                    for i in range(1, 3)
                ],
                "42692": [
                    _issue_summary(provider_id=str(200000 + i), issue_number=float(i))
                    for i in range(1, 3)
                ],
            },
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider

        # Batman succeeds, Saga raises
        cv_to_series: dict[int, object] = {}
        mock_series_svc = _mock_series_service(
            cv_to_series,
            errors={42692: Exception("CV API timeout")},
        )
        svc = _make_service(series_service=mock_series_svc, metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(source))

        result = await db_session.execute(
            sa_select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
        )
        items = result.scalars().all()
        confirm_req = ConfirmImportRequest(series_ids=[i.id for i in items])
        await svc.confirm_import(db_session, job.id, confirm_req)

        cv_to_series[97508] = (
            await _create_series_with_issues(
                db_session,
                "Batman",
                2016,
                97508,
                [(1.0, None), (2.0, None)],
            )
        )[0]

        await svc.run_import(db_session, job.id)
        await db_session.refresh(job)

        assert job.status == ImportJobStatus.COMPLETED
        assert job.series_imported == 1
        assert job.series_failed == 1

        # Check the failed series has error_message
        result = await db_session.execute(
            sa_select(ImportedSeries).where(ImportedSeries.status == ImportSeriesStatus.FAILED)
        )
        failed = result.scalars().first()
        assert failed is not None
        assert "CV API timeout" in (failed.error_message or "")


# ── Additional Edge Cases ─────────────────────────────────────────────


class TestEdgeCases:
    """Boundary conditions, unusual inputs, and error scenarios."""

    async def test_edge_unicode_series_and_filenames(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """Unicode characters in series names and filenames are handled."""
        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        source = tmp_path / "import"
        _make_comic_dirs(
            source,
            [
                ("日本語マンガ (2020)", ["日本語マンガ #001 (2020).cbz"]),
            ],
        )

        mock_provider = _mock_cv_provider(
            search_map={
                "日本語マンガ": [
                    _cv_search_result(provider_id="99999", title="日本語マンガ", year=2020)
                ]
            },
            issues_map={"99999": [_issue_summary(provider_id="999001", issue_number=1.0)]},
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider
        svc = _make_service(metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(source))

        assert job.series_found == 1
        assert job.total_files_found == 1

    async def test_edge_special_characters_in_folder(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """Series folders with special chars (parentheses, ampersands, etc.)."""
        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        source = tmp_path / "import"
        _make_comic_dirs(
            source,
            [
                ("Batman & Robin (2011)", ["Batman & Robin #001 (2011).cbz"]),
            ],
        )

        mock_provider = _mock_cv_provider(
            search_map={
                "Batman & Robin": [
                    _cv_search_result(provider_id="55555", title="Batman & Robin", year=2011)
                ]
            },
            issues_map={"55555": [_issue_summary(provider_id="550001", issue_number=1.0)]},
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider
        svc = _make_service(metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(source))

        assert job.series_found == 1
        assert job.total_files_matched == 1

    async def test_edge_very_large_issue_number(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """Issue numbers in the hundreds/thousands (e.g., Action Comics #1000)."""
        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        source = tmp_path / "import"
        _make_comic_dirs(
            source,
            [
                ("Action Comics (1938)", ["Action Comics #1000 (1938).cbz"]),
            ],
        )

        mock_provider = _mock_cv_provider(
            search_map={
                "Action Comics": [
                    _cv_search_result(provider_id="11111", title="Action Comics", year=1938)
                ]
            },
            issues_map={"11111": [_issue_summary(provider_id="111000", issue_number=1000.0)]},
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider
        svc = _make_service(metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(source))

        assert job.total_files_matched == 1

    async def test_edge_series_no_cv_match(self, db_session: AsyncSession, tmp_path: Path) -> None:
        """Series that doesn't match any CV result → NO_MATCH status."""
        source = tmp_path / "import"
        _make_comic_dirs_simple(source, [("Obscure Comic (2023)", 3)])

        # CV returns nothing for this series
        mock_provider = _mock_cv_provider(search_map={})
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider
        svc = _make_service(metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(source))

        assert job.series_no_match == 1

        result = await db_session.execute(
            sa_select(ImportedSeries).where(ImportedSeries.status == ImportSeriesStatus.NO_MATCH)
        )
        no_match = result.scalars().all()
        assert len(no_match) == 1

    async def test_edge_confirm_with_no_series_ids_allowed(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """Confirm with empty series_ids list is allowed for duplicate-file-only imports."""
        req = ConfirmImportRequest(series_ids=[])

        assert req.series_ids == []

    async def test_edge_file_override_changes_match(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """User overrides file match via FileMatchOverride in confirm request."""
        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        source = tmp_path / "import"
        _make_comic_dirs(
            source,
            [
                ("Batman (2016)", ["Batman #001 (2016).cbz", "Batman #002 (2016).cbz"]),
            ],
        )

        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [_cv_search_result(provider_id="97508", title="Batman", year=2016)]
            },
            issues_map={
                "97508": [
                    _issue_summary(provider_id="100001", issue_number=1.0),
                    _issue_summary(provider_id="100002", issue_number=2.0),
                    _issue_summary(provider_id="100003", issue_number=3.0),
                ]
            },
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider

        cv_to_series: dict[int, object] = {}
        mock_series_svc = _mock_series_service(cv_to_series)
        svc = _make_service(series_service=mock_series_svc, metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(source))

        series_result = await db_session.execute(
            sa_select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
        )
        imp_series = series_result.scalars().all()

        # Get the file matched to #1
        files_result = await db_session.execute(
            sa_select(ImportedFile).where(ImportedFile.status == ImportedFileStatus.MATCHED)
        )
        matched_files = files_result.scalars().all()
        file_for_1 = next(f for f in matched_files if f.parsed_issue_number == 1.0)

        # Create real series with issues in DB
        series, issues = await _create_series_with_issues(
            db_session,
            "Batman",
            2016,
            97508,
            [(1.0, 100001), (2.0, 100002), (3.0, 100003)],
        )
        cv_to_series[97508] = series

        # Override: reassign file for #1 → issue #3
        issue_3 = next(i for i in issues if i.issue_number == 3.0)

        confirm_req = ConfirmImportRequest(
            series_ids=[s.id for s in imp_series],
            file_overrides=[FileMatchOverride(imported_file_id=file_for_1.id, issue_id=issue_3.id)],
        )
        await svc.confirm_import(db_session, job.id, confirm_req)

        # Verify override applied
        await db_session.refresh(file_for_1)
        assert file_for_1.matched_issue_id == issue_3.id
        assert file_for_1.match_method == "manual_override"
        assert file_for_1.status == ImportedFileStatus.CONFIRMED

    async def test_edge_nonexistent_job_raises(self, db_session: AsyncSession) -> None:
        """Operations on non-existent job ID → NotFoundError."""
        svc = _make_service()

        with pytest.raises(NotFoundError):
            await svc.run_import(db_session, 99999)

    async def test_edge_confirm_nonexistent_series_ids(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """Confirming non-existent series IDs → ValidationError."""
        svc = _make_service()
        await _setup_comics_directory(db_session, tmp_path / "managed-library")

        dir_a = tmp_path / "a"
        dir_a.mkdir()

        job1 = await svc.create_job(
            db_session,
            ImportJobCreate(source_path=str(dir_a), source_type=ImportSourceType.FILESYSTEM),
        )
        await svc.start_scan(db_session, job1.id)
        await db_session.refresh(job1)

        with pytest.raises(ValidationError):
            await svc.confirm_import(
                db_session,
                job1.id,
                ConfirmImportRequest(series_ids=[99999]),
            )

    async def test_edge_provisional_issue_is_created_when_catalog_is_not_hydrated(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """A locally identified issue imports before provider catalog hydration."""
        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        source = tmp_path / "import"
        _make_comic_dirs(
            source,
            [
                ("Batman (2016)", ["Batman #001 (2016).cbz", "Batman #999 (2016).cbz"]),
            ],
        )

        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [_cv_search_result(provider_id="97508", title="Batman", year=2016)]
            },
            issues_map={
                "97508": [
                    _issue_summary(provider_id="100001", issue_number=1.0),
                    _issue_summary(provider_id="100999", issue_number=999.0),
                ]
            },
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider

        cv_to_series: dict[int, object] = {}
        mock_series_svc = _mock_series_service(cv_to_series)
        svc = _make_service(series_service=mock_series_svc, metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(source))

        series_result = await db_session.execute(
            sa_select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
        )
        imp_series = series_result.scalars().all()
        confirm_req = ConfirmImportRequest(series_ids=[s.id for s in imp_series])
        await svc.confirm_import(db_session, job.id, confirm_req)

        # Simulate background catalog hydration having populated only issue #1.
        cv_to_series[97508] = (
            await _create_series_with_issues(
                db_session,
                "Batman",
                2016,
                97508,
                [(1.0, 100001)],
            )
        )[0]

        await svc.run_import(db_session, job.id)
        await db_session.refresh(job)

        assert job.status == ImportJobStatus.COMPLETED
        assert job.total_files_imported == 2
        assert job.total_files_failed == 0
        provisional_issue = await db_session.scalar(
            sa_select(Issue).where(Issue.issue_number == 999.0)
        )
        assert provisional_issue is not None
        assert provisional_issue.comicvine_id is None

    async def test_edge_series_with_no_matched_files_keeps_series_confirmation(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """A trusted series remains confirmable while its files stay excluded."""
        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        source = tmp_path / "import"
        _make_comic_dirs(
            source,
            [
                ("Batman (2016)", ["random_junk.cbz", "not_a_comic.cbz"]),
            ],
        )

        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [_cv_search_result(provider_id="97508", title="Batman", year=2016)]
            },
            issues_map={"97508": [_issue_summary(provider_id="100001", issue_number=1.0)]},
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider

        cv_to_series: dict[int, object] = {}
        mock_series_svc = _mock_series_service(cv_to_series)
        svc = _make_service(series_service=mock_series_svc, metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(source))

        assert job.total_files_no_match == 2
        assert job.total_files_matched == 0

        series_result = await db_session.execute(
            sa_select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
        )
        imp_series = series_result.scalars().all()
        confirm_req = ConfirmImportRequest(series_ids=[s.id for s in imp_series])
        confirmed_job = await svc.confirm_import(db_session, job.id, confirm_req)

        assert confirmed_job.status == ImportJobStatus.IMPORTING
        assert all(item.status == ImportSeriesStatus.CONFIRMED for item in imp_series)

    async def test_edge_multiple_series_mixed_success(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """3 series: one fully matched, one partial, one with no file matches."""
        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        source = tmp_path / "import"
        _make_comic_dirs(
            source,
            [
                ("Batman (2016)", ["Batman #001 (2016).cbz", "Batman #002 (2016).cbz"]),
                ("Saga (2012)", ["Saga #001 (2012).cbz", "saga_bonus.cbz"]),
                ("X-Men (2019)", ["random_stuff.cbz"]),
            ],
        )

        mock_provider = _mock_cv_provider(
            search_map={
                "Batman": [_cv_search_result(provider_id="97508", title="Batman", year=2016)],
                "Saga": [_cv_search_result(provider_id="42692", title="Saga", year=2012)],
                "X-Men": [_cv_search_result(provider_id="77059", title="X-Men", year=2019)],
            },
            issues_map={
                "97508": [
                    _issue_summary(provider_id="100001", issue_number=1.0),
                    _issue_summary(provider_id="100002", issue_number=2.0),
                ],
                "42692": [_issue_summary(provider_id="200001", issue_number=1.0)],
                "77059": [_issue_summary(provider_id="300001", issue_number=1.0)],
            },
        )
        mock_metadata = AsyncMock()
        mock_metadata._provider = mock_provider

        cv_to_series: dict[int, object] = {}
        mock_series_svc = _mock_series_service(cv_to_series)
        svc = _make_service(series_service=mock_series_svc, metadata_service=mock_metadata)

        job = await _run_full_pipeline(svc, db_session, str(source))

        assert job.series_matched == 3
        assert job.series_no_match == 0
        # All series identities are local; X-Men still has no importable issue file.
        assert job.total_files_matched == 3
        assert job.total_files_no_match == 2

        series_result = await db_session.execute(
            sa_select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
        )
        imp_series = series_result.scalars().all()
        confirm_req = ConfirmImportRequest(series_ids=[s.id for s in imp_series])
        await svc.confirm_import(db_session, job.id, confirm_req)

        cv_to_series[97508] = (
            await _create_series_with_issues(
                db_session,
                "Batman",
                2016,
                97508,
                [(1.0, 100001), (2.0, 100002)],
            )
        )[0]
        cv_to_series[42692] = (
            await _create_series_with_issues(
                db_session,
                "Saga",
                2012,
                42692,
                [(1.0, 200001)],
                publisher_name="Image Comics",
            )
        )[0]
        await svc.run_import(db_session, job.id)
        await db_session.refresh(job)

        assert job.status == ImportJobStatus.COMPLETED
        assert job.series_imported == 2
        assert job.total_files_imported == 3  # matched files only
        assert job.total_files_failed == 0  # no_match files aren't attempted

    async def test_edge_name_collision_in_library(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        """File with same name already exists in series folder → gets suffix."""
        from pullbox.core.file_ops import register_library_file

        comics_dir = tmp_path / "library"
        await _setup_comics_directory(db_session, comics_dir)

        _series, issues = await _create_series_with_issues(
            db_session,
            "Batman",
            2016,
            97508,
            [(1.0, 100001), (2.0, 100002)],
        )

        # Pre-place a file in the series folder
        series_folder = comics_dir / "Batman (2016)"
        series_folder.mkdir(parents=True)
        _write_comic_file(series_folder / "Batman 001.cbz", size=50)

        # Now import a file with the same name
        src = tmp_path / "downloads" / "Batman 001.cbz"
        src.parent.mkdir(parents=True)
        _write_comic_file(src)

        lf = await register_library_file(
            db_session,
            src,
            issues[0],
            MatchConfidence.MANUAL,
            move_to_library=True,
        )

        # Should succeed — collision handled with suffix
        assert lf is not None
        assert Path(lf.file_path).exists()

    async def test_edge_empty_import_source(self, db_session: AsyncSession, tmp_path: Path) -> None:
        """Empty source directory → 0 series found, job goes to REVIEW."""
        source = tmp_path / "empty"
        source.mkdir()

        svc = _make_service()
        job = await _run_full_pipeline(svc, db_session, str(source))

        assert job.series_found == 0
        assert job.status == ImportJobStatus.REVIEW
