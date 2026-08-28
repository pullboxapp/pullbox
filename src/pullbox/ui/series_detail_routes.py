"""Series and issue detail UI routes."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import suppress
from typing import Annotated
from urllib.parse import unquote, urlsplit

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload
from starlette.responses import Response

from pullbox.api.deps import AuthenticatedUser, DbSession
from pullbox.composition.airdcpp import (
    get_airdcpp_search_coordinator,
    get_airdcpp_supervisor_registry,
    load_airdcpp_search_clients,
)
from pullbox.config import get_settings
from pullbox.core.page_sources import SUPPORTED_READER_FORMATS
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import LibraryFile
from pullbox.models.series import Series
from pullbox.services.airdcpp_route_tokens import get_airdcpp_route_token_store
from pullbox.services.airdcpp_search_types import AirDcppSearchProgress
from pullbox.services.reader_state_service import load_reader_state
from pullbox.services.reading_query_service import (
    load_series_reading_aggregates,
    load_visible_issue_states,
)
from pullbox.services.search_service import load_issue_search_target
from pullbox.services.series_service import SeriesService
from pullbox.ui.comicvine_series_search import wrap_comicvine_provider_for_ui_cache
from pullbox.ui.reading_presenters import IssueReadingView, present_issue_reading

logger = structlog.get_logger(__name__)

router = APIRouter()
issue_router = APIRouter()

_GetTemplates = Callable[[], Jinja2Templates]
_BuildContext = Callable[..., dict[str, object]]

_get_templates: _GetTemplates | None = None
_build_context: _BuildContext | None = None


def configure_series_detail_routes(
    *,
    get_templates: _GetTemplates,
    build_context: _BuildContext,
) -> None:
    """Provide shared UI runtime dependencies from the facade module."""
    global _get_templates, _build_context
    _get_templates = get_templates
    _build_context = build_context


def _templates() -> Jinja2Templates:
    if _get_templates is None:
        msg = "series detail routes have not been configured with templates"
        raise RuntimeError(msg)
    return _get_templates()


def _ctx(request: Request, user: object | None = None, **kwargs: object) -> dict[str, object]:
    if _build_context is None:
        msg = "series detail routes have not been configured with a context builder"
        raise RuntimeError(msg)
    context: Mapping[str, object] = _build_context(request, user, **kwargs)
    return dict(context)


def _pull_list_return_url(return_to: str | None) -> str:
    """Accept only an application-local Pull List URL for detail navigation."""
    if not return_to:
        return "/pull-list"
    parsed = urlsplit(return_to)
    if parsed.scheme or parsed.netloc or parsed.path != "/pull-list":
        return "/pull-list"
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


async def load_series_issues_context(
    session: DbSession,
    series_id: int,
    issue_status: str | None,
    page: int,
    *,
    user_id: int,
    sort: str = "-issue_number",
) -> dict[str, object]:
    """Load issue stats and paginated issues for a series."""
    count_result = await session.execute(
        select(Issue.status, func.count(Issue.id))
        .where(Issue.series_id == series_id)
        .group_by(Issue.status)
    )
    status_counts: dict[str, int] = {str(row[0]): row[1] for row in count_result.all()}
    owned_count = status_counts.get(IssueStatus.OWNED, 0)
    wanted_count = status_counts.get(IssueStatus.WANTED, 0)
    downloading_count = status_counts.get(IssueStatus.DOWNLOADING, 0)
    total_issue_count = sum(status_counts.values())

    per_page = 50
    issue_filters = [Issue.series_id == series_id]
    if issue_status:
        issue_filters.append(Issue.status == issue_status)

    filtered_total: int = (
        await session.execute(select(func.count(Issue.id)).where(*issue_filters))
    ).scalar_one()
    total_pages = max(1, (filtered_total + per_page - 1) // per_page)
    page = min(page, total_pages)
    offset = (page - 1) * per_page

    if sort.startswith("-"):
        sort_field = sort[1:]
        sort_desc = True
    else:
        sort_field = sort
        sort_desc = False

    sort_column = {
        "issue_number": Issue.issue_number,
        "title": Issue.title,
        "release_date": Issue.release_date,
        "status": Issue.status,
    }.get(sort_field, Issue.issue_number)

    order_clause = sort_column.desc().nullslast() if sort_desc else sort_column.asc().nullslast()

    issues_result = await session.execute(
        select(Issue)
        .options(joinedload(Issue.library_file))
        .where(*issue_filters)
        .order_by(order_clause)
        .limit(per_page)
        .offset(offset)
    )
    issues = list(issues_result.scalars().all())
    issue_ids = tuple(issue.id for issue in issues)
    reading_states = await load_visible_issue_states(
        session,
        user_id=user_id,
        issue_ids=issue_ids,
    )
    reader_enabled = get_settings().reader_enabled
    issue_reading_views = {
        issue.id: present_issue_reading(
            reading_states.get(issue.id),
            readable=_issue_is_readable(issue, reader_enabled=reader_enabled),
        )
        for issue in issues
    }
    reading_aggregates = await load_series_reading_aggregates(
        session,
        user_id=user_id,
        series_ids=(series_id,),
    )

    return {
        "issues": issues,
        "owned_count": owned_count,
        "wanted_count": wanted_count,
        "downloading_count": downloading_count,
        "total_issue_count": total_issue_count,
        "filtered_total": filtered_total,
        "page": page,
        "total_pages": total_pages,
        "issue_status": issue_status or "",
        "issue_sort": sort,
        "issue_reading_views": issue_reading_views,
        "series_reading_aggregate": reading_aggregates.get(series_id),
    }


def _issue_is_readable(issue: Issue, *, reader_enabled: bool) -> bool:
    return (
        reader_enabled
        and issue.status == IssueStatus.OWNED
        and issue.library_file is not None
        and issue.library_file.file_format in SUPPORTED_READER_FORMATS
    )


async def _load_issue_detail_record(session: DbSession, issue_id: int) -> Issue | None:
    result = await session.execute(
        select(Issue)
        .options(
            joinedload(Issue.series).joinedload(Series.publisher),
            joinedload(Issue.library_file),
            joinedload(Issue.creators),
        )
        .where(Issue.id == issue_id)
    )
    return result.unique().scalar_one_or_none()


async def _issue_reading_view(
    session: DbSession,
    *,
    user_id: int,
    issue: Issue,
    reader_enabled: bool,
) -> IssueReadingView:
    state = await load_reader_state(session, user_id=user_id, issue_id=issue.id)
    return present_issue_reading(
        state,
        readable=_issue_is_readable(issue, reader_enabled=reader_enabled),
    )


@router.get("/series/{series_id}", response_class=HTMLResponse, include_in_schema=False)
async def series_detail(
    request: Request,
    series_id: int,
    user: AuthenticatedUser,
    session: DbSession,
    issue_status: str | None = Query(None),
    page: int = Query(1, ge=1),
    issue_sort: str = Query("-issue_number"),
    source: Annotated[str | None, Query(alias="from")] = None,
    return_to: str | None = Query(None),
) -> Response:
    """Render the series detail page with issues."""
    result = await session.execute(
        select(Series)
        .options(
            joinedload(Series.publisher),
            joinedload(Series.parent_series),
            joinedload(Series.child_series),
        )
        .where(Series.id == series_id)
    )
    series = result.unique().scalar_one_or_none()
    if series is None:
        return RedirectResponse(url="/series", status_code=302)

    issues_ctx = await load_series_issues_context(
        session,
        series_id,
        issue_status,
        page,
        user_id=user.id,
        sort=issue_sort,
    )

    file_count: int = (
        await session.execute(
            select(func.count(LibraryFile.id)).where(
                LibraryFile.issue_id.in_(select(Issue.id).where(Issue.series_id == series_id))
            )
        )
    ).scalar_one()
    delete_context = await SeriesService.build_delete_context(session, [series_id])

    return _templates().TemplateResponse(
        request,
        "pages/series_detail.html",
        _ctx(
            request,
            user,
            series=series,
            file_count=file_count,
            delete_file_count=delete_context.linked_file_count,
            detail_origin=source if source == "pull-list" else None,
            detail_back_url=(
                _pull_list_return_url(return_to) if source == "pull-list" else "/series"
            ),
            **issues_ctx,
        ),
    )


@issue_router.get("/issues/{issue_id}", response_class=HTMLResponse, include_in_schema=False)
async def issue_detail(
    request: Request,
    issue_id: int,
    user: AuthenticatedUser,
    session: DbSession,
    read: str | None = Query(None),
) -> Response:
    """Render the issue detail page, fetching metadata on-demand if missing."""
    issue = await _load_issue_detail_record(session, issue_id)
    if issue is None:
        return RedirectResponse(url="/series", status_code=302)

    # On-demand metadata enrichment: fetch description from ComicVine if missing.
    if issue.comicvine_id and not issue.description:
        try:
            from pullbox.core.comicvine_key import get_comicvine_api_key
            from pullbox.providers.metadata.comicvine import ComicVineError, ComicVineProvider

            api_key = await get_comicvine_api_key(session)
            provider = ComicVineProvider(api_key=api_key)
            provider = wrap_comicvine_provider_for_ui_cache(provider, request)
            meta = await provider.get_issue(str(issue.comicvine_id))

            if meta.description and not issue.description:
                issue.description = meta.description
            if meta.comicvine_url and not issue.comicvine_url:
                issue.comicvine_url = meta.comicvine_url
            if meta.store_date:
                from pullbox.services.metadata_service import _parse_date

                parsed = _parse_date(meta.store_date)
                if parsed and not issue.store_date:
                    issue.store_date = parsed
            if meta.cover_url and not issue.cover_url:
                issue.cover_url = meta.cover_url

            issue.metadata_source = "comicvine"
            await session.flush()
            logger.info(
                "issue_metadata_enriched",
                issue_id=issue.id,
                comicvine_id=issue.comicvine_id,
            )
        except (ComicVineError, Exception):
            logger.exception("issue_metadata_enrich_failed", issue_id=issue.id)

    reader_enabled = get_settings().reader_enabled
    issue_reading = await _issue_reading_view(
        session,
        user_id=user.id,
        issue=issue,
        reader_enabled=reader_enabled,
    )
    deep_open_requested = read == "1"
    open_reader_on_load = deep_open_requested and issue_reading.primary_label is not None

    return _templates().TemplateResponse(
        request,
        "pages/issue_detail.html",
        _ctx(
            request,
            user,
            issue=issue,
            reader_enabled=reader_enabled,
            issue_reading=issue_reading,
            open_reader_on_load=open_reader_on_load,
            reader_deep_open_unavailable=deep_open_requested and not open_reader_on_load,
        ),
    )


@issue_router.get(
    "/htmx/issues/{issue_id}/reading",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def htmx_issue_reading_hero(
    request: Request,
    issue_id: int,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Refresh only the issue hero after a private reading-state command."""
    issue = await _load_issue_detail_record(session, issue_id)
    if issue is None:
        return Response(status_code=404)
    reader_enabled = get_settings().reader_enabled
    issue_reading = await _issue_reading_view(
        session,
        user_id=user.id,
        issue=issue,
        reader_enabled=reader_enabled,
    )
    return _templates().TemplateResponse(
        request,
        "partials/issue_detail_hero.html",
        _ctx(
            request,
            user,
            issue=issue,
            reader_enabled=reader_enabled,
            issue_reading=issue_reading,
        ),
    )


