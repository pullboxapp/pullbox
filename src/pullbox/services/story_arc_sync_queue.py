"""Durable outbox and bounded worker for automatic story-arc synchronization."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import structlog
from sqlalchemy import and_, delete, exists, func, insert, or_, select, tuple_, update

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.database import get_session_factory
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportJob,
    ImportJobAction,
    ImportJobActionStatus,
    ImportJobStatus,
)
from pullbox.models.library import LibraryFile
from pullbox.models.story_arc import (
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
from pullbox.models.story_arc_sync import (
    StoryArcSyncReason,
    StoryArcSyncWork,
    StoryArcSyncWorkState,
)
from pullbox.services.import_job_actions import ImportJobActionSpec
from pullbox.services.import_story_arc_placement_completion import (
    ImportStoryArcPlacementCompletionState,
    finalize_import_story_arc_placements,
)
from pullbox.services.story_arc_membership_policy import requires_order_review
from pullbox.services.story_arc_placement_integration import (
    STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION,
    StoryArcPlacementImportProvenance,
    StoryArcPlacementIntegrationError,
    StoryArcPlacementSyncService,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy.engine import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from pullbox.services.import_job_execution_types import RecordActionFunc, RecordActionsFunc

logger = structlog.get_logger(__name__)

STORY_ARC_SYNC_TASK_ID = "sync_story_arc_placements"
MAX_STORY_ARC_SYNC_BATCH_SIZE = 100
MAX_STORY_ARC_SYNC_ENQUEUE_MEMBERSHIPS = 200
MAX_IMPORT_STORY_ARC_SYNC_ENQUEUE_BATCH_SIZE = 200
DEFAULT_STORY_ARC_SYNC_BATCH_SIZE = 50
DEFAULT_STORY_ARC_DISCOVERY_LIMIT = 200
MAX_IMPORT_PLACEMENT_FINALIZE_BATCH_SIZE = 100
_CLAIM_LEASE = timedelta(minutes=15)
_CLAIM_HEARTBEAT_INTERVAL_SECONDS = 60.0
_ORIGIN_CANCELLATION_POLL_SECONDS = 0.25
_MAX_ATTEMPTS = 5
_RETRY_DELAYS = (
    timedelta(seconds=30),
    timedelta(minutes=2),
    timedelta(minutes=10),
    timedelta(minutes=30),
    timedelta(hours=1),
)
_POLICY_KEYS = frozenset(
    {
        "schema_version",
        "mode",
        "target_library_root_id",
        "destination_root",
        "folder_template",
        "file_template",
        "symlink_style",
        "synchronize",
    }
)
_IMPORT_PLACEMENT_ACTION_TYPE = "story_arc_managed_placement_requested"
_IMPORT_BUILD_PHASE = "story_arcs"
_IMPORT_PLACEMENT_PHASE = "story_arc_placements"
_STARTUP_RECOVERY_PAUSE_REASON = "startup_recovery"
_IMPORT_ENQUEUE_PHASES = frozenset({_IMPORT_BUILD_PHASE, _IMPORT_PLACEMENT_PHASE})
_IMPORT_PLACEMENT_PAYLOAD_KEYS = frozenset(
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
_IMPORT_HISTORY_CLEANUP_PAGE_SIZE = 1_000
_MANAGED_IMPORT_MODES = frozenset({"copy", "hardlink", "symlink"})
_PENDING_IMPORT_WORK_STATES = frozenset(
    {
        StoryArcSyncWorkState.QUEUED,
        StoryArcSyncWorkState.RUNNING,
        StoryArcSyncWorkState.RETRY_WAIT,
    }
)
_RETRYABLE_ERROR_CODES = frozenset(
    {
        "placement_concurrency_conflict",
        "placement_execution_failed",
    }
)


@dataclass(frozen=True, slots=True)
class StoryArcSyncDrainResult:
    """Bounded worker outcome used by task logging and continuation scheduling."""

    discovered: int
    claimed: int
    completed: int
    failed: int
    retrying: int
    cancelled: int
    lost_claims: int
    has_more: bool
    next_retry_at: datetime | None
    import_jobs_evaluated: tuple[int, ...] = ()
    import_jobs_completed: tuple[int, ...] = ()
    import_jobs_stalled: tuple[int, ...] = ()
    import_jobs_rollback_ready: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class StoryArcImportSyncEnqueueResult:
    """Idempotent import enqueue result without retrofitting prior ownership."""

    work: StoryArcSyncWork | None
    action: ImportJobAction | None
    classification: str
    desired_generation: str


@dataclass(frozen=True, slots=True)
class ImportStoryArcSyncProposal:
    """One exact staged origin requesting managed placement synchronization."""

    library_file: LibraryFile
    membership: IssueStoryArc
    story_arc: StoryArc
    imported_story_arc_id: int
    imported_story_arc_entry_id: int


@dataclass(frozen=True, slots=True)
class _PreparedImportStoryArcSyncProposal:
    proposal: ImportStoryArcSyncProposal
    desired_generation: str
    source_signature_hash: str

    @property
    def key(self) -> tuple[int, str]:
        return (int(self.proposal.membership.id), self.desired_generation)

    @property
    def origin(self) -> tuple[int, int]:
        return (
            self.proposal.imported_story_arc_id,
            self.proposal.imported_story_arc_entry_id,
        )


@dataclass(frozen=True, slots=True)
class _WorkContext:
    work_id: int
    membership_id: int
    story_arc_id: int
    library_file_id: int
    attempt_count: int
    import_provenance: StoryArcPlacementImportProvenance | None


def _stable_hash(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _origin_is_not_startup_recovery_paused() -> Any:
    """Fence origin work until its startup-recovered import is actively resumed."""
    protected_origin = exists().where(
        ImportJob.id == StoryArcSyncWork.origin_import_job_id,
        ImportJob.status == ImportJobStatus.PAUSED,
        ImportJob.progress_snapshot["pause_reason"].as_string() == _STARTUP_RECOVERY_PAUSE_REASON,
    )
    return ~protected_origin


def _source_signature_hash(library_file: LibraryFile) -> str:
    return _stable_hash(
        {
            "file_path": library_file.file_path,
            "file_size": library_file.file_size,
            "file_modified_at": library_file.file_modified_at,
            "file_hash": library_file.file_hash,
            "source_signature": dict(library_file.source_signature or {}),
        }
    )


def _source_signature_int(library_file: LibraryFile, key: str) -> int | None:
    value = dict(library_file.source_signature or {}).get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _source_signature_path(library_file: LibraryFile) -> str | None:
    value = dict(library_file.source_signature or {}).get("resolved_path")
    return value if isinstance(value, str) else None


def _desired_generation(
    library_file: LibraryFile,
    membership: IssueStoryArc,
    story_arc: StoryArc,
) -> tuple[str, str]:
    source_hash = _source_signature_hash(library_file)
    return (
        _stable_hash(
            {
                "library_file_id": library_file.id,
                "source_signature_hash": source_hash,
                "issue_story_arc_id": membership.id,
                "sequence_number": membership.sequence_number,
                "story_arc_revision": story_arc.revision,
                "policy_schema_version": story_arc.policy_schema_version,
            }
        ),
        source_hash,
    )


def _automatic_sync_enabled(story_arc: StoryArc) -> bool:
    snapshot = dict(story_arc.policy_snapshot or {})
    return bool(
        story_arc.lifecycle is StoryArcLifecycle.ACTIVE
        and story_arc.sync_enabled
        and story_arc.policy_schema_version == STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION
        and set(snapshot) == _POLICY_KEYS
        and snapshot.get("schema_version") == STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION
        and snapshot.get("synchronize") is True
        and snapshot.get("mode") != "logical"
        and isinstance(snapshot.get("destination_root"), str)
        and bool(str(snapshot.get("destination_root")).strip())
        and isinstance(snapshot.get("target_library_root_id"), int)
        and not isinstance(snapshot.get("target_library_root_id"), bool)
    )


async def _eligible_memberships_for_issue(
    session: AsyncSession,
    issue_id: int,
) -> list[tuple[IssueStoryArc, StoryArc]]:
    rows = list(
        (
            await session.execute(
                select(IssueStoryArc, StoryArc)
                .join(StoryArc, IssueStoryArc.story_arc_id == StoryArc.id)
                .where(
                    IssueStoryArc.issue_id == issue_id,
                    IssueStoryArc.sync_eligible.is_(True),
                    IssueStoryArc.resolution_state == StoryArcResolutionState.RESOLVED,
                    StoryArc.lifecycle == StoryArcLifecycle.ACTIVE,
                    StoryArc.sync_enabled.is_(True),
                    StoryArc.policy_schema_version == STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION,
                )
                .order_by(IssueStoryArc.id.asc())
                .limit(MAX_STORY_ARC_SYNC_ENQUEUE_MEMBERSHIPS)
            )
        ).all()
    )
    return [
        (membership, story_arc)
        for membership, story_arc in rows
        if _automatic_sync_enabled(story_arc)
    ]


async def _enqueue_pairs(
    session: AsyncSession,
    library_file: LibraryFile,
    pairs: list[tuple[IssueStoryArc, StoryArc]],
    *,
    reason: StoryArcSyncReason,
) -> int:
    proposals: list[tuple[IssueStoryArc, StoryArc, str, str]] = []
    for membership, story_arc in pairs:
        generation, source_hash = _desired_generation(library_file, membership, story_arc)
        proposals.append((membership, story_arc, generation, source_hash))
    if not proposals:
        return 0

    membership_ids = [membership.id for membership, _arc, _generation, _hash in proposals]
    desired_generations = [generation for _membership, _arc, generation, _hash in proposals]
    existing = set(
        (
            await session.execute(
                select(
                    StoryArcSyncWork.issue_story_arc_id,
                    StoryArcSyncWork.desired_generation,
                )
                .where(
                    StoryArcSyncWork.issue_story_arc_id.in_(membership_ids),
                    StoryArcSyncWork.desired_generation.in_(desired_generations),
                )
                .limit(len(proposals))
            )
        ).all()
    )
    queued = 0
    for membership, story_arc, generation, source_hash in proposals:
        if (membership.id, generation) in existing:
            continue
        session.add(
            StoryArcSyncWork(
                issue_story_arc_id=membership.id,
                library_file_id=library_file.id,
                desired_generation=generation,
                source_signature_hash=source_hash,
                source_file_path=library_file.file_path,
                source_file_size=library_file.file_size,
                source_file_modified_at=library_file.file_modified_at,
                source_file_hash=library_file.file_hash,
                source_signature_schema_version=_source_signature_int(
                    library_file,
                    "schema_version",
                ),
                source_signature_resolved_path=_source_signature_path(library_file),
                source_signature_size=_source_signature_int(library_file, "size"),
                source_signature_mtime_ns=_source_signature_int(library_file, "mtime_ns"),
                source_signature_device=_source_signature_int(library_file, "device"),
                source_signature_inode=_source_signature_int(library_file, "inode"),
                story_arc_revision=story_arc.revision,
                membership_sequence=membership.sequence_number,
                policy_schema_version=story_arc.policy_schema_version
                or STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION,
                reason=reason,
                state=StoryArcSyncWorkState.QUEUED,
            )
        )
        queued += 1
    if queued:
        await session.flush()
    return queued


async def enqueue_story_arc_sync_work(
    session: AsyncSession,
    library_file: LibraryFile,
    *,
    reason: StoryArcSyncReason = StoryArcSyncReason.CANONICAL_REGISTERED,
) -> int:
    """Add DB-only work for every currently eligible arc in the caller's transaction."""
    if library_file.id is None or library_file.issue_id is None:
        return 0
    pairs = await _eligible_memberships_for_issue(session, library_file.issue_id)
    return await _enqueue_pairs(session, library_file, pairs, reason=reason)


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _import_managed_policy_configured(story_arc: StoryArc) -> bool:
    snapshot = dict(story_arc.policy_snapshot or {})
    mode = snapshot.get("mode")
    symlink_style = snapshot.get("symlink_style")
    return bool(
        story_arc.lifecycle is StoryArcLifecycle.ACTIVE
        and story_arc.policy_schema_version == STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION
        and set(snapshot) == _POLICY_KEYS
        and snapshot.get("schema_version") == STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION
        and mode in _MANAGED_IMPORT_MODES
        and isinstance(snapshot.get("destination_root"), str)
        and bool(str(snapshot.get("destination_root")).strip())
        and isinstance(snapshot.get("target_library_root_id"), int)
        and not isinstance(snapshot.get("target_library_root_id"), bool)
        and isinstance(snapshot.get("synchronize"), bool)
        and (
            (mode == "symlink" and symlink_style in {"absolute", "relative"})
            or (mode != "symlink" and symlink_style is None)
        )
    )


