"""Series API routes — CRUD, monitoring, search, and metadata refresh."""

import asyncio
import json
import time
from typing import Any, Literal

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import case, func, inspect, select, update
from sqlalchemy.orm import contains_eager

from pullbox.api.deps import AuthenticatedUser, DbSession
from pullbox.core.events import IssueWanted, get_event_bus
from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.core.library_policy import load_search_on_add_default
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.search_log import SearchLog, SearchType
from pullbox.models.series import IssueCatalogState, Series, SeriesStatus
from pullbox.schemas.issue import IssueListResponse
from pullbox.schemas.pagination import PaginatedResponse
from pullbox.schemas.series import (
    SeriesBulkDelete,
    SeriesBulkUpdate,
    SeriesCreate,
    SeriesDeleteContextRequest,
    SeriesDeleteContextResponse,
    SeriesListResponse,
    SeriesResponse,
    SeriesUpdate,
)
from pullbox.services.cover_url_service import build_series_cover_url
from pullbox.services.metadata_service import MetadataService
from pullbox.services.search_targets import load_series_wanted_search_targets
from pullbox.services.series_service import SeriesService
from pullbox.tasks.search_task import search_series_issues

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/series", tags=["series"])

# Strong references to background tasks to prevent garbage collection
_background_tasks: set[asyncio.Task[object]] = set()
_BULK_UPDATE_CHUNK_SIZE = 500


# ── Helpers ───────────────────────────────────────────────────────────


def _issue_status_counts_subquery() -> Any:
    """Build per-series issue status counts without hydrating issue rows."""
    return (
        select(
            Issue.series_id.label("series_id"),
            func.coalesce(
                func.sum(case((Issue.status == IssueStatus.OWNED, 1), else_=0)),
                0,
            ).label("owned_count"),
            func.coalesce(
                func.sum(case((Issue.status == IssueStatus.WANTED, 1), else_=0)),
                0,
            ).label("wanted_count"),
        )
        .group_by(Issue.series_id)
        .subquery()
    )


def _enrich_series(
    series: Series,
    *,
    owned_count: int | None = None,
    wanted_count: int | None = None,
) -> dict[str, object]:
    """Add computed fields (publisher_name, owned_count, wanted_count) to a series."""
    mapper = inspect(type(series))
    data: dict[str, object] = {c.key: getattr(series, c.key) for c in mapper.columns}
    data["cover_path"] = build_series_cover_url(series)
    data["publisher_name"] = series.publisher.name if series.publisher else None
    data["owned_count"] = (
        int(owned_count)
        if owned_count is not None
        else sum(1 for i in series.issues if i.status == IssueStatus.OWNED)
    )
    data["wanted_count"] = (
        int(wanted_count)
        if wanted_count is not None
        else sum(1 for i in series.issues if i.status == IssueStatus.WANTED)
    )
    return data


def _enrich_series_list(
    series: Series,
    *,
    owned_count: int | None = None,
    wanted_count: int | None = None,
) -> dict[str, object]:
    """Add computed fields for list view."""
    return {
        "id": series.id,
        "title": series.title,
        "sort_title": series.sort_title,
        "year_start": series.year_start,
        "status": series.status,
        "series_type": series.series_type,
        "parent_series_id": series.parent_series_id,
        "monitored": series.monitored,
        "issue_count": series.issue_count,
        "publisher_name": series.publisher.name if series.publisher else None,
        "path": series.path,
        "library_root_id": series.library_root_id,
        "preferred_library_root_id": series.preferred_library_root_id,
        "owned_count": (
            int(owned_count)
            if owned_count is not None
            else sum(1 for i in series.issues if i.status == IssueStatus.OWNED)
        ),
        "wanted_count": (
            int(wanted_count)
            if wanted_count is not None
            else sum(1 for i in series.issues if i.status == IssueStatus.WANTED)
        ),
        "cover_path": build_series_cover_url(series),
    }


