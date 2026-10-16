"""What's New API routes."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Annotated, Any, Never

from fastapi import APIRouter, BackgroundTasks, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict

from pullbox.api.deps import AuthenticatedUser, DbSession, InteractiveOperatorUser  # noqa: TC001
from pullbox.config import get_settings
from pullbox.core.exceptions import PullboxError
from pullbox.schemas.search import (
    DcGrabRequest,
    DcGrabResponse,
    DirectGrabRequest,
    DirectGrabResponse,
    GrabReleaseRequest,
    GrabReleaseResponse,
)
from pullbox.schemas.whats_new import (
    WhatsNewCacheMetadata,
    WhatsNewCurrentWeekResponse,
    WhatsNewIssueSelection,
    WhatsNewUpcomingResponse,
    WhatsNewWatchRequest,
)
from pullbox.services.library_root_management import available_add_series_roots
from pullbox.services.series_interest import (
    ACTIVE_STATES,
    cancel_watch,
    find_watch,
    save_watch,
    watch_response,
)
from pullbox.services.whats_new_actions import WhatsNewSelectionError, load_release_selection
from pullbox.services.whats_new_cache_service import WhatsNewCacheService
from pullbox.services.whats_new_grab import validate_issue_selection
from pullbox.services.whats_new_refresh_queue import (
    RefreshQueueStatus,
    WhatsNewRefreshCoordinator,
    run_whats_new_refresh,
)

if TYPE_CHECKING:
    from pullbox.models.whats_new import WhatsNewReleaseCache

router = APIRouter(prefix="/whats-new", tags=["whats-new"], include_in_schema=False)
refresh_coordinator = WhatsNewRefreshCoordinator(runner=run_whats_new_refresh)

StoreDateQuery = Annotated[date | None, Query(alias="date")]


class ReleaseGrabRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selection: WhatsNewIssueSelection
    result: GrabReleaseRequest | DirectGrabRequest | DcGrabRequest


_grabbing_issues: set[int] = set()


@router.post("/grab", status_code=201)
async def grab_selected_release(
    body: ReleaseGrabRequest, session: DbSession, user: InteractiveOperatorUser
) -> object:
    """Admit one confirmed missing issue, then use the current selected-grab handlers."""
    from pullbox.api.v1 import issues

    if not get_settings().metadata_whats_new_actions_enabled:
        raise HTTPException(404, "Release discovery actions are not enabled.")
    issue_id = body.selection.issue_id
    if issue_id in _grabbing_issues:
        raise HTTPException(409, "This issue already has a Grab request in progress.")
    _grabbing_issues.add(issue_id)
    try:
        try:
            await validate_issue_selection(session, body.selection)
        except WhatsNewSelectionError as exc:
            raise HTTPException(409, str(exc)) from exc
        response: DirectGrabResponse | DcGrabResponse | GrabReleaseResponse
        if isinstance(body.result, DirectGrabRequest):
            response = await issues.grab_direct_release(
                issue_id=issue_id, body=body.result, _user=user, session=session
            )
        elif isinstance(body.result, DcGrabRequest):
            response = await issues.grab_direct_connect_release(
                issue_id=issue_id, body=body.result, user=user, session=session
            )
        else:
            response = await issues.grab_release(
                issue_id=issue_id, body=body.result, _user=user, session=session
            )
        await session.commit()
        return response
    finally:
        _grabbing_issues.discard(issue_id)


@router.post("/issue-state")
async def release_issue_state(
    body: WhatsNewIssueSelection, session: DbSession, user: InteractiveOperatorUser
) -> dict[str, object]:
    del user
    try:
        state = await validate_issue_selection(session, body, require_missing=False)
    except WhatsNewSelectionError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"issue_id": state.issue_id, "state": state.state.value, "label": state.label}


@router.post("/watch")
async def create_release_watch(
    body: WhatsNewWatchRequest, session: DbSession, user: InteractiveOperatorUser
) -> dict[str, object]:
    if not get_settings().metadata_whats_new_actions_enabled:
        raise HTTPException(404, "Release discovery actions are not enabled.")
    try:
        return watch_response(await save_watch(session, body, user.id))
    except WhatsNewSelectionError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/watch/{interest_id}/cancel")
async def cancel_release_watch(
    interest_id: Annotated[int, Path(gt=0, le=2**31 - 1)],
    session: DbSession,
    user: InteractiveOperatorUser,
) -> dict[str, object]:
    if not get_settings().metadata_whats_new_actions_enabled:
        raise HTTPException(404, "Release discovery actions are not enabled.")
    try:
        return watch_response(await cancel_watch(session, interest_id, user.id))
    except WhatsNewSelectionError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/resolve/{cache_id}/{release_id}")
async def resolve_release_context(
    cache_id: Annotated[int, Path(gt=0, le=2**31 - 1)],
    release_id: Annotated[int, Path(gt=0, le=2**63 - 1)],
    session: DbSession,
    _user: InteractiveOperatorUser,
) -> dict[str, object]:
    """Read saved discovery context without fetching providers or matching by title."""
    if not get_settings().metadata_whats_new_actions_enabled:
        raise HTTPException(404, "Release discovery actions are not enabled.")
    try:
        context = await load_release_selection(session, cache_id, release_id)
    except WhatsNewSelectionError as exc:
        raise HTTPException(409, str(exc)) from exc
    watch_roots = await available_add_series_roots(session, require_default=False)
    default_id = (
        watch_roots[0]["id"]
        if watch_roots and watch_roots[0]["is_default_managed_destination"]
        else None
    )
    roots = watch_roots if default_id else []
    interest = await find_watch(session, context.locg_series_id) if context.locg_series_id else None
    if interest is not None and interest.state in ACTIVE_STATES:
        roots = [root for root in watch_roots if root["id"] == interest.target_library_root_id]
    return {
        "selection": context.selection.model_dump(),
        "title": context.title,
        "publisher": context.publisher,
        "year": context.year,
        "can_link": context.locg_series_id is not None,
        "locg_series_id": context.locg_series_id,
        "roots": roots,
        "watch_roots": watch_roots,
        "watch_default_id": default_id,
        "can_watch": bool(
            context.locg_series_id and context.store_date and context.store_date > date.today()
        ),
    }


@router.get("")
async def get_whats_new(
    session: DbSession,
    _user: AuthenticatedUser,
    store_date: StoreDateQuery = None,
    publisher: str | None = None,
    upcoming: bool = False,
) -> WhatsNewCurrentWeekResponse | WhatsNewUpcomingResponse:
    """Return cached release data for the What's New page."""
    service = WhatsNewCacheService()
    if upcoming:
        row = await service.get_upcoming(session, publisher=publisher)
        payload = row.payload if row is not None else None
        if row is None and publisher:
            row = await service.get_upcoming(session)
            payload = (
                _filter_upcoming_payload_by_publisher(row.payload, publisher)
                if row is not None
                else None
            )
        if row is None:
            _raise_empty_cache()
        assert row is not None
        assert payload is not None
        return WhatsNewUpcomingResponse(
            **payload,
            cache=_cache_metadata(service, row),
        )

    if store_date is None:
        row = await service.get_latest_current_week(session)
    else:
        row = await service.get_current_week(session, store_date)
    if row is None:
        _raise_empty_cache()
    assert row is not None
    return WhatsNewCurrentWeekResponse(
        **row.payload,
        cache=_cache_metadata(service, row),
    )