def _origin_payload_matches(
    action: ImportJobAction,
    work: StoryArcSyncWork,
    *,
    job_id: int,
    membership_id: int,
) -> bool:
    payload = dict(action.payload or {})
    schema_version = payload.get("schema_version")
    sync_work_id = payload.get("sync_work_id")
    payload_membership_id = payload.get("membership_id")
    desired_generation = payload.get("desired_generation")
    imported_story_arc_id = payload.get("imported_story_arc_id")
    imported_story_arc_entry_id = payload.get("imported_story_arc_entry_id")
    source_import_job_id = payload.get("source_import_job_id")
    return bool(
        work.origin_import_job_id == job_id
        and _positive_int(work.origin_imported_story_arc_id)
        and _positive_int(work.origin_imported_story_arc_entry_id)
        and set(payload) == _IMPORT_PLACEMENT_PAYLOAD_KEYS
        and _positive_int(schema_version)
        and schema_version == 1
        and _positive_int(sync_work_id)
        and sync_work_id == work.id
        and _positive_int(payload_membership_id)
        and payload_membership_id == membership_id
        and isinstance(desired_generation, str)
        and desired_generation == work.desired_generation
        and _positive_int(imported_story_arc_id)
        and imported_story_arc_id == work.origin_imported_story_arc_id
        and _positive_int(imported_story_arc_entry_id)
        and imported_story_arc_entry_id == work.origin_imported_story_arc_entry_id
        and _positive_int(source_import_job_id)
        and source_import_job_id == work.origin_import_job_id
    )


def _is_exact_unpublished_import_work(
    work: StoryArcSyncWork,
    action: ImportJobAction | None,
    staged_arc: ImportedStoryArc | None,
    staged_entry: ImportedStoryArcEntry | None,
    *,
    placement_action_ids: frozenset[int],
) -> bool:
    """Return whether one held row is safe to discard with import history."""
    if (
        work.claimable
        or work.state is not StoryArcSyncWorkState.QUEUED
        or work.attempt_count != 0
        or work.next_attempt_at is not None
        or work.claim_token is not None
        or work.claimed_at is not None
        or work.cancel_requested_at is not None
        or work.last_error_code is not None
        or work.last_error_category is not None
        or work.last_error_detail is not None
        or bool(dict(work.last_result or {}))
        or action is None
        or staged_arc is None
        or staged_entry is None
        or action.id in placement_action_ids
    ):
        return False
    job_id = work.origin_import_job_id
    return bool(
        isinstance(job_id, int)
        and job_id > 0
        and action.id == work.origin_import_action_id
        and action.import_job_id == job_id
        and action.phase == _IMPORT_PLACEMENT_PHASE
        and action.action_type == _IMPORT_PLACEMENT_ACTION_TYPE
        and action.status is ImportJobActionStatus.COMPLETED
        and _origin_payload_matches(
            action,
            work,
            job_id=job_id,
            membership_id=int(work.issue_story_arc_id),
        )
        and staged_arc.id == work.origin_imported_story_arc_id
        and staged_arc.import_job_id == job_id
        and staged_entry.id == work.origin_imported_story_arc_entry_id
        and staged_entry.imported_story_arc_id == staged_arc.id
        and staged_entry.materialized_membership_id == work.issue_story_arc_id
    )


async def discard_unpublished_import_story_arc_sync_work(
    session: AsyncSession,
    job_ids: Sequence[int],
) -> int:
    """Remove only exact, never-published placement reservations for deleted history.

    Validation completes before the bulk delete. Any attempted, malformed, or
    artifact-owning row must retain its import provenance and use rollback.
    """
    normalized_job_ids = tuple(sorted({int(job_id) for job_id in job_ids if job_id > 0}))
    if not normalized_job_ids:
        return 0

    unsafe_work_id = await session.scalar(
        select(StoryArcSyncWork.id)
        .where(
            StoryArcSyncWork.origin_import_job_id.in_(normalized_job_ids),
            StoryArcSyncWork.claimable.is_(True),
            StoryArcSyncWork.state.in_(
                {
                    StoryArcSyncWorkState.QUEUED,
                    StoryArcSyncWorkState.RUNNING,
                    StoryArcSyncWorkState.RETRY_WAIT,
                    StoryArcSyncWorkState.FAILED,
                }
            ),
        )
        .order_by(StoryArcSyncWork.id.asc())
        .limit(1)
    )
    if unsafe_work_id is not None:
        raise ValidationError(
            "Import history cannot be deleted while Story Arc placement work "
            "needs rollback. Roll back this import before deleting its history."
        )

    last_work_id = 0
    while True:
        rows = list(
            (
                await session.execute(
                    select(
                        StoryArcSyncWork,
                        ImportJobAction,
                        ImportedStoryArc,
                        ImportedStoryArcEntry,
                    )
                    .outerjoin(
                        ImportJobAction,
                        ImportJobAction.id == StoryArcSyncWork.origin_import_action_id,
                    )
                    .outerjoin(
                        ImportedStoryArc,
                        ImportedStoryArc.id == StoryArcSyncWork.origin_imported_story_arc_id,
                    )
                    .outerjoin(
                        ImportedStoryArcEntry,
                        ImportedStoryArcEntry.id
                        == StoryArcSyncWork.origin_imported_story_arc_entry_id,
                    )
                    .where(
                        StoryArcSyncWork.origin_import_job_id.in_(normalized_job_ids),
                        StoryArcSyncWork.claimable.is_(False),
                        StoryArcSyncWork.id > last_work_id,
                    )
                    .order_by(StoryArcSyncWork.id.asc())
                    .limit(_IMPORT_HISTORY_CLEANUP_PAGE_SIZE)
                )
            ).all()
        )
        if not rows:
            break
        action_ids = tuple(
            int(action.id)
            for _work, action, _staged_arc, _staged_entry in rows
            if action is not None
        )
        placement_action_ids = frozenset(
            int(action_id)
            for action_id in (
                (
                    await session.scalars(
                        select(StoryArcPlacement.creating_action_id).where(
                            StoryArcPlacement.creating_action_id.in_(action_ids)
                        )
                    )
                ).all()
                if action_ids
                else ()
            )
            if action_id is not None
        )
        for work, action, staged_arc, staged_entry in rows:
            if not _is_exact_unpublished_import_work(
                work,
                action,
                staged_arc,
                staged_entry,
                placement_action_ids=placement_action_ids,
            ):
                raise ValidationError(
                    "Import history cannot be deleted while Story Arc placement work "
                    "needs rollback. Roll back this import before deleting its history."
                )
        last_work_id = int(rows[-1][0].id)

    result = await session.execute(
        delete(StoryArcSyncWork).where(
            StoryArcSyncWork.origin_import_job_id.in_(normalized_job_ids),
            StoryArcSyncWork.claimable.is_(False),
        )
    )
    cursor_result = cast("CursorResult[Any]", result)
    return max(int(cursor_result.rowcount or 0), 0)


def _validate_import_story_arc_sync_proposal(
    job: ImportJob,
    proposal: ImportStoryArcSyncProposal,
) -> None:
    library_file = proposal.library_file
    membership = proposal.membership
    story_arc = proposal.story_arc
    if (
        not _positive_int(job.id)
        or not _positive_int(library_file.id)
        or not _positive_int(membership.id)
        or not _positive_int(story_arc.id)
        or not _positive_int(proposal.imported_story_arc_id)
        or not _positive_int(proposal.imported_story_arc_entry_id)
        or membership.story_arc_id != story_arc.id
        or membership.issue_id is None
        or membership.issue_id != library_file.issue_id
        or membership.resolution_state is not StoryArcResolutionState.RESOLVED
        or not _import_managed_policy_configured(story_arc)
    ):
        raise StoryArcPlacementIntegrationError(
            "import_sync_context_invalid",
            "Import Story Arc placement context is incomplete or no longer exact",
            category="validation",
        )


def _import_story_arc_work_insert_statement(*, returning: bool) -> Any:
    statement = insert(StoryArcSyncWork)
    return statement.returning(StoryArcSyncWork) if returning else statement


