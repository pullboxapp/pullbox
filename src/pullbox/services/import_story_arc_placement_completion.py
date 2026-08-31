"""Truthful database-only completion for import-owned Story Arc placements."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NoReturn, cast

from sqlalchemy import and_, case, func, or_, select, update

from pullbox.core.exceptions import (
    JobCancelledError,
    JobPausedError,
    NotFoundError,
    ValidationError,
)
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportJob,
    ImportJobAction,
    ImportJobActionStatus,
    ImportJobStatus,
)
from pullbox.models.library import LibraryFile
from pullbox.models.story_arc import (
    ImportedStoryArcStatus,
    IssueStoryArc,
    StoryArcPlacement,
    StoryArcPlacementMode,
    StoryArcPlacementOwnership,
    StoryArcPlacementState,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.models.story_arc_sync import StoryArcSyncWork, StoryArcSyncWorkState

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession


_PHASE = "story_arc_placements"
_ACTION_TYPE = "story_arc_managed_placement_requested"
_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "sync_work_id",
        "membership_id",
        "desired_generation",
        "imported_story_arc_id",
        "imported_story_arc_entry_id",
        "source_import_job_id",
    }
)
_ORIGIN_PAGE_SIZE = 1_000
_ACTIVE_STATUSES = frozenset({ImportJobStatus.IMPORTING, ImportJobStatus.STALLED})
_PENDING_STATES = frozenset(
    {
        StoryArcSyncWorkState.QUEUED,
        StoryArcSyncWorkState.RUNNING,
        StoryArcSyncWorkState.RETRY_WAIT,
    }
)
_MANAGED_MODES = frozenset(
    {
        StoryArcPlacementMode.COPY,
        StoryArcPlacementMode.HARDLINK,
        StoryArcPlacementMode.SYMLINK,
    }
)


class ImportStoryArcPlacementCompletionState(enum.StrEnum):
    """One import-placement completion evaluation result."""

    PENDING = "pending"
    STALLED = "stalled"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class ImportStoryArcPlacementCounts:
    """Sanitized aggregate counts for every durable work state."""

    queued: int = 0
    running: int = 0
    retry_wait: int = 0
    failed: int = 0
    completed: int = 0
    cancelled: int = 0

    @property
    def total(self) -> int:
        return sum(self.as_dict().values())

    def as_dict(self) -> dict[str, int]:
        return {
            "queued": self.queued,
            "running": self.running,
            "retry_wait": self.retry_wait,
            "failed": self.failed,
            "completed": self.completed,
            "cancelled": self.cancelled,
        }


@dataclass(frozen=True, slots=True)
class ImportStoryArcPlacementCompletionOutcome:
    """Database-only finalizer outcome safe for logs and progress responses."""

    job_id: int
    state: ImportStoryArcPlacementCompletionState
    counts: ImportStoryArcPlacementCounts
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class _OriginAggregate:
    action_count: int
    invalid_count: int
    counts: ImportStoryArcPlacementCounts


_ERRORS = {
    "story_arc_placement_origin_invalid": (
        "Story-arc placement recovery data is incomplete. Roll back the import."
    ),
    "story_arc_placement_work_failed": (
        "One or more story-arc placements failed. Retry the placement work or roll back the import."
    ),
    "story_arc_placement_work_cancelled": (
        "Story-arc placement work ended unexpectedly. Retry the placement work or "
        "roll back the import."
    ),
    "story_arc_placement_evidence_invalid": (
        "Story-arc placement verification is incomplete. Roll back the import."
    ),
}


async def finalize_import_story_arc_placements(
    session: AsyncSession,
    job_id: int,
    *,
    now: datetime | None = None,
) -> ImportStoryArcPlacementCompletionOutcome:
    """Project one import's durable placement work into a truthful job state.

    The caller owns the surrounding transaction. This function locks and flushes
    the job row, but never commits and never reads provider or filesystem data.
    """
    job = await session.scalar(select(ImportJob).where(ImportJob.id == job_id).with_for_update())
    if job is None:
        raise NotFoundError("ImportJob", job_id)

    snapshot = dict(job.progress_snapshot or {})
    if job.status is ImportJobStatus.COMPLETED and snapshot.get("phase") == "done":
        return ImportStoryArcPlacementCompletionOutcome(
            job_id=job.id,
            state=ImportStoryArcPlacementCompletionState.COMPLETED,
            counts=_counts_from_snapshot(snapshot),
        )
    if job.status not in _ACTIVE_STATUSES or snapshot.get("phase") != _PHASE:
        raise ValidationError("Import job is not awaiting story-arc placements.")
    if job.control_request is not ImportControlRequest.NONE:
        raise ValidationError("Import job has an active control request.")

    expected_total = _positive_int(snapshot.get("story_arc_placements_total"))
    origin = await _aggregate_origin_work(session, job.id)
    invalid_origin = (
        expected_total is None
        or origin.action_count != expected_total
        or origin.invalid_count != 0
        or origin.counts.total != origin.action_count
    )
    if invalid_origin:
        return await _stall(
            session,
            job,
            snapshot,
            origin.counts,
            expected_total=expected_total,
            error_code="story_arc_placement_origin_invalid",
        )
    assert expected_total is not None
    if origin.counts.failed:
        return await _stall(
            session,
            job,
            snapshot,
            origin.counts,
            expected_total=expected_total,
            error_code="story_arc_placement_work_failed",
        )
    if origin.counts.cancelled:
        return await _stall(
            session,
            job,
            snapshot,
            origin.counts,
            expected_total=expected_total,
            error_code="story_arc_placement_work_cancelled",
        )
    if any(origin.counts.as_dict()[state.value] for state in _PENDING_STATES):
        return await _mark_pending(
            session,
            job,
            snapshot,
            origin.counts,
            expected_total=expected_total,
        )
    invalid_placements = await _count_invalid_completed_placements(session, job.id)
    if invalid_placements:
        return await _stall(
            session,
            job,
            snapshot,
            origin.counts,
            expected_total=expected_total,
            error_code="story_arc_placement_evidence_invalid",
        )
    if origin.counts.completed != expected_total:
        return await _stall(
            session,
            job,
            snapshot,
            origin.counts,
            expected_total=expected_total,
            error_code="story_arc_placement_origin_invalid",
        )
    return await _complete(
        session,
        job,
        snapshot,
        origin.counts,
        completed_at=now or datetime.now(UTC),
    )


async def inspect_import_story_arc_placement_origin(
    session: AsyncSession,
    job_id: int,
) -> ImportStoryArcPlacementCounts:
    """Return authoritative typed work counts before publishing the wait phase."""
    origin = await _aggregate_origin_work(session, job_id)
    if origin.invalid_count or origin.counts.total != origin.action_count:
        raise ValidationError("Import Story Arc placement origin evidence is incomplete.")
    return origin.counts


async def seal_import_story_arc_placement_origin(
    session: AsyncSession,
    job_id: int,
) -> ImportStoryArcPlacementCounts:
    """Make fully journaled import work claimable in the caller's final transaction."""
    fence = cast(
        "CursorResult[Any]",
        await session.execute(
            update(ImportJob)
            .where(
                ImportJob.id == job_id,
                ImportJob.status == ImportJobStatus.IMPORTING,
                ImportJob.control_request == ImportControlRequest.NONE,
                ImportJob.progress_snapshot["phase"].as_string() == "story_arcs",
            )
            .values(status=ImportJobStatus.IMPORTING)
            .execution_options(synchronize_session=False)
        ),
    )
    if fence.rowcount != 1:
        await _raise_seal_fence_control(session, job_id)

    counts = await inspect_import_story_arc_placement_origin(session, job_id)
    invalid_held = int(
        await session.scalar(
            select(func.count(StoryArcSyncWork.id)).where(
                StoryArcSyncWork.origin_import_job_id == job_id,
                StoryArcSyncWork.claimable.is_(False),
                StoryArcSyncWork.state != StoryArcSyncWorkState.QUEUED,
            )
        )
        or 0
    )
    if invalid_held:
        raise ValidationError("Held import Story Arc work changed before it was sealed.")
    await session.execute(
        update(StoryArcSyncWork)
        .where(
            StoryArcSyncWork.origin_import_job_id == job_id,
            StoryArcSyncWork.claimable.is_(False),
            StoryArcSyncWork.state == StoryArcSyncWorkState.QUEUED,
        )
        .values(claimable=True)
    )
    return counts


