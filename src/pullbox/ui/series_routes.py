"""Series list and add-series UI routes."""

import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import ColumnElement, Float, String, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import contains_eager
from starlette.responses import Response

from pullbox.api.deps import AuthenticatedUser, DbSession, get_request_session_factory
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import LibraryFile, LibraryRoot
from pullbox.models.publisher import Publisher
from pullbox.models.series import IssueCatalogState, Series, SeriesStatus
from pullbox.services.comicvine_persistent_cache import PersistentComicVineCacheProvider
from pullbox.services.cover_url_service import build_series_cover_url
from pullbox.services.reading_query_service import load_series_reading_aggregates
from pullbox.ui.comicvine_series_search import (
    ADD_SERIES_PER_PAGE,
    COMICVINE_SERIES_SEARCH_LIMIT,
    COMICVINE_SERIES_SORT_OPTIONS,
    format_comicvine_series_results,
    load_existing_series_by_cv_id,
    normalize_comicvine_series_sort,
    parse_comicvine_series_query,
    sort_comicvine_series_results,
)

logger = structlog.get_logger(__name__)

router = APIRouter()
htmx_router = APIRouter()

_SERIES_VIEW_MODES = {"list", "grid"}
_SERIES_DEFAULT_PER_PAGE = 25
_SERIES_MAX_PER_PAGE = 500
_GRID_EAGER_COVER_COUNT = 18
_GRID_HIGH_PRIORITY_COVER_COUNT = 12
_GRID_SYNC_DECODE_COVER_COUNT = 18
_ADD_SERIES_PREVIEW_MODE = "preview"
_ADD_SERIES_BROAD_QUERY_TOKENS = {"a", "an", "the"}
_GetTemplates = Callable[[], Jinja2Templates]
_BuildContext = Callable[..., dict[str, object]]
_LoadSystemConfigValues = Callable[[AsyncSession, Sequence[str]], Awaitable[dict[str, str]]]

_get_templates: _GetTemplates | None = None
_build_context: _BuildContext | None = None
_load_system_config_values: _LoadSystemConfigValues | None = None


def configure_series_routes(
    *,
    get_templates: _GetTemplates,
    build_context: _BuildContext,
    load_system_config_values: _LoadSystemConfigValues,
) -> None:
    """Provide shared UI runtime dependencies from the facade module."""
    global _get_templates, _build_context, _load_system_config_values
    _get_templates = get_templates
    _build_context = build_context
    _load_system_config_values = load_system_config_values


def _templates() -> Jinja2Templates:
    if _get_templates is None:
        msg = "series routes have not been configured with templates"
        raise RuntimeError(msg)
    return _get_templates()


def _ctx(request: Request, user: object | None = None, **kwargs: object) -> dict[str, object]:
    if _build_context is None:
        msg = "series routes have not been configured with a context builder"
        raise RuntimeError(msg)
    context: Mapping[str, object] = _build_context(request, user, **kwargs)
    return dict(context)


async def _system_config_values(
    session: AsyncSession,
    keys: Sequence[str],
) -> dict[str, str]:
    if _load_system_config_values is None:
        msg = "series routes have not been configured with a system config loader"
        raise RuntimeError(msg)
    return await _load_system_config_values(session, keys)


def normalize_series_per_page(per_page: int) -> int:
    """Return a bounded series page size for registry queries."""
    if per_page <= 0:
        return _SERIES_DEFAULT_PER_PAGE
    return min(per_page, _SERIES_MAX_PER_PAGE)


def resolve_series_view(request: Request, view_mode: str | None) -> str:
    """Resolve the active /series view from query or cookie."""
    if view_mode in _SERIES_VIEW_MODES:
        return view_mode
    cookie_view = request.cookies.get("series_view")
    if cookie_view in _SERIES_VIEW_MODES:
        return cookie_view
    return "list"


