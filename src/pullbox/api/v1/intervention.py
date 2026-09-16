"""Intervention queue API routes — list, approve, and reject pending matches."""

from __future__ import annotations

import math
from typing import Annotated

import structlog
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import delete, func, select
from sqlalchemy.orm import joinedload

from pullbox.api.deps import AuthenticatedUser, DbSession  # noqa: TC001
from pullbox.core.exceptions import NotFoundError
from pullbox.models.direct_acquisition import DirectAcquisitionAttempt
from pullbox.models.issue import Issue
from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
from pullbox.schemas.intervention import (
    ApproveResponse,
    BulkActionRequest,
    BulkActionResponse,
    BulkActionResult,
    IssueContext,
    PendingMatchCountResponse,
    PendingMatchListResponse,
    PendingMatchResponse,
    RejectRequest,
)
from pullbox.services.intervention_service import (
    InterventionService,
    is_direct_pending_match,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/intervention", tags=["intervention"])


def _resolve_status(status: str | None) -> PendingMatchStatus:
    """Parse status query param, defaulting to PENDING."""
    if status is not None:
        try:
            return PendingMatchStatus(status)
        except ValueError:
            return PendingMatchStatus.PENDING
    return PendingMatchStatus.PENDING


@router.get("", response_model=PendingMatchListResponse)
async def list_pending_matches(
    user: AuthenticatedUser,
    session: DbSession,
    status: Annotated[str | None, Query()] = None,
    series_id: Annotated[int | None, Query(gt=0)] = None,
    confidence: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PendingMatchListResponse:
    """List pending matches with optional filters."""
    filter_status = _resolve_status(status)

    # Build query with eager loading for issue → series
    query = (
        select(PendingMatch)
        .options(
            joinedload(PendingMatch.issue).selectinload(Issue.series),
        )
        .where(PendingMatch.status == filter_status)
    )
    count_query = select(func.count(PendingMatch.id)).where(PendingMatch.status == filter_status)

    if series_id is not None:
        query = query.join(Issue, PendingMatch.issue_id == Issue.id).where(
            Issue.series_id == series_id
        )
        count_query = count_query.join(Issue, PendingMatch.issue_id == Issue.id).where(
            Issue.series_id == series_id
        )

    if confidence is not None:
        query = query.where(PendingMatch.confidence == confidence)
        count_query = count_query.where(PendingMatch.confidence == confidence)

    total: int = (await session.execute(count_query)).scalar_one()

    query = query.order_by(PendingMatch.created_at.desc())
    query = query.offset((page - 1) * per_page).limit(per_page)

    result = await session.execute(query)
    items = list(result.unique().scalars().all())

    pages = max(1, math.ceil(total / per_page))

    return PendingMatchListResponse(
        items=[_to_response(pm) for pm in items],
        total=total,
        page=page,
        pages=pages,
    )


@router.get("/count", response_model=PendingMatchCountResponse)
async def pending_match_count(
    user: AuthenticatedUser,
    session: DbSession,
) -> PendingMatchCountResponse:
    """Get the count of pending matches (PENDING status only)."""
    svc = InterventionService()
    count = await svc.get_pending_count(session)
    return PendingMatchCountResponse(count=count)


@router.delete("/history")
async def clear_intervention_history(
    _user: AuthenticatedUser,
    session: DbSession,
) -> dict[str, int]:
    """Delete resolved intervention history entries, preserving pending work."""
    cursor_result = await session.execute(
        delete(PendingMatch).where(PendingMatch.status != PendingMatchStatus.PENDING)
    )
    count: int = cursor_result.rowcount  # type: ignore[attr-defined]
    logger.info("intervention_history_cleared", count=count)
    return {"deleted": count}


@router.delete("/history/{pending_id}", status_code=204)
async def remove_intervention_history_entry(
    pending_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> None:
    """Delete one resolved intervention history entry."""
    pending_match = await session.get(PendingMatch, pending_id)
    if pending_match is None:
        raise NotFoundError("PendingMatch", pending_id)
    if pending_match.status == PendingMatchStatus.PENDING:
        raise HTTPException(
            status_code=409,
            detail="Pending matches must be resolved from the queue.",
        )

    await session.delete(pending_match)
    logger.info("intervention_history_removed", pending_id=pending_id)


@router.post("/bulk-approve", response_model=BulkActionResponse)
async def bulk_approve(
    body: BulkActionRequest,
    user: AuthenticatedUser,
    session: DbSession,
) -> BulkActionResponse:
    """Approve multiple pending matches in one request."""
    from pullbox.composition.services import build_domain_download_service

    built = await build_domain_download_service(session)
    download_svc = built[0] if built is not None else None
    svc = InterventionService(download_service=download_svc)

    results: list[BulkActionResult] = []
    for pm_id in body.ids:
        try:
            await svc.approve_match(session, pm_id)
            results.append(BulkActionResult(id=pm_id, success=True))
        except (ValueError, RuntimeError, Exception) as exc:
            logger.warning("bulk_approve_item_failed", pending_id=pm_id, error=str(exc))
            results.append(BulkActionResult(id=pm_id, success=False, error=str(exc)))

    succeeded = sum(1 for r in results if r.success)
    logger.info(
        "bulk_approve_completed", total=len(results), succeeded=succeeded, user=user.username
    )

    return BulkActionResponse(
        processed=len(results),
        succeeded=succeeded,
        failed=len(results) - succeeded,
        results=results,
    )


@router.post("/bulk-reject", response_model=BulkActionResponse)
async def bulk_reject(
    body: BulkActionRequest,
    user: AuthenticatedUser,
    session: DbSession,
) -> BulkActionResponse:
    """Reject multiple pending matches in one request."""
    svc = InterventionService()

    results: list[BulkActionResult] = []
    for pm_id in body.ids:
        try:
            await svc.reject_match(session, pm_id, reason=body.reason)
            results.append(BulkActionResult(id=pm_id, success=True))
        except (ValueError, Exception) as exc:
            logger.warning("bulk_reject_item_failed", pending_id=pm_id, error=str(exc))
            results.append(BulkActionResult(id=pm_id, success=False, error=str(exc)))

    succeeded = sum(1 for r in results if r.success)
    logger.info(
        "bulk_reject_completed",
        total=len(results),
        succeeded=succeeded,
        user=user.username,
        reason=body.reason,
    )

    return BulkActionResponse(
        processed=len(results),
        succeeded=succeeded,
        failed=len(results) - succeeded,
        results=results,
    )


@router.get("/{pending_id}", response_model=PendingMatchResponse)
async def get_pending_match(
    pending_id: int,
    user: AuthenticatedUser,
    session: DbSession,
) -> PendingMatchResponse:
    """Get a single pending match by ID."""
    # Eager-load issue → series to avoid lazy-load greenlet errors
    query = (
        select(PendingMatch)
        .options(
            joinedload(PendingMatch.issue).selectinload(Issue.series),
        )
        .where(PendingMatch.id == pending_id)
    )
    result = await session.execute(query)
    pm = result.unique().scalar_one_or_none()
    if pm is None:
        raise NotFoundError("PendingMatch", pending_id)
    return _to_response(pm)


@router.post("/{pending_id}/approve", response_model=ApproveResponse, status_code=201)
async def approve_pending_match(
    pending_id: int,
    user: AuthenticatedUser,
    session: DbSession,
) -> ApproveResponse:
    """Approve a pending match — sends the release to the download client."""
    pm = await session.get(PendingMatch, pending_id)
    if pm is None:
        raise NotFoundError("PendingMatch", pending_id)

    if is_direct_pending_match(pm) or (pm.match_details or {}).get("source_kind") == "dc":
        svc = InterventionService()
    else:
        from pullbox.composition.services import build_domain_download_service

        built = await build_domain_download_service(session)
        if built is None:
            from pullbox.core.exceptions import ProviderError

            raise ProviderError("download", "No download clients configured")

        download_svc, _configs = built
        svc = InterventionService(download_service=download_svc)

    try:
        approved = await svc.approve_match(session, pending_id)
    except ValueError as exc:
        raise NotFoundError("PendingMatch", pending_id) from exc

    logger.info(
        "intervention_approved",
        pending_id=pending_id,
        issue_id=pm.issue_id,
        user=user.username,
    )

    if isinstance(approved, DirectAcquisitionAttempt):
        return ApproveResponse(
            acquisition_id=approved.id,
            issue_id=approved.issue_id,
            title=str(approved.candidate_snapshot.get("display_title") or pm.release_title),
            status=str(approved.state),
            source_kind="direct",
        )
    return ApproveResponse(
        download_id=approved.id,
        issue_id=approved.issue_id,
        title=approved.title,
        status=str(approved.state),
        source_kind="dc" if (pm.match_details or {}).get("source_kind") == "dc" else "indexer",
    )


@router.post("/{pending_id}/reject")
async def reject_pending_match(
    pending_id: int,
    user: AuthenticatedUser,
    session: DbSession,
    body: RejectRequest | None = None,
) -> dict[str, str]:
    """Reject a pending match with an optional reason."""
    pm = await session.get(PendingMatch, pending_id)
    if pm is None:
        raise NotFoundError("PendingMatch", pending_id)

    svc = InterventionService()
    try:
        await svc.reject_match(session, pending_id, reason=body.reason if body else None)
    except ValueError as exc:
        raise NotFoundError("PendingMatch", pending_id) from exc

    logger.info(
        "intervention_rejected",
        pending_id=pending_id,
        issue_id=pm.issue_id,
        user=user.username,
        reason=body.reason if body else None,
    )

    return {"message": "Match rejected."}


def _to_response(pm: PendingMatch) -> PendingMatchResponse:
    """Convert a PendingMatch ORM object to a response schema."""
    issue = pm.issue
    series_title: str | None = None
    # issue.series is eagerly loaded by the endpoint queries
    if issue and issue.series:
        series_title = str(issue.series.title)

    issue_ctx = IssueContext(
        id=issue.id if issue else 0,
        series_title=series_title,
        issue_number=float(issue.issue_number) if issue else 0.0,
        issue_type=str(issue.issue_type) if issue and issue.issue_type else None,
    )

    return PendingMatchResponse(
        id=pm.id,
        issue=issue_ctx,
        release_title=pm.release_title,
        download_url=pm.download_url,
        file_size=pm.file_size,
        confidence=pm.confidence,
        match_details=pm.match_details or {},
        is_torrent=pm.is_torrent,
        status=pm.status,
        created_at=pm.created_at,
        resolved_at=pm.resolved_at,
        resolved_by=pm.resolved_by,
    )
