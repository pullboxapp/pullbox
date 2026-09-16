"""Rollback contracts for import-owned Story Arc placement work."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

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
    StoryArcPlacement,
    StoryArcPlacementMode,
    StoryArcPlacementOwnership,
    StoryArcPlacementState,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.models.story_arc_sync import StoryArcSyncWork, StoryArcSyncWorkState
from pullbox.services.import_job_actions import (
    StoryArcManagedPlacementRollbackDeferredError,
    record_action,
    rollback_action,
)
from pullbox.services.story_arc_placement_integration import (
    StoryArcPlacementImportProvenance,
    StoryArcPlacementIntegrationError,
    StoryArcPlacementSyncService,
)
from pullbox.services.story_arc_sync_queue import enqueue_import_story_arc_sync_work

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class _ImportPlacementContext:
    job_id: int
    action_id: int
    work_id: int
    story_arc_id: int
    membership_id: int
    action_payload: dict[str, object]
    job: ImportJob
    action: ImportJobAction
    work: StoryArcSyncWork
    story_arc: StoryArc
    membership: IssueStoryArc
    library_file: LibraryFile
    canonical_path: Path


def _policy(root_id: int, destination_root: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": "copy",
        "target_library_root_id": root_id,
        "destination_root": str(destination_root),
        "folder_template": "{StoryArc}",
        "file_template": "{ReadingOrder:03d} - {Series} {IssueNumber}",
        "symlink_style": None,
        "synchronize": False,
    }


async def _seed_import_placement(
    session: AsyncSession,
    tmp_path: Path,
) -> _ImportPlacementContext:
    canonical_path = tmp_path / "library" / "Batman 001.cbz"
    canonical_path.parent.mkdir()
    canonical_path.write_bytes(b"canonical-library-file")
    destination_root = tmp_path / "story-arcs"
    destination_root.mkdir()
    root = LibraryRoot(name="Comics", path=str(canonical_path.parent), enabled=True)
    series = Series(title="Batman", sort_title="batman", library_root=root)
    issue = Issue(
        series=series,
        issue_number=1,
        issue_number_text="1",
        title="The Court of Owls",
    )
    library_file = LibraryFile(
        file_path=str(canonical_path),
        file_name=canonical_path.name,
        file_size=canonical_path.stat().st_size,
        file_format=FileFormat.CBZ,
        file_modified_at=datetime.now(UTC),
        match_confidence=MatchConfidence.HIGH,
        issue=issue,
        library_root=root,
        source_signature={
            "schema_version": 1,
            "resolved_path": str(canonical_path.resolve()),
            "size": canonical_path.stat().st_size,
            "mtime_ns": canonical_path.stat().st_mtime_ns,
            "device": canonical_path.stat().st_dev,
            "inode": canonical_path.stat().st_ino,
        },
    )
    story_arc = StoryArc(
        name="Court of Owls",
        source_kind=StoryArcSourceKind.MYLAR3,
        target_library_root_id=root.id,
        policy_schema_version=1,
        policy_snapshot={},
        sync_enabled=False,
        revision=1,
    )
    membership = IssueStoryArc(
        story_arc=story_arc,
        issue=issue,
        sequence_number=1,
        source_ordinal=1,
        resolution_state=StoryArcResolutionState.RESOLVED,
        source_kind=StoryArcSourceKind.MYLAR3,
        sync_eligible=False,
    )
    job = ImportJob(
        source_path=str(tmp_path / "mylar.db"),
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.IMPORTING,
        progress_snapshot={"phase": "story_arc_placements"},
    )
    session.add_all([library_file, membership, job])
    await session.flush()
    story_arc.target_library_root_id = root.id
    story_arc.policy_snapshot = _policy(root.id, destination_root)
    staged_arc = ImportedStoryArc(
        import_job=job,
        source_kind=StoryArcSourceKind.MYLAR3,
        source_key="mylar3:arc:1",
        source_ordinal=1,
        name=story_arc.name,
        status=ImportedStoryArcStatus.IMPORTED,
        selected_for_import=True,
        materialized_story_arc_id=story_arc.id,
    )
    staged_entry = ImportedStoryArcEntry(
        imported_story_arc=staged_arc,
        matched_issue_id=issue.id,
        materialized_membership_id=membership.id,
        source_ordinal=1,
        reading_order=1,
        resolution_state=StoryArcResolutionState.RESOLVED,
        source_kind=StoryArcSourceKind.MYLAR3,
        selected_for_import=True,
    )
    session.add(staged_entry)
    await session.flush()
    queued = await enqueue_import_story_arc_sync_work(
        session,
        job=job,
        library_file=library_file,
        membership=membership,
        story_arc=story_arc,
        imported_story_arc_id=staged_arc.id,
        imported_story_arc_entry_id=staged_entry.id,
        record_action=record_action,
    )
    assert queued.work is not None and queued.action is not None
    await session.flush()
    return _ImportPlacementContext(
        job_id=job.id,
        action_id=queued.action.id,
        work_id=queued.work.id,
        story_arc_id=story_arc.id,
        membership_id=membership.id,
        action_payload=dict(queued.action.payload),
        job=job,
        action=queued.action,
        work=queued.work,
        story_arc=story_arc,
        membership=membership,
        library_file=library_file,
        canonical_path=canonical_path,
    )


async def _rollback(session: AsyncSession, context: _ImportPlacementContext) -> None:
    async def refuse_series_delete(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Story Arc placement rollback reached series deletion")

    await rollback_action(
        session,
        action_id=context.action_id,
        action_type="story_arc_managed_placement_requested",
        payload=dict(context.action_payload),
        delete_series=refuse_series_delete,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        StoryArcSyncWorkState.QUEUED,
        StoryArcSyncWorkState.RETRY_WAIT,
        StoryArcSyncWorkState.FAILED,
    ],
)
async def test_unpublished_origin_work_is_cancelled_without_touching_canonical(
    db_session: AsyncSession,
    tmp_path: Path,
    state: StoryArcSyncWorkState,
) -> None:
    context = await _seed_import_placement(db_session, tmp_path)
    context.work.state = state
    context.work.next_attempt_at = (
        datetime.now(UTC) if state is StoryArcSyncWorkState.RETRY_WAIT else None
    )
    await db_session.flush()

    await _rollback(db_session, context)

    await db_session.refresh(context.work)
    assert context.work.state is StoryArcSyncWorkState.CANCELLED
    assert context.work.cancel_requested_at is not None
    assert context.work.claim_token is None
    assert context.work.claimed_at is None
    assert context.work.next_attempt_at is None
    assert context.work.last_result["rollback"]["status"] == "cancelled_before_publish"
    assert context.action.status is ImportJobActionStatus.ROLLED_BACK
    assert context.canonical_path.read_bytes() == b"canonical-library-file"
    assert await db_session.scalar(select(StoryArcPlacement.id)) is None


@pytest.mark.asyncio
async def test_completed_managed_placement_is_safely_removed_and_rollback_is_idempotent(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    context = await _seed_import_placement(db_session, tmp_path)
    synchronized = await StoryArcPlacementSyncService().sync_membership(
        db_session,
        context.story_arc_id,
        context.membership_id,
        import_provenance=StoryArcPlacementImportProvenance(
            import_job_id=context.job_id,
            import_action_id=context.action_id,
        ),
    )
    assert synchronized.placement is not None
    target = Path(synchronized.placement.placement_path)
    context.work.state = StoryArcSyncWorkState.COMPLETED
    context.work.last_result = {"schema_version": 1, "outcome": synchronized.outcome}
    await db_session.flush()

    await _rollback(db_session, context)
    await _rollback(db_session, context)

    assert not target.exists()
    assert context.canonical_path.read_bytes() == b"canonical-library-file"
    assert await db_session.get(StoryArcPlacement, synchronized.placement.id) is None
    await db_session.refresh(context.work)
    assert context.work.last_result["rollback"]["status"] == "managed_placement_removed"
    assert context.action.status is ImportJobActionStatus.ROLLED_BACK


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_state",
    [StoryArcSyncWorkState.FAILED, StoryArcSyncWorkState.CANCELLED],
)
async def test_terminal_work_with_published_owned_placement_is_safely_removed(
    db_session: AsyncSession,
    tmp_path: Path,
    terminal_state: StoryArcSyncWorkState,
) -> None:
    context = await _seed_import_placement(db_session, tmp_path)
    synchronized = await StoryArcPlacementSyncService().sync_membership(
        db_session,
        context.story_arc_id,
        context.membership_id,
        import_provenance=StoryArcPlacementImportProvenance(
            import_job_id=context.job_id,
            import_action_id=context.action_id,
        ),
    )
    assert synchronized.placement is not None
    target = Path(synchronized.placement.placement_path)
    context.work.state = terminal_state
    context.work.last_result = {"schema_version": 1, "outcome": synchronized.outcome}
    await db_session.flush()

    await _rollback(db_session, context)

    assert not target.exists()
    assert context.canonical_path.read_bytes() == b"canonical-library-file"
    assert await db_session.get(StoryArcPlacement, synchronized.placement.id) is None
    await db_session.refresh(context.work)
    assert context.work.state is StoryArcSyncWorkState.CANCELLED
    assert context.work.last_result["rollback"]["status"] == "managed_placement_removed"
    assert context.action.status is ImportJobActionStatus.ROLLED_BACK


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_state",
    [StoryArcSyncWorkState.FAILED, StoryArcSyncWorkState.CANCELLED],
)
async def test_terminal_unpublished_reservation_is_detached_only_when_target_is_absent(
    db_session: AsyncSession,
    tmp_path: Path,
    terminal_state: StoryArcSyncWorkState,
) -> None:
    context = await _seed_import_placement(db_session, tmp_path)
    target = tmp_path / "story-arcs" / "Court of Owls" / "001 - Batman 1.cbz"
    reservation = StoryArcPlacement(
        issue_story_arc_id=context.membership_id,
        library_file_id=context.library_file.id,
        library_root_id=context.story_arc.target_library_root_id,
        placement_path=str(target),
        mode=StoryArcPlacementMode.COPY,
        ownership=StoryArcPlacementOwnership.MANAGED,
        source_kind=StoryArcSourceKind.PULLBOX,
        source_import_job_id=context.job_id,
        creating_action_id=context.action_id,
        source_fingerprint={"schema_version": 1, "sha256": "source"},
        state=StoryArcPlacementState.FAILED,
        last_result={
            "schema_version": 1,
            "status": "failed",
            "error_code": "placement_execution_failed",
        },
        operation_token=None,
    )
    db_session.add(reservation)
    context.work.state = terminal_state
    await db_session.flush()
    reservation_id = int(reservation.id)

    await _rollback(db_session, context)

    assert await db_session.get(StoryArcPlacement, reservation_id) is None
    assert context.canonical_path.read_bytes() == b"canonical-library-file"
    await db_session.refresh(context.work)
    assert context.work.state is StoryArcSyncWorkState.CANCELLED
    assert context.work.last_result["rollback"]["status"] == "managed_placement_removed"
    assert context.action.status is ImportJobActionStatus.ROLLED_BACK


@pytest.mark.asyncio
async def test_terminal_unpublished_reservation_with_target_fails_closed(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    context = await _seed_import_placement(db_session, tmp_path)
    target = tmp_path / "story-arcs" / "Court of Owls" / "001 - Batman 1.cbz"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"unproven-user-or-partial-artifact")
    reservation = StoryArcPlacement(
        issue_story_arc_id=context.membership_id,
        library_file_id=context.library_file.id,
        library_root_id=context.story_arc.target_library_root_id,
        placement_path=str(target),
        mode=StoryArcPlacementMode.COPY,
        ownership=StoryArcPlacementOwnership.MANAGED,
        source_kind=StoryArcSourceKind.PULLBOX,
        source_import_job_id=context.job_id,
        creating_action_id=context.action_id,
        source_fingerprint={"schema_version": 1, "sha256": "source"},
        state=StoryArcPlacementState.DRIFTED,
        last_result={
            "schema_version": 1,
            "status": "failed",
            "error_code": "placement_execution_failed",
        },
        operation_token=None,
    )
    db_session.add(reservation)
    context.work.state = StoryArcSyncWorkState.FAILED
    await db_session.flush()

    with pytest.raises(StoryArcPlacementIntegrationError) as blocked:
        await _rollback(db_session, context)

    assert blocked.value.code == "fingerprint_mismatch"
    assert await db_session.get(StoryArcPlacement, reservation.id) is reservation
    assert target.read_bytes() == b"unproven-user-or-partial-artifact"
    await db_session.refresh(context.action)
    await db_session.refresh(context.work)
    assert context.action.status is ImportJobActionStatus.COMPLETED
    assert context.work.last_result["rollback"]["status"] == "placement_removal_prepared"
    assert context.canonical_path.read_bytes() == b"canonical-library-file"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_state",
    [StoryArcSyncWorkState.FAILED, StoryArcSyncWorkState.CANCELLED],
)
async def test_terminal_published_checkpoint_takes_over_token_and_removes_owned_target(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_state: StoryArcSyncWorkState,
) -> None:
    import pullbox.services.story_arc_placement_integration as integration

    context = await _seed_import_placement(db_session, tmp_path)
    synchronized = await StoryArcPlacementSyncService().sync_membership(
        db_session,
        context.story_arc_id,
        context.membership_id,
        import_provenance=StoryArcPlacementImportProvenance(
            import_job_id=context.job_id,
            import_action_id=context.action_id,
        ),
    )
    assert synchronized.placement is not None
    placement = await db_session.get(StoryArcPlacement, synchronized.placement.id)
    assert placement is not None
    placement_id = int(placement.id)
    target = Path(placement.placement_path)
    target_fingerprint = dict(placement.last_result)["target_fingerprint"]
    abandoned_token = "abandoned-publish-token"
    placement.state = StoryArcPlacementState.MISSING
    placement.operation_token = abandoned_token
    placement.last_result = {
        "schema_version": 1,
        "status": "published_pending_reconcile",
        "operation_token": abandoned_token,
        "target_fingerprint": target_fingerprint,
    }
    context.work.state = terminal_state
    context.work.claim_token = None
    context.work.claimed_at = None
    await db_session.flush()

    filesystem_call = integration._run_filesystem_call
    saw_atomic_transfer = False

    async def assert_atomic_transfer(
        call: Callable[[], object],
        *,
        heartbeat: Callable[[], Awaitable[None]] | None = None,
    ) -> tuple[object, bool]:
        nonlocal saw_atomic_transfer
        reserved = await db_session.get(StoryArcPlacement, placement_id)
        assert reserved is not None
        assert reserved.operation_token not in {None, abandoned_token}
        assert reserved.last_result["status"] == "remove_prepared"
        assert reserved.last_result["operation_token"] == reserved.operation_token
        saw_atomic_transfer = True
        return await filesystem_call(call, heartbeat=heartbeat)

    monkeypatch.setattr(integration, "_run_filesystem_call", assert_atomic_transfer)
    await _rollback(db_session, context)

    assert saw_atomic_transfer is True
    assert not target.exists()
    assert await db_session.get(StoryArcPlacement, placement_id) is None
    assert context.canonical_path.read_bytes() == b"canonical-library-file"
    await db_session.refresh(context.work)
    assert context.work.last_result["rollback"]["status"] == "managed_placement_removed"
    assert context.action.status is ImportJobActionStatus.ROLLED_BACK


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_state",
    [StoryArcSyncWorkState.FAILED, StoryArcSyncWorkState.CANCELLED],
)
async def test_terminal_referenced_published_checkpoint_detaches_database_only(
    db_session: AsyncSession,
    tmp_path: Path,
    terminal_state: StoryArcSyncWorkState,
) -> None:
    context = await _seed_import_placement(db_session, tmp_path)
    synchronized = await StoryArcPlacementSyncService().sync_membership(
        db_session,
        context.story_arc_id,
        context.membership_id,
        import_provenance=StoryArcPlacementImportProvenance(
            import_job_id=context.job_id,
            import_action_id=context.action_id,
        ),
    )
    assert synchronized.placement is not None
    placement = await db_session.get(StoryArcPlacement, synchronized.placement.id)
    assert placement is not None
    target = Path(placement.placement_path)
    target_fingerprint = dict(placement.last_result)["target_fingerprint"]
    abandoned_token = "referenced-abandoned-publish-token"
    placement.mode = StoryArcPlacementMode.REFERENCE_ONLY
    placement.ownership = StoryArcPlacementOwnership.REFERENCED
    placement.symlink_style = None
    placement.state = StoryArcPlacementState.MISSING
    placement.operation_token = abandoned_token
    placement.last_result = {
        "schema_version": 1,
        "status": "published_pending_reconcile",
        "operation_token": abandoned_token,
        "target_fingerprint": target_fingerprint,
    }
    context.work.state = terminal_state
    context.work.claim_token = None
    context.work.claimed_at = None
    await db_session.flush()

    await _rollback(db_session, context)

    assert target.read_bytes() == context.canonical_path.read_bytes()
    assert await db_session.get(StoryArcPlacement, placement.id) is None
    await db_session.refresh(context.work)
    assert context.work.last_result["rollback"]["status"] == "referenced_placement_detached"
    assert context.action.status is ImportJobActionStatus.ROLLED_BACK


@pytest.mark.asyncio
@pytest.mark.parametrize("target_present", [False, True])
async def test_terminal_referenced_checkpoint_without_fingerprint_detaches_only_database(
    db_session: AsyncSession,
    tmp_path: Path,
    target_present: bool,
) -> None:
    context = await _seed_import_placement(db_session, tmp_path)
    synchronized = await StoryArcPlacementSyncService().sync_membership(
        db_session,
        context.story_arc_id,
        context.membership_id,
        import_provenance=StoryArcPlacementImportProvenance(
            import_job_id=context.job_id,
            import_action_id=context.action_id,
        ),
    )
    assert synchronized.placement is not None
    placement = await db_session.get(StoryArcPlacement, synchronized.placement.id)
    assert placement is not None
    target = Path(placement.placement_path)
    placement.mode = StoryArcPlacementMode.REFERENCE_ONLY
    placement.ownership = StoryArcPlacementOwnership.REFERENCED
    placement.symlink_style = None
    placement.state = StoryArcPlacementState.DRIFTED
    placement.operation_token = None
    placement.last_result = {
        "schema_version": 1,
        "status": "failed",
        "error_code": "placement_execution_failed",
    }
    if not target_present:
        target.unlink()
    context.work.state = StoryArcSyncWorkState.FAILED
    await db_session.flush()

    await _rollback(db_session, context)

    assert target.exists() is target_present
    assert await db_session.get(StoryArcPlacement, placement.id) is None
    assert context.canonical_path.read_bytes() == b"canonical-library-file"
    await db_session.refresh(context.work)
    assert context.work.last_result["rollback"]["status"] == "referenced_placement_detached"


@pytest.mark.asyncio
async def test_published_checkpoint_with_mismatched_token_fails_closed(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    context = await _seed_import_placement(db_session, tmp_path)
    synchronized = await StoryArcPlacementSyncService().sync_membership(
        db_session,
        context.story_arc_id,
        context.membership_id,
        import_provenance=StoryArcPlacementImportProvenance(
            import_job_id=context.job_id,
            import_action_id=context.action_id,
        ),
    )
    assert synchronized.placement is not None
    placement = await db_session.get(StoryArcPlacement, synchronized.placement.id)
    assert placement is not None
    target = Path(placement.placement_path)
    target_fingerprint = dict(placement.last_result)["target_fingerprint"]
    placement.operation_token = "row-operation-token"
    placement.last_result = {
        "schema_version": 1,
        "status": "published_pending_reconcile",
        "operation_token": "different-checkpoint-token",
        "target_fingerprint": target_fingerprint,
    }
    context.work.state = StoryArcSyncWorkState.CANCELLED
    context.work.claim_token = None
    await db_session.flush()

    with pytest.raises(ValueError, match="not an exact published checkpoint"):
        await _rollback(db_session, context)

    assert target.read_bytes() == context.canonical_path.read_bytes()
    retained = await db_session.get(StoryArcPlacement, placement.id)
    assert retained is not None
    assert retained.operation_token == "row-operation-token"
    assert context.action.status is ImportJobActionStatus.COMPLETED


@pytest.mark.asyncio
async def test_terminal_claimed_published_checkpoint_defers_takeover(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    context = await _seed_import_placement(db_session, tmp_path)
    synchronized = await StoryArcPlacementSyncService().sync_membership(
        db_session,
        context.story_arc_id,
        context.membership_id,
        import_provenance=StoryArcPlacementImportProvenance(
            import_job_id=context.job_id,
            import_action_id=context.action_id,
        ),
    )
    assert synchronized.placement is not None
    placement = await db_session.get(StoryArcPlacement, synchronized.placement.id)
    assert placement is not None
    target = Path(placement.placement_path)
    target_fingerprint = dict(placement.last_result)["target_fingerprint"]
    placement.operation_token = "live-published-token"
    placement.last_result = {
        "schema_version": 1,
        "status": "published_pending_reconcile",
        "operation_token": "live-published-token",
        "target_fingerprint": target_fingerprint,
    }
    context.work.state = StoryArcSyncWorkState.CANCELLED
    context.work.claim_token = "live-worker-claim"
    context.work.claimed_at = datetime.now(UTC)
    await db_session.flush()

    with pytest.raises(StoryArcManagedPlacementRollbackDeferredError):
        await _rollback(db_session, context)

    assert target.read_bytes() == context.canonical_path.read_bytes()
    retained = await db_session.get(StoryArcPlacement, placement.id)
    assert retained is not None
    assert retained.operation_token == "live-published-token"
    assert context.action.status is ImportJobActionStatus.COMPLETED


@pytest.mark.asyncio
async def test_completed_reference_demotion_detaches_database_only(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    context = await _seed_import_placement(db_session, tmp_path)
    synchronized = await StoryArcPlacementSyncService().sync_membership(
        db_session,
        context.story_arc_id,
        context.membership_id,
        import_provenance=StoryArcPlacementImportProvenance(
            import_job_id=context.job_id,
            import_action_id=context.action_id,
        ),
    )
    assert synchronized.placement is not None
    target = Path(synchronized.placement.placement_path)
    placement = await db_session.get(StoryArcPlacement, synchronized.placement.id)
    assert placement is not None
    placement.mode = StoryArcPlacementMode.REFERENCE_ONLY
    placement.ownership = StoryArcPlacementOwnership.REFERENCED
    placement.symlink_style = None
    context.work.state = StoryArcSyncWorkState.COMPLETED
    await db_session.flush()

    await _rollback(db_session, context)

    assert target.read_bytes() == context.canonical_path.read_bytes()
    assert await db_session.get(StoryArcPlacement, placement.id) is None
    await db_session.refresh(context.work)
    assert context.work.last_result["rollback"]["status"] == "referenced_placement_detached"


@pytest.mark.asyncio
async def test_running_work_requests_cooperative_cancel_and_defers_later_rollback(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    context = await _seed_import_placement(db_session, tmp_path)
    context.work.state = StoryArcSyncWorkState.RUNNING
    context.work.claim_token = "active-worker-token"
    context.work.claimed_at = datetime.now(UTC)
    await db_session.commit()

    with pytest.raises(StoryArcManagedPlacementRollbackDeferredError) as deferred:
        await _rollback(db_session, context)

    assert deferred.value.work_id == context.work_id
    await db_session.refresh(context.work)
    await db_session.refresh(context.action)
    assert context.work.state is StoryArcSyncWorkState.RUNNING
    assert context.work.cancel_requested_at is not None
    assert context.action.status is ImportJobActionStatus.COMPLETED
    assert context.canonical_path.read_bytes() == b"canonical-library-file"


@pytest.mark.asyncio
async def test_completed_work_without_placement_fails_closed(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    context = await _seed_import_placement(db_session, tmp_path)
    context.work.state = StoryArcSyncWorkState.COMPLETED
    await db_session.flush()

    with pytest.raises(ValueError, match="placement evidence is missing"):
        await _rollback(db_session, context)

    assert context.action.status is ImportJobActionStatus.COMPLETED
    assert context.canonical_path.read_bytes() == b"canonical-library-file"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("placement_ownership", "membership_status", "rollback_status"),
    [
        (
            StoryArcPlacementOwnership.MANAGED,
            "placement_removed",
            "managed_placement_removed",
        ),
        (
            StoryArcPlacementOwnership.REFERENCED,
            "placement_reference_removed",
            "referenced_placement_detached",
        ),
    ],
)
async def test_prepared_removal_recovers_from_matching_membership_checkpoint(
    db_session: AsyncSession,
    tmp_path: Path,
    placement_ownership: StoryArcPlacementOwnership,
    membership_status: str,
    rollback_status: str,
) -> None:
    context = await _seed_import_placement(db_session, tmp_path)
    synchronized = await StoryArcPlacementSyncService().sync_membership(
        db_session,
        context.story_arc_id,
        context.membership_id,
        import_provenance=StoryArcPlacementImportProvenance(
            import_job_id=context.job_id,
            import_action_id=context.action_id,
        ),
    )
    assert synchronized.placement is not None
    placement = await db_session.get(StoryArcPlacement, synchronized.placement.id)
    membership = await db_session.get(IssueStoryArc, context.membership_id)
    work = await db_session.get(StoryArcSyncWork, context.work_id)
    assert placement is not None and membership is not None and work is not None
    placement_id = int(placement.id)
    target = Path(placement.placement_path)
    work.state = StoryArcSyncWorkState.COMPLETED
    work.last_result = {
        "rollback": {
            "schema_version": 1,
            "status": "placement_removal_prepared",
            "import_job_id": context.job_id,
            "import_action_id": context.action_id,
            "sync_work_id": context.work_id,
            "membership_id": context.membership_id,
            "desired_generation": str(context.action_payload["desired_generation"]),
            "placement_id": placement_id,
            "placement_ownership": placement_ownership.value,
        }
    }
    membership.sync_eligible = False
    membership.last_materialization_result = {
        "schema_version": 1,
        "status": membership_status,
        "placement_id": placement_id,
        "artifact_removed": placement_ownership is StoryArcPlacementOwnership.MANAGED,
        "canonical_preserved": True,
        **(
            {"referenced_artifact_preserved": True}
            if placement_ownership is StoryArcPlacementOwnership.REFERENCED
            else {}
        ),
    }
    if placement_ownership is StoryArcPlacementOwnership.MANAGED:
        target.unlink()
    await db_session.delete(placement)
    await db_session.commit()

    await _rollback(db_session, context)

    assert target.exists() is (placement_ownership is StoryArcPlacementOwnership.REFERENCED)
    assert context.canonical_path.read_bytes() == b"canonical-library-file"
    await db_session.refresh(context.work)
    await db_session.refresh(context.action)
    assert context.work.last_result["rollback"]["status"] == rollback_status
    assert context.action.status is ImportJobActionStatus.ROLLED_BACK


@pytest.mark.asyncio
async def test_prepared_removal_without_matching_membership_checkpoint_fails_closed(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    context = await _seed_import_placement(db_session, tmp_path)
    synchronized = await StoryArcPlacementSyncService().sync_membership(
        db_session,
        context.story_arc_id,
        context.membership_id,
        import_provenance=StoryArcPlacementImportProvenance(
            import_job_id=context.job_id,
            import_action_id=context.action_id,
        ),
    )
    assert synchronized.placement is not None
    placement = await db_session.get(StoryArcPlacement, synchronized.placement.id)
    membership = await db_session.get(IssueStoryArc, context.membership_id)
    work = await db_session.get(StoryArcSyncWork, context.work_id)
    assert placement is not None and membership is not None and work is not None
    placement_id = int(placement.id)
    target = Path(placement.placement_path)
    target.unlink()
    work.state = StoryArcSyncWorkState.COMPLETED
    work.last_result = {
        "rollback": {
            "schema_version": 1,
            "status": "placement_removal_prepared",
            "import_job_id": context.job_id,
            "import_action_id": context.action_id,
            "sync_work_id": context.work_id,
            "membership_id": context.membership_id,
            "desired_generation": str(context.action_payload["desired_generation"]),
            "placement_id": placement_id,
            "placement_ownership": StoryArcPlacementOwnership.MANAGED.value,
        }
    }
    membership.sync_eligible = False
    membership.last_materialization_result = {
        "schema_version": 1,
        "status": "placement_removed",
        "placement_id": placement_id + 1,
        "artifact_removed": True,
        "canonical_preserved": True,
    }
    await db_session.delete(placement)
    await db_session.commit()

    with pytest.raises(ValueError, match="membership removal checkpoint"):
        await _rollback(db_session, context)

    await db_session.refresh(context.action)
    assert context.action.status is ImportJobActionStatus.COMPLETED
    assert context.canonical_path.read_bytes() == b"canonical-library-file"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_state",
    [StoryArcSyncWorkState.COMPLETED, StoryArcSyncWorkState.FAILED],
)
async def test_noncurrent_action_owned_placement_fails_closed(
    db_session: AsyncSession,
    tmp_path: Path,
    terminal_state: StoryArcSyncWorkState,
) -> None:
    context = await _seed_import_placement(db_session, tmp_path)
    context.work.state = terminal_state
    ambiguous = StoryArcPlacement(
        issue_story_arc_id=context.membership_id,
        library_file_id=context.library_file.id,
        library_root_id=context.story_arc.target_library_root_id,
        placement_path=str(tmp_path / "story-arcs" / "ambiguous.cbz"),
        mode=StoryArcPlacementMode.COPY,
        ownership=StoryArcPlacementOwnership.MANAGED,
        source_kind=StoryArcSourceKind.PULLBOX,
        source_import_job_id=context.job_id,
        creating_action_id=context.action_id,
        source_fingerprint={"schema_version": 1},
        state=StoryArcPlacementState.MISSING,
        last_result={"schema_version": 1, "status": "prepared"},
        operation_token="prepared-operation",
    )
    db_session.add(ambiguous)
    await db_session.flush()

    with pytest.raises(ValueError, match="not an exact published checkpoint"):
        await _rollback(db_session, context)

    assert await db_session.get(StoryArcPlacement, ambiguous.id) is ambiguous
    assert context.action.status is ImportJobActionStatus.COMPLETED
    if terminal_state is StoryArcSyncWorkState.FAILED:
        await db_session.refresh(context.work)
        assert context.work.last_result["rollback"]["status"] == "work_fenced_for_rollback"
    assert context.canonical_path.read_bytes() == b"canonical-library-file"


@pytest.mark.asyncio
async def test_generation_change_refuses_rollback(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    context = await _seed_import_placement(db_session, tmp_path)
    payload = dict(context.action.payload)
    payload["desired_generation"] = "0" * 64
    context.action.payload = payload
    context.action_payload["desired_generation"] = "0" * 64
    await db_session.flush()

    with pytest.raises(ValueError, match="generation changed"):
        await _rollback(db_session, context)

    assert context.work.state is StoryArcSyncWorkState.QUEUED
    assert context.action.status is ImportJobActionStatus.COMPLETED
    assert context.canonical_path.read_bytes() == b"canonical-library-file"


@pytest.mark.asyncio
async def test_staged_entry_provenance_change_refuses_rollback(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    context = await _seed_import_placement(db_session, tmp_path)
    entry_id = int(context.action_payload["imported_story_arc_entry_id"])
    staged_entry = await db_session.get(ImportedStoryArcEntry, entry_id)
    assert staged_entry is not None
    staged_entry.materialized_membership_id = None
    await db_session.flush()

    with pytest.raises(ValueError, match="staged provenance changed"):
        await _rollback(db_session, context)

    assert context.work.state is StoryArcSyncWorkState.QUEUED
    assert context.action.status is ImportJobActionStatus.COMPLETED
    assert context.canonical_path.read_bytes() == b"canonical-library-file"