async def _load_series_response(session: DbSession, series_id: int) -> SeriesResponse:
    """Load a series with relationships and return a validated response."""
    issue_counts = _issue_status_counts_subquery()
    result = await session.execute(
        select(
            Series,
            func.coalesce(issue_counts.c.owned_count, 0).label("owned_count"),
            func.coalesce(issue_counts.c.wanted_count, 0).label("wanted_count"),
        )
        .outerjoin(Series.publisher)
        .outerjoin(issue_counts, issue_counts.c.series_id == Series.id)
        .options(contains_eager(Series.publisher))
        .where(Series.id == series_id)
    )
    row = result.unique().one_or_none()
    if row is None:
        raise NotFoundError("Series", series_id)
    series, owned_count, wanted_count = row
    return SeriesResponse.model_validate(
        _enrich_series(
            series,
            owned_count=int(owned_count or 0),
            wanted_count=int(wanted_count or 0),
        )
    )


async def _build_metadata_service(session: DbSession) -> MetadataService:
    """Construct a MetadataService using the DB-stored (encrypted) API key."""
    from pullbox.composition.services import build_metadata_service

    return await build_metadata_service(session)


async def _build_series_service(session: DbSession) -> SeriesService:
    """Construct a SeriesService while preserving the metadata-service patch seam."""
    from pullbox.composition.services import build_series_service

    metadata_svc = await _build_metadata_service(session)
    return build_series_service(metadata_svc)


# ── List ──────────────────────────────────────────────────────────────


@router.get("", response_model=PaginatedResponse[SeriesListResponse])
async def list_series(
    _user: AuthenticatedUser,
    session: DbSession,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    publisher_id: int | None = Query(None, description="Filter by publisher"),
    status: SeriesStatus | None = Query(None, description="Filter by series status"),  # noqa: B008
    monitored: bool | None = Query(None, description="Filter by monitored flag"),
    year: int | None = Query(None, description="Filter by start year"),
    sort: Literal["title", "year", "date_added"] = Query("title", description="Sort field"),
    order: Literal["asc", "desc"] = Query("asc", description="Sort order"),
) -> PaginatedResponse[SeriesListResponse]:
    """List all series with pagination, filtering, and sorting."""
    # Build filter conditions once
    filters = []
    if publisher_id is not None:
        filters.append(Series.publisher_id == publisher_id)
    if status is not None:
        filters.append(Series.status == status)
    if monitored is not None:
        filters.append(Series.monitored == monitored)
    if year is not None:
        filters.append(Series.year_start == year)

    total = (await session.execute(select(func.count(Series.id)).where(*filters))).scalar_one()

    # Apply sorting
    sort_column = {
        "title": Series.sort_title,
        "year": Series.year_start,
        "date_added": Series.created_at,
    }[sort]
    order_clause = sort_column.desc() if order == "desc" else sort_column.asc()

    issue_counts = _issue_status_counts_subquery()

    query = (
        select(
            Series,
            func.coalesce(issue_counts.c.owned_count, 0).label("owned_count"),
            func.coalesce(issue_counts.c.wanted_count, 0).label("wanted_count"),
        )
        .outerjoin(Series.publisher)
        .outerjoin(issue_counts, issue_counts.c.series_id == Series.id)
        .options(contains_eager(Series.publisher))
        .where(*filters)
        .order_by(order_clause)
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(query)
    rows = result.unique().all()

    items = [
        SeriesListResponse.model_validate(
            _enrich_series_list(
                series,
                owned_count=int(owned_count or 0),
                wanted_count=int(wanted_count or 0),
            )
        )
        for series, owned_count, wanted_count in rows
    ]
    return PaginatedResponse[SeriesListResponse](
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + limit) < total,
    )


# ── Bulk operations (before /{series_id} to avoid path param conflict) ──


@router.patch("/bulk", status_code=200)
async def bulk_update_series(
    body: SeriesBulkUpdate,
    _user: AuthenticatedUser,
    session: DbSession,
) -> dict[str, int]:
    """Bulk-update monitoring and reconcile the related issue states."""
    series_ids = list(dict.fromkeys(body.series_ids))
    updated = 0
    issues_updated = 0
    event_bus = get_event_bus()
    for offset in range(0, len(series_ids), _BULK_UPDATE_CHUNK_SIZE):
        chunk = series_ids[offset : offset + _BULK_UPDATE_CHUNK_SIZE]
        result = await session.execute(
            update(Series).where(Series.id.in_(chunk)).values(monitored=body.monitored)
        )
        updated += int(result.rowcount or 0)  # type: ignore[attr-defined]

        if body.monitored:
            issue_result = await session.execute(
                update(Issue)
                .where(
                    Issue.series_id.in_(chunk),
                    Issue.status == IssueStatus.SKIPPED,
                    Issue.manual_skip.is_(False),
                )
                .values(status=IssueStatus.WANTED)
                .returning(Issue.id, Issue.series_id)
            )
            wanted_issues = list(issue_result.all())
            issues_updated += len(wanted_issues)
            for issue_id, series_id in wanted_issues:
                await event_bus.emit(IssueWanted(issue_id=issue_id, series_id=series_id))
        else:
            issue_result = await session.execute(
                update(Issue)
                .where(
                    Issue.series_id.in_(chunk),
                    Issue.status.in_([IssueStatus.WANTED, IssueStatus.DOWNLOADING]),
                )
                .values(status=IssueStatus.SKIPPED)
            )
            issues_updated += int(issue_result.rowcount or 0)  # type: ignore[attr-defined]

    skipped = len(series_ids) - updated
    logger.info(
        "bulk_update_series",
        updated=updated,
        skipped=skipped,
        issues_updated=issues_updated,
        monitored=body.monitored,
    )
    return {"updated": updated, "skipped": skipped}