async def _raise_seal_fence_control(session: AsyncSession, job_id: int) -> NoReturn:
    """Translate a lost final-transition CAS into the cooperative runner path."""
    row = (
        await session.execute(
            select(
                ImportJob.status,
                ImportJob.control_request,
                ImportJob.progress_snapshot,
            ).where(ImportJob.id == job_id)
        )
    ).one_or_none()
    if row is None:
        raise JobCancelledError(f"Import job {job_id} was cancelled.")
    status, control_request, snapshot = row
    if control_request is ImportControlRequest.CANCEL or status in {
        ImportJobStatus.CANCELLING,
        ImportJobStatus.CANCELLED,
        ImportJobStatus.ROLLING_BACK,
        ImportJobStatus.ROLLED_BACK,
    }:
        raise JobCancelledError(f"Import job {job_id} was cancelled.")
    if control_request is ImportControlRequest.PAUSE or status in {
        ImportJobStatus.PAUSING,
        ImportJobStatus.PAUSED,
    }:
        raise JobPausedError(f"Import job {job_id} was paused.")
    phase = str(dict(snapshot or {}).get("phase") or "")
    raise ValidationError(
        "Import job cannot publish Story Arc placements from "
        f"status={status.value}, phase={phase or 'unknown'}."
    )


async def _aggregate_origin_work(session: AsyncSession, job_id: int) -> _OriginAggregate:
    action_count = invalid_count = 0
    state_counts = {state: 0 for state in StoryArcSyncWorkState}
    after_action_id = 0
    while True:
        rows = (
            await session.execute(
                _origin_work_page_statement(
                    job_id,
                    after_action_id=after_action_id,
                    limit=_ORIGIN_PAGE_SIZE,
                )
            )
        ).all()
        if not rows:
            break
        for row in rows:
            action_count += 1
            if row.work_state in state_counts:
                state_counts[row.work_state] += 1
            if not _origin_row_is_valid(row, job_id=job_id):
                invalid_count += 1
        after_action_id = int(rows[-1].action_id)

    return _OriginAggregate(
        action_count=action_count,
        invalid_count=invalid_count,
        counts=ImportStoryArcPlacementCounts(
            queued=state_counts[StoryArcSyncWorkState.QUEUED],
            running=state_counts[StoryArcSyncWorkState.RUNNING],
            retry_wait=state_counts[StoryArcSyncWorkState.RETRY_WAIT],
            failed=state_counts[StoryArcSyncWorkState.FAILED],
            completed=state_counts[StoryArcSyncWorkState.COMPLETED],
            cancelled=state_counts[StoryArcSyncWorkState.CANCELLED],
        ),
    )


