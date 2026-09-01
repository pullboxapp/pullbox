"""Step 4 integrates logical story arcs after canonical file processing."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

import pullbox.services.import_job_execution as execution_module
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobAction,
    ImportJobActionStatus,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryRoot
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    ImportedStoryArcStatus,
    IssueStoryArc,
    StoryArc,
    StoryArcExternalIdentity,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.services import library_root_management
from pullbox.services.import_job_actions import record_action, rollback_action
from pullbox.services.import_job_execution import execute_import_job
from pullbox.services.import_story_arc_materialization import StoryArcMaterializationResult
from pullbox.services.import_story_arc_placement_completion import (
    ImportStoryArcPlacementCounts,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


class _UnusedSeriesService:
    """Arc-only jobs never invoke a metadata provider service."""


@pytest.fixture
async def managed_import_root(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> LibraryRoot:
    """Provide the managed destination required by direct job fixtures."""
    root_path = tmp_path / "story-arc-execution-library"
    root_path.mkdir()
    root = LibraryRoot(
        name="Story arc execution library",
        path=str(root_path),
        enabled=True,
        allow_referenced_registrations=True,
        allow_managed_writes=True,
        is_default_managed_destination=True,
    )
    db_session.add(root)
    await db_session.flush()
    monkeypatch.setattr(
        library_root_management.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=100 * 1024**3),
    )
    return root


async def _execute(
    session: AsyncSession,
    job: ImportJob,
    *,
    process_series_files: object | None = None,
    log_event: AsyncMock | None = None,
    emit_progress: object | None = None,
    progress_callback: AsyncMock | None = None,
) -> AsyncMock:
    logs = log_event or AsyncMock()
    await execute_import_job(
        session,
        job.id,
        series_service=_UnusedSeriesService(),  # type: ignore[arg-type]
        process_series_files=(
            process_series_files  # type: ignore[arg-type]
            if process_series_files is not None
            else AsyncMock()
        ),
        raise_if_cancelled=AsyncMock(),
        record_action=record_action,
        log_event=logs,
        emit_progress=(emit_progress if emit_progress is not None else AsyncMock()),  # type: ignore[arg-type]
        estimate_remaining_seconds=lambda *_args: None,
        maybe_slow_item_delay=AsyncMock(),
        progress_callback=progress_callback,
    )
    return logs


@pytest.mark.asyncio
async def test_arc_only_execution_preserves_million_issue_and_missing_order(
    db_session: AsyncSession,
    managed_import_root: LibraryRoot,
) -> None:
    job = ImportJob(
        source_path="/private/mylar/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.REVIEW,
        target_library_root_id=managed_import_root.id,
    )
    db_session.add(job)
    await db_session.flush()
    staged = ImportedStoryArc(
        import_job_id=job.id,
        source_kind=StoryArcSourceKind.MYLAR3,
        source_key="mylar3:arc-only",
        source_arc_id="arc-only",
        source_ordinal=1,
        name="Arc Only",
        status=ImportedStoryArcStatus.CONFIRMED,
        selected_for_import=True,
    )
    db_session.add(staged)
    await db_session.flush()
    entry = ImportedStoryArcEntry(
        imported_story_arc_id=staged.id,
        source_ordinal=17,
        reading_order=None,
        reading_order_raw=None,
        source_kind=StoryArcSourceKind.MYLAR3,
        source_entry_id="entry-million",
        source_arc_id=staged.source_arc_id,
        source_issue_number_text="1000000",
        resolution_state=StoryArcResolutionState.MISSING,
        selected_for_import=True,
    )
    db_session.add(entry)
    await db_session.flush()

    logs = await _execute(db_session, job)

    membership = (await db_session.scalars(select(IssueStoryArc))).one()
    actions = list(
        (
            await db_session.scalars(select(ImportJobAction).order_by(ImportJobAction.sequence_no))
        ).all()
    )
    assert job.status == ImportJobStatus.COMPLETED
    assert staged.status == ImportedStoryArcStatus.IMPORTED
    assert membership.issue_id is None
    assert membership.source_issue_number_text == "1000000"
    assert membership.sequence_number == 17
    assert membership.legacy_sequence_was_null is True
    assert membership.resolution_state == StoryArcResolutionState.MISSING
    assert [action.action_type for action in actions] == [
        "story_arc_created",
        "story_arc_external_identity_created",
        "story_arc_membership_created",
    ]
    completion = next(
        call
        for call in logs.await_args_list
        if len(call.args) >= 4 and call.args[3] == "story_arc_materialization_completed"
    )
    assert completion.kwargs["arcs_examined"] == 1
    assert completion.kwargs["memberships_created"] == 1
    assert "source_path" not in completion.kwargs


@pytest.mark.asyncio
async def test_normal_exception_after_durable_arc_page_fails_closed_without_losing_provenance(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = ImportJob(
        source_path="/private/source/should-not-be-persisted",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.IMPORTING,
    )
    db_session.add(job)
    await db_session.flush()
    staged = ImportedStoryArc(
        import_job_id=job.id,
        source_kind=StoryArcSourceKind.MYLAR3,
        source_key="mylar3:durable-exception",
        source_arc_id="durable-exception",
        source_ordinal=1,
        name="Durable Exception",
        status=ImportedStoryArcStatus.CONFIRMED,
        selected_for_import=True,
    )
    db_session.add(staged)
    await db_session.flush()
    entry = ImportedStoryArcEntry(
        imported_story_arc_id=staged.id,
        source_ordinal=1,
        reading_order=1,
        reading_order_raw="1",
        source_kind=StoryArcSourceKind.MYLAR3,
        source_entry_id="durable-exception-entry",
        source_arc_id=staged.source_arc_id,
        source_issue_number_text="1",
        resolution_state=StoryArcResolutionState.MISSING,
        selected_for_import=True,
    )
    db_session.add(entry)
    await db_session.flush()
    logs = AsyncMock()
    real_commit = db_session.commit
    durable_action_page_committed = False

    async def track_durable_action_commit() -> None:
        nonlocal durable_action_page_committed
        action_count = int(
            await db_session.scalar(
                select(func.count())
                .select_from(ImportJobAction)
                .where(ImportJobAction.import_job_id == job.id)
            )
            or 0
        )
        await real_commit()
        if action_count:
            durable_action_page_committed = True

    monkeypatch.setattr(db_session, "commit", track_durable_action_commit)

    async def fail_after_first_durable_action_page(
        current_session: AsyncSession,
        current_job_id: int,
    ) -> None:
        assert current_session is db_session
        assert current_job_id == job.id
        if durable_action_page_committed:
            raise RuntimeError("/private/source/should-not-be-persisted")

    with pytest.raises(RuntimeError, match="should-not-be-persisted"):
        await execution_module._execute_story_arc_materialization(
            db_session,
            job,
            job_id=int(job.id),
            raise_if_cancelled=fail_after_first_durable_action_page,
            record_action=record_action,
            record_actions=None,
            log_event=logs,
            emit_progress=AsyncMock(),
            estimate_remaining_seconds=lambda *_args: None,
            progress_callback=None,
            runtime_revision_state={"value": 0},
            job_started_at=None,
        )

    await db_session.refresh(job)
    await db_session.refresh(staged)
    await db_session.refresh(entry)
    actions = list(
        (
            await db_session.scalars(
                select(ImportJobAction)
                .where(ImportJobAction.import_job_id == job.id)
                .order_by(ImportJobAction.sequence_no.desc())
            )
        ).all()
    )
    assert job.status is ImportJobStatus.FAILED
    assert job.import_completed_at is None
    assert job.error_message == "Story-arc registration failed; canonical files remain imported."
    assert job.progress_snapshot["status"] == ImportJobStatus.FAILED.value
    assert job.progress_snapshot["phase"] == "story_arcs"
    assert "/private" not in str(job.progress_snapshot)
    assert staged.status is ImportedStoryArcStatus.CONFIRMED
    assert staged.materialized_story_arc_id is not None
    assert entry.materialized_membership_id is not None
    assert await db_session.scalar(select(func.count()).select_from(StoryArc)) == 1
    assert await db_session.scalar(select(func.count()).select_from(IssueStoryArc)) == 1
    assert [action.action_type for action in reversed(actions)] == [
        "story_arc_created",
        "story_arc_external_identity_created",
        "story_arc_membership_created",
    ]
    assert all(action.status is ImportJobActionStatus.COMPLETED for action in actions)
    failure = next(
        call
        for call in logs.await_args_list
        if len(call.args) >= 4 and call.args[3] == "story_arc_materialization_failed"
    )
    assert failure.kwargs["failure_type"] == "RuntimeError"
    assert "/private" not in failure.kwargs["message"]

    delete_series = AsyncMock()
    for action in actions:
        await rollback_action(
            db_session,
            action_id=int(action.id),
            action_type=action.action_type,
            payload=dict(action.payload or {}),
            delete_series=delete_series,
        )
    await db_session.flush()

    assert await db_session.scalar(select(func.count()).select_from(IssueStoryArc)) == 0
    assert await db_session.scalar(select(func.count()).select_from(StoryArc)) == 0
    delete_series.assert_not_awaited()


@pytest.mark.asyncio
async def test_expected_per_arc_validation_failure_remains_nonfatal(
    db_session: AsyncSession,
    managed_import_root: LibraryRoot,
) -> None:
    job = ImportJob(
        source_path="/private/source",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.REVIEW,
        target_library_root_id=managed_import_root.id,
    )
    existing = StoryArc(name="Existing Arc")
    db_session.add_all([job, existing])
    await db_session.flush()
    db_session.add(
        StoryArcExternalIdentity(
            story_arc_id=existing.id,
            source="mylar3",
            namespace="story_arc",
            external_id="existing-arc",
        )
    )
    staged = ImportedStoryArc(
        import_job_id=job.id,
        source_kind=StoryArcSourceKind.MYLAR3,
        source_key="mylar3:expected-validation-failure",
        source_arc_id="existing-arc",
        source_ordinal=1,
        name="Implicit Merge Is Rejected",
        status=ImportedStoryArcStatus.CONFIRMED,
        selected_for_import=True,
    )
    db_session.add(staged)
    await db_session.flush()

    logs = await _execute(db_session, job)

    await db_session.refresh(job)
    await db_session.refresh(staged)
    assert job.status is ImportJobStatus.COMPLETED
    assert staged.status is ImportedStoryArcStatus.FAILED
    assert staged.materialized_story_arc_id is None
    assert job.error_message == (
        "Some story arcs could not be registered; canonical files remain imported."
    )
    completion = next(
        call
        for call in logs.await_args_list
        if len(call.args) >= 4 and call.args[3] == "story_arc_materialization_completed"
    )
    assert completion.args[2] == "WARNING"
    assert completion.kwargs["arcs_failed"] == 1


@pytest.mark.asyncio
async def test_post_file_resolution_links_new_canonical_issue(
    db_session: AsyncSession,
    managed_import_root: LibraryRoot,
) -> None:
    job = ImportJob(
        source_path="/private/source",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
        target_library_root_id=managed_import_root.id,
    )
    series = Series(title="Late Resolution", sort_title="late resolution")
    db_session.add_all([job, series])
    await db_session.flush()
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Late Resolution",
        status=ImportSeriesStatus.DUPLICATE,
        series_id=series.id,
        file_count=1,
    )
    db_session.add(imported_series)
    await db_session.flush()
    imported_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=imported_series.id,
        file_path="/private/source/Late Resolution 1000000.cbz",
        file_name="Late Resolution 1000000.cbz",
        file_size=100,
        file_format="cbz",
        status=ImportedFileStatus.MATCHED,
        include_in_import=True,
    )
    db_session.add(imported_file)
    await db_session.flush()
    staged = ImportedStoryArc(
        import_job_id=job.id,
        source_kind=StoryArcSourceKind.FOLDER,
        source_key="folder:late-resolution",
        source_ordinal=1,
        name="Late Resolution Arc",
        status=ImportedStoryArcStatus.CONFIRMED,
        selected_for_import=True,
    )
    db_session.add(staged)
    await db_session.flush()
    entry = ImportedStoryArcEntry(
        imported_story_arc_id=staged.id,
        import_file_id=imported_file.id,
        source_ordinal=1,
        reading_order=1_000_000,
        reading_order_raw="1000000",
        source_kind=StoryArcSourceKind.FOLDER,
        source_entry_id="late-entry",
        source_issue_number_text="1000000",
        resolution_state=StoryArcResolutionState.PENDING,
        selected_for_import=True,
    )
    db_session.add(entry)
    await db_session.flush()
    created_issue_id: int | None = None

    async def process_files(
        session: AsyncSession,
        _job: ImportJob,
        _item: ImportedSeries,
        **_kwargs: object,
    ) -> tuple[int, int]:
        nonlocal created_issue_id
        issue = Issue(
            series_id=series.id,
            issue_number=1_000_000.0,
            issue_number_text="1000000",
        )
        session.add(issue)
        await session.flush()
        created_issue_id = issue.id
        persisted_file = await session.get(ImportedFile, imported_file.id)
        assert persisted_file is not None
        persisted_file.matched_issue_id = issue.id
        persisted_file.status = ImportedFileStatus.IMPORTED
        return 1, 0

    await _execute(db_session, job, process_series_files=process_files)

    membership = (await db_session.scalars(select(IssueStoryArc))).one()
    assert created_issue_id is not None
    assert entry.matched_issue_id == created_issue_id
    assert entry.resolution_state == StoryArcResolutionState.RESOLVED
    assert membership.issue_id == created_issue_id
    assert membership.sequence_number == 1_000_000
    assert membership.source_issue_number_text == "1000000"


@pytest.mark.asyncio
async def test_materialization_failure_keeps_committed_canonical_registration(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    managed_import_root: LibraryRoot,
) -> None:
    job = ImportJob(
        source_path="/private/source/should-not-leak",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
        target_library_root_id=managed_import_root.id,
    )
    series = Series(title="Canonical Survives", sort_title="canonical survives")
    db_session.add_all([job, series])
    await db_session.flush()
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Canonical Survives",
        status=ImportSeriesStatus.DUPLICATE,
        series_id=series.id,
        file_count=1,
    )
    db_session.add(imported_series)
    await db_session.flush()
    imported_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=imported_series.id,
        file_path="/private/source/should-not-leak/Issue.cbz",
        file_name="Issue.cbz",
        file_size=100,
        file_format="cbz",
        status=ImportedFileStatus.MATCHED,
        include_in_import=True,
    )
    staged = ImportedStoryArc(
        import_job_id=job.id,
        source_kind=StoryArcSourceKind.FOLDER,
        source_key="folder:failing",
        source_ordinal=1,
        name="Failing Arc",
        status=ImportedStoryArcStatus.CONFIRMED,
        selected_for_import=True,
    )
    db_session.add_all([imported_file, staged])
    await db_session.flush()

    async def process_files(
        session: AsyncSession,
        _job: ImportJob,
        _item: ImportedSeries,
        **_kwargs: object,
    ) -> tuple[int, int]:
        issue = Issue(series_id=series.id, issue_number=1.0, issue_number_text="1")
        session.add(issue)
        await session.flush()
        imported_file.matched_issue_id = issue.id
        imported_file.status = ImportedFileStatus.IMPORTED
        return 1, 0

    async def fail_materialization(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("/private/source/should-not-leak")

    monkeypatch.setattr(
        execution_module,
        "materialize_confirmed_story_arcs",
        fail_materialization,
        raising=False,
    )
    logs = AsyncMock()
    with pytest.raises(RuntimeError, match="should-not-leak"):
        await _execute(
            db_session,
            job,
            process_series_files=process_files,
            log_event=logs,
        )

    await db_session.refresh(job)
    await db_session.refresh(staged)
    assert job.status == ImportJobStatus.FAILED
    assert job.total_files_imported == 1
    assert job.error_message == "Story-arc registration failed; canonical files remain imported."
    assert job.progress_snapshot["status"] == ImportJobStatus.FAILED.value
    assert "/private" not in str(job.progress_snapshot)
    assert staged.status == ImportedStoryArcStatus.CONFIRMED
    assert staged.materialized_story_arc_id is None
    assert await db_session.scalar(select(func.count()).select_from(Issue)) == 1
    assert await db_session.scalar(select(func.count()).select_from(StoryArc)) == 0
    failure = next(
        call
        for call in logs.await_args_list
        if len(call.args) >= 4 and call.args[3] == "story_arc_materialization_failed"
    )
    assert failure.kwargs["failure_type"] == "RuntimeError"
    assert "/private" not in failure.kwargs["message"]
    assert all(
        len(call.args) < 4 or call.args[3] != "import_completed" for call in logs.await_args_list
    )


@pytest.mark.asyncio
async def test_managed_placement_work_keeps_import_in_truthful_pending_phase(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    managed_import_root: LibraryRoot,
) -> None:
    job = ImportJob(
        source_path="/private/source",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
        target_library_root_id=managed_import_root.id,
    )
    db_session.add(job)
    await db_session.flush()

    async def queue_managed_placements(*_args: object, **_kwargs: object):
        return StoryArcMaterializationResult(
            arcs_examined=1,
            arcs_created=1,
            memberships_created=2,
            resolved_entries=2,
            managed_placements_queued=2,
        )

    monkeypatch.setattr(
        execution_module,
        "materialize_confirmed_story_arcs",
        queue_managed_placements,
    )
    monkeypatch.setattr(
        execution_module,
        "seal_import_story_arc_placement_origin",
        AsyncMock(return_value=ImportStoryArcPlacementCounts(queued=2)),
    )
    emitted_events: list[dict[str, object]] = []

    async def persist_emitted_progress(
        current_session: AsyncSession,
        current_job: ImportJob,
        event: object,
        _callback: object,
    ) -> None:
        payload = event.model_dump(mode="json")  # type: ignore[attr-defined]
        emitted_events.append(payload)
        current_job.progress_snapshot = payload
        await current_session.commit()

    logs = await _execute(
        db_session,
        job,
        emit_progress=persist_emitted_progress,
        progress_callback=AsyncMock(),
    )

    assert job.status is ImportJobStatus.IMPORTING
    assert job.import_completed_at is None
    assert job.progress_snapshot["phase"] == "story_arc_placements"
    assert job.progress_snapshot["progress"] == 99
    assert job.progress_snapshot["story_arc_placements_total"] == 2
    assert job.progress_snapshot["story_arc_placements_queued"] == 2
    assert job.progress_snapshot["story_arc_placements_completed"] == 0
    assert job.progress_snapshot["story_arc_placements_running"] == 0
    assert job.progress_snapshot["story_arc_placements_retry_wait"] == 0
    assert job.progress_snapshot["story_arc_placements_failed"] == 0
    assert job.progress_snapshot["story_arc_placements_cancelled"] == 0
    assert emitted_events[-1]["phase"] == "story_arc_placements"
    assert not any(
        str(event.get("message", "")).startswith("Registered ") for event in emitted_events
    )
    assert {call.args[3] for call in logs.await_args_list if len(call.args) >= 4} >= {
        "story_arc_materialization_completed",
        "story_arc_placements_queued",
    }
    assert all(
        len(call.args) < 4 or call.args[3] != "import_completed" for call in logs.await_args_list
    )
