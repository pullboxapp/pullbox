"""Network-first catalog discovery and atomic, targeted story-arc adoption."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import false, select, update
from sqlalchemy.exc import IntegrityError

from pullbox.core.issue_numbers import parse_issue_number_text
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcExternalIdentity,
    StoryArcLifecycle,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.services.story_arc_catalog_persistence import (
    canonical_root,
    publisher_id,
    seed_members,
)
from pullbox.services.story_arc_catalog_placement import initialize_catalog_placements
from pullbox.services.story_arc_catalog_types import (
    StoryArcCatalogError,
    StoryArcCatalogPreview,
    StoryArcCatalogRefreshPreview,
    StoryArcCatalogRefreshResult,
    catalog_snapshot,
    exact_provider_id,
    snapshot_fingerprint,
)
from pullbox.services.story_arc_placement_integration import (
    STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION,
    StoryArcPlacementIntegrationError,
    StoryArcPlacementPolicyInput,
    validate_story_arc_placement_policy_input,
)
from pullbox.services.story_arc_service import StoryArcService
from pullbox.services.story_arc_sync_queue import enqueue_story_arc_sync_work

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.providers.base import IssueMetadata, SeriesMetadata
    from pullbox.providers.story_arcs import StoryArcMetadata, StoryArcSearchResult

__all__ = [
    "StoryArcCatalogError",
    "StoryArcCatalogPreview",
    "StoryArcCatalogRefreshPreview",
    "StoryArcCatalogRefreshResult",
    "StoryArcCatalogService",
]

MAX_CATALOG_MEMBERS = 2_000
MAX_CATALOG_PARENTS = 200


class _CatalogProvider(Protocol):
    async def search_story_arcs_page(
        self, query: str, *, limit: int = 20, offset: int = 0
    ) -> tuple[list[StoryArcSearchResult], int]: ...
    async def get_story_arc(self, provider_id: str) -> StoryArcMetadata: ...
    async def get_story_arc_issues(
        self, issue_provider_ids: Sequence[str]
    ) -> list[IssueMetadata]: ...
    async def get_series(self, provider_id: str) -> SeriesMetadata: ...


class StoryArcCatalogService:
    """Fetch before opening a writer; flush within the caller's transaction only.

    Never fetch full parent catalogs, toggle parent monitoring, mutate files, or
    dispatch searches. UI adapters commit adoption before scheduling acquisition.
    """

    def __init__(self, provider: _CatalogProvider) -> None:
        self.provider = provider
        self.domain = StoryArcService()

    async def search(
        self, query: str, *, limit: int = 20, offset: int = 0
    ) -> tuple[list[StoryArcSearchResult], int]:
        return await self.provider.search_story_arcs_page(query, limit=limit, offset=offset)

    async def find_existing(
        self, session: AsyncSession, provider_ids: Sequence[str]
    ) -> dict[str, int]:
        ids = [exact_provider_id(value) for value in provider_ids]
        rows = await session.execute(
            select(StoryArc.comicvine_id, StoryArc.id).where(StoryArc.comicvine_id.in_(ids))
        )
        existing = {str(provider_id): arc_id for provider_id, arc_id in rows}
        identities = await session.execute(
            select(
                StoryArcExternalIdentity.external_id, StoryArcExternalIdentity.story_arc_id
            ).where(
                StoryArcExternalIdentity.source == "comicvine",
                StoryArcExternalIdentity.namespace == "story_arc",
                StoryArcExternalIdentity.external_id.in_(provider_ids),
            )
        )
        for provider_id, arc_id in identities:
            if provider_id in existing and existing[provider_id] != arc_id:
                raise StoryArcCatalogError(
                    "identity_conflict", "Provider story arc has conflicting local identities"
                )
            existing[provider_id] = arc_id
        return existing

    async def preview(
        self, provider_id: str, *, known_series_provider_ids: Collection[str] = ()
    ) -> StoryArcCatalogPreview:
        exact_provider_id(provider_id)
        metadata = await self.provider.get_story_arc(provider_id)
        if metadata.provider_id != provider_id:
            raise StoryArcCatalogError(
                "identity_conflict", "Provider returned a different story arc"
            )
        ids = metadata.issue_provider_ids
        self._validate_ids(ids)
        preview = StoryArcCatalogPreview(metadata=metadata, issues=(), series=(), fingerprint="")
        if metadata.membership_complete:
            issues = tuple(await self.provider.get_story_arc_issues(ids)) if ids else ()
            self._validate_hydration(ids, issues)
            parent_ids = tuple(dict.fromkeys(issue.series_provider_id for issue in issues))
            if len(parent_ids) > MAX_CATALOG_PARENTS:
                raise StoryArcCatalogError(
                    "catalog_limit_exceeded", "This arc exceeds the supported parent-series limit"
                )
            parents = []
            for parent_id in parent_ids:
                if parent_id in known_series_provider_ids:
                    continue
                parent = await self.provider.get_series(parent_id)
                if parent.provider_id != parent_id or not parent.title.strip():
                    raise StoryArcCatalogError(
                        "identity_conflict", "Provider returned a different parent series"
                    )
                parents.append(parent)
            preview = replace(preview, issues=issues, series=tuple(parents))
        return replace(preview, fingerprint=snapshot_fingerprint(preview))

    async def add(
        self,
        session: AsyncSession,
        preview: StoryArcCatalogPreview,
        *,
        ordered_issue_provider_ids: Sequence[str],
        library_root_id: int,
        monitored: bool = False,
        search_missing: bool = False,
        include_upcoming: bool = False,
        placement_policy: StoryArcPlacementPolicyInput | None = None,
        skipped_issue_provider_ids: Collection[str] = (),
    ) -> StoryArc:
        self._validate_preview(preview)
        order = tuple(ordered_issue_provider_ids)
        if len(order) != len(set(order)) or set(order) != set(preview.metadata.issue_provider_ids):
            raise StoryArcCatalogError(
                "invalid_order", "Review a complete, nonduplicated member order"
            )
        if not set(skipped_issue_provider_ids).issubset(order):
            raise StoryArcCatalogError(
                "invalid_skip", "Skipped members must belong to the reviewed arc"
            )
        root = await canonical_root(session, library_root_id)
        try:
            if placement_policy is None:
                from pullbox.services.story_arc_file_defaults import load_story_arc_file_defaults

                placement_policy = (await load_story_arc_file_defaults(session)).proposal()
            policy = await validate_story_arc_placement_policy_input(
                session,
                placement_policy,
                revision=1,
            )
        except StoryArcPlacementIntegrationError as exc:
            raise StoryArcCatalogError(exc.code, str(exc)) from exc
        if await self.find_existing(session, [preview.metadata.provider_id]):
            raise StoryArcCatalogError(
                "already_added", "This provider story arc is already in the library"
            )
        await self._enclose_savepoint(session)
        try:
            async with session.begin_nested():
                existing_identity = await session.scalar(
                    select(StoryArcExternalIdentity.id).where(
                        StoryArcExternalIdentity.source == "comicvine",
                        StoryArcExternalIdentity.namespace == "story_arc",
                        StoryArcExternalIdentity.external_id == preview.metadata.provider_id,
                    )
                )
                if existing_identity is not None:
                    raise StoryArcCatalogError(
                        "identity_conflict", "This provider story arc identity is already assigned"
                    )
                issues = await seed_members(session, preview, root, order)
                arc = await self.domain.create(
                    session,
                    name=preview.metadata.title,
                    description=preview.metadata.description,
                    monitored=monitored,
                    search_missing=search_missing,
                    include_upcoming=include_upcoming,
                    sync_enabled=policy.synchronize,
                    source_kind=StoryArcSourceKind.PROVIDER,
                )
                arc.comicvine_id = exact_provider_id(preview.metadata.provider_id)
                arc.comicvine_url = preview.metadata.comicvine_url
                arc.cover_url = preview.metadata.cover_url
                arc.publisher_id = await publisher_id(session, preview.metadata.publisher)
                arc.target_library_root_id = policy.target_library_root_id
                arc.policy_schema_version = STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION
                arc.policy_snapshot = policy.snapshot
                session.add(
                    StoryArcExternalIdentity(
                        story_arc_id=arc.id,
                        source="comicvine",
                        namespace="story_arc",
                        external_id=preview.metadata.provider_id,
                        source_url=preview.metadata.comicvine_url,
                        evidence={"snapshot_fingerprint": preview.fingerprint},
                    )
                )
                for position, provider_id in enumerate(order, start=1):
                    member = await self._member(
                        session, arc, preview, issues[provider_id], provider_id, position
                    )
                    if provider_id in skipped_issue_provider_ids:
                        member.resolution_state = StoryArcResolutionState.SKIPPED
                        member.sync_eligible = False
                self._diagnostics(arc, preview, root.id, (), ())
                await session.flush()
                if arc.sync_enabled:
                    files = await session.scalars(
                        select(LibraryFile).where(
                            LibraryFile.issue_id.in_([issue.id for issue in issues.values()])
                        )
                    )
                    for library_file in files:
                        await enqueue_story_arc_sync_work(session, library_file)
                await initialize_catalog_placements(session, arc)
                return arc
        except IntegrityError as exc:
            raise StoryArcCatalogError(
                "identity_conflict", "Catalog identities changed; refresh the preview"
            ) from exc

    async def preview_refresh(
        self, session: AsyncSession, story_arc_id: int, preview: StoryArcCatalogPreview
    ) -> StoryArcCatalogRefreshPreview:
        self._validate_preview(preview)
        arc = await self._arc(session, story_arc_id, preview)
        rows = list(
            (
                await session.scalars(
                    select(IssueStoryArc)
                    .where(IssueStoryArc.story_arc_id == arc.id)
                    .order_by(
                        IssueStoryArc.sequence_number,
                        IssueStoryArc.source_ordinal,
                        IssueStoryArc.id,
                    )
                )
            ).all()
        )
        existing = {
            row.source_issue_id
            for row in rows
            if row.source_kind is StoryArcSourceKind.PROVIDER
            and row.source_arc_id == preview.metadata.provider_id
        }
        canonical_ids = await session.scalars(
            select(Issue.comicvine_id)
            .join(IssueStoryArc, IssueStoryArc.issue_id == Issue.id)
            .where(IssueStoryArc.story_arc_id == arc.id, Issue.comicvine_id.is_not(None))
        )
        represented_ids = existing | {str(value) for value in canonical_ids}
        incoming = set(preview.metadata.issue_provider_ids)
        return StoryArcCatalogRefreshPreview(
            arc.id,
            arc.revision,
            tuple(
                value
                for value in preview.metadata.issue_provider_ids
                if value not in represented_ids
            ),
            tuple(
                row.source_issue_id
                for row in rows
                if row.source_issue_id in existing
                and row.source_issue_id not in incoming
                and row.source_issue_id is not None
            ),
        )

    async def refresh(
        self,
        session: AsyncSession,
        story_arc_id: int,
        preview: StoryArcCatalogPreview,
        *,
        expected_revision: int,
        library_root_id: int | None = None,
    ) -> StoryArcCatalogRefreshResult:
        delta = await self.preview_refresh(session, story_arc_id, preview)
        if isinstance(expected_revision, bool) or delta.revision != expected_revision:
            raise StoryArcCatalogError("revision_conflict", "Story arc changed; refresh the review")
        arc = await self._arc(session, story_arc_id, preview)
        catalog = arc.diagnostics.get("provider_catalog", {})
        root_id = catalog.get("canonical_library_root_id")
        if root_id is None:
            root_id = library_root_id
        root = await canonical_root(session, root_id)
        await self._enclose_savepoint(session)
        try:
            async with session.begin_nested():
                claimed = await session.execute(
                    update(StoryArc)
                    .where(StoryArc.id == arc.id, StoryArc.revision == expected_revision)
                    .values(revision=expected_revision + 1)
                )
                if claimed.rowcount != 1:  # type: ignore[attr-defined]
                    raise StoryArcCatalogError(
                        "revision_conflict", "Story arc changed; refresh the review"
                    )
                issues = await seed_members(
                    session, preview, root, preview.metadata.issue_provider_ids
                )
                rows = list(
                    (
                        await session.scalars(
                            select(IssueStoryArc).where(IssueStoryArc.story_arc_id == arc.id)
                        )
                    ).all()
                )
                position = max((row.sequence_number for row in rows), default=0)
                for row in rows:
                    if (
                        row.source_kind is StoryArcSourceKind.PROVIDER
                        and row.source_arc_id == preview.metadata.provider_id
                        and row.source_issue_id in issues
                        and row.issue_id is not None
                        and row.issue_id != issues[row.source_issue_id].id
                    ):
                        raise StoryArcCatalogError(
                            "identity_conflict",
                            "Membership disagrees with its exact provider identity",
                        )
                created = []
                for offset, provider_id in enumerate(delta.added_issue_provider_ids, start=1):
                    member = await self._member(
                        session, arc, preview, issues[provider_id], provider_id, position + offset
                    )
                    # Exact provider identity is sufficient for acquisition.
                    # Provider response order is not verified reading order, so
                    # keep placement paused until the user confirms this entry.
                    member.sync_eligible = False
                    member.evidence = {**member.evidence, "catalog_review_required": True}
                    created.append(member.id)
                pending = tuple(
                    row.id for row in rows if row.evidence.get("catalog_review_required") is True
                ) + tuple(created)
                arc.cover_url = preview.metadata.cover_url
                self._diagnostics(arc, preview, root.id, delta.removed_issue_provider_ids, pending)
                await session.flush()
                return StoryArcCatalogRefreshResult(
                    arc, tuple(created), delta.removed_issue_provider_ids
                )
        except IntegrityError as exc:
            raise StoryArcCatalogError(
                "identity_conflict", "Catalog identities changed; refresh the preview"
            ) from exc

    async def _arc(
        self, session: AsyncSession, story_arc_id: int, preview: StoryArcCatalogPreview
    ) -> StoryArc:
        arc = await session.get(StoryArc, story_arc_id)
        if arc is None or arc.lifecycle is not StoryArcLifecycle.ACTIVE:
            raise StoryArcCatalogError("arc_unavailable", "Story arc is unavailable or archived")
        if arc.comicvine_id != exact_provider_id(preview.metadata.provider_id):
            raise StoryArcCatalogError(
                "identity_conflict", "Provider snapshot belongs to a different story arc"
            )
        return arc

    async def _member(
        self,
        session: AsyncSession,
        arc: StoryArc,
        preview: StoryArcCatalogPreview,
        issue: Issue,
        provider_id: str,
        position: int,
    ) -> IssueStoryArc:
        metadata = next(value for value in preview.issues if value.provider_id == provider_id)
        member = await self.domain.add_membership(
            session,
            arc.id,
            issue_id=issue.id,
            sequence_number=position,
            source_ordinal=preview.metadata.issue_provider_ids.index(provider_id) + 1,
            source_kind=StoryArcSourceKind.PROVIDER,
            source_issue_number_text=metadata.issue_number_text or metadata.issue_number,
        )
        member.source_entry_id = provider_id
        member.source_issue_id = provider_id
        member.source_series_id = metadata.series_provider_id
        member.source_arc_id = preview.metadata.provider_id
        member.source_issue_title = metadata.title
        member.source_release_date_text = metadata.release_date
        parent = next(
            (value for value in preview.series if value.provider_id == metadata.series_provider_id),
            None,
        )
        if parent is not None:
            member.source_series_name = parent.title
            member.source_publisher = parent.publisher
        member.resolution_method = "exact_comicvine_id"
        member.resolution_confidence = 1.0
        member.evidence = {
            "provider": "comicvine",
            "snapshot_fingerprint": preview.fingerprint,
            "order_basis": preview.order_basis,
            "provider_response_ordinal": member.source_ordinal,
        }
        return member

    @staticmethod
    async def _enclose_savepoint(session: AsyncSession) -> None:
        """Keep SQLite's deferred BEGIN from committing the outer savepoint.

        Legacy sqlite3 transaction control starts BEGIN on DML, not SELECT or
        SAVEPOINT. This zero-row statement establishes the caller-owned outer
        transaction without changing records, so releasing our savepoint cannot
        commit the caller's work. PostgreSQL already encloses savepoints correctly.
        """
        if session.get_bind().dialect.name == "sqlite":
            await session.execute(
                update(StoryArc).where(false()).values(revision=StoryArc.revision)
            )

    @staticmethod
    def _diagnostics(
        arc: StoryArc,
        preview: StoryArcCatalogPreview,
        root_id: int,
        removed: tuple[str, ...],
        pending: tuple[int, ...],
    ) -> None:
        arc.diagnostics = {
            **arc.diagnostics,
            "provider_refresh_error": None,
            "provider_catalog": {
                "schema_version": 1,
                "snapshot": catalog_snapshot(preview),
                "snapshot_fingerprint": preview.fingerprint,
                "order_basis": preview.order_basis,
                "fetched_at": datetime.now(UTC).isoformat(),
                "canonical_library_root_id": root_id,
                "removed_issue_provider_ids": list(removed),
                "pending_membership_ids": list(pending),
            },
        }

    @staticmethod
    def _validate_ids(ids: Sequence[str]) -> None:
        if len(ids) > MAX_CATALOG_MEMBERS:
            raise StoryArcCatalogError(
                "catalog_limit_exceeded", "This arc exceeds the supported membership limit"
            )
        if len(ids) != len(set(ids)):
            raise StoryArcCatalogError(
                "identity_conflict", "Provider returned duplicate arc members"
            )
        for value in ids:
            exact_provider_id(value)

    @staticmethod
    def _validate_hydration(ids: Sequence[str], issues: Sequence[IssueMetadata]) -> None:
        if tuple(issue.provider_id for issue in issues) != tuple(ids):
            raise StoryArcCatalogError(
                "incomplete_hydration", "Provider did not hydrate the exact complete arc membership"
            )
        for issue in issues:
            exact_provider_id(issue.series_provider_id)
            try:
                parse_issue_number_text(issue.issue_number_text or issue.issue_number)
            except ValueError as exc:
                raise StoryArcCatalogError(
                    "invalid_issue_number", "Provider returned an invalid exact issue number"
                ) from exc

    def _validate_preview(self, preview: StoryArcCatalogPreview) -> None:
        if snapshot_fingerprint(preview) != preview.fingerprint:
            raise StoryArcCatalogError(
                "snapshot_changed", "Catalog snapshot changed; review it again"
            )
        if not preview.membership_complete:
            raise StoryArcCatalogError(
                "incomplete_membership", "Provider membership is incomplete; nothing was added"
            )
        self._validate_ids(preview.metadata.issue_provider_ids)
        self._validate_hydration(preview.metadata.issue_provider_ids, preview.issues)
