"""First-class logical story-arc API routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, NoReturn

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError

from pullbox.api.deps import AuthenticatedUser, DbSession  # noqa: TC001
from pullbox.config import get_settings
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcLifecycle,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.schemas.pagination import PaginatedResponse
from pullbox.schemas.story_arc import (
    StoryArcCreate,
    StoryArcMembershipCreate,
    StoryArcMembershipOrderResponse,
    StoryArcMembershipReorder,
    StoryArcMembershipResolve,
    StoryArcMembershipResponse,
    StoryArcMembershipUpdate,
    StoryArcResponse,
    StoryArcUpdate,
)
from pullbox.services.story_arc_service import (
    StoryArcConflictError,
    StoryArcNotFoundError,
    StoryArcService,
    StoryArcServiceError,
    StoryArcValidationError,
)

if TYPE_CHECKING:
    from sqlalchemy.sql.elements import ColumnElement
    from sqlalchemy.sql.selectable import Subquery

router = APIRouter(prefix="/story-arcs", tags=["story-arcs"])

_story_arc_service = StoryArcService()


def _membership_counts_subquery() -> Subquery:
    """Aggregate story-arc membership states without hydrating collections."""
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


def _arc_response(
    arc: StoryArc,
    *,
    membership_count: int = 0,
    resolved_count: int = 0,
    missing_count: int = 0,
    conflict_count: int = 0,
) -> StoryArcResponse:
    """Serialize one arc with already-computed state counts."""
    return StoryArcResponse.model_validate(
        {
            "id": arc.id,
            "name": arc.name,
            "normalized_name": arc.normalized_name,
            "description": arc.description,
            "comicvine_id": arc.comicvine_id,
            "comicvine_url": arc.comicvine_url,
            "source_kind": arc.source_kind,
            "lifecycle": arc.lifecycle,
            "monitored": arc.monitored,
            "search_missing": arc.search_missing,
            "include_upcoming": arc.include_upcoming,
            "sync_enabled": arc.sync_enabled,
            "target_library_root_id": arc.target_library_root_id,
            "revision": arc.revision,
            "membership_count": membership_count,
            "resolved_count": resolved_count,
            "missing_count": missing_count,
            "conflict_count": conflict_count,
            "created_at": arc.created_at,
            "updated_at": arc.updated_at,
        }
    )


async def _load_arc_response(session: DbSession, story_arc_id: int) -> StoryArcResponse:
    """Load an arc and its aggregate counts or return a stable 404."""
    counts = _membership_counts_subquery()
    row = (
        await session.execute(
            select(
                StoryArc,
                func.coalesce(counts.c.membership_count, 0),
                func.coalesce(counts.c.resolved_count, 0),
                func.coalesce(counts.c.missing_count, 0),
                func.coalesce(counts.c.conflict_count, 0),
            )
            .outerjoin(counts, counts.c.story_arc_id == StoryArc.id)
            .where(StoryArc.id == story_arc_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Story arc {story_arc_id} was not found")
    arc, membership_count, resolved_count, missing_count, conflict_count = row
    return _arc_response(
        arc,
        membership_count=int(membership_count),
        resolved_count=int(resolved_count),
        missing_count=int(missing_count),
        conflict_count=int(conflict_count),
    )


async def _require_arc(session: DbSession, story_arc_id: int) -> StoryArc:
    """Return one canonical arc or a safe API 404."""
    arc = await session.get(StoryArc, story_arc_id)
    if arc is None:
        raise HTTPException(status_code=404, detail=f"Story arc {story_arc_id} was not found")
    return arc


async def _require_membership(
    session: DbSession,
    story_arc_id: int,
    membership_id: int,
) -> IssueStoryArc:
    """Protect nested routes from mutating a membership in another arc."""
    membership = await session.get(IssueStoryArc, membership_id)
    if membership is None or membership.story_arc_id != story_arc_id:
        raise HTTPException(
            status_code=404,
            detail=f"Story-arc membership {membership_id} was not found",
        )
    return membership


def _raise_service_error(exc: StoryArcServiceError) -> NoReturn:
    """Translate domain failures into deterministic REST responses."""
    if isinstance(exc, StoryArcNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, StoryArcConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, StoryArcValidationError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


def _raise_integrity_conflict(exc: IntegrityError) -> NoReturn:
    """Hide backend constraint details while preserving conflict semantics."""
    raise HTTPException(
        status_code=409,
        detail="Story arc mutation conflicts with existing data",
    ) from exc


def _escaped_contains_pattern(value: str) -> str:
    """Treat user search text literally inside a portable LIKE expression."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("", response_model=PaginatedResponse[StoryArcResponse])
