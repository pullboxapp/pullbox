"""Tests for import scan setup helpers."""

from __future__ import annotations

import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from pullbox.core.collection_scanner import DiscoveredFile, DiscoveredSeries
from pullbox.core.file_safety import FileSafetyError
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
from pullbox.models.story_arc import StoryArcResolutionState, StoryArcSourceKind
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.services import import_scan_helpers
from pullbox.services.import_scan_helpers import (
    reset_scan_artifacts,
    validate_discovered_files_safety,
)

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession


async def _create_job_row(session: AsyncSession) -> ImportJob:
    job = ImportJob(
        source_path="/tmp/comics",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.SCANNING,
        error_message="Previous failure",
        progress_snapshot={"progress": 50},
        scan_total_files=10,
        scan_total_dirs=3,
        series_found=2,
        series_duplicate=1,
        series_matched=1,
        series_no_match=1,
        series_new=1,
        total_files_found=5,
        total_files_matched=4,
        total_files_duplicate=1,
        total_files_already_owned=1,
        total_files_conflict=1,
        total_files_no_match=1,
        scan_started_at=datetime.now(UTC),
        scan_completed_at=datetime.now(UTC),
        match_started_at=datetime.now(UTC),
        match_completed_at=datetime.now(UTC),
    )
    session.add(job)
    await session.flush()
    return job


def _discovered_file(path: str) -> DiscoveredFile:
    return DiscoveredFile(
        file_path=path,
        file_name=Path(path).name,
        file_size=100,
        file_format="cbz",
        parsed_series="Batman",
        parsed_issue_number=1.0,
        parsed_year=2016,
        parsed_publisher=None,
        has_comicinfo=False,
        comicvine_issue_id=None,
        issue_number_raw="001",
    )


def _discovered_series(*paths: str) -> DiscoveredSeries:
    return DiscoveredSeries(
        raw_series_name="Batman",
        raw_year=2016,
        raw_publisher=None,
        file_count=len(paths),
        sample_paths=list(paths),
        source_folder="/tmp/comics/Batman",
        source_folder_relative="Batman",
        files=[_discovered_file(path) for path in paths],
    )


async def test_reset_scan_artifacts_deletes_rows_and_clears_counters(
    db_session: AsyncSession,
) -> None:
    job = await _create_job_row(db_session)
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Batman",
        status=ImportSeriesStatus.MATCHED,
    )
    db_session.add(imported_series)
    await db_session.flush()
    db_session.add(
        imported_file := ImportedFile(
            import_job_id=job.id,
            import_series_id=imported_series.id,
            file_path="/tmp/comics/Batman/Batman 001.cbz",
            file_name="Batman 001.cbz",
            file_size=100,
            file_format="cbz",
            status=ImportedFileStatus.MATCHED,
        )
    )
    await db_session.flush()
    staged_arc = ImportedStoryArc(
        import_job_id=job.id,
        source_kind=StoryArcSourceKind.FOLDER,
        source_key="folder:batman",
        source_ordinal=1,
        name="Batman Event",
    )
    db_session.add(staged_arc)
    await db_session.flush()
    db_session.add(
        ImportedStoryArcEntry(
            imported_story_arc_id=staged_arc.id,
            import_file_id=imported_file.id,
            source_ordinal=1,
            resolution_state=StoryArcResolutionState.PENDING,
            source_kind=StoryArcSourceKind.FOLDER,
        )
    )
    await db_session.flush()

    await reset_scan_artifacts(db_session, job)

    assert await db_session.scalar(select(func.count(ImportedStoryArc.id))) == 0
    assert await db_session.scalar(select(func.count(ImportedStoryArcEntry.id))) == 0

    assert job.error_message is None
    assert job.progress_snapshot == {}
    assert job.scan_total_files == 0
    assert job.scan_total_dirs == 0
    assert job.series_found == 0
    assert job.series_duplicate == 0
    assert job.series_matched == 0
    assert job.series_no_match == 0
    assert job.series_new == 0
    assert job.total_files_found == 0
    assert job.total_files_matched == 0
    assert job.total_files_duplicate == 0
    assert job.total_files_already_owned == 0
    assert job.total_files_conflict == 0
    assert job.total_files_no_match == 0
    assert job.scan_started_at is None
    assert job.scan_completed_at is None
    assert job.match_started_at is None
    assert job.match_completed_at is None
    assert (await db_session.execute(select(ImportedSeries))).scalars().all() == []
    assert (await db_session.execute(select(ImportedFile))).scalars().all() == []


async def test_validate_discovered_files_safety_checks_each_unique_path_once(
    db_session: AsyncSession,
) -> None:
    checked_paths: list[Path] = []

    async def check_file_safety(_session: AsyncSession, path: Path) -> None:
        checked_paths.append(path)

    await validate_discovered_files_safety(
        db_session,
        [
            _discovered_series("/tmp/comics/Batman 001.cbz", "/tmp/comics/Batman 001.cbz"),
            _discovered_series("/tmp/comics/Batman 002.cbz"),
        ],
        check_file_safety=check_file_safety,
    )

    assert checked_paths == [
        Path("/tmp/comics/Batman 001.cbz"),
        Path("/tmp/comics/Batman 002.cbz"),
    ]