def _origin_work_page_statement(job_id: int, *, after_action_id: int, limit: int) -> Any:
    """Build a bounded origin page without casting untrusted JSON in SQL."""
    candidate = or_(
        ImportJobAction.phase == _PHASE,
        ImportJobAction.action_type == _ACTION_TYPE,
    )
    return (
        select(
            ImportJobAction.id.label("action_id"),
            ImportJobAction.phase.label("action_phase"),
            ImportJobAction.action_type.label("action_type"),
            ImportJobAction.status.label("action_status"),
            ImportJobAction.payload.label("action_payload"),
            StoryArcSyncWork.id.label("work_id"),
            StoryArcSyncWork.origin_import_action_id.label("work_action_id"),
            StoryArcSyncWork.origin_import_job_id.label("work_job_id"),
            StoryArcSyncWork.origin_imported_story_arc_id.label("work_imported_arc_id"),
            StoryArcSyncWork.origin_imported_story_arc_entry_id.label("work_imported_entry_id"),
            StoryArcSyncWork.issue_story_arc_id.label("work_membership_id"),
            StoryArcSyncWork.desired_generation.label("work_generation"),
            StoryArcSyncWork.state.label("work_state"),
            IssueStoryArc.story_arc_id.label("membership_arc_id"),
            LibraryFile.issue_id.label("library_issue_id"),
            ImportedStoryArc.id.label("staged_arc_id"),
            ImportedStoryArc.import_job_id.label("staged_arc_job_id"),
            ImportedStoryArc.status.label("staged_arc_status"),
            ImportedStoryArc.materialized_story_arc_id.label("staged_materialized_arc_id"),
            ImportedStoryArcEntry.id.label("staged_entry_id"),
            ImportedStoryArcEntry.imported_story_arc_id.label("staged_entry_arc_id"),
            ImportedStoryArcEntry.materialized_membership_id.label("staged_entry_membership_id"),
            ImportedStoryArcEntry.matched_issue_id.label("staged_entry_issue_id"),
            ImportedStoryArcEntry.resolution_state.label("staged_entry_resolution_state"),
        )
        .select_from(ImportJobAction)
        .outerjoin(
            StoryArcSyncWork,
            StoryArcSyncWork.origin_import_action_id == ImportJobAction.id,
        )
        .outerjoin(
            IssueStoryArc,
            IssueStoryArc.id == StoryArcSyncWork.issue_story_arc_id,
        )
        .outerjoin(
            LibraryFile,
            LibraryFile.id == StoryArcSyncWork.library_file_id,
        )
        .outerjoin(
            ImportedStoryArc,
            ImportedStoryArc.id == StoryArcSyncWork.origin_imported_story_arc_id,
        )
        .outerjoin(
            ImportedStoryArcEntry,
            ImportedStoryArcEntry.id == StoryArcSyncWork.origin_imported_story_arc_entry_id,
        )
        .where(
            ImportJobAction.import_job_id == job_id,
            ImportJobAction.id > after_action_id,
            candidate,
        )
        .order_by(ImportJobAction.id.asc())
        .limit(limit)
    )


