"""Read-only preparation for managed Story Arc placement-policy migration.

The current mutation service deliberately refuses a destination-policy change
while managed placements exist.  This module supplies the bounded work that
must precede any future executor: a complete, signed preview and an exact,
actor-bound confirmation check.  It never mutates a Story Arc, placement,
canonical file, or referenced artifact.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import stat
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func, or_, select

from pullbox.core.config_resolver import get_application_secret
from pullbox.models.import_job import (
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
)
from pullbox.models.story_arc_sync import StoryArcSyncWork, StoryArcSyncWorkState
from pullbox.services.story_arc_placement_integration import (
    STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION,
    StoryArcPlacementIntegrationError,
    StoryArcPlacementPolicy,
    StoryArcPlacementPolicyInput,
    StoryArcPlacementPolicyMode,
    _placement_policy_shape,
    _PlacementContext,
    _policy_from_arc,
    _rendered_target_path,
    validate_story_arc_placement_policy_input,
)
from pullbox.services.story_arc_placement_preview import StoryArcCollisionKind
from pullbox.services.story_arc_placement_service import (
    StoryArcPlacementInspection,
    StoryArcPlacementInspectionEvidence,
    StoryArcPlacementInspectionState,
    StoryArcPlacementPlan,
    inspect_story_arc_placement,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy.engine import Row
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import Select

STORY_ARC_POLICY_MIGRATION_CONFIRMATION = "CHANGE STORY ARC PLACEMENT POLICY"

_TOKEN_SALT = "pullbox-story-arc-policy-migration-v1"
_TOKEN_SCHEMA_VERSION = 1
_TOKEN_MAX_AGE_SECONDS = 15 * 60
_SCAN_PAGE_SIZE = 100
_MAX_RESPONSE_PAGE_SIZE = 100
_ACTIVE_PLACEMENT_STATUSES = (
    "prepared",
    "published_pending_reconcile",
    "reference_validation_prepared",
    "remove_prepared",
    "rename_prepared",
    "rename_recovery_required",
)
_ACTIVE_SYNC_STATES = (
    StoryArcSyncWorkState.QUEUED,
    StoryArcSyncWorkState.RUNNING,
    StoryArcSyncWorkState.RETRY_WAIT,
    StoryArcSyncWorkState.FAILED,
)
_TERMINAL_SYNC_STATES = (
    StoryArcSyncWorkState.COMPLETED,
    StoryArcSyncWorkState.CANCELLED,
)
_SETTLED_ROLLBACK_STATUSES = frozenset(
    {
        "cancelled_before_publish",
        "managed_placement_removed",
        "referenced_placement_detached",
    }
)
_UNCHANGED_MANAGED_STATUSES = frozenset({"complete", "rename_cancelled", "rename_failed"})
_MANAGED_POLICY_MODES = frozenset(
    {
        StoryArcPlacementPolicyMode.COPY,
        StoryArcPlacementPolicyMode.HARDLINK,
        StoryArcPlacementPolicyMode.SYMLINK,
    }
)


class _DigestWriter(Protocol):
    def update(self, value: bytes, /) -> None: ...


class StoryArcPolicyMigrationError(StoryArcPlacementIntegrationError):
    """Categorized failure at the read-only migration boundary."""


@dataclass(frozen=True, slots=True)
class StoryArcPolicyMigrationPreviewItem:
    """One old/new consequence from a complete preview, never an ORM row."""

    placement_id: int
    membership_id: int
    ownership: str
    action: str
    old_mode: str
    new_mode: str
    old_path: str
    new_path: str | None
    collision: str
    blocked: bool
    reason: str | None
    required_bytes: int


@dataclass(frozen=True, slots=True)
class StoryArcPolicyMigrationPreview:
    """Complete counts and digest plus one bounded keyset item page."""

    story_arc_id: int
    expected_revision: int
    current_policy: StoryArcPlacementPolicy
    proposed_policy: StoryArcPlacementPolicy
    scope_digest: str
    preview_token: str
    required_confirmation: str
    total_placement_count: int
    managed_migrate_count: int
    managed_remove_count: int
    managed_unchanged_count: int
    referenced_preserved_count: int
    collision_count: int
    blocked_count: int
    required_bytes: int
    available_bytes: int | None
    global_block_codes: tuple[str, ...]
    items: tuple[StoryArcPolicyMigrationPreviewItem, ...]
    limit: int
    after_placement_id: int
    next_cursor: int | None
    has_more: bool
    requires_confirmation: bool = True
    execution_supported: bool = False
    filesystem_mutated: bool = False


@dataclass(frozen=True, slots=True)
class StoryArcPolicyMigrationConfirmation:
    """Exact confirmation result without claiming unavailable execution."""

    story_arc_id: int
    expected_revision: int
    scope_digest: str
    confirmed: bool = True
    ready_for_execution: bool = False
    execution_supported: bool = False
    mutation_performed: bool = False
    policy_update_block_code: str = "managed_policy_change_requires_migration"


@dataclass(frozen=True, slots=True)
class _MembershipScope:
    context: _PlacementContext
    source_ordinal: int
    resolution_state: str
    sync_eligible: bool
    membership_updated_at: datetime
    membership_evidence: dict[str, object]
    materialization_result: dict[str, object]
    issue_updated_at: datetime | None
    series_id: int | None
    series_updated_at: datetime | None
    canonical_id: int | None
    canonical_path: str | None
    canonical_size: int | None
    canonical_format: str | None
    canonical_hash: str | None
    canonical_modified_at: datetime | None
    canonical_updated_at: datetime | None
    canonical_library_root_id: int | None
    canonical_storage_mode: str | None
    canonical_source_signature: dict[str, object]

    def digest_record(self) -> dict[str, object]:
        context = self.context
        return {
            "membership_id": context.membership_id,
            "story_arc_id": context.story_arc_id,
            "sequence_number": context.sequence_number,
            "source_ordinal": self.source_ordinal,
            "resolution_state": self.resolution_state,
            "sync_eligible": self.sync_eligible,
            "membership_updated_at": self.membership_updated_at,
            "membership_evidence": self.membership_evidence,
            "materialization_result": self.materialization_result,
            "issue_id": context.issue_id,
            "issue_number_text": context.issue_number_text,
            "issue_title": context.issue_title,
            "issue_updated_at": self.issue_updated_at,
            "story_arc_name": context.story_arc_name,
            "series_id": self.series_id,
            "series_name": context.series_name,
            "series_start_year": context.series_start_year,
            "series_end_year": context.series_end_year,
            "series_updated_at": self.series_updated_at,
            "publisher_name": context.publisher_name,
            "year": context.year,
            "canonical_id": self.canonical_id,
            "canonical_path": self.canonical_path,
            "canonical_size": self.canonical_size,
            "canonical_format": self.canonical_format,
            "canonical_hash": self.canonical_hash,
            "canonical_modified_at": self.canonical_modified_at,
            "canonical_updated_at": self.canonical_updated_at,
            "canonical_library_root_id": self.canonical_library_root_id,
            "canonical_storage_mode": self.canonical_storage_mode,
            "canonical_source_signature": self.canonical_source_signature,
        }


@dataclass(frozen=True, slots=True)
class _PlacementScope:
    id: int
    membership_id: int
    library_file_id: int | None
    library_root_id: int | None
    placement_path: str
    mode: StoryArcPlacementMode
    ownership: StoryArcPlacementOwnership
    symlink_style: str | None
    source_kind: str
    creating_action_id: int | None
    rendered_reading_order: int | None
    policy_schema_version: int | None
    operation_token: str | None
    source_fingerprint: dict[str, object]
    target_fingerprint: dict[str, object]
    state: StoryArcPlacementState
    last_result: dict[str, object]
    updated_at: datetime
    context: _MembershipScope

    def digest_record(self) -> dict[str, object]:
        return {
            "placement_id": self.id,
            "membership_id": self.membership_id,
            "library_file_id": self.library_file_id,
            "library_root_id": self.library_root_id,
            "placement_path": self.placement_path,
            "mode": self.mode.value,
            "ownership": self.ownership.value,
            "symlink_style": self.symlink_style,
            "source_kind": self.source_kind,
            "creating_action_id": self.creating_action_id,
            "rendered_reading_order": self.rendered_reading_order,
            "policy_schema_version": self.policy_schema_version,
            "operation_token": self.operation_token,
            "source_fingerprint": self.source_fingerprint,
            "target_fingerprint": self.target_fingerprint,
            "state": self.state.value,
            "last_result": self.last_result,
            "updated_at": self.updated_at,
        }

    def inspection_evidence(self) -> StoryArcPlacementInspectionEvidence:
        return StoryArcPlacementInspectionEvidence(
            placement_path=Path(self.placement_path),
            mode=self.mode,
            ownership=self.ownership,
            symlink_style=self.symlink_style,
            source_fingerprint=dict(self.source_fingerprint),
            target_fingerprint=dict(self.target_fingerprint),
        )


@dataclass(frozen=True, slots=True)
class _OccupiedPlacement:
    id: int
    ownership: StoryArcPlacementOwnership


@dataclass(frozen=True, slots=True)
class _ClassifiedPlacement:
    item: StoryArcPolicyMigrationPreviewItem
    digest_record: dict[str, object]


@dataclass(frozen=True, slots=True)
class _PreviewCounts:
    total: int = 0
    managed_migrate: int = 0
    managed_remove: int = 0
    managed_unchanged: int = 0
    referenced_preserved: int = 0
    collisions: int = 0
    blocked: int = 0
    required_bytes: int = 0

    def add(self, item: StoryArcPolicyMigrationPreviewItem) -> _PreviewCounts:
        return _PreviewCounts(
            total=self.total + 1,
            managed_migrate=self.managed_migrate + (item.action == "migrate_managed"),
            managed_remove=self.managed_remove + (item.action == "remove_managed"),
            managed_unchanged=self.managed_unchanged + (item.action == "managed_unchanged"),
            referenced_preserved=self.referenced_preserved + (item.action == "preserve_referenced"),
            collisions=self.collisions + (item.collision != "none"),
            blocked=self.blocked + item.blocked,
            required_bytes=self.required_bytes + item.required_bytes,
        )

    def token_record(self) -> dict[str, int]:
        return {
            "total": self.total,
            "managed_migrate": self.managed_migrate,
            "managed_remove": self.managed_remove,
            "managed_unchanged": self.managed_unchanged,
            "referenced_preserved": self.referenced_preserved,
            "collisions": self.collisions,
            "blocked": self.blocked,
            "required_bytes": self.required_bytes,
        }


@dataclass(frozen=True, slots=True)
class _SignedPreview:
    actor_id: int
    story_arc_id: int
    expected_revision: int
    current_policy: dict[str, object]
    proposed_policy: dict[str, object]
    scope_digest: str
    counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class _PolicyRootFingerprint:
    resolved_path: str | None
    device: int | None
    inode: int | None

    def digest_record(self) -> dict[str, object]:
        return {
            "resolved_path": self.resolved_path,
            "device": self.device,
            "inode": self.inode,
        }


@dataclass(frozen=True, slots=True)
class _TerminalSyncWorkScope:
    id: int
    last_result: object
    origin_import_job_id: int | None
    origin_import_action_id: int | None
    issue_story_arc_id: int
    desired_generation: str
    job_id: int | None
    job_status: ImportJobStatus | None
    action_id: int | None
    action_import_job_id: int | None
    action_status: ImportJobActionStatus | None


class StoryArcPolicyMigrationService:
    """Build and revalidate a complete migration preview without mutation."""

    async def preview_policy_change(
        self,
        session: AsyncSession,
        story_arc_id: int,
        *,
        actor_id: int,
        expected_revision: int,
        proposal: StoryArcPlacementPolicyInput,
        limit: int = 50,
        after_placement_id: int = 0,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> StoryArcPolicyMigrationPreview:
        """Return complete counts/digest and one bounded keyset page."""
        limit, after_placement_id = _bounded_page(limit, after_placement_id)
        current, proposed, arc_name = await self._validate_boundary(
            session,
            story_arc_id=story_arc_id,
            actor_id=actor_id,
            expected_revision=expected_revision,
            proposal=proposal,
            cancellation_requested=cancellation_requested,
        )
        await session.rollback()
        current_root_fingerprint = await _validate_policy_root(current, role="current")
        proposed_root_fingerprint = await _validate_policy_root(proposed, role="proposed")

        first_membership_digest = await _membership_scope_digest(
            session,
            story_arc_id=story_arc_id,
            arc_name=arc_name,
            cancellation_requested=cancellation_requested,
        )
        (
            first_placement_digest,
            exact_target_counts,
            folded_target_counts,
        ) = await _placement_target_index(
            session,
            story_arc_id=story_arc_id,
            arc_name=arc_name,
            proposed=proposed,
            cancellation_requested=cancellation_requested,
        )
        (
            second_placement_digest,
            plan_digest,
            counts,
            page_items,
            next_cursor,
            has_more,
        ) = await _classify_placements(
            session,
            story_arc_id=story_arc_id,
            arc_name=arc_name,
            current=current,
            proposed=proposed,
            exact_target_counts=exact_target_counts,
            folded_target_counts=folded_target_counts,
            limit=limit,
            after_placement_id=after_placement_id,
            cancellation_requested=cancellation_requested,
        )
        second_membership_digest = await _membership_scope_digest(
            session,
            story_arc_id=story_arc_id,
            arc_name=arc_name,
            cancellation_requested=cancellation_requested,
        )
        if (
            first_membership_digest != second_membership_digest
            or first_placement_digest != second_placement_digest
        ):
            raise StoryArcPolicyMigrationError(
                "migration_scope_changed",
                "Story Arc placement scope changed while the preview was generated",
                category="conflict",
            )
        await self._assert_boundary_stable(
            session,
            story_arc_id=story_arc_id,
            expected_revision=expected_revision,
            current=current,
            cancellation_requested=cancellation_requested,
        )
        await session.rollback()
        if (
            await _validate_policy_root(current, role="current") != current_root_fingerprint
            or await _validate_policy_root(proposed, role="proposed") != proposed_root_fingerprint
        ):
            raise StoryArcPolicyMigrationError(
                "migration_scope_changed",
                "Story Arc destination root changed while the preview was generated",
                category="conflict",
            )

        global_block_codes: list[str] = []
        available_bytes: int | None = None
        if proposed.mode in _MANAGED_POLICY_MODES and proposed.destination_root is not None:
            try:
                available_bytes = await asyncio.to_thread(
                    lambda: shutil.disk_usage(proposed.destination_root or "").free
                )
            except OSError:
                global_block_codes.append("destination_capacity_unavailable")
            else:
                if counts.required_bytes > available_bytes:
                    global_block_codes.append("insufficient_space")

        scope_digest = _digest_record(
            {
                "schema_version": 1,
                "story_arc_id": story_arc_id,
                "expected_revision": expected_revision,
                "current_policy": _policy_token_record(current),
                "proposed_policy": _policy_token_record(proposed),
                "current_root_fingerprint": current_root_fingerprint.digest_record(),
                "proposed_root_fingerprint": proposed_root_fingerprint.digest_record(),
                "membership_digest": second_membership_digest,
                "placement_digest": second_placement_digest,
                "plan_digest": plan_digest,
                "global_block_codes": global_block_codes,
            }
        )
        effective_counts = _PreviewCounts(
            total=counts.total,
            managed_migrate=counts.managed_migrate,
            managed_remove=counts.managed_remove,
            managed_unchanged=counts.managed_unchanged,
            referenced_preserved=counts.referenced_preserved,
            collisions=counts.collisions,
            blocked=counts.blocked,
            required_bytes=counts.required_bytes,
        )
        signed = _SignedPreview(
            actor_id=actor_id,
            story_arc_id=story_arc_id,
            expected_revision=expected_revision,
            current_policy=_policy_token_record(current),
            proposed_policy=_policy_token_record(proposed),
            scope_digest=scope_digest,
            counts=effective_counts.token_record(),
        )
        token = self._serializer().dumps(_signed_preview_payload(signed))
        await session.rollback()
        return StoryArcPolicyMigrationPreview(
            story_arc_id=story_arc_id,
            expected_revision=expected_revision,
            current_policy=current,
            proposed_policy=proposed,
            scope_digest=scope_digest,
            preview_token=token,
            required_confirmation=STORY_ARC_POLICY_MIGRATION_CONFIRMATION,
            total_placement_count=effective_counts.total,
            managed_migrate_count=effective_counts.managed_migrate,
            managed_remove_count=effective_counts.managed_remove,
            managed_unchanged_count=effective_counts.managed_unchanged,
            referenced_preserved_count=effective_counts.referenced_preserved,
            collision_count=effective_counts.collisions,
            blocked_count=effective_counts.blocked,
            required_bytes=effective_counts.required_bytes,
            available_bytes=available_bytes,
            global_block_codes=tuple(global_block_codes),
            items=page_items,
            limit=limit,
            after_placement_id=after_placement_id,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    async def prepare_confirmation(
        self,
        session: AsyncSession,
        story_arc_id: int,
        *,
        actor_id: int,
        expected_revision: int,
        proposal: StoryArcPlacementPolicyInput,
        preview_token: str,
        confirmation: str,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> StoryArcPolicyMigrationConfirmation:
        """Validate exact signed intent and current scope, still without mutation."""
        if confirmation != STORY_ARC_POLICY_MIGRATION_CONFIRMATION:
            raise StoryArcPolicyMigrationError(
                "confirmation_required",
                f'Type exactly "{STORY_ARC_POLICY_MIGRATION_CONFIRMATION}" to continue',
            )
        signed = self._decode_preview(preview_token)
        if (
            signed.actor_id != actor_id
            or signed.story_arc_id != story_arc_id
            or signed.expected_revision != expected_revision
        ):
            raise StoryArcPolicyMigrationError(
                "invalid_preview_token",
                "The migration preview does not match this actor or Story Arc policy change",
            )
        refreshed = await self.preview_policy_change(
            session,
            story_arc_id,
            actor_id=actor_id,
            expected_revision=expected_revision,
            proposal=proposal,
            limit=1,
            after_placement_id=0,
            cancellation_requested=cancellation_requested,
        )
        if (
            signed.current_policy != _policy_token_record(refreshed.current_policy)
            or signed.proposed_policy != _policy_token_record(refreshed.proposed_policy)
            or signed.scope_digest != refreshed.scope_digest
            or signed.counts != _preview_counts_record(refreshed)
        ):
            raise StoryArcPolicyMigrationError(
                "migration_preview_stale",
                (
                    "Story Arc placements, membership, canonical files, or policy "
                    "changed; preview again"
                ),
                category="conflict",
            )
        if refreshed.blocked_count or refreshed.global_block_codes:
            raise StoryArcPolicyMigrationError(
                "policy_migration_blocked",
                "Story Arc policy migration has blocked or colliding placements",
                category="collision",
            )
        await session.rollback()
        return StoryArcPolicyMigrationConfirmation(
            story_arc_id=story_arc_id,
            expected_revision=expected_revision,
            scope_digest=refreshed.scope_digest,
        )

    async def _validate_boundary(
        self,
        session: AsyncSession,
        *,
        story_arc_id: int,
        actor_id: int,
        expected_revision: int,
        proposal: StoryArcPlacementPolicyInput,
        cancellation_requested: Callable[[], bool] | None,
    ) -> tuple[StoryArcPlacementPolicy, StoryArcPlacementPolicy, str]:
        if not _is_positive_int(actor_id):
            raise StoryArcPolicyMigrationError("invalid_actor", "Actor identity is required")
        if not _is_positive_int(expected_revision):
            raise StoryArcPolicyMigrationError(
                "invalid_revision", "Expected Story Arc revision must be a positive integer"
            )
        arc = await session.get(StoryArc, story_arc_id)
        if arc is None:
            raise StoryArcPolicyMigrationError(
                "story_arc_not_found", "Story arc was not found", category="not_found"
            )
        if arc.lifecycle is StoryArcLifecycle.ARCHIVED:
            raise StoryArcPolicyMigrationError(
                "story_arc_archived", "Archived story arcs cannot change placement policy"
            )
        if arc.revision != expected_revision:
            raise StoryArcPolicyMigrationError(
                "revision_conflict",
                "Story Arc changed before migration preview",
                category="conflict",
            )
        current = _policy_from_arc(arc)
        if not current.configured:
            raise StoryArcPolicyMigrationError(
                "placement_policy_not_configured",
                "Configure the Story Arc placement policy before migrating it",
            )
        if current.target_library_root_id is None or not bool(
            await session.scalar(
                select(LibraryRoot.enabled).where(LibraryRoot.id == current.target_library_root_id)
            )
        ):
            raise StoryArcPolicyMigrationError(
                "current_destination_root_unavailable",
                "The current Story Arc destination root is unavailable",
                category="safety",
            )
        proposed = await validate_story_arc_placement_policy_input(
            session,
            proposal,
            revision=arc.revision,
        )
        if _placement_policy_shape(current) == _placement_policy_shape(proposed):
            raise StoryArcPolicyMigrationError(
                "managed_policy_migration_not_required",
                "This policy change does not alter managed placement destinations",
            )
        managed_id = await session.scalar(
            select(StoryArcPlacement.id)
            .join(IssueStoryArc, StoryArcPlacement.issue_story_arc_id == IssueStoryArc.id)
            .where(
                IssueStoryArc.story_arc_id == story_arc_id,
                StoryArcPlacement.ownership == StoryArcPlacementOwnership.MANAGED,
            )
            .order_by(StoryArcPlacement.id)
            .limit(1)
        )
        if managed_id is None:
            raise StoryArcPolicyMigrationError(
                "managed_policy_migration_not_required",
                "No managed Story Arc placements require migration",
            )
        if await session.scalar(_active_operation_statement(story_arc_id)) is not None:
            raise StoryArcPolicyMigrationError(
                "placement_operation_recovery_pending",
                "A Story Arc placement operation or recovery must finish before policy migration",
                category="conflict",
            )
        if await _active_sync_recovery_exists(
            session,
            story_arc_id=story_arc_id,
            cancellation_requested=cancellation_requested,
        ):
            raise StoryArcPolicyMigrationError(
                "placement_sync_work_pending",
                "Story Arc placement synchronization or recovery must finish before migration",
                category="conflict",
            )
        return current, proposed, arc.name

    async def _assert_boundary_stable(
        self,
        session: AsyncSession,
        *,
        story_arc_id: int,
        expected_revision: int,
        current: StoryArcPlacementPolicy,
        cancellation_requested: Callable[[], bool] | None,
    ) -> None:
        session.expire_all()
        arc = await session.get(StoryArc, story_arc_id)
        if (
            arc is None
            or arc.lifecycle is not StoryArcLifecycle.ACTIVE
            or arc.revision != expected_revision
            or _policy_from_arc(arc) != current
        ):
            raise StoryArcPolicyMigrationError(
                "migration_scope_changed",
                "Story Arc policy changed while the migration preview was generated",
                category="conflict",
            )
        if await session.scalar(_active_operation_statement(story_arc_id)) is not None:
            raise StoryArcPolicyMigrationError(
                "placement_operation_recovery_pending",
                "A Story Arc placement operation began during migration preview",
                category="conflict",
            )
        if await _active_sync_recovery_exists(
            session,
            story_arc_id=story_arc_id,
            cancellation_requested=cancellation_requested,
        ):
            raise StoryArcPolicyMigrationError(
                "placement_sync_work_pending",
                "Story Arc placement synchronization began during migration preview",
                category="conflict",
            )

    def _decode_preview(self, token: str) -> _SignedPreview:
        if not token:
            raise StoryArcPolicyMigrationError(
                "confirmation_required", "A signed migration preview must be confirmed"
            )
        try:
            raw = self._serializer().loads(token, max_age=_TOKEN_MAX_AGE_SECONDS)
        except SignatureExpired as exc:
            raise StoryArcPolicyMigrationError(
                "migration_preview_expired",
                "The migration preview expired; generate a new preview",
            ) from exc
        except BadSignature as exc:
            raise StoryArcPolicyMigrationError(
                "invalid_preview_token", "The migration preview token is invalid"
            ) from exc
        try:
            return _signed_preview_from_payload(raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise StoryArcPolicyMigrationError(
                "invalid_preview_token", "The migration preview token is invalid"
            ) from exc

    @staticmethod
    def _serializer() -> URLSafeTimedSerializer:
        return URLSafeTimedSerializer(get_application_secret(), salt=_TOKEN_SALT)


def _membership_scope_statement(
    story_arc_id: int,
    *,
    after_membership_id: int,
    limit: int,
) -> Select[tuple[IssueStoryArc, Issue, Series, Publisher]]:
    """Portable, bounded keyset query used by every membership digest pass."""
    return (
        select(IssueStoryArc, Issue, Series, Publisher)
        .outerjoin(Issue, IssueStoryArc.issue_id == Issue.id)
        .outerjoin(Series, Issue.series_id == Series.id)
        .outerjoin(Publisher, Series.publisher_id == Publisher.id)
        .where(
            IssueStoryArc.story_arc_id == story_arc_id,
            IssueStoryArc.id > after_membership_id,
        )
        .order_by(IssueStoryArc.id)
        .limit(limit)
    )


def _placement_scope_statement(
    story_arc_id: int,
    *,
    after_placement_id: int,
    limit: int,
) -> Select[tuple[StoryArcPlacement]]:
    """Portable, bounded keyset query for the exact placement scope."""
    return (
        select(StoryArcPlacement)
        .join(IssueStoryArc, StoryArcPlacement.issue_story_arc_id == IssueStoryArc.id)
        .where(
            IssueStoryArc.story_arc_id == story_arc_id,
            StoryArcPlacement.id > after_placement_id,
        )
        .order_by(StoryArcPlacement.id)
        .limit(limit)
    )


def _active_operation_statement(story_arc_id: int) -> Select[tuple[int]]:
    """Fail closed on both durable tokens and malformed recovery status rows."""
    return (
        select(StoryArcPlacement.id)
        .join(IssueStoryArc, StoryArcPlacement.issue_story_arc_id == IssueStoryArc.id)
        .where(
            IssueStoryArc.story_arc_id == story_arc_id,
            or_(
                StoryArcPlacement.operation_token.is_not(None),
                StoryArcPlacement.last_result["status"].as_string().in_(_ACTIVE_PLACEMENT_STATUSES),
            ),
        )
        .order_by(StoryArcPlacement.id)
        .limit(1)
    )


def _active_sync_work_statement(story_arc_id: int) -> Select[tuple[int]]:
    return (
        select(StoryArcSyncWork.id)
        .join(IssueStoryArc, StoryArcSyncWork.issue_story_arc_id == IssueStoryArc.id)
        .where(
            IssueStoryArc.story_arc_id == story_arc_id,
            or_(
                StoryArcSyncWork.state.in_(_ACTIVE_SYNC_STATES),
                StoryArcSyncWork.claim_token.is_not(None),
            ),
        )
        .order_by(StoryArcSyncWork.id)
        .limit(1)
    )


def _terminal_sync_work_statement(
    story_arc_id: int,
    *,
    after_work_id: int,
    limit: int,
) -> Select[tuple[object, ...]]:
    """Bound terminal work so Python can validate nested rollback markers exactly."""
    return (
        select(
            StoryArcSyncWork.id,
            StoryArcSyncWork.last_result,
            StoryArcSyncWork.origin_import_job_id,
            StoryArcSyncWork.origin_import_action_id,
            StoryArcSyncWork.issue_story_arc_id,
            StoryArcSyncWork.desired_generation,
            ImportJob.id,
            ImportJob.status,
            ImportJobAction.id,
            ImportJobAction.import_job_id,
            ImportJobAction.status,
        )
        .join(IssueStoryArc, StoryArcSyncWork.issue_story_arc_id == IssueStoryArc.id)
        .outerjoin(
            ImportJobAction,
            StoryArcSyncWork.origin_import_action_id == ImportJobAction.id,
        )
        .outerjoin(ImportJob, StoryArcSyncWork.origin_import_job_id == ImportJob.id)
        .where(
            IssueStoryArc.story_arc_id == story_arc_id,
            StoryArcSyncWork.state.in_(_TERMINAL_SYNC_STATES),
            StoryArcSyncWork.id > after_work_id,
        )
        .order_by(StoryArcSyncWork.id)
        .limit(limit)
    )


async def _active_sync_recovery_exists(
    session: AsyncSession,
    *,
    story_arc_id: int,
    cancellation_requested: Callable[[], bool] | None,
) -> bool:
    if await session.scalar(_active_sync_work_statement(story_arc_id)) is not None:
        return True
    after_id = 0
    while True:
        _check_cancelled(cancellation_requested)
        rows = (
            await session.execute(
                _terminal_sync_work_statement(
                    story_arc_id,
                    after_work_id=after_id,
                    limit=_SCAN_PAGE_SIZE,
                )
            )
        ).all()
        scopes = tuple(_terminal_sync_scope(row) for row in rows)
        if any(_terminal_sync_recovery_pending(scope) for scope in scopes):
            return True
        if len(scopes) < _SCAN_PAGE_SIZE:
            return False
        after_id = scopes[-1].id
        await asyncio.sleep(0)


def _terminal_sync_scope(row: Row[tuple[object, ...]]) -> _TerminalSyncWorkScope:
    (
        work_id,
        last_result,
        origin_import_job_id,
        origin_import_action_id,
        membership_id,
        desired_generation,
        job_id,
        job_status,
        action_id,
        action_import_job_id,
        action_status,
    ) = row
    return _TerminalSyncWorkScope(
        id=cast("int", work_id),
        last_result=last_result,
        origin_import_job_id=cast("int | None", origin_import_job_id),
        origin_import_action_id=cast("int | None", origin_import_action_id),
        issue_story_arc_id=cast("int", membership_id),
        desired_generation=cast("str", desired_generation),
        job_id=cast("int | None", job_id),
        job_status=cast("ImportJobStatus | None", job_status),
        action_id=cast("int | None", action_id),
        action_import_job_id=cast("int | None", action_import_job_id),
        action_status=cast("ImportJobActionStatus | None", action_status),
    )


def _terminal_sync_recovery_pending(scope: _TerminalSyncWorkScope) -> bool:
    result = scope.last_result
    if not isinstance(result, dict):
        return True
    if (
        scope.job_status is ImportJobStatus.ROLLING_BACK
        or scope.action_status is ImportJobActionStatus.ROLLBACK_FAILED
        or (scope.origin_import_job_id is not None and scope.job_id is None)
        or (scope.origin_import_action_id is not None and scope.action_id is None)
        or (
            scope.origin_import_action_id is not None
            and scope.action_import_job_id != scope.origin_import_job_id
        )
    ):
        return True
    if "rollback" not in result:
        return scope.action_status is ImportJobActionStatus.ROLLED_BACK
    marker = result["rollback"]
    if not isinstance(marker, dict):
        return True
    status = marker.get("status")
    base_keys = {
        "schema_version",
        "status",
        "import_job_id",
        "import_action_id",
        "sync_work_id",
        "membership_id",
        "desired_generation",
    }
    if (
        status not in _SETTLED_ROLLBACK_STATUSES
        or scope.action_status is not ImportJobActionStatus.ROLLED_BACK
        or scope.action_id is None
        or scope.origin_import_action_id != scope.action_id
        or scope.origin_import_job_id is None
        or marker.get("schema_version") != 1
        or marker.get("import_job_id") != scope.origin_import_job_id
        or marker.get("import_action_id") != scope.origin_import_action_id
        or marker.get("sync_work_id") != scope.id
        or marker.get("membership_id") != scope.issue_story_arc_id
        or marker.get("desired_generation") != scope.desired_generation
    ):
        return True
    if status == "cancelled_before_publish":
        return set(marker) != base_keys
    expected_ownership = (
        StoryArcPlacementOwnership.MANAGED.value
        if status == "managed_placement_removed"
        else StoryArcPlacementOwnership.REFERENCED.value
    )
    placement_id = marker.get("placement_id")
    return bool(
        set(marker) != base_keys | {"placement_id", "placement_ownership"}
        or not _is_positive_int(placement_id)
        or marker.get("placement_ownership") != expected_ownership
    )


async def _membership_scope_digest(
    session: AsyncSession,
    *,
    story_arc_id: int,
    arc_name: str,
    cancellation_requested: Callable[[], bool] | None,
) -> str:
    digest = hashlib.sha256()
    after_id = 0
    while True:
        _check_cancelled(cancellation_requested)
        page = await _load_membership_scope_page(
            session,
            story_arc_id=story_arc_id,
            arc_name=arc_name,
            after_membership_id=after_id,
            limit=_SCAN_PAGE_SIZE,
        )
        await session.rollback()
        for item in page:
            _update_digest(digest, item.digest_record())
        if len(page) < _SCAN_PAGE_SIZE:
            return digest.hexdigest()
        after_id = page[-1].context.membership_id
        await asyncio.sleep(0)


async def _placement_target_index(
    session: AsyncSession,
    *,
    story_arc_id: int,
    arc_name: str,
    proposed: StoryArcPlacementPolicy,
    cancellation_requested: Callable[[], bool] | None,
) -> tuple[str, dict[str, int], dict[str, int]]:
    digest = hashlib.sha256()
    exact_counts: dict[str, int] = {}
    folded_counts: dict[str, int] = {}
    after_id = 0
    while True:
        _check_cancelled(cancellation_requested)
        page = await _load_placement_scope_page(
            session,
            story_arc_id=story_arc_id,
            arc_name=arc_name,
            after_placement_id=after_id,
            limit=_SCAN_PAGE_SIZE,
        )
        await session.rollback()
        for placement in page:
            _update_digest(digest, placement.digest_record())
            if (
                placement.ownership is StoryArcPlacementOwnership.MANAGED
                and proposed.mode in _MANAGED_POLICY_MODES
            ):
                target = _rendered_target_path(placement.context.context, proposed)
                exact_key, folded_key = _path_keys(target)
                exact_counts[exact_key] = exact_counts.get(exact_key, 0) + 1
                folded_counts[folded_key] = folded_counts.get(folded_key, 0) + 1
        if len(page) < _SCAN_PAGE_SIZE:
            return digest.hexdigest(), exact_counts, folded_counts
        after_id = page[-1].id
        await asyncio.sleep(0)


async def _classify_placements(
    session: AsyncSession,
    *,
    story_arc_id: int,
    arc_name: str,
    current: StoryArcPlacementPolicy,
    proposed: StoryArcPlacementPolicy,
    exact_target_counts: dict[str, int],
    folded_target_counts: dict[str, int],
    limit: int,
    after_placement_id: int,
    cancellation_requested: Callable[[], bool] | None,
) -> tuple[
    str,
    str,
    _PreviewCounts,
    tuple[StoryArcPolicyMigrationPreviewItem, ...],
    int | None,
    bool,
]:
    placement_digest = hashlib.sha256()
    plan_digest = hashlib.sha256()
    counts = _PreviewCounts()
    response_items: list[StoryArcPolicyMigrationPreviewItem] = []
    response_has_more = False
    cursor = 0
    while True:
        _check_cancelled(cancellation_requested)
        page = await _load_placement_scope_page(
            session,
            story_arc_id=story_arc_id,
            arc_name=arc_name,
            after_placement_id=cursor,
            limit=_SCAN_PAGE_SIZE,
        )
        candidate_paths = tuple(
            dict.fromkeys(
                _rendered_target_path(item.context.context, proposed)
                for item in page
                if item.ownership is StoryArcPlacementOwnership.MANAGED
                and proposed.mode in _MANAGED_POLICY_MODES
            )
        )
        occupied = await _load_occupied_placements(session, candidate_paths)
        canonical_paths = await _load_canonical_destination_paths(session, candidate_paths)
        await session.rollback()
        classified_page = await asyncio.to_thread(
            _classify_page,
            page,
            current,
            proposed,
            exact_target_counts,
            folded_target_counts,
            occupied,
            canonical_paths,
            cancellation_requested,
        )
        for placement, classified in zip(page, classified_page, strict=True):
            _update_digest(placement_digest, placement.digest_record())
            _update_digest(plan_digest, classified.digest_record)
            counts = counts.add(classified.item)
            if placement.id <= after_placement_id:
                continue
            if len(response_items) < limit:
                response_items.append(classified.item)
            else:
                response_has_more = True
        if len(page) < _SCAN_PAGE_SIZE:
            break
        cursor = page[-1].id
        await asyncio.sleep(0)
    next_cursor = response_items[-1].placement_id if response_items and response_has_more else None
    return (
        placement_digest.hexdigest(),
        plan_digest.hexdigest(),
        counts,
        tuple(response_items),
        next_cursor,
        response_has_more,
    )


async def _load_membership_scope_page(
    session: AsyncSession,
    *,
    story_arc_id: int,
    arc_name: str,
    after_membership_id: int,
    limit: int,
) -> tuple[_MembershipScope, ...]:
    rows = (
        await session.execute(
            _membership_scope_statement(
                story_arc_id,
                after_membership_id=after_membership_id,
                limit=limit,
            )
        )
    ).all()
    return await _membership_scopes_from_rows(session, arc_name=arc_name, rows=rows)


async def _load_placement_scope_page(
    session: AsyncSession,
    *,
    story_arc_id: int,
    arc_name: str,
    after_placement_id: int,
    limit: int,
) -> tuple[_PlacementScope, ...]:
    rows = list(
        (
            await session.scalars(
                _placement_scope_statement(
                    story_arc_id,
                    after_placement_id=after_placement_id,
                    limit=limit,
                )
            )
        ).all()
    )
    if not rows:
        return ()
    membership_rows = (
        await session.execute(
            select(IssueStoryArc, Issue, Series, Publisher)
            .outerjoin(Issue, IssueStoryArc.issue_id == Issue.id)
            .outerjoin(Series, Issue.series_id == Series.id)
            .outerjoin(Publisher, Series.publisher_id == Publisher.id)
            .where(
                IssueStoryArc.story_arc_id == story_arc_id,
                IssueStoryArc.id.in_(tuple(row.issue_story_arc_id for row in rows)),
            )
            .order_by(IssueStoryArc.id)
        )
    ).all()
    contexts = {
        item.context.membership_id: item
        for item in await _membership_scopes_from_rows(
            session,
            arc_name=arc_name,
            rows=membership_rows,
        )
    }
    return tuple(_placement_scope(row, contexts[row.issue_story_arc_id]) for row in rows)


async def _membership_scopes_from_rows(
    session: AsyncSession,
    *,
    arc_name: str,
    rows: Sequence[Row[tuple[IssueStoryArc, Issue, Series, Publisher]]],
) -> tuple[_MembershipScope, ...]:
    issue_ids = tuple(issue.id for _membership, issue, _series, _publisher in rows if issue)
    files_by_issue: dict[int, LibraryFile] = {}
    if issue_ids:
        first_ids = (
            select(func.min(LibraryFile.id))
            .where(LibraryFile.issue_id.in_(issue_ids))
            .group_by(LibraryFile.issue_id)
        )
        files = list(
            (
                await session.scalars(
                    select(LibraryFile)
                    .where(LibraryFile.id.in_(first_ids))
                    .order_by(LibraryFile.id)
                )
            ).all()
        )
        for library_file in files:
            if library_file.issue_id is not None:
                files_by_issue[library_file.issue_id] = library_file
    return tuple(
        _membership_scope(
            arc_name=arc_name,
            membership=membership,
            issue=issue,
            series=series,
            publisher=publisher,
            library_file=files_by_issue.get(issue.id) if issue is not None else None,
        )
        for membership, issue, series, publisher in rows
    )


def _membership_scope(
    *,
    arc_name: str,
    membership: IssueStoryArc,
    issue: Issue | None,
    series: Series | None,
    publisher: Publisher | None,
    library_file: LibraryFile | None,
) -> _MembershipScope:
    issue_number_text = (
        issue.effective_issue_number_text
        if issue is not None
        else membership.source_issue_number_text or "unknown"
    )
    extension = library_file.file_format.value if library_file is not None else "cbz"
    context = _PlacementContext(
        membership_id=membership.id,
        story_arc_id=membership.story_arc_id,
        sequence_number=membership.sequence_number,
        issue_id=issue.id if issue is not None else None,
        issue_number_text=issue_number_text,
        story_arc_name=arc_name,
        series_name=(
            series.title
            if series is not None
            else membership.source_series_name or "Unknown Series"
        ),
        publisher_name=publisher.name if publisher is not None else membership.source_publisher,
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
    return _MembershipScope(
        context=context,
        source_ordinal=membership.source_ordinal,
        resolution_state=membership.resolution_state.value,
        sync_eligible=membership.sync_eligible,
        membership_updated_at=membership.updated_at,
        membership_evidence=dict(membership.evidence or {}),
        materialization_result=dict(membership.last_materialization_result or {}),
        issue_updated_at=issue.updated_at if issue is not None else None,
        series_id=series.id if series is not None else None,
        series_updated_at=series.updated_at if series is not None else None,
        canonical_id=library_file.id if library_file is not None else None,
        canonical_path=library_file.file_path if library_file is not None else None,
        canonical_size=library_file.file_size if library_file is not None else None,
        canonical_format=library_file.file_format.value if library_file is not None else None,
        canonical_hash=library_file.file_hash if library_file is not None else None,
        canonical_modified_at=(library_file.file_modified_at if library_file is not None else None),
        canonical_updated_at=library_file.updated_at if library_file is not None else None,
        canonical_library_root_id=(
            library_file.library_root_id if library_file is not None else None
        ),
        canonical_storage_mode=(
            library_file.storage_mode.value if library_file is not None else None
        ),
        canonical_source_signature=(
            dict(library_file.source_signature or {}) if library_file is not None else {}
        ),
    )


def _placement_scope(row: StoryArcPlacement, context: _MembershipScope) -> _PlacementScope:
    last_result = dict(row.last_result or {})
    raw_target = last_result.get("target_fingerprint")
    target_fingerprint = dict(raw_target) if isinstance(raw_target, dict) else {}
    return _PlacementScope(
        id=row.id,
        membership_id=row.issue_story_arc_id,
        library_file_id=row.library_file_id,
        library_root_id=row.library_root_id,
        placement_path=row.placement_path,
        mode=row.mode,
        ownership=row.ownership,
        symlink_style=row.symlink_style.value if row.symlink_style is not None else None,
        source_kind=row.source_kind.value,
        creating_action_id=row.creating_action_id,
        rendered_reading_order=row.rendered_reading_order,
        policy_schema_version=row.policy_schema_version,
        operation_token=row.operation_token,
        source_fingerprint=dict(row.source_fingerprint or {}),
        target_fingerprint=target_fingerprint,
        state=row.state,
        last_result=last_result,
        updated_at=row.updated_at,
        context=context,
    )


async def _load_occupied_placements(
    session: AsyncSession,
    paths: tuple[str, ...],
) -> dict[str, _OccupiedPlacement]:
    if not paths:
        return {}
    rows = (
        await session.execute(
            select(
                StoryArcPlacement.id,
                StoryArcPlacement.placement_path,
                StoryArcPlacement.ownership,
            )
            .where(StoryArcPlacement.placement_path.in_(paths))
            .order_by(StoryArcPlacement.id)
        )
    ).all()
    return {
        path: _OccupiedPlacement(id=placement_id, ownership=ownership)
        for placement_id, path, ownership in rows
    }


async def _load_canonical_destination_paths(
    session: AsyncSession,
    paths: tuple[str, ...],
) -> frozenset[str]:
    if not paths:
        return frozenset()
    return frozenset(
        (
            await session.scalars(
                select(LibraryFile.file_path)
                .where(LibraryFile.file_path.in_(paths))
                .order_by(LibraryFile.id)
            )
        ).all()
    )


def _classify_page(
    page: Sequence[_PlacementScope],
    current: StoryArcPlacementPolicy,
    proposed: StoryArcPlacementPolicy,
    exact_target_counts: dict[str, int],
    folded_target_counts: dict[str, int],
    occupied: dict[str, _OccupiedPlacement],
    canonical_paths: frozenset[str],
    cancellation_requested: Callable[[], bool] | None,
) -> tuple[_ClassifiedPlacement, ...]:
    results: list[_ClassifiedPlacement] = []
    for placement in page:
        _check_cancelled(cancellation_requested)
        results.append(
            _classify_placement(
                placement,
                current=current,
                proposed=proposed,
                exact_target_counts=exact_target_counts,
                folded_target_counts=folded_target_counts,
                occupied=occupied,
                canonical_paths=canonical_paths,
            )
        )
    return tuple(results)


def _classify_placement(
    placement: _PlacementScope,
    *,
    current: StoryArcPlacementPolicy,
    proposed: StoryArcPlacementPolicy,
    exact_target_counts: dict[str, int],
    folded_target_counts: dict[str, int],
    occupied: dict[str, _OccupiedPlacement],
    canonical_paths: frozenset[str],
) -> _ClassifiedPlacement:
    context = placement.context.context
    if placement.ownership is StoryArcPlacementOwnership.REFERENCED:
        item = StoryArcPolicyMigrationPreviewItem(
            placement_id=placement.id,
            membership_id=placement.membership_id,
            ownership=placement.ownership.value,
            action="preserve_referenced",
            old_mode=placement.mode.value,
            new_mode=placement.mode.value,
            old_path=placement.placement_path,
            new_path=placement.placement_path,
            collision="none",
            blocked=False,
            reason="Referenced artifact is preserved and is not a migration target",
            required_bytes=0,
        )
        return _classified(item, placement, old_inspection=None, new_inspection=None)

    desired_path = (
        _rendered_target_path(context, proposed)
        if proposed.mode is not StoryArcPlacementPolicyMode.LOGICAL
        else None
    )
    old_inspection = _inspect_current_managed(placement, current)
    if old_inspection.state is not StoryArcPlacementInspectionState.MANAGED_CURRENT:
        item = _managed_item(
            placement,
            proposed=proposed,
            desired_path=desired_path,
            action=_managed_action(placement, proposed, desired_path),
            blocked=True,
            collision="none",
            reason="Managed placement no longer matches its durable ownership evidence",
        )
        return _classified(
            item,
            placement,
            old_inspection=_inspection_digest(old_inspection),
            new_inspection=None,
        )

    source_size = _inspection_source_size(old_inspection)
    action = _managed_action(placement, proposed, desired_path)
    if action == "remove_managed":
        item = _managed_item(
            placement,
            proposed=proposed,
            desired_path=desired_path,
            action=action,
            source_size=source_size,
        )
        return _classified(
            item,
            placement,
            old_inspection=_inspection_digest(old_inspection),
            new_inspection=None,
        )
    if action == "managed_unchanged":
        item = _managed_item(
            placement,
            proposed=proposed,
            desired_path=desired_path,
            action=action,
            source_size=source_size,
        )
        return _classified(
            item,
            placement,
            old_inspection=_inspection_digest(old_inspection),
            new_inspection=None,
        )
    if desired_path is None:  # pragma: no cover - action exhaustiveness
        raise ValueError("Managed migration target is missing")

    exact_key, folded_key = _path_keys(desired_path)
    collision: str | None = None
    if exact_target_counts.get(exact_key, 0) > 1:
        collision = "duplicate_migration_target"
    elif folded_target_counts.get(folded_key, 0) > 1:
        collision = "case_only_migration_target"
    elif desired_path in canonical_paths:
        collision = "canonical_destination"
    else:
        tracked = occupied.get(desired_path)
        if tracked is not None and tracked.id != placement.id:
            collision = (
                "referenced_destination_preserved"
                if tracked.ownership is StoryArcPlacementOwnership.REFERENCED
                else "placement_destination_conflict"
            )
    if collision is not None:
        item = _managed_item(
            placement,
            proposed=proposed,
            desired_path=desired_path,
            action=action,
            blocked=True,
            collision=collision,
            reason="Rendered destination is not exclusively available for this managed placement",
            source_size=source_size,
        )
        return _classified(
            item,
            placement,
            old_inspection=_inspection_digest(old_inspection),
            new_inspection=None,
        )

    if _normal_path(desired_path) == _normal_path(placement.placement_path):
        new_inspection = _inspect_same_path_rebuild(placement, proposed, desired_path)
        ready = new_inspection.state is StoryArcPlacementInspectionState.FREE
        collision_code = (
            "none"
            if ready
            else new_inspection.collision.value
            if new_inspection.collision is not StoryArcCollisionKind.NONE
            else new_inspection.code or "inspection_blocked"
        )
        item = _managed_item(
            placement,
            proposed=proposed,
            desired_path=desired_path,
            action=action,
            blocked=not ready,
            collision=collision_code,
            reason=(None if ready else new_inspection.reason or "Destination is blocked"),
            source_size=source_size,
        )
        return _classified(
            item,
            placement,
            old_inspection=_inspection_digest(old_inspection),
            new_inspection=_inspection_digest(new_inspection),
        )

    new_inspection = inspect_story_arc_placement(
        _placement_plan(context, proposed),
    )
    ready = new_inspection.state is StoryArcPlacementInspectionState.FREE
    collision_code = (
        "none"
        if ready
        else new_inspection.code or new_inspection.collision.value or "inspection_blocked"
    )
    item = _managed_item(
        placement,
        proposed=proposed,
        desired_path=desired_path,
        action=action,
        blocked=not ready,
        collision=collision_code,
        reason=(None if ready else new_inspection.reason or "Destination is blocked"),
        source_size=source_size,
    )
    return _classified(
        item,
        placement,
        old_inspection=_inspection_digest(old_inspection),
        new_inspection=_inspection_digest(new_inspection),
    )


def _inspect_current_managed(
    placement: _PlacementScope,
    current: StoryArcPlacementPolicy,
) -> StoryArcPlacementInspection:
    rendered = (
        _rendered_target_path(placement.context.context, current)
        if current.mode is not StoryArcPlacementPolicyMode.LOGICAL
        else None
    )
    evidence_valid = (
        current.mode in _MANAGED_POLICY_MODES
        and placement.mode.value == current.mode.value
        and placement.policy_schema_version == STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION
        and placement.rendered_reading_order == placement.context.context.sequence_number
        and rendered is not None
        and _normal_path(rendered) == _normal_path(placement.placement_path)
        and placement.state is StoryArcPlacementState.CURRENT
        and placement.operation_token is None
        and bool(placement.source_fingerprint)
        and bool(placement.target_fingerprint)
        and placement.last_result.get("status") in _UNCHANGED_MANAGED_STATUSES
    )
    if not evidence_valid:
        return _blocked_inspection(placement, "managed_ownership_evidence_changed")
    return inspect_story_arc_placement(
        _placement_plan(placement.context.context, current),
        existing=placement.inspection_evidence(),
    )


def _inspect_same_path_rebuild(
    placement: _PlacementScope,
    proposed: StoryArcPlacementPolicy,
    desired_path: str,
) -> StoryArcPlacementInspection:
    inspected = inspect_story_arc_placement(
        _placement_plan(placement.context.context, proposed),
        existing=placement.inspection_evidence(),
    )
    if inspected.code != "representation_changed":
        return inspected
    if proposed.mode is StoryArcPlacementPolicyMode.HARDLINK:
        canonical_path = placement.context.context.canonical_path
        if canonical_path is None:
            return StoryArcPlacementInspection(
                state=StoryArcPlacementInspectionState.BLOCKED,
                mode=StoryArcPlacementMode.HARDLINK,
                target_path=Path(desired_path),
                collision=StoryArcCollisionKind.SOURCE_UNAVAILABLE,
                code="source_unavailable",
                reason="Canonical source is unavailable for hardlink migration",
            )
        try:
            source_device = _filesystem_device(Path(canonical_path))
            destination_device = _filesystem_device(Path(desired_path).parent)
        except OSError:
            return StoryArcPlacementInspection(
                state=StoryArcPlacementInspectionState.BLOCKED,
                mode=StoryArcPlacementMode.HARDLINK,
                target_path=Path(desired_path),
                collision=StoryArcCollisionKind.NONE,
                code="inspection_failed",
                reason="Proposed placement mode could not be inspected safely",
            )
        if source_device != destination_device:
            return StoryArcPlacementInspection(
                state=StoryArcPlacementInspectionState.BLOCKED,
                mode=StoryArcPlacementMode.HARDLINK,
                target_path=Path(desired_path),
                collision=StoryArcCollisionKind.CROSS_DEVICE,
                code="cross_device",
                reason="Hardlink source and destination are on different filesystems",
            )
    source_size = _inspection_source_size(inspected)
    return StoryArcPlacementInspection(
        state=StoryArcPlacementInspectionState.FREE,
        mode=_filesystem_mode(proposed.mode),
        target_path=Path(desired_path),
        collision=StoryArcCollisionKind.NONE,
        code="owned_rebuild",
        reason="Existing managed artifact is replaceable after ownership revalidation",
        required_bytes=(
            source_size or 0 if proposed.mode is StoryArcPlacementPolicyMode.COPY else 0
        ),
        proposed_ownership=StoryArcPlacementOwnership.MANAGED.value,
        source_fingerprint=inspected.source_fingerprint,
        target_fingerprint=inspected.target_fingerprint,
    )


def _filesystem_device(path: Path) -> int:
    return int(os.stat(path, follow_symlinks=False).st_dev)


def _inspection_source_size(inspection: StoryArcPlacementInspection) -> int | None:
    fingerprint = inspection.source_fingerprint
    value = fingerprint.get("size") if fingerprint is not None else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _blocked_inspection(
    placement: _PlacementScope,
    code: str,
) -> StoryArcPlacementInspection:
    return StoryArcPlacementInspection(
        state=StoryArcPlacementInspectionState.BLOCKED,
        mode=placement.mode,
        target_path=Path(placement.placement_path),
        collision=StoryArcCollisionKind.NONE,
        code=code,
        reason="Managed placement ownership evidence changed",
    )


def _managed_action(
    placement: _PlacementScope,
    proposed: StoryArcPlacementPolicy,
    desired_path: str | None,
) -> str:
    if proposed.mode not in _MANAGED_POLICY_MODES:
        return "remove_managed"
    if (
        desired_path is not None
        and _normal_path(desired_path) == _normal_path(placement.placement_path)
        and placement.mode.value == proposed.mode.value
        and placement.symlink_style
        == (proposed.symlink_style.value if proposed.symlink_style is not None else None)
    ):
        return "managed_unchanged"
    return "migrate_managed"


def _managed_item(
    placement: _PlacementScope,
    *,
    proposed: StoryArcPlacementPolicy,
    desired_path: str | None,
    action: str,
    blocked: bool = False,
    collision: str = "none",
    reason: str | None = None,
    source_size: int | None = None,
) -> StoryArcPolicyMigrationPreviewItem:
    required_bytes = (
        source_size or 0
        if action == "migrate_managed" and proposed.mode is StoryArcPlacementPolicyMode.COPY
        else 0
    )
    return StoryArcPolicyMigrationPreviewItem(
        placement_id=placement.id,
        membership_id=placement.membership_id,
        ownership=placement.ownership.value,
        action=action,
        old_mode=placement.mode.value,
        new_mode=proposed.mode.value,
        old_path=placement.placement_path,
        new_path=desired_path,
        collision=collision,
        blocked=blocked,
        reason=reason,
        required_bytes=required_bytes,
    )


def _classified(
    item: StoryArcPolicyMigrationPreviewItem,
    placement: _PlacementScope,
    *,
    old_inspection: dict[str, object] | None,
    new_inspection: dict[str, object] | None,
) -> _ClassifiedPlacement:
    return _ClassifiedPlacement(
        item=item,
        digest_record={
            "placement": placement.digest_record(),
            "action": item.action,
            "old_mode": item.old_mode,
            "new_mode": item.new_mode,
            "old_path": item.old_path,
            "new_path": item.new_path,
            "collision": item.collision,
            "blocked": item.blocked,
            "required_bytes": item.required_bytes,
            "old_inspection": old_inspection,
            "new_inspection": new_inspection,
        },
    )


def _placement_plan(
    context: _PlacementContext,
    policy: StoryArcPlacementPolicy,
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
    )


def _filesystem_mode(mode: StoryArcPlacementPolicyMode) -> StoryArcPlacementMode:
    if mode in {StoryArcPlacementPolicyMode.LOGICAL, StoryArcPlacementPolicyMode.REFERENCE_ONLY}:
        return StoryArcPlacementMode.REFERENCE_ONLY
    return StoryArcPlacementMode(mode.value)


def _inspection_digest(
    inspection: StoryArcPlacementInspection,
) -> dict[str, object]:
    return {
        "state": inspection.state.value,
        "mode": inspection.mode.value,
        "target_path": str(inspection.target_path) if inspection.target_path is not None else None,
        "collision": inspection.collision.value,
        "code": inspection.code,
        "required_bytes": inspection.required_bytes,
        "source_fingerprint": dict(inspection.source_fingerprint or {}),
        "target_fingerprint": dict(inspection.target_fingerprint or {}),
    }


async def _validate_policy_root(
    policy: StoryArcPlacementPolicy,
    *,
    role: str,
) -> _PolicyRootFingerprint:
    if policy.mode is StoryArcPlacementPolicyMode.LOGICAL:
        return _PolicyRootFingerprint(resolved_path=None, device=None, inode=None)
    if policy.destination_root is None:
        raise StoryArcPolicyMigrationError(
            f"{role}_destination_root_unavailable",
            f"The {role} Story Arc destination root is unavailable",
            category="safety",
        )

    def validate() -> _PolicyRootFingerprint:
        root = Path(policy.destination_root or "")
        if not root.is_absolute():
            raise OSError
        before = os.stat(root, follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode):
            raise OSError
        resolved = root.resolve(strict=True)
        if resolved != root or not resolved.is_dir():
            raise OSError
        if not os.access(resolved, os.R_OK | os.W_OK | os.X_OK):
            raise OSError
        metadata = os.stat(root, follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_dev != before.st_dev
            or metadata.st_ino != before.st_ino
        ):
            raise OSError
        return _PolicyRootFingerprint(
            resolved_path=str(resolved),
            device=int(metadata.st_dev),
            inode=int(metadata.st_ino),
        )

    try:
        return await asyncio.to_thread(validate)
    except OSError as exc:
        raise StoryArcPolicyMigrationError(
            f"{role}_destination_root_unavailable",
            f"The {role} Story Arc destination root is unavailable or unsafe",
            category="safety",
        ) from exc


def _policy_token_record(policy: StoryArcPlacementPolicy) -> dict[str, object]:
    return {
        "configured": policy.configured,
        "revision": policy.revision,
        **policy.snapshot,
    }


def _preview_counts_record(preview: StoryArcPolicyMigrationPreview) -> dict[str, int]:
    return {
        "total": preview.total_placement_count,
        "managed_migrate": preview.managed_migrate_count,
        "managed_remove": preview.managed_remove_count,
        "managed_unchanged": preview.managed_unchanged_count,
        "referenced_preserved": preview.referenced_preserved_count,
        "collisions": preview.collision_count,
        "blocked": preview.blocked_count,
        "required_bytes": preview.required_bytes,
    }


def _signed_preview_payload(preview: _SignedPreview) -> dict[str, object]:
    return {
        "schema_version": _TOKEN_SCHEMA_VERSION,
        "actor_id": preview.actor_id,
        "story_arc_id": preview.story_arc_id,
        "expected_revision": preview.expected_revision,
        "current_policy": preview.current_policy,
        "proposed_policy": preview.proposed_policy,
        "scope_digest": preview.scope_digest,
        "counts": preview.counts,
    }


def _signed_preview_from_payload(raw: object) -> _SignedPreview:
    expected_keys = {
        "schema_version",
        "actor_id",
        "story_arc_id",
        "expected_revision",
        "current_policy",
        "proposed_policy",
        "scope_digest",
        "counts",
    }
    if (
        not isinstance(raw, dict)
        or set(raw) != expected_keys
        or raw.get("schema_version") != _TOKEN_SCHEMA_VERSION
    ):
        raise ValueError("invalid preview payload")
    current = _validated_policy_record(raw["current_policy"])
    proposed = _validated_policy_record(raw["proposed_policy"])
    counts = _validated_counts_record(raw["counts"])
    return _SignedPreview(
        actor_id=_positive_int(raw["actor_id"]),
        story_arc_id=_positive_int(raw["story_arc_id"]),
        expected_revision=_positive_int(raw["expected_revision"]),
        current_policy=current,
        proposed_policy=proposed,
        scope_digest=_fixed_hex(raw["scope_digest"], length=64),
        counts=counts,
    )


def _validated_policy_record(raw: object) -> dict[str, object]:
    expected_keys = {
        "configured",
        "revision",
        "schema_version",
        "mode",
        "target_library_root_id",
        "destination_root",
        "folder_template",
        "file_template",
        "symlink_style",
        "synchronize",
    }
    if not isinstance(raw, dict) or set(raw) != expected_keys:
        raise ValueError("invalid policy")
    if raw["configured"] is not True or raw["schema_version"] != 1:
        raise ValueError("invalid policy")
    _positive_int(raw["revision"])
    StoryArcPlacementPolicyMode(_string(raw["mode"]))
    root_id = raw["target_library_root_id"]
    if root_id is not None:
        _positive_int(root_id)
    destination = raw["destination_root"]
    if destination is not None:
        _string(destination)
    _string(raw["folder_template"])
    _string(raw["file_template"])
    style = raw["symlink_style"]
    if style is not None:
        _string(style)
    if not isinstance(raw["synchronize"], bool):
        raise ValueError("invalid policy")
    return dict(raw)


def _validated_counts_record(raw: object) -> dict[str, int]:
    keys = {
        "total",
        "managed_migrate",
        "managed_remove",
        "managed_unchanged",
        "referenced_preserved",
        "collisions",
        "blocked",
        "required_bytes",
    }
    if not isinstance(raw, dict) or set(raw) != keys:
        raise ValueError("invalid counts")
    return {key: _nonnegative_int(raw[key]) for key in keys}


def _bounded_page(limit: int, after_placement_id: int) -> tuple[int, int]:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= _MAX_RESPONSE_PAGE_SIZE
    ):
        raise StoryArcPolicyMigrationError(
            "invalid_page_limit", f"Migration preview limit must be 1-{_MAX_RESPONSE_PAGE_SIZE}"
        )
    if (
        isinstance(after_placement_id, bool)
        or not isinstance(after_placement_id, int)
        or after_placement_id < 0
    ):
        raise StoryArcPolicyMigrationError(
            "invalid_page_cursor", "Migration preview cursor must be a non-negative integer"
        )
    return limit, after_placement_id


def _check_cancelled(cancellation_requested: Callable[[], bool] | None) -> None:
    if cancellation_requested is not None and cancellation_requested():
        raise StoryArcPolicyMigrationError(
            "migration_preview_cancelled",
            "Story Arc policy migration preview was cancelled",
            category="cancelled",
        )


def _path_keys(path: str) -> tuple[str, str]:
    exact = os.path.abspath(os.path.normpath(path))
    return hashlib.sha256(exact.encode()).hexdigest(), hashlib.sha256(
        exact.casefold().encode()
    ).hexdigest()


def _normal_path(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _update_digest(digest: _DigestWriter, value: object) -> None:
    digest.update(_canonical_json(value))
    digest.update(b"\n")


def _digest_record(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode()


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _positive_int(value: object) -> int:
    if not _is_positive_int(value):
        raise ValueError("positive integer required")
    return cast("int", value)


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("non-negative integer required")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("string required")
    return value


def _fixed_hex(value: object, *, length: int) -> str:
    result = _string(value)
    if len(result) != length or any(character not in "0123456789abcdef" for character in result):
        raise ValueError("fixed hexadecimal string required")
    return result


__all__ = [
    "STORY_ARC_POLICY_MIGRATION_CONFIRMATION",
    "StoryArcPolicyMigrationConfirmation",
    "StoryArcPolicyMigrationError",
    "StoryArcPolicyMigrationPreview",
    "StoryArcPolicyMigrationPreviewItem",
    "StoryArcPolicyMigrationService",
]
