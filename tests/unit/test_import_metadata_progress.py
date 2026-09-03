"""Import-wide metadata counts and lifecycle use the shared activity contract."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select

from pullbox.api.v1.activity import operation_progress_read
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.operation_progress import OperationProgressState
from pullbox.models.series import IssueCatalogState, Series
from pullbox.services.import_metadata_progress import (
    build_import_metadata_progress,
    catalog_hydration_import_job_ids,
)
from pullbox.services.operation_activity import list_operation_activity
from pullbox.services.operation_progress import publish_operation_progress


@pytest.fixture(params=[ImportSourceType.FILESYSTEM, ImportSourceType.MYLAR3])
async def metadata_job(db_session, request):
    job = ImportJob(
        source_path="/imports", source_type=request.param, status=ImportJobStatus.COMPLETED
    )
    first = Series(
        title="Synced",
        sort_title="synced",
        comicvine_id=101,
        issue_catalog_state=IssueCatalogState.COMPLETE,
    )
    second = Series(
        title="Pending",
        sort_title="pending",
        comicvine_id=102,
        issue_catalog_state=IssueCatalogState.HYDRATING,
    )
    db_session.add_all([job, first, second])
    await db_session.flush()
    items = [
        ImportedSeries(
            import_job_id=job.id,
            series_id=series.id,
            raw_series_name=series.title,
            status=ImportSeriesStatus.IMPORTED,
        )
        for series in (first, second, second)
    ]
    db_session.add_all(items)
    await db_session.flush()
    for number, status in enumerate(["complete", "pending", None]):
        db_session.add(
            ImportedFile(
                import_job_id=job.id,
                import_series_id=items[number].id,
                file_path=f"/imports/{number}.cbz",
                file_name=f"{number}.cbz",
                file_format="cbz",
                status=ImportedFileStatus.IMPORTED,
                diagnostics={"comicinfo_enrichment": {"status": status}} if status else {},
            )
        )
    await db_session.commit()
    return job


async def test_metadata_progress_is_one_overall_summary_with_bounded_queries(
    db_session, metadata_job
):
    statements = []
    engine = db_session.bind.sync_engine

    def record(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        update = await build_import_metadata_progress(
            db_session, job_id=metadata_job.id, running=True
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(statements) <= 3
    assert update is not None
    assert update.overall.current == 2
    assert update.overall.total == 4  # Repeated series references are counted once.
    assert update.item is None
    assert update.state == OperationProgressState.RUNNING
    assert "Series catalogs: 1 of 2" in update.message
    assert "ComicInfo files: 1 of 2" in update.message
    result = await publish_operation_progress(db_session, update)
    await db_session.commit()
    await db_session.refresh(result.operation)
    response = operation_progress_read(result.operation)
    assert response.overall.percent == 50
    assert response.item is None
    activity = await list_operation_activity(db_session)
    assert activity.spinner_count == 1
    assert len(activity.operations) == 1


async def test_catalog_owner_lookup_failure_does_not_interrupt_metadata_work():
    def broken_factory():
        raise RuntimeError("database unavailable")

    assert await catalog_hydration_import_job_ids(broken_factory) == []


async def test_metadata_resume_keeps_durable_counts_and_clears_pause(db_session, metadata_job):
    paused = await build_import_metadata_progress(db_session, job_id=metadata_job.id, running=False)
    assert paused.state == OperationProgressState.PAUSED
    assert paused.attention_required
    await publish_operation_progress(db_session, paused)
    await db_session.commit()
    assert (await list_operation_activity(db_session)).spinner_count == 0
    resumed = await build_import_metadata_progress(db_session, job_id=metadata_job.id, running=True)
    assert resumed.overall == paused.overall
    result = await publish_operation_progress(db_session, resumed)
    await db_session.commit()
    assert not result.operation.attention_required
    assert (await list_operation_activity(db_session)).spinner_count == 1


@pytest.mark.parametrize("failure", [False, True])
async def test_metadata_terminal_summary_stops_spinning_and_expires_only_on_success(
    db_session,
    metadata_job,
    failure,
):
    series = await db_session.scalar(select(Series).where(Series.comicvine_id == 102))
    series.issue_catalog_state = IssueCatalogState.FAILED if failure else IssueCatalogState.COMPLETE
    files = list((await db_session.scalars(select(ImportedFile))).all())
    for file in files:
        if file.diagnostics:
            file.diagnostics = {"comicinfo_enrichment": {"status": "complete"}}
    await db_session.commit()
    update = await build_import_metadata_progress(db_session, job_id=metadata_job.id, running=False)
    assert update.state == (
        OperationProgressState.FAILED if failure else OperationProgressState.COMPLETED
    )
    result = await publish_operation_progress(db_session, update)
    await db_session.commit()
    assert result.operation.overall_percent == 100
    assert result.operation.attention_required is failure
    activity = await list_operation_activity(
        db_session, now=datetime.now(UTC) + timedelta(seconds=16)
    )
    assert activity.spinner_count == 0
    assert len(activity.operations) == int(failure)


async def test_rollback_stops_metadata_activity_without_reporting_completion(
    db_session, metadata_job
):
    update = await build_import_metadata_progress(db_session, job_id=metadata_job.id, running=True)
    await publish_operation_progress(db_session, update)
    metadata_job.status = ImportJobStatus.CANCELLED
    await db_session.commit()
    update = await build_import_metadata_progress(db_session, job_id=metadata_job.id, running=False)
    assert update.state == OperationProgressState.CANCELLED
    assert update.overall.percent is None