async def list_story_arcs(
    _user: AuthenticatedUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    q: Annotated[str | None, Query(max_length=500)] = None,
    lifecycle: Annotated[StoryArcLifecycle | None, Query()] = None,
    monitored: Annotated[bool | None, Query()] = None,
    source_kind: Annotated[StoryArcSourceKind | None, Query()] = None,
) -> PaginatedResponse[StoryArcResponse]:
    """List bounded story arcs with filters, literal search, and state counts."""
    filters: list[ColumnElement[bool]] = []
    if q is not None and (search_text := q.strip()):
        filters.append(StoryArc.name.ilike(_escaped_contains_pattern(search_text), escape="\\"))
    if lifecycle is not None:
        filters.append(StoryArc.lifecycle == lifecycle)
    if monitored is not None:
        filters.append(StoryArc.monitored.is_(monitored))
    if source_kind is not None:
        filters.append(StoryArc.source_kind == source_kind)

    total = int(await session.scalar(select(func.count(StoryArc.id)).where(*filters)) or 0)
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
            .limit(limit)
            .offset(offset)
        )
    ).all()
    items = [
        _arc_response(
            arc,
            membership_count=int(membership_count),
            resolved_count=int(resolved_count),
            missing_count=int(missing_count),
            conflict_count=int(conflict_count),
        )
        for arc, membership_count, resolved_count, missing_count, conflict_count in rows
    ]
    return PaginatedResponse[StoryArcResponse](
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + limit) < total,
    )


@router.post("", response_model=StoryArcResponse, status_code=status.HTTP_201_CREATED)
async def create_story_arc(
    body: StoryArcCreate,
    _user: AuthenticatedUser,
    session: DbSession,
) -> StoryArcResponse:
    """Create and commit one Pullbox-owned logical story arc."""
    if not get_settings().story_arc_manual_create_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    try:
        arc = await _story_arc_service.create(
            session,
            name=body.name,
            description=body.description,
            monitored=body.monitored,
            search_missing=body.search_missing,
            include_upcoming=body.include_upcoming,
            sync_enabled=body.sync_enabled,
            source_kind=StoryArcSourceKind.PULLBOX,
        )
        story_arc_id = arc.id
        await session.commit()
    except StoryArcServiceError as exc:
        _raise_service_error(exc)
    except IntegrityError as exc:
        _raise_integrity_conflict(exc)
    return await _load_arc_response(session, story_arc_id)


