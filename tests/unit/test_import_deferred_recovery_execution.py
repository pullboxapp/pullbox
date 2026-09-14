"""Recovery stages work without changing source files or reviving unrelated decisions."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event

from pullbox.core.exceptions import JobPausedError, ProviderError
from pullbox.models.import_job import (
    ImportedFileStatus,
    ImportedSeries,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.providers.base import IssueSummary, SeriesMetadata
from pullbox.services.catalog.reader import CatalogIssueSummary
from pullbox.services.import_deferred_recovery import (
    apply_deferred_recovery,
    plan_deferred_recovery,
)
from pullbox.services.import_deferred_recovery_execution import prepare_deferred_recovery
from tests.unit.test_import_deferred_recovery import add_file, register, seed


async def test_apply_is_repeatable_and_prepares_only_its_exact_scope(db_session):
    job, item, _, _, _ = await seed(db_session)
    file = await add_file(db_session, job, item)
    unrelated = await add_file(
        db_session, job, item, file_path="/comics/Batman/105.cbz", status=ImportedFileStatus.MATCHED
    )
    original_path = file.file_path
    counts = await apply_deferred_recovery(db_session, job)
    assert counts["exact_target"] == 1
    assert file.status is ImportedFileStatus.CONFIRMED
    assert file.import_series_id != item.id
    assert file.file_path == original_path
    assert unrelated.status is ImportedFileStatus.MATCHED
    assert unrelated.import_series_id == item.id
    assert (
        await db_session.get(ImportedSeries, file.import_series_id)
    ).status is ImportSeriesStatus.CONFIRMED
    assert await plan_deferred_recovery(db_session, job.id) == ()
    assert (await apply_deferred_recovery(db_session, job)).get("exact_target", 0) == 0


async def test_duplicate_reference_keeps_canonical_link_and_evidence(db_session):
    job, item, _, issue, root = await seed(db_session)
    file = await add_file(db_session, job, item)
    twin = await add_file(db_session, job, item)
    await register(db_session, file, issue, root)
    await apply_deferred_recovery(db_session, job)
    assert file.status is ImportedFileStatus.ALREADY_OWNED
    assert twin.status is ImportedFileStatus.SKIPPED
    assert twin.duplicate_of_file_id == file.id
    assert twin.diagnostics["deferred_recovery"]["source_preserved"]
    assert job.total_files_no_match == 0


async def test_only_stale_series_without_any_file_records_are_archived(db_session):
    job, item, _, _, _ = await seed(db_session)
    item.status = ImportSeriesStatus.NO_MATCH
    item.diagnostics = {"reason": "path_missing"}
    stale = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Stale",
        status=ImportSeriesStatus.NO_MATCH,
        diagnostics={"reason": "path_missing"},
    )
    db_session.add(stale)
    await add_file(
        db_session,
        job,
        item,
        comicvine_issue_id=None,
        parsed_series="Unidentified",
        file_name="Unidentified.cbz",
        parsed_issue_number=None,
    )
    result = await apply_deferred_recovery(db_session, job)
    assert result["stale_series"] == 1
    assert stale.status is ImportSeriesStatus.SKIPPED
    assert item.status is ImportSeriesStatus.NO_MATCH


async def test_distinct_unowned_candidates_are_not_silently_selected(db_session):
    job, item, _, _, _ = await seed(db_session)
    await add_file(db_session, job, item)
    await add_file(db_session, job, item, file_path="/comics/Batman/104 alternate.cbz")
    assert await plan_deferred_recovery(db_session, job.id) == ()


async def test_background_recovery_fetches_each_candidate_catalog_once_and_resumes(db_session):
    job, item, _, _, _ = await seed(db_session)
    item.series_id = None
    item.cv_id = None
    item.status = ImportSeriesStatus.NO_MATCH
    file = await add_file(
        db_session,
        job,
        item,
        comicvine_issue_id=7001,
        diagnostics={
            "comicvine_series_id": 700,
            "metadata_signals": {"comicvine_series_id": "mylar3"},
        },
    )
    twin = await add_file(
        db_session, job, item, comicvine_issue_id=7001, diagnostics=file.diagnostics
    )
    job.status = ImportJobStatus.IMPORTING
    job.progress_snapshot = {"deferred_recovery": {"state": "queued"}}
    provider = AsyncMock()
    provider.get_series_metadata.return_value = SeriesMetadata(
        provider_id="700",
        title="Batman",
        year_start=2016,
        issue_count=1,
        sort_title="batman",
        year_end=None,
        status=None,
        publisher=None,
        description=None,
        cover_url=None,
        comicvine_url=None,
    )
    provider.get_issue_summaries_for_series.return_value = [
        IssueSummary(
            provider_id="7001",
            issue_number=104,
            issue_number_text="104",
            title=None,
            release_date="2021-01-01",
            cover_url=None,
            issue_type="issue",
        )
    ]

    assert await prepare_deferred_recovery(db_session, job.id, metadata_service=provider)
    assert file.status is ImportedFileStatus.CONFIRMED
    assert twin.status is ImportedFileStatus.SKIPPED
    assert file.diagnostics["target_issue_summary"]["provider_id"] == "7001"
    target = await db_session.get(ImportedSeries, file.import_series_id)
    assert target.cv_id == 700
    assert item.status is ImportSeriesStatus.SKIPPED
    assert file.file_path == twin.file_path
    provider.get_issue_summaries_for_series.assert_awaited_once_with(700)
    provider.get_series_metadata.assert_awaited_once_with(700)
    assert not await prepare_deferred_recovery(db_session, job.id, metadata_service=provider)
    assert provider.get_issue_summaries_for_series.await_count == 1


async def test_catalog_checkpoint_serializes_local_catalog_cutoff(db_session):
    job, item, _, _, _ = await seed(db_session)
    item.series_id = None
    item.cv_id = None
    item.status = ImportSeriesStatus.NO_MATCH
    await add_file(
        db_session,
        job,
        item,
        comicvine_issue_id=7001,
        diagnostics={
            "comicvine_series_id": 700,
            "metadata_signals": {"comicvine_series_id": "mylar3"},
        },
    )
    job.status = ImportJobStatus.IMPORTING
    job.progress_snapshot = {"deferred_recovery": {"state": "queued"}}
    provider = AsyncMock()
    provider.get_series_metadata.return_value = SeriesMetadata(
        provider_id="700",
        title="Batman",
        sort_title="batman",
        year_start=2016,
        year_end=None,
        status=None,
        publisher=None,
        description=None,
        cover_url=None,
        issue_count=1,
        comicvine_url=None,
    )
    provider.get_issue_summaries_for_series.return_value = [
        CatalogIssueSummary(
            provider_id="7001",
            issue_number=104,
            issue_number_text="104",
            title=None,
            release_date="2021-01-01",
            cover_url=None,
            issue_type="issue",
            source_cutoff_at=datetime(2026, 9, 13, 5, tzinfo=UTC),
        )
    ]

    assert await prepare_deferred_recovery(db_session, job.id, metadata_service=provider)

    stored = job.progress_snapshot["deferred_recovery"]["matches"]["7001"][0]["summary"]
    assert stored["source_cutoff_at"] == "2026-09-13T05:00:00+00:00"
    await db_session.commit()


async def test_catalog_recovery_does_not_write_checkpoint_before_provider_io(db_session):
    job, _item, _, _, _ = await seed(db_session)
    job.status = ImportJobStatus.IMPORTING
    job.progress_snapshot = {
        "deferred_recovery": {
            "state": "catalogs",
            "candidates": {"700": [7001]},
            "completed": [],
            "matches": {},
            "series_ids": [],
        }
    }
    await db_session.commit()

    updates: list[str] = []
    engine = db_session.bind.sync_engine

    def record_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("UPDATE IMPORT_JOBS"):
            updates.append(statement)

    event.listen(engine, "before_cursor_execute", record_statement)
    provider = AsyncMock()

    async def get_series(_cv_id):
        assert updates == []
        return SeriesMetadata(
            provider_id="700",
            title="Batman",
            sort_title="batman",
            year_start=2016,
            year_end=None,
            status=None,
            publisher=None,
            description=None,
            cover_url=None,
            issue_count=0,
            comicvine_url=None,
        )

    provider.get_series_metadata.side_effect = get_series
    provider.get_issue_summaries_for_series.return_value = []
    try:
        assert await prepare_deferred_recovery(db_session, job.id, metadata_service=provider)
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)


async def test_catalog_target_respects_strong_archive_number_evidence(db_session):
    from pullbox.services.import_deferred_recovery_execution import _catalog_target_agrees

    job, item, _, _, _ = await seed(db_session)
    file = await add_file(
        db_session,
        job,
        item,
        diagnostics={
            "source_metadata": {
                "archive_entry_issue_hint": {"confidence": "strong", "issue_number": 105}
            }
        },
    )
    assert not _catalog_target_agrees(
        file,
        item,
        {
            "title": "Batman",
            "summary": {"provider_id": "1001", "issue_number": 104, "issue_type": "issue"},
        },
    )


async def test_local_background_recovery_requires_no_provider_calls(db_session):
    job, item, _, _, _ = await seed(db_session)
    file = await add_file(db_session, job, item)
    job.status = ImportJobStatus.IMPORTING
    job.progress_snapshot = {"deferred_recovery": {"state": "queued"}}
    provider = AsyncMock()
    assert await prepare_deferred_recovery(db_session, job.id, metadata_service=provider)
    assert file.status is ImportedFileStatus.CONFIRMED
    provider.get_series_metadata.assert_not_awaited()
    provider.get_issue_summaries_for_series.assert_not_awaited()


async def test_catalog_failure_checkpoints_without_repeating_completed_requests(db_session):
    job, item, _, _, _ = await seed(db_session)
    item.series_id = None
    item.status = ImportSeriesStatus.NO_MATCH
    await add_file(
        db_session,
        job,
        item,
        comicvine_issue_id=7001,
        diagnostics={
            "comicvine_series_id": 700,
            "metadata_signals": {"comicvine_series_id": "mylar3"},
            "source_metadata": {
                "identity_conflicts": [
                    {"field": "comicvine_series_id", "first": 700, "conflicting": 800},
                ]
            },
        },
    )
    job.status = ImportJobStatus.IMPORTING
    job.progress_snapshot = {"deferred_recovery": {"state": "queued"}}
    provider = AsyncMock()

    async def get_series(cv_id):
        assert not db_session.in_transaction()
        return SeriesMetadata(
            provider_id=str(cv_id),
            title="Batman",
            sort_title="batman",
            year_start=2016,
            year_end=None,
            status=None,
            publisher=None,
            description=None,
            cover_url=None,
            issue_count=0,
            comicvine_url=None,
        )

    provider.get_series_metadata.side_effect = get_series
    provider.get_issue_summaries_for_series.side_effect = [
        [],
        ProviderError("comicvine", "offline"),
    ]
    with pytest.raises(JobPausedError):
        await prepare_deferred_recovery(db_session, job.id, metadata_service=provider)
    assert job.progress_snapshot["deferred_recovery"]["completed"] == ["700"]
    provider.get_issue_summaries_for_series.side_effect = None
    provider.get_issue_summaries_for_series.return_value = []
    await prepare_deferred_recovery(db_session, job.id, metadata_service=provider)
    assert [call.args[0] for call in provider.get_issue_summaries_for_series.await_args_list] == [
        700,
        800,
        800,
    ]
    assert job.status is ImportJobStatus.COMPLETED


async def test_preparation_cancel_preserves_original_import(db_session):
    from pullbox.services.import_deferred_recovery_execution import cancel_deferred_preparation

    job, item, _, _, _ = await seed(db_session)
    original = await add_file(db_session, job, item, status=ImportedFileStatus.IMPORTED)
    file = await add_file(db_session, job, item, file_path="/comics/Batman/other.cbz")
    job.progress_snapshot = {"deferred_recovery": {"state": "queued"}}
    await apply_deferred_recovery(db_session, job)
    job.status = ImportJobStatus.CANCELLING
    assert await cancel_deferred_preparation(db_session, job)
    assert job.status is ImportJobStatus.COMPLETED
    assert original.status is ImportedFileStatus.IMPORTED
    assert file.status is ImportedFileStatus.NO_MATCH


@pytest.mark.parametrize("started", [True, False])
async def test_paused_recovery_cancel_never_requests_original_rollback(db_session, started):
    from datetime import UTC, datetime

    from pullbox.services.import_job_controls import request_cancel

    job, _, _, _, _ = await seed(db_session)
    job.status = ImportJobStatus.PAUSED
    job.import_started_at = datetime.now(UTC) if started else None
    job.progress_snapshot = {"deferred_recovery": {"state": "catalogs"}}
    await db_session.commit()
    await request_cancel(db_session, job.id, log_event=AsyncMock())
    assert job.status is ImportJobStatus.CANCELLING


async def test_recovery_execution_leaves_unrelated_ready_groups_and_arcs_untouched(
    db_session, monkeypatch
):
    from pullbox.models.import_job import ImportFileHandlingMode
    from pullbox.services import import_job_execution as execution

    job, item, _, _, _ = await seed(db_session)
    job.file_handling_mode = ImportFileHandlingMode.IN_PLACE
    job.move_to_library = False
    file = await add_file(db_session, job, item)
    await apply_deferred_recovery(db_session, job)
    unrelated = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Not selected",
        cv_id=999,
        status=ImportSeriesStatus.CONFIRMED,
    )
    db_session.add(unrelated)
    await db_session.flush()
    await add_file(
        db_session,
        job,
        unrelated,
        status=ImportedFileStatus.CONFIRMED,
        file_path="/comics/other.cbz",
    )
    job.progress_snapshot = {
        **job.progress_snapshot,
        "deferred_recovery": {**job.progress_snapshot["deferred_recovery"], "state": "prepared"},
    }
    execute_group = AsyncMock(return_value=(1, 0, 1, 0, True))
    arcs = AsyncMock(side_effect=AssertionError("Unrelated story arcs must not execute"))
    monkeypatch.setattr(execution, "_execute_new_series", execute_group)
    monkeypatch.setattr(execution, "_execute_story_arc_materialization", arcs)
    await execution.execute_import_job(
        db_session,
        job.id,
        series_service=AsyncMock(),
        process_series_files=AsyncMock(),
        raise_if_cancelled=AsyncMock(),
        record_action=AsyncMock(),
        log_event=AsyncMock(),
        emit_progress=AsyncMock(),
        estimate_remaining_seconds=lambda *a, **kw: None,
        maybe_slow_item_delay=AsyncMock(),
    )
    assert execute_group.await_count == 1
    assert execute_group.await_args.args[2].id == file.import_series_id
    assert unrelated.status is ImportSeriesStatus.CONFIRMED
    assert job.progress_snapshot["deferred_recovery"]["state"] == "completed"
    assert job.status is ImportJobStatus.COMPLETED


async def test_copy_capacity_is_scoped_to_recovery_files(db_session):
    from pullbox.services.import_managed_copy_preflight import selected_managed_copy_source_bytes

    job, item, _, _, _ = await seed(db_session)
    await add_file(db_session, job, item)
    await apply_deferred_recovery(db_session, job)
    item.status = ImportSeriesStatus.CONFIRMED
    await add_file(
        db_session,
        job,
        item,
        file_path="/comics/unrelated.cbz",
        status=ImportedFileStatus.CONFIRMED,
        file_size=99999,
    )
    job.progress_snapshot = {
        **job.progress_snapshot,
        "deferred_recovery": {**job.progress_snapshot["deferred_recovery"], "state": "prepared"},
    }
    assert await selected_managed_copy_source_bytes(db_session, job.id) == 1024


async def test_cleanup_preview_counts_physical_paths_and_queues_without_inline_work(db_session):
    from pullbox.models.import_job import ImportFileHandlingMode
    from pullbox.models.user import User
    from pullbox.services.import_completed_cleanup import (
        CompletedImportCleanupAction,
        apply_completed_import_cleanup,
        preview_completed_import_cleanup,
        summarize_completed_import_cleanup_scope,
    )

    db_session.add(User(id=42, username="recovery-test", password_hash="unused"))
    job, item, _, _, _ = await seed(db_session)
    job.file_handling_mode = ImportFileHandlingMode.IN_PLACE
    job.move_to_library = False
    files = [await add_file(db_session, job, item), await add_file(db_session, job, item)]
    await db_session.commit()
    action = CompletedImportCleanupAction.RECHECK_DEFERRED_FILES
    preview = await preview_completed_import_cleanup(db_session, job.id, action, actor_id=42)
    assert preview.affected_count == 1
    assert preview.affected_file_count == 2
    summary = await summarize_completed_import_cleanup_scope(db_session, job.id, action)
    assert summary.affected_count == 1
    result = await apply_completed_import_cleanup(
        db_session, job.id, action, actor_id=42, preview_token=preview.preview_token
    )
    assert result.requires_import_retry
    assert job.status is ImportJobStatus.IMPORTING
    assert job.progress_snapshot["deferred_recovery"]["state"] == "queued"
    assert all(file.status is ImportedFileStatus.NO_MATCH for file in files)


async def test_cleanup_preview_signs_and_bounds_empty_stale_series(db_session):
    from pullbox.models.import_job import ImportFileHandlingMode
    from pullbox.models.user import User
    from pullbox.services.import_completed_cleanup import (
        CompletedImportCleanupAction,
        apply_completed_import_cleanup,
        preview_completed_import_cleanup,
    )

    db_session.add(User(id=42, username="recovery-test", password_hash="unused"))
    job, item, _, _, _ = await seed(db_session)
    job.file_handling_mode = ImportFileHandlingMode.IN_PLACE
    job.move_to_library = False
    await add_file(db_session, job, item)
    stale = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Missing before preview",
        status=ImportSeriesStatus.NO_MATCH,
        diagnostics={"reason": "path_missing"},
    )
    db_session.add(stale)
    await db_session.commit()

    action = CompletedImportCleanupAction.RECHECK_DEFERRED_FILES
    preview = await preview_completed_import_cleanup(db_session, job.id, action, actor_id=42)

    assert preview.affected_count == 2
    assert preview.affected_file_count == 1
    result = await apply_completed_import_cleanup(
        db_session, job.id, action, actor_id=42, preview_token=preview.preview_token
    )
    assert result.affected_count == 2
    assert job.progress_snapshot["deferred_recovery"]["stale_series_ids"] == [stale.id]

    late_stale = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Missing after preview",
        status=ImportSeriesStatus.NO_MATCH,
        diagnostics={"reason": "source_missing"},
    )
    db_session.add(late_stale)
    await db_session.flush()

    await apply_deferred_recovery(db_session, job, running=True)

    assert stale.status is ImportSeriesStatus.SKIPPED
    assert late_stale.status is ImportSeriesStatus.NO_MATCH


async def test_active_import_is_not_changed_by_offline_recovery(db_session):
    job, item, _, _, _ = await seed(db_session)
    item.status = ImportSeriesStatus.NO_MATCH
    item.diagnostics = {"reason": "path_missing"}
    job.status = ImportJobStatus.IMPORTING
    assert await apply_deferred_recovery(db_session, job) == {}
    assert item.status is ImportSeriesStatus.NO_MATCH