def _import_story_arc_work_row(
    *,
    job_id: int,
    action_id: int,
    prepared: _PreparedImportStoryArcSyncProposal,
) -> dict[str, Any]:
    proposal = prepared.proposal
    library_file = proposal.library_file
    membership = proposal.membership
    story_arc = proposal.story_arc
    return {
        "issue_story_arc_id": membership.id,
        "library_file_id": library_file.id,
        "origin_import_action_id": action_id,
        "origin_import_job_id": job_id,
        "origin_imported_story_arc_id": proposal.imported_story_arc_id,
        "origin_imported_story_arc_entry_id": proposal.imported_story_arc_entry_id,
        "desired_generation": prepared.desired_generation,
        "source_signature_hash": prepared.source_signature_hash,
        "source_file_path": library_file.file_path,
        "source_file_size": library_file.file_size,
        "source_file_modified_at": library_file.file_modified_at,
        "source_file_hash": library_file.file_hash,
        "source_signature_schema_version": _source_signature_int(
            library_file,
            "schema_version",
        ),
        "source_signature_resolved_path": _source_signature_path(library_file),
        "source_signature_size": _source_signature_int(library_file, "size"),
        "source_signature_mtime_ns": _source_signature_int(library_file, "mtime_ns"),
        "source_signature_device": _source_signature_int(library_file, "device"),
        "source_signature_inode": _source_signature_int(library_file, "inode"),
        "story_arc_revision": story_arc.revision,
        "membership_sequence": membership.sequence_number,
        "policy_schema_version": story_arc.policy_schema_version
        or STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION,
        "reason": StoryArcSyncReason.CANONICAL_REGISTERED,
        "claimable": False,
        "state": StoryArcSyncWorkState.QUEUED,
        "attempt_count": 0,
        "last_result": {},
    }


def _provisional_import_story_arc_action_payload(
    *,
    job_id: int,
    prepared: _PreparedImportStoryArcSyncProposal,
) -> dict[str, Any]:
    proposal = prepared.proposal
    return {
        "schema_version": 1,
        "sync_work_id": None,
        "membership_id": proposal.membership.id,
        "desired_generation": prepared.desired_generation,
        "imported_story_arc_id": proposal.imported_story_arc_id,
        "imported_story_arc_entry_id": proposal.imported_story_arc_entry_id,
        "source_import_job_id": job_id,
    }


def _invalid_existing_origin() -> StoryArcPlacementIntegrationError:
    return StoryArcPlacementIntegrationError(
        "import_sync_existing_origin_invalid",
        "Existing import placement work has an invalid origin binding",
        category="ownership",
    )


def _unusable_existing_work() -> StoryArcPlacementIntegrationError:
    return StoryArcPlacementIntegrationError(
        "import_sync_existing_work_unusable",
        "Existing import placement work is terminal without completed placement evidence",
        category="conflict",
    )


def _invalid_completed_placement() -> StoryArcPlacementIntegrationError:
    return StoryArcPlacementIntegrationError(
        "import_sync_completed_placement_invalid",
        "Completed import placement work lacks one exact owned placement",
        category="ownership",
    )


def _unverified_non_origin_placement() -> StoryArcPlacementIntegrationError:
    return StoryArcPlacementIntegrationError(
        "import_sync_non_origin_placement_unverified",
        "Existing non-import work lacks one exact current placement",
        category="conflict",
    )


def _work_matches_prepared_generation(
    work: StoryArcSyncWork,
    prepared: _PreparedImportStoryArcSyncProposal,
) -> bool:
    proposal = prepared.proposal
    return bool(
        work.issue_story_arc_id == proposal.membership.id
        and work.library_file_id == proposal.library_file.id
        and work.desired_generation == prepared.desired_generation
        and work.source_signature_hash == prepared.source_signature_hash
        and work.story_arc_revision == proposal.story_arc.revision
        and work.membership_sequence == proposal.membership.sequence_number
        and work.policy_schema_version
        == (proposal.story_arc.policy_schema_version or STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION)
    )


def _placement_matches_work_generation(
    placement: StoryArcPlacement,
    work: StoryArcSyncWork,
) -> bool:
    mode_ownership_valid = bool(
        (
            placement.mode is StoryArcPlacementMode.REFERENCE_ONLY
            and placement.ownership is StoryArcPlacementOwnership.REFERENCED
        )
        or (
            placement.mode
            in {
                StoryArcPlacementMode.COPY,
                StoryArcPlacementMode.HARDLINK,
                StoryArcPlacementMode.SYMLINK,
            }
            and placement.ownership is StoryArcPlacementOwnership.MANAGED
        )
    )
    return bool(
        placement.issue_story_arc_id == work.issue_story_arc_id
        and placement.library_file_id == work.library_file_id
        and placement.rendered_reading_order == work.membership_sequence
        and placement.policy_schema_version == work.policy_schema_version
        and placement.state is StoryArcPlacementState.CURRENT
        and placement.operation_token is None
        and mode_ownership_valid
    )


def _completed_origin_placement_matches(
    placement: StoryArcPlacement,
    work: StoryArcSyncWork,
) -> bool:
    return bool(
        _placement_matches_work_generation(placement, work)
        and placement.ownership is StoryArcPlacementOwnership.MANAGED
        and placement.mode
        in {
            StoryArcPlacementMode.COPY,
            StoryArcPlacementMode.HARDLINK,
            StoryArcPlacementMode.SYMLINK,
        }
        and placement.source_kind is StoryArcSourceKind.PULLBOX
        and dict(placement.last_result or {}).get("status") == "complete"
    )


def _existing_work_placement_statement(
    *,
    action_ids: Sequence[int],
    membership_library_pairs: Sequence[tuple[int, int]],
) -> Any:
    """Load bounded evidence for origin ownership and non-origin generation checks."""
    criteria: list[Any] = []
    if action_ids:
        criteria.append(StoryArcPlacement.creating_action_id.in_(action_ids))
    if membership_library_pairs:
        criteria.append(
            tuple_(
                StoryArcPlacement.issue_story_arc_id,
                StoryArcPlacement.library_file_id,
            ).in_(membership_library_pairs)
        )
    if not criteria:
        raise ValueError("Existing-work placement lookup requires at least one exact key")
    return select(StoryArcPlacement).where(or_(*criteria)).order_by(StoryArcPlacement.id.asc())


def _classify_existing_import_work(
    *,
    job_id: int,
    prepared: _PreparedImportStoryArcSyncProposal,
    work: StoryArcSyncWork,
    action: ImportJobAction | None,
    origin_binding: tuple[int, int | None, int, int | None] | None,
    pair_placements: Sequence[StoryArcPlacement],
    action_placements: Sequence[StoryArcPlacement],
) -> StoryArcImportSyncEnqueueResult:
    proposal = prepared.proposal
    if not _work_matches_prepared_generation(work, prepared):
        if work.origin_import_action_id is not None:
            raise _invalid_existing_origin()
        raise _unverified_non_origin_placement()
    if work.origin_import_action_id is None:
        if any(
            value is not None
            for value in (
                work.origin_import_job_id,
                work.origin_imported_story_arc_id,
                work.origin_imported_story_arc_entry_id,
            )
        ):
            raise _invalid_existing_origin()
        exact_placements = [
            placement
            for placement in pair_placements
            if _placement_matches_work_generation(placement, work)
        ]
        if len(exact_placements) != 1:
            raise _unverified_non_origin_placement()
        return StoryArcImportSyncEnqueueResult(
            work=work,
            action=None,
            classification="existing_non_origin_placement",
            desired_generation=prepared.desired_generation,
        )
    if (
        action is None
        or action.id != work.origin_import_action_id
        or action.import_job_id != job_id
        or action.phase != _IMPORT_PLACEMENT_PHASE
        or action.action_type != _IMPORT_PLACEMENT_ACTION_TYPE
        or action.status is not ImportJobActionStatus.COMPLETED
        or work.origin_import_job_id != job_id
        or origin_binding
        != (
            work.origin_imported_story_arc_id,
            work.issue_story_arc_id,
            job_id,
            proposal.story_arc.id,
        )
        or not _origin_payload_matches(
            action,
            work,
            job_id=job_id,
            membership_id=int(proposal.membership.id),
        )
    ):
        raise _invalid_existing_origin()
    same_staged_origin = bool(
        work.origin_imported_story_arc_id == proposal.imported_story_arc_id
        and work.origin_imported_story_arc_entry_id == proposal.imported_story_arc_entry_id
    )
    if not same_staged_origin and (
        work.origin_imported_story_arc_id != proposal.imported_story_arc_id
    ):
        raise _invalid_existing_origin()
    if work.state in _PENDING_IMPORT_WORK_STATES:
        classification = (
            "existing_import_work_pending"
            if same_staged_origin
            else "existing_import_membership_duplicate"
        )
    elif work.state is StoryArcSyncWorkState.COMPLETED:
        if (
            len(action_placements) != 1
            or action_placements[0].source_import_job_id != job_id
            or action_placements[0].creating_action_id != action.id
            or not _completed_origin_placement_matches(action_placements[0], work)
        ):
            raise _invalid_completed_placement()
        classification = (
            "existing_import_work_completed"
            if same_staged_origin
            else "existing_import_membership_duplicate"
        )
    else:
        raise _unusable_existing_work()
    return StoryArcImportSyncEnqueueResult(
        work=work,
        action=action,
        classification=classification,
        desired_generation=prepared.desired_generation,
    )


