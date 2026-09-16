"""Unit tests for import history context helpers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import (
    FileFormat,
    LibraryFile,
    LibraryFileStorageMode,
    LibraryRoot,
    MatchConfidence,
)
from pullbox.models.series import Series
from pullbox.ui.import_history import (
    _history_resume_step_for_job,
    _load_import_history_context,
    _normalize_import_history_sort,
)


def _job(
    *,
    status: ImportJobStatus,
    source_path: str = "/imports/source",
    import_started_at: datetime | None = None,
) -> ImportJob:
    return ImportJob(
        source_path=source_path,
        source_type=ImportSourceType.FILESYSTEM,
        status=status,
        import_started_at=import_started_at,
    )


def test_history_sort_normalization_keeps_known_fields_and_defaults_unknowns() -> None:
    assert _normalize_import_history_sort(None) == "-created_at"
    assert _normalize_import_history_sort("") == "-created_at"
    assert _normalize_import_history_sort("source_path") == "source_path"
    assert _normalize_import_history_sort("-status") == "-status"
    assert _normalize_import_history_sort("unsafe_field") == "-created_at"


@pytest.mark.parametrize(
    ("job", "expected_step"),
    [
        (_job(status=ImportJobStatus.REVIEW), 3),
        (_job(status=ImportJobStatus.PAUSED), 2),
        (
            _job(
                status=ImportJobStatus.PAUSED,
                import_started_at=datetime(2026, 6, 1, tzinfo=UTC),
            ),
            4,
        ),
        (_job(status=ImportJobStatus.MATCHING), 2),
        (_job(status=ImportJobStatus.IMPORTING), 4),
        (_job(status=ImportJobStatus.CANCELLING), 4),
        (_job(status=ImportJobStatus.COMPLETED), None),
    ],
)
def test_history_resume_steps_reflect_active_import_phase(
    job: ImportJob,
    expected_step: int | None,
) -> None:
    assert _history_resume_step_for_job(job) == expected_step


@pytest.mark.asyncio
async def test_import_history_context_sorts_paginates_and_restores_terminal_counts(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    terminal_job = ImportJob(
        source_path="/imports/page-00",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.COMPLETED,
        series_found=99,
        series_imported=99,
        series_failed=99,
        series_no_match=99,
    )
    db_session.add(terminal_job)
    await db_session.flush()
    db_session.add_all(
        [
            ImportedSeries(
                import_job_id=terminal_job.id,
                raw_series_name="Imported",
                status=ImportSeriesStatus.IMPORTED,
                file_count=1,
            ),
            ImportedSeries(
                import_job_id=terminal_job.id,
                raw_series_name="Failed",
                status=ImportSeriesStatus.FAILED,
                file_count=1,
            ),
            ImportedSeries(
                import_job_id=terminal_job.id,
                raw_series_name="No Match",
                status=ImportSeriesStatus.NO_MATCH,
                file_count=1,
            ),
        ]
    )
    for idx in range(1, 30):
        db_session.add(
            ImportJob(
                source_path=f"/imports/page-{idx:02d}",
                source_type=ImportSourceType.FILESYSTEM,
                status=ImportJobStatus.FAILED,
                series_found=idx,
                series_imported=idx,
                series_failed=0,
                series_no_match=0,
            )
        )
    await db_session.commit()

    context = await _load_import_history_context(
        db_session,
        sort="source_path",
        requested_page=2,
    )

    jobs = context["jobs"]
    assert len(jobs) == 5
    assert [job.source_path for job in jobs] == [  # type: ignore[attr-defined]
        "/imports/page-25",
        "/imports/page-26",
        "/imports/page-27",
        "/imports/page-28",
        "/imports/page-29",
    ]
    assert context["page"] == 2
    assert context["total_pages"] == 2
    assert context["clearable_jobs_total"] == 30

    first_page = await _load_import_history_context(
        db_session,
        sort="source_path",
        requested_page=1,
    )
    metrics = first_page["job_history_metrics"][terminal_job.id]  # type: ignore[index]
    assert metrics == {
        "series_found": 3,
        "series_imported": 1,
        "series_failed": 1,
        "series_no_match": 1,
    }


@pytest.mark.asyncio
async def test_import_history_context_filters_and_reports_live_summary(db_session) -> None:  # type: ignore[no-untyped-def]
    db_session.add_all(
        [
            ImportJob(
                source_path="/imports/needle-active",
                source_type=ImportSourceType.FILESYSTEM,
                status=ImportJobStatus.SCANNING,
            ),
            ImportJob(
                source_path="/imports/needle-paused",
                source_type=ImportSourceType.FILESYSTEM,
                status=ImportJobStatus.PAUSED,
            ),
            ImportJob(
                source_path="/imports/needle-completed",
                source_type=ImportSourceType.FILESYSTEM,
                status=ImportJobStatus.COMPLETED,
            ),
            ImportJob(
                source_path="/imports/other",
                source_type=ImportSourceType.FILESYSTEM,
                status=ImportJobStatus.FAILED,
            ),
        ]
    )
    await db_session.commit()

    context = await _load_import_history_context(
        db_session,
        search_query="needle",
        sort="-status",
        requested_page=10,
    )

    assert context["search_query"] == "needle"
    assert context["sort"] == "-status"
    assert context["page"] == 1
    assert context["total_jobs"] == 3
    assert context["history_has_live_jobs"] is True
    assert context["history_stats"] == {
        "active": 1,
        "resumable": 1,
        "results_ready": 1,
    }
    assert [job.source_path for job in context["jobs"]] == [  # type: ignore[index]
        "/imports/needle-paused",
        "/imports/needle-completed",
        "/imports/needle-active",
    ]


@pytest.mark.asyncio
async def test_import_history_only_offers_clean_library_for_completed_imports(
    db_session,
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    root = LibraryRoot(name="References", path=str(tmp_path))
    db_session.add(root)
    await db_session.flush()
    clean_library_job_ids: dict[ImportJobStatus, int] = {}

    for index, status in enumerate((ImportJobStatus.COMPLETED, ImportJobStatus.FAILED), start=1):
        job = ImportJob(
            source_path=f"/imports/reference-{index}",
            source_type=ImportSourceType.FILESYSTEM,
            status=status,
        )
        series = Series(title=f"Series {index}", sort_title=f"series {index}")
        db_session.add_all([job, series])
        await db_session.flush()
        issue = Issue(
            series_id=series.id,
            issue_number=1.0,
            issue_number_text="1",
            status=IssueStatus.OWNED,
        )
        imported_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name=series.title,
            status=ImportSeriesStatus.IMPORTED,
            series_id=series.id,
            file_count=1,
        )
        db_session.add_all([issue, imported_series])
        await db_session.flush()
        file_path = f"/imports/reference-{index}/issue.cbz"
        library_file = LibraryFile(
            file_path=file_path,
            file_name="issue.cbz",
            file_size=1024,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.HIGH,
            issue_id=issue.id,
            library_root_id=root.id,
            storage_mode=LibraryFileStorageMode.REFERENCED,
        )
        db_session.add(library_file)
        await db_session.flush()
        db_session.add(
            ImportedFile(
                import_job_id=job.id,
                import_series_id=imported_series.id,
                file_path=file_path,
                file_name="issue.cbz",
                file_size=1024,
                file_format="cbz",
                status=ImportedFileStatus.IMPORTED,
                matched_issue_id=issue.id,
                library_file_id=library_file.id,
            )
        )
        clean_library_job_ids[status] = job.id

    await db_session.commit()

    context = await _load_import_history_context(db_session)

    assert context["clean_library_job_ids"] == {clean_library_job_ids[ImportJobStatus.COMPLETED]}
