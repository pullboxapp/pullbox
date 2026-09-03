"""Tests for import review summary count loading."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import event, select

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)


@pytest.mark.asyncio
async def test_load_import_review_summary_uses_persisted_review_rows(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    from pullbox.ui.import_review_summary import (
        load_import_review_summary,
        load_import_safety_failure_summary,
    )

    job = ImportJob(
        source_path="/tmp/import",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
    )
    db_session.add(job)
    await db_session.flush()

    matched = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Negation",
        status=ImportSeriesStatus.MATCHED,
        file_count=1,
        files_matched=1,
        selected_for_import=True,
    )
    no_match = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Mystery Book",
        status=ImportSeriesStatus.NO_MATCH,
        file_count=1,
    )
    series_conflict = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Crossed Annual",
        status=ImportSeriesStatus.NO_MATCH,
        file_count=1,
        diagnostics={"kind": "series_conflict"},
    )
    duplicate = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="In Library",
        status=ImportSeriesStatus.DUPLICATE,
        file_count=1,
        files_matched=1,
    )
    db_session.add_all([matched, no_match, series_conflict, duplicate])
    await db_session.flush()

    db_session.add_all(
        [
            ImportedFile(
                import_job_id=job.id,
                import_series_id=matched.id,
                file_path="/tmp/import/Negation 001.cbz",
                file_name="Negation 001.cbz",
                file_format="cbz",
                status=ImportedFileStatus.MATCHED,
            ),
            ImportedFile(
                import_job_id=job.id,
                import_series_id=no_match.id,
                file_path="/tmp/import/Mystery Book.cbz",
                file_name="Mystery Book.cbz",
                file_format="cbz",
                status=ImportedFileStatus.NO_MATCH,
            ),
            ImportedFile(
                import_job_id=job.id,
                import_series_id=series_conflict.id,
                file_path="/tmp/import/Crossed Annual.cbz",
                file_name="Crossed Annual.cbz",
                file_format="cbz",
                status=ImportedFileStatus.SAFETY_BLOCKED,
                diagnostics={
                    "safety_block": {
                        "kind": "archive_decompressed_size",
                        "reason": (
                            "Archive decompressed size exceeds limit at "
                            "/mnt/user/private/Crossed Annual.cbz"
                        ),
                        "details": ["/mnt/user/private/Crossed Annual.cbz"],
                        "overrideable": True,
                    }
                },
            ),
            ImportedFile(
                import_job_id=job.id,
                import_series_id=duplicate.id,
                file_path="/tmp/import/In Library 002.cbz",
                file_name="In Library 002.cbz",
                file_format="cbz",
                status=ImportedFileStatus.MATCHED,
            ),
        ]
    )
    await db_session.flush()

    summary = await load_import_review_summary(db_session, job)

    assert summary["series_total"] == 4
    assert summary["series_matched"] == 1
    assert summary["series_no_match"] == 1
    assert summary["series_candidate_conflicts"] == 1
    assert summary["series_conflicts_total"] == 1
    assert summary["files_total"] == 4
    assert summary["files_matched"] == 2
    assert summary["files_no_match"] == 1
    assert summary["files_safety_blocked"] == 1
    assert summary["matched_series_selected"] == 1
    assert summary["selected_items_total"] == 1
    assert summary["importable_items_total"] == 1
    assert summary["duplicate_files_importable"] == 1
    safety_summary = await load_import_safety_failure_summary(db_session, job, page_size=1)
    assert safety_summary == [
        {
            "category": "decompression_size_limit",
            "label": "Decompression-size limit",
            "count": 1,
            "codes": ["archive_decompressed_size_limit"],
            "reason": "The archive exceeds Pullbox's configured decompressed-size limit.",
            "retryable": False,
            "overrideable": True,
            "overrideable_count": 1,
            "bulk_overrideable_count": 1,
            "bulk_overrideable": True,
            "examples": ["Crossed Annual.cbz"],
        }
    ]
    assert "/mnt/user/private" not in str(safety_summary)


@pytest.mark.asyncio
async def test_load_import_review_summary_blends_live_counters_for_active_scan(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    from pullbox.ui.import_review_summary import load_import_review_summary

    job = ImportJob(
        source_path="/tmp/import",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.MATCHING,
        scan_total_files=42,
        series_found=8,
        series_matched=5,
        series_no_match=2,
        total_files_imported=3,
        total_files_failed=1,
    )
    db_session.add(job)
    await db_session.flush()

    summary = await load_import_review_summary(db_session, job)

    assert summary["series_total"] == 8
    assert summary["series_matched"] == 5
    assert summary["series_no_match"] == 2
    assert summary["files_total"] == 42
    assert summary["files_imported"] == 3
    assert summary["files_failed"] == 1


@pytest.mark.asyncio
async def test_load_import_safety_failure_summary_classifies_source_revalidation(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    from pullbox.ui.import_review_summary import load_import_safety_failure_summary

    job = ImportJob(
        source_path="/tmp/import",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
    )
    series = ImportedSeries(
        import_job=job,
        raw_series_name="Changed Source",
        status=ImportSeriesStatus.FAILED,
        file_count=1,
    )
    failed_file = ImportedFile(
        import_job=job,
        import_series=series,
        file_path="/mnt/private/Changed Source 001.cbz",
        file_name="Changed Source 001.cbz",
        file_format="cbz",
        status=ImportedFileStatus.FAILED,
        diagnostics={
            "source_revalidation": {
                "reason": "source_changed",
                "retryable": True,
            }
        },
    )
    db_session.add_all([job, series, failed_file])
    await db_session.flush()

    summary = await load_import_safety_failure_summary(db_session, job, page_size=1)

    assert summary == [
        {
            "category": "source_changed",
            "label": "Source changed or unavailable",
            "count": 1,
            "codes": ["source_changed"],
            "reason": (
                "The source changed or became unavailable after scanning. Rescan before retrying."
            ),
            "retryable": True,
            "overrideable": False,
            "overrideable_count": 0,
            "bulk_overrideable_count": 0,
            "bulk_overrideable": False,
            "examples": ["Changed Source 001.cbz"],
        }
    ]
    assert "/mnt/private" not in str(summary)


@pytest.mark.asyncio
async def test_bulk_overrideability_counts_only_current_safety_blocked_rows(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    from pullbox.ui.import_review_summary import load_import_safety_failure_summary

    job = ImportJob(
        source_path="/tmp/import",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
    )
    series = ImportedSeries(
        import_job=job,
        raw_series_name="Mixed Safety",
        status=ImportSeriesStatus.FAILED,
        file_count=2,
    )
    size_block = {
        "kind": "archive_decompressed_size",
        "reason": "Archive decompressed size exceeds limit",
        "overrideable": True,
    }
    db_session.add_all(
        [
            job,
            series,
            ImportedFile(
                import_job=job,
                import_series=series,
                file_path="/private/current.cbz",
                file_name="Current.cbz",
                file_format="cbz",
                status=ImportedFileStatus.SAFETY_BLOCKED,
                diagnostics={"safety_block": size_block},
            ),
            ImportedFile(
                import_job=job,
                import_series=series,
                file_path="/private/failed.cbz",
                file_name="Failed.cbz",
                file_format="cbz",
                status=ImportedFileStatus.FAILED,
                diagnostics={"safety_block": size_block},
            ),
        ]
    )
    await db_session.flush()

    summary = await load_import_safety_failure_summary(db_session, job, page_size=1)

    assert summary[0]["count"] == 2
    assert summary[0]["overrideable_count"] == 2
    assert summary[0]["bulk_overrideable_count"] == 1
    assert summary[0]["bulk_overrideable"] is True

    current = await db_session.scalar(
        select(ImportedFile).where(ImportedFile.file_name == "Current.cbz")
    )
    assert current is not None
    current.status = ImportedFileStatus.FAILED
    await db_session.flush()

    failed_only = await load_import_safety_failure_summary(db_session, job, page_size=1)
    assert failed_only[0]["bulk_overrideable_count"] == 0
    assert failed_only[0]["bulk_overrideable"] is False


@pytest.mark.asyncio
async def test_safety_summary_streams_complete_counts_in_one_query(
    db_session,
    async_engine,
) -> None:  # type: ignore[no-untyped-def]
    from pullbox.ui.import_review_summary import load_import_safety_failure_summary

    job = ImportJob(
        source_path="/tmp/import",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
    )
    other_job = ImportJob(
        source_path="/tmp/other",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
    )
    series = ImportedSeries(import_job=job, raw_series_name="Safety Review")
    other_series = ImportedSeries(import_job=other_job, raw_series_name="Other")
    size_block = {
        "kind": "archive_decompressed_size",
        "reason": "Archive decompressed size exceeds limit",
        "overrideable": True,
    }
    db_session.add_all([job, other_job, series, other_series])
    for index in range(9):
        db_session.add(
            ImportedFile(
                import_job=other_job if index == 8 else job,
                import_series=other_series if index == 8 else series,
                file_path=f"/private/{index}.cbz",
                file_name=f"{index}.cbz",
                file_format="cbz",
                status=(
                    ImportedFileStatus.FAILED
                    if index == 6
                    else ImportedFileStatus.MATCHED
                    if index == 7
                    else ImportedFileStatus.SAFETY_BLOCKED
                ),
                diagnostics={
                    "safety_block": size_block,
                    "source_revalidation": {"reason": "source_changed"},
                    "unrelated_metadata": {"description": "x" * 10_000},
                },
            )
        )
    await db_session.flush()

    queries = []
    fetch_sizes = []

    def record_select(_conn, _cursor, statement, _parameters, _context, _executemany):  # type: ignore[no-untyped-def]
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)
            fetch_sizes.append(_context.execution_options.get("yield_per"))

    event.listen(async_engine.sync_engine, "before_cursor_execute", record_select)
    try:
        summary = await load_import_safety_failure_summary(db_session, job, page_size=2)
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", record_select)

    assert len(summary) == 1
    assert summary[0]["count"] == 7
    assert summary[0]["overrideable_count"] == 7
    assert summary[0]["bulk_overrideable_count"] == 6
    assert summary[0]["examples"] == ["0.cbz", "1.cbz", "2.cbz"]
    assert len(queries) == 1
    assert fetch_sizes == [2]


@pytest.mark.asyncio
@pytest.mark.parametrize("safety_block", [None, "legacy", [], 3])
async def test_safety_summary_falls_back_to_source_revalidation(
    db_session,
    safety_block,
) -> None:  # type: ignore[no-untyped-def]
    from pullbox.ui.import_review_summary import load_import_safety_failure_summary

    job = ImportJob(
        source_path="/tmp/import",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
    )
    series = ImportedSeries(import_job=job, raw_series_name="Changed Source")
    db_session.add_all(
        [
            job,
            series,
            ImportedFile(
                import_job=job,
                import_series=series,
                file_path="/private/changed.cbz",
                file_name="changed.cbz",
                file_format="cbz",
                status=ImportedFileStatus.FAILED,
                diagnostics={
                    "safety_block": safety_block,
                    "source_revalidation": {"reason": "source_changed"},
                },
            ),
            ImportedFile(
                import_job=job,
                import_series=series,
                file_path="/private/empty.cbz",
                file_name="empty.cbz",
                file_format="cbz",
                status=ImportedFileStatus.FAILED,
                diagnostics=None,
            ),
        ]
    )
    await db_session.flush()

    summary = await load_import_safety_failure_summary(db_session, job, page_size=1)

    assert len(summary) == 1
    assert summary[0]["category"] == "source_changed"
    assert summary[0]["count"] == 1
    assert summary[0]["bulk_overrideable_count"] == 0


@pytest.mark.asyncio
async def test_safety_summary_closes_stream_on_cancellation(
    db_session,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    from pullbox.services.import_safety_diagnostics import ImportSafetyFailureSummaryAccumulator
    from pullbox.ui.import_review_summary import load_import_safety_failure_summary

    job = ImportJob(
        source_path="/tmp/import",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
    )
    series = ImportedSeries(import_job=job, raw_series_name="Changed Source")
    db_session.add_all(
        [
            job,
            series,
            ImportedFile(
                import_job=job,
                import_series=series,
                file_path="/private/changed.cbz",
                file_name="changed.cbz",
                file_format="cbz",
                status=ImportedFileStatus.FAILED,
                diagnostics={"source_revalidation": {"reason": "source_changed"}},
            ),
        ]
    )
    await db_session.flush()
    stream = db_session.stream
    results = []

    async def track_stream(*args, **kwargs):  # type: ignore[no-untyped-def]
        result = await stream(*args, **kwargs)
        results.append(result)
        return result

    def cancel(*_args):  # type: ignore[no-untyped-def]
        raise asyncio.CancelledError

    monkeypatch.setattr(db_session, "stream", track_stream)
    monkeypatch.setattr(ImportSafetyFailureSummaryAccumulator, "add", cancel)

    with pytest.raises(asyncio.CancelledError):
        await load_import_safety_failure_summary(db_session, job, page_size=1)

    assert len(results) == 1
    assert results[0].closed