@router.delete("/bulk", status_code=200)
async def bulk_delete_series(
    body: SeriesBulkDelete,
    _user: AuthenticatedUser,
    session: DbSession,
) -> dict[str, int]:
    """Bulk-delete multiple series."""
    deleted = 0
    skipped = 0
    for sid in body.series_ids:
        try:
            await SeriesService.delete(
                session,
                sid,
                delete_files=body.delete_files,
                delete_folder=body.delete_folder,
            )
            deleted += 1
        except NotFoundError:
            skipped += 1
    logger.info("bulk_delete_series", deleted=deleted, skipped=skipped)
    return {"deleted": deleted, "skipped": skipped}


@router.post("/delete-context", response_model=SeriesDeleteContextResponse, status_code=200)
async def build_series_delete_context(
    body: SeriesDeleteContextRequest,
    _user: AuthenticatedUser,
    session: DbSession,
) -> SeriesDeleteContextResponse:
    """Build truthful delete-modal context from the real filesystem state."""
    context = await SeriesService.build_delete_context(session, body.series_ids)
    return SeriesDeleteContextResponse(
        series_count=context.series_count,
        linked_file_count=context.linked_file_count,
        managed_file_count=context.managed_file_count,
        referenced_file_count=context.referenced_file_count,
    )


# ── Folder operations (before /{series_id} to avoid path param conflict) ──


@router.post("/folders/rename-all", status_code=200)
async def rename_all_folders(
    _user: AuthenticatedUser,
    session: DbSession,
) -> dict[str, int]:
    """Rename all series folders to match the current naming template."""
    series_svc = await _build_series_service(session)
    return await series_svc.rename_all_series_folders(session)


# ── Detail ────────────────────────────────────────────────────────────


