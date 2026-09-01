"""Tests for import review table context loading."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.library import LibraryRoot


@pytest.mark.asyncio
async def test_load_import_review_context_scopes_file_rows_to_requested_job(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    """A malformed/stale cross-job file row must not leak into Step 3."""
    from pullbox.ui.import_review_context import load_import_review_context

    job = ImportJob(
        source_path="/tmp/import-a",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
    )
    other_job = ImportJob(
        source_path="/tmp/import-b",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
    )
    db_session.add_all([job, other_job])
    await db_session.flush()

    series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Job A Series",
        status=ImportSeriesStatus.MATCHED,
        file_count=1,
        files_matched=1,
        selected_for_import=True,
    )
    db_session.add(series)
    await db_session.flush()

    db_session.add_all(
        [
            ImportedFile(
                import_job_id=job.id,
                import_series_id=series.id,
                file_path="/tmp/import-a/Job A Series 001.cbz",
                file_name="Job A Series 001.cbz",
                file_format="cbz",
                status=ImportedFileStatus.MATCHED,
            ),
            ImportedFile(
                import_job_id=other_job.id,
                import_series_id=series.id,
                file_path="/tmp/import-b/Foreign Match.cbz",
                file_name="Foreign Match.cbz",
                file_format="cbz",
                status=ImportedFileStatus.MATCHED,
            ),
            ImportedFile(
                import_job_id=other_job.id,
                import_series_id=series.id,
                file_path="/tmp/import-b/Foreign Safety Block.cbz",
                file_name="Foreign Safety Block.cbz",
                file_format="cbz",
                status=ImportedFileStatus.SAFETY_BLOCKED,
            ),
        ]
    )
    await db_session.flush()

    context = await load_import_review_context(
        db_session,
        job,
        status="matched",
        page=1,
        sort=None,
    )

    assert context["status_counts"].get("safety_blocked", 0) == 0
    assert context["safety_blocked_files_by_series_id"][series.id] == []
    matched_targets = context["matched_file_targets_by_series_id"][series.id]
    assert [target["file_name"] for target in matched_targets] == ["Job A Series 001.cbz"]
    matched_group = next(
        group
        for group in context["review_file_groups_by_series_id"][series.id]
        if group["key"] == "matched"
    )
    assert [row["file_name"] for row in matched_group["rows"]] == ["Job A Series 001.cbz"]


@pytest.mark.asyncio
async def test_safety_review_count_uses_job_series_index(db_session) -> None:  # type: ignore[no-untyped-def]
    """Keep the Step 3 safety count off a per-series full file-table scan."""
    from pullbox.ui.import_review_tables import _safety_blocked_files_filter

    statement = select(func.count(ImportedSeries.id)).where(
        ImportedSeries.import_job_id == 1,
        _safety_blocked_files_filter(),
    )
    dialect = db_session.get_bind().dialect
    sql = str(statement.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
    plan_rows = (await db_session.execute(text(f"EXPLAIN QUERY PLAN {sql}"))).all()
    plan = "\n".join(str(row[-1]) for row in plan_rows)

    assert "ix_import_files_job_series" in plan
    assert "SCAN import_files" not in plan


@pytest.mark.asyncio
async def test_load_import_review_context_filters_matched_rows_and_selection_state(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    from pullbox.ui.import_review_context import load_import_review_context

    root = LibraryRoot(name="Comics", path="/comics", enabled=True)
    job = ImportJob(
        source_path="/tmp/import",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
    )
    db_session.add_all([root, job])
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
    db_session.add_all([matched, no_match])
    await db_session.flush()

    db_session.add(
        ImportedFile(
            import_job_id=job.id,
            import_series_id=matched.id,
            file_path="/tmp/import/Negation 001.cbz",
            file_name="Negation 001.cbz",
            file_format="cbz",
            status=ImportedFileStatus.MATCHED,
        )
    )
    await db_session.flush()

    context = await load_import_review_context(
        db_session,
        job,
        status="matched",
        page=1,
        sort=None,
    )

    assert context["job"].id == job.id
    assert context["current_view"] == "matched"
    assert context["status_filter"] == "matched"
    assert context["sort"] == "confidence"
    assert context["total"] == 1
    assert [item.id for item in context["series_items"]] == [matched.id]
    assert [root.id for root in context["library_roots"]] == [root.id]
    assert context["selected_series_ids"] == [matched.id]
    assert context["status_counts"]["matched"] == 1
    assert context["status_counts"]["no_match"] == 1
    assert context["review_summary"]["selected_items_total"] == 1


@pytest.mark.asyncio
async def test_load_import_review_context_normalizes_legacy_safety_details_for_ui(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    from pullbox.ui.import_review_context import load_import_review_context

    job = ImportJob(
        source_path="/tmp/import",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
    )
    series = ImportedSeries(
        import_job=job,
        raw_series_name="Unsafe Archive",
        status=ImportSeriesStatus.NO_MATCH,
        file_count=1,
        diagnostics={"safety_blocked_files": 1},
    )
    blocked = ImportedFile(
        import_job=job,
        import_series=series,
        file_path="/mnt/user/private/Unsafe Archive.cbz",
        file_name="Unsafe Archive.cbz",
        file_format="cbz",
        status=ImportedFileStatus.SAFETY_BLOCKED,
        diagnostics={
            "safety_block": {
                "kind": "file_safety_blocked",
                "reason": "Archive contains path traversal at /mnt/user/private/secret",
                "details": ["/mnt/user/private/secret"],
                "overrideable": True,
            }
        },
    )
    db_session.add_all([job, series, blocked])
    await db_session.flush()

    context = await load_import_review_context(
        db_session,
        job,
        status="safety_blocked",
        page=1,
        sort=None,
    )

    safety_context = context["safety_block_context_by_file_id"][blocked.id]
    assert safety_context["category"] == "dangerous_path_or_payload"
    assert safety_context["code"] == "dangerous_archive_path"
    assert safety_context["retryable"] is False
    assert safety_context["overrideable"] is False
    assert "/mnt/user/private" not in str(safety_context)
    assert context["safety_failure_summary"][0]["count"] == 1