@issue_router.get(
    "/htmx/issues/{issue_id}/reading-row",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def htmx_issue_reading_row(
    request: Request,
    issue_id: int,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Refresh one series issue row and its reading aggregate after mutation."""
    result = await session.execute(
        select(Issue)
        .options(joinedload(Issue.series), joinedload(Issue.library_file))
        .where(Issue.id == issue_id)
    )
    issue = result.unique().scalar_one_or_none()
    if issue is None:
        return Response(status_code=404)
    states = await load_visible_issue_states(
        session,
        user_id=user.id,
        issue_ids=(issue.id,),
    )
    aggregates = await load_series_reading_aggregates(
        session,
        user_id=user.id,
        series_ids=(issue.series_id,),
    )
    issue_reading_views = {
        issue.id: present_issue_reading(
            states.get(issue.id),
            readable=_issue_is_readable(issue, reader_enabled=get_settings().reader_enabled),
        )
    }
    return _templates().TemplateResponse(
        request,
        "partials/series_issue_reading_row_bundle.html",
        _ctx(
            request,
            user,
            issue=issue,
            series=issue.series,
            issue_reading_views=issue_reading_views,
            series_reading_aggregate=aggregates.get(issue.series_id),
        ),
    )


@issue_router.get(
    "/htmx/issues/{issue_id}/dc-search-status",
    response_class=JSONResponse,
    include_in_schema=False,
)
async def htmx_issue_dc_search_status(
    issue_id: int,
    user: AuthenticatedUser,
    session: DbSession,
) -> JSONResponse:
    """Return a secret-free cooldown projection without reserving a search."""
    del user
    if not get_settings().airdcpp_enabled or await session.get(Issue, issue_id) is None:
        return JSONResponse({"available": False, "client_count": 0, "remaining_seconds": 0})
    registry = get_airdcpp_supervisor_registry()
    coordinator = get_airdcpp_search_coordinator()
    if registry is None or coordinator is None:
        return JSONResponse({"available": False, "client_count": 0, "remaining_seconds": 0})
    clients = await load_airdcpp_search_clients(session, registry)
    await session.commit()
    waits = await coordinator.cooldown_status(tuple(client.config_id for client in clients))
    return JSONResponse(
        {
            "available": bool(clients),
            "client_count": len(clients),
            "remaining_seconds": max(waits.values(), default=0),
        }
    )


@issue_router.get(
    "/htmx/issues/{issue_id}/dc-search-results",
    response_class=StreamingResponse,
    include_in_schema=False,
)
async def htmx_issue_dc_search_results(
    request: Request,
    issue_id: int,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Stream DC-only progress after existing source results have rendered."""
    if not get_settings().airdcpp_enabled:
        return Response(status_code=404)
    target = await load_issue_search_target(session, issue_id)
    if target is None:
        return Response(status_code=404)
    registry = get_airdcpp_supervisor_registry()
    coordinator = get_airdcpp_search_coordinator()
    if registry is None or coordinator is None:
        return Response(status_code=404)
    clients = await load_airdcpp_search_clients(session, registry)
    await session.commit()
    if not clients:
        return Response(status_code=404)

    template = _templates().get_template("partials/issue_dc_search_results.html")

    async def stream() -> AsyncIterator[str]:
        progress_queue: asyncio.Queue[AirDcppSearchProgress] = asyncio.Queue(maxsize=64)

        async def on_progress(progress: AirDcppSearchProgress) -> None:
            await progress_queue.put(progress)

        search_task = asyncio.create_task(
            coordinator.search(
                clients,
                target,
                manual=True,
                on_progress=on_progress,
            )
        )
        try:
            while not search_task.done() or not progress_queue.empty():
                progress_task = asyncio.create_task(progress_queue.get())
                done, _pending = await asyncio.wait(
                    {progress_task, search_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if progress_task in done:
                    progress = progress_task.result()
                    yield _sse_frame(
                        {
                            "kind": "progress",
                            "progress": {
                                "config_id": progress.config_id,
                                "state": progress.state.value,
                                "remaining_seconds": progress.remaining_seconds,
                            },
                        }
                    )
                else:
                    progress_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await progress_task

            outcome = await search_task
            route_store = get_airdcpp_route_token_store()
            dc_rows = [
                {
                    "candidate": candidate,
                    "route_token": route_store.issue(
                        candidate,
                        issue_id=target.issue_id,
                        user_id=user.id,
                        search_log_id=None,
                    ),
                }
                for candidate in outcome.matched
            ]
            html = template.render(
                _ctx(
                    request,
                    user,
                    issue={
                        "id": target.issue_id,
                        "series_id": target.series_id,
                        "issue_number": target.issue_number,
                    },
                    outcome=outcome,
                    dc_rows=dc_rows,
                )
            )
            result_count = len(outcome.matched) + len(outcome.rejected)
            qualifier = " partial" if outcome.partial else ""
            yield _sse_frame(
                {
                    "kind": "results",
                    "html": html,
                    "summary": f"{result_count} Direct Connect results{qualifier}.",
                }
            )
        finally:
            if not search_task.done():
                search_task.cancel()
                with suppress(asyncio.CancelledError):
                    await search_task

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


def _sse_frame(payload: dict[str, object]) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


@issue_router.post(
    "/htmx/issues/{issue_id}/toggle",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def htmx_toggle_issue_status(
    request: Request,
    issue_id: int,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Toggle issue status between wanted and skipped (HTMX partial)."""
    result = await session.execute(
        select(Issue)
        .options(joinedload(Issue.series), joinedload(Issue.library_file))
        .where(Issue.id == issue_id)
    )
    issue = result.scalar_one_or_none()
    if issue is None:
        return Response(status_code=404)

    if issue.status == IssueStatus.WANTED:
        issue.status = IssueStatus.SKIPPED
        issue.manual_skip = True
    elif issue.status == IssueStatus.SKIPPED:
        issue.status = IssueStatus.WANTED
        issue.manual_skip = False

    await session.commit()

    states = await load_visible_issue_states(
        session,
        user_id=user.id,
        issue_ids=(issue.id,),
    )
    issue_reading_views = {
        issue.id: present_issue_reading(
            states.get(issue.id),
            readable=_issue_is_readable(issue, reader_enabled=get_settings().reader_enabled),
        )
    }

    return _templates().TemplateResponse(
        request,
        "partials/issue_row.html",
        _ctx(
            request,
            user,
            issue=issue,
            series=issue.series,
            issue_reading_views=issue_reading_views,
        ),
    )


@issue_router.get(
    "/htmx/issues/{issue_id}/search-results",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def htmx_issue_search_results(
    request: Request,
    issue_id: int,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Return interactive search results as an HTML partial (HTMX)."""
    from pullbox.api.v1.issues import (
        _build_issue_search_log,
        _persist_direct_bundle_results,
        _run_issue_search,
    )

    issue = await session.get(Issue, issue_id)
    if issue is None:
        return Response(status_code=404)

    bundle = await _run_issue_search(
        session,
        issue_id,
        include_download_clients=False,
    )
    issue_ctx = {
        "id": bundle.issue.id,
        "series_id": bundle.target.series_id,
        "issue_number": bundle.issue.issue_number,
    }

    if bundle.runtime is None:
        return _templates().TemplateResponse(
            request,
            "partials/issue_search_results.html",
            _ctx(
                request,
                user,
                issue=issue_ctx,
                matched=[],
                rejected=[],
                search_time_ms=bundle.search_time_ms,
            ),
        )

    search_log = _build_issue_search_log(bundle)
    session.add(search_log)
    await session.flush()
    await _persist_direct_bundle_results(session, bundle, search_log_id=search_log.id)
    await session.commit()

    logger.info(
        "htmx_issue_search_results",
        issue_id=issue_id,
        matched=len(bundle.matched_items),
        rejected=len(bundle.rejected_items),
        search_time_ms=bundle.search_time_ms,
    )

    return _templates().TemplateResponse(
        request,
        "partials/issue_search_results.html",
        _ctx(
            request,
            user,
            issue=issue_ctx,
            matched=[m.model_dump() for m in bundle.matched_items],
            rejected=[r.model_dump() for r in bundle.rejected_items],
            search_time_ms=bundle.search_time_ms,
            search_log_id=search_log.id,
        ),
    )


@issue_router.post("/htmx/series/{series_id}/delete", include_in_schema=False)
async def htmx_delete_series(
    request: Request,
    series_id: int,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Delete a series, optionally removing files and/or folder from disk."""
    del user
    body = await request.json()
    delete_files: bool = body.get("delete_files", False)
    delete_folder: bool = body.get("delete_folder", False)

    from pullbox.core.exceptions import NotFoundError
    from pullbox.services.series_service import SeriesService

    try:
        await SeriesService.delete(
            session,
            series_id,
            delete_files=delete_files,
            delete_folder=delete_folder,
        )
        await session.flush()
    except NotFoundError:
        pass

    return RedirectResponse(url="/series", status_code=302)


@issue_router.post("/htmx/series/{series_id}/alternate-names", include_in_schema=False)
async def htmx_add_alternate_name(
    request: Request,
    series_id: int,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Add an alternate name to a series."""
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if not name:
        return Response(status_code=400)

    series = await session.get(Series, series_id)
    if series is None:
        return Response(status_code=404)

    current = list(series.alternate_names) if series.alternate_names else []
    if name not in current:
        current.append(name)
        series.alternate_names = current

    return _templates().TemplateResponse(
        request,
        "partials/series_detail_alternate_names_list.html",
        _ctx(request, user, series=series),
    )


@issue_router.delete("/htmx/series/{series_id}/alternate-names/{name}", include_in_schema=False)
async def htmx_remove_alternate_name(
    request: Request,
    series_id: int,
    name: str,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Remove an alternate name from a series."""
    decoded_name = unquote(name)

    series = await session.get(Series, series_id)
    if series is None:
        return Response(status_code=404)

    current = list(series.alternate_names) if series.alternate_names else []
    if decoded_name in current:
        current.remove(decoded_name)
        series.alternate_names = current

    return _templates().TemplateResponse(
        request,
        "partials/series_detail_alternate_names_list.html",
        _ctx(request, user, series=series),
    )


@issue_router.get(
    "/htmx/series/{series_id}/issues",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def htmx_series_issues(
    request: Request,
    series_id: int,
    user: AuthenticatedUser,
    session: DbSession,
    issue_status: str | None = Query(None),
    page: int = Query(1, ge=1),
    issue_sort: str = Query("-issue_number"),
) -> Response:
    """Return the series issues panel partial for HTMX polling."""
    series = await session.get(Series, series_id)
    if series is None:
        return Response(status_code=404)

    issues_ctx = await load_series_issues_context(
        session,
        series_id,
        issue_status,
        page,
        user_id=user.id,
        sort=issue_sort,
    )

    return _templates().TemplateResponse(
        request,
        "partials/series_issues_bundle.html",
        _ctx(request, user, series=series, **issues_ctx),
    )