async def enqueue_import_story_arc_sync_work_batch(
    session: AsyncSession,
    *,
    job: ImportJob,
    proposals: Sequence[ImportStoryArcSyncProposal],
    record_actions: RecordActionsFunc,
) -> list[StoryArcImportSyncEnqueueResult]:
    """Create a bounded ordered batch of exact import action/work bindings.

    All proposal, staged-origin, duplicate, existing-work, and placement checks
    finish before the callback is allowed to write the first journal action.
    The caller owns the surrounding transaction; this helper never commits.
    """
    ordered_proposals = tuple(proposals)
    if not ordered_proposals:
        return []
    if len(ordered_proposals) > MAX_IMPORT_STORY_ARC_SYNC_ENQUEUE_BATCH_SIZE:
        raise ValueError(
            "Import Story Arc placement enqueue accepts at most "
            f"{MAX_IMPORT_STORY_ARC_SYNC_ENQUEUE_BATCH_SIZE} proposals"
        )
    for proposal in ordered_proposals:
        _validate_import_story_arc_sync_proposal(job, proposal)
    if (
        job.status is not ImportJobStatus.IMPORTING
        or job.control_request is not ImportControlRequest.NONE
        or dict(job.progress_snapshot or {}).get("phase") not in _IMPORT_ENQUEUE_PHASES
    ):
        raise StoryArcPlacementIntegrationError(
            "import_sync_job_inactive",
            "Import job is not actively publishing Story Arc placements",
            category="cancelled",
        )

    prepared = [
        _PreparedImportStoryArcSyncProposal(
            proposal=proposal,
            desired_generation=generation,
            source_signature_hash=source_hash,
        )
        for proposal in ordered_proposals
        for generation, source_hash in [
            _desired_generation(
                proposal.library_file,
                proposal.membership,
                proposal.story_arc,
            )
        ]
    ]
    entry_ids = sorted({item.proposal.imported_story_arc_entry_id for item in prepared})
    staged_rows = (
        await session.execute(
            select(
                ImportedStoryArcEntry.id,
                ImportedStoryArcEntry.imported_story_arc_id,
                ImportedStoryArcEntry.materialized_membership_id,
                ImportedStoryArc.import_job_id,
                ImportedStoryArc.materialized_story_arc_id,
            )
            .join(
                ImportedStoryArc,
                ImportedStoryArcEntry.imported_story_arc_id == ImportedStoryArc.id,
            )
            .where(ImportedStoryArcEntry.id.in_(entry_ids))
        )
    ).all()
    staged_bindings = {
        int(entry_id): (
            int(imported_story_arc_id),
            materialized_membership_id,
            int(import_job_id),
            materialized_story_arc_id,
        )
        for (
            entry_id,
            imported_story_arc_id,
            materialized_membership_id,
            import_job_id,
            materialized_story_arc_id,
        ) in staged_rows
    }
    for item in prepared:
        proposal = item.proposal
        if staged_bindings.get(proposal.imported_story_arc_entry_id) != (
            proposal.imported_story_arc_id,
            proposal.membership.id,
            job.id,
            proposal.story_arc.id,
        ):
            raise StoryArcPlacementIntegrationError(
                "import_sync_origin_binding_invalid",
                "Imported Story Arc entry does not own the requested membership",
                category="ownership",
            )

    imported_arc_by_key: dict[tuple[int, str], int] = {}
    for item in prepared:
        prior_imported_arc_id = imported_arc_by_key.setdefault(
            item.key,
            item.proposal.imported_story_arc_id,
        )
        if prior_imported_arc_id != item.proposal.imported_story_arc_id:
            raise _invalid_existing_origin()

    keys = sorted(imported_arc_by_key)
    existing_work_rows = list(
        (
            await session.scalars(
                select(StoryArcSyncWork).where(
                    tuple_(
                        StoryArcSyncWork.issue_story_arc_id,
                        StoryArcSyncWork.desired_generation,
                    ).in_(keys)
                )
            )
        ).all()
    )
    work_by_key = {
        (work.issue_story_arc_id, work.desired_generation): work for work in existing_work_rows
    }
    missing_origin_entry_ids = sorted(
        {
            int(work.origin_imported_story_arc_entry_id)
            for work in existing_work_rows
            if work.origin_imported_story_arc_entry_id is not None
            and int(work.origin_imported_story_arc_entry_id) not in staged_bindings
        }
    )
    if missing_origin_entry_ids:
        origin_staged_rows = (
            await session.execute(
                select(
                    ImportedStoryArcEntry.id,
                    ImportedStoryArcEntry.imported_story_arc_id,
                    ImportedStoryArcEntry.materialized_membership_id,
                    ImportedStoryArc.import_job_id,
                    ImportedStoryArc.materialized_story_arc_id,
                )
                .join(
                    ImportedStoryArc,
                    ImportedStoryArcEntry.imported_story_arc_id == ImportedStoryArc.id,
                )
                .where(ImportedStoryArcEntry.id.in_(missing_origin_entry_ids))
            )
        ).all()
        staged_bindings.update(
            {
                int(entry_id): (
                    int(imported_story_arc_id),
                    materialized_membership_id,
                    int(import_job_id),
                    materialized_story_arc_id,
                )
                for (
                    entry_id,
                    imported_story_arc_id,
                    materialized_membership_id,
                    import_job_id,
                    materialized_story_arc_id,
                ) in origin_staged_rows
            }
        )
    action_ids = sorted(
        {
            int(work.origin_import_action_id)
            for work in existing_work_rows
            if work.origin_import_action_id is not None
        }
    )
    actions_by_id: dict[int, ImportJobAction] = {}
    if action_ids:
        actions_by_id = {
            int(action.id): action
            for action in (
                await session.scalars(
                    select(ImportJobAction).where(ImportJobAction.id.in_(action_ids))
                )
            ).all()
        }

    placement_rows: list[StoryArcPlacement] = []
    if existing_work_rows:
        placement_rows = list(
            (
                await session.scalars(
                    _existing_work_placement_statement(
                        action_ids=action_ids,
                        membership_library_pairs=sorted(
                            {
                                (work.issue_story_arc_id, work.library_file_id)
                                for work in existing_work_rows
                            }
                        ),
                    )
                )
            ).all()
        )
    placements_by_pair: dict[tuple[int, int], list[StoryArcPlacement]] = {}
    placements_by_action: dict[int, list[StoryArcPlacement]] = {}
    for placement in placement_rows:
        if placement.library_file_id is not None:
            placements_by_pair.setdefault(
                (int(placement.issue_story_arc_id), int(placement.library_file_id)),
                [],
            ).append(placement)
        if placement.creating_action_id is not None:
            placements_by_action.setdefault(int(placement.creating_action_id), []).append(placement)

    representative_index_by_key: dict[tuple[int, str], int] = {}
    for index, item in enumerate(prepared):
        prior_index = representative_index_by_key.get(item.key)
        if (
            prior_index is None
            or item.proposal.imported_story_arc_entry_id
            < prepared[prior_index].proposal.imported_story_arc_entry_id
        ):
            representative_index_by_key[item.key] = index
    representative_by_key = {
        key: prepared[index] for key, index in representative_index_by_key.items()
    }
    existing_results_by_key: dict[tuple[int, str], StoryArcImportSyncEnqueueResult] = {}
    for key, work in work_by_key.items():
        item = representative_by_key[key]
        existing_results_by_key[key] = _classify_existing_import_work(
            job_id=int(job.id),
            prepared=item,
            work=work,
            action=(
                actions_by_id.get(int(work.origin_import_action_id))
                if work.origin_import_action_id is not None
                else None
            ),
            origin_binding=(
                staged_bindings.get(int(work.origin_imported_story_arc_entry_id))
                if work.origin_imported_story_arc_entry_id is not None
                else None
            ),
            pair_placements=placements_by_pair.get(
                (int(work.issue_story_arc_id), int(work.library_file_id)),
                (),
            ),
            action_placements=(
                placements_by_action.get(int(work.origin_import_action_id), ())
                if work.origin_import_action_id is not None
                else ()
            ),
        )

    memberships_without_work = sorted(
        {item.key[0] for item in prepared if item.key not in existing_results_by_key}
    )
    placements_by_membership: dict[int, StoryArcPlacement] = {}
    if memberships_without_work:
        first_placement_ids = (
            select(
                StoryArcPlacement.issue_story_arc_id.label("membership_id"),
                func.min(StoryArcPlacement.id).label("placement_id"),
            )
            .where(StoryArcPlacement.issue_story_arc_id.in_(memberships_without_work))
            .group_by(StoryArcPlacement.issue_story_arc_id)
            .subquery()
        )
        placements_by_membership = {
            int(placement.issue_story_arc_id): placement
            for placement in (
                await session.scalars(
                    select(StoryArcPlacement).join(
                        first_placement_ids,
                        StoryArcPlacement.id == first_placement_ids.c.placement_id,
                    )
                )
            ).all()
        }

    results: list[StoryArcImportSyncEnqueueResult | None] = [None for _item in prepared]
    creation_items: list[_PreparedImportStoryArcSyncProposal] = []
    creation_index_by_key: dict[tuple[int, str], int] = {}
    duplicate_indexes_by_key: dict[tuple[int, str], list[int]] = {}
    for index, item in enumerate(prepared):
        representative_index = representative_index_by_key[item.key]
        if representative_index != index:
            duplicate_indexes_by_key.setdefault(item.key, []).append(index)
            continue
        existing_result = existing_results_by_key.get(item.key)
        if existing_result is not None:
            results[index] = existing_result
            continue
        existing_placement = placements_by_membership.get(item.key[0])
        if existing_placement is not None:
            results[index] = StoryArcImportSyncEnqueueResult(
                work=None,
                action=None,
                classification=(
                    "existing_managed_placement"
                    if existing_placement.ownership is StoryArcPlacementOwnership.MANAGED
                    else "existing_referenced_placement"
                ),
                desired_generation=item.desired_generation,
            )
            continue
        creation_index_by_key[item.key] = index
        creation_items.append(item)

    if creation_items:
        provisional_payloads = [
            _provisional_import_story_arc_action_payload(
                job_id=int(job.id),
                prepared=item,
            )
            for item in creation_items
        ]
        specs = [
            ImportJobActionSpec(
                phase=_IMPORT_PLACEMENT_PHASE,
                action_type=_IMPORT_PLACEMENT_ACTION_TYPE,
                payload=payload,
            )
            for payload in provisional_payloads
        ]
        actions = await record_actions(session, job, specs)
        if (
            len(actions) != len(specs)
            or len({action.id for action in actions}) != len(actions)
            or any(
                not _positive_int(action.id)
                or action.import_job_id != job.id
                or action.phase != spec.phase
                or action.action_type != spec.action_type
                or dict(action.payload or {}) != spec.payload
                for action, spec in zip(actions, specs, strict=True)
            )
        ):
            raise StoryArcPlacementIntegrationError(
                "import_sync_action_invalid",
                "Import action recorder returned an invalid ownership action",
                category="ownership",
            )
        work_rows = [
            _import_story_arc_work_row(
                job_id=int(job.id),
                action_id=int(action.id),
                prepared=item,
            )
            for item, action in zip(creation_items, actions, strict=True)
        ]
        works = list(
            (
                await session.scalars(
                    _import_story_arc_work_insert_statement(returning=True),
                    work_rows,
                )
            ).all()
        )
        work_by_action_id = {
            int(work.origin_import_action_id): work
            for work in works
            if work.origin_import_action_id is not None
        }
        if len(work_by_action_id) != len(actions):
            raise StoryArcPlacementIntegrationError(
                "import_sync_action_invalid",
                "Import action recorder returned an invalid ownership action",
                category="ownership",
            )
        for item, action, payload in zip(
            creation_items,
            actions,
            provisional_payloads,
            strict=True,
        ):
            created_work = work_by_action_id.get(int(action.id))
            if created_work is None or not _positive_int(created_work.id):
                raise StoryArcPlacementIntegrationError(
                    "import_sync_action_invalid",
                    "Import action recorder returned an invalid ownership action",
                    category="ownership",
                )
            action.payload = {**payload, "sync_work_id": int(created_work.id)}
            creation_result = StoryArcImportSyncEnqueueResult(
                work=created_work,
                action=action,
                classification="created",
                desired_generation=item.desired_generation,
            )
            results[creation_index_by_key[item.key]] = creation_result
        await session.flush()

    for key, duplicate_indexes in duplicate_indexes_by_key.items():
        representative_index = representative_index_by_key[key]
        representative_result = results[representative_index]
        if representative_result is None:
            raise RuntimeError("Import Story Arc placement duplicate lost its representative")
        for duplicate_index in duplicate_indexes:
            duplicate = prepared[duplicate_index]
            representative = prepared[representative_index]
            results[duplicate_index] = StoryArcImportSyncEnqueueResult(
                work=representative_result.work,
                action=representative_result.action,
                classification=(
                    "in_call_duplicate"
                    if duplicate.origin == representative.origin
                    else "in_call_membership_duplicate"
                ),
                desired_generation=representative_result.desired_generation,
            )

    if any(result is None for result in results):
        raise RuntimeError("Import Story Arc placement enqueue left an unclassified proposal")
    return [cast("StoryArcImportSyncEnqueueResult", result) for result in results]