def _origin_row_is_valid(row: Any, *, job_id: int) -> bool:
    """Validate exact typed provenance and the journal payload in Python."""
    values = row._mapping
    payload_value = values["action_payload"]
    payload = payload_value if isinstance(payload_value, dict) else {}
    work_id = _positive_int(values["work_id"])
    membership_id = _positive_int(values["work_membership_id"])
    imported_arc_id = _positive_int(values["work_imported_arc_id"])
    imported_entry_id = _positive_int(values["work_imported_entry_id"])
    generation = values["work_generation"]
    return bool(
        values["action_phase"] == _PHASE
        and values["action_type"] == _ACTION_TYPE
        and values["action_status"] is ImportJobActionStatus.COMPLETED
        and work_id is not None
        and values["work_action_id"] == values["action_id"]
        and values["work_job_id"] == job_id
        and membership_id is not None
        and imported_arc_id is not None
        and imported_entry_id is not None
        and isinstance(generation, str)
        and len(generation) == 64
        and set(payload) == _PAYLOAD_KEYS
        and _positive_int(payload.get("schema_version")) == 1
        and _positive_int(payload.get("sync_work_id")) == work_id
        and _positive_int(payload.get("membership_id")) == membership_id
        and payload.get("desired_generation") == generation
        and _positive_int(payload.get("imported_story_arc_id")) == imported_arc_id
        and _positive_int(payload.get("imported_story_arc_entry_id")) == imported_entry_id
        and _positive_int(payload.get("source_import_job_id")) == job_id
        and values["staged_arc_id"] == imported_arc_id
        and values["staged_arc_job_id"] == job_id
        and values["staged_arc_status"] is ImportedStoryArcStatus.IMPORTED
        and values["staged_materialized_arc_id"] == values["membership_arc_id"]
        and values["staged_entry_id"] == imported_entry_id
        and values["staged_entry_arc_id"] == imported_arc_id
        and values["staged_entry_membership_id"] == membership_id
        and values["staged_entry_issue_id"] == values["library_issue_id"]
        and values["staged_entry_resolution_state"] is StoryArcResolutionState.RESOLVED
    )


