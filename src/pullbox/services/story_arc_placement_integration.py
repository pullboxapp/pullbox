"""Short-transaction database boundary for story-arc placement operations.

The filesystem service is intentionally synchronous.  This adapter freezes a
complete per-arc policy, commits a prepared placement row, performs filesystem
work in a worker thread with no database transaction open, and then reconciles
the durable row.  Canonical issues and library files are read-only throughout.
"""

from __future__ import annotations

import asyncio
import enum
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import and_, delete, func, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError

from pullbox.core.story_arc_naming import (
    DEFAULT_STORY_ARC_FILE_TEMPLATE,
    DEFAULT_STORY_ARC_FOLDER_TEMPLATE,
    StoryArcNamingValues,
    render_story_arc_relative_path,
    validate_story_arc_file_template,
    validate_story_arc_folder_template,
)
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportJob,
    ImportJobAction,
    ImportJobActionStatus,
    ImportJobStatus,
)
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile, LibraryRoot
from pullbox.models.publisher import Publisher
from pullbox.models.series import Series
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
    StoryArcSymlinkStyle,
)
from pullbox.services.story_arc_placement_preview import (
    StoryArcCollisionKind,
    StoryArcPlacementPreviewState,
)
from pullbox.services.story_arc_placement_service import (
    ManagedStoryArcPlacementEvidence,
    PreparedManagedStoryArcPlacementEvidence,
    StoryArcPlacementCancellationError,
    StoryArcPlacementCollisionError,
    StoryArcPlacementError,
    StoryArcPlacementInspection,
    StoryArcPlacementInspectionEvidence,
    StoryArcPlacementInspectionState,
    StoryArcPlacementJournalEvent,
    StoryArcPlacementOwnershipError,
    StoryArcPlacementPlan,
    StoryArcPlacementPreparation,
    StoryArcPlacementRemovalResult,
    StoryArcPlacementResult,
    StoryArcPlacementSafetyError,
    execute_story_arc_placement,
    inspect_story_arc_placement,
    prepare_story_arc_placement,
    recover_prepared_story_arc_placement,
    remove_managed_story_arc_placement,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION = 1
MAX_STORY_ARC_PLACEMENT_PAGE_SIZE = 200
_PLACEMENT_HEARTBEAT_SECONDS = 10.0
_PLACEMENT_OPERATION_LEASE = timedelta(minutes=5)
_POLICY_SNAPSHOT_KEYS = frozenset(
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
_IMPORT_PLACEMENT_PHASE = "story_arc_placements"
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


class StoryArcPlacementPolicyMode(enum.StrEnum):
    """User-facing policy mode, including a truly logical-only arc."""

    LOGICAL = "logical"
    REFERENCE_ONLY = "reference_only"
    COPY = "copy"
    HARDLINK = "hardlink"
    SYMLINK = "symlink"


class StoryArcPlacementIntegrationError(RuntimeError):
    """Safe categorized failure exposed by the database/API adapter."""

    def __init__(self, code: str, message: str, *, category: str = "validation") -> None:
        self.code = code
        self.category = category
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class StoryArcPlacementPolicyInput:
    """Complete candidate policy supplied by an API or import adapter."""

    mode: StoryArcPlacementPolicyMode | str
    target_library_root_id: int | None
    destination_root: str | None
    folder_template: str = DEFAULT_STORY_ARC_FOLDER_TEMPLATE
    file_template: str = DEFAULT_STORY_ARC_FILE_TEMPLATE
    symlink_style: StoryArcSymlinkStyle | str | None = None
    synchronize: bool = False


@dataclass(frozen=True, slots=True)
class StoryArcPlacementPolicy:
    """Validated immutable effective policy for one story arc."""

    configured: bool
    revision: int
    mode: StoryArcPlacementPolicyMode
    target_library_root_id: int | None
    destination_root: str | None
    folder_template: str
    file_template: str
    symlink_style: StoryArcSymlinkStyle | None
    synchronize: bool

    @property
    def snapshot(self) -> dict[str, object]:
        """Return the complete versioned JSON representation persisted on the arc."""
        return {
            "schema_version": STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION,
            "mode": self.mode.value,
            "target_library_root_id": self.target_library_root_id,
            "destination_root": self.destination_root,
            "folder_template": self.folder_template,
            "file_template": self.file_template,
            "symlink_style": self.symlink_style.value if self.symlink_style else None,
            "synchronize": self.synchronize,
        }


@dataclass(frozen=True, slots=True)
class StoryArcPlacementPreviewItem:
    """One bounded membership preview without ORM or filesystem ownership claims."""

    membership_id: int
    sequence_number: int
    issue_id: int | None
    issue_number_text: str
    mode: str
    state: str
    target_path: str | None
    collision: str
    reason: str | None
    required_bytes: int
    proposed_ownership: str
    overwrite_allowed: bool
    classification: str
    placement_id: int | None
    current_ownership: str | None
    inspection_code: str | None


@dataclass(frozen=True, slots=True)
class StoryArcPlacementPreviewPage:
    """Bounded preview page with deterministic pagination metadata."""

    items: tuple[StoryArcPlacementPreviewItem, ...]
    total: int
    limit: int
    offset: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class StoryArcPlacementView:
    """Detached API-safe view of one durable placement row."""

    id: int
    issue_story_arc_id: int
    library_file_id: int | None
    library_root_id: int | None
    placement_path: str
    mode: StoryArcPlacementMode
    ownership: StoryArcPlacementOwnership
    symlink_style: StoryArcSymlinkStyle | None
    rendered_reading_order: int | None
    policy_schema_version: int | None
    source_fingerprint: dict[str, object]
    target_fingerprint: dict[str, object]
    state: StoryArcPlacementState
    last_result: dict[str, object]
    last_checked_at: datetime | None


@dataclass(frozen=True, slots=True)
class StoryArcPlacementPage:
    """Bounded durable placement page."""

    items: tuple[StoryArcPlacementView, ...]
    total: int
    limit: int
    offset: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class StoryArcPlacementSyncResult:
    """Truthful result from one logical or filesystem synchronization."""

    membership_id: int
    outcome: str
    placement: StoryArcPlacementView | None


@dataclass(frozen=True, slots=True)
class StoryArcPlacementRemovalView:
    """Truthful ownership-aware result of explicitly removing placement evidence."""

    placement_id: int
    ownership: StoryArcPlacementOwnership
    artifact_removed: bool
    canonical_preserved: bool = True
    referenced_artifact_preserved: bool = False
    automatic_sync_disabled: bool = True


@dataclass(frozen=True, slots=True)
class StoryArcPlacementImportProvenance:
    """Immutable import job/action ownership stamped only on a new managed row."""

    import_job_id: int
    import_action_id: int


@dataclass(frozen=True, slots=True)
class _PlacementContext:
    membership_id: int
    story_arc_id: int
    sequence_number: int
    issue_id: int | None
    issue_number_text: str
    story_arc_name: str
    series_name: str
    publisher_name: str | None
    issue_title: str | None
    year: int | None
    series_start_year: int | None
    series_end_year: int | None
    library_file_id: int | None
    canonical_path: str | None
    extension: str

    def naming_values(self) -> StoryArcNamingValues:
        return StoryArcNamingValues(
            story_arc=self.story_arc_name,
            reading_order=self.sequence_number,
            series=self.series_name,
            publisher=self.publisher_name,
            issue_number=self.issue_number_text,
            issue_title=self.issue_title,
            year=self.year,
            start_year=self.series_start_year,
            end_year=self.series_end_year,
            extension=self.extension,
        )


@dataclass(frozen=True, slots=True)
class _PreviewPlacementEvidence:
    """Detached evidence for the one placement matching a rendered page target."""

    id: int
    issue_story_arc_id: int
    placement_path: str
    mode: StoryArcPlacementMode
    ownership: StoryArcPlacementOwnership
    symlink_style: StoryArcSymlinkStyle | None
    source_fingerprint: dict[str, object]
    target_fingerprint: dict[str, object]
    creating_action_id: int | None


@dataclass(slots=True)
class _MembershipSyncLock:
    """One reference-counted process-local membership operation lock."""

    lock: asyncio.Lock
    users: int = 0


_sync_locks: dict[tuple[int, int], _MembershipSyncLock] = {}


@asynccontextmanager
async def _membership_sync_lock(lock_key: tuple[int, int]) -> AsyncIterator[None]:
    """Serialize sync/removal without dropping a lock that still has waiters."""
    entry = _sync_locks.setdefault(lock_key, _MembershipSyncLock(lock=asyncio.Lock()))
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        if entry.users == 0 and _sync_locks.get(lock_key) is entry:
            del _sync_locks[lock_key]


class StoryArcPlacementSyncService:
    """Freeze policies and synchronize one membership with short transactions."""

    async def get_policy(
        self,
        session: AsyncSession,
        story_arc_id: int,
    ) -> StoryArcPlacementPolicy:
        arc = await session.get(StoryArc, story_arc_id)
        if arc is None:
            raise _not_found("story_arc_not_found", "Story arc was not found")
        return _policy_from_arc(arc)

    async def validate_policy(
        self,
        session: AsyncSession,
        story_arc_id: int,
        proposal: StoryArcPlacementPolicyInput,
    ) -> StoryArcPlacementPolicy:
        arc = await session.get(StoryArc, story_arc_id)
        if arc is None:
            raise _not_found("story_arc_not_found", "Story arc was not found")
        if arc.lifecycle is StoryArcLifecycle.ARCHIVED:
            raise StoryArcPlacementIntegrationError(
                "story_arc_archived",
                "Archived story arcs cannot change placement policy",
            )
        return await validate_story_arc_placement_policy_input(
            session,
            proposal,
            revision=arc.revision,
        )

    async def update_policy(
        self,
        session: AsyncSession,
        story_arc_id: int,
        *,
        expected_revision: int,
        proposal: StoryArcPlacementPolicyInput,
    ) -> StoryArcPlacementPolicy:
        if isinstance(expected_revision, bool) or expected_revision < 1:
            raise StoryArcPlacementIntegrationError(
                "invalid_revision",
                "Expected story-arc revision must be a positive integer",
            )
        policy = await self.validate_policy(session, story_arc_id, proposal)
        current_arc = await session.get(StoryArc, story_arc_id)
        if current_arc is None:  # pragma: no cover - validate_policy loaded it
            raise _not_found("story_arc_not_found", "Story arc was not found")
        if current_arc.revision != expected_revision:
            raise StoryArcPlacementIntegrationError(
                "revision_conflict",
                (
                    "Story arc revision changed: expected "
                    f"{expected_revision}, current {current_arc.revision}"
                ),
                category="conflict",
            )
        current_policy = _policy_from_arc(current_arc)
        if (
            current_policy.configured
            and _placement_policy_shape(current_policy) != _placement_policy_shape(policy)
            and await _arc_has_managed_placements(session, story_arc_id)
        ):
            raise StoryArcPlacementIntegrationError(
                "managed_policy_change_requires_migration",
                (
                    "Move or remove current managed placements before changing their "
                    "destination policy"
                ),
                category="conflict",
            )
        next_revision = expected_revision + 1
        result = await session.execute(
            sa_update(StoryArc)
            .where(
                StoryArc.id == story_arc_id,
                StoryArc.revision == expected_revision,
                StoryArc.lifecycle == StoryArcLifecycle.ACTIVE,
            )
            .values(
                target_library_root_id=policy.target_library_root_id,
                policy_schema_version=STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION,
                policy_snapshot=policy.snapshot,
                sync_enabled=policy.synchronize,
                revision=next_revision,
            )
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            current = await session.get(StoryArc, story_arc_id)
            if current is None:
                raise _not_found("story_arc_not_found", "Story arc was not found")
            if current.lifecycle is StoryArcLifecycle.ARCHIVED:
                raise StoryArcPlacementIntegrationError(
                    "story_arc_archived",
                    "Archived story arcs cannot change placement policy",
                )
            raise StoryArcPlacementIntegrationError(
                "revision_conflict",
                (
                    "Story arc revision changed: expected "
                    f"{expected_revision}, current {current.revision}"
                ),
                category="conflict",
            )
        await session.execute(
            sa_update(IssueStoryArc)
            .where(IssueStoryArc.story_arc_id == story_arc_id)
            .values(
                sync_eligible=(
                    and_(
                        IssueStoryArc.issue_id.is_not(None),
                        IssueStoryArc.resolution_state == StoryArcResolutionState.RESOLVED,
                    )
                    if policy.synchronize
                    else False
                )
            )
        )
        await session.commit()
        return StoryArcPlacementPolicy(
            configured=True,
            revision=next_revision,
            mode=policy.mode,
            target_library_root_id=policy.target_library_root_id,
            destination_root=policy.destination_root,
            folder_template=policy.folder_template,
            file_template=policy.file_template,
            symlink_style=policy.symlink_style,
            synchronize=policy.synchronize,
        )

    async def preview_arc(
        self,
        session: AsyncSession,
        story_arc_id: int,
        *,
        limit: int,
        offset: int,
        proposal: StoryArcPlacementPolicyInput | None = None,
    ) -> StoryArcPlacementPreviewPage:
        limit, offset = _bounded_page(limit, offset)
        policy = (
            await self.validate_policy(session, story_arc_id, proposal)
            if proposal is not None
            else await self.get_policy(session, story_arc_id)
        )
        total, contexts = await _load_context_page(
            session,
            story_arc_id,
            limit=limit,
            offset=offset,
        )
        target_paths = _rendered_target_paths(contexts, policy)
        evidence_by_path = await _load_matching_preview_evidence(session, target_paths)
        # Close the read transaction before filesystem inspection in the worker.
        await session.rollback()
        items = await asyncio.to_thread(
            _preview_contexts,
            contexts,
            policy,
            evidence_by_path,
        )
        return StoryArcPlacementPreviewPage(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=(offset + limit) < total,
        )

    async def list_placements(
        self,
        session: AsyncSession,
        story_arc_id: int,
        *,
        limit: int,
        offset: int,
    ) -> StoryArcPlacementPage:
        limit, offset = _bounded_page(limit, offset)
        if await session.get(StoryArc, story_arc_id) is None:
            raise _not_found("story_arc_not_found", "Story arc was not found")
        arc_filter = IssueStoryArc.story_arc_id == story_arc_id
        total = int(
            await session.scalar(
                select(func.count(StoryArcPlacement.id))
                .join(
                    IssueStoryArc,
                    StoryArcPlacement.issue_story_arc_id == IssueStoryArc.id,
                )
                .where(arc_filter)
            )
            or 0
        )
        rows = list(
            (
                await session.scalars(
                    select(StoryArcPlacement)
                    .join(
                        IssueStoryArc,
                        StoryArcPlacement.issue_story_arc_id == IssueStoryArc.id,
                    )
                    .where(arc_filter)
                    .order_by(
                        IssueStoryArc.sequence_number.asc(),
                        StoryArcPlacement.id.asc(),
                    )
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        )
        return StoryArcPlacementPage(
            items=tuple(_placement_view(row) for row in rows),
            total=total,
            limit=limit,
            offset=offset,
            has_more=(offset + limit) < total,
        )

    async def sync_membership(
        self,
        session: AsyncSession,
        story_arc_id: int,
        membership_id: int,
        *,
        adopt_identical_existing: bool = False,
        cancellation_requested: Callable[[], bool] | None = None,
        import_provenance: StoryArcPlacementImportProvenance | None = None,
    ) -> StoryArcPlacementSyncResult:
        lock_key = (story_arc_id, membership_id)
        async with _membership_sync_lock(lock_key):
            return await self._sync_membership_locked(
                session,
                story_arc_id,
                membership_id,
                adopt_identical_existing=adopt_identical_existing,
                cancellation_requested=cancellation_requested,
                import_provenance=import_provenance,
            )

    async def retry_placement(
        self,
        session: AsyncSession,
        story_arc_id: int,
        placement_id: int,
        *,
        adopt_identical_existing: bool = False,
    ) -> StoryArcPlacementSyncResult:
        placement = await _require_placement(session, story_arc_id, placement_id)
        return await self.sync_membership(
            session,
            story_arc_id,
            placement.issue_story_arc_id,
            adopt_identical_existing=adopt_identical_existing,
        )

    async def repair_placement(
        self,
        session: AsyncSession,
        story_arc_id: int,
        placement_id: int,
    ) -> StoryArcPlacementSyncResult:
        placement = await _require_placement(session, story_arc_id, placement_id)
        if placement.ownership is not StoryArcPlacementOwnership.MANAGED:
            raise StoryArcPlacementIntegrationError(
                "referenced_placement_immutable",
                "Referenced story-arc placements cannot be repaired or changed",
                category="ownership",
            )
        return await self.sync_membership(
            session,
            story_arc_id,
            placement.issue_story_arc_id,
        )

    async def remove_placement(
        self,
        session: AsyncSession,
        story_arc_id: int,
        placement_id: int,
        *,
        confirm_managed_artifact_removal: bool = False,
        abandoned_published_operation_token: str | None = None,
    ) -> StoryArcPlacementRemovalView:
        """Remove only owned placement evidence, never a canonical or referenced file."""
        placement = await _require_placement(session, story_arc_id, placement_id)
        ownership = placement.ownership
        membership_id = placement.issue_story_arc_id
        if ownership is StoryArcPlacementOwnership.REFERENCED:
            # Close the initial read transaction before waiting for an active
            # synchronization.  The durable operation token below remains the
            # cross-process fence; this lock provides deterministic local UX.
            await session.commit()
            async with _membership_sync_lock((story_arc_id, membership_id)):
                current = await _require_placement(session, story_arc_id, placement_id)
                if current.ownership is not StoryArcPlacementOwnership.REFERENCED:
                    raise StoryArcPlacementIntegrationError(
                        "placement_ownership_changed",
                        "Story-arc placement ownership changed before it could be forgotten",
                        category="ownership",
                    )
                reference_token_filter: ColumnElement[bool] = StoryArcPlacement.operation_token.is_(
                    None
                )
                if abandoned_published_operation_token is not None:
                    _require_published_operation_token(
                        current,
                        abandoned_published_operation_token,
                    )
                    reference_token_filter = (
                        StoryArcPlacement.operation_token == abandoned_published_operation_token
                    )
                reference_removal_token = uuid4().hex
                previous_result = dict(current.last_result or {})
                reserve = await session.execute(
                    sa_update(StoryArcPlacement)
                    .where(
                        StoryArcPlacement.id == placement_id,
                        StoryArcPlacement.ownership == StoryArcPlacementOwnership.REFERENCED,
                        reference_token_filter,
                    )
                    .values(
                        operation_token=reference_removal_token,
                        last_result={
                            **previous_result,
                            "schema_version": 1,
                            "status": "remove_prepared",
                            "operation_token": reference_removal_token,
                        },
                    )
                )
                if reserve.rowcount != 1:  # type: ignore[attr-defined]
                    await session.rollback()
                    raise StoryArcPlacementIntegrationError(
                        "placement_operation_in_progress",
                        "Referenced placement evidence is being updated by another operation",
                        category="conflict",
                    )
                result = await session.execute(
                    delete(StoryArcPlacement).where(
                        StoryArcPlacement.id == placement_id,
                        StoryArcPlacement.ownership == StoryArcPlacementOwnership.REFERENCED,
                        StoryArcPlacement.operation_token == reference_removal_token,
                    )
                )
                if result.rowcount != 1:  # type: ignore[attr-defined]
                    await session.rollback()
                    raise StoryArcPlacementIntegrationError(
                        "placement_operation_in_progress",
                        "Referenced placement evidence is being updated by another operation",
                        category="conflict",
                    )
                await session.execute(
                    sa_update(IssueStoryArc)
                    .where(IssueStoryArc.id == membership_id)
                    .values(
                        sync_eligible=False,
                        last_materialization_result={
                            "schema_version": 1,
                            "status": "placement_reference_removed",
                            "placement_id": placement_id,
                            "artifact_removed": False,
                            "canonical_preserved": True,
                            "referenced_artifact_preserved": True,
                        },
                    )
                )
                await session.commit()
                return StoryArcPlacementRemovalView(
                    placement_id=placement_id,
                    ownership=ownership,
                    artifact_removed=False,
                    referenced_artifact_preserved=True,
                )

        if not confirm_managed_artifact_removal:
            raise StoryArcPlacementIntegrationError(
                "managed_removal_confirmation_required",
                (
                    "Confirm removal of the Pullbox-managed arc artifact; "
                    "the canonical comic will be preserved"
                ),
            )

        evidence = _managed_removal_evidence(placement)
        if evidence is None:
            raise StoryArcPlacementIntegrationError(
                "managed_ownership_evidence_missing",
                "Managed placement cannot be removed without durable ownership evidence",
                category="ownership",
            )
        policy = await self.get_policy(session, story_arc_id)
        if policy.destination_root is None or policy.mode in {
            StoryArcPlacementPolicyMode.LOGICAL,
            StoryArcPlacementPolicyMode.REFERENCE_ONLY,
        }:
            raise StoryArcPlacementIntegrationError(
                "managed_policy_evidence_missing",
                "Managed placement has no valid destination policy for safe removal",
                category="safety",
            )
        canonical_path_raw = (
            await session.scalar(
                select(LibraryFile.file_path).where(LibraryFile.id == placement.library_file_id)
            )
            if placement.library_file_id is not None
            else None
        )
        observed_token = placement.operation_token
        previous_result = dict(placement.last_result or {})
        if abandoned_published_operation_token is not None:
            _require_published_operation_token(
                placement,
                abandoned_published_operation_token,
            )
        if (
            observed_token is not None
            and abandoned_published_operation_token is None
            and placement.updated_at > datetime.now(UTC) - _PLACEMENT_OPERATION_LEASE
        ):
            raise StoryArcPlacementIntegrationError(
                "placement_operation_in_progress",
                "A story-arc placement operation is already in progress",
                category="conflict",
            )

        operation_token = uuid4().hex
        managed_token_filter: ColumnElement[bool] = (
            StoryArcPlacement.operation_token.is_(None)
            if observed_token is None
            else StoryArcPlacement.operation_token == observed_token
        )
        reserve = await session.execute(
            sa_update(StoryArcPlacement)
            .where(
                StoryArcPlacement.id == placement_id,
                StoryArcPlacement.ownership == StoryArcPlacementOwnership.MANAGED,
                managed_token_filter,
            )
            .values(
                operation_token=operation_token,
                last_result={
                    **previous_result,
                    "schema_version": 1,
                    "status": "remove_prepared",
                    "operation_token": operation_token,
                },
            )
        )
        if reserve.rowcount != 1:  # type: ignore[attr-defined]
            await session.rollback()
            raise StoryArcPlacementIntegrationError(
                "placement_operation_superseded",
                "Another worker reserved this story-arc placement",
                category="conflict",
            )
        await session.commit()

        caller_cancelled = False
        try:
            removed, caller_cancelled = await _run_filesystem_call(
                partial(
                    remove_managed_story_arc_placement,
                    evidence,
                    destination_root=Path(policy.destination_root),
                    canonical_path=(
                        Path(canonical_path_raw) if canonical_path_raw is not None else None
                    ),
                ),
                heartbeat=partial(
                    _refresh_operation_lease,
                    session,
                    placement_id=placement_id,
                    operation_token=operation_token,
                ),
            )
        except StoryArcPlacementError as exc:
            if (
                not evidence.target_fingerprint
                and exc.code == "filesystem_error"
                and isinstance(exc.__cause__, FileNotFoundError)
            ):
                # The secure, root-anchored walk proved that an intermediate
                # target directory is absent.  This is the same idempotent
                # absence represented by the removal-only ``{}`` sentinel.
                removed = StoryArcPlacementRemovalResult(
                    placement_path=evidence.placement_path,
                    removed=False,
                )
            else:
                await _persist_removal_failure(
                    session,
                    placement_id=placement_id,
                    operation_token=operation_token,
                    error=exc,
                )
                raise _translate_filesystem_error(exc) from exc
        except (OSError, ValueError) as exc:
            error = StoryArcPlacementIntegrationError(
                "placement_removal_failed",
                "Story-arc placement removal failed safely",
                category="safety",
            )
            await _persist_removal_failure(
                session,
                placement_id=placement_id,
                operation_token=operation_token,
                error=error,
            )
            raise error from exc

        await _delete_removed_placement_checkpoint(
            session,
            placement_id=placement_id,
            membership_id=membership_id,
            operation_token=operation_token,
            artifact_removed=removed.removed,
        )
        if caller_cancelled:
            raise asyncio.CancelledError
        return StoryArcPlacementRemovalView(
            placement_id=placement_id,
            ownership=ownership,
            artifact_removed=removed.removed,
        )

    async def _sync_membership_locked(
        self,
        session: AsyncSession,
        story_arc_id: int,
        membership_id: int,
        *,
        adopt_identical_existing: bool,
        cancellation_requested: Callable[[], bool] | None,
        import_provenance: StoryArcPlacementImportProvenance | None,
    ) -> StoryArcPlacementSyncResult:
        arc = await session.get(StoryArc, story_arc_id)
        if arc is None:
            raise _not_found("story_arc_not_found", "Story arc was not found")
        if arc.lifecycle is StoryArcLifecycle.ARCHIVED:
            raise StoryArcPlacementIntegrationError(
                "story_arc_archived",
                "Archived story arcs cannot synchronize placements",
            )
        policy = _policy_from_arc(arc)
        if not policy.configured:
            raise StoryArcPlacementIntegrationError(
                "placement_policy_not_configured",
                "Configure the story-arc placement policy before synchronization",
            )
        if not await _policy_root_is_available(session, policy):
            raise StoryArcPlacementIntegrationError(
                "target_library_root_unavailable",
                "Selected story-arc library root is no longer available",
                category="safety",
            )
        context = await _load_one_context(session, story_arc_id, membership_id)
        membership = await session.get(IssueStoryArc, membership_id)
        if membership is None:  # pragma: no cover - guarded by context loader
            raise _not_found("membership_not_found", "Story-arc membership was not found")
        if context.issue_id is None or context.library_file_id is None:
            raise StoryArcPlacementIntegrationError(
                "canonical_file_unavailable",
                "The resolved story-arc membership has no canonical library file",
                category="safety",
            )
        if membership.resolution_state is not StoryArcResolutionState.RESOLVED:
            raise StoryArcPlacementIntegrationError(
                "membership_not_resolved",
                "Only a resolved story-arc membership can synchronize a placement",
            )
        if import_provenance is not None:
            await _validate_import_provenance(
                session,
                provenance=import_provenance,
                story_arc_id=story_arc_id,
                membership_id=membership_id,
            )
            if (
                policy.mode
                not in {
                    StoryArcPlacementPolicyMode.COPY,
                    StoryArcPlacementPolicyMode.HARDLINK,
                    StoryArcPlacementPolicyMode.SYMLINK,
                }
                or adopt_identical_existing
            ):
                raise StoryArcPlacementIntegrationError(
                    "import_placement_requires_managed_mode",
                    "Import-origin placement work requires copy, hardlink, or symlink mode",
                    category="ownership",
                )

        if policy.mode is StoryArcPlacementPolicyMode.LOGICAL:
            outcome = StoryArcPlacementPolicyMode.LOGICAL.value
            membership.last_materialization_result = {
                "schema_version": 1,
                "status": "complete",
                "outcome": outcome,
            }
            await session.commit()
            return StoryArcPlacementSyncResult(
                membership_id=membership_id,
                outcome=outcome,
                placement=None,
            )
        if policy.mode is StoryArcPlacementPolicyMode.REFERENCE_ONLY:
            return await _sync_reference_only(
                session,
                context=context,
                policy=policy,
                membership=membership,
                adopt_identical_existing=adopt_identical_existing,
                cancellation_requested=cancellation_requested,
            )
        if adopt_identical_existing:
            # Explicit adoption is a read-only referenced operation.  Route it
            # through the fail-closed reference path so a disappearing target
            # can never turn the confirmation into an unjournaled managed copy.
            return await _sync_reference_only(
                session,
                context=context,
                policy=policy,
                membership=membership,
                adopt_identical_existing=True,
                cancellation_requested=cancellation_requested,
            )

        plan = _build_plan(
            context,
            policy,
            adopt_identical_existing=adopt_identical_existing,
        )
        if plan.destination_root is None:  # pragma: no cover - complete policy invariant
            raise StoryArcPlacementIntegrationError(
                "destination_root_required",
                "Managed placement policy requires a destination root",
            )
        relative_path = render_story_arc_relative_path(
            plan.values,
            folder_template=policy.folder_template,
            file_template=policy.file_template,
        )
        target_path = plan.destination_root / relative_path
        existing = await session.scalar(
            select(StoryArcPlacement)
            .where(
                StoryArcPlacement.issue_story_arc_id == membership_id,
                StoryArcPlacement.placement_path == str(target_path),
            )
            .limit(1)
        )
        other_managed = await session.scalar(
            select(StoryArcPlacement)
            .where(
                StoryArcPlacement.issue_story_arc_id == membership_id,
                StoryArcPlacement.ownership == StoryArcPlacementOwnership.MANAGED,
                StoryArcPlacement.placement_path != str(target_path),
            )
            .order_by(StoryArcPlacement.id.asc())
            .limit(1)
        )
        if existing is not None and existing.ownership is StoryArcPlacementOwnership.REFERENCED:
            if import_provenance is not None:
                raise StoryArcPlacementIntegrationError(
                    "import_placement_reference_not_owned",
                    "Import-origin work cannot adopt or replace a referenced artifact",
                    category="ownership",
                )
            # A prior explicit adoption or crash-safe ownership downgrade stays
            # referenced.  Validate it read-only and never silently promote it
            # to a Pullbox-managed artifact.
            return await _sync_reference_only(
                session,
                context=context,
                policy=policy,
                membership=membership,
                adopt_identical_existing=False,
                cancellation_requested=cancellation_requested,
            )
        if import_provenance is not None and other_managed is not None:
            raise StoryArcPlacementIntegrationError(
                "import_placement_existing_managed_not_owned",
                "Import-origin work cannot change an existing managed placement",
                category="ownership",
            )
        if other_managed is not None:
            other_managed.state = StoryArcPlacementState.DRIFTED
            other_managed.last_result = {
                **dict(other_managed.last_result or {}),
                "status": "drifted",
                "error_code": "policy_destination_changed",
            }
            membership.last_materialization_result = {
                "schema_version": 1,
                "status": "drifted",
                "error_code": "policy_destination_changed",
            }
            await session.commit()
            raise StoryArcPlacementIntegrationError(
                "policy_destination_changed",
                "Existing managed placement requires an explicit policy-change repair",
                category="conflict",
            )
        if (
            import_provenance is not None
            and existing is not None
            and (
                existing.source_import_job_id != import_provenance.import_job_id
                or existing.creating_action_id != import_provenance.import_action_id
            )
        ):
            raise StoryArcPlacementIntegrationError(
                "import_placement_existing_managed_not_owned",
                "Import-origin work cannot retrofit ownership onto an existing placement",
                category="ownership",
            )

        existing_id = existing.id if existing is not None else None
        existing_operation_token = existing.operation_token if existing is not None else None
        existing_last_result = dict(existing.last_result or {}) if existing is not None else {}
        if (
            existing is not None
            and existing_operation_token is not None
            and existing_last_result.get("status") != "published_pending_reconcile"
            and existing.updated_at > datetime.now(UTC) - _PLACEMENT_OPERATION_LEASE
        ):
            raise StoryArcPlacementIntegrationError(
                "placement_operation_in_progress",
                "A story-arc placement operation is already in progress",
                category="conflict",
            )
        evidence = _managed_evidence(existing) if existing is not None else None
        prepared_evidence = _prepared_managed_evidence(existing) if existing is not None else None

        # Close the read transaction before hashing the canonical source or
        # inspecting the destination root.  Only detached scalar context is
        # retained across this boundary.
        await session.commit()
        if cancellation_requested is not None and cancellation_requested():
            cancellation = StoryArcPlacementIntegrationError(
                "cancelled",
                "Story-arc placement was cancelled",
                category="cancelled",
            )
            await _persist_preflight_failure(
                session,
                membership_id=membership_id,
                placement_id=existing_id,
                observed_operation_token=existing_operation_token,
                error=cancellation,
            )
            raise cancellation
        try:
            recovered = (
                await asyncio.to_thread(
                    recover_prepared_story_arc_placement,
                    plan,
                    prepared_evidence,
                )
                if prepared_evidence is not None
                else None
            )
            preparation = (
                await asyncio.to_thread(
                    prepare_story_arc_placement,
                    plan,
                    existing_managed=evidence,
                )
                if recovered is None
                else None
            )
        except StoryArcPlacementError as exc:
            if existing_id is not None and prepared_evidence is not None:
                await _persist_failure(
                    session,
                    membership_id=membership_id,
                    placement_id=existing_id,
                    operation_token=(
                        prepared_evidence.operation_token
                        if prepared_evidence is not None
                        else uuid4().hex
                    ),
                    error=exc,
                )
            else:
                await _persist_preflight_failure(
                    session,
                    membership_id=membership_id,
                    placement_id=existing_id,
                    observed_operation_token=existing_operation_token,
                    error=exc,
                )
            raise _translate_filesystem_error(exc) from exc

        if recovered is not None:
            if existing_id is None or prepared_evidence is None:  # pragma: no cover
                raise StoryArcPlacementIntegrationError(
                    "prepared_placement_missing",
                    "Prepared story-arc placement record disappeared during recovery",
                    category="conflict",
                )
            synchronized, reconcile_cancelled = await _run_published_reconciliation(
                session,
                context=context,
                policy=policy,
                prepared_placement_id=existing_id,
                operation_token=prepared_evidence.operation_token,
                result=recovered,
            )
            if reconcile_cancelled:
                raise asyncio.CancelledError
            return synchronized

        if preparation is None:  # pragma: no cover - recovered returned above
            raise StoryArcPlacementIntegrationError(
                "placement_preparation_missing",
                "Story-arc placement preparation did not produce durable evidence",
                category="safety",
            )

        session.expire_all()
        current_arc = await session.get(StoryArc, story_arc_id)
        if current_arc is None:
            raise _not_found("story_arc_not_found", "Story arc was not found")
        if _policy_from_arc(current_arc) != policy:
            raise StoryArcPlacementIntegrationError(
                "placement_policy_changed",
                "Story-arc placement policy changed during preparation",
                category="conflict",
            )
        current_context = await _load_one_context(session, story_arc_id, membership_id)
        if current_context != context:
            raise StoryArcPlacementIntegrationError(
                "canonical_context_changed",
                "Canonical issue or library-file context changed during preparation",
                category="conflict",
            )
        if import_provenance is not None:
            await _validate_import_provenance(
                session,
                provenance=import_provenance,
                story_arc_id=story_arc_id,
                membership_id=membership_id,
            )
            if cancellation_requested is not None and cancellation_requested():
                raise StoryArcPlacementIntegrationError(
                    "cancelled",
                    "Import-origin story-arc placement was cancelled before reservation",
                    category="cancelled",
                )
        existing = (
            await session.get(StoryArcPlacement, existing_id)
            if existing_id is not None
            else await session.scalar(
                select(StoryArcPlacement).where(
                    StoryArcPlacement.placement_path == str(preparation.target_path)
                )
            )
        )
        if existing_id is None and existing is not None:
            raise StoryArcPlacementIntegrationError(
                "placement_concurrency_conflict",
                "Another placement operation reserved this destination",
                category="conflict",
            )
        if existing_id is not None and existing is None:
            raise StoryArcPlacementIntegrationError(
                "prepared_placement_missing",
                "Prepared story-arc placement record disappeared during preparation",
                category="conflict",
            )

        operation_token = uuid4().hex
        if existing is None:
            existing = StoryArcPlacement(
                issue_story_arc_id=membership_id,
                library_file_id=context.library_file_id,
                library_root_id=policy.target_library_root_id,
                placement_path=str(preparation.target_path),
                mode=_filesystem_mode(policy.mode),
                ownership=StoryArcPlacementOwnership.MANAGED,
                symlink_style=policy.symlink_style,
                source_kind=StoryArcSourceKind.PULLBOX,
                source_import_job_id=(
                    import_provenance.import_job_id if import_provenance is not None else None
                ),
                creating_action_id=(
                    import_provenance.import_action_id if import_provenance is not None else None
                ),
                rendered_reading_order=context.sequence_number,
                policy_schema_version=STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION,
                source_fingerprint=dict(preparation.source_fingerprint),
                state=StoryArcPlacementState.MISSING,
                last_result={},
                operation_token=operation_token,
            )
            session.add(existing)
            existing.last_result = {
                "schema_version": 1,
                "status": "prepared",
                "operation_token": operation_token,
                "prepared_evidence": _preparation_snapshot(preparation),
            }
            await session.flush()
            prepared_placement_id = existing.id
        else:
            token_filter = (
                StoryArcPlacement.operation_token.is_(None)
                if existing_operation_token is None
                else StoryArcPlacement.operation_token == existing_operation_token
            )
            reserve_result = await session.execute(
                sa_update(StoryArcPlacement)
                .where(StoryArcPlacement.id == existing.id, token_filter)
                .values(
                    source_fingerprint=dict(preparation.source_fingerprint),
                    operation_token=operation_token,
                    last_result={
                        **existing_last_result,
                        "schema_version": 1,
                        "status": "prepared",
                        "operation_token": operation_token,
                        "prepared_evidence": _preparation_snapshot(preparation),
                    },
                )
            )
            if reserve_result.rowcount != 1:  # type: ignore[attr-defined]
                await session.rollback()
                raise StoryArcPlacementIntegrationError(
                    "placement_operation_superseded",
                    "Another worker reserved this story-arc placement",
                    category="conflict",
                )
            prepared_placement_id = existing.id
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise StoryArcPlacementIntegrationError(
                "placement_concurrency_conflict",
                "Another placement operation reserved this destination",
                category="conflict",
            ) from exc

        caller_cancelled = False
        try:
            result, caller_cancelled = await _run_filesystem_call(
                partial(
                    execute_story_arc_placement,
                    plan,
                    existing_managed=evidence,
                    preparation=preparation,
                    cancellation_requested=cancellation_requested,
                ),
                heartbeat=partial(
                    _refresh_operation_lease,
                    session,
                    placement_id=prepared_placement_id,
                    operation_token=operation_token,
                ),
            )
        except StoryArcPlacementError as exc:
            await _persist_failure(
                session,
                membership_id=membership_id,
                placement_id=prepared_placement_id,
                operation_token=operation_token,
                error=exc,
            )
            raise _translate_filesystem_error(exc) from exc
        except (OSError, ValueError) as exc:
            error = StoryArcPlacementIntegrationError(
                "placement_execution_failed",
                "Story-arc placement execution failed safely",
                category="safety",
            )
            await _persist_failure(
                session,
                membership_id=membership_id,
                placement_id=prepared_placement_id,
                operation_token=operation_token,
                error=error,
            )
            raise error from exc

        synchronized, reconcile_cancelled = await _run_published_reconciliation(
            session,
            context=context,
            policy=policy,
            prepared_placement_id=prepared_placement_id,
            operation_token=operation_token,
            result=result,
        )
        if caller_cancelled or reconcile_cancelled:
            raise asyncio.CancelledError
        return synchronized


async def validate_story_arc_placement_policy_input(
    session: AsyncSession,
    proposal: StoryArcPlacementPolicyInput,
    *,
    revision: int,
) -> StoryArcPlacementPolicy:
    """Validate a complete policy for normal management or staged import confirmation."""
    try:
        mode = StoryArcPlacementPolicyMode(proposal.mode)
    except ValueError as exc:
        raise StoryArcPlacementIntegrationError(
            "unsupported_mode",
            "Unsupported story-arc placement policy mode",
        ) from exc
    try:
        validate_story_arc_folder_template(proposal.folder_template)
    except ValueError as exc:
        raise StoryArcPlacementIntegrationError(
            "invalid_folder_template",
            str(exc),
        ) from exc
    try:
        validate_story_arc_file_template(proposal.file_template)
    except ValueError as exc:
        raise StoryArcPlacementIntegrationError(
            "invalid_file_template",
            str(exc),
        ) from exc
    if not isinstance(proposal.synchronize, bool):
        raise StoryArcPlacementIntegrationError(
            "invalid_synchronize_flag",
            "Story-arc synchronize must be true or false",
        )
    symlink_style: StoryArcSymlinkStyle | None = None
    if proposal.symlink_style is not None:
        try:
            symlink_style = StoryArcSymlinkStyle(proposal.symlink_style)
        except ValueError as exc:
            raise StoryArcPlacementIntegrationError(
                "unsupported_symlink_style",
                "Unsupported story-arc symlink style",
            ) from exc
    if mode is StoryArcPlacementPolicyMode.SYMLINK and symlink_style is None:
        raise StoryArcPlacementIntegrationError(
            "symlink_style_required",
            "Symlink placement policy requires an absolute or relative style",
        )
    if mode is not StoryArcPlacementPolicyMode.SYMLINK and symlink_style is not None:
        raise StoryArcPlacementIntegrationError(
            "symlink_style_not_allowed",
            "Only symlink placement policy may specify a symlink style",
        )

    if mode is StoryArcPlacementPolicyMode.LOGICAL:
        if proposal.synchronize:
            raise StoryArcPlacementIntegrationError(
                "logical_policy_cannot_synchronize",
                "Logical-only story arcs do not synchronize filesystem placements",
            )
        if proposal.target_library_root_id is not None or proposal.destination_root is not None:
            raise StoryArcPlacementIntegrationError(
                "logical_policy_has_root",
                "Logical-only story arcs must not configure a placement root",
            )
        return StoryArcPlacementPolicy(
            configured=True,
            revision=revision,
            mode=mode,
            target_library_root_id=None,
            destination_root=None,
            folder_template=proposal.folder_template,
            file_template=proposal.file_template,
            symlink_style=None,
            synchronize=proposal.synchronize,
        )

    root_id = proposal.target_library_root_id
    if isinstance(root_id, bool) or not isinstance(root_id, int) or root_id < 1:
        raise StoryArcPlacementIntegrationError(
            "target_library_root_required",
            "Placement policy requires a selected library root",
        )
    library_root = await session.get(LibraryRoot, root_id)
    if library_root is None or not library_root.enabled:
        raise StoryArcPlacementIntegrationError(
            "target_library_root_unavailable",
            "Selected story-arc library root is unavailable",
            category="safety",
        )
    raw_destination = proposal.destination_root
    if raw_destination is None or not raw_destination.strip():
        raise StoryArcPlacementIntegrationError(
            "destination_root_required",
            "Placement policy requires an approved destination root",
        )
    destination = Path(raw_destination)
    if not destination.is_absolute():
        raise StoryArcPlacementIntegrationError(
            "destination_root_not_absolute",
            "Story-arc destination root must be absolute",
            category="safety",
        )
    if destination.is_symlink():
        raise StoryArcPlacementIntegrationError(
            "symlink_root",
            "Story-arc destination root cannot be a symbolic link",
            category="safety",
        )
    try:
        resolved_destination = destination.resolve(strict=True)
    except OSError as exc:
        raise StoryArcPlacementIntegrationError(
            "destination_root_unavailable",
            "Story-arc destination root is unavailable",
            category="safety",
        ) from exc
    if not resolved_destination.is_dir():
        raise StoryArcPlacementIntegrationError(
            "destination_root_unavailable",
            "Story-arc destination root is not a directory",
            category="safety",
        )
    try:
        resolved_library_root = Path(library_root.path).resolve(strict=True)
    except OSError as exc:
        raise StoryArcPlacementIntegrationError(
            "target_library_root_unavailable",
            "Selected story-arc library root is unavailable",
            category="safety",
        ) from exc
    if not resolved_library_root.is_dir():
        raise StoryArcPlacementIntegrationError(
            "target_library_root_unavailable",
            "Selected story-arc library root is unavailable",
            category="safety",
        )
    if not resolved_destination.is_relative_to(resolved_library_root):
        raise StoryArcPlacementIntegrationError(
            "destination_root_outside_library_root",
            "Story-arc destination root must be within the selected library root",
            category="safety",
        )
    return StoryArcPlacementPolicy(
        configured=True,
        revision=revision,
        mode=mode,
        target_library_root_id=root_id,
        destination_root=str(resolved_destination),
        folder_template=proposal.folder_template,
        file_template=proposal.file_template,
        symlink_style=symlink_style,
        synchronize=proposal.synchronize,
    )


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


async def _validate_import_provenance(
    session: AsyncSession,
    *,
    provenance: StoryArcPlacementImportProvenance,
    story_arc_id: int,
    membership_id: int,
) -> None:
    """Fail closed unless an active import action exactly owns this request."""
    if not _is_positive_int(provenance.import_job_id) or not _is_positive_int(
        provenance.import_action_id
    ):
        raise StoryArcPlacementIntegrationError(
            "import_placement_provenance_invalid",
            "Import placement provenance requires positive job and action identifiers",
            category="ownership",
        )
    action = await session.get(ImportJobAction, provenance.import_action_id)
    job = await session.get(ImportJob, provenance.import_job_id)
    if (
        action is None
        or job is None
        or action.import_job_id != job.id
        or action.phase != _IMPORT_PLACEMENT_PHASE
        or action.action_type != _IMPORT_PLACEMENT_ACTION_TYPE
        or action.status is not ImportJobActionStatus.COMPLETED
    ):
        raise StoryArcPlacementIntegrationError(
            "import_placement_provenance_invalid",
            "Import placement action is missing, inactive, or belongs to another job",
            category="ownership",
        )
    payload = dict(action.payload or {})
    if (
        set(payload) != _IMPORT_PLACEMENT_PAYLOAD_KEYS
        or payload.get("schema_version") != 1
        or payload.get("membership_id") != membership_id
        or payload.get("source_import_job_id") != job.id
        or not _is_positive_int(payload.get("sync_work_id"))
        or not _is_positive_int(payload.get("imported_story_arc_id"))
        or not _is_positive_int(payload.get("imported_story_arc_entry_id"))
        or not isinstance(payload.get("desired_generation"), str)
        or len(str(payload.get("desired_generation"))) != 64
    ):
        raise StoryArcPlacementIntegrationError(
            "import_placement_payload_invalid",
            "Import placement action payload does not match the requested membership",
            category="ownership",
        )
    if (
        job.status is not ImportJobStatus.IMPORTING
        or job.control_request is not ImportControlRequest.NONE
        or dict(job.progress_snapshot or {}).get("phase") != _IMPORT_PLACEMENT_PHASE
    ):
        raise StoryArcPlacementIntegrationError(
            "import_placement_job_inactive",
            "Import job is not actively publishing Story Arc placements",
            category="cancelled",
        )
    membership_arc_id = await session.scalar(
        select(IssueStoryArc.story_arc_id).where(IssueStoryArc.id == membership_id)
    )
    if membership_arc_id != story_arc_id:
        raise StoryArcPlacementIntegrationError(
            "import_placement_membership_changed",
            "Import placement membership no longer belongs to the requested Story Arc",
            category="ownership",
        )


def _policy_from_arc(arc: StoryArc) -> StoryArcPlacementPolicy:
    raw = dict(arc.policy_snapshot or {})
    if arc.policy_schema_version != STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION:
        return _logical_default(arc.revision)
    if set(raw) != _POLICY_SNAPSHOT_KEYS or raw.get("schema_version") != 1:
        return _logical_default(arc.revision)
    try:
        mode = StoryArcPlacementPolicyMode(str(raw["mode"]))
        raw_style = raw["symlink_style"]
        style = StoryArcSymlinkStyle(str(raw_style)) if raw_style is not None else None
        root_id_raw = raw["target_library_root_id"]
        if root_id_raw is not None and (
            isinstance(root_id_raw, bool) or not isinstance(root_id_raw, int)
        ):
            raise ValueError
        root_id = root_id_raw
        destination_raw = raw["destination_root"]
        if destination_raw is not None and not isinstance(destination_raw, str):
            raise ValueError
        destination = destination_raw
        folder_template_raw = raw["folder_template"]
        file_template_raw = raw["file_template"]
        if not isinstance(folder_template_raw, str) or not isinstance(file_template_raw, str):
            raise ValueError
        folder_template = folder_template_raw
        file_template = file_template_raw
        synchronize_raw = raw["synchronize"]
        if not isinstance(synchronize_raw, bool):
            raise ValueError
        validate_story_arc_folder_template(folder_template)
        validate_story_arc_file_template(file_template)
        if mode is StoryArcPlacementPolicyMode.SYMLINK and style is None:
            raise ValueError
        if mode is not StoryArcPlacementPolicyMode.SYMLINK and style is not None:
            raise ValueError
        if mode is StoryArcPlacementPolicyMode.LOGICAL:
            if root_id is not None or destination is not None or synchronize_raw:
                raise ValueError
        elif root_id is None or destination is None or not Path(destination).is_absolute():
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return _logical_default(arc.revision)
    return StoryArcPlacementPolicy(
        configured=True,
        revision=arc.revision,
        mode=mode,
        target_library_root_id=root_id,
        destination_root=destination,
        folder_template=folder_template,
        file_template=file_template,
        symlink_style=style,
        synchronize=synchronize_raw,
    )


def _logical_default(revision: int) -> StoryArcPlacementPolicy:
    return StoryArcPlacementPolicy(
        configured=False,
        revision=revision,
        mode=StoryArcPlacementPolicyMode.LOGICAL,
        target_library_root_id=None,
        destination_root=None,
        folder_template=DEFAULT_STORY_ARC_FOLDER_TEMPLATE,
        file_template=DEFAULT_STORY_ARC_FILE_TEMPLATE,
        symlink_style=None,
        synchronize=False,
    )


async def _policy_root_is_available(
    session: AsyncSession,
    policy: StoryArcPlacementPolicy,
) -> bool:
    if policy.mode is StoryArcPlacementPolicyMode.LOGICAL:
        return True
    if policy.target_library_root_id is None:
        return False
    enabled = await session.scalar(
        select(LibraryRoot.enabled).where(LibraryRoot.id == policy.target_library_root_id)
    )
    return enabled is True


def _placement_policy_shape(
    policy: StoryArcPlacementPolicy,
) -> tuple[object, ...]:
    return (
        policy.mode,
        policy.target_library_root_id,
        policy.destination_root,
        policy.folder_template,
        policy.file_template,
        policy.symlink_style,
    )


async def _arc_has_managed_placements(
    session: AsyncSession,
    story_arc_id: int,
) -> bool:
    placement_id = await session.scalar(
        select(StoryArcPlacement.id)
        .join(IssueStoryArc, StoryArcPlacement.issue_story_arc_id == IssueStoryArc.id)
        .where(
            IssueStoryArc.story_arc_id == story_arc_id,
            StoryArcPlacement.ownership == StoryArcPlacementOwnership.MANAGED,
        )
        .limit(1)
    )
    return placement_id is not None


async def _load_context_page(
    session: AsyncSession,
    story_arc_id: int,
    *,
    limit: int,
    offset: int,
) -> tuple[int, tuple[_PlacementContext, ...]]:
    arc = await session.get(StoryArc, story_arc_id)
    if arc is None:
        raise _not_found("story_arc_not_found", "Story arc was not found")
    total = int(
        await session.scalar(
            select(func.count(IssueStoryArc.id)).where(IssueStoryArc.story_arc_id == story_arc_id)
        )
        or 0
    )
    rows = (
        await session.execute(
            select(IssueStoryArc, Issue, Series, Publisher)
            .outerjoin(Issue, IssueStoryArc.issue_id == Issue.id)
            .outerjoin(Series, Issue.series_id == Series.id)
            .outerjoin(Publisher, Series.publisher_id == Publisher.id)
            .where(IssueStoryArc.story_arc_id == story_arc_id)
            .order_by(
                IssueStoryArc.sequence_number.asc(),
                IssueStoryArc.source_ordinal.asc(),
                IssueStoryArc.id.asc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    issue_ids = [issue.id for _membership, issue, _series, _publisher in rows if issue is not None]
    files_by_issue: dict[int, LibraryFile] = {}
    if issue_ids:
        files = list(
            (
                await session.scalars(
                    select(LibraryFile)
                    .where(LibraryFile.issue_id.in_(issue_ids))
                    .order_by(LibraryFile.issue_id.asc(), LibraryFile.id.asc())
                )
            ).all()
        )
        for library_file in files:
            if library_file.issue_id is not None:
                files_by_issue.setdefault(library_file.issue_id, library_file)
    return total, tuple(
        _context_from_row(
            arc,
            membership,
            issue,
            series,
            publisher,
            files_by_issue.get(issue.id) if issue is not None else None,
        )
        for membership, issue, series, publisher in rows
    )


async def _load_one_context(
    session: AsyncSession,
    story_arc_id: int,
    membership_id: int,
) -> _PlacementContext:
    arc = await session.get(StoryArc, story_arc_id)
    if arc is None:
        raise _not_found("story_arc_not_found", "Story arc was not found")
    row = (
        await session.execute(
            select(IssueStoryArc, Issue, Series, Publisher)
            .outerjoin(Issue, IssueStoryArc.issue_id == Issue.id)
            .outerjoin(Series, Issue.series_id == Series.id)
            .outerjoin(Publisher, Series.publisher_id == Publisher.id)
            .where(
                IssueStoryArc.id == membership_id,
                IssueStoryArc.story_arc_id == story_arc_id,
            )
        )
    ).one_or_none()
    if row is None:
        raise _not_found("membership_not_found", "Story-arc membership was not found")
    membership, issue, series, publisher = row
    library_file = None
    if issue is not None:
        library_file = await session.scalar(
            select(LibraryFile)
            .where(LibraryFile.issue_id == issue.id)
            .order_by(LibraryFile.id.asc())
            .limit(1)
        )
    return _context_from_row(arc, membership, issue, series, publisher, library_file)


def _context_from_row(
    arc: StoryArc,
    membership: IssueStoryArc,
    issue: Issue | None,
    series: Series | None,
    publisher: Publisher | None,
    library_file: LibraryFile | None,
) -> _PlacementContext:
    exact_number = (
        issue.effective_issue_number_text
        if issue is not None
        else membership.source_issue_number_text or "unknown"
    )
    extension = (
        library_file.file_format.value
        if library_file is not None
        else Path(library_file.file_path).suffix.lstrip(".").lower()
        if library_file is not None
        else "cbz"
    )
    return _PlacementContext(
        membership_id=membership.id,
        story_arc_id=membership.story_arc_id,
        sequence_number=membership.sequence_number,
        issue_id=issue.id if issue is not None else None,
        issue_number_text=exact_number,
        story_arc_name=arc.name,
        series_name=(
            series.title
            if series is not None
            else membership.source_series_name or "Unknown Series"
        ),
        publisher_name=(publisher.name if publisher is not None else membership.source_publisher),
        issue_title=issue.title if issue is not None else membership.source_issue_title,
        year=(
            issue.release_date.year
            if issue is not None and issue.release_date is not None
            else series.year_start
            if series is not None
            else None
        ),
        series_start_year=series.year_start if series is not None else None,
        series_end_year=series.year_end if series is not None else None,
        library_file_id=library_file.id if library_file is not None else None,
        canonical_path=library_file.file_path if library_file is not None else None,
        extension=extension,
    )


def _rendered_target_paths(
    contexts: Sequence[_PlacementContext],
    policy: StoryArcPlacementPolicy,
) -> dict[int, str]:
    """Render only the targets represented by the already-bounded context page."""
    if policy.mode is StoryArcPlacementPolicyMode.LOGICAL or policy.destination_root is None:
        return {}
    return {context.membership_id: _rendered_target_path(context, policy) for context in contexts}


def _rendered_target_path(
    context: _PlacementContext,
    policy: StoryArcPlacementPolicy,
) -> str:
    if policy.destination_root is None:
        raise ValueError("Placement policy has no destination root")
    return str(
        Path(policy.destination_root)
        / render_story_arc_relative_path(
            context.naming_values(),
            folder_template=policy.folder_template,
            file_template=policy.file_template,
        )
    )


async def _load_matching_preview_evidence(
    session: AsyncSession,
    target_paths: dict[int, str],
) -> dict[str, _PreviewPlacementEvidence]:
    """Load at most one unique placement row per target on the bounded page."""
    if not target_paths:
        return {}
    rows = list(
        (
            await session.scalars(
                select(StoryArcPlacement)
                .where(StoryArcPlacement.placement_path.in_(tuple(target_paths.values())))
                .order_by(StoryArcPlacement.id.asc())
            )
        ).all()
    )
    return {
        row.placement_path: _PreviewPlacementEvidence(
            id=row.id,
            issue_story_arc_id=row.issue_story_arc_id,
            placement_path=row.placement_path,
            mode=row.mode,
            ownership=row.ownership,
            symlink_style=row.symlink_style,
            source_fingerprint=dict(row.source_fingerprint or {}),
            target_fingerprint=_stored_target_fingerprint(row),
            creating_action_id=row.creating_action_id,
        )
        for row in rows
    }


def _stored_target_fingerprint(row: StoryArcPlacement) -> dict[str, object]:
    target_fingerprint = dict(row.last_result or {}).get("target_fingerprint")
    return dict(target_fingerprint) if isinstance(target_fingerprint, dict) else {}


def _preview_contexts(
    contexts: Sequence[_PlacementContext],
    policy: StoryArcPlacementPolicy,
    evidence_by_path: dict[str, _PreviewPlacementEvidence],
) -> tuple[StoryArcPlacementPreviewItem, ...]:
    items: list[StoryArcPlacementPreviewItem] = []
    for context in contexts:
        if policy.mode is StoryArcPlacementPolicyMode.LOGICAL:
            items.append(
                StoryArcPlacementPreviewItem(
                    membership_id=context.membership_id,
                    sequence_number=context.sequence_number,
                    issue_id=context.issue_id,
                    issue_number_text=context.issue_number_text,
                    mode=policy.mode.value,
                    state=StoryArcPlacementPreviewState.LOGICAL_ONLY.value,
                    target_path=None,
                    collision=StoryArcCollisionKind.NONE.value,
                    reason=None,
                    required_bytes=0,
                    proposed_ownership=StoryArcPlacementOwnership.REFERENCED.value,
                    overwrite_allowed=False,
                    classification="logical_only",
                    placement_id=None,
                    current_ownership=None,
                    inspection_code=None,
                )
            )
            continue
        preview_mode = (
            StoryArcPlacementMode.COPY
            if policy.mode is StoryArcPlacementPolicyMode.REFERENCE_ONLY
            else _filesystem_mode(policy.mode)
        )
        plan = StoryArcPlacementPlan(
            issue_story_arc_id=context.membership_id,
            library_file_id=context.library_file_id,
            canonical_path=Path(context.canonical_path) if context.canonical_path else None,
            destination_root=Path(policy.destination_root) if policy.destination_root else None,
            values=context.naming_values(),
            mode=preview_mode,
            symlink_style=policy.symlink_style,
            folder_template=policy.folder_template,
            file_template=policy.file_template,
        )
        rendered_target = _rendered_target_path(context, policy)
        evidence = evidence_by_path.get(rendered_target) if rendered_target else None
        if evidence is not None and evidence.issue_story_arc_id != context.membership_id:
            items.append(_preview_destination_conflict(context, policy, evidence))
            continue
        inspection = inspect_story_arc_placement(
            plan,
            existing=_inspection_evidence(evidence),
        )
        items.append(_preview_item_from_inspection(context, policy, evidence, inspection))
    return tuple(items)


def _inspection_evidence(
    evidence: _PreviewPlacementEvidence | None,
) -> StoryArcPlacementInspectionEvidence | None:
    if evidence is None:
        return None
    return StoryArcPlacementInspectionEvidence(
        placement_path=Path(evidence.placement_path),
        mode=evidence.mode,
        ownership=evidence.ownership,
        symlink_style=evidence.symlink_style,
        source_fingerprint=dict(evidence.source_fingerprint),
        target_fingerprint=dict(evidence.target_fingerprint),
    )


def _preview_item_from_inspection(
    context: _PlacementContext,
    policy: StoryArcPlacementPolicy,
    evidence: _PreviewPlacementEvidence | None,
    inspection: StoryArcPlacementInspection,
) -> StoryArcPlacementPreviewItem:
    classification = inspection.state.value
    if inspection.state is StoryArcPlacementInspectionState.FREE:
        classification = (
            "reference_missing"
            if policy.mode is StoryArcPlacementPolicyMode.REFERENCE_ONLY
            else "will_materialize"
        )
    state = _legacy_inspection_state(inspection.state, policy)
    collision = _inspection_collision(inspection, policy)
    reason = inspection.reason
    if classification == "reference_missing":
        reason = "No existing artifact is available to reference"
    elif inspection.state is StoryArcPlacementInspectionState.UNTRACKED_IDENTICAL:
        reason = "An identical user artifact requires explicit referenced-placement review"
    return StoryArcPlacementPreviewItem(
        membership_id=context.membership_id,
        sequence_number=context.sequence_number,
        issue_id=context.issue_id,
        issue_number_text=context.issue_number_text,
        mode=policy.mode.value,
        state=state,
        target_path=str(inspection.target_path) if inspection.target_path is not None else None,
        collision=collision,
        reason=reason,
        required_bytes=(
            0
            if policy.mode is StoryArcPlacementPolicyMode.REFERENCE_ONLY
            else inspection.required_bytes
        ),
        proposed_ownership=(
            StoryArcPlacementOwnership.REFERENCED.value
            if policy.mode is StoryArcPlacementPolicyMode.REFERENCE_ONLY
            or inspection.state is StoryArcPlacementInspectionState.UNTRACKED_IDENTICAL
            else inspection.proposed_ownership
        ),
        overwrite_allowed=False,
        classification=classification,
        placement_id=evidence.id if evidence is not None else None,
        current_ownership=(evidence.ownership.value if evidence is not None else None),
        inspection_code=inspection.code,
    )


def _legacy_inspection_state(
    state: StoryArcPlacementInspectionState,
    policy: StoryArcPlacementPolicy,
) -> str:
    if state in {
        StoryArcPlacementInspectionState.MANAGED_CURRENT,
        StoryArcPlacementInspectionState.REFERENCED_CURRENT,
    }:
        return StoryArcPlacementPreviewState.ALREADY_REPRESENTED.value
    if state in {
        StoryArcPlacementInspectionState.MANAGED_MISSING,
        StoryArcPlacementInspectionState.REFERENCED_MISSING,
    } or (
        state is StoryArcPlacementInspectionState.FREE
        and policy.mode is StoryArcPlacementPolicyMode.REFERENCE_ONLY
    ):
        return "missing"
    if state in {
        StoryArcPlacementInspectionState.MANAGED_DRIFTED,
        StoryArcPlacementInspectionState.REFERENCED_DRIFTED,
    }:
        return "drifted"
    if state is StoryArcPlacementInspectionState.FREE:
        return StoryArcPlacementPreviewState.READY.value
    return StoryArcPlacementPreviewState.BLOCKED.value


def _inspection_collision(
    inspection: StoryArcPlacementInspection,
    policy: StoryArcPlacementPolicy,
) -> str:
    if inspection.state in {
        StoryArcPlacementInspectionState.MANAGED_CURRENT,
        StoryArcPlacementInspectionState.REFERENCED_CURRENT,
        StoryArcPlacementInspectionState.FREE,
    }:
        if (
            inspection.state is StoryArcPlacementInspectionState.FREE
            and policy.mode is StoryArcPlacementPolicyMode.REFERENCE_ONLY
        ):
            return "reference_missing"
        return StoryArcCollisionKind.NONE.value
    if inspection.state is StoryArcPlacementInspectionState.MANAGED_MISSING:
        return "managed_missing"
    if inspection.state is StoryArcPlacementInspectionState.REFERENCED_MISSING:
        return "reference_missing"
    if inspection.state is StoryArcPlacementInspectionState.UNTRACKED_IDENTICAL:
        return "identical_unconfirmed"
    if (
        inspection.state
        in {
            StoryArcPlacementInspectionState.MANAGED_DRIFTED,
            StoryArcPlacementInspectionState.REFERENCED_DRIFTED,
        }
        and inspection.source_fingerprint is not None
        and inspection.target_fingerprint is not None
        and not _inspection_content_matches(inspection)
    ):
        return StoryArcCollisionKind.DIFFERENT_CONTENT.value
    if (
        inspection.state
        in {
            StoryArcPlacementInspectionState.MANAGED_DRIFTED,
            StoryArcPlacementInspectionState.REFERENCED_DRIFTED,
        }
        and inspection.collision
        in {
            StoryArcCollisionKind.NONE,
            StoryArcCollisionKind.SAME_INODE_REFERENCED,
        }
        and inspection.code is not None
    ):
        return inspection.code
    return inspection.collision.value


def _inspection_content_matches(inspection: StoryArcPlacementInspection) -> bool:
    source = inspection.source_fingerprint or {}
    target = inspection.target_fingerprint or {}
    target_content = target.get("content") if target.get("kind") == "symlink" else target
    return bool(
        isinstance(target_content, dict)
        and source.get("sha256") is not None
        and source.get("sha256") == target_content.get("sha256")
    )


def _preview_destination_conflict(
    context: _PlacementContext,
    policy: StoryArcPlacementPolicy,
    evidence: _PreviewPlacementEvidence,
) -> StoryArcPlacementPreviewItem:
    return StoryArcPlacementPreviewItem(
        membership_id=context.membership_id,
        sequence_number=context.sequence_number,
        issue_id=context.issue_id,
        issue_number_text=context.issue_number_text,
        mode=policy.mode.value,
        state=StoryArcPlacementPreviewState.BLOCKED.value,
        target_path=evidence.placement_path,
        collision="placement_destination_conflict",
        reason="Story-arc destination is already tracked by another membership",
        required_bytes=0,
        proposed_ownership=(
            StoryArcPlacementOwnership.REFERENCED.value
            if policy.mode is StoryArcPlacementPolicyMode.REFERENCE_ONLY
            else StoryArcPlacementOwnership.MANAGED.value
        ),
        overwrite_allowed=False,
        classification="blocked",
        placement_id=evidence.id,
        current_ownership=evidence.ownership.value,
        inspection_code="placement_destination_conflict",
    )


async def _run_filesystem_call[FilesystemResultT](
    call: Callable[[], FilesystemResultT],
    *,
    heartbeat: Callable[[], Awaitable[None]] | None = None,
) -> tuple[FilesystemResultT, bool]:
    """Let a worker reach a safe boundary before propagating task cancellation.

    Cancelling an await on ``asyncio.to_thread`` does not stop its thread.  A
    copy could otherwise publish after the coroutine had abandoned its
    prepared row.  Shield the worker, remember cancellation, and let the caller
    persist publish evidence and reconcile before cancellation is re-raised.
    """
    worker = asyncio.create_task(asyncio.to_thread(call))
    caller_cancelled = False
    while True:
        try:
            done, _pending = await asyncio.wait(
                {worker},
                timeout=_PLACEMENT_HEARTBEAT_SECONDS,
            )
            if done:
                return worker.result(), caller_cancelled
            if heartbeat is not None:
                await heartbeat()
        except asyncio.CancelledError:
            caller_cancelled = True


async def _run_published_reconciliation(
    session: AsyncSession,
    *,
    context: _PlacementContext,
    policy: StoryArcPlacementPolicy,
    prepared_placement_id: int,
    operation_token: str,
    result: StoryArcPlacementResult,
) -> tuple[StoryArcPlacementSyncResult, bool]:
    """Finish durable checkpoint/reconcile before propagating cancellation."""

    async def finish() -> StoryArcPlacementSyncResult:
        await _persist_published_checkpoint(
            session,
            context=context,
            policy=policy,
            prepared_placement_id=prepared_placement_id,
            operation_token=operation_token,
            result=result,
        )
        return await _reconcile_result(
            session,
            context=context,
            policy=policy,
            prepared_placement_id=prepared_placement_id,
            operation_token=operation_token,
            result=result,
        )

    worker = asyncio.create_task(finish())
    caller_cancelled = False
    while True:
        try:
            return await asyncio.shield(worker), caller_cancelled
        except asyncio.CancelledError:
            caller_cancelled = True


async def _sync_reference_only(
    session: AsyncSession,
    *,
    context: _PlacementContext,
    policy: StoryArcPlacementPolicy,
    membership: IssueStoryArc,
    adopt_identical_existing: bool,
    cancellation_requested: Callable[[], bool] | None,
) -> StoryArcPlacementSyncResult:
    """Validate and explicitly adopt an existing artifact without creating one."""
    if policy.destination_root is None:
        raise StoryArcPlacementIntegrationError(
            "destination_root_required",
            "Reference-only placement policy requires a destination root",
        )
    target_path = Path(policy.destination_root) / render_story_arc_relative_path(
        context.naming_values(),
        folder_template=policy.folder_template,
        file_template=policy.file_template,
    )
    existing = await session.scalar(
        select(StoryArcPlacement).where(StoryArcPlacement.placement_path == str(target_path))
    )
    if existing is not None and existing.issue_story_arc_id != context.membership_id:
        raise StoryArcPlacementIntegrationError(
            "placement_destination_conflict",
            "Story-arc destination is already tracked by another membership",
            category="conflict",
        )
    if existing is not None and existing.ownership is StoryArcPlacementOwnership.MANAGED:
        raise StoryArcPlacementIntegrationError(
            "managed_placement_requires_repair",
            "A managed placement cannot be converted to referenced implicitly",
            category="ownership",
        )
    if existing is None and not adopt_identical_existing:
        raise StoryArcPlacementIntegrationError(
            "reference_adoption_required",
            "Adopting an existing user artifact requires explicit confirmation",
            category="conflict",
        )

    reference_operation_token: str | None = None
    if existing is not None:
        observed_token = existing.operation_token
        if (
            observed_token is not None
            and existing.updated_at > datetime.now(UTC) - _PLACEMENT_OPERATION_LEASE
        ):
            raise StoryArcPlacementIntegrationError(
                "placement_operation_in_progress",
                "Referenced placement evidence is being updated by another operation",
                category="conflict",
            )
        reference_operation_token = uuid4().hex
        token_filter = (
            StoryArcPlacement.operation_token.is_(None)
            if observed_token is None
            else StoryArcPlacement.operation_token == observed_token
        )
        previous_result = dict(existing.last_result or {})
        reserve = await session.execute(
            sa_update(StoryArcPlacement)
            .where(
                StoryArcPlacement.id == existing.id,
                StoryArcPlacement.ownership == StoryArcPlacementOwnership.REFERENCED,
                token_filter,
            )
            .values(
                operation_token=reference_operation_token,
                last_result={
                    **previous_result,
                    "schema_version": 1,
                    "status": "reference_validation_prepared",
                    "operation_token": reference_operation_token,
                },
            )
        )
        if reserve.rowcount != 1:  # type: ignore[attr-defined]
            await session.rollback()
            raise StoryArcPlacementIntegrationError(
                "placement_operation_superseded",
                "Another worker reserved this referenced placement",
                category="conflict",
            )

    # No database transaction remains open during read-only filesystem validation.
    await session.commit()

    def target_exists() -> bool:
        return target_path.exists() or target_path.is_symlink()

    if not await asyncio.to_thread(target_exists):
        error = StoryArcPlacementIntegrationError(
            "reference_target_missing",
            "No existing artifact is available to reference",
            category="safety",
        )
        await _persist_reference_failure(
            session,
            membership.id,
            error,
            placement_id=existing.id if existing is not None else None,
            operation_token=reference_operation_token,
        )
        raise error

    def adoption_cancelled() -> bool:
        return bool(
            (cancellation_requested is not None and cancellation_requested()) or not target_exists()
        )

    def adoption_journal(event: StoryArcPlacementJournalEvent) -> None:
        # Existing-target resolution returns before a publish journal event. If
        # execution ever reaches preparation, the target changed and this
        # reference-only operation must fail before creating anything.
        if event.stage == "prepared":
            raise StoryArcPlacementSafetyError(
                "reference_target_changed",
                "Referenced story-arc artifact changed during validation",
            )

    plan = StoryArcPlacementPlan(
        issue_story_arc_id=context.membership_id,
        library_file_id=context.library_file_id,
        canonical_path=Path(context.canonical_path) if context.canonical_path else None,
        destination_root=Path(policy.destination_root),
        values=context.naming_values(),
        mode=StoryArcPlacementMode.COPY,
        folder_template=policy.folder_template,
        file_template=policy.file_template,
        adopt_identical_existing=True,
    )
    caller_cancelled = False
    try:
        result, caller_cancelled = await _run_filesystem_call(
            partial(
                execute_story_arc_placement,
                plan,
                cancellation_requested=adoption_cancelled,
                journal=adoption_journal,
            )
        )
    except StoryArcPlacementError as exc:
        await _persist_reference_failure(
            session,
            membership.id,
            exc,
            placement_id=existing.id if existing is not None else None,
            operation_token=reference_operation_token,
        )
        raise _translate_filesystem_error(exc) from exc
    if result.ownership is not StoryArcPlacementOwnership.REFERENCED:
        error = StoryArcPlacementIntegrationError(
            "reference_materialization_forbidden",
            "Reference-only synchronization refused to create an artifact",
            category="safety",
        )
        await _persist_reference_failure(
            session,
            membership.id,
            error,
            placement_id=existing.id if existing is not None else None,
            operation_token=reference_operation_token,
        )
        raise error

    placement = await session.scalar(
        select(StoryArcPlacement).where(StoryArcPlacement.placement_path == str(target_path))
    )
    if existing is not None and (
        placement is None
        or placement.id != existing.id
        or placement.operation_token != reference_operation_token
    ):
        await session.rollback()
        raise StoryArcPlacementIntegrationError(
            "placement_operation_superseded",
            "Referenced placement validation lost its ownership lease",
            category="conflict",
        )
    if placement is None:
        placement = StoryArcPlacement(
            issue_story_arc_id=context.membership_id,
            library_file_id=context.library_file_id,
            library_root_id=policy.target_library_root_id,
            placement_path=str(target_path),
            mode=StoryArcPlacementMode.REFERENCE_ONLY,
            ownership=StoryArcPlacementOwnership.REFERENCED,
            symlink_style=None,
            source_kind=StoryArcSourceKind.PULLBOX,
            rendered_reading_order=context.sequence_number,
            policy_schema_version=STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION,
            source_fingerprint=dict(result.source_fingerprint),
            state=StoryArcPlacementState.CURRENT,
            last_result={},
        )
        session.add(placement)
    placement.library_file_id = context.library_file_id
    placement.library_root_id = policy.target_library_root_id
    placement.source_fingerprint = dict(result.source_fingerprint)
    placement.state = StoryArcPlacementState.CURRENT
    placement.last_checked_at = datetime.now(UTC)
    placement.operation_token = None
    placement.last_result = {
        "schema_version": 1,
        "status": "complete",
        "outcome": result.state.value,
        "target_fingerprint": dict(result.target_fingerprint),
    }
    await session.execute(
        sa_update(IssueStoryArc)
        .where(IssueStoryArc.id == context.membership_id)
        .values(
            sync_eligible=policy.synchronize,
            last_materialization_result={
                "schema_version": 1,
                "status": "complete",
                "outcome": result.state.value,
            },
        )
    )
    await session.commit()
    synchronized = StoryArcPlacementSyncResult(
        membership_id=context.membership_id,
        outcome=result.state.value,
        placement=_placement_view(placement),
    )
    if caller_cancelled:
        raise asyncio.CancelledError
    return synchronized


async def _persist_reference_failure(
    session: AsyncSession,
    membership_id: int,
    error: StoryArcPlacementError | StoryArcPlacementIntegrationError,
    *,
    placement_id: int | None = None,
    operation_token: str | None = None,
) -> None:
    failure = {
        "schema_version": 1,
        "status": "failed",
        "error_code": error.code,
        "error_category": _error_category(error),
        "message": str(error),
    }
    if placement_id is not None:
        placement = await session.get(StoryArcPlacement, placement_id)
        if placement is not None and placement.ownership is StoryArcPlacementOwnership.REFERENCED:
            await session.refresh(placement)
            if operation_token is not None and placement.operation_token != operation_token:
                await session.rollback()
                return
            previous = dict(placement.last_result or {})
            if isinstance(previous.get("target_fingerprint"), dict):
                failure["target_fingerprint"] = dict(previous["target_fingerprint"])
            placement.state = (
                StoryArcPlacementState.MISSING
                if error.code == "reference_target_missing"
                else StoryArcPlacementState.DRIFTED
            )
            placement.last_checked_at = datetime.now(UTC)
            placement.operation_token = None
            placement.last_result = failure
    membership = await session.get(IssueStoryArc, membership_id)
    if membership is not None:
        membership.last_materialization_result = failure
    await session.commit()


def _build_plan(
    context: _PlacementContext,
    policy: StoryArcPlacementPolicy,
    *,
    adopt_identical_existing: bool,
) -> StoryArcPlacementPlan:
    return StoryArcPlacementPlan(
        issue_story_arc_id=context.membership_id,
        library_file_id=context.library_file_id,
        canonical_path=Path(context.canonical_path) if context.canonical_path else None,
        destination_root=Path(policy.destination_root) if policy.destination_root else None,
        values=context.naming_values(),
        mode=_filesystem_mode(policy.mode),
        symlink_style=policy.symlink_style,
        folder_template=policy.folder_template,
        file_template=policy.file_template,
        adopt_identical_existing=adopt_identical_existing,
    )


def _filesystem_mode(mode: StoryArcPlacementPolicyMode) -> StoryArcPlacementMode:
    if mode is StoryArcPlacementPolicyMode.LOGICAL:
        return StoryArcPlacementMode.REFERENCE_ONLY
    return StoryArcPlacementMode(mode.value)


def _require_published_operation_token(
    placement: StoryArcPlacement,
    operation_token: str,
) -> None:
    """Authorize takeover only from the exact durable post-publish checkpoint."""
    last_result = dict(placement.last_result or {})
    target_fingerprint = last_result.get("target_fingerprint")
    if (
        not operation_token
        or placement.operation_token != operation_token
        or last_result.get("schema_version") != 1
        or last_result.get("status") != "published_pending_reconcile"
        or last_result.get("operation_token") != operation_token
        or not isinstance(target_fingerprint, dict)
        or not target_fingerprint
    ):
        raise StoryArcPlacementIntegrationError(
            "placement_published_operation_token_invalid",
            "Placement operation is not an exact abandoned published checkpoint",
            category="conflict",
        )


def _managed_evidence(
    placement: StoryArcPlacement,
) -> ManagedStoryArcPlacementEvidence | None:
    if placement.ownership is not StoryArcPlacementOwnership.MANAGED:
        return None
    target_fingerprint = dict(placement.last_result or {}).get("target_fingerprint")
    if not placement.source_fingerprint or not isinstance(target_fingerprint, dict):
        return None
    # Imported placements retain the import action id.  For normal sync, the
    # committed placement row itself is the durable ownership record, and its
    # positive id is used only as the filesystem service's ownership token.
    ownership_token = placement.creating_action_id or placement.id
    return ManagedStoryArcPlacementEvidence(
        issue_story_arc_id=placement.issue_story_arc_id,
        placement_path=Path(placement.placement_path),
        mode=placement.mode,
        ownership=placement.ownership,
        symlink_style=placement.symlink_style,
        source_fingerprint=dict(placement.source_fingerprint),
        target_fingerprint=dict(target_fingerprint),
        creating_action_id=ownership_token,
    )


def _managed_removal_evidence(
    placement: StoryArcPlacement,
) -> ManagedStoryArcPlacementEvidence | None:
    """Build removal-only evidence, using an empty fingerprint as absence proof.

    The filesystem removal service checks secure path absence before comparing
    fingerprints.  Thus ``{}`` can prove only that an absent target is safe to
    forget; any existing target necessarily mismatches and is preserved.
    """
    if placement.ownership is not StoryArcPlacementOwnership.MANAGED:
        return None
    raw_target_fingerprint = dict(placement.last_result or {}).get("target_fingerprint")
    if raw_target_fingerprint is None:
        target_fingerprint: dict[str, object] = {}
    elif isinstance(raw_target_fingerprint, dict):
        target_fingerprint = dict(raw_target_fingerprint)
    else:
        return None
    if not placement.source_fingerprint:
        return None
    ownership_token = placement.creating_action_id or placement.id
    return ManagedStoryArcPlacementEvidence(
        issue_story_arc_id=placement.issue_story_arc_id,
        placement_path=Path(placement.placement_path),
        mode=placement.mode,
        ownership=placement.ownership,
        symlink_style=placement.symlink_style,
        source_fingerprint=dict(placement.source_fingerprint),
        target_fingerprint=target_fingerprint,
        creating_action_id=ownership_token,
    )


def _preparation_snapshot(
    preparation: StoryArcPlacementPreparation,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "issue_story_arc_id": preparation.issue_story_arc_id,
        "placement_path": str(preparation.target_path),
        "mode": preparation.mode.value,
        "symlink_style": (
            preparation.symlink_style.value if preparation.symlink_style is not None else None
        ),
        "rendered_reading_order": preparation.rendered_reading_order,
        "source_fingerprint": dict(preparation.source_fingerprint),
        "destination_root_fingerprint": dict(preparation.destination_root_fingerprint),
    }


def _prepared_managed_evidence(
    placement: StoryArcPlacement,
) -> PreparedManagedStoryArcPlacementEvidence | None:
    last_result = dict(placement.last_result or {})
    if last_result.get("status") != "prepared":
        return None
    operation_token = last_result.get("operation_token")
    raw = last_result.get("prepared_evidence")
    if (
        not isinstance(operation_token, str)
        or not operation_token
        or operation_token != placement.operation_token
        or not isinstance(raw, dict)
    ):
        return None
    try:
        issue_story_arc_id = raw["issue_story_arc_id"]
        placement_path = raw["placement_path"]
        mode = raw["mode"]
        symlink_style = raw["symlink_style"]
        source_fingerprint = raw["source_fingerprint"]
        root_fingerprint = raw["destination_root_fingerprint"]
        if (
            isinstance(issue_story_arc_id, bool)
            or not isinstance(issue_story_arc_id, int)
            or not isinstance(placement_path, str)
            or not isinstance(mode, str)
            or (symlink_style is not None and not isinstance(symlink_style, str))
            or not isinstance(source_fingerprint, dict)
            or not isinstance(root_fingerprint, dict)
        ):
            raise ValueError
        parsed_mode = StoryArcPlacementMode(mode)
        parsed_style = StoryArcSymlinkStyle(symlink_style) if symlink_style is not None else None
    except (KeyError, TypeError, ValueError):
        return None
    if (
        issue_story_arc_id != placement.issue_story_arc_id
        or placement_path != placement.placement_path
        or parsed_mode is not placement.mode
        or parsed_style is not placement.symlink_style
        or source_fingerprint != dict(placement.source_fingerprint or {})
    ):
        return None
    return PreparedManagedStoryArcPlacementEvidence(
        issue_story_arc_id=issue_story_arc_id,
        placement_path=Path(placement_path),
        mode=parsed_mode,
        symlink_style=parsed_style,
        source_fingerprint=dict(source_fingerprint),
        destination_root_fingerprint=dict(root_fingerprint),
        operation_token=operation_token,
    )


async def _refresh_operation_lease(
    session: AsyncSession,
    *,
    placement_id: int,
    operation_token: str,
) -> None:
    result = await session.execute(
        sa_update(StoryArcPlacement)
        .where(
            StoryArcPlacement.id == placement_id,
            StoryArcPlacement.operation_token == operation_token,
        )
        .values(updated_at=datetime.now(UTC))
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        raise StoryArcPlacementIntegrationError(
            "placement_operation_superseded",
            "Story-arc placement operation lost its ownership lease",
            category="conflict",
        )
    await session.commit()


async def _persist_removal_failure(
    session: AsyncSession,
    *,
    placement_id: int,
    operation_token: str,
    error: StoryArcPlacementError | StoryArcPlacementIntegrationError,
) -> None:
    """Release one removal lease while retaining evidence needed for a safe retry."""
    session.expire_all()
    placement = await session.get(StoryArcPlacement, placement_id)
    if placement is None or placement.operation_token != operation_token:
        await session.rollback()
        return
    previous = dict(placement.last_result or {})
    failure = {
        **(
            {"target_fingerprint": dict(previous["target_fingerprint"])}
            if isinstance(previous.get("target_fingerprint"), dict)
            else {}
        ),
        "schema_version": 1,
        "status": "failed",
        "operation": "remove",
        "error_code": error.code,
        "error_category": _error_category(error),
        "message": str(error),
    }
    result = await session.execute(
        sa_update(StoryArcPlacement)
        .where(
            StoryArcPlacement.id == placement_id,
            StoryArcPlacement.operation_token == operation_token,
        )
        .values(
            state=(
                StoryArcPlacementState.DRIFTED
                if _error_category(error) in {"safety", "collision", "ownership"}
                else StoryArcPlacementState.FAILED
            ),
            last_result=failure,
            operation_token=None,
        )
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        return
    await session.execute(
        sa_update(IssueStoryArc)
        .where(IssueStoryArc.id == placement.issue_story_arc_id)
        .values(last_materialization_result=failure)
    )
    await session.commit()


async def _delete_removed_placement_checkpoint(
    session: AsyncSession,
    *,
    placement_id: int,
    membership_id: int,
    operation_token: str,
    artifact_removed: bool,
) -> None:
    """Atomically forget ownership only after the managed artifact is absent."""
    result = await session.execute(
        delete(StoryArcPlacement).where(
            StoryArcPlacement.id == placement_id,
            StoryArcPlacement.operation_token == operation_token,
            StoryArcPlacement.ownership == StoryArcPlacementOwnership.MANAGED,
        )
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        raise StoryArcPlacementIntegrationError(
            "placement_operation_superseded",
            "Story-arc placement removal lost its ownership lease",
            category="conflict",
        )
    await session.execute(
        sa_update(IssueStoryArc)
        .where(IssueStoryArc.id == membership_id)
        .values(
            sync_eligible=False,
            last_materialization_result={
                "schema_version": 1,
                "status": "placement_removed",
                "placement_id": placement_id,
                "artifact_removed": artifact_removed,
                "canonical_preserved": True,
            },
        )
    )
    await session.commit()


async def _persist_preflight_failure(
    session: AsyncSession,
    *,
    membership_id: int,
    placement_id: int | None,
    observed_operation_token: str | None,
    error: StoryArcPlacementError | StoryArcPlacementIntegrationError,
) -> None:
    category = _error_category(error)
    failure = {
        "schema_version": 1,
        "status": "failed",
        "error_code": error.code,
        "error_category": category,
        "message": str(error),
    }
    if placement_id is not None:
        session.expire_all()
        placement = await session.get(StoryArcPlacement, placement_id)
        if placement is not None:
            previous = dict(placement.last_result or {})
            if isinstance(previous.get("target_fingerprint"), dict):
                failure["target_fingerprint"] = dict(previous["target_fingerprint"])
            token_filter = (
                StoryArcPlacement.operation_token.is_(None)
                if observed_operation_token is None
                else StoryArcPlacement.operation_token == observed_operation_token
            )
            result = await session.execute(
                sa_update(StoryArcPlacement)
                .where(StoryArcPlacement.id == placement_id, token_filter)
                .values(
                    state=StoryArcPlacementState.DRIFTED,
                    last_result=failure,
                    operation_token=None,
                )
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                await session.rollback()
                return
    await session.execute(
        sa_update(IssueStoryArc)
        .where(IssueStoryArc.id == membership_id)
        .values(last_materialization_result=failure)
    )
    await session.commit()


async def _persist_failure(
    session: AsyncSession,
    *,
    membership_id: int,
    placement_id: int,
    operation_token: str,
    error: StoryArcPlacementError | StoryArcPlacementIntegrationError,
) -> None:
    session.expire_all()
    placement = await session.get(StoryArcPlacement, placement_id)
    if placement is None or placement.operation_token != operation_token:
        await session.rollback()
        return
    category = _error_category(error)
    previous = dict(placement.last_result or {})
    failure = {
        **(
            {"target_fingerprint": dict(previous["target_fingerprint"])}
            if isinstance(previous.get("target_fingerprint"), dict)
            else {}
        ),
        "schema_version": 1,
        "status": "failed",
        "error_code": error.code,
        "error_category": category,
        "message": str(error),
        "operation_token": operation_token,
    }
    if (
        category == "collision"
        and placement.ownership is StoryArcPlacementOwnership.MANAGED
        and previous.get("status") == "prepared"
        and not isinstance(previous.get("target_fingerprint"), dict)
        and placement.creating_action_id is None
    ):
        result = await session.execute(
            delete(StoryArcPlacement).where(
                StoryArcPlacement.id == placement_id,
                StoryArcPlacement.operation_token == operation_token,
            )
        )
    else:
        result = await session.execute(
            sa_update(StoryArcPlacement)
            .where(
                StoryArcPlacement.id == placement_id,
                StoryArcPlacement.operation_token == operation_token,
            )
            .values(
                state=(
                    StoryArcPlacementState.DRIFTED
                    if category == "collision"
                    else StoryArcPlacementState.FAILED
                ),
                last_result=failure,
                operation_token=None,
            )
        )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        return
    await session.execute(
        sa_update(IssueStoryArc)
        .where(IssueStoryArc.id == membership_id)
        .values(last_materialization_result=failure)
    )
    await session.commit()


async def _persist_published_checkpoint(
    session: AsyncSession,
    *,
    context: _PlacementContext,
    policy: StoryArcPlacementPolicy,
    prepared_placement_id: int,
    operation_token: str,
    result: StoryArcPlacementResult,
) -> None:
    """Durably bridge filesystem publish and final user-visible reconciliation."""
    if result.target_path is None:
        raise StoryArcPlacementIntegrationError(
            "placement_target_missing",
            "Placement publish returned no durable target evidence",
            category="safety",
        )
    checkpoint = {
        "schema_version": 1,
        "status": "published_pending_reconcile",
        "outcome": result.state.value,
        "operation_token": operation_token,
        "target_fingerprint": dict(result.target_fingerprint),
    }
    update_result = await session.execute(
        sa_update(StoryArcPlacement)
        .where(
            StoryArcPlacement.id == prepared_placement_id,
            StoryArcPlacement.operation_token == operation_token,
        )
        .values(
            library_file_id=result.library_file_id,
            library_root_id=policy.target_library_root_id,
            placement_path=str(result.target_path),
            mode=result.mode,
            ownership=result.ownership,
            symlink_style=result.symlink_style,
            rendered_reading_order=result.rendered_reading_order,
            policy_schema_version=STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION,
            source_fingerprint=dict(result.source_fingerprint),
            last_checked_at=datetime.now(UTC),
            last_result=checkpoint,
        )
    )
    if update_result.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        raise StoryArcPlacementIntegrationError(
            "placement_operation_superseded",
            "Story-arc placement operation was superseded before checkpoint",
            category="conflict",
        )
    await session.execute(
        sa_update(IssueStoryArc)
        .where(IssueStoryArc.id == context.membership_id)
        .values(
            last_materialization_result={
                "schema_version": 1,
                "status": "published_pending_reconcile",
                "outcome": result.state.value,
                "placement_id": prepared_placement_id,
            }
        )
    )
    await session.commit()


async def _reconcile_result(
    session: AsyncSession,
    *,
    context: _PlacementContext,
    policy: StoryArcPlacementPolicy,
    prepared_placement_id: int,
    operation_token: str,
    result: StoryArcPlacementResult,
) -> StoryArcPlacementSyncResult:
    target_path = result.target_path
    if target_path is None:
        raise StoryArcPlacementIntegrationError(
            "placement_target_missing",
            "Managed placement execution returned no destination",
            category="safety",
        )
    session.expire_all()
    fence_code: str | None = None
    current_arc = await session.get(StoryArc, context.story_arc_id)
    if (
        current_arc is None
        or current_arc.lifecycle is not StoryArcLifecycle.ACTIVE
        or _policy_from_arc(current_arc) != policy
    ):
        fence_code = "placement_policy_changed"
    else:
        try:
            current_context = await _load_one_context(
                session,
                context.story_arc_id,
                context.membership_id,
            )
        except StoryArcPlacementIntegrationError:
            fence_code = "canonical_context_changed"
        else:
            if current_context != context:
                fence_code = "canonical_context_changed"
    if fence_code is None and not await _policy_root_is_available(session, policy):
        fence_code = "target_library_root_unavailable"

    final_status = "complete" if fence_code is None else "drifted"
    final_result = {
        "schema_version": 1,
        "status": final_status,
        "outcome": result.state.value,
        "operation_token": operation_token,
        "target_fingerprint": dict(result.target_fingerprint),
    }
    if fence_code is not None:
        final_result["error_code"] = fence_code
    update_result = await session.execute(
        sa_update(StoryArcPlacement)
        .where(
            StoryArcPlacement.id == prepared_placement_id,
            StoryArcPlacement.operation_token == operation_token,
        )
        .values(
            library_file_id=result.library_file_id,
            library_root_id=policy.target_library_root_id,
            placement_path=str(target_path),
            mode=result.mode,
            ownership=result.ownership,
            symlink_style=result.symlink_style,
            rendered_reading_order=result.rendered_reading_order,
            policy_schema_version=STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION,
            source_fingerprint=dict(result.source_fingerprint),
            state=(
                StoryArcPlacementState.CURRENT
                if fence_code is None
                else StoryArcPlacementState.DRIFTED
            ),
            last_checked_at=datetime.now(UTC),
            last_result=final_result,
            operation_token=None,
        )
    )
    if update_result.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        raise StoryArcPlacementIntegrationError(
            "placement_operation_superseded",
            "Story-arc placement operation was superseded before reconciliation",
            category="conflict",
        )
    await session.execute(
        sa_update(IssueStoryArc)
        .where(IssueStoryArc.id == context.membership_id)
        .values(
            sync_eligible=(policy.synchronize if fence_code is None else False),
            last_materialization_result={
                "schema_version": 1,
                "status": final_status,
                "outcome": result.state.value,
                "placement_id": prepared_placement_id,
                **({"error_code": fence_code} if fence_code is not None else {}),
            },
        )
    )
    await session.commit()
    session.expire_all()
    placement = await session.get(StoryArcPlacement, prepared_placement_id)
    if placement is None:
        raise StoryArcPlacementIntegrationError(
            "prepared_placement_missing",
            "Prepared story-arc placement disappeared after reconciliation",
            category="conflict",
        )
    if fence_code is not None:
        raise StoryArcPlacementIntegrationError(
            fence_code,
            "Story-arc placement was published but its policy or canonical context changed",
            category="conflict",
        )
    return StoryArcPlacementSyncResult(
        membership_id=context.membership_id,
        outcome=result.state.value,
        placement=_placement_view(placement),
    )


async def _require_placement(
    session: AsyncSession,
    story_arc_id: int,
    placement_id: int,
) -> StoryArcPlacement:
    placement = await session.scalar(
        select(StoryArcPlacement)
        .join(IssueStoryArc, StoryArcPlacement.issue_story_arc_id == IssueStoryArc.id)
        .where(
            StoryArcPlacement.id == placement_id,
            IssueStoryArc.story_arc_id == story_arc_id,
        )
    )
    if placement is None:
        raise _not_found("placement_not_found", "Story-arc placement was not found")
    return placement


def _placement_view(row: StoryArcPlacement) -> StoryArcPlacementView:
    last_result = dict(row.last_result or {})
    target_fingerprint = last_result.get("target_fingerprint")
    return StoryArcPlacementView(
        id=row.id,
        issue_story_arc_id=row.issue_story_arc_id,
        library_file_id=row.library_file_id,
        library_root_id=row.library_root_id,
        placement_path=row.placement_path,
        mode=row.mode,
        ownership=row.ownership,
        symlink_style=row.symlink_style,
        rendered_reading_order=row.rendered_reading_order,
        policy_schema_version=row.policy_schema_version,
        source_fingerprint=dict(row.source_fingerprint or {}),
        target_fingerprint=(
            dict(target_fingerprint) if isinstance(target_fingerprint, dict) else {}
        ),
        state=row.state,
        last_result=last_result,
        last_checked_at=row.last_checked_at,
    )


def _translate_filesystem_error(exc: StoryArcPlacementError) -> StoryArcPlacementIntegrationError:
    return StoryArcPlacementIntegrationError(
        exc.code,
        str(exc),
        category=_error_category(exc),
    )


def _error_category(
    exc: StoryArcPlacementError | StoryArcPlacementIntegrationError,
) -> str:
    if isinstance(exc, StoryArcPlacementIntegrationError):
        return exc.category
    if isinstance(exc, StoryArcPlacementCollisionError):
        return "collision"
    if isinstance(exc, StoryArcPlacementOwnershipError):
        return "ownership"
    if isinstance(exc, StoryArcPlacementCancellationError):
        return "cancelled"
    if isinstance(exc, StoryArcPlacementSafetyError):
        return "safety"
    return "operation"


def _bounded_page(limit: int, offset: int) -> tuple[int, int]:
    if isinstance(limit, bool) or not 1 <= limit <= MAX_STORY_ARC_PLACEMENT_PAGE_SIZE:
        raise StoryArcPlacementIntegrationError(
            "invalid_page_limit",
            f"Placement page limit must be from 1 to {MAX_STORY_ARC_PLACEMENT_PAGE_SIZE}",
        )
    if isinstance(offset, bool) or offset < 0:
        raise StoryArcPlacementIntegrationError(
            "invalid_page_offset",
            "Placement page offset must be non-negative",
        )
    return limit, offset


def _not_found(code: str, message: str) -> StoryArcPlacementIntegrationError:
    return StoryArcPlacementIntegrationError(code, message, category="not_found")