async def enqueue_import_story_arc_sync_work(
    session: AsyncSession,
    *,
    job: ImportJob,
    library_file: LibraryFile,
    membership: IssueStoryArc,
    story_arc: StoryArc,
    imported_story_arc_id: int,
    imported_story_arc_entry_id: int,
    record_action: RecordActionFunc,
) -> StoryArcImportSyncEnqueueResult:
    """Compatibility wrapper for one import-owned placement proposal."""

    async def record_actions_adapter(
        callback_session: AsyncSession,
        callback_job: ImportJob,
        specs: Sequence[ImportJobActionSpec],
    ) -> list[ImportJobAction]:
        return [
            await record_action(
                callback_session,
                callback_job,
                phase=spec.phase,
                action_type=spec.action_type,
                payload=spec.payload,
            )
            for spec in specs
        ]

    results = await enqueue_import_story_arc_sync_work_batch(
        session,
        job=job,
        proposals=[
            ImportStoryArcSyncProposal(
                library_file=library_file,
                membership=membership,
                story_arc=story_arc,
                imported_story_arc_id=imported_story_arc_id,
                imported_story_arc_entry_id=imported_story_arc_entry_id,
            )
        ],
        record_actions=cast("RecordActionsFunc", record_actions_adapter),
    )
    return results[0]


async def discover_story_arc_sync_work(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_STORY_ARC_DISCOVERY_LIMIT,
) -> int:
    """Boundedly recover eligible canonical files with no current work generation."""
    if isinstance(limit, bool) or not 1 <= limit <= DEFAULT_STORY_ARC_DISCOVERY_LIMIT:
        raise ValueError(
            f"Story-arc discrepancy limit must be from 1 to {DEFAULT_STORY_ARC_DISCOVERY_LIMIT}"
        )

    current_work = exists().where(
        StoryArcSyncWork.issue_story_arc_id == IssueStoryArc.id,
        StoryArcSyncWork.library_file_id == LibraryFile.id,
        StoryArcSyncWork.story_arc_revision == StoryArc.revision,
        StoryArcSyncWork.membership_sequence == IssueStoryArc.sequence_number,
        StoryArcSyncWork.policy_schema_version == StoryArc.policy_schema_version,
        StoryArcSyncWork.source_file_path == LibraryFile.file_path,
        StoryArcSyncWork.source_file_size == LibraryFile.file_size,
        StoryArcSyncWork.source_file_modified_at == LibraryFile.file_modified_at,
        or_(
            StoryArcSyncWork.source_file_hash == LibraryFile.file_hash,
            and_(
                StoryArcSyncWork.source_file_hash.is_(None),
                LibraryFile.file_hash.is_(None),
            ),
        ),
        or_(
            StoryArcSyncWork.source_signature_schema_version
            == LibraryFile.source_signature["schema_version"].as_integer(),
            and_(
                StoryArcSyncWork.source_signature_schema_version.is_(None),
                LibraryFile.source_signature["schema_version"].as_integer().is_(None),
            ),
        ),
        or_(
            StoryArcSyncWork.source_signature_resolved_path
            == LibraryFile.source_signature["resolved_path"].as_string(),
            and_(
                StoryArcSyncWork.source_signature_resolved_path.is_(None),
                LibraryFile.source_signature["resolved_path"].as_string().is_(None),
            ),
        ),
        or_(
            StoryArcSyncWork.source_signature_size
            == LibraryFile.source_signature["size"].as_integer(),
            and_(
                StoryArcSyncWork.source_signature_size.is_(None),
                LibraryFile.source_signature["size"].as_integer().is_(None),
            ),
        ),
        or_(
            StoryArcSyncWork.source_signature_mtime_ns
            == LibraryFile.source_signature["mtime_ns"].as_integer(),
            and_(
                StoryArcSyncWork.source_signature_mtime_ns.is_(None),
                LibraryFile.source_signature["mtime_ns"].as_integer().is_(None),
            ),
        ),
        or_(
            StoryArcSyncWork.source_signature_device
            == LibraryFile.source_signature["device"].as_integer(),
            and_(
                StoryArcSyncWork.source_signature_device.is_(None),
                LibraryFile.source_signature["device"].as_integer().is_(None),
            ),
        ),
        or_(
            StoryArcSyncWork.source_signature_inode
            == LibraryFile.source_signature["inode"].as_integer(),
            and_(
                StoryArcSyncWork.source_signature_inode.is_(None),
                LibraryFile.source_signature["inode"].as_integer().is_(None),
            ),
        ),
    )
    rows = list(
        (
            await session.execute(
                select(LibraryFile, IssueStoryArc, StoryArc)
                .join(IssueStoryArc, LibraryFile.issue_id == IssueStoryArc.issue_id)
                .join(StoryArc, IssueStoryArc.story_arc_id == StoryArc.id)
                .where(
                    IssueStoryArc.sync_eligible.is_(True),
                    IssueStoryArc.resolution_state == StoryArcResolutionState.RESOLVED,
                    StoryArc.lifecycle == StoryArcLifecycle.ACTIVE,
                    StoryArc.sync_enabled.is_(True),
                    StoryArc.policy_schema_version == STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION,
                    ~current_work,
                )
                .order_by(IssueStoryArc.id.asc(), LibraryFile.id.asc())
                .limit(limit)
            )
        ).all()
    )
    queued = 0
    for library_file, membership, story_arc in rows:
        if not _automatic_sync_enabled(story_arc):
            continue
        queued += await _enqueue_pairs(
            session,
            library_file,
            [(membership, story_arc)],
            reason=StoryArcSyncReason.DISCREPANCY_RECOVERY,
        )
    return queued


async def claim_story_arc_sync_work(
    session: AsyncSession,
    work_id: int,
    *,
    now: datetime,
    import_only: bool = False,
) -> str | None:
    """Atomically lease one ready or stale work row before synchronization I/O."""
    token = secrets.token_urlsafe(24)
    stale_before = now - _CLAIM_LEASE
    origin_scope = (StoryArcSyncWork.origin_import_job_id.is_not(None),) if import_only else ()
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(StoryArcSyncWork)
            .where(
                *origin_scope,
                _origin_is_not_startup_recovery_paused(),
                StoryArcSyncWork.id == work_id,
                StoryArcSyncWork.claimable.is_(True),
                or_(
                    StoryArcSyncWork.state == StoryArcSyncWorkState.QUEUED,
                    and_(
                        StoryArcSyncWork.state == StoryArcSyncWorkState.RETRY_WAIT,
                        StoryArcSyncWork.next_attempt_at.is_not(None),
                        StoryArcSyncWork.next_attempt_at <= now,
                    ),
                    and_(
                        StoryArcSyncWork.state == StoryArcSyncWorkState.RUNNING,
                        or_(
                            StoryArcSyncWork.claimed_at.is_(None),
                            StoryArcSyncWork.claimed_at <= stale_before,
                        ),
                    ),
                ),
            )
            .values(
                state=StoryArcSyncWorkState.RUNNING,
                attempt_count=StoryArcSyncWork.attempt_count + 1,
                next_attempt_at=None,
                claim_token=token,
                claimed_at=now,
            )
        ),
    )
    await session.commit()
    return token if result.rowcount == 1 else None


async def _refresh_story_arc_sync_claim(
    session: AsyncSession,
    work_id: int,
    claim_token: str,
    *,
    now: datetime,
) -> bool:
    """Refresh one live claim without reviving work owned by another worker."""
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(StoryArcSyncWork)
            .where(
                StoryArcSyncWork.id == work_id,
                StoryArcSyncWork.claim_token == claim_token,
                StoryArcSyncWork.state == StoryArcSyncWorkState.RUNNING,
            )
            .values(claimed_at=now)
        ),
    )
    await session.commit()
    return result.rowcount == 1


async def _maintain_claim_lease(
    session_factory: async_sessionmaker[AsyncSession],
    work_id: int,
    claim_token: str,
    stop_requested: asyncio.Event,
    *,
    interval_seconds: float,
    now_fn: Callable[[], datetime],
) -> None:
    """Heartbeat a live claim while placement I/O runs in a different session."""
    while not stop_requested.is_set():
        try:
            await asyncio.wait_for(stop_requested.wait(), timeout=interval_seconds)
        except TimeoutError:
            try:
                async with session_factory() as heartbeat_session:
                    refreshed = await _refresh_story_arc_sync_claim(
                        heartbeat_session,
                        work_id,
                        claim_token,
                        now=now_fn(),
                    )
            except Exception:
                logger.warning(
                    "story_arc_sync_claim_heartbeat_failed",
                    work_id=work_id,
                    exc_info=True,
                )
                continue
            if not refreshed:
                logger.warning(
                    "story_arc_sync_claim_lost_during_heartbeat",
                    work_id=work_id,
                )
                return


async def _origin_claim_may_publish(
    session: AsyncSession,
    work_id: int,
    claim_token: str,
) -> bool:
    row = (
        await session.execute(
            select(StoryArcSyncWork, ImportJobAction, ImportJob)
            .join(
                ImportJobAction,
                StoryArcSyncWork.origin_import_action_id == ImportJobAction.id,
            )
            .join(ImportJob, StoryArcSyncWork.origin_import_job_id == ImportJob.id)
            .where(
                StoryArcSyncWork.id == work_id,
                StoryArcSyncWork.claim_token == claim_token,
                StoryArcSyncWork.state == StoryArcSyncWorkState.RUNNING,
            )
        )
    ).one_or_none()
    if row is None:
        return False
    work, action, job = row
    return bool(
        work.cancel_requested_at is None
        and action.import_job_id == work.origin_import_job_id
        and action.status is ImportJobActionStatus.COMPLETED
        and action.phase == _IMPORT_PLACEMENT_PHASE
        and action.action_type == _IMPORT_PLACEMENT_ACTION_TYPE
        and job.status is ImportJobStatus.IMPORTING
        and job.control_request is ImportControlRequest.NONE
        and dict(job.progress_snapshot or {}).get("phase") == _IMPORT_PLACEMENT_PHASE
        and _origin_payload_matches(
            action,
            work,
            job_id=job.id,
            membership_id=work.issue_story_arc_id,
        )
    )


