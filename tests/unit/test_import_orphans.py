"""Tests for import orphaned-series helper shims."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from pullbox.core.exceptions import ProviderError, ValidationError
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
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import LibraryRoot
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.providers.base import IssueSummary, SeriesMetadata
from pullbox.schemas.import_job import OrphanRecoveryDecision, RecoverOrphanRequest
from pullbox.services.import_service import ImportService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _make_service(
    series_service: AsyncMock | None = None,
    metadata_service: AsyncMock | None = None,
) -> ImportService:
    return ImportService(
        series_service=series_service or AsyncMock(),
        metadata_service=metadata_service or AsyncMock(),
        event_bus=AsyncMock(),
    )


async def _create_job_row(
    session: AsyncSession,
    *,
    status: ImportJobStatus = ImportJobStatus.COMPLETED,
    series_failed: int = 0,
) -> ImportJob:
    job = ImportJob(
        source_path="/tmp/comics",
        source_type=ImportSourceType.FILESYSTEM,
        status=status,
        series_failed=series_failed,
    )
    session.add(job)
    await session.flush()
    return job


async def _create_imported_series(
    session: AsyncSession,
    job: ImportJob,
    *,
    name: str,
    status: ImportSeriesStatus = ImportSeriesStatus.NO_MATCH,
    file_count: int = 1,
) -> ImportedSeries:
    series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name=name,
        raw_year=2025,
        status=status,
        file_count=file_count,
        files_total=file_count,
        error_message="retry me" if status == ImportSeriesStatus.FAILED else None,
    )
    session.add(series)
    await session.flush()
    return series


async def _create_series_with_issue(
    session: AsyncSession,
    *,
    series_id: int,
    issue_id: int,
    title: str,
    issue_number: float,
) -> None:
    series = Series(
        id=series_id,
        title=title,
        sort_title=title.lower(),
        year_start=2025,
        status=SeriesStatus.CONTINUING,
        series_type=SeriesType.STANDARD,
    )
    issue = Issue(
        id=issue_id,
        series=series,
        issue_number=issue_number,
        title=f"#{issue_number:g}",
        issue_type=IssueType.ISSUE,
        status=IssueStatus.SKIPPED,
    )
    session.add_all([series, issue])
    await session.flush()


def test_apply_orphan_recovery_decisions_assigns_issue() -> None:
    from pullbox.services.import_orphans import apply_orphan_recovery_decisions

    item = ImportedSeries(id=7, import_job_id=3, raw_series_name="Recovered")
    imp_file = ImportedFile(
        id=9,
        import_job_id=3,
        import_series_id=7,
        file_path="/imports/recovered.cbz",
        file_name="recovered.cbz",
        status=ImportedFileStatus.NO_MATCH,
        diagnostics={"existing": "kept"},
    )
    issue = Issue(id=42, series_id=5, issue_number=4.0, comicvine_id=1116296)

    apply_orphan_recovery_decisions(
        item=item,
        files=[imp_file],
        decisions=[
            OrphanRecoveryDecision(
                imported_file_id=9,
                action="assign",
                issue_cv_id=1116296,
            )
        ],
        cv_id_to_issue={1116296: issue},
    )

    assert imp_file.status == ImportedFileStatus.MATCHED
    assert imp_file.matched_issue_id == 42
    assert imp_file.matched_issue_cv_id == 1116296
    assert imp_file.match_confidence == "manual"
    assert imp_file.match_method == "orphan_recovery"
    assert imp_file.error_message is None
    assert imp_file.diagnostics["existing"] == "kept"
    assert imp_file.diagnostics["resolution"] == "assigned"


def test_apply_orphan_recovery_decisions_skips_file() -> None:
    from pullbox.services.import_orphans import apply_orphan_recovery_decisions

    item = ImportedSeries(id=7, import_job_id=3, raw_series_name="Recovered")
    imp_file = ImportedFile(
        id=9,
        import_job_id=3,
        import_series_id=7,
        file_path="/imports/recovered.cbz",
        file_name="recovered.cbz",
        status=ImportedFileStatus.NO_MATCH,
        matched_issue_id=42,
        matched_issue_cv_id=1116296,
        diagnostics={"existing": "kept"},
    )

    apply_orphan_recovery_decisions(
        item=item,
        files=[imp_file],
        decisions=[OrphanRecoveryDecision(imported_file_id=9, action="skip")],
        cv_id_to_issue={},
    )

    assert imp_file.status == ImportedFileStatus.SKIPPED
    assert imp_file.matched_issue_id is None
    assert imp_file.matched_issue_cv_id is None
    assert imp_file.match_confidence is None
    assert imp_file.match_method == "orphan_recovery_skip"
    assert imp_file.error_message is None
    assert imp_file.diagnostics["existing"] == "kept"
    assert imp_file.diagnostics["resolution"] == "skipped"


def test_summarize_orphan_recovery_marks_imported_when_no_files_remaining() -> None:
    from pullbox.services.import_orphans import summarize_orphan_recovery_result

    item = ImportedSeries(
        id=7,
        import_job_id=3,
        raw_series_name="Recovered",
        series_id=12,
        status=ImportSeriesStatus.RECOVERY_PENDING,
    )
    files = [
        ImportedFile(status=ImportedFileStatus.IMPORTED),
        ImportedFile(status=ImportedFileStatus.SKIPPED),
    ]

    summary = summarize_orphan_recovery_result(item=item, files=files)

    assert item.status == ImportSeriesStatus.IMPORTED
    assert summary == {
        "imported_series_id": 7,
        "status": ImportSeriesStatus.IMPORTED,
        "series_id": 12,
        "imported_count": 1,
        "skipped_count": 1,
        "failed_count": 0,
        "files_remaining": 0,
    }


def test_summarize_orphan_recovery_keeps_pending_when_work_remains() -> None:
    from pullbox.services.import_orphans import summarize_orphan_recovery_result

    item = ImportedSeries(
        id=7,
        import_job_id=3,
        raw_series_name="Recovered",
        series_id=12,
        status=ImportSeriesStatus.RECOVERY_PENDING,
    )
    files = [
        ImportedFile(status=ImportedFileStatus.IMPORTED),
        ImportedFile(status=ImportedFileStatus.FAILED),
    ]

    summary = summarize_orphan_recovery_result(item=item, files=files)

    assert item.status == ImportSeriesStatus.RECOVERY_PENDING
    assert summary["imported_count"] == 1
    assert summary["failed_count"] == 1
    assert summary["files_remaining"] == 1


async def test_get_orphaned_series_filters_completed_no_match_rows(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    completed = await _create_job_row(db_session)
    cancelled = await _create_job_row(db_session, status=ImportJobStatus.CANCELLED)
    await _create_imported_series(db_session, completed, name="Big", file_count=20)
    await _create_imported_series(db_session, completed, name="Small", file_count=2)
    await _create_imported_series(
        db_session,
        completed,
        name="Identified",
        status=ImportSeriesStatus.RECOVERY_PENDING,
    )
    await _create_imported_series(
        db_session,
        completed,
        name="Dismissed",
        status=ImportSeriesStatus.SKIPPED,
    )
    await _create_imported_series(db_session, cancelled, name="Cancelled")

    items, total = await service.get_orphaned_series(db_session)
    count = await service.get_orphaned_count(db_session)

    assert total == 3
    assert count == 3
    assert [item.raw_series_name for item in items] == ["Big", "Small", "Identified"]


async def test_get_orphaned_series_includes_imported_rows_with_issue_recovery_left(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    imported = await _create_imported_series(
        db_session,
        job,
        name="Absolute Martian Manhunter",
        status=ImportSeriesStatus.IMPORTED,
        file_count=11,
    )
    imported.files_imported = 10
    imported.files_no_match = 1
    db_session.add(
        ImportedFile(
            import_job_id=job.id,
            import_series_id=imported.id,
            file_path="/tmp/amm-vol1.cbz",
            file_name="Absolute Martian Manhunter Vol 01.cbz",
            file_size=1024,
            file_format="cbz",
            status=ImportedFileStatus.NO_MATCH,
        )
    )
    await db_session.flush()

    items, total = await service.get_orphaned_series(db_session)
    count = await service.get_orphaned_count(db_session)

    assert total == 1
    assert count == 1
    assert [item.raw_series_name for item in items] == ["Absolute Martian Manhunter"]


async def test_dismiss_orphan_can_skip_unresolved_files_under_imported_series(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    imported = await _create_imported_series(
        db_session,
        job,
        name="Henchgirl",
        status=ImportSeriesStatus.IMPORTED,
    )
    imported.files_imported = 1
    imported.files_no_match = 1
    imp_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=imported.id,
        file_path="/tmp/henchgirl-expanded.pdf",
        file_name="Henchgirl expanded.pdf",
        file_size=1024,
        file_format="pdf",
        status=ImportedFileStatus.NO_MATCH,
    )
    db_session.add(imp_file)
    await db_session.flush()

    await service.dismiss_orphan(db_session, imported.id)

    assert imported.status == ImportSeriesStatus.IMPORTED
    assert imp_file.status == ImportedFileStatus.SKIPPED
    assert imported.files_no_match == 0


async def test_assign_cv_to_orphan_starts_recovery_and_logs_event(
    db_session: AsyncSession,
) -> None:
    metadata_service = AsyncMock()
    metadata_service.get_series_metadata.return_value = SeriesMetadata(
        provider_id="162966",
        title="Absolute Wonder Woman",
        sort_title="absolute wonder woman",
        year_start=2024,
        year_end=None,
        status="Continuing",
        publisher="DC Comics",
        description=None,
        cover_url=None,
        issue_count=12,
        comicvine_url="https://comicvine.gamespot.com/absolute-wonder-woman/4050-162966/",
    )
    service = _make_service(metadata_service=metadata_service)
    job = await _create_job_row(db_session)
    orphan = await _create_imported_series(db_session, job, name="Absolute Wonder Woman")

    result = await service.assign_cv_to_orphan(db_session, orphan.id, 162966)

    assert result.id == orphan.id
    assert orphan.status == ImportSeriesStatus.RECOVERY_PENDING
    assert orphan.series_id is None
    assert orphan.cv_id == 162966
    assert orphan.cv_title == "Absolute Wonder Woman"
    assert orphan.cv_publisher == "DC Comics"
    log = (
        await db_session.execute(
            select(ImportJobLog).where(
                ImportJobLog.import_job_id == job.id,
                ImportJobLog.event == "import_orphan_recovery_started",
            )
        )
    ).scalar_one()
    assert log.data["cv_title"] == "Absolute Wonder Woman"


async def test_assign_cv_to_orphan_failure_keeps_no_match_and_logs_event(
    db_session: AsyncSession,
) -> None:
    metadata_service = AsyncMock()
    metadata_service.get_series_metadata.side_effect = ProviderError("comicvine", "not found")
    service = _make_service(metadata_service=metadata_service)
    job = await _create_job_row(db_session)
    orphan = await _create_imported_series(db_session, job, name="Mystery Box")

    with pytest.raises(ProviderError):
        await service.assign_cv_to_orphan(db_session, orphan.id, 99999)

    assert orphan.status == ImportSeriesStatus.NO_MATCH
    log = (
        await db_session.execute(
            select(ImportJobLog).where(
                ImportJobLog.import_job_id == job.id,
                ImportJobLog.event == "import_orphan_assign_failed",
            )
        )
    ).scalar_one()
    assert log.data["cv_id"] == 99999


async def test_dismiss_orphan_allows_recovery_pending_rows(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    orphan = await _create_imported_series(
        db_session,
        job,
        name="Recovery Candidate",
        status=ImportSeriesStatus.RECOVERY_PENDING,
    )

    await service.dismiss_orphan(db_session, orphan.id)

    assert orphan.status == ImportSeriesStatus.SKIPPED


async def test_get_orphan_recovery_context_prefers_issue_id_then_unique_issue_number(
    db_session: AsyncSession,
) -> None:
    metadata_service = AsyncMock()
    metadata_service.get_issue_summaries_for_series.return_value = [
        IssueSummary(
            provider_id="501",
            issue_number=1.0,
            title="Issue One",
            release_date="2024-01-01",
            cover_url=None,
            issue_type="issue",
        ),
        IssueSummary(
            provider_id="502",
            issue_number=2.0,
            title="Issue Two",
            release_date="2024-02-01",
            cover_url=None,
            issue_type="issue",
        ),
    ]
    service = _make_service(metadata_service=metadata_service)
    job = await _create_job_row(db_session)
    orphan = await _create_imported_series(
        db_session,
        job,
        name="Absolute Wonder Woman",
        status=ImportSeriesStatus.RECOVERY_PENDING,
        file_count=2,
    )
    orphan.cv_id = 162966
    orphan.cv_title = "Absolute Wonder Woman"
    db_session.add_all(
        [
            ImportedFile(
                import_job_id=job.id,
                import_series_id=orphan.id,
                file_path="/tmp/aww-001.cbz",
                file_name="AWW 001.cbz",
                file_size=1024,
                file_format="cbz",
                comicvine_issue_id=501,
                parsed_issue_number=9.0,
                status=ImportedFileStatus.PENDING,
            ),
            ImportedFile(
                import_job_id=job.id,
                import_series_id=orphan.id,
                file_path="/tmp/aww-002.cbz",
                file_name="AWW 002.cbz",
                file_size=1024,
                file_format="cbz",
                parsed_issue_number=2.0,
                status=ImportedFileStatus.PENDING,
            ),
        ]
    )
    await db_session.flush()

    payload = await service.get_orphan_recovery_context(db_session, orphan.id)

    assert payload["requires_library_root"] is True
    assert payload["files_remaining"] == 2
    assert payload["files"][0]["suggested_issue_cv_id"] == 501
    assert payload["files"][1]["suggested_issue_cv_id"] == 502


async def test_get_orphan_recovery_context_allows_imported_rows_with_unresolved_files(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    root = LibraryRoot(name="Root", path="/tmp/library", enabled=True)
    db_session.add(root)
    await db_session.flush()

    from pullbox.models.issue import Issue
    from pullbox.models.series import Series, SeriesStatus

    series = Series(
        title="Absolute Martian Manhunter",
        sort_title="absolute martian manhunter",
        year_start=2025,
        comicvine_id=168590,
        status=SeriesStatus.CONTINUING,
        issue_count=1,
        library_root_id=root.id,
        path="/tmp/library/Absolute Martian Manhunter (2025)",
    )
    db_session.add(series)
    await db_session.flush()
    db_session.add(
        Issue(
            series_id=series.id,
            issue_number=1.0,
            title="Vol. 1: Martian Vision",
            comicvine_id=1144216,
        )
    )

    orphan = await _create_imported_series(
        db_session,
        job,
        name="Absolute Martian Manhunter",
        status=ImportSeriesStatus.IMPORTED,
    )
    orphan.cv_id = 168590
    orphan.cv_title = "Absolute Martian Manhunter"
    orphan.series_id = series.id
    orphan.files_imported = 10
    orphan.files_no_match = 1
    db_session.add(
        ImportedFile(
            import_job_id=job.id,
            import_series_id=orphan.id,
            file_path="/tmp/amm-vol-01.cbz",
            file_name="Absolute Martian Manhunter Vol 01.cbz",
            file_size=1024,
            file_format="cbz",
            status=ImportedFileStatus.NO_MATCH,
            comicvine_issue_id=1144216,
        )
    )
    await db_session.flush()

    payload = await service.get_orphan_recovery_context(db_session, orphan.id)

    assert payload["imported_series"].id == orphan.id
    assert payload["requires_library_root"] is False
    assert payload["files_remaining"] == 1
    assert payload["issue_options"][0]["issue_cv_id"] == 1144216


async def test_recover_orphan_refreshes_job_after_file_processing_commits(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    series = Series(
        title="Henchgirl",
        sort_title="henchgirl",
        year_start=2020,
        comicvine_id=130322,
        status=SeriesStatus.CONTINUING,
        series_type=SeriesType.TPB,
    )
    db_session.add(series)
    await db_session.flush()
    issue = Issue(
        comicvine_id=799319,
        series_id=series.id,
        issue_number=1.0,
        title="Expanded Edition",
        issue_type=IssueType.TPB,
        status=IssueStatus.SKIPPED,
    )
    db_session.add(issue)
    await db_session.flush()

    orphan = await _create_imported_series(
        db_session,
        job,
        name="Henchgirl expanded Edition Comic",
        status=ImportSeriesStatus.IMPORTED,
    )
    orphan.cv_id = 130322
    orphan.cv_title = "Henchgirl"
    orphan.series_id = series.id
    orphan.files_imported = 0
    orphan.files_no_match = 1
    imp_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=orphan.id,
        file_path="/tmp/henchgirl-expanded.pdf",
        file_name="Henchgirl expanded.pdf",
        file_size=1024,
        file_format="pdf",
        status=ImportedFileStatus.NO_MATCH,
    )
    db_session.add(imp_file)
    await db_session.flush()

    async def _commit_during_processing(
        session: AsyncSession,
        _job: ImportJob,
        current_item: ImportedSeries,
        *,
        duplicate_mode: bool = False,
        series_id_override: int | None = None,
        report_file_progress=None,
    ) -> tuple[int, int]:
        del duplicate_mode, series_id_override, report_file_progress
        result = await session.execute(
            select(ImportedFile).where(ImportedFile.import_series_id == current_item.id)
        )
        target_file = result.scalar_one()
        target_file.status = ImportedFileStatus.IMPORTED
        target_file.matched_issue_id = issue.id
        target_file.matched_issue_cv_id = issue.comicvine_id
        await session.commit()
        return 1, 0

    service._process_series_files = _commit_during_processing

    payload = await service.recover_orphan(
        db_session,
        orphan.id,
        RecoverOrphanRequest(
            decisions=[
                {
                    "imported_file_id": imp_file.id,
                    "action": "assign",
                    "issue_cv_id": 799319,
                }
            ]
        ),
    )

    refreshed_job = await db_session.get(ImportJob, job.id)
    refreshed_orphan = await db_session.get(ImportedSeries, orphan.id)
    refreshed_file = await db_session.get(ImportedFile, imp_file.id)

    assert payload["status"] == ImportSeriesStatus.IMPORTED
    assert payload["imported_count"] == 1
    assert refreshed_job is not None and refreshed_job.total_files_imported == 1
    assert refreshed_orphan is not None and refreshed_orphan.files_imported == 1
    assert refreshed_file is not None and refreshed_file.status == ImportedFileStatus.IMPORTED


async def test_get_orphan_recovery_context_defaults_library_root_when_available(
    db_session: AsyncSession,
) -> None:
    metadata_service = AsyncMock()
    metadata_service.get_issue_summaries_for_series.return_value = []
    service = _make_service(metadata_service=metadata_service)
    job = await _create_job_row(db_session)
    db_session.add(LibraryRoot(name="Main", path="/library/main", enabled=True))
    await db_session.flush()
    orphan = await _create_imported_series(
        db_session,
        job,
        name="Persephone",
        status=ImportSeriesStatus.RECOVERY_PENDING,
    )
    orphan.cv_id = 12345

    payload = await service.get_orphan_recovery_context(db_session, orphan.id)

    assert payload["requires_library_root"] is True
    assert payload["selected_library_root_id"] is not None
    assert payload["available_library_roots"][0]["name"] == "Main"


async def test_recover_orphan_requires_library_root_when_job_has_none(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    orphan = await _create_imported_series(
        db_session,
        job,
        name="Henchgirl",
        status=ImportSeriesStatus.RECOVERY_PENDING,
    )
    orphan.cv_id = 12345
    imp_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=orphan.id,
        file_path="/tmp/henchgirl-001.cbz",
        file_name="Henchgirl 001.cbz",
        file_size=1024,
        file_format="cbz",
        status=ImportedFileStatus.PENDING,
    )
    db_session.add(imp_file)
    await db_session.flush()

    with pytest.raises(ValidationError, match="Choose a library root"):
        await service.recover_orphan(
            db_session,
            orphan.id,
            RecoverOrphanRequest(
                decisions=[
                    {
                        "imported_file_id": imp_file.id,
                        "action": "skip",
                    }
                ]
            ),
        )


async def test_recover_orphan_helper_requires_assign_without_existing_import(
    db_session: AsyncSession,
) -> None:
    from pullbox.services.import_orphans import recover_orphan

    job = await _create_job_row(db_session)
    root = LibraryRoot(name="Root", path="/tmp/library", enabled=True)
    db_session.add(root)
    await db_session.flush()
    job.target_library_root_id = root.id
    orphan = await _create_imported_series(
        db_session,
        job,
        name="Henchgirl",
        status=ImportSeriesStatus.RECOVERY_PENDING,
    )
    orphan.cv_id = 12345
    imp_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=orphan.id,
        file_path="/tmp/henchgirl-001.cbz",
        file_name="Henchgirl 001.cbz",
        file_size=1024,
        file_format="cbz",
        status=ImportedFileStatus.PENDING,
    )
    db_session.add(imp_file)
    await db_session.flush()

    with pytest.raises(ValidationError, match="Assign at least one file"):
        await recover_orphan(
            db_session,
            job,
            orphan,
            RecoverOrphanRequest(
                decisions=[
                    {
                        "imported_file_id": imp_file.id,
                        "action": "skip",
                    }
                ]
            ),
            series_service=AsyncMock(),
            process_series_files=AsyncMock(),
            record_action=AsyncMock(),
            recompute_file_counters=AsyncMock(),
            recompute_series_counters=AsyncMock(),
            log_event=AsyncMock(),
        )


async def test_retry_failed_series_resets_rows_and_job_counter(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session, series_failed=2)
    failed_a = await _create_imported_series(
        db_session,
        job,
        name="Failed A",
        status=ImportSeriesStatus.FAILED,
    )
    failed_b = await _create_imported_series(
        db_session,
        job,
        name="Failed B",
        status=ImportSeriesStatus.FAILED,
    )
    imported = await _create_imported_series(
        db_session,
        job,
        name="Imported",
        status=ImportSeriesStatus.IMPORTED,
    )
    failed_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=failed_a.id,
        file_path="/tmp/failed-a-001.cbz",
        file_name="Failed A 001.cbz",
        file_size=1024,
        file_format="cbz",
        status=ImportedFileStatus.FAILED,
        parsed_issue_number=1.0,
        match_confidence="high",
        match_method="issue_number",
        error_message="boom",
    )
    db_session.add(failed_file)
    await db_session.flush()

    updated_job, count = await service.retry_failed_series(db_session, job.id)

    assert count == 2
    assert updated_job.status == ImportJobStatus.IMPORTING
    assert updated_job.series_failed == 0
    assert failed_a.status == ImportSeriesStatus.CONFIRMED
    assert failed_a.error_message is None
    assert failed_b.status == ImportSeriesStatus.CONFIRMED
    assert imported.status == ImportSeriesStatus.IMPORTED
    await db_session.refresh(failed_file)
    assert failed_file.status == ImportedFileStatus.CONFIRMED
    assert failed_file.error_message is None


async def test_retry_failed_series_repairs_confirmed_file_target_summary(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session, series_failed=1)
    failed = await _create_imported_series(
        db_session,
        job,
        name="The Amazing Spider-Man",
        status=ImportSeriesStatus.FAILED,
    )
    imp_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=failed.id,
        file_path="/tmp/The Amazing Spider-Man 54.LR.cbz",
        file_name="The Amazing Spider-Man 54.LR.cbz",
        file_size=1024,
        file_format="cbz",
        status=ImportedFileStatus.CONFIRMED,
        parsed_issue_number=54.0,
        issue_number_raw="54.LR",
        matched_issue_cv_id=123456,
        match_confidence="high",
        match_method="comicvine_id",
        diagnostics={"source_issue_type": "issue"},
    )
    db_session.add(imp_file)
    await db_session.flush()

    updated_job, count = await service.retry_failed_series(db_session, job.id)

    assert count == 1
    assert updated_job.status == ImportJobStatus.IMPORTING
    assert imp_file.status == ImportedFileStatus.CONFIRMED
    assert imp_file.diagnostics["target_issue_summary"] == {
        "provider_id": "123456",
        "issue_number": 54.0,
        "title": None,
        "release_date": None,
        "cover_url": None,
        "issue_type": "issue",
        "issue_number_text": "54LR",
    }


async def test_retry_failed_series_resets_duplicate_file_failures(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    duplicate = await _create_imported_series(
        db_session,
        job,
        name="2000AD",
        status=ImportSeriesStatus.DUPLICATE,
    )
    await _create_series_with_issue(
        db_session,
        series_id=160,
        issue_id=3218,
        title="2000AD",
        issue_number=2481.0,
    )
    duplicate.series_id = 160
    duplicate.files_failed = 1
    failed_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=duplicate.id,
        file_path="/tmp/2000AD prog 2481.cbz",
        file_name="2000AD prog 2481.cbz",
        file_size=1024,
        file_format="cbz",
        status=ImportedFileStatus.FAILED,
        include_in_import=True,
        parsed_issue_number=2481.0,
        matched_issue_id=3218,
        match_confidence="medium",
        match_method="issue_number",
        error_message="QueuePool timeout",
    )
    db_session.add(failed_file)
    await db_session.flush()

    updated_job, count = await service.retry_failed_series(db_session, job.id)

    assert count == 1
    assert updated_job.status == ImportJobStatus.IMPORTING
    assert updated_job.total_files_failed == 0
    assert duplicate.status == ImportSeriesStatus.DUPLICATE
    assert duplicate.files_failed == 0
    assert duplicate.files_matched == 1
    await db_session.refresh(failed_file)
    assert failed_file.status == ImportedFileStatus.CONFIRMED
    assert failed_file.include_in_import is True
    assert failed_file.error_message is None


async def test_retry_failed_series_resets_partial_import_file_failures(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    imported = await _create_imported_series(
        db_session,
        job,
        name="Partially Imported",
        status=ImportSeriesStatus.IMPORTED,
    )
    await _create_series_with_issue(
        db_session,
        series_id=321,
        issue_id=322,
        title="Partially Imported",
        issue_number=2.0,
    )
    imported.series_id = 321
    imported.files_imported = 1
    imported.files_failed = 1
    imported_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=imported.id,
        file_path="/tmp/imported-001.cbz",
        file_name="imported-001.cbz",
        file_size=1024,
        file_format="cbz",
        status=ImportedFileStatus.IMPORTED,
    )
    failed_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=imported.id,
        file_path="/tmp/imported-002.cbz",
        file_name="imported-002.cbz",
        file_size=1024,
        file_format="cbz",
        status=ImportedFileStatus.FAILED,
        include_in_import=True,
        parsed_issue_number=2.0,
        matched_issue_id=322,
        match_confidence="high",
        match_method="issue_number",
        error_message="temporary failure",
    )
    db_session.add_all([imported_file, failed_file])
    await db_session.flush()

    updated_job, count = await service.retry_failed_series(db_session, job.id)

    assert count == 1
    assert updated_job.status == ImportJobStatus.IMPORTING
    assert updated_job.total_files_failed == 0
    assert imported.status == ImportSeriesStatus.CONFIRMED
    assert imported.files_imported == 1
    assert imported.files_matched == 1
    await db_session.refresh(failed_file)
    assert failed_file.status == ImportedFileStatus.CONFIRMED
    assert failed_file.error_message is None


async def test_retry_failed_series_rejects_jobs_without_failures(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)

    with pytest.raises(ValidationError, match="No failed"):
        await service.retry_failed_series(db_session, job.id)