def series_type_code(series_type_value: str) -> str:
    """Return the compact mission-control code for a series type."""
    type_codes = {
        "standard": "STD",
        "one_shot": "ONE",
        "limited": "LTD",
        "annual": "ANN",
        "tpb": "TPB",
        "hardcover": "HC",
        "omnibus": "OMNI",
        "graphic_novel": "GN",
        "special": "SPC",
    }
    return type_codes.get(series_type_value, series_type_value[:3].upper())


def _lower_enum_sort(column: Any) -> ColumnElement[str]:
    """Return a portable case-insensitive sort for native enum columns."""
    return func.lower(cast(column, String))


def build_series_filters(
    q: str | None,
    status: str | None,
    monitored: str | None,
) -> list[ColumnElement[bool]]:
    """Build the shared Series page filters."""
    filters: list[ColumnElement[bool]] = []
    if q:
        from pullbox.core.db_utils import escape_like

        filters.append(Series.title.ilike(f"%{escape_like(q)}%"))
    if status:
        filters.append(Series.status == status)
    if monitored is not None and monitored != "":
        filters.append(Series.monitored == (monitored == "true"))
    return filters


def series_cover_src(series: Series) -> str | None:
    """Prefer the local cover endpoint whenever a series has any cover source."""
    return build_series_cover_url(series)


