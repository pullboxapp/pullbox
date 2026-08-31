"""Durable rollback orchestration around in-flight Story Arc placement work."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event

from pullbox.models.import_job import (
    ImportControlRequest,
    ImportJob,
    ImportJobAction,
    ImportJobActionStatus,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_sync import StoryArcSyncWork, StoryArcSyncWorkState
from pullbox.services import import_rollback_execution
from pullbox.services.import_job_actions import (
    StoryArcManagedPlacementRollbackDeferredError,
)
from pullbox.services.import_rollback_execution import RollbackActionPlan, rollback_import_job

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

    from pullbox.schemas.import_job import ImportProgressEvent


@pytest.mark.asyncio
async def test_rollback_reads_completed_actions_in_bounded_stable_pages(
    db_session: AsyncSession,
    async_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = ImportJob(
        source_path="/imports/large-library",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.ROLLING_BACK,
        import_started_at=datetime.now(UTC),
    )
    actions = [
        ImportJobAction(
            import_job=job,
            sequence_no=sequence_no,
            phase="files",
            action_type=f"action_{index}",
            payload={"index": index},
        )
        for index, sequence_no in enumerate((1, 2, 2, 3, 4), start=1)
    ]
    db_session.add_all(actions)
    await db_session.flush()
    monkeypatch.setattr(
        import_rollback_execution,
        "ROLLBACK_ACTION_PAGE_SIZE",
        2,
        raising=False,
    )
    action_selects: list[str] = []

    def record_statement(*args: object) -> None:
        sql = str(args[2]).lstrip().upper()
        if "FROM IMPORT_JOB_ACTIONS" in sql and "COUNT(" not in sql:
            action_selects.append(sql)

    observed: list[tuple[int, int, str]] = []

    async def reverse_action(_session: AsyncSession, plan: RollbackActionPlan) -> None:
        action_id = int(plan.action_id)
        action = await db_session.get(ImportJobAction, action_id)
        assert action is not None
        observed.append((action.sequence_no, action.id, action.action_type))
        action.status = ImportJobActionStatus.ROLLED_BACK

    event.listen(async_engine.sync_engine, "before_cursor_execute", record_statement)
    try:
        completed = await rollback_import_job(
            db_session,
            job.id,
            rollback_action=reverse_action,
            restore_review_state=AsyncMock(),
            recompute_series_counters=AsyncMock(),
            recompute_file_counters=AsyncMock(),
            log_event=AsyncMock(),
            emit_progress=AsyncMock(),
            estimate_remaining_seconds=lambda *_args: None,
            job_stats=lambda _job: {},
        )
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", record_statement)

    expected = sorted(
        ((action.sequence_no, action.id, action.action_type) for action in actions),
        key=lambda item: (item[0], item[1]),
        reverse=True,
    )
    assert completed is True
    assert observed == expected
    assert len(action_selects) == 3
    assert all("LIMIT" in statement for statement in action_selects)


@pytest.mark.asyncio
async def test_rollback_checkpoints_pages_and_coalesces_durable_progress(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = ImportJob(
        source_path="/imports/checkpointed-library",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.ROLLING_BACK,
        import_started_at=datetime.now(UTC),
    )
    actions = [
        ImportJobAction(
            import_job=job,
            sequence_no=sequence_no,
            phase="files",
            action_type=f"action_{sequence_no}",
        )
        for sequence_no in range(1, 6)
    ]
    db_session.add_all(actions)
    await db_session.flush()
    monkeypatch.setattr(
        import_rollback_execution,
        "ROLLBACK_ACTION_PAGE_SIZE",
        2,
        raising=False,
    )
    monkeypatch.setattr(
        import_rollback_execution,
        "monotonic",
        lambda: 100.0,
        raising=False,
    )
    commit_count = 0

    def record_commit(_session: object) -> None:
        nonlocal commit_count
        commit_count += 1

    async def reverse_action(_session: AsyncSession, plan: RollbackActionPlan) -> None:
        action = await db_session.get(ImportJobAction, plan.action_id)
        assert action is not None
        action.status = ImportJobActionStatus.ROLLED_BACK

    emitted_progress: list[int] = []

    async def emit_progress(
        session: AsyncSession,
        _job: ImportJob,
        progress_event: ImportProgressEvent,
        callback: AsyncMock,
    ) -> None:
        emitted_progress.append(int(progress_event.progress))
        await session.commit()
        await callback(progress_event)

    progress_callback = AsyncMock()
    event.listen(db_session.sync_session, "after_commit", record_commit)
    try:
        completed = await rollback_import_job(
            db_session,
            job.id,
            rollback_action=reverse_action,
            restore_review_state=AsyncMock(),
            recompute_series_counters=AsyncMock(),
            recompute_file_counters=AsyncMock(),
            log_event=AsyncMock(),
            emit_progress=emit_progress,
            estimate_remaining_seconds=lambda *_args: None,
            job_stats=lambda _job: {},
            progress_callback=progress_callback,
        )
    finally:
        event.remove(db_session.sync_session, "after_commit", record_commit)

    assert completed is True
    assert commit_count == 3
    assert emitted_progress == [100]
    progress_callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_running_story_arc_placement_defers_reverse_walk_without_failing_job(
    db_session: AsyncSession,
) -> None:
    job = ImportJob(
        source_path="/imports/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.ROLLING_BACK,
        control_request=ImportControlRequest.CANCEL,
        import_started_at=datetime.now(UTC),
        progress_snapshot={"mode": "rollback", "phase": "queued", "progress": 0},
    )
    action = ImportJobAction(
        import_job=job,
        sequence_no=1,
        phase="story_arc_placements",
        action_type="story_arc_managed_placement_requested",
        payload={"sync_work_id": 73},
    )
    root = LibraryRoot(name="Rollback", path="/library/rollback", enabled=True)
    series = Series(title="Rollback", sort_title="rollback", library_root=root)
    issue = Issue(series=series, issue_number=1, issue_number_text="1")
    library_file = LibraryFile(
        file_path="/library/rollback/issue.cbz",
        file_name="issue.cbz",
        file_size=1,
        file_format=FileFormat.CBZ,
        file_modified_at=datetime.now(UTC),
        match_confidence=MatchConfidence.HIGH,
        issue=issue,
        library_root=root,
    )
    membership = IssueStoryArc(
        story_arc=StoryArc(name="Rollback Arc"),
        issue=issue,
        sequence_number=1,
        source_ordinal=1,
        resolution_state=StoryArcResolutionState.RESOLVED,
        source_kind=StoryArcSourceKind.MYLAR3,
    )
    db_session.add_all([action, library_file, membership])
    await db_session.flush()
    work = StoryArcSyncWork(
        issue_story_arc_id=membership.id,
        library_file_id=library_file.id,
        origin_import_action_id=action.id,
        origin_import_job_id=job.id,
        desired_generation="7" * 64,
        source_signature_hash="8" * 64,
        source_file_path=library_file.file_path,
        source_file_size=library_file.file_size,
        source_file_modified_at=library_file.file_modified_at,
        story_arc_revision=1,
        membership_sequence=1,
        policy_schema_version=1,
        state=StoryArcSyncWorkState.RUNNING,
    )
    db_session.add(work)
    await db_session.flush()
    work_id = int(work.id)
    action.payload = {"sync_work_id": work_id}
    await db_session.flush()
    reverse = AsyncMock(side_effect=StoryArcManagedPlacementRollbackDeferredError(work_id))
    restore = AsyncMock()
    recompute_series = AsyncMock()
    recompute_files = AsyncMock()
    log_event = AsyncMock()

    completed = await rollback_import_job(
        db_session,
        job.id,
        rollback_action=reverse,
        restore_review_state=restore,
        recompute_series_counters=recompute_series,
        recompute_file_counters=recompute_files,
        log_event=log_event,
        emit_progress=AsyncMock(),
        estimate_remaining_seconds=lambda *_args: None,
        job_stats=lambda _job: {},
    )

    assert completed is False
    assert job.status is ImportJobStatus.ROLLING_BACK
    assert job.control_request is ImportControlRequest.CANCEL
    assert job.progress_snapshot["phase"] == "story_arc_placements"
    assert job.progress_snapshot["story_arc_rollback_waiting_work_id"] == work_id
    assert job.story_arc_rollback_waiting_work_id == work_id
    restore.assert_not_awaited()
    recompute_series.assert_not_awaited()
    recompute_files.assert_not_awaited()
    assert any(
        len(call.args) >= 4 and call.args[3] == "import_rollback_waiting_for_story_arc_placement"
        for call in log_event.await_args_list
    )


@pytest.mark.asyncio
async def test_completed_rollback_clears_deferred_followup_marker(
    db_session: AsyncSession,
) -> None:
    job = ImportJob(
        source_path="/imports/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.ROLLING_BACK,
        import_started_at=datetime.now(UTC),
        story_arc_placement_followup_pending=True,
        progress_snapshot={
            "mode": "rollback",
            "phase": "queued",
            "story_arc_placement_followup_pending": True,
        },
    )
    db_session.add(job)
    await db_session.flush()

    completed = await rollback_import_job(
        db_session,
        job.id,
        rollback_action=AsyncMock(),
        restore_review_state=AsyncMock(),
        recompute_series_counters=AsyncMock(),
        recompute_file_counters=AsyncMock(),
        log_event=AsyncMock(),
        emit_progress=AsyncMock(),
        estimate_remaining_seconds=lambda *_args: None,
        job_stats=lambda _job: {},
    )

    assert completed is True
    assert job.status is ImportJobStatus.ROLLED_BACK
    assert job.story_arc_placement_followup_pending is False
    assert job.progress_snapshot == {}
