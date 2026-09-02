"""Provider-free logical story-arc lifecycle and membership management."""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.orm import selectinload

from pullbox.core.issue_numbers import normalize_issue_number_text
from pullbox.core.story_arc_identity import normalize_story_arc_name
from pullbox.models.issue import Issue
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcLifecycle,
    StoryArcPlacement,
    StoryArcPlacementOwnership,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.services.story_arc_membership_policy import order_review_filter, requires_order_review

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class StoryArcServiceError(Exception):
    """Base error for logical story-arc operations."""


class StoryArcNotFoundError(StoryArcServiceError):
    """Raised when an arc, membership, or canonical issue does not exist."""


class StoryArcValidationError(StoryArcServiceError):
    """Raised when a requested story-arc mutation is invalid."""


class StoryArcConflictError(StoryArcServiceError):
    """Raised for duplicate identities and optimistic revision conflicts."""


class DuplicateStoryArcMembershipError(StoryArcConflictError):
    """Raised when one arc would contain the same canonical issue twice."""


class _Unset(enum.Enum):
    VALUE = enum.auto()


_UNSET = _Unset.VALUE


def _display_name_and_key(name: str) -> tuple[str, str]:
    display_name = " ".join(name.split())
    if not display_name:
        raise StoryArcValidationError("Story-arc name must not be blank")
    if len(display_name) > 500:
        raise StoryArcValidationError("Story-arc name exceeds 500 characters")
    try:
        normalized_name = normalize_story_arc_name(display_name)
    except ValueError as exc:
        raise StoryArcValidationError(str(exc)) from exc
    return display_name, normalized_name


def _validate_order_value(value: int, *, label: str) -> int:
    if isinstance(value, bool) or value < 0:
        raise StoryArcValidationError(f"{label} must be a non-negative integer")
    return value


def _normalize_source_issue_number(value: str | float | int) -> str:
    try:
        return normalize_issue_number_text(value)
    except ValueError as exc:
        raise StoryArcValidationError(str(exc)) from exc