async def test_validate_discovered_files_safety_reuses_default_policy_for_batch(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_session.add_all(
        [
            SystemConfig(key="block_dangerous_files", value="false", value_type="bool"),
            SystemConfig(key="archive_size_limit_mb", value="123", value_type="int"),
        ]
    )
    await db_session.flush()
    safety_runs: list[tuple[Path, bool, int]] = []

    def fake_run_safety_checks(
        path: Path,
        *,
        block_dangerous: bool,
        max_archive_size: int,
    ) -> None:
        safety_runs.append((path, block_dangerous, max_archive_size))

    monkeypatch.setattr(import_scan_helpers, "run_safety_checks", fake_run_safety_checks)

    await validate_discovered_files_safety(
        db_session,
        [
            _discovered_series(
                "/tmp/comics/Batman 001.cbz",
                "/tmp/comics/Batman 002.cbz",
            )
        ],
    )

    assert safety_runs == [
        (Path("/tmp/comics/Batman 001.cbz"), False, 123 * 1024 * 1024),
        (Path("/tmp/comics/Batman 002.cbz"), False, 123 * 1024 * 1024),
    ]


async def test_validate_discovered_files_safety_reuses_compact_archive_evidence(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "Batman 001.cbz"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Batman 001 p001.jpg", b"page")
        archive.writestr(
            "metadata/ComicInfo.xml",
            (
                "<ComicInfo><Series>Batman</Series><Number>1</Number>"
                "<StoryArc>Batman: The Court of Owls</StoryArc>"
                "<StoryArcNumber>001.50-A</StoryArcNumber>"
                "<Notes>[cv_vol_id:42721]</Notes></ComicInfo>"
            ),
        )
    discovered = _discovered_series(str(archive_path))

    await validate_discovered_files_safety(db_session, [discovered])

    evidence = discovered.files[0].metadata_diagnostics["archive_member_evidence"]
    assert evidence == {
        "member_index_scanned": True,
        "comicinfo_entry_count": 1,
        "comicinfo_entry": "metadata/ComicInfo.xml",
        "comicinfo": {
            "series": "Batman",
            "number": "1",
            "volume": None,
            "title": None,
            "year": None,
            "month": None,
            "day": None,
            "publisher": None,
            "notes": "[cv_vol_id:42721]",
            "summary": None,
            "writer": None,
            "penciller": None,
            "inker": None,
            "colorist": None,
            "letterer": None,
            "cover_artist": None,
            "editor": None,
            "page_count": None,
            "genre": None,
            "web": None,
            "story_arc": "Batman: The Court of Owls",
            "story_arc_number": "001.50-A",
            "series_group": None,
            "language": None,
        },
    }
    assert "entry_names" not in evidence


async def test_validate_discovered_files_safety_closes_metadata_poor_archive_probe(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "Batman 001.cbz"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("page001.jpg", b"page")
    discovered = _discovered_series(str(archive_path))
    discovered.files[0].metadata_diagnostics.update(
        {
            "archive_metadata_loaded": False,
            "archive_metadata_deferred": True,
        }
    )

    await validate_discovered_files_safety(db_session, [discovered])

    diagnostics = discovered.files[0].metadata_diagnostics
    assert diagnostics["archive_metadata_loaded"] is True
    assert diagnostics["archive_metadata_deferred"] is False
    assert diagnostics["archive_entry_issue_hint_checked"] is True
    assert diagnostics["has_comicinfo"] is False
    assert "archive_member_evidence" not in diagnostics


async def test_validate_discovered_files_safety_marks_blocked_files_without_raising(
    db_session: AsyncSession,
) -> None:
    """Oversized archives should become review rows instead of failing the whole scan."""
    discovered = _discovered_series(
        "/tmp/comics/Batman 001.cbz",
        "/tmp/comics/Batman Omnibus.cbz",
    )

    async def check_file_safety(_session: AsyncSession, path: Path) -> None:
        if path.name == "Batman Omnibus.cbz":
            raise FileSafetyError(
                "Archive decompressed size (4,248,234,210 bytes) exceeds limit "
                "(2,097,152,000 bytes)",
                details=[str(path)],
            )

    await validate_discovered_files_safety(
        db_session,
        [discovered],
        check_file_safety=check_file_safety,
    )

    normal_file, blocked_file = discovered.files
    assert "file_safety" not in normal_file.metadata_diagnostics
    assert blocked_file.metadata_diagnostics["file_safety"] == {
        "kind": "archive_decompressed_size",
        "category": "decompression_size_limit",
        "code": "archive_decompressed_size_limit",
        "reason": "The archive exceeds Pullbox's configured decompressed-size limit.",
        "sanitized_reason": "The archive exceeds Pullbox's configured decompressed-size limit.",
        "source": "file_safety",
        "retryable": False,
        "overrideable": True,
    }


async def test_validate_discovered_files_safety_sanitizes_non_overrideable_failure(
    db_session: AsyncSession,
) -> None:
    discovered = _discovered_series("/tmp/comics/Corrupt 001.cbz")

    async def check_file_safety(_session: AsyncSession, path: Path) -> None:
        raise FileSafetyError(
            f"Archive could not be inspected: {path}",
            details=[str(path), "/mnt/user/private/not-for-ui.cbz"],
        )

    await validate_discovered_files_safety(
        db_session,
        [discovered],
        check_file_safety=check_file_safety,
    )

    safety = discovered.files[0].metadata_diagnostics["file_safety"]
    assert safety["category"] == "archive_inspection_failed"
    assert safety["code"] == "archive_inspection_failed"
    assert safety["retryable"] is True
    assert safety["overrideable"] is False
    assert "/tmp/comics" not in str(safety)
    assert "/mnt/user/private" not in str(safety)
