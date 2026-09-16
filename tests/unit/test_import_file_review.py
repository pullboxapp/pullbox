"""Tests for import file-review helper shims."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from pullbox.core.exceptions import ValidationError
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobLog,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series, SeriesStatus
from pullbox.services.import_service import ImportService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _make_service() -> ImportService:
    return ImportService(
        series_service=AsyncMock(),
        metadata_service=AsyncMock(),
        event_bus=AsyncMock(),
    )


async def _create_job_row(session: AsyncSession) -> ImportJob:
    job = ImportJob(
        source_path="/tmp/comics",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
    )
    session.add(job)
    await session.flush()
    return job


async def _create_library_issue(session: AsyncSession) -> Issue:
    series = Series(
        title="Absolute Wonder Woman",
        sort_title="absolute wonder woman",
        year_start=2024,
        comicvine_id=162966,
        status=SeriesStatus.CONTINUING,
    )
    session.add(series)
    await session.flush()
    issue = Issue(
        series_id=series.id,
        issue_number=19.0,
        title="The Ties That Bind",
        comicvine_id=123456,
        status=IssueStatus.WANTED,
    )
    session.add(issue)
    await session.flush()
    return issue


async def _create_imported_series(
    session: AsyncSession,
    job: ImportJob,
    *,
    status: ImportSeriesStatus = ImportSeriesStatus.MATCHED,
    series_id: int | None = None,
    diagnostics: dict[str, object] | None = None,
) -> ImportedSeries:
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Absolute Wonder Woman",
        raw_year=2024,
        status=status,
        series_id=series_id,
        file_count=1,
        files_total=1,
        diagnostics=diagnostics,
    )
    session.add(imported_series)
    await session.flush()
    return imported_series


def _make_imported_file(job: ImportJob, imported_series: ImportedSeries) -> ImportedFile:
    return ImportedFile(
        import_job_id=job.id,
        import_series_id=imported_series.id,
        file_path="/tmp/absolute-wonder-woman-019.cbz",
        file_name="absolute-wonder-woman-019.cbz",
        file_size=1024,
        file_format="cbz",
        status=ImportedFileStatus.NO_MATCH,
    )


async def test_override_file_match_updates_file_and_logs_event(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    imported_series = await _create_imported_series(db_session, job)
    issue = await _create_library_issue(db_session)
    imported_file = _make_imported_file(job, imported_series)
    db_session.add(imported_file)
    await db_session.flush()

    updated = await service.override_file_match(
        db_session,
        job.id,
        imported_file.id,
        issue.id,
    )

    assert updated.status == ImportedFileStatus.MATCHED
    assert updated.include_in_import is False
    assert updated.matched_issue_id == issue.id
    assert updated.match_method == "manual_override"
    assert updated.diagnostics["target_issue_id"] == issue.id
    log = (
        await db_session.execute(
            select(ImportJobLog).where(
                ImportJobLog.import_job_id == job.id,
                ImportJobLog.event == "import_file_match_override",
            )
        )
    ).scalar_one()
    assert log.data["file_id"] == imported_file.id
    assert log.data["issue_id"] == issue.id


@pytest.mark.parametrize("repair", [False, True])
async def test_manual_match_cannot_bypass_archive_content_review(
    db_session: AsyncSession,
    repair: bool,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    imported_series = await _create_imported_series(db_session, job)
    issue = await _create_library_issue(db_session)
    imported_file = _make_imported_file(job, imported_series)
    imported_file.status = ImportedFileStatus.SAFETY_BLOCKED
    imported_file.diagnostics = {
        "safety_block": {"code": "archive_no_pages", "overrideable": False}
    }
    db_session.add(imported_file)
    await db_session.flush()
    with pytest.raises(ValidationError, match="safety review"):
        await service.override_file_match(
            db_session,
            job.id,
            imported_file.id,
            issue.id,
            repair_source_metadata=repair,
        )
    assert imported_file.status is ImportedFileStatus.SAFETY_BLOCKED
    assert not imported_file.include_in_import


async def test_low_level_manual_match_cannot_bypass_safety_review(db_session):
    service = _make_service()
    job = await _create_job_row(db_session)
    item = await _create_imported_series(db_session, job)
    issue = await _create_library_issue(db_session)
    file = _make_imported_file(job, item)
    file.status = ImportedFileStatus.SAFETY_BLOCKED
    db_session.add(file)
    await db_session.flush()
    with pytest.raises(ValidationError, match="safety review"):
        await service._apply_manual_file_match(db_session, file, issue, method="manual_override")


async def test_override_file_match_rejects_non_actionable_duplicate_series(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    issue = await _create_library_issue(db_session)
    imported_series = await _create_imported_series(
        db_session,
        job,
        status=ImportSeriesStatus.DUPLICATE,
        series_id=issue.series_id,
        diagnostics={
            "kind": "duplicate_series",
            "actionable_duplicate_merge": False,
            "fully_owned_series": True,
        },
    )
    imported_file = _make_imported_file(job, imported_series)
    db_session.add(imported_file)
    await db_session.flush()

    with pytest.raises(ValidationError, match="manual reassignment is disabled"):
        await service.override_file_match(
            db_session,
            job.id,
            imported_file.id,
            issue.id,
        )


async def test_override_duplicate_file_marks_already_owned_target(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    issue = await _create_library_issue(db_session)
    imported_series = await _create_imported_series(
        db_session,
        job,
        status=ImportSeriesStatus.DUPLICATE,
        series_id=issue.series_id,
        diagnostics={
            "kind": "duplicate_series",
            "actionable_duplicate_merge": True,
        },
    )
    library_root = LibraryRoot(name="Main", path="/library")
    db_session.add(library_root)
    await db_session.flush()
    db_session.add(
        LibraryFile(
            file_path=str(Path("/library/absolute-wonder-woman-019.cbz")),
            file_name="absolute-wonder-woman-019.cbz",
            file_size=2048,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.HIGH,
            issue_id=issue.id,
            library_root_id=library_root.id,
        )
    )
    imported_file = _make_imported_file(job, imported_series)
    db_session.add(imported_file)
    await db_session.flush()

    updated = await service.override_file_match(
        db_session,
        job.id,
        imported_file.id,
        issue.id,
    )

    assert updated.status == ImportedFileStatus.ALREADY_OWNED
    assert updated.include_in_import is False
    assert updated.diagnostics["target_state"] == "already_owned"