class StoryArcService:
    """Manage logical arcs without providers or filesystem materialization.

    The caller owns the transaction. Methods flush generated identifiers and
    constraint checks but never commit. Every membership mutation advances the
    parent arc revision so API and UI adapters can use one optimistic token.
    """

    async def create(
        self,
        session: AsyncSession,
        *,
        name: str,
        description: str | None = None,
        monitored: bool = False,
        search_missing: bool = False,
        include_upcoming: bool = False,
        sync_enabled: bool = False,
        source_kind: StoryArcSourceKind = StoryArcSourceKind.PULLBOX,
    ) -> StoryArc:
        """Create an empty logical story arc after exact identity checks."""
        display_name, normalized_name = _display_name_and_key(name)
        arc = StoryArc(
            name=display_name,
            normalized_name=normalized_name,
            description=description,
            source_kind=source_kind,
            lifecycle=StoryArcLifecycle.ACTIVE,
            monitored=monitored,
            search_missing=search_missing,
            include_upcoming=include_upcoming,
            sync_enabled=sync_enabled,
            revision=1,
        )
        session.add(arc)
        await session.flush()
        return arc

    async def update(
        self,
        session: AsyncSession,
        story_arc_id: int,
        *,
        expected_revision: int,
        name: str | _Unset = _UNSET,
        description: str | _Unset | None = _UNSET,
        monitored: bool | _Unset = _UNSET,
        search_missing: bool | _Unset = _UNSET,
        include_upcoming: bool | _Unset = _UNSET,
        sync_enabled: bool | _Unset = _UNSET,
    ) -> StoryArc:
        """Patch arc metadata and monitoring flags with revision protection."""
        arc = await self._get_arc(session, story_arc_id)
        self._assert_revision(arc, expected_revision)

        if isinstance(name, str):
            display_name, normalized_name = _display_name_and_key(name)
            if (
                display_name != arc.name or normalized_name != arc.normalized_name
            ) and await self._has_managed_placements(session, story_arc_id=arc.id):
                raise StoryArcValidationError(
                    "Story-arc name cannot change while a managed placement exists"
                )
            arc.name = display_name
            arc.normalized_name = normalized_name

        if description is None or isinstance(description, str):
            arc.description = description

        requested_flags = {
            "monitored": monitored,
            "search_missing": search_missing,
            "include_upcoming": include_upcoming,
            "sync_enabled": sync_enabled,
        }
        if arc.lifecycle == StoryArcLifecycle.ARCHIVED and any(
            value is True for value in requested_flags.values()
        ):
            raise StoryArcValidationError("Archived story arcs cannot enable monitoring or sync")
        for attribute, value in requested_flags.items():
            if isinstance(value, bool):
                setattr(arc, attribute, value)

        if isinstance(sync_enabled, bool):
            await self._set_membership_sync_eligibility(
                session,
                story_arc_id=arc.id,
                enabled=sync_enabled,
            )

        arc.revision += 1
        await session.flush()
        return arc

    async def archive(
        self,
        session: AsyncSession,
        story_arc_id: int,
        *,
        expected_revision: int,
    ) -> StoryArc:
        """Soft-archive an arc and disable all automated behavior."""
        arc = await self._get_arc(session, story_arc_id)
        self._assert_revision(arc, expected_revision)
        if arc.lifecycle == StoryArcLifecycle.ARCHIVED:
            return arc
        arc.lifecycle = StoryArcLifecycle.ARCHIVED
        arc.monitored = False
        arc.search_missing = False
        arc.include_upcoming = False
        arc.sync_enabled = False
        await self._set_membership_sync_eligibility(
            session,
            story_arc_id=arc.id,
            enabled=False,
        )
        arc.revision += 1
        await session.flush()
        return arc

    async def list_memberships(
        self,
        session: AsyncSession,
        story_arc_id: int,
    ) -> list[IssueStoryArc]:
        """Return every membership in stable reading order."""
        await self._get_arc(session, story_arc_id)
        result = await session.scalars(
            select(IssueStoryArc)
            .where(IssueStoryArc.story_arc_id == story_arc_id)
            .options(selectinload(IssueStoryArc.issue))
            .order_by(
                IssueStoryArc.sequence_number.asc(),
                IssueStoryArc.source_ordinal.asc(),
                IssueStoryArc.id.asc(),
            )
        )
        return list(result.all())

    async def add_membership(
        self,
        session: AsyncSession,
        story_arc_id: int,
        *,
        issue_id: int | None,
        sequence_number: int,
        source_issue_number_text: str | float | int | None = None,
        source_ordinal: int = 0,
        source_kind: StoryArcSourceKind = StoryArcSourceKind.PULLBOX,
    ) -> IssueStoryArc:
        """Add a resolved or reviewable missing entry without creating an issue."""
        arc = await self._get_active_arc(session, story_arc_id)
        sequence_number = _validate_order_value(sequence_number, label="sequence number")
        source_ordinal = _validate_order_value(source_ordinal, label="source ordinal")

        issue: Issue | None = None
        if issue_id is not None:
            issue = await session.get(Issue, issue_id)
            if issue is None:
                raise StoryArcNotFoundError(f"Issue {issue_id} was not found")

        exact_number = (
            _normalize_source_issue_number(source_issue_number_text)
            if source_issue_number_text is not None
            else issue.effective_issue_number_text
            if issue is not None
            else None
        )
        if exact_number is None:
            raise StoryArcValidationError(
                "An unresolved story-arc entry requires an exact source issue number"
            )

        existing = await self._find_idempotent_or_duplicate_membership(
            session,
            story_arc_id=story_arc_id,
            issue_id=issue_id,
            sequence_number=sequence_number,
            source_ordinal=source_ordinal,
            source_issue_number_text=exact_number,
            source_kind=source_kind,
        )
        if existing is not None:
            return existing

        membership = IssueStoryArc(
            story_arc_id=story_arc_id,
            issue_id=issue_id,
            sequence_number=sequence_number,
            source_ordinal=source_ordinal,
            resolution_state=(
                StoryArcResolutionState.RESOLVED
                if issue_id is not None
                else StoryArcResolutionState.MISSING
            ),
            source_kind=source_kind,
            source_issue_number_text=exact_number,
            sync_eligible=issue_id is not None and bool(arc.sync_enabled),
        )
        session.add(membership)
        arc.revision += 1
        await session.flush()
        return membership

    async def update_membership(
        self,
        session: AsyncSession,
        membership_id: int,
        *,
        sequence_number: int | None = None,
        source_ordinal: int | None = None,
        source_issue_number_text: str | float | int | None = None,
        intentionally_skipped: bool | None = None,
    ) -> IssueStoryArc:
        """Update order or review state while preserving canonical ownership."""
        membership = await self._get_membership(session, membership_id)
        arc = await self._get_active_arc(session, membership.story_arc_id)
        if any(
            value is not None
            for value in (
                sequence_number,
                source_ordinal,
                source_issue_number_text,
                intentionally_skipped,
            )
        ) and await self._has_managed_placements(session, membership_id=membership.id):
            raise StoryArcValidationError(
                "Story-arc membership cannot change while a managed placement exists"
            )
        changed = False

        if sequence_number is not None:
            sequence_number = _validate_order_value(sequence_number, label="sequence number")
            if membership.sequence_number != sequence_number:
                membership.sequence_number = sequence_number
                changed = True
        if source_ordinal is not None:
            source_ordinal = _validate_order_value(source_ordinal, label="source ordinal")
            if membership.source_ordinal != source_ordinal:
                membership.source_ordinal = source_ordinal
                changed = True
        if source_issue_number_text is not None:
            exact_number = _normalize_source_issue_number(source_issue_number_text)
            if membership.source_issue_number_text != exact_number:
                membership.source_issue_number_text = exact_number
                changed = True
        if intentionally_skipped is not None:
            next_state = (
                StoryArcResolutionState.SKIPPED
                if intentionally_skipped
                else StoryArcResolutionState.RESOLVED
                if membership.issue_id is not None
                else StoryArcResolutionState.MISSING
            )
            if membership.resolution_state != next_state:
                membership.resolution_state = next_state
                membership.sync_eligible = bool(
                    not intentionally_skipped
                    and membership.issue_id is not None
                    and arc.sync_enabled
                    and not requires_order_review(membership)
                )
                changed = True

        if changed:
            arc.revision += 1
            await session.flush()
        return membership

    async def resolve_membership(
        self,
        session: AsyncSession,
        membership_id: int,
        *,
        issue_id: int,
    ) -> IssueStoryArc:
        """Resolve or replace an entry with an existing canonical issue."""
        membership = await self._get_membership(session, membership_id)
        arc = await self._get_active_arc(session, membership.story_arc_id)
        issue = await session.get(Issue, issue_id)
        if issue is None:
            raise StoryArcNotFoundError(f"Issue {issue_id} was not found")

        duplicate_id = await session.scalar(
            select(IssueStoryArc.id).where(
                IssueStoryArc.story_arc_id == membership.story_arc_id,
                IssueStoryArc.issue_id == issue_id,
                IssueStoryArc.id != membership.id,
            )
        )
        if duplicate_id is not None:
            raise DuplicateStoryArcMembershipError(
                f"Issue {issue_id} already belongs to story arc {membership.story_arc_id}"
            )
        if (
            membership.issue_id == issue_id
            and membership.resolution_state == StoryArcResolutionState.RESOLVED
            and not requires_order_review(membership)
        ):
            return membership
        if await self._has_managed_placements(session, membership_id=membership.id):
            raise StoryArcValidationError(
                "Story-arc membership cannot resolve differently while a managed placement exists"
            )

        membership.issue_id = issue_id
        membership.resolution_state = StoryArcResolutionState.RESOLVED
        membership.sync_eligible = bool(arc.sync_enabled)
        if requires_order_review(membership):
            membership.evidence = {**membership.evidence, "catalog_review_required": False}
        if membership.source_issue_number_text is None:
            membership.source_issue_number_text = issue.effective_issue_number_text
        arc.revision += 1
        await session.flush()
        return membership

    async def detach_deleted_issue(
        self,
        session: AsyncSession,
        issue_id: int,
    ) -> int:
        """Retain memberships as missing entries before deleting an issue."""
        issue = await session.get(Issue, issue_id)
        memberships = list(
            (
                await session.scalars(
                    select(IssueStoryArc).where(IssueStoryArc.issue_id == issue_id)
                )
            ).all()
        )
        if not memberships:
            return 0
        if await self._has_managed_placements(session, issue_id=issue_id):
            raise StoryArcValidationError(
                "Canonical issue cannot detach while a managed story-arc placement exists"
            )

        affected_arc_ids: set[int] = set()
        for membership in memberships:
            membership.issue_id = None
            if membership.resolution_state != StoryArcResolutionState.SKIPPED:
                membership.resolution_state = StoryArcResolutionState.MISSING
            if membership.source_issue_number_text is None and issue is not None:
                membership.source_issue_number_text = issue.effective_issue_number_text
            affected_arc_ids.add(membership.story_arc_id)
        await self._advance_arc_revisions(session, affected_arc_ids)
        await session.flush()
        return len(memberships)

    async def reconcile_missing_issue_references(
        self,
        session: AsyncSession,
        *,
        story_arc_id: int | None = None,
    ) -> int:
        """Repair resolved states whose canonical issue was deleted via FK cleanup."""
        statement = select(IssueStoryArc).where(
            IssueStoryArc.issue_id.is_(None),
            IssueStoryArc.resolution_state == StoryArcResolutionState.RESOLVED,
        )
        if story_arc_id is not None:
            statement = statement.where(IssueStoryArc.story_arc_id == story_arc_id)
        memberships = list((await session.scalars(statement)).all())
        if not memberships:
            return 0

        affected_arc_ids = {membership.story_arc_id for membership in memberships}
        for membership in memberships:
            membership.resolution_state = StoryArcResolutionState.MISSING
        await self._advance_arc_revisions(session, affected_arc_ids)
        await session.flush()
        return len(memberships)

    async def reorder_memberships(
        self,
        session: AsyncSession,
        story_arc_id: int,
        *,
        ordered_membership_ids: list[int],
        expected_revision: int,
    ) -> list[IssueStoryArc]:
        """Apply one complete, duplicate-free order with optimistic locking."""
        arc = await self._get_active_arc(session, story_arc_id)
        self._assert_revision(arc, expected_revision)
        if await self._has_managed_placements(session, story_arc_id=story_arc_id):
            raise StoryArcValidationError(
                "Story-arc memberships cannot reorder while a managed placement exists"
            )
        memberships = await self.list_memberships(session, story_arc_id)
        existing_ids = {membership.id for membership in memberships}
        requested_ids = set(ordered_membership_ids)
        if len(requested_ids) != len(ordered_membership_ids) or requested_ids != existing_ids:
            raise StoryArcValidationError(
                "Membership reorder must include every arc membership exactly once"
            )

        by_id = {membership.id: membership for membership in memberships}
        reordered = [by_id[membership_id] for membership_id in ordered_membership_ids]
        for position, membership in enumerate(reordered, start=1):
            membership.sequence_number = position
        arc.revision += 1
        await session.flush()
        return reordered

    async def remove_membership(
        self,
        session: AsyncSession,
        membership_id: int,
    ) -> None:
        """Remove one association without touching its canonical issue."""
        membership = await self._get_membership(session, membership_id)
        arc = await self._get_active_arc(session, membership.story_arc_id)
        if await self._has_managed_placements(session, membership_id=membership.id):
            raise StoryArcValidationError(
                "Story-arc membership cannot be removed while a managed placement exists"
            )
        await session.delete(membership)
        arc.revision += 1
        await session.flush()

    async def _get_arc(self, session: AsyncSession, story_arc_id: int) -> StoryArc:
        arc = await session.get(StoryArc, story_arc_id)
        if arc is None:
            raise StoryArcNotFoundError(f"Story arc {story_arc_id} was not found")
        return arc

    async def _get_active_arc(self, session: AsyncSession, story_arc_id: int) -> StoryArc:
        arc = await self._get_arc(session, story_arc_id)
        if arc.lifecycle == StoryArcLifecycle.ARCHIVED:
            raise StoryArcValidationError("Archived story arcs cannot change memberships")
        return arc

    async def _get_membership(
        self,
        session: AsyncSession,
        membership_id: int,
    ) -> IssueStoryArc:
        membership = await session.get(IssueStoryArc, membership_id)
        if membership is None:
            raise StoryArcNotFoundError(f"Story-arc membership {membership_id} was not found")
        return membership

    async def _find_idempotent_or_duplicate_membership(
        self,
        session: AsyncSession,
        *,
        story_arc_id: int,
        issue_id: int | None,
        sequence_number: int,
        source_ordinal: int,
        source_issue_number_text: str,
        source_kind: StoryArcSourceKind,
    ) -> IssueStoryArc | None:
        if issue_id is not None:
            existing = await session.scalar(
                select(IssueStoryArc).where(
                    IssueStoryArc.story_arc_id == story_arc_id,
                    IssueStoryArc.issue_id == issue_id,
                )
            )
            if existing is None:
                return None
            if (
                existing.sequence_number == sequence_number
                and existing.source_ordinal == source_ordinal
                and existing.source_issue_number_text == source_issue_number_text
                and existing.source_kind == source_kind
                and existing.resolution_state == StoryArcResolutionState.RESOLVED
            ):
                return existing
            raise DuplicateStoryArcMembershipError(
                f"Issue {issue_id} already belongs to story arc {story_arc_id}"
            )

        unresolved_existing: IssueStoryArc | None = await session.scalar(
            select(IssueStoryArc).where(
                IssueStoryArc.story_arc_id == story_arc_id,
                IssueStoryArc.issue_id.is_(None),
                IssueStoryArc.sequence_number == sequence_number,
                IssueStoryArc.source_ordinal == source_ordinal,
                IssueStoryArc.source_issue_number_text == source_issue_number_text,
                IssueStoryArc.source_kind == source_kind,
            )
        )
        return unresolved_existing

    async def _advance_arc_revisions(
        self,
        session: AsyncSession,
        story_arc_ids: set[int],
    ) -> None:
        if not story_arc_ids:
            return
        arcs = list(
            (await session.scalars(select(StoryArc).where(StoryArc.id.in_(story_arc_ids)))).all()
        )
        for arc in arcs:
            arc.revision += 1

    @staticmethod
    async def _has_managed_placements(
        session: AsyncSession,
        *,
        story_arc_id: int | None = None,
        membership_id: int | None = None,
        issue_id: int | None = None,
    ) -> bool:
        statement = (
            select(StoryArcPlacement.id)
            .join(IssueStoryArc, StoryArcPlacement.issue_story_arc_id == IssueStoryArc.id)
            .where(StoryArcPlacement.ownership == StoryArcPlacementOwnership.MANAGED)
        )
        if story_arc_id is not None:
            statement = statement.where(IssueStoryArc.story_arc_id == story_arc_id)
        if membership_id is not None:
            statement = statement.where(IssueStoryArc.id == membership_id)
        if issue_id is not None:
            statement = statement.where(IssueStoryArc.issue_id == issue_id)
        return await session.scalar(statement.limit(1)) is not None

    @staticmethod
    async def _set_membership_sync_eligibility(
        session: AsyncSession,
        *,
        story_arc_id: int,
        enabled: bool,
    ) -> None:
        statement = sa_update(IssueStoryArc).where(
            IssueStoryArc.story_arc_id == story_arc_id,
            IssueStoryArc.resolution_state == StoryArcResolutionState.RESOLVED,
        )
        # Constant values keep already-loaded ORM members coherent on both DBs.
        await session.execute(statement.values(sync_eligible=False))
        if enabled:
            await session.execute(
                statement.where(~order_review_filter()).values(sync_eligible=True)
            )

    @staticmethod
    def _assert_revision(arc: StoryArc, expected_revision: int) -> None:
        if arc.revision != expected_revision:
            raise StoryArcConflictError(
                f"Story arc revision changed: expected {expected_revision}, current {arc.revision}"
            )
