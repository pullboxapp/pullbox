"""Import workflow state and progress helpers.

This module defines the DB-first runtime contract shared by Step 2 scan,
Step 4 import, and rollback. The import UI should be able to rebuild its
current state from ``ImportJob.progress_snapshot`` alone after a refresh or
restart, with SSE acting only as a low-latency mirror of committed state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast

from sqlalchemy import select as sa_select

from pullbox.core.exceptions import JobCancelledError, JobPausedError
from pullbox.models.import_job import ImportControlRequest, ImportJob, ImportJobStatus
from pullbox.services.import_progress_runtime import elapsed_seconds_since, stage_label

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.schemas.import_job import ImportProgressEvent

SCAN_PROGRESS_INVENTORY_END = 10
SCAN_PROGRESS_MATERIALIZE_START = 10
SCAN_PROGRESS_MATERIALIZE_END = 35
SCAN_PROGRESS_ANALYZE_START = 35
SCAN_PROGRESS_ANALYZE_END = 45
SCAN_PROGRESS_MATCH_START = 45
SCAN_PROGRESS_MATCH_END = 80
SCAN_PROGRESS_FILE_MATCH_START = 80
SCAN_PROGRESS_FILE_MATCH_END = 99
WORKFLOW_SNAPSHOT_VERSION = 2
ImportProgressMode = Literal["scan", "import", "rollback"]
_INVENTORY_PROGRESS_THRESHOLDS: tuple[tuple[int, int], ...] = (
    (1, 1),
    (10, 2),
    (50, 3),
    (200, 4),
    (1_000, 5),
    (5_000, 6),
    (15_000, 7),
    (30_000, 8),
    (50_000, 9),
)

ACTIVE_IMPORT_JOB_STATUSES = frozenset(
    {
        ImportJobStatus.PENDING,
        ImportJobStatus.SCANNING,
        ImportJobStatus.PAUSING,
        ImportJobStatus.PAUSED,
        ImportJobStatus.ANALYZING,
        ImportJobStatus.MATCHING,
        ImportJobStatus.FILE_MATCHING,
        ImportJobStatus.REVIEW,
        ImportJobStatus.IMPORTING,
        ImportJobStatus.STALLED,
        ImportJobStatus.CANCELLING,
        ImportJobStatus.ROLLING_BACK,
    }
)

SCAN_PHASE_STATUSES = frozenset(
    {
        ImportJobStatus.SCANNING,
        ImportJobStatus.PAUSING,
        ImportJobStatus.PAUSED,
        ImportJobStatus.ANALYZING,
        ImportJobStatus.MATCHING,
        ImportJobStatus.FILE_MATCHING,
    }
)

RUNNING_IMPORT_JOB_STATUSES = frozenset(
    {
        ImportJobStatus.PENDING,
        ImportJobStatus.SCANNING,
        ImportJobStatus.ANALYZING,
        ImportJobStatus.MATCHING,
        ImportJobStatus.FILE_MATCHING,
        ImportJobStatus.IMPORTING,
        ImportJobStatus.STALLED,
        ImportJobStatus.ROLLING_BACK,
    }
)

_PROTECTED_RUNTIME_STATUSES = frozenset(
    {
        ImportJobStatus.REVIEW,
        ImportJobStatus.PAUSED,
        ImportJobStatus.STALLED,
        ImportJobStatus.CANCELLING,
        ImportJobStatus.ROLLING_BACK,
        ImportJobStatus.COMPLETED,
        ImportJobStatus.FAILED,
        ImportJobStatus.CANCELLED,
        ImportJobStatus.ROLLED_BACK,
    }
)


def snapshot_mode_for_job(job: ImportJob, *, default: str = "scan") -> str:
    """Return the durable workflow mode for the current job state."""
    if job.status == ImportJobStatus.ROLLING_BACK:
        return "rollback"
    if (
        job.status
        in {
            ImportJobStatus.IMPORTING,
            ImportJobStatus.COMPLETED,
            ImportJobStatus.CANCELLED,
            ImportJobStatus.FAILED,
        }
        and job.import_started_at is not None
    ):
        return "import"

    snapshot = dict(job.progress_snapshot or {})
    snapshot_mode = str(snapshot.get("mode") or "").strip().lower()
    if snapshot_mode in {"scan", "import", "rollback"}:
        return snapshot_mode
    return default


def paused_message_for_mode(mode: str) -> str:
    """Return the user-facing paused message for a workflow mode."""
    if mode == "import":
        return "Import is paused."
    if mode == "rollback":
        return "Rollback is paused."
    return "Scan is paused."


def stalled_message() -> str:
    """Return the user-facing recoverable import-stall message."""
    return "Import stalled because the database was busy. Resume when ready."


def snapshot_requested_action_for_job(job: ImportJob) -> ImportControlRequest:
    """Return the currently persisted cooperative control request."""
    requested = job.control_request
    if isinstance(requested, ImportControlRequest):
        return requested
    with_value = str(requested or ImportControlRequest.NONE.value).strip().lower()
    for candidate in ImportControlRequest:
        if candidate.value == with_value:
            return candidate
    return ImportControlRequest.NONE


def next_progress_revision(job: ImportJob) -> int:
    """Bump and return the durable snapshot revision for this job."""
    job.progress_revision = int(job.progress_revision or 0) + 1
    return job.progress_revision


def sync_paused_job_state(job: ImportJob) -> None:
    """Persist a truthful paused snapshot without losing current progress detail."""
    snapshot = dict(job.progress_snapshot or {})
    mode = snapshot_mode_for_job(job)
    default_phase = (
        "importing" if mode == "import" else ("rollback" if mode == "rollback" else "inventory")
    )
    current_series_name = snapshot.get("current_series_name") or snapshot.get("current_series")

    job.status = ImportJobStatus.PAUSED
    job.control_request = ImportControlRequest.NONE
    paused_message = job.error_message or paused_message_for_mode(mode)
    sync_progress_snapshot_state(
        job,
        status=ImportJobStatus.PAUSED,
        mode=mode,
        phase=str(snapshot.get("phase") or default_phase),
        progress=int(snapshot.get("progress") or 0),
        message=paused_message,
        current_series_id=snapshot.get("current_series_id"),
        current_series_name=str(current_series_name) if current_series_name else None,
        current_file_id=snapshot.get("current_file_id"),
        current_file_name=(
            str(snapshot.get("current_file_name")) if snapshot.get("current_file_name") else None
        ),
        current_file_stage=(
            str(snapshot.get("current_file_stage")) if snapshot.get("current_file_stage") else None
        ),
        current_file_progress_current=snapshot.get("current_file_progress_current"),
        current_file_progress_total=snapshot.get("current_file_progress_total"),
        current_file_progress_pct=snapshot.get("current_file_progress_pct"),
        current_file_progress_unit=(
            str(snapshot.get("current_file_progress_unit"))
            if snapshot.get("current_file_progress_unit")
            else None
        ),
        current_item_kind=(
            str(snapshot.get("current_item_kind")) if snapshot.get("current_item_kind") else None
        ),
        current_item_stage=(
            str(snapshot.get("current_item_stage")) if snapshot.get("current_item_stage") else None
        ),
        current_item_stage_label=(
            str(snapshot.get("current_item_stage_label"))
            if snapshot.get("current_item_stage_label")
            else None
        ),
        current_item_progress_pct=snapshot.get("current_item_progress_pct"),
        current_item_detail=(
            str(snapshot.get("current_item_detail"))
            if snapshot.get("current_item_detail")
            else None
        ),
        estimated_seconds_remaining=snapshot.get("estimated_seconds_remaining"),
        elapsed_seconds=snapshot.get("elapsed_seconds"),
    )


def sync_stalled_job_state(job: ImportJob) -> None:
    """Persist a recoverable stalled snapshot without losing current progress detail."""
    snapshot = dict(job.progress_snapshot or {})
    mode = snapshot_mode_for_job(job)
    default_phase = (
        "importing" if mode == "import" else ("rollback" if mode == "rollback" else "inventory")
    )
    current_series_name = snapshot.get("current_series_name") or snapshot.get("current_series")

    job.status = ImportJobStatus.STALLED
    job.control_request = ImportControlRequest.NONE
    job.error_message = stalled_message()
    sync_progress_snapshot_state(
        job,
        status=ImportJobStatus.STALLED,
        mode=mode,
        phase=str(snapshot.get("phase") or default_phase),
        progress=int(snapshot.get("progress") or 0),
        message=stalled_message(),
        current_series_id=snapshot.get("current_series_id"),
        current_series_name=str(current_series_name) if current_series_name else None,
        current_file_id=snapshot.get("current_file_id"),
        current_file_name=(
            str(snapshot.get("current_file_name")) if snapshot.get("current_file_name") else None
        ),
        current_file_stage=(
            str(snapshot.get("current_file_stage")) if snapshot.get("current_file_stage") else None
        ),
        current_file_progress_current=snapshot.get("current_file_progress_current"),
        current_file_progress_total=snapshot.get("current_file_progress_total"),
        current_file_progress_pct=snapshot.get("current_file_progress_pct"),
        current_file_progress_unit=(
            str(snapshot.get("current_file_progress_unit"))
            if snapshot.get("current_file_progress_unit")
            else None
        ),
        current_item_kind=(
            str(snapshot.get("current_item_kind")) if snapshot.get("current_item_kind") else None
        ),
        current_item_stage=(
            str(snapshot.get("current_item_stage")) if snapshot.get("current_item_stage") else None
        ),
        current_item_stage_label=(
            str(snapshot.get("current_item_stage_label"))
            if snapshot.get("current_item_stage_label")
            else None
        ),
        current_item_progress_pct=snapshot.get("current_item_progress_pct"),
        current_item_detail=(
            str(snapshot.get("current_item_detail"))
            if snapshot.get("current_item_detail")
            else None
        ),
        estimated_seconds_remaining=snapshot.get("estimated_seconds_remaining"),
        elapsed_seconds=snapshot.get("elapsed_seconds"),
    )


def import_control_state_for_job(job: ImportJob) -> dict[str, object]:
    """Return durable UI control affordances for the current job state."""
    requested_action = snapshot_requested_action_for_job(job)
    action_pending = requested_action != ImportControlRequest.NONE
    snapshot = dict(job.progress_snapshot or {})
    placement_wait = (
        job.status in {ImportJobStatus.IMPORTING, ImportJobStatus.STALLED}
        and snapshot.get("phase") == "story_arc_placements"
    )
    failed_placements = snapshot.get("story_arc_placements_failed")
    cancelled_placements = snapshot.get("story_arc_placements_cancelled")
    has_retryable_terminal_placements = (
        isinstance(failed_placements, int)
        and not isinstance(failed_placements, bool)
        and failed_placements >= 0
        and isinstance(cancelled_placements, int)
        and not isinstance(cancelled_placements, bool)
        and cancelled_placements >= 0
        and failed_placements + cancelled_placements > 0
    )
    can_pause = (
        job.status
        in {
            ImportJobStatus.SCANNING,
            ImportJobStatus.ANALYZING,
            ImportJobStatus.MATCHING,
            ImportJobStatus.FILE_MATCHING,
            ImportJobStatus.IMPORTING,
        }
        and not action_pending
        and not placement_wait
    )
    can_resume = (
        job.status in {ImportJobStatus.PAUSED, ImportJobStatus.STALLED} and not placement_wait
    ) or (job.status == ImportJobStatus.REVIEW and job.import_started_at is None)
    can_cancel = (
        job.status
        in ACTIVE_IMPORT_JOB_STATUSES
        - {
            ImportJobStatus.REVIEW,
            ImportJobStatus.ROLLING_BACK,
        }
        and not action_pending
    )
    can_discard = job.status in {
        ImportJobStatus.PENDING,
        ImportJobStatus.PAUSED,
        ImportJobStatus.STALLED,
        ImportJobStatus.REVIEW,
        ImportJobStatus.FAILED,
        ImportJobStatus.CANCELLED,
        ImportJobStatus.ROLLED_BACK,
    }
    can_delete = job.status in {
        ImportJobStatus.PENDING,
        ImportJobStatus.REVIEW,
        ImportJobStatus.PAUSED,
        ImportJobStatus.STALLED,
        ImportJobStatus.FAILED,
        ImportJobStatus.CANCELLED,
        ImportJobStatus.ROLLED_BACK,
    }
    can_view_results = job.status in {
        ImportJobStatus.COMPLETED,
        ImportJobStatus.FAILED,
    }
    can_retry = job.status in {
        ImportJobStatus.CANCELLED,
        ImportJobStatus.ROLLED_BACK,
    }
    can_retry_story_arc_placements = bool(
        job.status is ImportJobStatus.STALLED
        and job.import_started_at is not None
        and placement_wait
        and not action_pending
        and has_retryable_terminal_placements
    )
    can_rollback = bool(job.import_started_at) and job.status in {
        ImportJobStatus.COMPLETED,
        ImportJobStatus.FAILED,
    }
    return {
        "can_pause": can_pause,
        "can_resume": can_resume,
        "can_cancel": can_cancel,
        "can_discard": can_discard,
        "can_delete": can_delete,
        "can_view_results": can_view_results,
        "can_retry": can_retry,
        "can_retry_story_arc_placements": can_retry_story_arc_placements,
        "can_rollback": can_rollback,
        "transfer_method": job.transfer_method,
        "convert_to_preferred_format": job.convert_to_preferred_format,
        "requested_action": requested_action.value,
    }


def phase_progress(start: int, end: int, completed: int, total: int) -> int:
    """Scale measured phase work into a bounded overall progress range."""
    if end <= start:
        return start
    if total <= 0:
        return start
    fraction = min(max(completed / total, 0.0), 1.0)
    return start + round((end - start) * fraction)


def inventory_progress_hint(directories_visited: int) -> int:
    """Return a monotonic hint for the indeterminate inventory phase.

    The inventory pass intentionally walks the tree to discover the total
    number of directories and candidate files, so an exact percentage is not
    available without adding yet another pre-pass. This helper turns real
    work milestones into a bounded 0-9 progress hint so the bar can move while
    inventory is still actively counting.
    """
    progress = 0
    for threshold, hint in _INVENTORY_PROGRESS_THRESHOLDS:
        if directories_visited >= threshold:
            progress = hint
        else:
            break
    return min(progress, SCAN_PROGRESS_INVENTORY_END - 1)


def runtime_snapshot_payload(
    job: ImportJob,
    *,
    status: ImportJobStatus | None = None,
    mode: str | None = None,
    phase: str | None = None,
    progress: int | None = None,
    message: str | None = None,
    current_series_id: int | None = None,
    current_series_name: str | None = None,
    current_file_id: int | None = None,
    current_file_name: str | None = None,
    current_file_stage: str | None = None,
    current_file_progress_current: int | None = None,
    current_file_progress_total: int | None = None,
    current_file_progress_pct: int | None = None,
    current_file_progress_unit: str | None = None,
    current_item_kind: str | None = None,
    current_item_stage: str | None = None,
    current_item_stage_label: str | None = None,
    current_item_progress_pct: int | None = None,
    current_item_detail: str | None = None,
    estimated_seconds_remaining: int | None = None,
    elapsed_seconds: int | None = None,
    review_summary: dict[str, int] | None = None,
    recent_logs: list[dict[str, object]] | None = None,
    bump_revision: bool = False,
) -> dict[str, object]:
    """Build the authoritative runtime snapshot stored on an import job."""
    snapshot = dict(job.progress_snapshot or {})
    effective_status = status or job.status
    effective_mode = mode or snapshot_mode_for_job(job)
    effective_revision = (
        next_progress_revision(job) if bump_revision else int(job.progress_revision or 0)
    )
    requested_action = snapshot_requested_action_for_job(job)
    checkpoint_at = datetime.now(UTC).isoformat()

    runtime = {
        "snapshot_version": WORKFLOW_SNAPSHOT_VERSION,
        "job_id": job.id,
        "status": effective_status.value,
        "mode": effective_mode,
        "phase": phase if phase is not None else snapshot.get("phase", ""),
        "progress": progress if progress is not None else int(snapshot.get("progress") or 0),
        "message": message if message is not None else str(snapshot.get("message") or ""),
        "requested_action": requested_action.value,
        "progress_revision": effective_revision,
        "last_checkpoint_at": checkpoint_at,
        "current_series_id": current_series_id,
        "current_series_name": current_series_name,
        "current_file_id": current_file_id,
        "current_file_name": current_file_name,
        "current_file_stage": current_file_stage,
        "current_file_progress_current": current_file_progress_current,
        "current_file_progress_total": current_file_progress_total,
        "current_file_progress_pct": current_file_progress_pct,
        "current_file_progress_unit": current_file_progress_unit,
        "current_item_kind": current_item_kind,
        "current_item_stage": current_item_stage,
        "current_item_stage_label": (
            current_item_stage_label
            if current_item_stage_label is not None
            else stage_label(current_item_stage)
        ),
        "current_item_progress_pct": current_item_progress_pct,
        "current_item_detail": current_item_detail,
        "current_series": current_series_name,
        "current_series_status": snapshot.get("current_series_status"),
        "estimated_seconds_remaining": estimated_seconds_remaining,
        "elapsed_seconds": elapsed_seconds,
        "error_message": job.error_message,
        "scan_total_files": job.scan_total_files,
        "scan_total_dirs": job.scan_total_dirs,
        "series_found": job.series_found,
        "series_duplicate": job.series_duplicate,
        "series_matched": job.series_matched,
        "series_no_match": job.series_no_match,
        "series_new": job.series_new,
        "series_imported": job.series_imported,
        "series_failed": job.series_failed,
        "total_files_found": job.total_files_found,
        "total_files_matched": job.total_files_matched,
        "total_files_duplicate": job.total_files_duplicate,
        "total_files_already_owned": job.total_files_already_owned,
        "total_files_conflict": job.total_files_conflict,
        "total_files_no_match": job.total_files_no_match,
        "total_files_imported": job.total_files_imported,
        "total_files_failed": job.total_files_failed,
        "story_arc_placements_total": snapshot.get("story_arc_placements_total"),
        "story_arc_placements_queued": snapshot.get("story_arc_placements_queued"),
        "story_arc_placements_running": snapshot.get("story_arc_placements_running"),
        "story_arc_placements_retry_wait": snapshot.get("story_arc_placements_retry_wait"),
        "story_arc_placements_failed": snapshot.get("story_arc_placements_failed"),
        "story_arc_placements_completed": snapshot.get("story_arc_placements_completed"),
        "story_arc_placements_cancelled": snapshot.get("story_arc_placements_cancelled"),
        "review_summary": (
            review_summary if review_summary is not None else snapshot.get("review_summary")
        ),
        "scan_started_at": job.scan_started_at.isoformat() if job.scan_started_at else None,
        "import_started_at": job.import_started_at.isoformat() if job.import_started_at else None,
        "recent_logs": (
            recent_logs if recent_logs is not None else snapshot.get("recent_logs") or []
        ),
        "control_state": import_control_state_for_job(job),
    }
    return runtime


def initialize_progress_snapshot(
    job: ImportJob,
    *,
    mode: str,
    phase: str,
    progress: int,
    message: str,
    status: ImportJobStatus | None = None,
) -> dict[str, object]:
    """Reset the runtime payload for a new workflow mode."""
    return runtime_snapshot_payload(
        job,
        status=status,
        mode=mode,
        phase=phase,
        progress=progress,
        message=message,
        current_series_id=None,
        current_series_name=None,
        current_file_id=None,
        current_file_name=None,
        current_file_stage=None,
        current_file_progress_current=None,
        current_file_progress_total=None,
        current_file_progress_pct=None,
        current_file_progress_unit=None,
        current_item_kind=None,
        current_item_stage=None,
        current_item_stage_label=None,
        current_item_progress_pct=None,
        current_item_detail=None,
        estimated_seconds_remaining=None,
        elapsed_seconds=None,
        review_summary=None,
        recent_logs=[],
        bump_revision=True,
    )


def sync_progress_snapshot_state(
    job: ImportJob,
    *,
    status: ImportJobStatus | None = None,
    mode: str | None = None,
    phase: str | None = None,
    progress: int | None = None,
    message: str | None = None,
    current_series_id: int | None = None,
    current_series_name: str | None = None,
    current_file_id: int | None = None,
    current_file_name: str | None = None,
    current_file_stage: str | None = None,
    current_file_progress_current: int | None = None,
    current_file_progress_total: int | None = None,
    current_file_progress_pct: int | None = None,
    current_file_progress_unit: str | None = None,
    current_item_kind: str | None = None,
    current_item_stage: str | None = None,
    current_item_stage_label: str | None = None,
    current_item_progress_pct: int | None = None,
    current_item_detail: str | None = None,
    estimated_seconds_remaining: int | None = None,
    elapsed_seconds: int | None = None,
    bump_revision: bool = True,
) -> None:
    """Keep the durable progress snapshot aligned with job lifecycle transitions."""
    job.progress_snapshot = runtime_snapshot_payload(
        job,
        status=status,
        mode=mode,
        phase=phase,
        progress=progress,
        message=message,
        current_series_id=current_series_id,
        current_series_name=current_series_name,
        current_file_id=current_file_id,
        current_file_name=current_file_name,
        current_file_stage=current_file_stage,
        current_file_progress_current=current_file_progress_current,
        current_file_progress_total=current_file_progress_total,
        current_file_progress_pct=current_file_progress_pct,
        current_file_progress_unit=current_file_progress_unit,
        current_item_kind=current_item_kind,
        current_item_stage=current_item_stage,
        current_item_stage_label=current_item_stage_label,
        current_item_progress_pct=current_item_progress_pct,
        current_item_detail=current_item_detail,
        estimated_seconds_remaining=estimated_seconds_remaining,
        elapsed_seconds=elapsed_seconds,
        bump_revision=bump_revision,
    )


def estimate_remaining_seconds(
    started_at: datetime | None,
    progress: int,
) -> int | None:
    """Estimate remaining runtime from elapsed wall-clock time and progress."""
    if started_at is None or progress <= 0 or progress >= 100:
        return None

    elapsed_seconds = max(int((datetime.now(UTC) - started_at).total_seconds()), 0)
    if elapsed_seconds < 2:
        return None

    completed_fraction = progress / 100
    if completed_fraction <= 0:
        return None

    estimated_total_seconds = elapsed_seconds / completed_fraction
    remaining = max(round(estimated_total_seconds - elapsed_seconds), 0)
    return remaining or None


async def persist_progress_snapshot(
    session: AsyncSession,
    job: ImportJob,
    event: ImportProgressEvent,
) -> None:
    """Persist the latest progress payload on the job for recovery/UI hydration."""
    payload = event.model_dump(mode="json")
    if int(payload.get("progress_revision") or 0) <= 0:
        payload["progress_revision"] = next_progress_revision(job)
    else:
        job.progress_revision = int(payload["progress_revision"])
    payload["snapshot_version"] = WORKFLOW_SNAPSHOT_VERSION
    payload["requested_action"] = snapshot_requested_action_for_job(job).value
    payload["last_checkpoint_at"] = datetime.now(UTC).isoformat()
    payload["control_state"] = import_control_state_for_job(job)
    payload["error_message"] = job.error_message
    job.progress_snapshot = payload
    from pullbox.services.import_operation_progress import build_import_operation_update
    from pullbox.services.operation_progress import publish_operation_progress

    await publish_operation_progress(session, build_import_operation_update(job, event))
    await session.flush()


def apply_progress_event_contract(
    job: ImportJob,
    event: ImportProgressEvent,
    *,
    started_at: datetime | None = None,
) -> None:
    """Populate the additive explicit progress contract on an event."""
    if event.current_series and not event.current_series_name:
        event.current_series_name = event.current_series
    if event.current_series_name and not event.current_series:
        event.current_series = event.current_series_name

    if event.current_item_kind is None:
        if event.current_file_name:
            event.current_item_kind = "file"
        elif event.current_series_name or event.current_series:
            event.current_item_kind = "series"

    if event.current_item_stage is None:
        if event.current_file_stage:
            event.current_item_stage = event.current_file_stage
        elif event.current_item_kind:
            event.current_item_stage = event.phase

    if event.current_item_stage_label is None:
        event.current_item_stage_label = stage_label(event.current_item_stage)

    if event.current_item_progress_pct is None:
        if event.current_file_progress_pct is not None:
            event.current_item_progress_pct = event.current_file_progress_pct
        elif event.current_item_kind is not None:
            event.current_item_progress_pct = 0

    if event.elapsed_seconds is None:
        event.elapsed_seconds = elapsed_seconds_since(started_at)


async def emit_live_progress(
    job: ImportJob,
    event: ImportProgressEvent,
    *,
    progress_callback: Callable[[ImportProgressEvent], Awaitable[None]] | None,
    revision_state: dict[str, int],
    started_at: datetime | None = None,
) -> None:
    """Publish an explicit live-only event without writing a durable snapshot."""
    if job.status in _PROTECTED_RUNTIME_STATUSES and event.status != job.status:
        return

    highest_revision = max(
        int(revision_state.get("value") or 0),
        int(job.progress_revision or 0),
        int(event.progress_revision or 0),
    )
    revision_state["value"] = highest_revision + 1
    event.progress_revision = revision_state["value"]
    event.ephemeral_progress = True
    event.snapshot_version = WORKFLOW_SNAPSHOT_VERSION
    event.mode = cast("ImportProgressMode", snapshot_mode_for_job(job, default=event.mode))
    event.requested_action = snapshot_requested_action_for_job(job)
    event.last_checkpoint_at = datetime.now(UTC)
    event.control_state = import_control_state_for_job(job)
    apply_progress_event_contract(job, event, started_at=started_at)
    from pullbox.services.import_operation_progress import build_import_operation_update
    from pullbox.services.operation_progress_dispatch import queue_operation_progress

    await queue_operation_progress(build_import_operation_update(job, event))
    if progress_callback is not None:
        await progress_callback(event)


async def emit_progress(
    session: AsyncSession,
    job: ImportJob,
    event: ImportProgressEvent,
    progress_callback: Callable[[ImportProgressEvent], Awaitable[None]] | None = None,
) -> None:
    """Persist, commit, and then forward a progress event.

    Step 2 and Step 4 treat the database snapshot as the source of truth.
    The event bus is only a transport mirror, so callers must never publish
    a progress payload that is not yet durable.
    """
    if job.status in _PROTECTED_RUNTIME_STATUSES and event.status != job.status:
        return
    event.mode = cast("ImportProgressMode", snapshot_mode_for_job(job, default=event.mode))
    if event.progress_revision <= 0:
        event.progress_revision = next_progress_revision(job)
    else:
        job.progress_revision = int(event.progress_revision)
    event.snapshot_version = WORKFLOW_SNAPSHOT_VERSION
    event.requested_action = snapshot_requested_action_for_job(job)
    event.last_checkpoint_at = datetime.now(UTC)
    workflow_started_at = job.import_started_at if event.mode == "import" else job.scan_started_at
    apply_progress_event_contract(job, event, started_at=workflow_started_at)
    event.control_state = import_control_state_for_job(job)
    await persist_progress_snapshot(session, job, event)
    await session.commit()
    from pullbox.services.import_operation_progress import build_import_operation_update
    from pullbox.services.operation_progress_dispatch import notify_activity_changed

    await notify_activity_changed(build_import_operation_update(job, event))
    if progress_callback is not None:
        await progress_callback(event)


async def raise_if_job_cancelled(session: AsyncSession, job_id: int) -> None:
    """Abort cooperative work if the import job has been paused or cancelled."""
    result = await session.execute(
        sa_select(ImportJob.status, ImportJob.control_request).where(ImportJob.id == job_id)
    )
    row = result.one_or_none()
    if row is None:
        raise JobCancelledError(f"Import job {job_id} was cancelled.")
    current_status, control_request = row
    requested_action = control_request
    if requested_action == ImportControlRequest.CANCEL:
        raise JobCancelledError(f"Import job {job_id} was cancelled.")
    if requested_action == ImportControlRequest.PAUSE:
        raise JobPausedError(f"Import job {job_id} was paused.")
    if current_status is None or current_status in {
        ImportJobStatus.CANCELLING,
        ImportJobStatus.CANCELLED,
    }:
        raise JobCancelledError(f"Import job {job_id} was cancelled.")
    if current_status in {ImportJobStatus.PAUSING, ImportJobStatus.PAUSED}:
        raise JobPausedError(f"Import job {job_id} was paused.")
