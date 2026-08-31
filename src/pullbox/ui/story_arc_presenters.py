"""Bounded query presenters for normal Story Arc management pages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import joinedload

from pullbox.core.db_utils import escape_like
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryRoot
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcLifecycle,
    StoryArcResolutionState,
)
from pullbox.models.story_arc_sync import StoryArcSyncWork, StoryArcSyncWorkState
from pullbox.services.story_arc_placement_integration import (
    StoryArcPlacementPolicy,
    StoryArcPlacementPolicyInput,
    StoryArcPlacementPolicyMode,
    StoryArcPlacementPreviewItem,
    StoryArcPlacementSyncService,
    StoryArcPlacementView,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement
    from sqlalchemy.sql.selectable import Subquery


@dataclass(frozen=True, slots=True)
class StoryArcListItemView:
    """One compact Story Arc row with aggregate entry state."""

    id: int
    name: str
    description: str | None
    lifecycle: str
    source_kind: str
    monitored: bool
    sync_enabled: bool
    revision: int
    membership_count: int
    resolved_count: int
    missing_count: int
    conflict_count: int
    completion_label: str


@dataclass(frozen=True, slots=True)
class StoryArcListPageView:
    """A bounded Story Arc registry page."""

    items: tuple[StoryArcListItemView, ...]
    total: int
    page: int
    per_page: int
    total_pages: int


@dataclass(frozen=True, slots=True)
class StoryArcMembershipView:
    """One ordered membership with fully loaded canonical context."""

    id: int
    issue_id: int | None
    position: int
    sequence_number: int
    source_ordinal: int
    exact_issue_number: str
    series_name: str
    issue_title: str
    resolution_state: str
    resolution_label: str
    canonical_state: str
    canonical_file_available: bool
    can_resolve: bool
    local_search_query: str
    metadata_add_url: str
    is_first: bool
    is_last: bool


@dataclass(frozen=True, slots=True)
class StoryArcInitialPlacementView:
    """Aggregate initial-file progress without exposing internal path snapshots."""

    state: str
    total: int
    completed: int
    failed: int
    pending: int


@dataclass(frozen=True, slots=True)
class StoryArcDetailView:
    """Story Arc metadata and one bounded ordered membership page."""

    id: int
    name: str
    description: str | None
    lifecycle: str
    source_kind: str
    monitored: bool
    search_missing: bool
    include_upcoming: bool
    sync_enabled: bool
    revision: int
    membership_count: int
    resolved_count: int
    missing_count: int
    conflict_count: int
    memberships: tuple[StoryArcMembershipView, ...]
    page: int
    per_page: int
    total_pages: int
    next_sequence_number: int
    comicvine_id: int | None = None
    catalog_removed_count: int = 0
    catalog_added_review_count: int = 0
    initial_placements: StoryArcInitialPlacementView | None = None

    @property
    def active(self) -> bool:
        """Whether membership and automation controls remain editable."""
        return self.lifecycle == StoryArcLifecycle.ACTIVE.value


@dataclass(frozen=True, slots=True)
class StoryArcPlacementRootView:
    """One bounded root option approved by the library-root registry."""

    id: int
    name: str
    path: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class StoryArcPlacementPolicyView:
    """Complete effective or candidate policy rendered without hidden defaults."""

    configured: bool
    revision: int
    mode: str
    mode_label: str
    layout_label: str
    target_library_root_id: int | None
    destination_root: str
    folder_template: str
    file_template: str
    symlink_style: str
    synchronize: bool


@dataclass(frozen=True, slots=True)
class StoryArcPlacementPreviewItemView:
    """One ownership-aware read-only preview row."""

    membership_id: int
    sequence_number: int
    issue_number_text: str
    mode: str
    method_label: str
    state: str
    classification: str
    target_path: str
    collision: str
    inspection_code: str
    reason: str
    required_bytes: int
    required_bytes_label: str
    proposed_ownership: str
    ownership_label: str
    placement_id: int | None
    current_ownership: str


@dataclass(frozen=True, slots=True)
class StoryArcPlacementPreviewPageView:
    """Bounded preview rows plus explicit coverage metadata."""

    items: tuple[StoryArcPlacementPreviewItemView, ...]
    total: int
    page: int
    per_page: int
    total_pages: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class StoryArcPlacementStateView:
    """One durable placement record detached from the ORM session."""

    id: int
    membership_id: int
    placement_path: str
    mode: str
    mode_label: str
    ownership: str
    state: str
    last_checked_label: str
    can_retry: bool
    can_repair: bool
    can_remove: bool
    safety_block_reason: str


@dataclass(frozen=True, slots=True)
class StoryArcPlacementStatePageView:
    """Bounded durable placement state page."""

    items: tuple[StoryArcPlacementStateView, ...]
    total: int
    page: int
    per_page: int
    total_pages: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class StoryArcSyncWorkSummaryView:
    """Fixed-cardinality aggregate of durable automatic placement work."""

    queued: int
    running: int
    retry_wait: int
    failed: int
    completed: int
    cancelled: int
    total: int


@dataclass(frozen=True, slots=True)
class StoryArcPlacementContextView:
    """Normal-product policy, preview, root options, and durable state."""

    policy: StoryArcPlacementPolicyView
    roots: tuple[StoryArcPlacementRootView, ...]
    roots_truncated: bool
    preview: StoryArcPlacementPreviewPageView
    placements: StoryArcPlacementStatePageView
    sync_work: StoryArcSyncWorkSummaryView
    page: int
    total_pages: int
    preview_only: bool


_placement_service = StoryArcPlacementSyncService()
_PLACEMENT_UI_PAGE_SIZE = 10
_PLACEMENT_ROOT_OPTION_LIMIT = 100


def _membership_counts_subquery() -> Subquery:
    """Aggregate membership state once for a bounded registry query."""
    return (
        select(
            IssueStoryArc.story_arc_id.label("story_arc_id"),
            func.count(IssueStoryArc.id).label("membership_count"),
            func.sum(
                case(
                    (IssueStoryArc.resolution_state == StoryArcResolutionState.RESOLVED, 1),
                    else_=0,
                )
            ).label("resolved_count"),
            func.sum(
                case(
                    (IssueStoryArc.resolution_state == StoryArcResolutionState.MISSING, 1),
                    else_=0,
                )
            ).label("missing_count"),
            func.sum(
                case(
                    (IssueStoryArc.resolution_state == StoryArcResolutionState.CONFLICT, 1),
                    else_=0,
                )
            ).label("conflict_count"),
        )
        .group_by(IssueStoryArc.story_arc_id)
        .subquery()
    )


def _completion_label(*, total: int, resolved: int) -> str:
    if total == 0:
        return "Empty"
    return f"{resolved} of {total} resolved"


async def load_story_arc_list_page(
    session: AsyncSession,
    *,
    q: str | None,
    lifecycle: StoryArcLifecycle | None,
    monitored: bool | None,
    page: int,
    per_page: int,
) -> StoryArcListPageView:
    """Load one stable Story Arc registry page with literal search semantics."""
    filters: list[ColumnElement[bool]] = []
    if q is not None and (search_text := q.strip()):
        filters.append(StoryArc.name.ilike(f"%{escape_like(search_text)}%", escape="\\"))
    if lifecycle is not None:
        filters.append(StoryArc.lifecycle == lifecycle)
    if monitored is not None:
        filters.append(StoryArc.monitored.is_(monitored))

    total = int(await session.scalar(select(func.count(StoryArc.id)).where(*filters)) or 0)
    total_pages = max(1, (total + per_page - 1) // per_page)
    safe_page = min(page, total_pages)
    counts = _membership_counts_subquery()
    rows = (
        await session.execute(
            select(
                StoryArc,
                func.coalesce(counts.c.membership_count, 0),
                func.coalesce(counts.c.resolved_count, 0),
                func.coalesce(counts.c.missing_count, 0),
                func.coalesce(counts.c.conflict_count, 0),
            )
            .outerjoin(counts, counts.c.story_arc_id == StoryArc.id)
            .where(*filters)
            .order_by(StoryArc.normalized_name.asc(), StoryArc.id.asc())
            .limit(per_page)
            .offset((safe_page - 1) * per_page)
        )
    ).all()
    items: list[StoryArcListItemView] = []
    for arc, membership_count, resolved_count, missing_count, conflict_count in rows:
        membership_total = int(membership_count)
        resolved_total = int(resolved_count)
        items.append(
            StoryArcListItemView(
                id=arc.id,
                name=arc.name,
                description=arc.description,
                lifecycle=arc.lifecycle.value,
                source_kind=arc.source_kind.value,
                monitored=arc.monitored,
                sync_enabled=arc.sync_enabled,
                revision=arc.revision,
                membership_count=membership_total,
                resolved_count=resolved_total,
                missing_count=int(missing_count),
                conflict_count=int(conflict_count),
                completion_label=_completion_label(
                    total=membership_total,
                    resolved=resolved_total,
                ),
            )
        )
    return StoryArcListPageView(
        items=tuple(items),
        total=total,
        page=safe_page,
        per_page=per_page,
        total_pages=total_pages,
    )


def _resolution_label(state: StoryArcResolutionState) -> str:
    return {
        StoryArcResolutionState.PENDING: "Pending review",
        StoryArcResolutionState.RESOLVED: "Resolved",
        StoryArcResolutionState.MISSING: "Missing",
        StoryArcResolutionState.AMBIGUOUS: "Ambiguous",
        StoryArcResolutionState.CONFLICT: "Conflict",
        StoryArcResolutionState.SKIPPED: "Skipped",
    }[state]


def _canonical_state(issue: Issue | None) -> str:
    if issue is None:
        return "Not matched"
    if issue.library_file is not None:
        return "File available"
    if issue.status.value == "owned":
        return "Owned record; file unavailable"
    return issue.status.value.replace("_", " ").title()


def _present_membership(
    membership: IssueStoryArc,
    *,
    position: int,
    membership_total: int,
) -> StoryArcMembershipView:
    issue = membership.issue
    exact_issue_number = membership.source_issue_number_text
    if exact_issue_number is None and issue is not None:
        exact_issue_number = issue.effective_issue_number_text
    exact_issue_number = exact_issue_number or "Unknown"
    series_name = membership.source_series_name
    if series_name is None and issue is not None:
        series_name = issue.series.title
    issue_title = membership.source_issue_title
    if issue_title is None and issue is not None:
        issue_title = issue.title
    source_series_query = (membership.source_series_name or "").strip()
    if not source_series_query and issue is not None:
        source_series_query = issue.series.title
    metadata_query = source_series_query
    return StoryArcMembershipView(
        id=membership.id,
        issue_id=membership.issue_id,
        position=position,
        sequence_number=membership.sequence_number,
        source_ordinal=membership.source_ordinal,
        exact_issue_number=exact_issue_number,
        series_name=series_name or "Series not matched",
        issue_title=issue_title or "Untitled issue",
        resolution_state=membership.resolution_state.value,
        resolution_label=_resolution_label(membership.resolution_state),
        canonical_state=_canonical_state(issue),
        canonical_file_available=issue is not None and issue.library_file is not None,
        can_resolve=(
            membership.issue_id is None
            or membership.resolution_state
            in {
                StoryArcResolutionState.PENDING,
                StoryArcResolutionState.MISSING,
                StoryArcResolutionState.AMBIGUOUS,
                StoryArcResolutionState.CONFLICT,
            }
        ),
        local_search_query=source_series_query or exact_issue_number,
        metadata_add_url=(
            f"/series/add?{urlencode({'q': metadata_query})}" if metadata_query else "/series/add"
        ),
        is_first=position == 1,
        is_last=position == membership_total,
    )


async def load_story_arc_detail(
    session: AsyncSession,
    *,
    story_arc_id: int,
    page: int,
    per_page: int,
) -> StoryArcDetailView | None:
    """Load one arc and one bounded, relationship-complete membership page."""
    arc = await session.get(StoryArc, story_arc_id)
    if arc is None:
        return None

    count_row = (
        await session.execute(
            select(
                func.count(IssueStoryArc.id),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                IssueStoryArc.resolution_state == StoryArcResolutionState.RESOLVED,
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                IssueStoryArc.resolution_state == StoryArcResolutionState.MISSING,
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                IssueStoryArc.resolution_state == StoryArcResolutionState.CONFLICT,
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
            ).where(IssueStoryArc.story_arc_id == story_arc_id)
        )
    ).one()
    membership_total = int(count_row[0])
    total_pages = max(1, (membership_total + per_page - 1) // per_page)
    safe_page = min(page, total_pages)
    offset = (safe_page - 1) * per_page
    memberships = list(
        (
            await session.scalars(
                select(IssueStoryArc)
                .where(IssueStoryArc.story_arc_id == story_arc_id)
                .options(
                    joinedload(IssueStoryArc.issue).joinedload(Issue.series),
                    joinedload(IssueStoryArc.issue).joinedload(Issue.library_file),
                )
                .order_by(
                    IssueStoryArc.sequence_number.asc(),
                    IssueStoryArc.source_ordinal.asc(),
                    IssueStoryArc.id.asc(),
                )
                .limit(per_page)
                .offset(offset)
            )
        ).unique()
    )
    membership_views = tuple(
        _present_membership(
            membership,
            position=offset + index,
            membership_total=membership_total,
        )
        for index, membership in enumerate(memberships, start=1)
    )
    return StoryArcDetailView(
        id=arc.id,
        name=arc.name,
        description=arc.description,
        lifecycle=arc.lifecycle.value,
        source_kind=arc.source_kind.value,
        monitored=arc.monitored,
        search_missing=arc.search_missing,
        include_upcoming=arc.include_upcoming,
        sync_enabled=arc.sync_enabled,
        revision=arc.revision,
        membership_count=membership_total,
        resolved_count=int(count_row[1]),
        missing_count=int(count_row[2]),
        conflict_count=int(count_row[3]),
        memberships=membership_views,
        page=safe_page,
        per_page=per_page,
        total_pages=total_pages,
        next_sequence_number=membership_total + 1,
        comicvine_id=arc.comicvine_id,
        catalog_removed_count=_catalog_diagnostic_count(arc, "removed_issue_provider_ids"),
        catalog_added_review_count=_catalog_diagnostic_count(arc, "pending_membership_ids"),
        initial_placements=_initial_placement_view(arc),
    )


def _catalog_diagnostic_count(arc: StoryArc, key: str) -> int:
    catalog = (arc.diagnostics or {}).get("provider_catalog")
    if not isinstance(catalog, dict):
        return 0
    values = catalog.get(key)
    return len(values) if isinstance(values, list) else 0


def _initial_placement_view(arc: StoryArc) -> StoryArcInitialPlacementView | None:
    marker = (arc.diagnostics or {}).get("catalog_initial_placements")
    if not isinstance(marker, dict):
        return None
    state = str(marker.get("state", "pending"))
    if state not in {"pending", "running", "failed", "complete", "blocked"}:
        state = "blocked"
    counts = {
        key: value if type(value := marker.get(key)) is int and value >= 0 else 0
        for key in ("total", "completed", "failed", "pending")
    }
    return StoryArcInitialPlacementView(state=state, **counts)


def _placement_mode_label(mode: str) -> str:
    return {
        "logical": "Logical only",
        "reference_only": "Reference only",
        "copy": "Copy",
        "hardlink": "Hardlink",
        "symlink": "Symlink",
    }.get(mode, mode.replace("_", " ").title())


def _placement_policy_view(policy: StoryArcPlacementPolicy) -> StoryArcPlacementPolicyView:
    return StoryArcPlacementPolicyView(
        configured=policy.configured,
        revision=policy.revision,
        mode=policy.mode.value,
        mode_label=_placement_mode_label(policy.mode.value),
        layout_label=(
            "Logical only"
            if policy.mode is StoryArcPlacementPolicyMode.LOGICAL
            else "Separate Story Arc folder"
        ),
        target_library_root_id=policy.target_library_root_id,
        destination_root=policy.destination_root or "",
        folder_template=policy.folder_template,
        file_template=policy.file_template,
        symlink_style=policy.symlink_style.value if policy.symlink_style is not None else "",
        synchronize=policy.synchronize,
    )


def _bytes_label(value: int) -> str:
    return f"{value:,} bytes"


def _preview_ownership_label(item: StoryArcPlacementPreviewItem) -> str:
    if item.classification == "untracked_identical":
        return "User-owned (untracked)"
    if item.classification == "different_content":
        return "User-owned (different content)"
    ownership = item.current_ownership or item.proposed_ownership
    if ownership == "referenced":
        return "Referenced (user-owned)"
    return ownership.replace("_", " ").title()


def _preview_item_view(item: StoryArcPlacementPreviewItem) -> StoryArcPlacementPreviewItemView:
    # Placement integration returns a frozen data object. Keeping this mapper
    # field-explicit makes template output independent of ORM/session lifetime.
    return StoryArcPlacementPreviewItemView(
        membership_id=item.membership_id,
        sequence_number=item.sequence_number,
        issue_number_text=item.issue_number_text,
        mode=item.mode,
        method_label=_placement_mode_label(item.mode),
        state=item.state,
        classification=item.classification,
        target_path=item.target_path or "Not applicable",
        collision=item.collision,
        inspection_code=item.inspection_code or "",
        reason=item.reason or "",
        required_bytes=item.required_bytes,
        required_bytes_label=_bytes_label(item.required_bytes),
        proposed_ownership=item.proposed_ownership,
        ownership_label=_preview_ownership_label(item),
        placement_id=item.placement_id,
        current_ownership=item.current_ownership or "",
    )


def _placement_state_view(item: StoryArcPlacementView) -> StoryArcPlacementStateView:
    """Detach durable state and suppress writes after an ownership safety block."""
    last_result = dict(item.last_result)
    removal_safety_blocked = (
        item.ownership.value == "managed"
        and item.state.value == "drifted"
        and last_result.get("operation") == "remove"
        and last_result.get("error_category") in {"safety", "collision", "ownership"}
    )
    return StoryArcPlacementStateView(
        id=item.id,
        membership_id=item.issue_story_arc_id,
        placement_path=item.placement_path,
        mode=item.mode.value,
        mode_label=_placement_mode_label(item.mode.value),
        ownership=item.ownership.value,
        state=item.state.value,
        last_checked_label=(
            item.last_checked_at.isoformat() if item.last_checked_at is not None else "Not checked"
        ),
        can_retry=item.state.value != "current" and not removal_safety_blocked,
        can_repair=(
            item.ownership.value == "managed"
            and item.state.value in {"missing", "failed"}
            and not removal_safety_blocked
        ),
        can_remove=not removal_safety_blocked,
        safety_block_reason=(
            "Pullbox blocked changes because this managed artifact no longer matches its "
            "recorded ownership evidence. Review the artifact manually; the canonical "
            "library file was not changed."
            if removal_safety_blocked
            else ""
        ),
    )


async def load_story_arc_placement_roots(
    session: AsyncSession,
    *,
    selected_root_id: int | None,
) -> tuple[tuple[StoryArcPlacementRootView, ...], bool]:
    roots = list(
        (
            await session.scalars(
                select(LibraryRoot)
                .where(
                    or_(
                        LibraryRoot.enabled.is_(True),
                        LibraryRoot.id == selected_root_id,
                    )
                )
                .order_by(LibraryRoot.name.asc(), LibraryRoot.id.asc())
                .limit(_PLACEMENT_ROOT_OPTION_LIMIT + 1)
            )
        ).all()
    )
    truncated = len(roots) > _PLACEMENT_ROOT_OPTION_LIMIT
    return (
        tuple(
            StoryArcPlacementRootView(
                id=root.id,
                name=root.name,
                path=root.path,
                enabled=root.enabled,
            )
            for root in roots[:_PLACEMENT_ROOT_OPTION_LIMIT]
        ),
        truncated,
    )


async def _load_sync_work_summary(
    session: AsyncSession,
    *,
    story_arc_id: int,
) -> StoryArcSyncWorkSummaryView:
    rows = (
        await session.execute(
            select(StoryArcSyncWork.state, func.count(StoryArcSyncWork.id))
            .join(
                IssueStoryArc,
                StoryArcSyncWork.issue_story_arc_id == IssueStoryArc.id,
            )
            .where(IssueStoryArc.story_arc_id == story_arc_id)
            .group_by(StoryArcSyncWork.state)
        )
    ).all()
    counts = {state.value: int(count) for state, count in rows}
    return StoryArcSyncWorkSummaryView(
        queued=counts.get(StoryArcSyncWorkState.QUEUED.value, 0),
        running=counts.get(StoryArcSyncWorkState.RUNNING.value, 0),
        retry_wait=counts.get(StoryArcSyncWorkState.RETRY_WAIT.value, 0),
        failed=counts.get(StoryArcSyncWorkState.FAILED.value, 0),
        completed=counts.get(StoryArcSyncWorkState.COMPLETED.value, 0),
        cancelled=counts.get(StoryArcSyncWorkState.CANCELLED.value, 0),
        total=sum(counts.values()),
    )


async def load_story_arc_placement_context(
    session: AsyncSession,
    *,
    story_arc_id: int,
    page: int,
    proposal: StoryArcPlacementPolicyInput | None = None,
) -> StoryArcPlacementContextView:
    """Load one bounded policy preview and durable-state page for normal UI."""
    offset = (page - 1) * _PLACEMENT_UI_PAGE_SIZE
    policy = (
        await _placement_service.validate_policy(session, story_arc_id, proposal)
        if proposal is not None
        else await _placement_service.get_policy(session, story_arc_id)
    )
    preview = await _placement_service.preview_arc(
        session,
        story_arc_id,
        limit=_PLACEMENT_UI_PAGE_SIZE,
        offset=offset,
        proposal=proposal,
    )
    placements = await _placement_service.list_placements(
        session,
        story_arc_id,
        limit=_PLACEMENT_UI_PAGE_SIZE,
        offset=offset,
    )
    preview_pages = max(1, (preview.total + _PLACEMENT_UI_PAGE_SIZE - 1) // _PLACEMENT_UI_PAGE_SIZE)
    placement_pages = max(
        1,
        (placements.total + _PLACEMENT_UI_PAGE_SIZE - 1) // _PLACEMENT_UI_PAGE_SIZE,
    )
    total_pages = max(preview_pages, placement_pages)
    safe_page = min(page, total_pages)
    if safe_page != page:
        offset = (safe_page - 1) * _PLACEMENT_UI_PAGE_SIZE
        preview = await _placement_service.preview_arc(
            session,
            story_arc_id,
            limit=_PLACEMENT_UI_PAGE_SIZE,
            offset=offset,
            proposal=proposal,
        )
        placements = await _placement_service.list_placements(
            session,
            story_arc_id,
            limit=_PLACEMENT_UI_PAGE_SIZE,
            offset=offset,
        )
    roots, roots_truncated = await load_story_arc_placement_roots(
        session,
        selected_root_id=policy.target_library_root_id,
    )
    sync_work = await _load_sync_work_summary(session, story_arc_id=story_arc_id)
    return StoryArcPlacementContextView(
        policy=_placement_policy_view(policy),
        roots=roots,
        roots_truncated=roots_truncated,
        preview=StoryArcPlacementPreviewPageView(
            items=tuple(_preview_item_view(item) for item in preview.items),
            total=preview.total,
            page=safe_page,
            per_page=_PLACEMENT_UI_PAGE_SIZE,
            total_pages=preview_pages,
            has_more=preview.has_more,
        ),
        placements=StoryArcPlacementStatePageView(
            items=tuple(_placement_state_view(item) for item in placements.items),
            total=placements.total,
            page=safe_page,
            per_page=_PLACEMENT_UI_PAGE_SIZE,
            total_pages=placement_pages,
            has_more=placements.has_more,
        ),
        sync_work=sync_work,
        page=safe_page,
        total_pages=total_pages,
        preview_only=proposal is not None,
    )