@router.get("/{story_arc_id}", response_model=StoryArcResponse)
async def get_story_arc(
    story_arc_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> StoryArcResponse:
    """Read one logical story arc with membership counts."""
    return await _load_arc_response(session, story_arc_id)


@router.patch("/{story_arc_id}", response_model=StoryArcResponse)
async def update_story_arc(
    story_arc_id: int,
    body: StoryArcUpdate,
    _user: AuthenticatedUser,
    session: DbSession,
) -> StoryArcResponse:
    """Patch arc metadata using one optimistic revision token."""
    existing = await _require_arc(session, story_arc_id)
    fields = body.model_fields_set
    try:
        await _story_arc_service.update(
            session,
            story_arc_id,
            expected_revision=body.expected_revision,
            name=body.name if "name" in fields and body.name is not None else existing.name,
            description=body.description if "description" in fields else existing.description,
            monitored=body.monitored if body.monitored is not None else existing.monitored,
            search_missing=(
                body.search_missing if body.search_missing is not None else existing.search_missing
            ),
            include_upcoming=(
                body.include_upcoming
                if body.include_upcoming is not None
                else existing.include_upcoming
            ),
            sync_enabled=(
                body.sync_enabled if body.sync_enabled is not None else existing.sync_enabled
            ),
        )
        await session.commit()
    except StoryArcServiceError as exc:
        _raise_service_error(exc)
    except IntegrityError as exc:
        _raise_integrity_conflict(exc)
    return await _load_arc_response(session, story_arc_id)


@router.delete("/{story_arc_id}", response_model=StoryArcResponse)
async def archive_story_arc(
    story_arc_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
    expected_revision: Annotated[int, Query(ge=1)],
) -> StoryArcResponse:
    """Safely archive an arc without deleting memberships or canonical issues."""
    try:
        await _story_arc_service.archive(
            session,
            story_arc_id,
            expected_revision=expected_revision,
        )
        await session.commit()
    except StoryArcServiceError as exc:
        _raise_service_error(exc)
    except IntegrityError as exc:
        _raise_integrity_conflict(exc)
    return await _load_arc_response(session, story_arc_id)


@router.get(
    "/{story_arc_id}/memberships",
    response_model=PaginatedResponse[StoryArcMembershipResponse],
)
async def list_story_arc_memberships(
    story_arc_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PaginatedResponse[StoryArcMembershipResponse]:
    """List a bounded page in deterministic reading order."""
    await _require_arc(session, story_arc_id)
    total = int(
        await session.scalar(
            select(func.count(IssueStoryArc.id)).where(IssueStoryArc.story_arc_id == story_arc_id)
        )
        or 0
    )
    memberships = list(
        (
            await session.scalars(
                select(IssueStoryArc)
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
    )
    return PaginatedResponse[StoryArcMembershipResponse](
        items=[StoryArcMembershipResponse.model_validate(item) for item in memberships],
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + limit) < total,
    )


@router.post(
    "/{story_arc_id}/memberships",
    response_model=StoryArcMembershipResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_story_arc_membership(
    story_arc_id: int,
    body: StoryArcMembershipCreate,
    _user: AuthenticatedUser,
    session: DbSession,
) -> StoryArcMembershipResponse:
    """Add one resolved or unresolved membership and commit it."""
    try:
        membership = await _story_arc_service.add_membership(
            session,
            story_arc_id,
            issue_id=body.issue_id,
            sequence_number=body.sequence_number,
            source_ordinal=body.source_ordinal,
            source_issue_number_text=body.source_issue_number_text,
            source_kind=StoryArcSourceKind.PULLBOX,
        )
        await session.commit()
    except StoryArcServiceError as exc:
        _raise_service_error(exc)
    except IntegrityError as exc:
        _raise_integrity_conflict(exc)
    return StoryArcMembershipResponse.model_validate(membership)


@router.put(
    "/{story_arc_id}/memberships/reorder",
    response_model=StoryArcMembershipOrderResponse,
)
async def reorder_story_arc_memberships(
    story_arc_id: int,
    body: StoryArcMembershipReorder,
    _user: AuthenticatedUser,
    session: DbSession,
) -> StoryArcMembershipOrderResponse:
    """Atomically apply one complete, duplicate-free membership order."""
    try:
        memberships = await _story_arc_service.reorder_memberships(
            session,
            story_arc_id,
            ordered_membership_ids=body.membership_ids,
            expected_revision=body.expected_revision,
        )
        arc = await _require_arc(session, story_arc_id)
        revision = arc.revision
        await session.commit()
    except StoryArcServiceError as exc:
        _raise_service_error(exc)
    except IntegrityError as exc:
        _raise_integrity_conflict(exc)
    return StoryArcMembershipOrderResponse(
        items=[StoryArcMembershipResponse.model_validate(item) for item in memberships],
        revision=revision,
    )


@router.patch(
    "/{story_arc_id}/memberships/{membership_id}",
    response_model=StoryArcMembershipResponse,
)
async def update_story_arc_membership(
    story_arc_id: int,
    membership_id: int,
    body: StoryArcMembershipUpdate,
    _user: AuthenticatedUser,
    session: DbSession,
) -> StoryArcMembershipResponse:
    """Patch order, exact source number, or intentional skip state."""
    await _require_membership(session, story_arc_id, membership_id)
    try:
        membership = await _story_arc_service.update_membership(
            session,
            membership_id,
            sequence_number=body.sequence_number,
            source_ordinal=body.source_ordinal,
            source_issue_number_text=body.source_issue_number_text,
            intentionally_skipped=body.intentionally_skipped,
        )
        await session.commit()
    except StoryArcServiceError as exc:
        _raise_service_error(exc)
    except IntegrityError as exc:
        _raise_integrity_conflict(exc)
    return StoryArcMembershipResponse.model_validate(membership)


@router.post(
    "/{story_arc_id}/memberships/{membership_id}/resolve",
    response_model=StoryArcMembershipResponse,
)
async def resolve_story_arc_membership(
    story_arc_id: int,
    membership_id: int,
    body: StoryArcMembershipResolve,
    _user: AuthenticatedUser,
    session: DbSession,
) -> StoryArcMembershipResponse:
    """Resolve an entry only to an existing canonical issue."""
    await _require_membership(session, story_arc_id, membership_id)
    try:
        membership = await _story_arc_service.resolve_membership(
            session,
            membership_id,
            issue_id=body.issue_id,
        )
        await session.commit()
    except StoryArcServiceError as exc:
        _raise_service_error(exc)
    except IntegrityError as exc:
        _raise_integrity_conflict(exc)
    return StoryArcMembershipResponse.model_validate(membership)


@router.delete(
    "/{story_arc_id}/memberships/{membership_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_story_arc_membership(
    story_arc_id: int,
    membership_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Remove only the association, never its canonical issue."""
    await _require_membership(session, story_arc_id, membership_id)
    try:
        await _story_arc_service.remove_membership(session, membership_id)
        await session.commit()
    except StoryArcServiceError as exc:
        _raise_service_error(exc)
    except IntegrityError as exc:
        _raise_integrity_conflict(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
