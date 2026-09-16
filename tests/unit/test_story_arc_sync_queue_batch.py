"""Bounded import-owned story-arc placement enqueue contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select
from sqlalchemy.dialects import postgresql, sqlite

from pullbox.api.v1.import_job_control_actions import clear_import_history_response
from pullbox.core.exceptions import ValidationError
from pullbox.models.import_job import (
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
    ImportedStoryArcStatus,
    IssueStoryArc,
    StoryArc,
    StoryArcLifecycle,
    StoryArcPlacement,
    StoryArcPlacementMode,
    StoryArcPlacementOwnership,
    StoryArcPlacementState,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.models.story_arc_sync import StoryArcSyncWork, StoryArcSyncWorkState
from pullbox.services import import_job_actions as journal
from pullbox.services import import_job_controls
from pullbox.services import story_arc_sync_queue as sync_queue
from pullbox.services.story_arc_placement_integration import StoryArcPlacementIntegrationError

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Dialect
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


def _policy(root_id: int, destination: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": "copy",
        "target_library_root_id": root_id,
        "destination_root": str(destination),
        "folder_template": "{StoryArc}",
        "file_template": "{ReadingOrder:03d} - {Series} {IssueNumber}",
        "symlink_style": None,
        "synchronize": False,
    }


async def _seed_batch(
    session: AsyncSession,
    tmp_path: Path,
    *,
    count: int,
) -> tuple[ImportJob, list[sync_queue.ImportStoryArcSyncProposal]]:
    root = LibraryRoot(name=f"Comics {count}", path=str(tmp_path), enabled=True)
    series = Series(title=f"Series {count}", sort_title=f"series {count}", library_root=root)
    issue = Issue(series=series, issue_number=1, issue_number_text="1")
    library_file = LibraryFile(
        file_path=str(tmp_path / f"Series {count} 001.cbz"),
        file_name=f"Series {count} 001.cbz",
        file_size=123,
        file_format=FileFormat.CBZ,
        file_modified_at=datetime.now(UTC),
        match_confidence=MatchConfidence.HIGH,
        issue=issue,
        library_root=root,
        source_signature={"schema_version": 1, "size": 123, "mtime_ns": 99},
    )
    job = ImportJob(
        source_path=str(tmp_path / f"batch-{count}.db"),
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.IMPORTING,
        progress_snapshot={"phase": "story_arc_placements"},
    )
    session.add_all([library_file, job])
    await session.flush()

    arcs: list[StoryArc] = []
    memberships: list[IssueStoryArc] = []
    for ordinal in range(1, count + 1):
        arc = StoryArc(
            name=f"Arc {count}-{ordinal}",
            source_kind=StoryArcSourceKind.PULLBOX,
            lifecycle=StoryArcLifecycle.ACTIVE,
            sync_enabled=False,
            target_library_root_id=root.id,
            policy_schema_version=1,
            policy_snapshot=_policy(int(root.id), tmp_path / "arcs"),
            revision=ordinal,
        )
        membership = IssueStoryArc(
            story_arc=arc,
            issue=issue,
            sequence_number=ordinal,
            source_ordinal=ordinal,
            resolution_state=StoryArcResolutionState.RESOLVED,
            source_kind=StoryArcSourceKind.MYLAR3,
            sync_eligible=False,
        )
        arcs.append(arc)
        memberships.append(membership)
    session.add_all(memberships)
    await session.flush()

    staged_arcs: list[ImportedStoryArc] = []
    entries: list[ImportedStoryArcEntry] = []
    for ordinal, (arc, membership) in enumerate(zip(arcs, memberships, strict=True), start=1):
        staged_arc = ImportedStoryArc(
            import_job=job,
            source_kind=StoryArcSourceKind.MYLAR3,
            source_key=f"mylar3:arc:{count}:{ordinal}",
            source_ordinal=ordinal,
            name=arc.name,
            status=ImportedStoryArcStatus.IMPORTED,
            materialized_story_arc_id=arc.id,
        )
        entry = ImportedStoryArcEntry(
            imported_story_arc=staged_arc,
            matched_issue_id=issue.id,
            materialized_membership_id=membership.id,
            source_ordinal=1,
            reading_order=ordinal,
            resolution_state=StoryArcResolutionState.RESOLVED,
            source_kind=StoryArcSourceKind.MYLAR3,
        )
        staged_arcs.append(staged_arc)
        entries.append(entry)
    session.add_all(entries)
    await session.flush()

    return job, [
        sync_queue.ImportStoryArcSyncProposal(
            library_file=library_file,
            membership=membership,
            story_arc=arc,
            imported_story_arc_id=int(staged_arc.id),
            imported_story_arc_entry_id=int(entry.id),
        )
        for arc, membership, staged_arc, entry in zip(
            arcs,
            memberships,
            staged_arcs,
            entries,
            strict=True,
        )
    ]


def _add_exact_current_placement(
    session: AsyncSession,
    *,
    proposal: sync_queue.ImportStoryArcSyncProposal,
    suffix: str,
    action: ImportJobAction | None = None,
    operation_token: str | None = None,
    state: StoryArcPlacementState = StoryArcPlacementState.CURRENT,
    mode: StoryArcPlacementMode = StoryArcPlacementMode.COPY,
    ownership: StoryArcPlacementOwnership = StoryArcPlacementOwnership.MANAGED,
    source_kind: StoryArcSourceKind = StoryArcSourceKind.PULLBOX,
    last_result: dict[str, object] | None = None,
) -> StoryArcPlacement:
    placement = StoryArcPlacement(
        issue_story_arc_id=int(proposal.membership.id),
        library_file_id=int(proposal.library_file.id),
        library_root_id=proposal.library_file.library_root_id,
        placement_path=f"/story-arcs/{suffix}.cbz",
        mode=mode,
        ownership=ownership,
        source_kind=source_kind,
        source_import_job_id=(action.import_job_id if action is not None else None),
        creating_action_id=(int(action.id) if action is not None else None),
        rendered_reading_order=proposal.membership.sequence_number,
        policy_schema_version=proposal.story_arc.policy_schema_version,
        operation_token=operation_token,
        source_fingerprint={"schema_version": 1, "size": proposal.library_file.file_size},
        state=state,
        last_result=(
            {"schema_version": 1, "status": "complete"} if last_result is None else last_result
        ),
    )
    session.add(placement)
    return placement


async def _add_non_origin_work(
    session: AsyncSession,
    proposal: sync_queue.ImportStoryArcSyncProposal,
    *,
    state: StoryArcSyncWorkState,
) -> StoryArcSyncWork:
    generation, source_hash = sync_queue._desired_generation(
        proposal.library_file,
        proposal.membership,
        proposal.story_arc,
    )
    work = StoryArcSyncWork(
        issue_story_arc_id=int(proposal.membership.id),
        library_file_id=int(proposal.library_file.id),
        desired_generation=generation,
        source_signature_hash=source_hash,
        source_file_path=proposal.library_file.file_path,
        source_file_size=proposal.library_file.file_size,
        source_file_modified_at=proposal.library_file.file_modified_at,
        source_file_hash=proposal.library_file.file_hash,
        story_arc_revision=proposal.story_arc.revision,
        membership_sequence=proposal.membership.sequence_number,
        policy_schema_version=proposal.story_arc.policy_schema_version or 1,
        state=state,
    )
    session.add(work)
    await session.flush()
    return work


async def _seed_failed_job_with_held_import_work(
    session: AsyncSession,
    tmp_path: Path,
) -> tuple[int, int]:
    """Persist one failed import whose placement work was never published."""
    job, proposals = await _seed_batch(session, tmp_path, count=1)
    results = await sync_queue.enqueue_import_story_arc_sync_work_batch(
        session,
        job=job,
        proposals=proposals,
        record_actions=journal.record_actions,
    )
    work = results[0].work
    assert work is not None
    assert work.claimable is False
    job.status = ImportJobStatus.FAILED
    job.import_started_at = datetime.now(UTC)
    await session.commit()
    return int(job.id), int(work.id)


@pytest.mark.asyncio
async def test_deleting_failed_import_discards_exact_unpublished_story_arc_work(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    job_id, work_id = await _seed_failed_job_with_held_import_work(db_session, tmp_path)

    result = await import_job_controls.cancel_job(
        db_session,
        job_id,
        log_job_deleted=lambda _job_id, _status: None,
    )
    await db_session.commit()

    assert result == "deleted"
    assert await db_session.get(ImportJob, job_id) is None
    assert await db_session.get(StoryArcSyncWork, work_id) is None


@pytest.mark.asyncio
async def test_clearing_import_history_discards_exact_unpublished_story_arc_work(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    job_id, work_id = await _seed_failed_job_with_held_import_work(db_session, tmp_path)

    response = await clear_import_history_response(db_session)
    await db_session.commit()

    assert response == {"deleted": 1}
    assert await db_session.get(ImportJob, job_id) is None
    assert await db_session.get(StoryArcSyncWork, work_id) is None


@pytest.mark.asyncio
async def test_deleting_failed_import_refuses_claimable_story_arc_work(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    job_id, work_id = await _seed_failed_job_with_held_import_work(db_session, tmp_path)
    work = await db_session.get(StoryArcSyncWork, work_id)
    assert work is not None
    work.claimable = True
    await db_session.commit()

    with pytest.raises(ValidationError, match="Roll back this import"):
        await import_job_controls.cancel_job(
            db_session,
            job_id,
            log_job_deleted=lambda _job_id, _status: None,
        )

    assert await db_session.get(ImportJob, job_id) is not None
    assert await db_session.get(StoryArcSyncWork, work_id) is not None


@pytest.mark.asyncio
async def test_clearing_history_refuses_claimable_story_arc_work(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    job_id, work_id = await _seed_failed_job_with_held_import_work(db_session, tmp_path)
    work = await db_session.get(StoryArcSyncWork, work_id)
    assert work is not None
    work.claimable = True
    await db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await clear_import_history_response(db_session)

    assert exc_info.value.status_code == 409
    assert "Roll back this import" in str(exc_info.value.detail)
    assert await db_session.get(ImportJob, job_id) is not None
    assert await db_session.get(StoryArcSyncWork, work_id) is not None


@pytest.mark.asyncio
async def test_deleting_failed_import_refuses_attempted_held_story_arc_work(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    job_id, work_id = await _seed_failed_job_with_held_import_work(db_session, tmp_path)
    work = await db_session.get(StoryArcSyncWork, work_id)
    assert work is not None
    work.attempt_count = 1
    await db_session.commit()

    with pytest.raises(ValidationError, match="Roll back this import"):
        await import_job_controls.cancel_job(
            db_session,
            job_id,
            log_job_deleted=lambda _job_id, _status: None,
        )

    assert await db_session.get(ImportJob, job_id) is not None
    assert await db_session.get(StoryArcSyncWork, work_id) is not None


@pytest.mark.asyncio
async def test_import_enqueue_batch_creates_200_ordered_exact_bindings(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    job, proposals = await _seed_batch(db_session, tmp_path, count=200)

    results = await sync_queue.enqueue_import_story_arc_sync_work_batch(
        db_session,
        job=job,
        proposals=proposals,
        record_actions=journal.record_actions,
    )

    assert len(results) == 200
    assert [result.classification for result in results] == ["created"] * 200
    assert [result.work.issue_story_arc_id for result in results if result.work] == [
        proposal.membership.id for proposal in proposals
    ]
    assert [result.action.sequence_no for result in results if result.action] == list(range(1, 201))
    for proposal, result in zip(proposals, results, strict=True):
        assert result.work is not None and result.action is not None
        assert result.work.origin_import_action_id == result.action.id
        assert result.action.payload == {
            "schema_version": 1,
            "sync_work_id": result.work.id,
            "membership_id": proposal.membership.id,
            "desired_generation": result.desired_generation,
            "imported_story_arc_id": proposal.imported_story_arc_id,
            "imported_story_arc_entry_id": proposal.imported_story_arc_entry_id,
            "source_import_job_id": job.id,
        }


@pytest.mark.asyncio
async def test_import_enqueue_batch_query_count_is_constant_at_200(
    db_session: AsyncSession,
    async_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    job, proposals = await _seed_batch(db_session, tmp_path, count=200)
    statements: list[str] = []

    def record_statement(*args: object) -> None:
        sql = str(args[2]).lstrip().upper()
        if sql.startswith(("SELECT", "INSERT", "UPDATE")):
            statements.append(sql)

    event.listen(async_engine.sync_engine, "before_cursor_execute", record_statement)
    try:
        results = await sync_queue.enqueue_import_story_arc_sync_work_batch(
            db_session,
            job=job,
            proposals=proposals,
            record_actions=journal.record_actions,
        )
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", record_statement)

    assert len(results) == 200
    assert sum(sql.startswith("SELECT") for sql in statements) <= 5
    assert sum(sql.startswith("INSERT") for sql in statements) <= 2
    assert sum(sql.startswith("UPDATE") for sql in statements) <= 1


@pytest.mark.asyncio
async def test_import_enqueue_batch_durable_recovery_query_count_is_constant_at_200(
    db_session: AsyncSession,
    async_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    job, proposals = await _seed_batch(db_session, tmp_path, count=200)
    await sync_queue.enqueue_import_story_arc_sync_work_batch(
        db_session,
        job=job,
        proposals=proposals,
        record_actions=journal.record_actions,
    )
    await db_session.commit()
    statements: list[str] = []

    def record_statement(*args: object) -> None:
        sql = str(args[2]).lstrip().upper()
        if sql.startswith(("SELECT", "INSERT", "UPDATE")):
            statements.append(sql)

    event.listen(async_engine.sync_engine, "before_cursor_execute", record_statement)
    try:
        results = await sync_queue.enqueue_import_story_arc_sync_work_batch(
            db_session,
            job=job,
            proposals=proposals,
            record_actions=journal.record_actions,
        )
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", record_statement)

    assert [result.classification for result in results] == ["existing_import_work_pending"] * 200
    assert sum(sql.startswith("SELECT") for sql in statements) <= 5
    assert not any(sql.startswith(("INSERT", "UPDATE")) for sql in statements)


@pytest.mark.asyncio
async def test_import_enqueue_batch_never_commits_callers_transaction(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    job, proposals = await _seed_batch(db_session, tmp_path, count=2)
    await db_session.commit()

    results = await sync_queue.enqueue_import_story_arc_sync_work_batch(
        db_session,
        job=job,
        proposals=proposals,
        record_actions=journal.record_actions,
    )
    assert len(results) == 2
    assert db_session.in_transaction()

    await db_session.rollback()

    assert int(await db_session.scalar(select(func.count(StoryArcSyncWork.id))) or 0) == 0
    assert int(await db_session.scalar(select(func.count(ImportJobAction.id))) or 0) == 0


@pytest.mark.asyncio
async def test_import_enqueue_batch_marks_exact_duplicate_as_in_call_only(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    job, proposals = await _seed_batch(db_session, tmp_path, count=1)

    results = await sync_queue.enqueue_import_story_arc_sync_work_batch(
        db_session,
        job=job,
        proposals=[proposals[0], proposals[0]],
        record_actions=journal.record_actions,
    )

    assert [result.classification for result in results] == [
        "created",
        "in_call_duplicate",
    ]
    assert results[0].work is results[1].work
    assert results[0].action is results[1].action
    assert int(await db_session.scalar(select(func.count(StoryArcSyncWork.id))) or 0) == 1
    assert int(await db_session.scalar(select(func.count(ImportJobAction.id))) or 0) == 1


@pytest.mark.asyncio
async def test_import_enqueue_batch_deduplicates_distinct_entries_for_one_membership(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    job, proposals = await _seed_batch(db_session, tmp_path, count=1)
    original = proposals[0]
    duplicate_entry = ImportedStoryArcEntry(
        imported_story_arc_id=original.imported_story_arc_id,
        matched_issue_id=original.membership.issue_id,
        materialized_membership_id=original.membership.id,
        source_ordinal=2,
        reading_order=2,
        resolution_state=StoryArcResolutionState.RESOLVED,
        source_kind=StoryArcSourceKind.MYLAR3,
    )
    db_session.add(duplicate_entry)
    await db_session.flush()
    duplicate = sync_queue.ImportStoryArcSyncProposal(
        library_file=original.library_file,
        membership=original.membership,
        story_arc=original.story_arc,
        imported_story_arc_id=original.imported_story_arc_id,
        imported_story_arc_entry_id=int(duplicate_entry.id),
    )

    results = await sync_queue.enqueue_import_story_arc_sync_work_batch(
        db_session,
        job=job,
        proposals=[duplicate, original],
        record_actions=journal.record_actions,
    )

    assert [result.classification for result in results] == [
        "in_call_membership_duplicate",
        "created",
    ]
    assert results[0].work is results[1].work
    assert results[0].action is results[1].action
    assert results[1].work is not None
    assert (
        results[1].work.origin_imported_story_arc_entry_id == original.imported_story_arc_entry_id
    )
    assert int(await db_session.scalar(select(func.count(StoryArcSyncWork.id))) or 0) == 1
    assert int(await db_session.scalar(select(func.count(ImportJobAction.id))) or 0) == 1

    await db_session.commit()
    replay = await sync_queue.enqueue_import_story_arc_sync_work_batch(
        db_session,
        job=job,
        proposals=[duplicate],
        record_actions=journal.record_actions,
    )

    assert replay[0].classification == "existing_import_membership_duplicate"
    assert replay[0].work is not None
    assert replay[0].work.origin_imported_story_arc_entry_id == original.imported_story_arc_entry_id
    assert int(await db_session.scalar(select(func.count(StoryArcSyncWork.id))) or 0) == 1
    assert int(await db_session.scalar(select(func.count(ImportJobAction.id))) or 0) == 1


@pytest.mark.asyncio
async def test_import_enqueue_batch_holds_build_phase_work_out_of_ready_lanes(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    job, proposals = await _seed_batch(db_session, tmp_path, count=1)
    job.progress_snapshot = {"phase": "story_arcs"}

    results = await sync_queue.enqueue_import_story_arc_sync_work_batch(
        db_session,
        job=job,
        proposals=proposals,
        record_actions=journal.record_actions,
    )

    work = results[0].work
    assert work is not None
    assert work.claimable is False
    assert (
        await sync_queue._ready_work_ids(
            db_session,
            now=datetime.now(UTC),
            limit=1,
        )
        == []
    )
    assert (
        await sync_queue.claim_story_arc_sync_work(
            db_session,
            int(work.id),
            now=datetime.now(UTC),
        )
        is None
    )


@pytest.mark.asyncio
async def test_unclaimable_retry_work_does_not_schedule_continuation(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    job, proposals = await _seed_batch(db_session, tmp_path, count=2)
    results = await sync_queue.enqueue_import_story_arc_sync_work_batch(
        db_session,
        job=job,
        proposals=proposals,
        record_actions=journal.record_actions,
    )
    held_work = results[0].work
    claimable_work = results[1].work
    assert held_work is not None and claimable_work is not None
    now = datetime.now(UTC)
    held_work.state = StoryArcSyncWorkState.RETRY_WAIT
    held_work.next_attempt_at = now + timedelta(seconds=1)
    claimable_work.claimable = True
    claimable_work.state = StoryArcSyncWorkState.RETRY_WAIT
    claimable_work.next_attempt_at = now + timedelta(seconds=2)
    await db_session.flush()

    assert await sync_queue._next_retry_at(db_session) == claimable_work.next_attempt_at


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        StoryArcSyncWorkState.QUEUED,
        StoryArcSyncWorkState.RUNNING,
        StoryArcSyncWorkState.RETRY_WAIT,
    ],
)
async def test_import_enqueue_batch_classifies_durable_active_origin_as_pending(
    db_session: AsyncSession,
    tmp_path: Path,
    state: StoryArcSyncWorkState,
) -> None:
    job, proposals = await _seed_batch(db_session, tmp_path, count=1)
    created = await sync_queue.enqueue_import_story_arc_sync_work_batch(
        db_session,
        job=job,
        proposals=proposals,
        record_actions=journal.record_actions,
    )
    work = created[0].work
    assert work is not None
    work.state = state
    await db_session.commit()

    recovered = await sync_queue.enqueue_import_story_arc_sync_work_batch(
        db_session,
        job=job,
        proposals=proposals,
        record_actions=journal.record_actions,
    )

    assert recovered[0].classification == "existing_import_work_pending"
    assert recovered[0].work is work
    assert recovered[0].action is created[0].action


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [StoryArcSyncWorkState.FAILED, StoryArcSyncWorkState.CANCELLED],
)
async def test_import_enqueue_batch_rejects_terminal_origin_without_completed_placement(
    db_session: AsyncSession,
    tmp_path: Path,
    state: StoryArcSyncWorkState,
) -> None:
    job, proposals = await _seed_batch(db_session, tmp_path, count=1)
    created = await sync_queue.enqueue_import_story_arc_sync_work_batch(
        db_session,
        job=job,
        proposals=proposals,
        record_actions=journal.record_actions,
    )
    work = created[0].work
    assert work is not None
    work.state = state
    await db_session.flush()

    with pytest.raises(StoryArcPlacementIntegrationError) as exc_info:
        await sync_queue.enqueue_import_story_arc_sync_work_batch(
            db_session,
            job=job,
            proposals=proposals,
            record_actions=journal.record_actions,
        )

    assert exc_info.value.code == "import_sync_existing_work_unusable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [ImportJobActionStatus.ROLLED_BACK, ImportJobActionStatus.ROLLBACK_FAILED],
)
async def test_import_enqueue_batch_requires_completed_origin_action(
    db_session: AsyncSession,
    tmp_path: Path,
    status: ImportJobActionStatus,
) -> None:
    job, proposals = await _seed_batch(db_session, tmp_path, count=1)
    created = await sync_queue.enqueue_import_story_arc_sync_work_batch(
        db_session,
        job=job,
        proposals=proposals,
        record_actions=journal.record_actions,
    )
    action = created[0].action
    assert action is not None
    action.status = status
    await db_session.commit()

    with pytest.raises(StoryArcPlacementIntegrationError) as exc_info:
        await sync_queue.enqueue_import_story_arc_sync_work_batch(
            db_session,
            job=job,
            proposals=proposals,
            record_actions=journal.record_actions,
        )

    assert exc_info.value.code == "import_sync_existing_origin_invalid"


@pytest.mark.asyncio
async def test_import_enqueue_batch_completed_origin_requires_one_exact_owned_placement(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    job, proposals = await _seed_batch(db_session, tmp_path, count=1)
    created = await sync_queue.enqueue_import_story_arc_sync_work_batch(
        db_session,
        job=job,
        proposals=proposals,
        record_actions=journal.record_actions,
    )
    work = created[0].work
    action = created[0].action
    assert work is not None and action is not None
    work.state = StoryArcSyncWorkState.COMPLETED
    _add_exact_current_placement(
        db_session,
        proposal=proposals[0],
        suffix="completed-origin",
        action=action,
    )
    await db_session.commit()

    recovered = await sync_queue.enqueue_import_story_arc_sync_work_batch(
        db_session,
        job=job,
        proposals=proposals,
        record_actions=journal.record_actions,
    )

    assert recovered[0].classification == "existing_import_work_completed"
    assert recovered[0].work is work
    assert recovered[0].action is action


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_evidence",
    ["missing", "leased", "referenced", "duplicate"],
)
async def test_import_enqueue_batch_rejects_completed_origin_without_one_exact_placement(
    db_session: AsyncSession,
    tmp_path: Path,
    invalid_evidence: str,
) -> None:
    job, proposals = await _seed_batch(db_session, tmp_path, count=1)
    created = await sync_queue.enqueue_import_story_arc_sync_work_batch(
        db_session,
        job=job,
        proposals=proposals,
        record_actions=journal.record_actions,
    )
    work = created[0].work
    action = created[0].action
    assert work is not None and action is not None
    work.state = StoryArcSyncWorkState.COMPLETED
    if invalid_evidence != "missing":
        _add_exact_current_placement(
            db_session,
            proposal=proposals[0],
            suffix="invalid-origin-1",
            action=action,
            operation_token=("active-lease" if invalid_evidence == "leased" else None),
            mode=(
                StoryArcPlacementMode.REFERENCE_ONLY
                if invalid_evidence == "referenced"
                else StoryArcPlacementMode.COPY
            ),
            ownership=(
                StoryArcPlacementOwnership.REFERENCED
                if invalid_evidence == "referenced"
                else StoryArcPlacementOwnership.MANAGED
            ),
        )
    if invalid_evidence == "duplicate":
        _add_exact_current_placement(
            db_session,
            proposal=proposals[0],
            suffix="invalid-origin-2",
            action=action,
        )
    await db_session.commit()

    with pytest.raises(StoryArcPlacementIntegrationError) as exc_info:
        await sync_queue.enqueue_import_story_arc_sync_work_batch(
            db_session,
            job=job,
            proposals=proposals,
            record_actions=journal.record_actions,
        )

    assert exc_info.value.code == "import_sync_completed_placement_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "ownership"),
    [
        (StoryArcPlacementMode.COPY, StoryArcPlacementOwnership.MANAGED),
        (StoryArcPlacementMode.REFERENCE_ONLY, StoryArcPlacementOwnership.REFERENCED),
    ],
)
async def test_import_enqueue_batch_reuses_non_origin_work_only_with_exact_placement(
    db_session: AsyncSession,
    tmp_path: Path,
    mode: StoryArcPlacementMode,
    ownership: StoryArcPlacementOwnership,
) -> None:
    job, proposals = await _seed_batch(db_session, tmp_path, count=1)
    work = await _add_non_origin_work(
        db_session,
        proposals[0],
        state=StoryArcSyncWorkState.FAILED,
    )
    _add_exact_current_placement(
        db_session,
        proposal=proposals[0],
        suffix=f"non-origin-{ownership.value}",
        mode=mode,
        ownership=ownership,
        source_kind=(
            StoryArcSourceKind.MYLAR3
            if ownership is StoryArcPlacementOwnership.REFERENCED
            else StoryArcSourceKind.PULLBOX
        ),
        last_result=(
            {"schema_version": 1, "code": "reference_current"}
            if ownership is StoryArcPlacementOwnership.REFERENCED
            else None
        ),
    )
    await db_session.commit()

    recovered = await sync_queue.enqueue_import_story_arc_sync_work_batch(
        db_session,
        job=job,
        proposals=proposals,
        record_actions=journal.record_actions,
    )

    assert recovered[0].classification == "existing_non_origin_placement"
    assert recovered[0].work is work
    assert recovered[0].action is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_evidence",
    ["leased", "drifted", "wrong_sequence", "wrong_policy", "duplicate"],
)
async def test_import_enqueue_batch_rejects_inexact_non_origin_placement_evidence(
    db_session: AsyncSession,
    tmp_path: Path,
    invalid_evidence: str,
) -> None:
    job, proposals = await _seed_batch(db_session, tmp_path, count=1)
    await _add_non_origin_work(
        db_session,
        proposals[0],
        state=StoryArcSyncWorkState.COMPLETED,
    )
    placement = _add_exact_current_placement(
        db_session,
        proposal=proposals[0],
        suffix="inexact-non-origin-1",
        operation_token=("active-lease" if invalid_evidence == "leased" else None),
        state=(
            StoryArcPlacementState.DRIFTED
            if invalid_evidence == "drifted"
            else StoryArcPlacementState.CURRENT
        ),
    )
    if invalid_evidence == "wrong_sequence":
        placement.rendered_reading_order = proposals[0].membership.sequence_number + 1
    elif invalid_evidence == "wrong_policy":
        placement.policy_schema_version = (proposals[0].story_arc.policy_schema_version or 1) + 1
    elif invalid_evidence == "duplicate":
        _add_exact_current_placement(
            db_session,
            proposal=proposals[0],
            suffix="inexact-non-origin-2",
        )
    await db_session.commit()

    with pytest.raises(StoryArcPlacementIntegrationError) as exc_info:
        await sync_queue.enqueue_import_story_arc_sync_work_batch(
            db_session,
            job=job,
            proposals=proposals,
            record_actions=journal.record_actions,
        )

    assert exc_info.value.code == "import_sync_non_origin_placement_unverified"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        StoryArcSyncWorkState.QUEUED,
        StoryArcSyncWorkState.RUNNING,
        StoryArcSyncWorkState.RETRY_WAIT,
        StoryArcSyncWorkState.FAILED,
        StoryArcSyncWorkState.CANCELLED,
        StoryArcSyncWorkState.COMPLETED,
    ],
)
async def test_import_enqueue_batch_rejects_non_origin_work_without_exact_placement(
    db_session: AsyncSession,
    tmp_path: Path,
    state: StoryArcSyncWorkState,
) -> None:
    job, proposals = await _seed_batch(db_session, tmp_path, count=1)
    await _add_non_origin_work(db_session, proposals[0], state=state)
    await db_session.commit()

    with pytest.raises(StoryArcPlacementIntegrationError) as exc_info:
        await sync_queue.enqueue_import_story_arc_sync_work_batch(
            db_session,
            job=job,
            proposals=proposals,
            record_actions=journal.record_actions,
        )

    assert exc_info.value.code == "import_sync_non_origin_placement_unverified"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload_key", "malformed_value"),
    [
        ("schema_version", True),
        ("sync_work_id", 1.0),
        ("membership_id", True),
        ("desired_generation", 1),
    ],
)
async def test_import_enqueue_batch_rejects_python_equal_malformed_origin_payload(
    db_session: AsyncSession,
    tmp_path: Path,
    payload_key: str,
    malformed_value: object,
) -> None:
    job, proposals = await _seed_batch(db_session, tmp_path, count=1)
    created = await sync_queue.enqueue_import_story_arc_sync_work_batch(
        db_session,
        job=job,
        proposals=proposals,
        record_actions=journal.record_actions,
    )
    action = created[0].action
    assert action is not None
    action.payload = {**dict(action.payload), payload_key: malformed_value}
    await db_session.flush()

    with pytest.raises(StoryArcPlacementIntegrationError) as exc_info:
        await sync_queue.enqueue_import_story_arc_sync_work_batch(
            db_session,
            job=job,
            proposals=proposals,
            record_actions=journal.record_actions,
        )

    assert exc_info.value.code == "import_sync_existing_origin_invalid"


@pytest.mark.asyncio
async def test_import_enqueue_batch_conflicting_duplicate_fails_before_writes(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    job, proposals = await _seed_batch(db_session, tmp_path, count=1)
    original = proposals[0]
    competing_arc = ImportedStoryArc(
        import_job=job,
        source_kind=StoryArcSourceKind.MYLAR3,
        source_key="mylar3:arc:competing",
        source_ordinal=2,
        name=original.story_arc.name,
        status=ImportedStoryArcStatus.IMPORTED,
        materialized_story_arc_id=original.story_arc.id,
    )
    competing_entry = ImportedStoryArcEntry(
        imported_story_arc=competing_arc,
        matched_issue_id=original.membership.issue_id,
        materialized_membership_id=original.membership.id,
        source_ordinal=1,
        reading_order=1,
        resolution_state=StoryArcResolutionState.RESOLVED,
        source_kind=StoryArcSourceKind.MYLAR3,
    )
    db_session.add(competing_entry)
    await db_session.flush()
    conflicting = sync_queue.ImportStoryArcSyncProposal(
        library_file=original.library_file,
        membership=original.membership,
        story_arc=original.story_arc,
        imported_story_arc_id=int(competing_arc.id),
        imported_story_arc_entry_id=int(competing_entry.id),
    )

    with pytest.raises(StoryArcPlacementIntegrationError) as exc_info:
        await sync_queue.enqueue_import_story_arc_sync_work_batch(
            db_session,
            job=job,
            proposals=[original, conflicting],
            record_actions=journal.record_actions,
        )

    assert exc_info.value.code == "import_sync_existing_origin_invalid"
    assert int(await db_session.scalar(select(func.count(StoryArcSyncWork.id))) or 0) == 0
    assert int(await db_session.scalar(select(func.count(ImportJobAction.id))) or 0) == 0


@pytest.mark.asyncio
async def test_import_enqueue_batch_origin_validation_is_atomic(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    job, proposals = await _seed_batch(db_session, tmp_path, count=2)
    invalid = sync_queue.ImportStoryArcSyncProposal(
        library_file=proposals[1].library_file,
        membership=proposals[1].membership,
        story_arc=proposals[1].story_arc,
        imported_story_arc_id=proposals[1].imported_story_arc_id,
        imported_story_arc_entry_id=proposals[0].imported_story_arc_entry_id,
    )

    with pytest.raises(StoryArcPlacementIntegrationError) as exc_info:
        await sync_queue.enqueue_import_story_arc_sync_work_batch(
            db_session,
            job=job,
            proposals=[proposals[0], invalid],
            record_actions=journal.record_actions,
        )

    assert exc_info.value.code == "import_sync_origin_binding_invalid"
    assert int(await db_session.scalar(select(func.count(StoryArcSyncWork.id))) or 0) == 0
    assert int(await db_session.scalar(select(func.count(ImportJobAction.id))) or 0) == 0


@pytest.mark.asyncio
async def test_import_enqueue_batch_rejects_more_than_200_before_queries_or_writes(
    db_session: AsyncSession,
    async_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    job, proposals = await _seed_batch(db_session, tmp_path, count=1)
    statements: list[str] = []

    def record_statement(*args: object) -> None:
        statements.append(str(args[2]))

    event.listen(async_engine.sync_engine, "before_cursor_execute", record_statement)
    try:
        with pytest.raises(ValueError, match="at most 200"):
            await sync_queue.enqueue_import_story_arc_sync_work_batch(
                db_session,
                job=job,
                proposals=proposals * 201,
                record_actions=journal.record_actions,
            )
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", record_statement)

    assert statements == []


@pytest.mark.parametrize(
    "dialect",
    [sqlite.dialect(), postgresql.dialect()],  # type: ignore[no-untyped-call]
)
def test_import_enqueue_batch_work_insert_compiles_portably(dialect: Dialect) -> None:
    statement = sync_queue._import_story_arc_work_insert_statement(returning=True)

    sql = str(statement.compile(dialect=dialect)).upper()

    assert sql.startswith("INSERT INTO STORY_ARC_SYNC_WORK")
    assert "RETURNING" in sql
    assert "ON CONFLICT" not in sql


@pytest.mark.parametrize(
    "dialect",
    [sqlite.dialect(), postgresql.dialect()],  # type: ignore[no-untyped-call]
)
def test_existing_work_placement_lookup_compiles_portably(dialect: Dialect) -> None:
    statement = sync_queue._existing_work_placement_statement(
        action_ids=[11, 12],
        membership_library_pairs=[(21, 31), (22, 32)],
    )

    sql = str(statement.compile(dialect=dialect)).upper()

    assert sql.startswith("SELECT STORY_ARC_PLACEMENTS")
    assert "CREATING_ACTION_ID" in sql
    assert "ISSUE_STORY_ARC_ID" in sql
    assert "LIBRARY_FILE_ID" in sql
    assert "ORDER BY STORY_ARC_PLACEMENTS.ID ASC" in sql