@router.post("/refresh", status_code=status.HTTP_202_ACCEPTED)
async def refresh_whats_new(
    background_tasks: BackgroundTasks,
    _user: AuthenticatedUser,
) -> dict[str, object]:
    """Queue a refresh of cached release data."""
    result = await refresh_coordinator.queue_refresh(background_tasks)
    if result.status == RefreshQueueStatus.ALREADY_RUNNING:
        raise PullboxError(
            result.message,
            code="WHATS_NEW_REFRESH_IN_PROGRESS",
            status_code=409,
        )
    return {"status": result.status.value, "message": result.message}


def _cache_metadata(
    service: WhatsNewCacheService,
    row: WhatsNewReleaseCache,
) -> WhatsNewCacheMetadata:
    stale = service.is_stale(row)
    return WhatsNewCacheMetadata(
        status=service.cache_status_label(row),
        fetched_at=row.fetched_at,
        last_successful_refresh_at=row.last_successful_refresh_at,
        stale=stale,
    )


def _filter_upcoming_payload_by_publisher(
    payload: dict[str, Any],
    publisher: str,
) -> dict[str, Any]:
    filtered = dict(payload)
    weeks = payload.get("weeks")
    if not isinstance(weeks, list):
        filtered["weeks"] = []
        return filtered

    filtered_weeks: list[dict[str, Any]] = []
    for value in weeks:
        if not isinstance(value, dict):
            continue
        issues = value.get("issues")
        if not isinstance(issues, list):
            continue
        filtered_issues = [
            issue
            for issue in issues
            if isinstance(issue, dict) and _issue_publisher_matches(issue, publisher)
        ]
        if not filtered_issues:
            continue
        week = dict(value)
        week["issues"] = filtered_issues
        week["count"] = len(filtered_issues)
        filtered_weeks.append(week)

    filtered["weeks"] = filtered_weeks
    return filtered


def _issue_publisher_matches(issue: dict[str, Any], publisher: str) -> bool:
    value = issue.get("publisher")
    if not isinstance(value, dict):
        return False
    return _normalize_publisher_name(value.get("name")) == _normalize_publisher_name(publisher)


def _normalize_publisher_name(value: object) -> str:
    return str(value or "").strip().casefold()


def _raise_empty_cache() -> Never:
    raise PullboxError(
        "No cached release data is available yet.",
        code="WHATS_NEW_CACHE_EMPTY",
        status_code=503,
    )
