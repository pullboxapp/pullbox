"""Tests for import workflow counter helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.series import Series
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
        status=ImportJobStatus.MATCHING,
    )
    session.add(job)
    await session.flush()
    return job


def _make_imported_file(
    job: ImportJob,
    series: ImportedSeries,
    *,
    name: str,
    status: ImportedFileStatus,
) -> ImportedFile:
    return ImportedFile(
        import_job_id=job.id,
        import_series_id=series.id,
        file_path=f"/tmp/comics/{name}",
        file_name=name,
        file_size=1024,
        file_format="cbz",
        status=status,
    )


def test_job_stats_exports_progress_counter_snapshot() -> None:
    job = ImportJob(
        source_path="/tmp/comics",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.MATCHING,
        scan_total_files=7,
        scan_total_dirs=2,
        series_found=3,
        series_duplicate=1,
        series_matched=2,
        series_no_match=1,
        series_new=2,
        series_imported=1,
        series_failed=1,
        total_files_found=9,
        total_files_matched=4,
        total_files_duplicate=1,
        total_files_already_owned=2,
        total_files_conflict=1,
        total_files_no_match=1,
        total_files_imported=3,
        total_files_failed=1,
    )

    assert ImportService._job_stats(job) == {
        "scan_total_files": 7,
        "scan_total_dirs": 2,
        "series_found": 3,
        "series_duplicate": 1,
        "series_matched": 2,
        "series_no_match": 1,
        "series_new": 2,
        "series_imported": 1,
        "series_failed": 1,
        "total_files_found": 9,
        "total_files_matched": 4,
        "total_files_duplicate": 1,
        "total_files_already_owned": 2,
        "total_files_conflict": 1,
        "total_files_no_match": 1,
        "total_files_imported": 3,
        "total_files_failed": 1,
    }


async def test_recompute_series_counters_counts_persisted_statuses(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    db_session.add_all(
        [
            ImportedSeries(
                import_job_id=job.id,
                raw_series_name="Absolute Wonder Woman",
                status=ImportSeriesStatus.MATCHED,
            ),
            ImportedSeries(
                import_job_id=job.id,
                raw_series_name="Absolute Batman",
                status=ImportSeriesStatus.DUPLICATE,
            ),
            ImportedSeries(
                import_job_id=job.id,
                raw_series_name="Absolute Flash",
                status=ImportSeriesStatus.NO_MATCH,
            ),
        ]
    )
    await db_session.flush()

    await service._recompute_series_counters(db_session, job)

    assert job.series_found == 3
    assert job.series_matched == 1
    assert job.series_duplicate == 1
    assert job.series_no_match == 1
    assert job.series_new == 2


async def test_recompute_file_counters_updates_series_job_and_duplicate_diagnostics(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    existing_series = Series(
        title="Absolute Wonder Woman",
        sort_title="absolute wonder woman",
    )
    db_session.add(existing_series)
    await db_session.flush()
    duplicate_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Absolute Wonder Woman",
        status=ImportSeriesStatus.DUPLICATE,
        series_id=existing_series.id,
        diagnostics={"kind": "duplicate_series"},
    )
    matched_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Absolute Batman",
        status=ImportSeriesStatus.MATCHED,
    )
    db_session.add_all([duplicate_series, matched_series])
    await db_session.flush()
    db_session.add_all(
        [
            _make_imported_file(
                job,
                duplicate_series,
                name="ww-001.cbz",
                status=ImportedFileStatus.MATCHED,
            ),
            _make_imported_file(
                job,
                duplicate_series,
                name="ww-owned.cbz",
                status=ImportedFileStatus.ALREADY_OWNED,
            ),
            _make_imported_file(
                job,
                matched_series,
                name="batman-001.cbz",
                status=ImportedFileStatus.CONFIRMED,
            ),
            _make_imported_file(
                job,
                matched_series,
                name="batman-failed.cbz",
                status=ImportedFileStatus.FAILED,
            ),
        ]
    )
    await db_session.flush()

    await service._recompute_file_counters(db_session, job)

    assert duplicate_series.files_total == 2
    assert duplicate_series.files_matched == 1
    assert duplicate_series.files_already_owned == 1
    assert duplicate_series.diagnostics["actionable_duplicate_merge"] is True
    assert duplicate_series.diagnostics["has_importable_files"] is True
    assert duplicate_series.diagnostics["importable_files"] == 1
    assert matched_series.files_matched == 1
    assert matched_series.files_failed == 1
    assert job.total_files_found == 4
    assert job.total_files_matched == 2
    assert job.total_files_already_owned == 1
    assert job.total_files_failed == 1


async def test_recompute_file_counters_does_not_shrink_discovery_total(db_session) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    job.total_files_found = 5
    job.scan_total_files = 6
    series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Batman",
        status=ImportSeriesStatus.MATCHED,
    )
    db_session.add(series)
    await db_session.flush()
    db_session.add_all(
        [
            _make_imported_file(
                job,
                series,
                name="batman-001.cbz",
                status=ImportedFileStatus.IMPORTED,
            ),
            _make_imported_file(
                job,
                series,
                name="batman-changed.cbz",
                status=ImportedFileStatus.FAILED,
            ),
        ]
    )
    await db_session.flush()

    await service._recompute_file_counters(db_session, job)

    assert job.total_files_found == 6
    assert job.total_files_imported == 1
    assert job.total_files_failed == 1