async def _monitor_origin_cancellation(
    session_factory: async_sessionmaker[AsyncSession],
    work_id: int,
    claim_token: str,
    stop_requested: asyncio.Event,
    cancellation_requested: asyncio.Event,
    *,
    interval_seconds: float,
) -> None:
    """Poll only claimed import work and signal the filesystem's safe cancellation path."""
    while not stop_requested.is_set():
        try:
            await asyncio.wait_for(stop_requested.wait(), timeout=interval_seconds)
        except TimeoutError:
            try:
                async with session_factory() as session:
                    may_publish = await _origin_claim_may_publish(
                        session,
                        work_id,
                        claim_token,
                    )
                    await session.rollback()
            except Exception:
                logger.warning(
                    "story_arc_import_cancellation_check_failed",
                    work_id=work_id,
                    exc_info=True,
                )
                continue
            if not may_publish:
                cancellation_requested.set()
                return


def _ready_work_statements(
    *,
    now: datetime,
    limit: int,
    import_only: bool = False,
) -> tuple[Any, ...]:
    """Build one bounded, index-orderable query for each readiness lane."""
    stale_before = now - _CLAIM_LEASE
    origin_scope = (StoryArcSyncWork.origin_import_job_id.is_not(None),) if import_only else ()
    return (
        select(StoryArcSyncWork.id, StoryArcSyncWork.claimed_at)
        .where(
            *origin_scope,
            _origin_is_not_startup_recovery_paused(),
            StoryArcSyncWork.claimable.is_(True),
            StoryArcSyncWork.state == StoryArcSyncWorkState.RUNNING,
            StoryArcSyncWork.claimed_at.is_(None),
        )
        .order_by(StoryArcSyncWork.claimed_at.asc(), StoryArcSyncWork.id.asc())
        .limit(limit),
        select(StoryArcSyncWork.id, StoryArcSyncWork.created_at)
        .where(
            *origin_scope,
            _origin_is_not_startup_recovery_paused(),
            StoryArcSyncWork.claimable.is_(True),
            StoryArcSyncWork.state == StoryArcSyncWorkState.QUEUED,
        )
        .order_by(StoryArcSyncWork.created_at.asc(), StoryArcSyncWork.id.asc())
        .limit(limit),
        select(StoryArcSyncWork.id, StoryArcSyncWork.next_attempt_at)
        .where(
            *origin_scope,
            _origin_is_not_startup_recovery_paused(),
            StoryArcSyncWork.claimable.is_(True),
            StoryArcSyncWork.state == StoryArcSyncWorkState.RETRY_WAIT,
            StoryArcSyncWork.next_attempt_at.is_not(None),
            StoryArcSyncWork.next_attempt_at <= now,
        )
        .order_by(StoryArcSyncWork.next_attempt_at.asc(), StoryArcSyncWork.id.asc())
        .limit(limit),
        select(StoryArcSyncWork.id, StoryArcSyncWork.claimed_at)
        .where(
            *origin_scope,
            _origin_is_not_startup_recovery_paused(),
            StoryArcSyncWork.claimable.is_(True),
            StoryArcSyncWork.state == StoryArcSyncWorkState.RUNNING,
            StoryArcSyncWork.claimed_at.is_not(None),
            StoryArcSyncWork.claimed_at <= stale_before,
        )
        .order_by(StoryArcSyncWork.claimed_at.asc(), StoryArcSyncWork.id.asc())
        .limit(limit),
    )


async def _ready_work_ids(
    session: AsyncSession,
    *,
    now: datetime,
    limit: int,
    import_only: bool = False,
) -> list[int]:
    """Merge bounded ready lanes without an unindexable OR/COALESCE scan."""
    unclaimed_ready_at = datetime.min.replace(tzinfo=UTC)
    candidates: dict[int, tuple[datetime, int, int]] = {}
    for lane_rank, statement in enumerate(
        _ready_work_statements(now=now, limit=limit, import_only=import_only)
    ):
        rows = (await session.execute(statement)).all()
        for work_id, ready_at in rows:
            normalized_id = int(work_id)
            sort_key = (ready_at or unclaimed_ready_at, lane_rank, normalized_id)
            existing = candidates.get(normalized_id)
            if existing is None or sort_key < existing:
                candidates[normalized_id] = sort_key
    ordered = sorted(candidates.items(), key=lambda item: item[1])
    return [work_id for work_id, _sort_key in ordered[:limit]]


async def _load_claimed_context(
    session: AsyncSession,
    work_id: int,
    claim_token: str,
) -> _WorkContext | None:
    row = (
        await session.execute(
            select(
                StoryArcSyncWork,
                IssueStoryArc,
                StoryArc,
                LibraryFile,
                ImportJobAction,
                ImportJob,
            )
            .join(
                IssueStoryArc,
                StoryArcSyncWork.issue_story_arc_id == IssueStoryArc.id,
            )
            .join(StoryArc, IssueStoryArc.story_arc_id == StoryArc.id)
            .join(LibraryFile, StoryArcSyncWork.library_file_id == LibraryFile.id)
            .outerjoin(
                ImportJobAction,
                StoryArcSyncWork.origin_import_action_id == ImportJobAction.id,
            )
            .outerjoin(ImportJob, StoryArcSyncWork.origin_import_job_id == ImportJob.id)
            .where(
                StoryArcSyncWork.id == work_id,
                StoryArcSyncWork.claim_token == claim_token,
                StoryArcSyncWork.state == StoryArcSyncWorkState.RUNNING,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    work, membership, story_arc, library_file, origin_action, origin_job = row
    if (
        membership.issue_id is None
        or membership.issue_id != library_file.issue_id
        or membership.resolution_state is not StoryArcResolutionState.RESOLVED
        or work.desired_generation != _desired_generation(library_file, membership, story_arc)[0]
        or requires_order_review(membership)
    ):
        return None
    if work.origin_import_action_id is None:
        if (
            any(
                value is not None
                for value in (
                    work.origin_import_job_id,
                    work.origin_imported_story_arc_id,
                    work.origin_imported_story_arc_entry_id,
                )
            )
            or not membership.sync_eligible
            or not _automatic_sync_enabled(story_arc)
        ):
            return None
        provenance = None
    else:
        if (
            work.cancel_requested_at is not None
            or origin_action is None
            or origin_job is None
            or origin_action.id != work.origin_import_action_id
            or work.origin_import_job_id != origin_job.id
            or origin_action.import_job_id != work.origin_import_job_id
            or not _positive_int(work.origin_imported_story_arc_id)
            or not _positive_int(work.origin_imported_story_arc_entry_id)
            or origin_action.phase != _IMPORT_PLACEMENT_PHASE
            or origin_action.action_type != _IMPORT_PLACEMENT_ACTION_TYPE
            or origin_action.status is not ImportJobActionStatus.COMPLETED
            or origin_job.status is not ImportJobStatus.IMPORTING
            or origin_job.control_request is not ImportControlRequest.NONE
            or dict(origin_job.progress_snapshot or {}).get("phase") != _IMPORT_PLACEMENT_PHASE
            or not _import_managed_policy_configured(story_arc)
            or not _origin_payload_matches(
                origin_action,
                work,
                job_id=origin_job.id,
                membership_id=membership.id,
            )
        ):
            return None
        imported_story_arc_id = work.origin_imported_story_arc_id
        imported_story_arc_entry_id = work.origin_imported_story_arc_entry_id
        assert imported_story_arc_id is not None
        assert imported_story_arc_entry_id is not None
        staged_binding = await session.scalar(
            select(ImportedStoryArcEntry.id)
            .join(
                ImportedStoryArc,
                ImportedStoryArcEntry.imported_story_arc_id == ImportedStoryArc.id,
            )
            .where(
                ImportedStoryArc.id == imported_story_arc_id,
                ImportedStoryArc.import_job_id == origin_job.id,
                ImportedStoryArc.materialized_story_arc_id == story_arc.id,
                ImportedStoryArcEntry.id == imported_story_arc_entry_id,
                ImportedStoryArcEntry.materialized_membership_id == membership.id,
            )
        )
        if staged_binding is None:
            return None
        provenance = StoryArcPlacementImportProvenance(
            import_job_id=origin_job.id,
            import_action_id=origin_action.id,
        )
    return _WorkContext(
        work_id=work.id,
        membership_id=membership.id,
        story_arc_id=story_arc.id,
        library_file_id=library_file.id,
        attempt_count=work.attempt_count,
        import_provenance=provenance,
    )


async def _finish_work(
    session: AsyncSession,
    context: _WorkContext,
    claim_token: str,
    *,
    outcome: str,
) -> bool:
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(StoryArcSyncWork)
            .where(
                StoryArcSyncWork.id == context.work_id,
                StoryArcSyncWork.claim_token == claim_token,
                StoryArcSyncWork.state == StoryArcSyncWorkState.RUNNING,
            )
            .values(
                state=StoryArcSyncWorkState.COMPLETED,
                claim_token=None,
                claimed_at=None,
                next_attempt_at=None,
                last_error_code=None,
                last_error_category=None,
                last_error_detail=None,
                last_result={"schema_version": 1, "outcome": outcome},
            )
        ),
    )
    await session.commit()
    return result.rowcount == 1


async def _cancel_work(
    session: AsyncSession,
    work_id: int,
    claim_token: str,
    *,
    code: str,
) -> bool:
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(StoryArcSyncWork)
            .where(
                StoryArcSyncWork.id == work_id,
                StoryArcSyncWork.claim_token == claim_token,
                StoryArcSyncWork.state == StoryArcSyncWorkState.RUNNING,
            )
            .values(
                state=StoryArcSyncWorkState.CANCELLED,
                claim_token=None,
                claimed_at=None,
                next_attempt_at=None,
                last_error_code=code,
                last_error_category="superseded",
                last_error_detail="Automatic story-arc synchronization is no longer eligible.",
            )
        ),
    )
    await session.commit()
    return result.rowcount == 1


def _is_retryable(exc: StoryArcPlacementIntegrationError) -> bool:
    return exc.code in _RETRYABLE_ERROR_CODES or exc.category in {"operation", "cancelled"}


async def _fail_or_retry_work(
    session: AsyncSession,
    context: _WorkContext,
    claim_token: str,
    *,
    now: datetime,
    code: str,
    category: str,
    detail: str,
    retryable: bool,
) -> StoryArcSyncWorkState | None:
    should_retry = retryable and context.attempt_count < _MAX_ATTEMPTS
    state = StoryArcSyncWorkState.RETRY_WAIT if should_retry else StoryArcSyncWorkState.FAILED
    next_attempt_at = (
        now + _RETRY_DELAYS[min(context.attempt_count - 1, len(_RETRY_DELAYS) - 1)]
        if should_retry
        else None
    )
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(StoryArcSyncWork)
            .where(
                StoryArcSyncWork.id == context.work_id,
                StoryArcSyncWork.claim_token == claim_token,
                StoryArcSyncWork.state == StoryArcSyncWorkState.RUNNING,
            )
            .values(
                state=state,
                claim_token=None,
                claimed_at=None,
                next_attempt_at=next_attempt_at,
                last_error_code=code,
                last_error_category=category,
                last_error_detail=detail,
            )
        ),
    )
    await session.commit()
    return state if result.rowcount == 1 else None


