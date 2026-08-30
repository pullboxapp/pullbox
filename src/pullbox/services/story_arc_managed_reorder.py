"""Previewed, crash-truthful reordering for managed Story Arc placements.

Adjacent UI moves affect at most two memberships.  This service keeps that
boundary explicit: it renders a complete two-membership plan, signs it for
confirmation, durably reserves every managed artifact, and only then moves
files outside a database transaction.  Canonical files and referenced
placements are observations only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NoReturn, cast
from uuid import uuid4

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import and_, or_, select
from sqlalchemy import update as sa_update

from pullbox.core.config_resolver import get_application_secret
from pullbox.models.library import LibraryFile
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcLifecycle,
    StoryArcPlacement,
    StoryArcPlacementMode,
    StoryArcPlacementOwnership,
    StoryArcPlacementState,
)
from pullbox.services.story_arc_placement_integration import (
    STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION,
    StoryArcPlacementPolicyMode,
    _load_one_context,
    _policy_from_arc,
    _rendered_target_path,
)
from pullbox.services.story_arc_placement_service import (
    Fingerprint,
    StoryArcPlacementError,
    _case_only_collision,
    _case_only_collision_at,
    _entry_exists_at,
    _fingerprint_target,
    _fingerprint_target_at,
    _fsync_directory,
    _open_secure_parent_directory,
    _path_exists,
    _reject_canonical_destination,
    _SecureParentDirectory,
    _validate_path_limits,
    _validate_removal_representation_at,
    _validate_target_lexically,
    _validated_root,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import Select

StoryArcReorderDirection = Literal["up", "down"]
StoryArcReorderAction = Literal[
    "rename",
    "managed_unchanged",
    "referenced_drift",
    "referenced_unchanged",
    "logical_reorder",
]

_TOKEN_SALT = "pullbox-story-arc-managed-reorder-v1"
_TOKEN_MAX_AGE_SECONDS = 15 * 60
_TOKEN_SCHEMA_VERSION = 1
_JOURNAL_SCHEMA_VERSION = 1
_MAX_PLACEMENTS_PER_ADJACENT_MOVE = 100
_MAX_COLLISION_SCAN_PATHS = 204
_UNCHANGED_MANAGED_STATUSES = frozenset({"complete", "rename_cancelled", "rename_failed"})
_ACTIVE_REORDER_STATUSES = frozenset({"rename_prepared", "rename_recovery_required"})


class StoryArcManagedReorderError(RuntimeError):
    """Categorized failure that preserves safe UI and recovery semantics."""

    def __init__(self, code: str, message: str, *, category: str = "validation") -> None:
        self.code = code
        self.category = category
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class StoryArcReorderPreviewItem:
    """One complete old/new placement consequence in a signed preview."""

    membership_id: int
    placement_id: int | None
    ownership: str
    mode: str
    old_reading_order: int
    new_reading_order: int
    old_path: str | None
    new_path: str | None
    rendered_path_after: str | None
    temporary_path: str | None
    action: StoryArcReorderAction


@dataclass(frozen=True, slots=True)
class StoryArcReorderPreview:
    """Read-only adjacent-move preview requiring explicit confirmation."""

    story_arc_id: int
    membership_id: int
    direction: StoryArcReorderDirection
    expected_revision: int
    preview_token: str
    items: tuple[StoryArcReorderPreviewItem, ...]
    managed_rename_count: int
    referenced_drift_count: int
    referenced_preserved_count: int
    recovery_pending: bool
    requires_confirmation: bool = True
    filesystem_mutated: bool = False


@dataclass(frozen=True, slots=True)
class StoryArcReorderResult:
    """Truthful completed reorder counts."""

    story_arc_id: int
    revision: int
    managed_renamed: int
    referenced_preserved: int
    recovery_pending: bool = False


@dataclass(frozen=True, slots=True)
class _MembershipPlan:
    membership_id: int
    old_sequence_number: int
    old_source_ordinal: int
    new_sequence_number: int
    new_source_ordinal: int


@dataclass(frozen=True, slots=True)
class _PlacementPlan:
    placement_id: int
    membership_id: int
    ownership: str
    mode: str
    old_reading_order: int
    new_reading_order: int
    old_path: str
    new_path: str
    temporary_path: str | None
    rendered_path_after: str
    action: StoryArcReorderAction
    canonical_path: str | None
    source_fingerprint: Fingerprint
    target_fingerprint: Fingerprint
    placement_state: str | None
    last_result_snapshot: dict[str, object]


@dataclass(frozen=True, slots=True)
class _SignedPlan:
    story_arc_id: int
    selected_membership_id: int
    direction: StoryArcReorderDirection
    expected_revision: int
    operation_token: str
    plan_digest: str
    destination_root: str | None
    memberships: tuple[_MembershipPlan, ...]
    placements: tuple[_PlacementPlan, ...]


@dataclass(frozen=True, slots=True)
class _FilesystemResult:
    fingerprints: dict[int, Fingerprint]


class _CancelledDuringReorderError(RuntimeError):
    pass


class StoryArcManagedReorderService:
    """Coordinate signed preview, durable preparation, and safe adjacent moves."""

    async def preview_adjacent_move(
        self,
        session: AsyncSession,
        story_arc_id: int,
        membership_id: int,
        *,
        direction: StoryArcReorderDirection,
        expected_revision: int,
    ) -> StoryArcReorderPreview:
        """Render a complete bounded plan without mutating the DB or filesystem."""
        plan = await self._build_plan(
            session,
            story_arc_id=story_arc_id,
            membership_id=membership_id,
            direction=direction,
            expected_revision=expected_revision,
        )
        return self._preview_from_plan(plan)

    async def confirm_adjacent_move(
        self,
        session: AsyncSession,
        *,
        story_arc_id: int,
        membership_id: int,
        direction: StoryArcReorderDirection,
        expected_revision: int,
        preview_token: str,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> StoryArcReorderResult:
        """Confirm one signed plan, with restart-safe durable filesystem truth."""
        plan = self._decode_plan(preview_token)
        _assert_plan_binding(
            plan,
            story_arc_id=story_arc_id,
            membership_id=membership_id,
            direction=direction,
            expected_revision=expected_revision,
        )
        try:
            await self._verify_and_prepare(session, plan)
        except StoryArcManagedReorderError as exc:
            await session.rollback()
            if not await self._has_active_journal(session, plan):
                await session.rollback()
                raise
            # A recovered prepared operation can outlive its original browser
            # request.  If current DB truth no longer accepts that plan, end
            # the inspection transaction before restoring its old paths.
            await session.rollback()
            await self._raise_after_semantic_failure(session, plan, exc)
        try:
            filesystem_result = await asyncio.to_thread(
                _complete_filesystem_plan,
                plan,
                cancellation_requested,
            )
        except _CancelledDuringReorderError as exc:
            await self._record_rolled_back(
                session,
                plan,
                status="rename_cancelled",
            )
            raise StoryArcManagedReorderError(
                "reorder_cancelled",
                "Story Arc reorder was cancelled and its old paths were restored",
                category="cancelled",
            ) from exc
        except (OSError, StoryArcPlacementError, StoryArcManagedReorderError) as exc:
            rollback_complete = await asyncio.to_thread(_restore_old_paths, plan)
            if rollback_complete:
                await self._record_rolled_back(session, plan, status="rename_failed")
                raise StoryArcManagedReorderError(
                    "reorder_failed",
                    "Story Arc reorder failed and its old paths were restored",
                    category="filesystem",
                ) from exc
            await self._record_recovery_required(session, plan, exc)
            raise StoryArcManagedReorderError(
                "reorder_recovery_required",
                "Story Arc reorder needs recovery before another move can begin",
                category="recovery",
            ) from exc

        try:
            revision = await self._reconcile_success(
                session,
                plan,
                filesystem_result,
            )
        except StoryArcManagedReorderError as exc:
            await session.rollback()
            # Files are already published.  A semantic DB conflict cannot be
            # retried forever (notably after an unrelated revision bump), so
            # restore the old paths outside a transaction and retire the
            # prepared journal.  Incomplete restoration remains discoverable.
            await self._raise_after_semantic_failure(session, plan, exc)
        except Exception:
            await session.rollback()
            # The committed prepared journal remains authoritative.  A retry
            # with the same signed token observes the final files and completes
            # the database reconciliation without moving them again.
            raise

        return StoryArcReorderResult(
            story_arc_id=plan.story_arc_id,
            revision=revision,
            managed_renamed=sum(item.action == "rename" for item in plan.placements),
            # The result is scoped to the bounded adjacent-move plan.  An
            # affected referenced row remains preserved even when fresh
            # reconciliation observes concurrent drift and skips it: this
            # operation never mutates referenced placements or their files.
            referenced_preserved=sum(
                item.ownership == StoryArcPlacementOwnership.REFERENCED.value
                for item in plan.placements
            ),
        )

    def inspect_preview_token(
        self,
        *,
        story_arc_id: int,
        membership_id: int,
        direction: StoryArcReorderDirection,
        expected_revision: int,
        preview_token: str,
        recovery_pending: bool = False,
    ) -> StoryArcReorderPreview:
        """Rehydrate signed preview truth without touching the DB or filesystem."""
        plan = self._decode_plan(preview_token)
        _assert_plan_binding(
            plan,
            story_arc_id=story_arc_id,
            membership_id=membership_id,
            direction=direction,
            expected_revision=expected_revision,
        )
        preview = self._preview_from_plan(plan)
        return replace(
            preview,
            preview_token=preview_token,
            recovery_pending=recovery_pending,
        )

    async def load_pending_preview(
        self,
        session: AsyncSession,
        story_arc_id: int,
    ) -> StoryArcReorderPreview | None:
        """Discover one durable prepared reorder without a browser-held token."""
        coordinator = await session.scalar(_pending_coordinator_statement(story_arc_id))
        if coordinator is None:
            return None
        journal = dict(coordinator.last_result or {})
        recovery_token = journal.get("recovery_token")
        if not isinstance(recovery_token, str):
            raise StoryArcManagedReorderError(
                "reorder_recovery_required",
                "Prepared Story Arc reorder is missing its recovery payload",
                category="recovery",
            )
        plan = self._decode_durable_plan(recovery_token)
        managed = [item for item in plan.placements if item.action == "rename"]
        if (
            plan.story_arc_id != story_arc_id
            or coordinator.operation_token != plan.operation_token
            or journal.get("plan_digest") != plan.plan_digest
            or journal.get("coordinator_placement_id") != coordinator.id
            or not managed
            or coordinator.id != min(item.placement_id for item in managed)
        ):
            raise StoryArcManagedReorderError(
                "reorder_recovery_required",
                "Prepared Story Arc reorder recovery payload does not match its journal",
                category="recovery",
            )
        managed_rows = {
            row.id: row
            for row in (
                await session.scalars(
                    select(StoryArcPlacement).where(
                        StoryArcPlacement.id.in_(tuple(item.placement_id for item in managed))
                    )
                )
            ).all()
        }
        if len(managed_rows) != len(managed):
            raise StoryArcManagedReorderError(
                "reorder_recovery_required",
                "Prepared Story Arc reorder journal is incomplete",
                category="recovery",
            )
        for item in managed:
            row = managed_rows[item.placement_id]
            row_journal = dict(row.last_result or {})
            if (
                row.operation_token != plan.operation_token
                or row_journal.get("status") not in _ACTIVE_REORDER_STATUSES
                or row_journal.get("operation") != "story_arc_reorder"
                or row_journal.get("plan_digest") != plan.plan_digest
                or row_journal.get("coordinator_placement_id") != coordinator.id
                or row_journal.get("old_path") != item.old_path
                or row_journal.get("new_path") != item.new_path
                or row_journal.get("temporary_path") != item.temporary_path
            ):
                raise StoryArcManagedReorderError(
                    "reorder_recovery_required",
                    "Prepared Story Arc reorder journal changed before recovery",
                    category="recovery",
                )
        return replace(self._preview_from_plan(plan), recovery_pending=True)

    async def _build_plan(
        self,
        session: AsyncSession,
        *,
        story_arc_id: int,
        membership_id: int,
        direction: StoryArcReorderDirection,
        expected_revision: int,
    ) -> _SignedPlan:
        arc = await session.get(StoryArc, story_arc_id)
        if arc is None:
            raise StoryArcManagedReorderError(
                "story_arc_not_found", "Story arc was not found", category="not_found"
            )
        if arc.lifecycle is StoryArcLifecycle.ARCHIVED:
            raise StoryArcManagedReorderError(
                "story_arc_archived", "Archived story arcs cannot be reordered"
            )
        if arc.revision != expected_revision:
            raise StoryArcManagedReorderError(
                "revision_conflict",
                "Story arc changed after the page was loaded",
                category="conflict",
            )
        selected = await session.scalar(
            select(IssueStoryArc).where(
                IssueStoryArc.id == membership_id,
                IssueStoryArc.story_arc_id == story_arc_id,
            )
        )
        if selected is None:
            raise StoryArcManagedReorderError(
                "membership_not_found",
                "Story-arc membership was not found",
                category="not_found",
            )
        neighbour = await _load_adjacent_membership(
            session,
            selected=selected,
            direction=direction,
        )
        if neighbour is None:
            edge = "already_first" if direction == "up" else "already_last"
            raise StoryArcManagedReorderError(edge, f"Membership is {edge.replace('_', ' ')}")

        membership_plans = (
            _MembershipPlan(
                membership_id=selected.id,
                old_sequence_number=selected.sequence_number,
                old_source_ordinal=selected.source_ordinal,
                new_sequence_number=neighbour.sequence_number,
                new_source_ordinal=neighbour.source_ordinal,
            ),
            _MembershipPlan(
                membership_id=neighbour.id,
                old_sequence_number=neighbour.sequence_number,
                old_source_ordinal=neighbour.source_ordinal,
                new_sequence_number=selected.sequence_number,
                new_source_ordinal=selected.source_ordinal,
            ),
        )
        operation_token = uuid4().hex
        policy = _policy_from_arc(arc)
        placement_plans = await self._build_placement_plans(
            session,
            arc=arc,
            policy_mode=policy.mode,
            destination_root=policy.destination_root,
            membership_plans=membership_plans,
        )
        core: dict[str, object] = {
            "story_arc_id": story_arc_id,
            "selected_membership_id": membership_id,
            "direction": direction,
            "expected_revision": expected_revision,
            "destination_root": policy.destination_root,
            "memberships": [asdict(item) for item in membership_plans],
            "placements": [asdict(item) for item in placement_plans],
        }
        digest = _plan_digest(core)
        # Temp names are derived only after the semantic plan is frozen, so the
        # digest cannot depend recursively on its own filename.
        placement_plans = tuple(
            replace(
                item,
                temporary_path=(
                    str(
                        Path(item.old_path).with_name(
                            ".pullbox-story-arc-reorder-"
                            f"{operation_token[:12]}-{item.placement_id}.tmp"
                        )
                    )
                    if item.action == "rename"
                    else None
                ),
            )
            for item in placement_plans
        )
        _validate_complete_preview_paths(
            placement_plans,
            destination_root=policy.destination_root,
        )
        await _validate_database_preview_paths(session, placement_plans)
        return _SignedPlan(
            story_arc_id=story_arc_id,
            selected_membership_id=membership_id,
            direction=direction,
            expected_revision=expected_revision,
            operation_token=operation_token,
            plan_digest=digest,
            destination_root=policy.destination_root,
            memberships=membership_plans,
            placements=placement_plans,
        )

    async def _build_placement_plans(
        self,
        session: AsyncSession,
        *,
        arc: StoryArc,
        policy_mode: StoryArcPlacementPolicyMode,
        destination_root: str | None,
        membership_plans: tuple[_MembershipPlan, _MembershipPlan],
    ) -> tuple[_PlacementPlan, ...]:
        policy = _policy_from_arc(arc)
        contexts = {
            item.membership_id: await _load_one_context(session, arc.id, item.membership_id)
            for item in membership_plans
        }
        membership_ids = tuple(contexts)
        rows = list(
            (
                await session.scalars(
                    select(StoryArcPlacement)
                    .where(StoryArcPlacement.issue_story_arc_id.in_(membership_ids))
                    .order_by(StoryArcPlacement.id.asc())
                    .limit(_MAX_PLACEMENTS_PER_ADJACENT_MOVE + 1)
                )
            ).all()
        )
        if len(rows) > _MAX_PLACEMENTS_PER_ADJACENT_MOVE:
            raise StoryArcManagedReorderError(
                "placement_limit",
                "Adjacent reorder exceeds the bounded placement safety limit",
                category="safety",
            )
        by_membership: dict[int, list[StoryArcPlacement]] = {
            membership_id: [] for membership_id in membership_ids
        }
        for row in rows:
            by_membership[row.issue_story_arc_id].append(row)

        plans: list[_PlacementPlan] = []
        for membership in membership_plans:
            context = contexts[membership.membership_id]
            old_rendered = (
                _rendered_target_path(context, policy)
                if policy_mode is not StoryArcPlacementPolicyMode.LOGICAL
                and destination_root is not None
                else None
            )
            new_context = replace(
                context,
                sequence_number=membership.new_sequence_number,
            )
            new_rendered = (
                _rendered_target_path(new_context, policy)
                if policy_mode is not StoryArcPlacementPolicyMode.LOGICAL
                and destination_root is not None
                else None
            )
            placement_rows = by_membership[membership.membership_id]
            if not placement_rows:
                plans.append(
                    _PlacementPlan(
                        placement_id=0,
                        membership_id=membership.membership_id,
                        ownership="logical",
                        mode="logical",
                        old_reading_order=membership.old_sequence_number,
                        new_reading_order=membership.new_sequence_number,
                        old_path="",
                        new_path="",
                        temporary_path=None,
                        rendered_path_after="",
                        action="logical_reorder",
                        canonical_path=None,
                        source_fingerprint={},
                        target_fingerprint={},
                        placement_state=None,
                        last_result_snapshot={},
                    )
                )
                continue
            for row in placement_rows:
                plans.append(
                    _placement_plan_from_row(
                        row,
                        membership=membership,
                        old_rendered=old_rendered,
                        new_rendered=new_rendered,
                        canonical_path=context.canonical_path,
                        policy_mode=policy_mode,
                        arc_policy_schema_version=arc.policy_schema_version,
                    )
                )
        return tuple(plans)

    def _preview_from_plan(self, plan: _SignedPlan) -> StoryArcReorderPreview:
        token = self._serializer().dumps(_plan_to_payload(plan))
        items = tuple(
            StoryArcReorderPreviewItem(
                membership_id=item.membership_id,
                placement_id=item.placement_id or None,
                ownership=item.ownership,
                mode=item.mode,
                old_reading_order=item.old_reading_order,
                new_reading_order=item.new_reading_order,
                old_path=item.old_path or None,
                new_path=item.new_path or None,
                rendered_path_after=item.rendered_path_after or None,
                temporary_path=item.temporary_path,
                action=item.action,
            )
            for item in plan.placements
        )
        return StoryArcReorderPreview(
            story_arc_id=plan.story_arc_id,
            membership_id=plan.selected_membership_id,
            direction=plan.direction,
            expected_revision=plan.expected_revision,
            preview_token=token,
            items=items,
            managed_rename_count=sum(item.action == "rename" for item in items),
            referenced_drift_count=sum(item.action == "referenced_drift" for item in items),
            referenced_preserved_count=sum(
                item.ownership == StoryArcPlacementOwnership.REFERENCED.value for item in items
            ),
            recovery_pending=False,
        )

    async def _verify_and_prepare(
        self,
        session: AsyncSession,
        plan: _SignedPlan,
    ) -> None:
        arc = await session.get(StoryArc, plan.story_arc_id)
        if arc is None:
            raise StoryArcManagedReorderError(
                "story_arc_not_found", "Story arc was not found", category="not_found"
            )
        if arc.revision != plan.expected_revision:
            raise StoryArcManagedReorderError(
                "revision_conflict",
                "Story arc changed after the preview was generated",
                category="conflict",
            )
        memberships = {
            row.id: row
            for row in (
                await session.scalars(
                    select(IssueStoryArc).where(
                        IssueStoryArc.id.in_(
                            tuple(item.membership_id for item in plan.memberships)
                        ),
                        IssueStoryArc.story_arc_id == plan.story_arc_id,
                    )
                )
            ).all()
        }
        if len(memberships) != len(plan.memberships):
            raise StoryArcManagedReorderError(
                "membership_not_found",
                "A previewed Story Arc membership no longer exists",
                category="not_found",
            )
        for expected in plan.memberships:
            current = memberships[expected.membership_id]
            if (
                current.sequence_number != expected.old_sequence_number
                or current.source_ordinal != expected.old_source_ordinal
            ):
                raise StoryArcManagedReorderError(
                    "revision_conflict",
                    "Story Arc order changed after the preview was generated",
                    category="conflict",
                )

        persisted_plans = [item for item in plan.placements if item.placement_id > 0]
        managed_plans = [item for item in persisted_plans if item.action == "rename"]
        placement_ids = tuple(item.placement_id for item in persisted_plans)
        placements = {
            row.id: row
            for row in (
                await session.scalars(
                    select(StoryArcPlacement).where(StoryArcPlacement.id.in_(placement_ids))
                )
            ).all()
        }
        if len(placements) != len(persisted_plans):
            raise StoryArcManagedReorderError(
                "placement_changed",
                "A Story Arc placement no longer matches the preview",
                category="ownership",
            )
        for placement_plan in persisted_plans:
            row = placements[placement_plan.placement_id]
            if placement_plan.action == "rename":
                continue
            _validate_nonrenamed_row_matches_plan(row, placement_plan)
            if placement_plan.action == "managed_unchanged":
                _validate_artifact_at_old_path(placement_plan)

        if not managed_plans:
            await session.rollback()
            return
        already_prepared = True
        for placement_plan in managed_plans:
            row = placements[placement_plan.placement_id]
            journal = dict(row.last_result or {})
            is_same_prepared = (
                row.operation_token == plan.operation_token
                and journal.get("status") in _ACTIVE_REORDER_STATUSES
                and journal.get("plan_digest") == plan.plan_digest
                and journal.get("operation_token") == plan.operation_token
                and journal.get("old_path") == placement_plan.old_path
                and journal.get("new_path") == placement_plan.new_path
                and journal.get("temporary_path") == placement_plan.temporary_path
                and journal.get("old_reading_order") == placement_plan.old_reading_order
                and journal.get("new_reading_order") == placement_plan.new_reading_order
                and journal.get("target_fingerprint") == placement_plan.target_fingerprint
                and row.issue_story_arc_id == placement_plan.membership_id
                and row.ownership is StoryArcPlacementOwnership.MANAGED
                and row.mode.value == placement_plan.mode
                and _normal_path(row.placement_path) == _normal_path(placement_plan.old_path)
            )
            if not is_same_prepared:
                already_prepared = False
                _validate_managed_row_matches_plan(row, placement_plan)
                _validate_artifact_at_old_path(placement_plan)

        if already_prepared:
            # End the verification transaction before resuming any filesystem
            # work from the durable prepared journal.
            await session.rollback()
            return
        if any(placements[item.placement_id].operation_token for item in managed_plans):
            raise StoryArcManagedReorderError(
                "reorder_recovery_required",
                "A managed placement has an unfinished operation",
                category="recovery",
            )

        now = datetime.now(UTC)
        coordinator_placement_id = min(item.placement_id for item in managed_plans)
        recovery_token = self._serializer().dumps(_plan_to_payload(plan))
        for placement_plan in managed_plans:
            row = placements[placement_plan.placement_id]
            previous = dict(row.last_result or {})
            journal_payload: dict[str, object] = {
                "schema_version": _JOURNAL_SCHEMA_VERSION,
                "status": "rename_prepared",
                "operation": "story_arc_reorder",
                "operation_token": plan.operation_token,
                "plan_digest": plan.plan_digest,
                "coordinator": placement_plan.placement_id == coordinator_placement_id,
                "coordinator_placement_id": coordinator_placement_id,
                "old_path": placement_plan.old_path,
                "new_path": placement_plan.new_path,
                "temporary_path": placement_plan.temporary_path,
                "old_reading_order": placement_plan.old_reading_order,
                "new_reading_order": placement_plan.new_reading_order,
                "target_fingerprint": dict(placement_plan.target_fingerprint),
                "previous_result": previous,
            }
            if placement_plan.placement_id == coordinator_placement_id:
                # The browser-held confirmation token is not restart truth.
                # Keep one signed, bounded coordinator payload in the durable
                # journal so a fresh process can discover and reissue it.
                journal_payload["recovery_token"] = recovery_token
            result = await session.execute(
                sa_update(StoryArcPlacement)
                .where(
                    StoryArcPlacement.id == placement_plan.placement_id,
                    StoryArcPlacement.issue_story_arc_id == placement_plan.membership_id,
                    StoryArcPlacement.ownership == StoryArcPlacementOwnership.MANAGED,
                    StoryArcPlacement.state == StoryArcPlacementState.CURRENT,
                    StoryArcPlacement.operation_token.is_(None),
                    StoryArcPlacement.placement_path == placement_plan.old_path,
                    StoryArcPlacement.rendered_reading_order == placement_plan.old_reading_order,
                )
                .values(
                    operation_token=plan.operation_token,
                    last_checked_at=now,
                    last_result=journal_payload,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                await session.rollback()
                raise StoryArcManagedReorderError(
                    "reorder_operation_superseded",
                    "Managed placement was reserved by another operation",
                    category="conflict",
                )
        await session.commit()

    async def _reconcile_success(
        self,
        session: AsyncSession,
        plan: _SignedPlan,
        filesystem_result: _FilesystemResult,
    ) -> int:
        arc = await session.get(StoryArc, plan.story_arc_id)
        if arc is None or arc.revision != plan.expected_revision:
            raise StoryArcManagedReorderError(
                "revision_conflict",
                "Story Arc changed before filesystem reconciliation",
                category="conflict",
            )
        placement_ids = tuple(
            item.placement_id for item in plan.placements if item.placement_id > 0
        )
        placements = {
            row.id: row
            for row in (
                await session.scalars(
                    select(StoryArcPlacement).where(StoryArcPlacement.id.in_(placement_ids))
                )
            ).all()
        }
        changed_nonrenamed: set[int] = set()
        for item in plan.placements:
            if item.placement_id == 0:
                continue
            row = placements.get(item.placement_id)
            if row is None:
                if item.action == "rename":
                    raise StoryArcManagedReorderError(
                        "managed_placement_changed",
                        "A managed placement disappeared before reconciliation",
                        category="conflict",
                    )
                changed_nonrenamed.add(item.placement_id)
                continue
            if item.action == "rename":
                journal = dict(row.last_result or {})
                if (
                    row.operation_token != plan.operation_token
                    or journal.get("status") not in _ACTIVE_REORDER_STATUSES
                    or journal.get("plan_digest") != plan.plan_digest
                    or journal.get("operation_token") != plan.operation_token
                    or journal.get("old_path") != item.old_path
                    or journal.get("new_path") != item.new_path
                    or journal.get("temporary_path") != item.temporary_path
                    or _normal_path(row.placement_path) != _normal_path(item.old_path)
                ):
                    raise StoryArcManagedReorderError(
                        "reorder_operation_superseded",
                        "Managed placement operation was superseded",
                        category="conflict",
                    )
            else:
                try:
                    _validate_nonrenamed_row_matches_plan(row, item)
                except StoryArcManagedReorderError:
                    # The reorder never owns referenced or path-unchanged rows.
                    # Preserve a concurrent inspection/repair/delete verbatim;
                    # its stale rendered-order evidence remains truthful drift
                    # for normal synchronization to revisit.
                    changed_nonrenamed.add(item.placement_id)

        # Break database path uniqueness inside the same post-filesystem
        # transaction, then publish the actual final paths before commit.
        for item in plan.placements:
            if item.action == "rename":
                row = placements[item.placement_id]
                if item.temporary_path is None:
                    raise StoryArcManagedReorderError(
                        "invalid_preview_token", "Reorder temporary path is missing"
                    )
                row.placement_path = item.temporary_path
        await session.flush()

        now = datetime.now(UTC)
        for item in plan.placements:
            if item.placement_id == 0:
                continue
            if item.placement_id in changed_nonrenamed:
                continue
            row = placements[item.placement_id]
            if item.action == "rename":
                row.placement_path = item.new_path
                row.rendered_reading_order = item.new_reading_order
                row.policy_schema_version = STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION
                row.operation_token = None
                row.state = StoryArcPlacementState.CURRENT
                row.last_checked_at = now
                row.last_result = {
                    "schema_version": _JOURNAL_SCHEMA_VERSION,
                    "status": "complete",
                    "outcome": "reordered",
                    "plan_digest": plan.plan_digest,
                    "target_fingerprint": dict(filesystem_result.fingerprints[item.placement_id]),
                }
            elif item.action == "managed_unchanged":
                row.rendered_reading_order = item.new_reading_order
                row.state = StoryArcPlacementState.CURRENT
                row.last_checked_at = now
                row.last_result = {
                    "schema_version": _JOURNAL_SCHEMA_VERSION,
                    "status": "complete",
                    "outcome": "reading_order_updated",
                    "plan_digest": plan.plan_digest,
                    "target_fingerprint": dict(item.target_fingerprint),
                }
            elif item.action in {"referenced_drift", "referenced_unchanged"}:
                row.rendered_reading_order = item.new_reading_order
                if item.action == "referenced_drift":
                    row.state = StoryArcPlacementState.DRIFTED
                prior = dict(row.last_result or {})
                row.last_result = {
                    **prior,
                    "schema_version": _JOURNAL_SCHEMA_VERSION,
                    "status": "referenced_preserved",
                    "desired_path": item.rendered_path_after,
                    "artifact_mutated": False,
                }

        memberships = {
            row.id: row
            for row in (
                await session.scalars(
                    select(IssueStoryArc).where(
                        IssueStoryArc.id.in_(tuple(item.membership_id for item in plan.memberships))
                    )
                )
            ).all()
        }
        for membership_plan in plan.memberships:
            membership = memberships[membership_plan.membership_id]
            membership.sequence_number = membership_plan.new_sequence_number
            membership.source_ordinal = membership_plan.new_source_ordinal
        arc.revision += 1
        await session.commit()
        return arc.revision

    async def _has_active_journal(
        self,
        session: AsyncSession,
        plan: _SignedPlan,
    ) -> bool:
        row = await session.scalar(
            select(StoryArcPlacement)
            .where(StoryArcPlacement.operation_token == plan.operation_token)
            .order_by(StoryArcPlacement.id)
            .limit(1)
        )
        if row is None:
            return False
        journal = dict(row.last_result or {})
        return (
            journal.get("status") in _ACTIVE_REORDER_STATUSES
            and journal.get("operation") == "story_arc_reorder"
            and journal.get("operation_token") == plan.operation_token
            and journal.get("plan_digest") == plan.plan_digest
        )

    async def _raise_after_semantic_failure(
        self,
        session: AsyncSession,
        plan: _SignedPlan,
        exc: StoryArcManagedReorderError,
    ) -> NoReturn:
        restored = await asyncio.to_thread(_restore_old_paths, plan)
        if restored:
            await self._record_rolled_back(session, plan, status="rename_failed")
            raise StoryArcManagedReorderError(
                exc.code,
                f"{exc}; managed placement paths were restored",
                category=exc.category,
            ) from exc
        await self._record_recovery_required(session, plan, exc)
        raise StoryArcManagedReorderError(
            "reorder_recovery_required",
            "Story Arc reorder needs recovery before another move can begin",
            category="recovery",
        ) from exc

    async def _record_rolled_back(
        self,
        session: AsyncSession,
        plan: _SignedPlan,
        *,
        status: Literal["rename_cancelled", "rename_failed"],
    ) -> None:
        rows = list(
            (
                await session.scalars(
                    select(StoryArcPlacement).where(
                        StoryArcPlacement.operation_token == plan.operation_token
                    )
                )
            ).all()
        )
        now = datetime.now(UTC)
        by_id = {item.placement_id: item for item in plan.placements}
        for row in rows:
            expected = by_id.get(row.id)
            if expected is None:
                continue
            row.operation_token = None
            row.state = StoryArcPlacementState.CURRENT
            row.last_checked_at = now
            row.last_result = {
                "schema_version": _JOURNAL_SCHEMA_VERSION,
                "status": status,
                "plan_digest": plan.plan_digest,
                "target_fingerprint": dict(expected.target_fingerprint),
                "retryable": True,
            }
        await session.commit()

    async def _record_recovery_required(
        self,
        session: AsyncSession,
        plan: _SignedPlan,
        exc: BaseException,
    ) -> None:
        rows = list(
            (
                await session.scalars(
                    select(StoryArcPlacement).where(
                        StoryArcPlacement.operation_token == plan.operation_token
                    )
                )
            ).all()
        )
        for row in rows:
            previous = dict(row.last_result or {})
            row.state = StoryArcPlacementState.DRIFTED
            row.last_checked_at = datetime.now(UTC)
            row.last_result = {
                **previous,
                "schema_version": _JOURNAL_SCHEMA_VERSION,
                "status": "rename_recovery_required",
                "plan_digest": plan.plan_digest,
                "error_type": type(exc).__name__,
                "retry_with_same_preview": True,
            }
        await session.commit()

    def _decode_plan(self, token: str) -> _SignedPlan:
        if not token:
            raise StoryArcManagedReorderError(
                "confirmation_required", "A reorder preview must be confirmed"
            )
        try:
            raw = self._serializer().loads(token, max_age=_TOKEN_MAX_AGE_SECONDS)
        except SignatureExpired as exc:
            raise StoryArcManagedReorderError(
                "preview_expired", "The reorder preview expired; generate a new preview"
            ) from exc
        except BadSignature as exc:
            raise StoryArcManagedReorderError(
                "invalid_preview_token", "The reorder preview token is invalid"
            ) from exc
        try:
            return _plan_from_payload(raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise StoryArcManagedReorderError(
                "invalid_preview_token", "The reorder preview token is invalid"
            ) from exc

    def _decode_durable_plan(self, token: str) -> _SignedPlan:
        """Decode a DB-held signed recovery token without browser TTL expiry."""
        try:
            raw = self._serializer().loads(token)
        except BadSignature as exc:
            raise StoryArcManagedReorderError(
                "reorder_recovery_required",
                "Prepared Story Arc reorder recovery signature is invalid",
                category="recovery",
            ) from exc
        try:
            return _plan_from_payload(raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise StoryArcManagedReorderError(
                "reorder_recovery_required",
                "Prepared Story Arc reorder recovery payload is invalid",
                category="recovery",
            ) from exc

    @staticmethod
    def _serializer() -> URLSafeTimedSerializer:
        return URLSafeTimedSerializer(get_application_secret(), salt=_TOKEN_SALT)


def _pending_coordinator_statement(
    story_arc_id: int,
) -> Select[tuple[StoryArcPlacement]]:
    """Return the bounded, portable durable-reorder discovery query."""
    return (
        select(StoryArcPlacement)
        .join(
            IssueStoryArc,
            StoryArcPlacement.issue_story_arc_id == IssueStoryArc.id,
        )
        .where(
            IssueStoryArc.story_arc_id == story_arc_id,
            StoryArcPlacement.operation_token.is_not(None),
            StoryArcPlacement.last_result["operation"].as_string() == "story_arc_reorder",
            StoryArcPlacement.last_result["coordinator"].as_boolean().is_(True),
        )
        .order_by(StoryArcPlacement.id)
        .limit(1)
    )


async def _load_adjacent_membership(
    session: AsyncSession,
    *,
    selected: IssueStoryArc,
    direction: StoryArcReorderDirection,
) -> IssueStoryArc | None:
    key = (
        IssueStoryArc.sequence_number,
        IssueStoryArc.source_ordinal,
        IssueStoryArc.id,
    )
    before = or_(
        IssueStoryArc.sequence_number < selected.sequence_number,
        and_(
            IssueStoryArc.sequence_number == selected.sequence_number,
            IssueStoryArc.source_ordinal < selected.source_ordinal,
        ),
        and_(
            IssueStoryArc.sequence_number == selected.sequence_number,
            IssueStoryArc.source_ordinal == selected.source_ordinal,
            IssueStoryArc.id < selected.id,
        ),
    )
    after = or_(
        IssueStoryArc.sequence_number > selected.sequence_number,
        and_(
            IssueStoryArc.sequence_number == selected.sequence_number,
            IssueStoryArc.source_ordinal > selected.source_ordinal,
        ),
        and_(
            IssueStoryArc.sequence_number == selected.sequence_number,
            IssueStoryArc.source_ordinal == selected.source_ordinal,
            IssueStoryArc.id > selected.id,
        ),
    )
    statement = select(IssueStoryArc).where(
        IssueStoryArc.story_arc_id == selected.story_arc_id,
        before if direction == "up" else after,
    )
    statement = statement.order_by(
        *(column.desc() for column in key)
        if direction == "up"
        else (column.asc() for column in key)
    )
    return cast("IssueStoryArc | None", await session.scalar(statement.limit(1)))


def _placement_plan_from_row(
    row: StoryArcPlacement,
    *,
    membership: _MembershipPlan,
    old_rendered: str | None,
    new_rendered: str | None,
    canonical_path: str | None,
    policy_mode: StoryArcPlacementPolicyMode,
    arc_policy_schema_version: int | None,
) -> _PlacementPlan:
    ownership = row.ownership.value
    if row.ownership is StoryArcPlacementOwnership.REFERENCED:
        desired = new_rendered or row.placement_path
        action: StoryArcReorderAction = (
            "referenced_unchanged"
            if os.path.normcase(os.path.abspath(row.placement_path))
            == os.path.normcase(os.path.abspath(desired))
            else "referenced_drift"
        )
        return _PlacementPlan(
            placement_id=row.id,
            membership_id=row.issue_story_arc_id,
            ownership=ownership,
            mode=row.mode.value,
            old_reading_order=membership.old_sequence_number,
            new_reading_order=membership.new_sequence_number,
            old_path=row.placement_path,
            new_path=row.placement_path,
            temporary_path=None,
            rendered_path_after=desired,
            action=action,
            canonical_path=canonical_path,
            source_fingerprint=dict(row.source_fingerprint or {}),
            target_fingerprint=_stored_target_fingerprint(row),
            placement_state=row.state.value,
            last_result_snapshot=dict(row.last_result or {}),
        )

    if old_rendered is None or new_rendered is None:
        raise StoryArcManagedReorderError(
            "managed_policy_missing",
            "Managed placement has no active rendered policy",
            category="ownership",
        )
    if (
        row.mode.value != policy_mode.value
        or row.policy_schema_version != arc_policy_schema_version
        or row.rendered_reading_order != membership.old_sequence_number
        or _normal_path(row.placement_path) != _normal_path(old_rendered)
    ):
        raise StoryArcManagedReorderError(
            "managed_placement_changed",
            "Managed placement no longer matches its rendered policy",
            category="ownership",
        )
    target_fingerprint = _stored_target_fingerprint(row)
    if (
        row.state is not StoryArcPlacementState.CURRENT
        or row.operation_token is not None
        or not row.source_fingerprint
        or not target_fingerprint
        or dict(row.last_result or {}).get("status") not in _UNCHANGED_MANAGED_STATUSES
    ):
        raise StoryArcManagedReorderError(
            "managed_placement_changed",
            "Managed placement lacks unchanged action-owned evidence",
            category="ownership",
        )
    action = (
        "managed_unchanged"
        if _normal_path(old_rendered) == _normal_path(new_rendered)
        else "rename"
    )
    _validate_artifact_fingerprint(
        Path(row.placement_path),
        target_fingerprint,
        canonical_path=Path(canonical_path) if canonical_path else None,
        mode=row.mode,
    )
    return _PlacementPlan(
        placement_id=row.id,
        membership_id=row.issue_story_arc_id,
        ownership=ownership,
        mode=row.mode.value,
        old_reading_order=membership.old_sequence_number,
        new_reading_order=membership.new_sequence_number,
        old_path=row.placement_path,
        new_path=new_rendered,
        temporary_path=None,
        rendered_path_after=new_rendered,
        action=action,
        canonical_path=canonical_path,
        source_fingerprint=dict(row.source_fingerprint),
        target_fingerprint=target_fingerprint,
        placement_state=row.state.value,
        last_result_snapshot=dict(row.last_result or {}),
    )


def _validate_complete_preview_paths(
    plans: Sequence[_PlacementPlan],
    *,
    destination_root: str | None,
) -> None:
    managed = [item for item in plans if item.action == "rename"]
    if not managed:
        return
    if destination_root is None:
        raise StoryArcManagedReorderError(
            "managed_policy_missing", "Managed reorder destination root is missing"
        )
    root = Path(destination_root)
    old_paths = {_normal_path(item.old_path) for item in managed}
    new_paths: set[str] = set()
    canonical_paths = {
        _normal_path(item.canonical_path) for item in managed if item.canonical_path is not None
    }
    if len(managed) > _MAX_COLLISION_SCAN_PATHS:
        raise StoryArcManagedReorderError(
            "placement_limit", "Reorder collision scan exceeds its bounded limit"
        )
    for item in managed:
        old = Path(item.old_path)
        new = Path(item.new_path)
        _validate_target_lexically(root, old)
        _validate_target_lexically(root, new)
        _validate_path_limits(old)
        _validate_path_limits(new)
        if _normal_path(old) in canonical_paths or _normal_path(new) in canonical_paths:
            raise StoryArcManagedReorderError(
                "canonical_destination",
                "Reorder cannot mutate a canonical library artifact",
                category="safety",
            )
        if item.canonical_path is not None:
            _reject_canonical_destination(Path(item.canonical_path), old)
            _reject_canonical_destination(Path(item.canonical_path), new)
        if item.temporary_path is not None:
            temporary = Path(item.temporary_path)
            _validate_target_lexically(root, temporary)
            _validate_path_limits(temporary)
            if item.canonical_path is not None:
                _reject_canonical_destination(Path(item.canonical_path), temporary)
            if _path_exists(temporary) or _case_only_collision(temporary) is not None:
                raise StoryArcManagedReorderError(
                    "temporary_collision",
                    "A reorder recovery checkpoint path is already occupied",
                    category="collision",
                )
        normalized_new = _normal_path(new)
        if normalized_new in new_paths:
            raise StoryArcManagedReorderError(
                "destination_collision",
                "Two managed placements render to the same destination",
                category="collision",
            )
        new_paths.add(normalized_new)
        if normalized_new not in old_paths and _path_exists(new):
            raise StoryArcManagedReorderError(
                "destination_collision",
                "A rendered reorder destination already exists",
                category="collision",
            )
        case_collision = _case_only_collision(new)
        if case_collision is not None and _normal_path(case_collision) not in old_paths:
            raise StoryArcManagedReorderError(
                "case_only_collision",
                "A case-only reorder destination collision exists",
                category="collision",
            )


async def _validate_database_preview_paths(
    session: AsyncSession,
    plans: Sequence[_PlacementPlan],
) -> None:
    """Reject canonical or independently tracked paths with two bounded queries."""
    managed = [item for item in plans if item.action == "rename"]
    if not managed:
        return
    candidate_paths = tuple(
        dict.fromkeys(
            raw_path
            for item in managed
            for raw_path in (item.old_path, item.new_path, item.temporary_path)
            if raw_path is not None
        )
    )
    canonical = await session.scalar(
        select(LibraryFile.file_path).where(LibraryFile.file_path.in_(candidate_paths)).limit(1)
    )
    if canonical is not None:
        raise StoryArcManagedReorderError(
            "canonical_destination",
            "Reorder cannot mutate a canonical library artifact",
            category="safety",
        )
    planned_ids = {item.placement_id for item in managed}
    occupied_rows = (
        await session.execute(
            select(StoryArcPlacement.id, StoryArcPlacement.placement_path).where(
                StoryArcPlacement.placement_path.in_(
                    tuple(
                        raw_path
                        for item in managed
                        for raw_path in (item.new_path, item.temporary_path)
                        if raw_path is not None
                    )
                )
            )
        )
    ).all()
    temporary_paths = {item.temporary_path for item in managed if item.temporary_path is not None}
    if any(
        row_path in temporary_paths or row_id not in planned_ids
        for row_id, row_path in occupied_rows
    ):
        raise StoryArcManagedReorderError(
            "destination_collision",
            "A rendered reorder destination belongs to another placement",
            category="collision",
        )


def _validate_managed_row_matches_plan(
    row: StoryArcPlacement,
    expected: _PlacementPlan,
) -> None:
    if (
        row.issue_story_arc_id != expected.membership_id
        or row.ownership is not StoryArcPlacementOwnership.MANAGED
        or row.mode.value != expected.mode
        or row.state is not StoryArcPlacementState.CURRENT
        or row.operation_token is not None
        or row.rendered_reading_order != expected.old_reading_order
        or _normal_path(row.placement_path) != _normal_path(expected.old_path)
        or dict(row.source_fingerprint or {}) != expected.source_fingerprint
        or _stored_target_fingerprint(row) != expected.target_fingerprint
        or dict(row.last_result or {}).get("status") not in _UNCHANGED_MANAGED_STATUSES
    ):
        raise StoryArcManagedReorderError(
            "managed_placement_changed",
            "Managed placement no longer matches its action-owned preview evidence",
            category="ownership",
        )


def _validate_nonrenamed_row_matches_plan(
    row: StoryArcPlacement,
    expected: _PlacementPlan,
) -> None:
    if (
        row.issue_story_arc_id != expected.membership_id
        or row.ownership.value != expected.ownership
        or row.mode.value != expected.mode
        or row.operation_token is not None
        or _normal_path(row.placement_path) != _normal_path(expected.old_path)
        or dict(row.source_fingerprint or {}) != expected.source_fingerprint
        or _stored_target_fingerprint(row) != expected.target_fingerprint
        or row.state.value != expected.placement_state
        or dict(row.last_result or {}) != expected.last_result_snapshot
    ):
        raise StoryArcManagedReorderError(
            "placement_changed",
            "Story Arc placement changed after the preview was generated",
            category="ownership",
        )
    if expected.action == "managed_unchanged" and (
        row.ownership is not StoryArcPlacementOwnership.MANAGED
        or row.state is not StoryArcPlacementState.CURRENT
        or row.rendered_reading_order != expected.old_reading_order
        or dict(row.last_result or {}).get("status") not in _UNCHANGED_MANAGED_STATUSES
    ):
        raise StoryArcManagedReorderError(
            "managed_placement_changed",
            "Managed placement lacks unchanged action-owned evidence",
            category="ownership",
        )


def _validate_artifact_at_old_path(expected: _PlacementPlan) -> None:
    _validate_artifact_fingerprint(
        Path(expected.old_path),
        expected.target_fingerprint,
        canonical_path=(
            Path(expected.canonical_path) if expected.canonical_path is not None else None
        ),
        mode=StoryArcPlacementMode(expected.mode),
    )


def _validate_artifact_fingerprint(
    path: Path,
    expected: Fingerprint,
    *,
    canonical_path: Path | None,
    mode: StoryArcPlacementMode,
) -> None:
    try:
        if not _path_exists(path):
            raise StoryArcManagedReorderError(
                "managed_placement_changed",
                "Managed placement is missing from its previewed path",
                category="ownership",
            )
        _validate_removal_representation(path, mode=mode)
        actual = _fingerprint_target(path)
    except StoryArcManagedReorderError:
        raise
    except (OSError, StoryArcPlacementError) as exc:
        raise StoryArcManagedReorderError(
            "managed_placement_changed",
            "Managed placement could not be validated safely",
            category="ownership",
        ) from exc
    if actual != expected:
        raise StoryArcManagedReorderError(
            "managed_placement_changed",
            "Managed placement changed after its ownership evidence was recorded",
            category="ownership",
        )
    if canonical_path is not None:
        _reject_canonical_destination(canonical_path, path)


def _validate_removal_representation(path: Path, *, mode: StoryArcPlacementMode) -> None:
    root_guard = _validated_root(_common_destination_root(path))
    with _open_secure_parent_directory(root_guard, path.parent, create=False) as parent:
        _validate_removal_representation_at(mode, parent, path.name)


def _common_destination_root(path: Path) -> Path:
    # This helper is used only for a read-only type check before the signed plan
    # has its policy root attached.  Opening the immediate real parent still
    # rejects a symlink parent and pins the exact entry for fingerprinting.
    return path.parent


def _complete_filesystem_plan(
    plan: _SignedPlan,
    cancellation_requested: Callable[[], bool] | None,
) -> _FilesystemResult:
    managed = [item for item in plan.placements if item.action == "rename"]
    if not managed:
        return _FilesystemResult(fingerprints={})
    if plan.destination_root is None:
        raise StoryArcManagedReorderError(
            "managed_policy_missing", "Managed reorder destination root is missing"
        )
    _validate_plan_candidates(plan, managed)
    try:
        if cancellation_requested is not None and cancellation_requested():
            raise _CancelledDuringReorderError
        locations = _locate_managed_artifacts(
            managed,
            root=Path(plan.destination_root),
            prefer="new",
        )
        for item in managed:
            if cancellation_requested is not None and cancellation_requested():
                raise _CancelledDuringReorderError
            location = locations[item.placement_id]
            if location == item.old_path:
                if item.temporary_path is None:
                    raise StoryArcManagedReorderError(
                        "invalid_preview_token", "Reorder temporary path is missing"
                    )
                _exclusive_move(
                    root=Path(plan.destination_root),
                    source=Path(item.old_path),
                    destination=Path(item.temporary_path),
                    expected=item.target_fingerprint,
                    canonical_path=(
                        Path(item.canonical_path) if item.canonical_path is not None else None
                    ),
                    mode=StoryArcPlacementMode(item.mode),
                    create_destination_parent=False,
                )
                locations[item.placement_id] = item.temporary_path

        if cancellation_requested is not None and cancellation_requested():
            raise _CancelledDuringReorderError

        for item in managed:
            if cancellation_requested is not None and cancellation_requested():
                raise _CancelledDuringReorderError
            location = locations[item.placement_id]
            if location == item.new_path:
                continue
            if location != item.temporary_path or item.temporary_path is None:
                raise StoryArcManagedReorderError(
                    "reorder_recovery_required",
                    "Managed reorder artifact has an unexpected recovery location",
                    category="recovery",
                )
            _exclusive_move(
                root=Path(plan.destination_root),
                source=Path(item.temporary_path),
                destination=Path(item.new_path),
                expected=item.target_fingerprint,
                canonical_path=(
                    Path(item.canonical_path) if item.canonical_path is not None else None
                ),
                mode=StoryArcPlacementMode(item.mode),
                create_destination_parent=True,
            )
            locations[item.placement_id] = item.new_path
    except _CancelledDuringReorderError as exc:
        if not _restore_old_paths(plan):
            raise StoryArcManagedReorderError(
                "reorder_recovery_required",
                "Cancelled reorder could not restore every old placement path",
                category="recovery",
            ) from exc
        raise

    final_fingerprints: dict[int, Fingerprint] = {}
    for item in managed:
        final_fingerprint = _fingerprint_target(Path(item.new_path))
        if final_fingerprint != item.target_fingerprint:
            raise StoryArcManagedReorderError(
                "managed_placement_changed",
                "Managed placement changed before reorder reconciliation",
                category="recovery",
            )
        final_fingerprints[item.placement_id] = final_fingerprint
    return _FilesystemResult(fingerprints=final_fingerprints)


def _validate_plan_candidates(plan: _SignedPlan, managed: Sequence[_PlacementPlan]) -> None:
    if not managed:
        return
    if plan.destination_root is None:
        raise StoryArcManagedReorderError(
            "managed_policy_missing", "Managed reorder destination root is missing"
        )
    root = Path(plan.destination_root)
    root_guard = _validated_root(root)
    del root_guard
    candidate_paths: list[Path] = []
    for item in managed:
        if item.temporary_path is None:
            raise StoryArcManagedReorderError(
                "invalid_preview_token", "Reorder temporary path is missing"
            )
        for raw_path in (item.old_path, item.temporary_path, item.new_path):
            path = Path(raw_path)
            _validate_target_lexically(root, path)
            _validate_path_limits(path)
            candidate_paths.append(path)
            if item.canonical_path is not None:
                _reject_canonical_destination(Path(item.canonical_path), path)
    normalized = [_normal_path(path) for path in candidate_paths]
    if len(normalized) != len(set(normalized)):
        # Old/new overlap between distinct placements is valid; a temp path is
        # never allowed to overlap anything else.
        temporary = {_normal_path(cast("str", item.temporary_path)) for item in managed}
        non_temporary = {
            _normal_path(path) for item in managed for path in (item.old_path, item.new_path)
        }
        if temporary & non_temporary or len(temporary) != len(managed):
            raise StoryArcManagedReorderError(
                "invalid_preview_token", "Reorder paths overlap unsafely"
            )


def _locate_managed_artifacts(
    plans: Sequence[_PlacementPlan],
    *,
    root: Path,
    prefer: Literal["new", "old"],
) -> dict[int, str]:
    candidate_paths = {
        path
        for item in plans
        for path in (item.old_path, item.temporary_path, item.new_path)
        if path is not None
    }
    fingerprints: dict[str, Fingerprint] = {}
    for raw_path in candidate_paths:
        path = Path(raw_path)
        if _path_exists(path):
            fingerprints[raw_path] = _fingerprint_target(path)
    locations: dict[int, str] = {}
    claimed: set[str] = set()
    for item in plans:
        candidates = [
            raw_path
            for raw_path in (item.old_path, item.temporary_path, item.new_path)
            if raw_path is not None and fingerprints.get(raw_path) == item.target_fingerprint
        ]
        if not candidates:
            raise StoryArcManagedReorderError(
                "managed_placement_changed",
                "Managed placement is missing or changed during recovery",
                category="recovery",
            )
        preference = (
            (item.new_path, item.temporary_path, item.old_path)
            if prefer == "new"
            else (item.old_path, item.temporary_path, item.new_path)
        )
        selected = next(path for path in preference if path in candidates)
        if selected in claimed:
            raise StoryArcManagedReorderError(
                "managed_placement_changed",
                "Two managed placements claim the same recovery artifact",
                category="recovery",
            )
        # ``_exclusive_move`` publishes by an exclusive hard-link followed by
        # unlink.  A process death between those syscalls leaves two names for
        # the exact recorded inode.  The signed plan and exact fingerprint make
        # it safe to finish that interrupted step by keeping the furthest-safe
        # name for the requested direction and unlinking only its duplicates.
        for duplicate in candidates:
            if duplicate == selected:
                continue
            _unlink_exact_duplicate(
                root=root,
                path=Path(duplicate),
                expected=item.target_fingerprint,
                canonical_path=(
                    Path(item.canonical_path) if item.canonical_path is not None else None
                ),
                mode=StoryArcPlacementMode(item.mode),
            )
            fingerprints.pop(duplicate, None)
        locations[item.placement_id] = selected
        claimed.add(selected)
    return locations


def _unlink_exact_duplicate(
    *,
    root: Path,
    path: Path,
    expected: Fingerprint,
    canonical_path: Path | None,
    mode: StoryArcPlacementMode,
) -> None:
    root_guard = _validated_root(root)
    _validate_target_lexically(root, path)
    if canonical_path is not None:
        _reject_canonical_destination(canonical_path, path)
    with _open_secure_parent_directory(
        root_guard,
        path.parent,
        create=False,
    ) as parent:
        _validate_removal_representation_at(mode, parent, path.name)
        actual = _fingerprint_target_at(
            parent,
            path.name,
            canonical_path=canonical_path,
        )
        if actual != expected:
            raise StoryArcManagedReorderError(
                "managed_placement_changed",
                "Duplicate recovery artifact changed before reconciliation",
                category="recovery",
            )
        os.unlink(path.name, dir_fd=parent.parent_fd)
        _fsync_directory(parent.parent_fd)


def _exclusive_move(
    *,
    root: Path,
    source: Path,
    destination: Path,
    expected: Fingerprint,
    canonical_path: Path | None,
    mode: StoryArcPlacementMode,
    create_destination_parent: bool,
) -> None:
    root_guard = _validated_root(root)
    _validate_target_lexically(root, source)
    _validate_target_lexically(root, destination)
    if canonical_path is not None:
        _reject_canonical_destination(canonical_path, source)
        _reject_canonical_destination(canonical_path, destination)
    with _open_secure_parent_directory(root_guard, source.parent, create=False) as source_parent:
        _validate_removal_representation_at(mode, source_parent, source.name)
        actual = _fingerprint_target_at(
            source_parent,
            source.name,
            canonical_path=canonical_path,
        )
        if actual != expected:
            raise StoryArcManagedReorderError(
                "managed_placement_changed",
                "Managed placement changed while the reorder was executing",
                category="ownership",
            )
        with _open_secure_parent_directory(
            root_guard,
            destination.parent,
            create=create_destination_parent,
        ) as destination_parent:
            if _entry_exists_at(destination_parent.parent_fd, destination.name):
                raise StoryArcManagedReorderError(
                    "destination_collision",
                    "Reorder destination was occupied before publication",
                    category="collision",
                )
            case_collision = _case_only_collision_at(
                destination_parent.parent_fd,
                destination.name,
            )
            if case_collision is not None:
                raise StoryArcManagedReorderError(
                    "case_only_collision",
                    "A case-only reorder destination collision exists",
                    category="collision",
                )
            link_published = False
            source_unlinked = False
            try:
                os.link(
                    source.name,
                    destination.name,
                    src_dir_fd=source_parent.parent_fd,
                    dst_dir_fd=destination_parent.parent_fd,
                    follow_symlinks=False,
                )
                link_published = True
                _fsync_directory(destination_parent.parent_fd)
                os.unlink(source.name, dir_fd=source_parent.parent_fd)
                source_unlinked = True
                _fsync_directory(source_parent.parent_fd)
            except BaseException:
                # Never remove a destination merely because it now exists.  A
                # foreign entry may have won the race after our precheck.  We
                # may undo only our own successfully published hard-link, and
                # only while the exact source inode still exists as the
                # authoritative copy.  Once source unlink succeeds, the
                # destination is the sole durable artifact and must be left for
                # journal-based restart reconciliation.
                if link_published and not source_unlinked:
                    with suppress(OSError, StoryArcPlacementError):
                        _cleanup_published_link_if_source_authoritative(
                            source_parent=source_parent,
                            source_name=source.name,
                            destination_parent=destination_parent,
                            destination_name=destination.name,
                            expected=expected,
                            canonical_path=canonical_path,
                            mode=mode,
                        )
                raise


def _cleanup_published_link_if_source_authoritative(
    *,
    source_parent: _SecureParentDirectory,
    source_name: str,
    destination_parent: _SecureParentDirectory,
    destination_name: str,
    expected: Fingerprint,
    canonical_path: Path | None,
    mode: StoryArcPlacementMode,
) -> None:
    """Undo only a proven duplicate link while the exact source still exists."""
    if not _entry_exists_at(source_parent.parent_fd, source_name) or not _entry_exists_at(
        destination_parent.parent_fd, destination_name
    ):
        return
    _validate_removal_representation_at(mode, source_parent, source_name)
    _validate_removal_representation_at(mode, destination_parent, destination_name)
    source_fingerprint = _fingerprint_target_at(
        source_parent,
        source_name,
        canonical_path=canonical_path,
    )
    destination_fingerprint = _fingerprint_target_at(
        destination_parent,
        destination_name,
        canonical_path=canonical_path,
    )
    if (
        source_fingerprint != expected
        or destination_fingerprint != expected
        or source_fingerprint.get("device") != destination_fingerprint.get("device")
        or source_fingerprint.get("inode") != destination_fingerprint.get("inode")
    ):
        return
    os.unlink(destination_name, dir_fd=destination_parent.parent_fd)
    _fsync_directory(destination_parent.parent_fd)


def _restore_old_paths(plan: _SignedPlan) -> bool:
    managed = [item for item in plan.placements if item.action == "rename"]
    if not managed or plan.destination_root is None:
        return True
    try:
        locations = _locate_managed_artifacts(
            managed,
            root=Path(plan.destination_root),
            prefer="old",
        )
        # First vacate every final destination.  Only then restore old paths,
        # which is safe even when the plan is a direct A/B swap.
        for item in reversed(managed):
            location = locations[item.placement_id]
            if location != item.new_path:
                continue
            if item.temporary_path is None:
                return False
            _exclusive_move(
                root=Path(plan.destination_root),
                source=Path(item.new_path),
                destination=Path(item.temporary_path),
                expected=item.target_fingerprint,
                canonical_path=(
                    Path(item.canonical_path) if item.canonical_path is not None else None
                ),
                mode=StoryArcPlacementMode(item.mode),
                create_destination_parent=False,
            )
            locations[item.placement_id] = item.temporary_path
        for item in reversed(managed):
            location = locations[item.placement_id]
            if location == item.old_path:
                continue
            if location != item.temporary_path or item.temporary_path is None:
                return False
            _exclusive_move(
                root=Path(plan.destination_root),
                source=Path(item.temporary_path),
                destination=Path(item.old_path),
                expected=item.target_fingerprint,
                canonical_path=(
                    Path(item.canonical_path) if item.canonical_path is not None else None
                ),
                mode=StoryArcPlacementMode(item.mode),
                create_destination_parent=True,
            )
        return True
    except (OSError, StoryArcPlacementError, StoryArcManagedReorderError):
        return False


def _stored_target_fingerprint(row: StoryArcPlacement) -> Fingerprint:
    raw = dict(row.last_result or {}).get("target_fingerprint")
    return dict(raw) if isinstance(raw, dict) else {}


def _plan_digest(value: dict[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _normal_path(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _assert_plan_binding(
    plan: _SignedPlan,
    *,
    story_arc_id: int,
    membership_id: int,
    direction: StoryArcReorderDirection,
    expected_revision: int,
) -> None:
    if (
        plan.story_arc_id != story_arc_id
        or plan.selected_membership_id != membership_id
        or plan.direction != direction
        or plan.expected_revision != expected_revision
    ):
        raise StoryArcManagedReorderError(
            "invalid_preview_token",
            "The reorder preview does not match this Story Arc move",
        )


def _plan_to_payload(plan: _SignedPlan) -> dict[str, object]:
    return {
        "schema_version": _TOKEN_SCHEMA_VERSION,
        "story_arc_id": plan.story_arc_id,
        "selected_membership_id": plan.selected_membership_id,
        "direction": plan.direction,
        "expected_revision": plan.expected_revision,
        "operation_token": plan.operation_token,
        "plan_digest": plan.plan_digest,
        "destination_root": plan.destination_root,
        "memberships": [asdict(item) for item in plan.memberships],
        "placements": [asdict(item) for item in plan.placements],
    }


def _plan_from_payload(raw: object) -> _SignedPlan:
    if not isinstance(raw, dict) or raw.get("schema_version") != _TOKEN_SCHEMA_VERSION:
        raise ValueError("invalid schema")
    memberships_raw = raw["memberships"]
    placements_raw = raw["placements"]
    if not isinstance(memberships_raw, list) or len(memberships_raw) != 2:
        raise ValueError("invalid memberships")
    if (
        not isinstance(placements_raw, list)
        or not placements_raw
        or len(placements_raw) > _MAX_PLACEMENTS_PER_ADJACENT_MOVE + 2
    ):
        raise ValueError("invalid placements")
    memberships = tuple(_membership_plan_from_raw(item) for item in memberships_raw)
    placements = tuple(_placement_plan_from_raw(item) for item in placements_raw)
    direction = raw["direction"]
    if direction not in {"up", "down"}:
        raise ValueError("invalid direction")
    destination_root = raw["destination_root"]
    if destination_root is not None and not isinstance(destination_root, str):
        raise ValueError("invalid root")
    plan = _SignedPlan(
        story_arc_id=_positive_int(raw["story_arc_id"]),
        selected_membership_id=_positive_int(raw["selected_membership_id"]),
        direction=cast("StoryArcReorderDirection", direction),
        expected_revision=_positive_int(raw["expected_revision"]),
        operation_token=_fixed_string(raw["operation_token"], length=32),
        plan_digest=_fixed_string(raw["plan_digest"], length=64),
        destination_root=destination_root,
        memberships=memberships,
        placements=placements,
    )
    semantic: dict[str, object] = {
        "story_arc_id": plan.story_arc_id,
        "selected_membership_id": plan.selected_membership_id,
        "direction": plan.direction,
        "expected_revision": plan.expected_revision,
        "destination_root": plan.destination_root,
        "memberships": [asdict(item) for item in plan.memberships],
        "placements": [{**asdict(item), "temporary_path": None} for item in plan.placements],
    }
    if _plan_digest(semantic) != plan.plan_digest:
        raise ValueError("digest mismatch")
    return plan


def _membership_plan_from_raw(raw: object) -> _MembershipPlan:
    if not isinstance(raw, dict) or set(raw) != {
        "membership_id",
        "old_sequence_number",
        "old_source_ordinal",
        "new_sequence_number",
        "new_source_ordinal",
    }:
        raise ValueError("invalid membership plan")
    return _MembershipPlan(
        membership_id=_positive_int(raw["membership_id"]),
        old_sequence_number=_nonnegative_int(raw["old_sequence_number"]),
        old_source_ordinal=_nonnegative_int(raw["old_source_ordinal"]),
        new_sequence_number=_nonnegative_int(raw["new_sequence_number"]),
        new_source_ordinal=_nonnegative_int(raw["new_source_ordinal"]),
    )


def _placement_plan_from_raw(raw: object) -> _PlacementPlan:
    expected_keys = {
        "placement_id",
        "membership_id",
        "ownership",
        "mode",
        "old_reading_order",
        "new_reading_order",
        "old_path",
        "new_path",
        "temporary_path",
        "rendered_path_after",
        "action",
        "canonical_path",
        "source_fingerprint",
        "target_fingerprint",
        "placement_state",
        "last_result_snapshot",
    }
    if not isinstance(raw, dict) or set(raw) != expected_keys:
        raise ValueError("invalid placement plan")
    action = raw["action"]
    if action not in {
        "rename",
        "managed_unchanged",
        "referenced_drift",
        "referenced_unchanged",
        "logical_reorder",
    }:
        raise ValueError("invalid action")
    temporary_path = raw["temporary_path"]
    canonical_path = raw["canonical_path"]
    if temporary_path is not None and not isinstance(temporary_path, str):
        raise ValueError("invalid temporary path")
    if canonical_path is not None and not isinstance(canonical_path, str):
        raise ValueError("invalid canonical path")
    source_fingerprint = raw["source_fingerprint"]
    target_fingerprint = raw["target_fingerprint"]
    last_result_snapshot = raw["last_result_snapshot"]
    placement_state = raw["placement_state"]
    if (
        not isinstance(source_fingerprint, dict)
        or not isinstance(target_fingerprint, dict)
        or not isinstance(last_result_snapshot, dict)
        or (placement_state is not None and not isinstance(placement_state, str))
    ):
        raise ValueError("invalid fingerprint")
    return _PlacementPlan(
        placement_id=_nonnegative_int(raw["placement_id"]),
        membership_id=_positive_int(raw["membership_id"]),
        ownership=_string(raw["ownership"]),
        mode=_string(raw["mode"]),
        old_reading_order=_nonnegative_int(raw["old_reading_order"]),
        new_reading_order=_nonnegative_int(raw["new_reading_order"]),
        old_path=_string(raw["old_path"]),
        new_path=_string(raw["new_path"]),
        temporary_path=temporary_path,
        rendered_path_after=_string(raw["rendered_path_after"]),
        action=cast("StoryArcReorderAction", action),
        canonical_path=canonical_path,
        source_fingerprint=dict(source_fingerprint),
        target_fingerprint=dict(target_fingerprint),
        placement_state=placement_state,
        last_result_snapshot=dict(last_result_snapshot),
    )


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("positive integer required")
    return value


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("nonnegative integer required")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("string required")
    return value


def _fixed_string(value: object, *, length: int) -> str:
    result = _string(value)
    if len(result) != length:
        raise ValueError("fixed string length required")
    return result
