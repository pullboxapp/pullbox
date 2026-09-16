"""Intervention queue and history context loaders."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import cast as typing_cast

from sqlalchemy import ColumnElement, String, cast, func, or_, select
from sqlalchemy.orm import joinedload

from pullbox.models.indexer import IndexerConfig
from pullbox.models.issue import Issue
from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
from pullbox.models.series import Series
from pullbox.ui.intervention_filter_helpers import (
    INTERVENTION_REASON_LABELS,
    build_intervention_item_meta,
    get_intervention_history_order_by,
    intervention_lane_clause,
    intervention_protocol_clause,
    intervention_review_reason_clause,
    intervention_source_expr,
    normalize_intervention_confidence_filter,
    normalize_intervention_history_sort,
    normalize_intervention_outcome_filter,
    normalize_intervention_protocol_filter,
    normalize_intervention_reason_filter,
    normalize_intervention_tab,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

INTERVENTION_PAGE_SIZE = 25


async def load_intervention_source_options(
    session: AsyncSession,
    *,
    status_filter: PendingMatchStatus,
    current_source: str | None,
) -> list[tuple[str, str]]:
    """Return distinct source options for the intervention toolbar."""
    source_expr = intervention_source_expr()
    result = await session.execute(
        select(source_expr)
        .select_from(PendingMatch)
        .outerjoin(IndexerConfig, PendingMatch.indexer_id == IndexerConfig.id)
        .where(PendingMatch.status == status_filter)
        .distinct()
        .order_by(source_expr.asc())
    )

    options: list[tuple[str, str]] = [("", "All Sources")]
    seen_sources: set[str] = set()
    for source_value in result.scalars().all():
        normalized_value = str(source_value or "Unknown").strip() or "Unknown"
        if normalized_value in seen_sources:
            continue
        seen_sources.add(normalized_value)
        options.append((normalized_value, normalized_value))

    if current_source and current_source not in seen_sources:
        options.append((current_source, current_source))

    return options


def build_intervention_queue_filters(
    *,
    reason_filter: str | None = None,
    confidence_filter: str | None = None,
    protocol_filter: str | None = None,
    search_query: str | None = None,
    lane: str = "review",
) -> tuple[list[ColumnElement[bool]], str, str, str, str]:
    """Build normalized queue filters for intervention queue queries."""
    from pullbox.core.db_utils import escape_like

    normalized_reason = normalize_intervention_reason_filter(reason_filter)
    normalized_confidence = normalize_intervention_confidence_filter(confidence_filter)
    normalized_protocol = normalize_intervention_protocol_filter(protocol_filter)
    normalized_search = (search_query or "").strip()
    source_expr = intervention_source_expr()

    filters: list[ColumnElement[bool]] = [
        PendingMatch.status == PendingMatchStatus.PENDING,
        intervention_lane_clause(lane),
    ]
    if normalized_reason:
        filters.append(intervention_review_reason_clause(normalized_reason))
    if normalized_confidence:
        filters.append(PendingMatch.confidence == normalized_confidence)
    if normalized_protocol:
        filters.append(intervention_protocol_clause(normalized_protocol))
    if normalized_search:
        search_term = f"%{escape_like(normalized_search)}%"
        filters.append(
            or_(
                PendingMatch.release_title.ilike(search_term),
                Series.title.ilike(search_term),
                cast(Issue.issue_number, String).ilike(search_term),
                source_expr.ilike(search_term),
            )
        )

    return (
        filters,
        normalized_reason,
        normalized_confidence,
        normalized_protocol,
        normalized_search,
    )


async def load_intervention_queue_context(
    session: AsyncSession,
    *,
    reason_filter: str | None = None,
    confidence_filter: str | None = None,
    protocol_filter: str | None = None,
    search_query: str | None = None,
    requested_page: int = 1,
    lane: str = "review",
) -> dict[str, object]:
    """Load queue-only intervention context for actionable pending matches."""
    (
        filters,
        normalized_reason,
        normalized_confidence,
        normalized_protocol,
        normalized_search,
    ) = build_intervention_queue_filters(
        reason_filter=reason_filter,
        confidence_filter=confidence_filter,
        protocol_filter=protocol_filter,
        search_query=search_query,
        lane=lane,
    )

    summary_result = await session.execute(
        select(PendingMatch.confidence, func.count(PendingMatch.id))
        .where(PendingMatch.status == PendingMatchStatus.PENDING, intervention_lane_clause(lane))
        .group_by(PendingMatch.confidence)
    )
    confidence_counts = {
        (typing_cast("str", confidence_value) or "").lower(): count
        for confidence_value, count in summary_result.all()
    }
    pending_count = sum(confidence_counts.values())

    filtered_count: int = (
        await session.execute(
            select(func.count(PendingMatch.id))
            .select_from(PendingMatch)
            .join(Issue, PendingMatch.issue_id == Issue.id)
            .join(Series, Issue.series_id == Series.id)
            .outerjoin(IndexerConfig, PendingMatch.indexer_id == IndexerConfig.id)
            .where(*filters)
        )
    ).scalar_one()

    total_pages = max(1, (filtered_count + INTERVENTION_PAGE_SIZE - 1) // INTERVENTION_PAGE_SIZE)
    page = min(max(1, requested_page), total_pages)

    pending_result = await session.execute(
        select(PendingMatch)
        .join(Issue, PendingMatch.issue_id == Issue.id)
        .join(Series, Issue.series_id == Series.id)
        .outerjoin(IndexerConfig, PendingMatch.indexer_id == IndexerConfig.id)
        .options(
            joinedload(PendingMatch.issue).joinedload(Issue.series),
            joinedload(PendingMatch.indexer),
        )
        .where(*filters)
        .order_by(PendingMatch.updated_at.desc(), PendingMatch.id.desc())
        .offset((page - 1) * INTERVENTION_PAGE_SIZE)
        .limit(INTERVENTION_PAGE_SIZE)
    )
    pending_matches = list(pending_result.unique().scalars().all())
    item_meta = {
        pending_match.id: build_intervention_item_meta(pending_match)
        for pending_match in pending_matches
    }

    return {
        "tab": "queue",
        "lane": lane,
        "pending_count": pending_count,
        "pending_matches": pending_matches,
        "intervention_item_meta": item_meta,
        "filtered_count": filtered_count,
        "visible_count": len(pending_matches),
        "page": page,
        "page_size": INTERVENTION_PAGE_SIZE,
        "total_pages": total_pages,
        "has_filters": bool(
            normalized_reason or normalized_confidence or normalized_protocol or normalized_search
        ),
        "reason_filter": normalized_reason,
        "confidence_filter": normalized_confidence,
        "protocol_filter": normalized_protocol,
        "search_query": normalized_search,
        "review_reason_options": [
            ("", "All Reasons"),
            ("fuzzy_series", INTERVENTION_REASON_LABELS["fuzzy_series"]),
            ("issue_mismatch", INTERVENTION_REASON_LABELS["issue_mismatch"]),
            ("year_mismatch", INTERVENTION_REASON_LABELS["year_mismatch"]),
            ("type_mismatch", INTERVENTION_REASON_LABELS["type_mismatch"]),
            ("size_warning", INTERVENTION_REASON_LABELS["size_warning"]),
        ],
        "protocol_options": [
            ("", "All Protocols"),
            ("usenet", "Usenet"),
            ("torrent", "Torrent"),
            ("direct", "Direct"),
            ("dc", "Direct Connect"),
        ],
        "high_count": confidence_counts.get("high", 0),
        "medium_count": confidence_counts.get("medium", 0),
        "low_count": confidence_counts.get("low", 0),
    }


async def load_intervention_history_context(
    session: AsyncSession,
    *,
    outcome_filter: str | None = None,
    confidence_filter: str | None = None,
    protocol_filter: str | None = None,
    search_query: str | None = None,
    sort: str | None = None,
    requested_page: int = 1,
) -> dict[str, object]:
    """Load history-only intervention context for resolved review items."""
    from pullbox.core.db_utils import escape_like

    normalized_outcome = normalize_intervention_outcome_filter(outcome_filter)
    normalized_confidence = normalize_intervention_confidence_filter(confidence_filter)
    normalized_protocol = normalize_intervention_protocol_filter(protocol_filter)
    normalized_search = (search_query or "").strip()
    normalized_sort = normalize_intervention_history_sort(sort)
    source_expr = intervention_source_expr()
    summary_filters: list[ColumnElement[bool]] = [PendingMatch.status != PendingMatchStatus.PENDING]

    filters: list[ColumnElement[bool]] = summary_filters.copy()
    if normalized_outcome:
        outcome_clause = PendingMatch.status == PendingMatchStatus(normalized_outcome)
        filters.append(outcome_clause)
        summary_filters.append(outcome_clause)
    if normalized_confidence:
        filters.append(PendingMatch.confidence == normalized_confidence)
    if normalized_protocol:
        filters.append(intervention_protocol_clause(normalized_protocol))
    if normalized_search:
        search_term = f"%{escape_like(normalized_search)}%"
        search_clause = or_(
            PendingMatch.release_title.ilike(search_term),
            Series.title.ilike(search_term),
            cast(Issue.issue_number, String).ilike(search_term),
            source_expr.ilike(search_term),
        )
        filters.append(search_clause)
        summary_filters.append(search_clause)

    if normalized_confidence:
        summary_filters.append(PendingMatch.confidence == normalized_confidence)
    if normalized_protocol:
        summary_filters.append(intervention_protocol_clause(normalized_protocol))

    summary_result = await session.execute(
        select(PendingMatch.status, func.count(PendingMatch.id))
        .select_from(PendingMatch)
        .join(Issue, PendingMatch.issue_id == Issue.id)
        .join(Series, Issue.series_id == Series.id)
        .outerjoin(IndexerConfig, PendingMatch.indexer_id == IndexerConfig.id)
        .where(*summary_filters)
        .group_by(PendingMatch.status)
    )
    status_counts = {
        (typing_cast("str", status_value) or "").lower(): count
        for status_value, count in summary_result.all()
    }

    filtered_count: int = (
        await session.execute(
            select(func.count(PendingMatch.id))
            .select_from(PendingMatch)
            .join(Issue, PendingMatch.issue_id == Issue.id)
            .join(Series, Issue.series_id == Series.id)
            .outerjoin(IndexerConfig, PendingMatch.indexer_id == IndexerConfig.id)
            .where(*filters)
        )
    ).scalar_one()

    total_pages = max(1, (filtered_count + INTERVENTION_PAGE_SIZE - 1) // INTERVENTION_PAGE_SIZE)
    page = min(max(1, requested_page), total_pages)

    history_result = await session.execute(
        select(PendingMatch)
        .join(Issue, PendingMatch.issue_id == Issue.id)
        .join(Series, Issue.series_id == Series.id)
        .outerjoin(IndexerConfig, PendingMatch.indexer_id == IndexerConfig.id)
        .options(
            joinedload(PendingMatch.issue).joinedload(Issue.series),
            joinedload(PendingMatch.indexer),
        )
        .where(*filters)
        .order_by(*get_intervention_history_order_by(normalized_sort))
        .offset((page - 1) * INTERVENTION_PAGE_SIZE)
        .limit(INTERVENTION_PAGE_SIZE)
    )
    history_items = list(history_result.unique().scalars().all())
    item_meta = {
        pending_match.id: build_intervention_item_meta(pending_match)
        for pending_match in history_items
    }

    return {
        "tab": "history",
        "history_total": filtered_count,
        "history_items": history_items,
        "intervention_item_meta": item_meta,
        "page": page,
        "page_size": INTERVENTION_PAGE_SIZE,
        "total_pages": total_pages,
        "has_filters": bool(
            normalized_outcome or normalized_confidence or normalized_protocol or normalized_search
        ),
        "outcome_filter": normalized_outcome,
        "confidence_filter": normalized_confidence,
        "protocol_filter": normalized_protocol,
        "search_query": normalized_search,
        "sort": normalized_sort,
        "history_approved_count": status_counts.get("approved", 0),
        "history_rejected_count": status_counts.get("rejected", 0),
        "history_expired_count": status_counts.get("expired", 0),
        "outcome_options": [
            ("", "All Outcomes"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
            ("expired", "Expired"),
        ],
        "protocol_options": [
            ("", "All Protocols"),
            ("usenet", "Usenet"),
            ("torrent", "Torrent"),
            ("direct", "Direct"),
            ("dc", "Direct Connect"),
        ],
    }


async def load_intervention_context(
    session: AsyncSession,
    *,
    tab: str | None = None,
    reason_filter: str | None = None,
    outcome_filter: str | None = None,
    confidence_filter: str | None = None,
    protocol_filter: str | None = None,
    search_query: str | None = None,
    sort: str | None = None,
    requested_page: int = 1,
) -> dict[str, object]:
    """Load the intervention workspace context for queue or history."""
    normalized_tab = normalize_intervention_tab(tab)
    queue_lane = "recovery" if normalized_tab == "recovery" else "review"

    summary_result = await session.execute(
        select(PendingMatch.confidence, func.count(PendingMatch.id))
        .where(
            PendingMatch.status == PendingMatchStatus.PENDING,
            intervention_lane_clause(queue_lane),
        )
        .group_by(PendingMatch.confidence)
    )
    confidence_counts = {
        (typing_cast("str", confidence_value) or "").lower(): count
        for confidence_value, count in summary_result.all()
    }
    active_pending_count = sum(confidence_counts.values())
    match_review_count: int = (
        await session.execute(
            select(func.count(PendingMatch.id)).where(
                PendingMatch.status == PendingMatchStatus.PENDING,
                intervention_lane_clause("review"),
            )
        )
    ).scalar_one()
    recovery_count: int = (
        await session.execute(
            select(func.count(PendingMatch.id)).where(
                PendingMatch.status == PendingMatchStatus.PENDING,
                intervention_lane_clause("recovery"),
            )
        )
    ).scalar_one()
    pending_count = match_review_count + recovery_count
    history_total: int = (
        await session.execute(
            select(func.count(PendingMatch.id)).where(
                PendingMatch.status != PendingMatchStatus.PENDING
            )
        )
    ).scalar_one()

    if normalized_tab == "history":
        active_context = await load_intervention_history_context(
            session,
            outcome_filter=outcome_filter,
            confidence_filter=confidence_filter,
            protocol_filter=protocol_filter,
            search_query=search_query,
            sort=sort,
            requested_page=requested_page,
        )
    else:
        active_context = await load_intervention_queue_context(
            session,
            reason_filter=reason_filter,
            confidence_filter=confidence_filter,
            protocol_filter=protocol_filter,
            search_query=search_query,
            requested_page=requested_page,
            lane=queue_lane,
        )

    active_context["tab"] = normalized_tab

    return {
        "tab": normalized_tab,
        "queue_count": pending_count,
        "history_total": history_total,
        "match_review_count": match_review_count,
        "recovery_count": recovery_count,
        **active_context,
        "pending_count": pending_count,
        "active_pending_count": active_pending_count,
        "high_count": confidence_counts.get("high", 0),
        "medium_count": confidence_counts.get("medium", 0),
        "low_count": confidence_counts.get("low", 0),
    }
