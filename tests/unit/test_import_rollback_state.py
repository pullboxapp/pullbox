"""Tests for import rollback review-state restoration helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series, SeriesType
from pullbox.services.import_rollback_state import restore_review_state_after_rollback

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _create_job(session: AsyncSession) -> ImportJob:
    job = ImportJob(
        source_path="/tmp/comics",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.ROLLING_BACK,
    )
    session.add(job)
    await session.flush()
    return job


async def _create_library_series(session: AsyncSession, title: str) -> Series:
    series = Series(
        title=title,
        sort_title=title.lower(),
        year_start=2024,
        comicvine_id=1000 + len(title),
        series_type=SeriesType.STANDARD,
    )
    session.add(series)
    await session.flush()
    return series


async def _create_library_file(session: AsyncSession, file_name: str) -> LibraryFile:
    root = LibraryRoot(name="Comics", path="/tmp/library")
    session.add(root)
    await session.flush()
    library_file = LibraryFile(
        file_path=f"/tmp/library/{file_name}",
        file_name=file_name,
        file_size=10,
        file_format=FileFormat.PDF,
        file_modified_at=datetime.now(UTC),
        library_root_id=root.id,
        match_confidence=MatchConfidence.HIGH,
    )
    session.add(library_file)
    await session.flush()
    return library_file


async def _create_imported_series(
    session: AsyncSession,
    job: ImportJob,
    *,
    status: ImportSeriesStatus,
    series_id: int | None = None,
) -> ImportedSeries:
    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Absolute Wonder Woman",
        raw_year=2024,
        status=status,
        file_count=1,
        series_id=series_id,
        error_message="previous error",
        files_imported=1,
        files_failed=1,
    )
    session.add(item)
    await session.flush()
    return item


def _make_file(
    job: ImportJob,
    item: ImportedSeries,
    *,
    status: ImportedFileStatus,
    suffix: str,
    conflict_group_id: int | None = None,
    matched_issue_cv_id: int | None = None,
    diagnostics: dict[str, object] | None = None,
) -> ImportedFile:
    return ImportedFile(
        import_job_id=job.id,
        import_series_id=item.id,
        file_path=f"/tmp/comics/Absolute Wonder Woman {suffix}.cbz",
        file_name=f"Absolute Wonder Woman {suffix}.cbz",
        file_size=10,
        file_format="cbz",
        status=status,
        conflict_group_id=conflict_group_id,
        matched_issue_cv_id=matched_issue_cv_id,
        include_in_import=True,
        error_message="previous file error",
        diagnostics=diagnostics or {},
    )


async def test_restore_review_state_after_rollback_resets_series_and_files(
    db_session: AsyncSession,
) -> None:
    job = await _create_job(db_session)
    library_series = await _create_library_series(db_session, "Absolute Wonder Woman")
    imported = await _create_imported_series(
        db_session,
        job,
        status=ImportSeriesStatus.IMPORTED,
        series_id=library_series.id,
    )
    duplicate = await _create_imported_series(
        db_session,
        job,
        status=ImportSeriesStatus.DUPLICATE,
        series_id=library_series.id,
    )
    pending = await _create_imported_series(
        db_session,
        job,
        status=ImportSeriesStatus.PENDING,
    )
    conflict_file = _make_file(
        job,
        imported,
        status=ImportedFileStatus.IMPORTED,
        suffix="019",
        conflict_group_id=42,
    )
    already_owned_file = _make_file(
        job,
        imported,
        status=ImportedFileStatus.FAILED,
        suffix="020",
        diagnostics={"target_state": "already_owned"},
    )
    matched_file = _make_file(
        job,
        imported,
        status=ImportedFileStatus.CONFIRMED,
        suffix="021",
        matched_issue_cv_id=12345,
    )
    no_match_file = _make_file(
        job,
        imported,
        status=ImportedFileStatus.SKIPPED,
        suffix="022",
    )
    duplicate_matched_file = _make_file(
        job,
        duplicate,
        status=ImportedFileStatus.IMPORTED,
        suffix="023",
        matched_issue_cv_id=23456,
    )
    pending_file = _make_file(
        job,
        pending,
        status=ImportedFileStatus.PENDING,
        suffix="024",
    )
    db_session.add_all(
        [
            conflict_file,
            already_owned_file,
            matched_file,
            no_match_file,
            duplicate_matched_file,
            pending_file,
        ]
    )
    await db_session.flush()

    await restore_review_state_after_rollback(db_session, job.id)

    assert imported.status == ImportSeriesStatus.MATCHED
    assert imported.series_id is None
    assert imported.error_message is None
    assert imported.files_imported == 0
    assert imported.files_failed == 0
    assert duplicate.status == ImportSeriesStatus.DUPLICATE
    assert duplicate.series_id == library_series.id
    assert pending.status == ImportSeriesStatus.PENDING

    assert conflict_file.status == ImportedFileStatus.CONFLICT
    assert conflict_file.include_in_import is False
    assert already_owned_file.status == ImportedFileStatus.ALREADY_OWNED
    assert already_owned_file.include_in_import is False
    assert matched_file.status == ImportedFileStatus.MATCHED
    assert matched_file.include_in_import is False
    assert no_match_file.status == ImportedFileStatus.NO_MATCH
    assert no_match_file.include_in_import is False
    assert no_match_file.diagnostics["reason"] == "rollback_restored_unmatched"
    assert duplicate_matched_file.status == ImportedFileStatus.MATCHED
    assert duplicate_matched_file.include_in_import is False
    assert pending_file.status == ImportedFileStatus.PENDING


async def test_restore_review_state_after_rollback_preserves_orphan_recovery_context(
    db_session: AsyncSession,
) -> None:
    job = await _create_job(db_session)
    library_series = await _create_library_series(db_session, "Henchgirl")
    recovered = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Henchgirl expanded Edition Comic",
        raw_year=2020,
        status=ImportSeriesStatus.IMPORTED,
        file_count=2,
        cv_id=130322,
        user_selected_cv_id=130322,
        cv_title="Henchgirl",
        series_id=library_series.id,
        files_imported=1,
    )
    db_session.add(recovered)
    await db_session.flush()
    library_file = await _create_library_file(db_session, "Henchgirl 001.pdf")

    assigned_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=recovered.id,
        file_path="/tmp/imports/Henchgirl 001.pdf",
        file_name="Henchgirl 001.pdf",
        file_size=10,
        file_format="pdf",
        status=ImportedFileStatus.IMPORTED,
        matched_issue_cv_id=799319,
        include_in_import=True,
        library_file_id=library_file.id,
        error_message="import failed previously",
        diagnostics={"kind": "orphan_recovery", "resolution": "assigned"},
    )
    skipped_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=recovered.id,
        file_path="/tmp/imports/Henchgirl bonus.pdf",
        file_name="Henchgirl bonus.pdf",
        file_size=10,
        file_format="pdf",
        status=ImportedFileStatus.SKIPPED,
        include_in_import=False,
        error_message="should clear",
        diagnostics={"kind": "orphan_recovery", "resolution": "skipped"},
    )
    db_session.add_all([assigned_file, skipped_file])
    await db_session.flush()

    await restore_review_state_after_rollback(db_session, job.id)

    assert recovered.status == ImportSeriesStatus.RECOVERY_PENDING
    assert recovered.series_id is None
    assert recovered.files_imported == 0
    assert recovered.files_failed == 0
    assert assigned_file.status == ImportedFileStatus.MATCHED
    assert assigned_file.library_file_id is None
    assert assigned_file.error_message is None
    assert skipped_file.status == ImportedFileStatus.SKIPPED
    assert skipped_file.library_file_id is None
    assert skipped_file.error_message is None