async def _count_invalid_completed_placements(session: AsyncSession, job_id: int) -> int:
    valid_placement = and_(
        StoryArcPlacement.source_import_job_id == job_id,
        StoryArcPlacement.creating_action_id == ImportJobAction.id,
        StoryArcPlacement.issue_story_arc_id == StoryArcSyncWork.issue_story_arc_id,
        StoryArcPlacement.library_file_id == StoryArcSyncWork.library_file_id,
        StoryArcPlacement.ownership == StoryArcPlacementOwnership.MANAGED,
        StoryArcPlacement.mode.in_(_MANAGED_MODES),
        StoryArcPlacement.state == StoryArcPlacementState.CURRENT,
        StoryArcPlacement.source_kind == StoryArcSourceKind.PULLBOX,
        StoryArcPlacement.policy_schema_version == StoryArcSyncWork.policy_schema_version,
        StoryArcPlacement.rendered_reading_order == StoryArcSyncWork.membership_sequence,
        StoryArcPlacement.operation_token.is_(None),
        StoryArcPlacement.last_result["status"].as_string() == "complete",
    )
    per_action = (
        select(
            ImportJobAction.id.label("action_id"),
            func.count(StoryArcPlacement.id).label("placement_count"),
            func.coalesce(
                func.sum(case((valid_placement, 1), else_=0)),
                0,
            ).label("valid_count"),
        )
        .select_from(ImportJobAction)
        .join(
            StoryArcSyncWork,
            StoryArcSyncWork.origin_import_action_id == ImportJobAction.id,
        )
        .outerjoin(
            StoryArcPlacement,
            StoryArcPlacement.creating_action_id == ImportJobAction.id,
        )
        .where(
            ImportJobAction.import_job_id == job_id,
            ImportJobAction.phase == _PHASE,
            ImportJobAction.action_type == _ACTION_TYPE,
            StoryArcSyncWork.origin_import_job_id == job_id,
            StoryArcSyncWork.state == StoryArcSyncWorkState.COMPLETED,
        )
        .group_by(ImportJobAction.id)
        .subquery()
    )
    return int(
        await session.scalar(
            select(
                func.coalesce(
                    func.sum(
                        case(
                            (
                                or_(
                                    per_action.c.placement_count != 1,
                                    per_action.c.valid_count != 1,
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                )
            ).select_from(per_action)
        )
        or 0
    )


async def _mark_pending(
    session: AsyncSession,
    job: ImportJob,
    snapshot: dict[str, object],
    counts: ImportStoryArcPlacementCounts,
    *,
    expected_total: int,
) -> ImportStoryArcPlacementCompletionOutcome:
    job.story_arc_placement_followup_pending = False
    updated = _with_counts(snapshot, counts, expected_total=expected_total)
    updated.update(
        {
            "status": ImportJobStatus.IMPORTING.value,
            "mode": "import",
            "phase": _PHASE,
            "progress": 99,
            "message": "Creating the approved story-arc copies and links...",
        }
    )
    await _apply_job_state(
        session,
        job,
        status=ImportJobStatus.IMPORTING,
        snapshot=updated,
        error_message=None,
        import_completed_at=None,
    )
    return ImportStoryArcPlacementCompletionOutcome(
        job_id=job.id,
        state=ImportStoryArcPlacementCompletionState.PENDING,
        counts=counts,
    )


async def _stall(
    session: AsyncSession,
    job: ImportJob,
    snapshot: dict[str, object],
    counts: ImportStoryArcPlacementCounts,
    *,
    expected_total: int | None,
    error_code: str,
) -> ImportStoryArcPlacementCompletionOutcome:
    job.story_arc_placement_followup_pending = False
    message = _ERRORS[error_code]
    updated = _with_counts(
        snapshot,
        counts,
        expected_total=expected_total if expected_total is not None else counts.total,
    )
    updated.update(
        {
            "status": ImportJobStatus.STALLED.value,
            "mode": "import",
            "phase": _PHASE,
            "progress": 99,
            "message": message,
        }
    )
    await _apply_job_state(
        session,
        job,
        status=ImportJobStatus.STALLED,
        snapshot=updated,
        error_message=message,
        import_completed_at=None,
    )
    return ImportStoryArcPlacementCompletionOutcome(
        job_id=job.id,
        state=ImportStoryArcPlacementCompletionState.STALLED,
        counts=counts,
        error_code=error_code,
    )


async def _complete(
    session: AsyncSession,
    job: ImportJob,
    snapshot: dict[str, object],
    counts: ImportStoryArcPlacementCounts,
    *,
    completed_at: datetime,
) -> ImportStoryArcPlacementCompletionOutcome:
    job.story_arc_placement_followup_pending = True
    updated = _with_counts(snapshot, counts, expected_total=counts.total)
    updated.update(
        {
            "status": ImportJobStatus.COMPLETED.value,
            "mode": "import",
            "phase": "done",
            "progress": 100,
            "message": "Import completed.",
            "story_arc_placement_followup_pending": True,
        }
    )
    await _apply_job_state(
        session,
        job,
        status=ImportJobStatus.COMPLETED,
        snapshot=updated,
        error_message=None,
        import_completed_at=completed_at,
    )
    return ImportStoryArcPlacementCompletionOutcome(
        job_id=job.id,
        state=ImportStoryArcPlacementCompletionState.COMPLETED,
        counts=counts,
    )


async def _apply_job_state(
    session: AsyncSession,
    job: ImportJob,
    *,
    status: ImportJobStatus,
    snapshot: dict[str, object],
    error_message: str | None,
    import_completed_at: datetime | None,
) -> None:
    changed = (
        job.status is not status
        or dict(job.progress_snapshot or {}) != snapshot
        or job.error_message != error_message
        or job.import_completed_at != import_completed_at
    )
    if not changed:
        return
    job.status = status
    job.progress_snapshot = snapshot
    job.error_message = error_message
    job.import_completed_at = import_completed_at
    job.progress_revision = int(job.progress_revision or 0) + 1
    await session.flush()


def _with_counts(
    snapshot: dict[str, object],
    counts: ImportStoryArcPlacementCounts,
    *,
    expected_total: int,
) -> dict[str, object]:
    updated = dict(snapshot)
    updated["story_arc_placements_total"] = expected_total
    for state, count in counts.as_dict().items():
        updated[f"story_arc_placements_{state}"] = count
    return updated


def _counts_from_snapshot(snapshot: dict[str, object]) -> ImportStoryArcPlacementCounts:
    values: dict[str, int] = {}
    for state in StoryArcSyncWorkState:
        value = snapshot.get(f"story_arc_placements_{state.value}")
        values[state.value] = (
            value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
        )
    return ImportStoryArcPlacementCounts(**values)


def _positive_int(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    return value