async def retry_import_story_arc_sync_work(
    session: AsyncSession,
    job_id: int,
) -> tuple[ImportJob, int]:
    """Safely reopen exact terminal placement work for one stalled import.

    The caller owns the transaction.  Every import-origin row is validated
    before one conditional UPDATE resets only FAILED/CANCELLED rows; canonical
    import execution and any pending or completed placement work are untouched.
    """
    from pullbox.services.import_story_arc_placement_completion import (
        inspect_import_story_arc_placement_origin,
    )

    job = await session.scalar(select(ImportJob).where(ImportJob.id == job_id).with_for_update())
    if job is None:
        raise NotFoundError("ImportJob", job_id)

    snapshot = dict(job.progress_snapshot or {})
    if (
        job.status is not ImportJobStatus.STALLED
        or job.import_started_at is None
        or snapshot.get("phase") != _IMPORT_PLACEMENT_PHASE
    ):
        raise ValidationError("Import job is not stalled on Story Arc placements.")
    if job.control_request is not ImportControlRequest.NONE:
        raise ValidationError("Import job has an active control request.")

    counts = await inspect_import_story_arc_placement_origin(session, job_id)
    expected_total = snapshot.get("story_arc_placements_total")
    if (
        not isinstance(expected_total, int)
        or isinstance(expected_total, bool)
        or expected_total <= 0
        or counts.total != expected_total
    ):
        raise ValidationError("Import Story Arc placement origin evidence is incomplete.")

    retrying_count = counts.failed + counts.cancelled
    if retrying_count == 0:
        raise ValidationError("No failed or cancelled Story Arc placement work to retry.")

    terminal_states = (
        StoryArcSyncWorkState.FAILED,
        StoryArcSyncWorkState.CANCELLED,
    )
    terminal_rows = (
        await session.execute(
            select(StoryArcSyncWork, IssueStoryArc, StoryArc, LibraryFile)
            .join(
                IssueStoryArc,
                StoryArcSyncWork.issue_story_arc_id == IssueStoryArc.id,
            )
            .join(StoryArc, IssueStoryArc.story_arc_id == StoryArc.id)
            .join(LibraryFile, StoryArcSyncWork.library_file_id == LibraryFile.id)
            .where(
                StoryArcSyncWork.origin_import_job_id == job_id,
                StoryArcSyncWork.state.in_(terminal_states),
            )
            .order_by(StoryArcSyncWork.id.asc())
        )
    ).all()
    if len(terminal_rows) != retrying_count:
        raise ValidationError("Import Story Arc placement origin evidence is incomplete.")

    work_ids: list[int] = []
    for work, membership, story_arc, library_file in terminal_rows:
        desired_generation, source_signature_hash = _desired_generation(
            library_file,
            membership,
            story_arc,
        )
        if (
            not work.claimable
            or work.claim_token is not None
            or work.claimed_at is not None
            or work.next_attempt_at is not None
            or work.cancel_requested_at is not None
            or not _import_managed_policy_configured(story_arc)
            or work.desired_generation != desired_generation
            or work.source_signature_hash != source_signature_hash
            or work.story_arc_revision != story_arc.revision
            or work.membership_sequence != membership.sequence_number
            or work.policy_schema_version
            != (story_arc.policy_schema_version or STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION)
        ):
            raise ValidationError(
                "Import Story Arc placement work is no longer exact and cannot be retried."
            )
        work_ids.append(int(work.id))

    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(StoryArcSyncWork)
            .where(
                StoryArcSyncWork.id.in_(work_ids),
                StoryArcSyncWork.origin_import_job_id == job_id,
                StoryArcSyncWork.state.in_(terminal_states),
                StoryArcSyncWork.claimable.is_(True),
                StoryArcSyncWork.claim_token.is_(None),
                StoryArcSyncWork.claimed_at.is_(None),
                StoryArcSyncWork.cancel_requested_at.is_(None),
            )
            .values(
                state=StoryArcSyncWorkState.QUEUED,
                attempt_count=0,
                next_attempt_at=None,
                claim_token=None,
                claimed_at=None,
                cancel_requested_at=None,
                last_error_code=None,
                last_error_category=None,
                last_error_detail=None,
                last_result={},
            )
        ),
    )
    if result.rowcount != retrying_count:
        raise ValidationError("Story Arc placement work changed concurrently; retry refused.")

    updated_snapshot = dict(snapshot)
    updated_snapshot.update(
        {
            "status": ImportJobStatus.IMPORTING.value,
            "mode": "import",
            "phase": _IMPORT_PLACEMENT_PHASE,
            "progress": 99,
            "message": "Creating the approved story-arc copies and links...",
            "story_arc_placements_total": counts.total,
            "story_arc_placements_queued": counts.queued + retrying_count,
            "story_arc_placements_running": counts.running,
            "story_arc_placements_retry_wait": counts.retry_wait,
            "story_arc_placements_failed": 0,
            "story_arc_placements_completed": counts.completed,
            "story_arc_placements_cancelled": 0,
            "story_arc_placement_followup_pending": False,
        }
    )
    job.status = ImportJobStatus.IMPORTING
    job.error_message = None
    job.import_completed_at = None
    job.story_arc_placement_followup_pending = False
    job.progress_snapshot = updated_snapshot
    job.progress_revision = int(job.progress_revision or 0) + 1
    await session.flush()
    return job, retrying_count


async def _next_retry_at(
    session: AsyncSession,
    *,
    import_only: bool = False,
) -> datetime | None:
    origin_scope = (StoryArcSyncWork.origin_import_job_id.is_not(None),) if import_only else ()
    return await session.scalar(
        select(func.min(StoryArcSyncWork.next_attempt_at)).where(
            *origin_scope,
            _origin_is_not_startup_recovery_paused(),
            StoryArcSyncWork.claimable.is_(True),
            StoryArcSyncWork.state == StoryArcSyncWorkState.RETRY_WAIT,
        )
    )


async def _has_ready_work(
    session: AsyncSession,
    *,
    now: datetime,
    import_only: bool = False,
) -> bool:
    return bool(
        await _ready_work_ids(
            session,
            now=now,
            limit=1,
            import_only=import_only,
        )
    )


async def _origin_import_job_ids_for_work_ids(
    session: AsyncSession,
    work_ids: list[int],
) -> tuple[int, ...]:
    """Return only the import jobs touched by this bounded worker drain."""
    if not work_ids:
        return ()
    return tuple(
        int(job_id)
        for job_id in (
            await session.scalars(
                select(StoryArcSyncWork.origin_import_job_id)
                .where(
                    StoryArcSyncWork.id.in_(work_ids),
                    StoryArcSyncWork.origin_import_job_id.is_not(None),
                )
                .distinct()
                .order_by(StoryArcSyncWork.origin_import_job_id.asc())
            )
        ).all()
        if job_id is not None
    )


def _waiting_import_story_arc_finalizer_predicate() -> Any:
    """Build the shared eligibility fence for bounded placement finalization."""
    pending_origin_work = exists().where(
        StoryArcSyncWork.origin_import_job_id == ImportJob.id,
        StoryArcSyncWork.state.in_(_PENDING_IMPORT_WORK_STATES),
    )
    return or_(
        and_(
            ImportJob.status.in_(
                {
                    ImportJobStatus.IMPORTING,
                    ImportJobStatus.STALLED,
                }
            ),
            ImportJob.control_request == ImportControlRequest.NONE,
            ImportJob.progress_snapshot["phase"].as_string() == _IMPORT_PLACEMENT_PHASE,
            ~pending_origin_work,
        ),
        and_(
            ImportJob.status == ImportJobStatus.COMPLETED,
            ImportJob.story_arc_placement_followup_pending.is_(True),
        ),
    )


async def _finalize_waiting_import_story_arc_placements(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    candidate_job_ids: tuple[int, ...] | None = None,
) -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
    """Finalize terminal touched jobs, or perform one bounded recovery sweep."""
    normalized_candidates = tuple(
        dict.fromkeys(job_id for job_id in (candidate_job_ids or ()) if job_id > 0)
    )[:MAX_IMPORT_PLACEMENT_FINALIZE_BATCH_SIZE]

    async with session_factory() as list_session:
        eligible = _waiting_import_story_arc_finalizer_predicate()
        touched_limit = MAX_IMPORT_PLACEMENT_FINALIZE_BATCH_SIZE
        if normalized_candidates and touched_limit > 1:
            touched_limit -= 1
        touched_rows: tuple[tuple[int, ImportJobStatus], ...] = ()
        if normalized_candidates and touched_limit:
            touched_rows = tuple(
                (int(job_id), status)
                for job_id, status in (
                    await list_session.execute(
                        select(ImportJob.id, ImportJob.status)
                        .where(
                            ImportJob.id.in_(normalized_candidates),
                            eligible,
                        )
                        .order_by(ImportJob.id.asc())
                        .limit(touched_limit)
                    )
                ).all()
            )
        recovery_limit = MAX_IMPORT_PLACEMENT_FINALIZE_BATCH_SIZE - len(touched_rows)
        recovery_statement = select(ImportJob.id, ImportJob.status).where(eligible)
        if normalized_candidates:
            recovery_statement = recovery_statement.where(
                ImportJob.id.not_in(normalized_candidates)
            )
        recovery_rows = tuple(
            (int(job_id), status)
            for job_id, status in (
                await list_session.execute(
                    recovery_statement.order_by(ImportJob.id.asc()).limit(recovery_limit)
                )
            ).all()
        )
        candidates = (*touched_rows, *recovery_rows)
        await list_session.rollback()

    evaluated: list[int] = []
    completed: list[int] = []
    stalled: list[int] = []
    for job_id, status in candidates:
        if status is ImportJobStatus.COMPLETED:
            evaluated.append(job_id)
            completed.append(job_id)
            continue
        async with session_factory() as finalizer_session:
            try:
                outcome = await finalize_import_story_arc_placements(
                    finalizer_session,
                    job_id,
                )
                await finalizer_session.commit()
            except (NotFoundError, ValidationError):
                await finalizer_session.rollback()
                continue
        evaluated.append(job_id)
        if outcome.state is ImportStoryArcPlacementCompletionState.COMPLETED:
            completed.append(job_id)
        elif outcome.state is ImportStoryArcPlacementCompletionState.STALLED:
            stalled.append(job_id)
    return tuple(evaluated), tuple(completed), tuple(stalled)