@router.get("/series", response_class=HTMLResponse, include_in_schema=False)
async def series_list(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    q: str | None = Query(None),
    status: str | None = Query(None),
    monitored: str | None = Query(None),
    sort: str = Query("title"),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=0),
    partial: str | None = Query(None),
    view_mode: str | None = Query(None),
) -> Response:
    """Render the series listing page with search, filtering, and sorting."""
    del partial
    active_view = resolve_series_view(request, view_mode)

    if sort.startswith("-"):
        sort_field = sort[1:]
        sort_desc = True
    else:
        sort_field = sort
        sort_desc = False

    filters = build_series_filters(q, status, monitored)

    count_query = select(func.count(Series.id))
    if filters:
        count_query = count_query.where(*filters)
    total: int = (await session.execute(count_query)).scalar_one()

    filtered_series_ids = select(Series.id)
    if filters:
        filtered_series_ids = filtered_series_ids.where(*filters)
    filtered_series_ids_subquery = filtered_series_ids.subquery()

    per_page = normalize_series_per_page(per_page)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)

    status_sort = _lower_enum_sort(Series.status)
    series_type_sort = _lower_enum_sort(Series.series_type)
    requires_issue_aggregate_sort = sort_field in {"issues", "acquisition"}

    issue_counts = None
    if requires_issue_aggregate_sort:
        issue_counts = (
            select(
                Issue.series_id.label("series_id"),
                func.count(Issue.id).label("total_issues"),
                func.coalesce(
                    func.sum(case((Issue.status == IssueStatus.OWNED, 1), else_=0)),
                    0,
                ).label("owned_count"),
                func.coalesce(
                    func.sum(case((Issue.status == IssueStatus.WANTED, 1), else_=0)),
                    0,
                ).label("wanted_count"),
                func.max(Issue.release_date).label("latest_release_date"),
            )
            .where(Issue.series_id.in_(select(filtered_series_ids_subquery.c.id)))
            .group_by(Issue.series_id)
            .subquery()
        )

    zero_issue_count = cast(0, Float)
    aggregate_total = (
        func.coalesce(issue_counts.c.total_issues, 0)
        if issue_counts is not None
        else zero_issue_count
    )
    aggregate_owned = (
        func.coalesce(issue_counts.c.owned_count, 0)
        if issue_counts is not None
        else zero_issue_count
    )
    acquisition_sort = case(
        (
            aggregate_total > 0,
            cast(aggregate_owned, Float) / cast(aggregate_total, Float),
        ),
        else_=0.0,
    )
    sort_map = {
        "title": func.lower(Series.sort_title),
        "year": Series.year_start,
        "date_added": Series.created_at,
        "publisher": func.lower(Publisher.name),
        "status": status_sort,
        "issues": aggregate_owned,
        "acquisition": acquisition_sort,
        "series_type": series_type_sort,
    }
    sort_col = sort_map.get(sort_field, func.lower(Series.sort_title))
    order_clause = sort_col.desc() if sort_desc else sort_col.asc()
    if sort_field in {"year", "publisher"}:
        order_clause = order_clause.nullslast()

    order_clauses = [order_clause]
    if sort_field == "title":
        order_clauses.extend(
            [
                Series.year_start.desc().nullslast(),
                series_type_sort.asc(),
            ]
        )
    else:
        order_clauses.extend(
            [
                func.lower(Series.sort_title).asc(),
                Series.year_start.desc().nullslast(),
            ]
        )
    order_clauses.append(Series.id.asc())

    route_query_start = time.monotonic()
    query = select(Series).outerjoin(Series.publisher).options(contains_eager(Series.publisher))
    if issue_counts is not None:
        query = query.outerjoin(issue_counts, issue_counts.c.series_id == Series.id)
    query = query.order_by(*order_clauses)
    offset = (page - 1) * per_page
    query = query.limit(per_page).offset(offset)
    if filters:
        query = query.where(*filters)

    result = await session.execute(query)
    visible_series = list(result.unique().scalars().all())
    visible_series_ids = [series.id for series in visible_series]
    visible_reading_aggregates = await load_series_reading_aggregates(
        session,
        user_id=user.id,
        series_ids=tuple(visible_series_ids),
    )

    visible_issue_counts: dict[int, tuple[int, int, int, date | None]] = {}
    if visible_series_ids:
        visible_counts_result = await session.execute(
            select(
                Issue.series_id,
                func.count(Issue.id),
                func.coalesce(
                    func.sum(case((Issue.status == IssueStatus.OWNED, 1), else_=0)),
                    0,
                ),
                func.coalesce(
                    func.sum(case((Issue.status == IssueStatus.WANTED, 1), else_=0)),
                    0,
                ),
                func.max(Issue.release_date),
            )
            .where(Issue.series_id.in_(visible_series_ids))
            .group_by(Issue.series_id)
        )
        visible_issue_counts = {
            int(series_id): (int(total), int(owned), int(wanted), latest_release_date)
            for series_id, total, owned, wanted, latest_release_date in visible_counts_result.all()
        }

    series_rows = [
        (series, *visible_issue_counts.get(series.id, (0, 0, 0, None))) for series in visible_series
    ]

    request.state.series_list_query_ms = round((time.monotonic() - route_query_start) * 1000, 2)
    catalog_sync_refresh_active = any(
        s.issue_catalog_state == IssueCatalogState.HYDRATING
        for s, _total_issues, _owned, _wanted, _latest_release_date in series_rows
    )

    for s, total_issues, _owned, _wanted, latest_release_date in series_rows:
        if s.status == SeriesStatus.UNKNOWN and int(total_issues or 0) > 0:
            if latest_release_date:
                cutoff = date.today() - timedelta(days=180)
                s.status = (
                    SeriesStatus.CONTINUING if latest_release_date >= cutoff else SeriesStatus.ENDED
                )
            else:
                s.status = SeriesStatus.CONTINUING

    series_data = []
    for index, (s, total_issues, owned, wanted, _latest_release_date) in enumerate(series_rows):
        owned = int(owned or 0)
        wanted = int(wanted or 0)
        total_issues = int(total_issues or 0)
        completion_pct = round((owned / total_issues) * 100) if total_issues > 0 else 0
        if not s.monitored:
            system_tone = "off"
        elif completion_pct >= 80:
            system_tone = "green"
        elif completion_pct >= 35:
            system_tone = "amber"
        else:
            system_tone = "amber"

        if completion_pct >= 80:
            acquisition_tone = "green"
        elif completion_pct >= 35:
            acquisition_tone = "amber"
        else:
            acquisition_tone = "red"

        cover_src = series_cover_src(s)
        reading_aggregate = visible_reading_aggregates.get(s.id)
        series_data.append(
            {
                "series": s,
                "owned_count": owned,
                "wanted_count": wanted,
                "total_issues": total_issues,
                "completion_pct": completion_pct,
                "system_tone": system_tone,
                "acquisition_tone": acquisition_tone,
                "type_code": series_type_code(s.series_type.value),
                "cover_src": cover_src,
                "cover_loading": (
                    "eager" if active_view == "grid" and index < _GRID_EAGER_COVER_COUNT else "lazy"
                ),
                "cover_fetchpriority": (
                    "high"
                    if active_view == "grid" and index < _GRID_HIGH_PRIORITY_COVER_COUNT
                    else "auto"
                ),
                "cover_decoding": (
                    "sync"
                    if active_view == "grid" and index < _GRID_SYNC_DECODE_COVER_COUNT
                    else "async"
                ),
                "readable_count": (
                    reading_aggregate.readable_count if reading_aggregate is not None else 0
                ),
                "read_count": (
                    reading_aggregate.completed_count if reading_aggregate is not None else 0
                ),
                "read_percent": (
                    reading_aggregate.completion_percent if reading_aggregate is not None else 0
                ),
            }
        )

    registry_series_counts = await session.execute(
        select(
            func.coalesce(
                func.sum(case((Series.monitored.is_(True), 1), else_=0)),
                0,
            ),
            func.count(Series.id),
        ).where(Series.id.in_(select(filtered_series_ids_subquery.c.id)))
    )
    monitored_count, total_filtered_series = registry_series_counts.one()

    registry_issue_counts = await session.execute(
        select(
            func.count(Issue.id),
            func.coalesce(
                func.sum(case((Issue.status == IssueStatus.OWNED, 1), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(case((Issue.status == IssueStatus.WANTED, 1), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(case((Issue.status == IssueStatus.DOWNLOADING, 1), else_=0)),
                0,
            ),
        ).where(Issue.series_id.in_(select(filtered_series_ids_subquery.c.id)))
    )
    total_issue_count, owned_issue_count, wanted_issue_count, downloading_issue_count = (
        registry_issue_counts.one()
    )

    registry_library_size = await session.execute(
        select(func.coalesce(func.sum(LibraryFile.file_size), 0)).where(
            LibraryFile.issue_id.in_(
                select(Issue.id).where(
                    Issue.series_id.in_(select(filtered_series_ids_subquery.c.id))
                )
            )
        )
    )
    library_size_bytes = registry_library_size.scalar_one()

    registry_completion_pct = (
        round((owned_issue_count / total_issue_count) * 100) if total_issue_count > 0 else 0
    )
    paused_count = max(int(total_filtered_series) - int(monitored_count), 0)

    filter_params: dict[str, str] = {}
    if q:
        filter_params["q"] = q
    if status:
        filter_params["status"] = status
    if monitored is not None and monitored != "":
        filter_params["monitored"] = monitored
    if sort != "title":
        filter_params["sort"] = sort
    if per_page != 25:
        filter_params["per_page"] = str(per_page)
    filter_query = urlencode(filter_params)

    ctx = _ctx(
        request,
        user,
        series_data=series_data,
        active_view=active_view,
        total=total,
        page=page,
        total_pages=total_pages,
        per_page=per_page,
        q=q or "",
        status_filter=status or "",
        monitored_filter=monitored or "",
        sort=sort,
        filter_query=filter_query,
        catalog_sync_refresh_active=catalog_sync_refresh_active,
        registry_metrics={
            "monitored_count": int(monitored_count),
            "paused_count": paused_count,
            "total_series": int(total_filtered_series),
            "total_issue_count": int(total_issue_count),
            "owned_issue_count": int(owned_issue_count),
            "wanted_issue_count": int(wanted_issue_count),
            "downloading_issue_count": int(downloading_issue_count),
            "completion_pct": registry_completion_pct,
            "library_size_bytes": int(library_size_bytes),
        },
    )

    render_start = time.monotonic()
    if request.headers.get("HX-Request"):
        response = _templates().TemplateResponse(
            request,
            "partials/series_results_bundle.html",
            ctx,
        )
    else:
        response = _templates().TemplateResponse(request, "pages/series_list.html", ctx)
    request.state.series_list_render_ms = round((time.monotonic() - render_start) * 1000, 2)
    return response


@router.get("/series/selection-ids", include_in_schema=False)
async def series_selection_ids(
    _user: AuthenticatedUser,
    session: DbSession,
    q: str | None = Query(None),
    status: str | None = Query(None),
    monitored: str | None = Query(None),
) -> JSONResponse:
    """Return all series IDs matching the current Series page filters."""
    filters = build_series_filters(q, status, monitored)
    query = select(Series.id).order_by(Series.id)
    if filters:
        query = query.where(*filters)
    ids = list((await session.execute(query)).scalars().all())
    return JSONResponse({"ids": ids, "total": len(ids)})


@router.get("/series/add", response_class=HTMLResponse, include_in_schema=False)
async def add_series_page(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    q: str | None = Query(None),
    sort: str | None = Query("relevance"),
    page: int = Query(1, ge=1),
    search_mode: str | None = Query(None),
) -> Response:
    """Render the add series page with ComicVine search."""
    roots_result = await session.execute(
        select(LibraryRoot).where(LibraryRoot.enabled.is_(True)).order_by(LibraryRoot.name)
    )
    roots = list(roots_result.scalars().all())

    add_series_sort_options = COMICVINE_SERIES_SORT_OPTIONS
    add_series_search_ctx = await load_add_series_search_context(
        session,
        q,
        sort,
        page,
        search_mode=search_mode,
        session_factory=get_request_session_factory(request),
    )

    template_context = _ctx(
        request,
        user,
        roots=roots,
        add_series_sort_options=add_series_sort_options,
        **add_series_search_ctx,
    )

    if request.headers.get("HX-Request") == "true":
        return _templates().TemplateResponse(
            request,
            "partials/add_series_results_bundle.html",
            template_context,
        )

    return _templates().TemplateResponse(
        request,
        "pages/add_series.html",
        template_context,
    )


def normalize_add_series_sort(sort: str | None) -> str:
    return normalize_comicvine_series_sort(sort)


def sort_add_series_results(
    results: list[Any],
    sort: str,
    *,
    query: str | None = None,
    year_hint: int | None = None,
) -> list[Any]:
    return sort_comicvine_series_results(results, sort, query=query, year_hint=year_hint)


async def load_add_series_search_context(
    session: DbSession,
    query: str | None,
    sort: str | None,
    page: int = 1,
    *,
    search_mode: str | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict[str, object]:
    per_page = ADD_SERIES_PER_PAGE
    normalized_query = (query or "").strip()
    normalized_sort = normalize_add_series_sort(sort)
    requested_page = max(1, page)
    roots_count = (
        await session.scalar(
            select(func.count(LibraryRoot.id)).where(LibraryRoot.enabled.is_(True))
        )
    ) or 0

    base_context: dict[str, object] = {
        "search_query": normalized_query,
        "add_series_sort": normalized_sort,
        "is_preview_search": False,
        "add_series_full_search_url": "",
        "search_results": [],
        "search_error": None,
        "search_total_results": 0,
        "search_shown_count": 0,
        "search_in_library_count": 0,
        "search_page": 1,
        "search_total_pages": 1,
        "search_pagination_base_url": "",
        "library_roots_count": roots_count,
    }

    if len(normalized_query) < 2:
        return base_context

    parsed_query = parse_comicvine_series_query(normalized_query)
    preview_mode = search_mode == _ADD_SERIES_PREVIEW_MODE
    if preview_mode and not _is_add_series_preview_query_ready(parsed_query.title_query):
        return base_context

    full_search_url = "/series/add?" + urlencode(
        {
            "q": normalized_query,
            "sort": normalized_sort,
        }
    )
    base_context["add_series_full_search_url"] = full_search_url

    try:
        from pullbox.core.comicvine_key import get_comicvine_api_key
        from pullbox.providers.metadata.comicvine import ComicVineProvider

        api_key = await get_comicvine_api_key(session)
        provider: Any = ComicVineProvider(api_key=api_key)
        if session_factory is not None:
            provider = PersistentComicVineCacheProvider(provider, session_factory)
        naming_config = await _system_config_values(
            session,
            (
                "series_folder_template",
                "replace_illegal_characters",
                "colon_replacement",
            ),
        )
        folder_template = naming_config.get("series_folder_template", "{Series} ({Year})")
        replace_illegal = naming_config.get("replace_illegal_characters", "true") == "true"
        colon_replacement = naming_config.get("colon_replacement", "dash")
        if preview_mode:
            cv_results, _total_results = await provider.search_series_page(
                parsed_query.title_query,
                parsed_query.year_hint,
                limit=per_page,
            )
        else:
            cv_results, _total_results = await provider.search_series_globally(
                parsed_query.title_query,
                max_results=COMICVINE_SERIES_SEARCH_LIMIT,
            )
        searchable_total = len(cv_results)
        total_pages = max(1, (searchable_total + per_page - 1) // per_page)
        resolved_page = min(requested_page, total_pages)

        sorted_results = sort_add_series_results(
            list(cv_results),
            normalized_sort,
            query=parsed_query.title_query,
            year_hint=parsed_query.year_hint,
        )
        page_start = (resolved_page - 1) * per_page
        visible_results = sorted_results[page_start : page_start + per_page]
        existing_series_by_cv_id = await load_existing_series_by_cv_id(session, visible_results)
        search_results = format_comicvine_series_results(
            visible_results,
            existing_series_by_cv_id=existing_series_by_cv_id,
            folder_template=folder_template,
            replace_illegal=replace_illegal,
            colon_replacement=colon_replacement,
        )
    except Exception:
        logger.exception("comicvine_search_failed", query=normalized_query)
        base_context["search_error"] = "ComicVine search failed. Check your API key in settings."
        return base_context

    in_library_count = sum(1 for item in search_results if bool(item.get("already_added")))
    shown_count = len(search_results)

    base_context.update(
        {
            "is_preview_search": preview_mode,
            "search_results": search_results,
            "search_total_results": searchable_total,
            "search_shown_count": shown_count,
            "search_in_library_count": in_library_count,
            "search_page": resolved_page,
            "search_total_pages": total_pages,
            "search_pagination_base_url": full_search_url,
        }
    )
    return base_context


@htmx_router.get("/htmx/series/search", response_class=HTMLResponse, include_in_schema=False)
async def htmx_search_series(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    q: str | None = Query(None),
    sort: str | None = Query("relevance"),
    page: int = Query(1, ge=1),
    search_mode: str | None = Query(None),
) -> Response:
    """Search ComicVine and return results as an HTMX partial."""
    add_series_search_ctx = await load_add_series_search_context(
        session,
        q,
        sort,
        page,
        search_mode=search_mode,
        session_factory=get_request_session_factory(request),
    )
    return _templates().TemplateResponse(
        request,
        "partials/add_series_results_bundle.html",
        _ctx(request, user, **add_series_search_ctx),
    )


def _is_add_series_preview_query_ready(title_query: str) -> bool:
    """Avoid firing expensive remote work for partial article/very broad typing."""
    tokens = re.findall(r"[a-z0-9]+", title_query.casefold())
    meaningful_tokens = [token for token in tokens if token not in _ADD_SERIES_BROAD_QUERY_TOKENS]
    return any(len(token) >= 3 for token in meaningful_tokens)