@router.get("/{series_id}", response_model=SeriesResponse)
async def get_series(
    series_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> SeriesResponse:
    """Get full series detail by ID."""
    return await _load_series_response(session, series_id)


# ── Create ────────────────────────────────────────────────────────────


@router.post("", response_model=SeriesResponse, status_code=201)
async def add_series(
    body: SeriesCreate,
    _user: AuthenticatedUser,
    session: DbSession,
) -> SeriesResponse:
    """Add a series to the library from ComicVine."""
    search_on_add = await load_search_on_add_default(session)
    if body.search_on_add is not None and body.search_on_add != search_on_add:
        raise ValidationError("Search on add is now controlled by the global import policy.")

    series_svc = await _build_series_service(session)

    series = await series_svc.add_from_comicvine(
        session,
        body.comicvine_id,
        body.library_root_id,
        search_on_add=search_on_add,
    )
    await session.flush()
    return await _load_series_response(session, series.id)


# ── Update ────────────────────────────────────────────────────────────


@router.put("/{series_id}", response_model=SeriesResponse)
async def update_series(
    series_id: int,
    body: SeriesUpdate,
    _user: AuthenticatedUser,
    session: DbSession,
) -> SeriesResponse:
    """Update series monitoring and lifecycle configuration."""
    series_svc = await _build_series_service(session)

    if body.monitored is not None:
        await series_svc.toggle_monitoring(session, series_id, body.monitored)
    if "status_override" in body.model_fields_set:
        await series_svc.set_status_override(session, series_id, body.status_override)

    return await _load_series_response(session, series_id)


# ── Delete ────────────────────────────────────────────────────────────


@router.delete("/{series_id}", status_code=204)
async def delete_series(
    series_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> None:
    """Remove a series from the library."""
    await SeriesService.delete(session, series_id)


# ── Issues for Series ─────────────────────────────────────────────────


@router.get(
    "/{series_id}/issues",
    response_model=PaginatedResponse[IssueListResponse],
)
async def list_series_issues(
    series_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> PaginatedResponse[IssueListResponse]:
    """List all issues for a series with pagination."""
    from pullbox.services.issue_service import IssueService

    # Verify series exists
    series = await session.get(Series, series_id)
    if not series:
        raise NotFoundError("Series", series_id)

    issues, total = await IssueService.get_for_series(session, series_id, limit, offset)
    issue_mapper = inspect(Issue)
    items = [
        IssueListResponse.model_validate(
            {
                **{c.key: getattr(i, c.key) for c in issue_mapper.columns},
                "issue_number_text": i.effective_issue_number_text,
                "has_file": i.library_file is not None,
            }
        )
        for i in issues
    ]
    return PaginatedResponse[IssueListResponse](
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + limit) < total,
    )


# ── Actions ───────────────────────────────────────────────────────────


@router.post("/{series_id}/search", status_code=200)
async def search_series(
    series_id: int,
    request: Request,
    response: Response,
    _user: AuthenticatedUser,
    session: DbSession,
) -> dict[str, object]:
    """Trigger an async background search for all wanted issues in a series."""
    # Verify series exists
    series = await session.get(Series, series_id)
    if not series:
        raise NotFoundError("Series", series_id)

    targets = await load_series_wanted_search_targets(session, series_id)
    if not targets:
        message = "No wanted issues to search"
        if request.headers.get("HX-Request"):
            response.headers["HX-Trigger"] = json.dumps(
                {"toast": {"message": message, "level": "info"}}
            )
        return {
            "series_id": series_id,
            "issues_to_search": 0,
            "status": "no_wanted",
            "message": message,
        }

    # Launch background task
    task_id = f"search_series_{series_id}_{int(time.time())}"
    pending_logs: list[SearchLog] = []
    for target in targets:
        pending_logs.append(
            SearchLog(
                issue_id=target.issue_id,
                series_title=target.series_title,
                issue_number=target.issue_number,
                search_type=SearchType.BULK,
                results_found=0,
                results_grabbed=0,
                results_queued=0,
                results_rejected=0,
                details={
                    "run_state": "running",
                    "search_scope": "series",
                    "task_id": task_id,
                    "series_id": series_id,
                    "results_count": 0,
                },
                best_confidence=None,
            )
        )
    session.add_all(pending_logs)
    await session.flush()
    pending_log_ids_by_issue = {log.issue_id: log.id for log in pending_logs}
    await session.commit()

    task = asyncio.create_task(
        search_series_issues(
            series_id,
            pending_log_ids_by_issue=pending_log_ids_by_issue,
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    message = "Search started for " + str(len(targets)) + " issues"
    if request.headers.get("HX-Request"):
        response.headers["HX-Trigger"] = json.dumps(
            {"toast": {"message": message, "level": "success"}}
        )

    logger.info(
        "series_search_launched",
        series_id=series_id,
        task_id=task_id,
        issues_to_search=len(targets),
    )
    return {
        "task_id": task_id,
        "series_id": series_id,
        "issues_to_search": len(targets),
        "status": "started",
        "message": message,
    }


@router.post("/{series_id}/refresh", response_model=SeriesResponse)
async def refresh_series(
    series_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> SeriesResponse:
    """Refresh series metadata from ComicVine."""
    series = await session.get(Series, series_id)
    if series is None:
        raise NotFoundError("Series", series_id)
    if series.issue_catalog_state == IssueCatalogState.HYDRATING:
        raise HTTPException(
            status_code=409,
            detail="Initial metadata sync is already in progress.",
        )
    metadata_svc = await _build_metadata_service(session)
    await metadata_svc.refresh_series(session, series_id, force=True)
    return await _load_series_response(session, series_id)


@router.post("/{series_id}/rename-folder", status_code=200)
async def rename_series_folder(
    series_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> dict[str, str | None]:
    """Rename a single series folder to match the current naming template."""
    series_svc = await _build_series_service(session)
    new_path = await series_svc.rename_series_folder(session, series_id)
    return {"new_path": new_path}