async def _has_remaining_import_story_arc_finalizers(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    exclude_job_ids: tuple[int, ...],
) -> bool:
    """Return whether bounded finalization left another eligible job behind."""
    async with session_factory() as session:
        eligible = _waiting_import_story_arc_finalizer_predicate()
        statement = select(ImportJob.id).where(eligible)
        if exclude_job_ids:
            statement = statement.where(ImportJob.id.not_in(exclude_job_ids))
        remaining_job_id = await session.scalar(statement.order_by(ImportJob.id.asc()).limit(1))
        await session.rollback()
    return remaining_job_id is not None


async def _ready_import_story_arc_rollbacks(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, ...]:
    """Return bounded deferred rollbacks whose in-flight work is now fenced."""
    async with session_factory() as session:
        ready = tuple(
            int(job_id)
            for job_id in (
                await session.scalars(
                    select(ImportJob.id)
                    .outerjoin(
                        StoryArcSyncWork,
                        StoryArcSyncWork.id == ImportJob.story_arc_rollback_waiting_work_id,
                    )
                    .where(
                        ImportJob.status == ImportJobStatus.ROLLING_BACK,
                        ImportJob.story_arc_rollback_waiting_work_id.is_not(None),
                        or_(
                            StoryArcSyncWork.id.is_(None),
                            StoryArcSyncWork.state != StoryArcSyncWorkState.RUNNING,
                        ),
                    )
                    .order_by(ImportJob.id.asc())
                    .limit(MAX_IMPORT_PLACEMENT_FINALIZE_BATCH_SIZE)
                )
            ).all()
        )
        await session.rollback()
    return ready


async def process_story_arc_sync_work(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    sync_service: StoryArcPlacementSyncService | Any | None = None,
    batch_size: int = DEFAULT_STORY_ARC_SYNC_BATCH_SIZE,
    discover: bool = True,
    import_only: bool = False,
    now_fn: Callable[[], datetime] | None = None,
    heartbeat_interval_seconds: float = _CLAIM_HEARTBEAT_INTERVAL_SECONDS,
    heartbeat_now_fn: Callable[[], datetime] | None = None,
    origin_cancellation_poll_seconds: float = _ORIGIN_CANCELLATION_POLL_SECONDS,
) -> StoryArcSyncDrainResult:
    """Discover and process one bounded batch with a fresh session per phase.

    Import-only drains are safe to run under the global import scheduler fence:
    they skip discovery and ignore every ordinary synchronization lane.
    """
    if isinstance(batch_size, bool) or not 1 <= batch_size <= MAX_STORY_ARC_SYNC_BATCH_SIZE:
        raise ValueError(
            f"Story-arc sync batch size must be from 1 to {MAX_STORY_ARC_SYNC_BATCH_SIZE}"
        )
    if (
        isinstance(heartbeat_interval_seconds, bool)
        or heartbeat_interval_seconds <= 0
        or not math.isfinite(heartbeat_interval_seconds)
    ):
        raise ValueError("Story-arc sync heartbeat interval must be a positive finite number")
    if (
        isinstance(origin_cancellation_poll_seconds, bool)
        or origin_cancellation_poll_seconds <= 0
        or not math.isfinite(origin_cancellation_poll_seconds)
    ):
        raise ValueError(
            "Story-arc import cancellation poll interval must be a positive finite number"
        )
    factory = session_factory or get_session_factory()
    service = sync_service or StoryArcPlacementSyncService()
    effective_now_fn = now_fn or (lambda: datetime.now(UTC))
    effective_heartbeat_now_fn = heartbeat_now_fn or effective_now_fn
    discovered = 0
    if discover and not import_only:
        async with factory() as discovery_session:
            discovered = await discover_story_arc_sync_work(discovery_session)
            await discovery_session.commit()

    async with factory() as list_session:
        work_ids = await _ready_work_ids(
            list_session,
            now=effective_now_fn(),
            limit=batch_size,
            import_only=import_only,
        )
        await list_session.rollback()

    claimed = completed = failed = retrying = cancelled = lost_claims = 0
    for work_id in work_ids:
        item_now = effective_now_fn()
        async with factory() as claim_session:
            claim_token = await claim_story_arc_sync_work(
                claim_session,
                work_id,
                now=item_now,
                import_only=import_only,
            )
        if claim_token is None:
            continue
        claimed += 1

        async with factory() as context_session:
            context = await _load_claimed_context(context_session, work_id, claim_token)
            await context_session.rollback()
        if context is None:
            async with factory() as result_session:
                cancelled_claim = await _cancel_work(
                    result_session,
                    work_id,
                    claim_token,
                    code="sync_work_superseded",
                )
            if cancelled_claim:
                cancelled += 1
            else:
                lost_claims += 1
            continue

        heartbeat_stop = asyncio.Event()
        origin_cancellation_stop = asyncio.Event()
        origin_cancellation_requested = asyncio.Event()
        heartbeat = asyncio.create_task(
            _maintain_claim_lease(
                factory,
                work_id,
                claim_token,
                heartbeat_stop,
                interval_seconds=heartbeat_interval_seconds,
                now_fn=effective_heartbeat_now_fn,
            )
        )
        origin_cancellation_monitor = (
            asyncio.create_task(
                _monitor_origin_cancellation(
                    factory,
                    work_id,
                    claim_token,
                    origin_cancellation_stop,
                    origin_cancellation_requested,
                    interval_seconds=origin_cancellation_poll_seconds,
                )
            )
            if context.import_provenance is not None
            else None
        )
        try:
            try:
                async with factory() as sync_session:
                    if context.import_provenance is None:
                        result = await service.sync_membership(
                            sync_session,
                            context.story_arc_id,
                            context.membership_id,
                        )
                    else:
                        result = await service.sync_membership(
                            sync_session,
                            context.story_arc_id,
                            context.membership_id,
                            import_provenance=context.import_provenance,
                            cancellation_requested=origin_cancellation_requested.is_set,
                        )
            finally:
                heartbeat_stop.set()
                origin_cancellation_stop.set()
                await heartbeat
                if origin_cancellation_monitor is not None:
                    await origin_cancellation_monitor
            async with factory() as result_session:
                finished_claim = await _finish_work(
                    result_session,
                    context,
                    claim_token,
                    outcome=result.outcome,
                )
            if finished_claim:
                completed += 1
            else:
                lost_claims += 1
        except asyncio.CancelledError:
            heartbeat_stop.set()
            if not heartbeat.done():
                await heartbeat
            async with factory() as result_session:
                cancelled_state = await _fail_or_retry_work(
                    result_session,
                    context,
                    claim_token,
                    now=effective_now_fn(),
                    code="sync_worker_cancelled",
                    category="cancelled",
                    detail="Automatic story-arc synchronization was interrupted.",
                    retryable=True,
                )
            if cancelled_state is None:
                logger.warning(
                    "story_arc_sync_claim_lost_during_cancellation",
                    work_id=context.work_id,
                    issue_story_arc_id=context.membership_id,
                )
            raise
        except StoryArcPlacementIntegrationError as exc:
            if context.import_provenance is not None and (
                origin_cancellation_requested.is_set() or exc.category == "cancelled"
            ):
                async with factory() as result_session:
                    cancelled_claim = await _cancel_work(
                        result_session,
                        context.work_id,
                        claim_token,
                        code=exc.code,
                    )
                if cancelled_claim:
                    cancelled += 1
                else:
                    lost_claims += 1
                continue
            async with factory() as result_session:
                state = await _fail_or_retry_work(
                    result_session,
                    context,
                    claim_token,
                    now=effective_now_fn(),
                    code=exc.code,
                    category=exc.category,
                    detail=str(exc),
                    retryable=_is_retryable(exc),
                )
            if state is None:
                lost_claims += 1
            elif state is StoryArcSyncWorkState.RETRY_WAIT:
                retrying += 1
            else:
                failed += 1
            logger.warning(
                "story_arc_sync_item_failed",
                work_id=context.work_id,
                issue_story_arc_id=context.membership_id,
                error_code=exc.code,
                error_category=exc.category,
                claim_lost=state is None,
                retrying=state is StoryArcSyncWorkState.RETRY_WAIT,
            )
        except Exception:
            async with factory() as result_session:
                state = await _fail_or_retry_work(
                    result_session,
                    context,
                    claim_token,
                    now=effective_now_fn(),
                    code="story_arc_sync_unexpected_failure",
                    category="operation",
                    detail="Automatic story-arc synchronization failed unexpectedly.",
                    retryable=True,
                )
            if state is None:
                lost_claims += 1
            elif state is StoryArcSyncWorkState.RETRY_WAIT:
                retrying += 1
            else:
                failed += 1
            logger.exception(
                "story_arc_sync_item_failed_unexpectedly",
                work_id=context.work_id,
                issue_story_arc_id=context.membership_id,
                claim_lost=state is None,
                retrying=state is StoryArcSyncWorkState.RETRY_WAIT,
            )

    async with factory() as summary_session:
        touched_import_job_ids = await _origin_import_job_ids_for_work_ids(
            summary_session,
            work_ids,
        )
        has_more = await _has_ready_work(
            summary_session,
            now=effective_now_fn(),
            import_only=import_only,
        )
        next_retry_at = await _next_retry_at(
            summary_session,
            import_only=import_only,
        )
        await summary_session.rollback()
    (
        import_jobs_evaluated,
        import_jobs_completed,
        import_jobs_stalled,
    ) = await _finalize_waiting_import_story_arc_placements(
        factory,
        candidate_job_ids=(touched_import_job_ids if work_ids else None),
    )
    finalizer_has_more = await _has_remaining_import_story_arc_finalizers(
        factory,
        exclude_job_ids=import_jobs_evaluated,
    )
    has_more = has_more or finalizer_has_more
    import_jobs_rollback_ready = await _ready_import_story_arc_rollbacks(factory)
    return StoryArcSyncDrainResult(
        discovered=discovered,
        claimed=claimed,
        completed=completed,
        failed=failed,
        retrying=retrying,
        cancelled=cancelled,
        lost_claims=lost_claims,
        has_more=has_more,
        next_retry_at=next_retry_at,
        import_jobs_evaluated=import_jobs_evaluated,
        import_jobs_completed=import_jobs_completed,
        import_jobs_stalled=import_jobs_stalled,
        import_jobs_rollback_ready=import_jobs_rollback_ready,
    )


def request_story_arc_sync_now() -> None:
    """Best-effort latency nudge; the durable scheduled sweep remains authoritative."""
    from pullbox.core.scheduler import get_scheduler

    try:
        status = get_scheduler().run_task_now(STORY_ARC_SYNC_TASK_ID)
        if status == "queued":
            logger.debug("story_arc_sync_triggered_after_registration")
    except Exception:
        logger.warning("story_arc_sync_trigger_failed", exc_info=True)
