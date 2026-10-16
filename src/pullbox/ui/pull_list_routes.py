"""Pull list UI route and context loading."""

from collections.abc import Callable, Mapping
from typing import Annotated
from typing import cast as typing_cast
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import ColumnElement, String, and_, asc, case, cast, desc, func, or_, select
from sqlalchemy.orm import joinedload
from starlette.responses import Response

from pullbox.api.deps import AuthenticatedUser, DbSession
from pullbox.config import get_settings
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.series import Series
from pullbox.services.series_interest import active_watches

router = APIRouter()

_PULL_LIST_FILTERS = {"wanted", "complete", "paused"}
_PULL_LIST_SORTS = {
    "title",
    "-title",
    "owned",
    "-owned",
    "wanted",
    "-wanted",
    "downloading",
    "-downloading",
    "total",
    "-total",
    "progress",
    "-progress",
    "status",
    "-status",
}

_GetTemplates = Callable[[], Jinja2Templates]
_BuildContext = Callable[..., dict[str, object]]

_get_templates: _GetTemplates | None = None
_build_context: _BuildContext | None = None


def configure_pull_list_routes(
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
        msg = "pull list routes have not been configured with templates"
        raise RuntimeError(msg)
    return _get_templates()


def _ctx(request: Request, user: object | None = None, **kwargs: object) -> dict[str, object]:
    if _build_context is None:
        msg = "pull list routes have not been configured with a context builder"
        raise RuntimeError(msg)
    context: Mapping[str, object] = _build_context(request, user, **kwargs)
    return dict(context)


@router.get("/pull-list", response_class=HTMLResponse, include_in_schema=False)
async def pull_list(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    filter: str | None = Query(None),
    search: str | None = Query(None),
    sort: str | None = Query("title"),
    page: int = Query(1, ge=1),
    per_page: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Response:
    """Render the pull list page with monitored series and issue counts."""
    filter_value = filter if filter in _PULL_LIST_FILTERS else ""
    search_query = (search or "").strip()
    sort_value = sort if sort in _PULL_LIST_SORTS else "title"

    issue_counts_sq = (
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
            func.coalesce(
                func.sum(case((Issue.status == IssueStatus.DOWNLOADING, 1), else_=0)),
                0,
            ).label("downloading_count"),
        )
        .group_by(Issue.series_id)
        .subquery()
    )

    counts_join = Series.__table__.outerjoin(
        issue_counts_sq,
        issue_counts_sq.c.series_id == Series.id,
    )

    (
        monitored_total,
        paused_total,
        wanted_series_total,
        wanted_issue_total,
        downloading_series_total,
        owned_issue_total,
        monitored_issue_total,
    ) = (
        await session.execute(
            select(
                func.coalesce(
                    func.sum(case((Series.monitored.is_(True), 1), else_=0)),
                    0,
                ),
                func.coalesce(
                    func.sum(case((Series.monitored.is_(False), 1), else_=0)),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(
                                    Series.monitored.is_(True),
                                    func.coalesce(issue_counts_sq.c.wanted_count, 0) > 0,
                                ),
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
                                Series.monitored.is_(True),
                                func.coalesce(issue_counts_sq.c.wanted_count, 0),
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
                                and_(
                                    Series.monitored.is_(True),
                                    func.coalesce(issue_counts_sq.c.downloading_count, 0) > 0,
                                ),
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
                                Series.monitored.is_(True),
                                func.coalesce(issue_counts_sq.c.owned_count, 0),
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
                                Series.monitored.is_(True),
                                func.coalesce(issue_counts_sq.c.total_issues, 0),
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
            ).select_from(counts_join)
        )
    ).one()

    filters: list[ColumnElement[bool]] = [
        Series.monitored.is_(False) if filter_value == "paused" else Series.monitored.is_(True)
    ]
    if search_query:
        search_term = f"%{search_query}%"
        filters.append(
            or_(
                Series.title.ilike(search_term),
                Series.sort_title.ilike(search_term),
                cast(Series.year_start, String).ilike(search_term),
            )
        )
    if filter_value == "wanted":
        filters.append(func.coalesce(issue_counts_sq.c.wanted_count, 0) > 0)
    elif filter_value == "complete":
        filters.extend(
            [
                func.coalesce(issue_counts_sq.c.total_issues, 0) > 0,
                func.coalesce(issue_counts_sq.c.owned_count, 0)
                >= func.coalesce(issue_counts_sq.c.total_issues, 0),
            ]
        )

    total = (
        await session.execute(
            select(func.count(Series.id)).select_from(counts_join).where(*filters)
        )
    ).scalar_one()
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    offset = (page - 1) * per_page
    progress_sort = case(
        (
            func.coalesce(issue_counts_sq.c.total_issues, 0) > 0,
            (func.coalesce(issue_counts_sq.c.owned_count, 0) * 1.0)
            / func.coalesce(issue_counts_sq.c.total_issues, 1),
        ),
        else_=0.0,
    )
    status_sort = case((Series.monitored.is_(True), 0), else_=1)
    sort_map = {
        "title": typing_cast("ColumnElement[object]", Series.sort_title),
        "owned": typing_cast(
            "ColumnElement[object]",
            func.coalesce(issue_counts_sq.c.owned_count, 0),
        ),
        "wanted": typing_cast(
            "ColumnElement[object]",
            func.coalesce(issue_counts_sq.c.wanted_count, 0),
        ),
        "downloading": typing_cast(
            "ColumnElement[object]",
            func.coalesce(issue_counts_sq.c.downloading_count, 0),
        ),
        "total": typing_cast(
            "ColumnElement[object]",
            func.coalesce(issue_counts_sq.c.total_issues, 0),
        ),
        "progress": typing_cast("ColumnElement[object]", progress_sort),
        "status": typing_cast("ColumnElement[object]", status_sort),
    }
    sort_field = sort_value.lstrip("-")
    primary_sort = sort_map.get(sort_field, typing_cast("ColumnElement[object]", Series.sort_title))
    order_clause = desc(primary_sort) if sort_value.startswith("-") else asc(primary_sort)

    result = await session.execute(
        select(
            Series,
            func.coalesce(issue_counts_sq.c.owned_count, 0).label("owned_count"),
            func.coalesce(issue_counts_sq.c.wanted_count, 0).label("wanted_count"),
            func.coalesce(issue_counts_sq.c.downloading_count, 0).label("downloading_count"),
            func.coalesce(issue_counts_sq.c.total_issues, 0).label("total_issues"),
        )
        .options(joinedload(Series.publisher))
        .outerjoin(issue_counts_sq, issue_counts_sq.c.series_id == Series.id)
        .where(*filters)
        .order_by(order_clause, Series.sort_title.asc(), Series.id.asc())
        .limit(per_page)
        .offset(offset)
    )

    pull_data = []
    for row in result.all():
        series = row[0]
        owned = int(row.owned_count or 0)
        wanted = int(row.wanted_count or 0)
        downloading = int(row.downloading_count or 0)
        total_series_issues = int(row.total_issues or 0)
        completion_pct = (
            round((owned / total_series_issues) * 100) if total_series_issues > 0 else 0
        )
        pull_data.append(
            {
                "series": series,
                "owned_count": owned,
                "wanted_count": wanted,
                "downloading_count": downloading,
                "total_issues": total_series_issues,
                "completion_pct": completion_pct,
            }
        )

    tracked_series_total = int(monitored_total or 0) + int(paused_total or 0)
    completion_pct = (
        round((int(owned_issue_total or 0) / int(monitored_issue_total or 0)) * 100)
        if int(monitored_issue_total or 0) > 0
        else 0
    )
    max_series_metric = max(tracked_series_total, 1)
    max_monitored_issue_metric = max(int(monitored_issue_total or 0), 1)
    pull_metrics = {
        "monitored_series": int(monitored_total or 0),
        "paused_series": int(paused_total or 0),
        "wanted_series": int(wanted_series_total or 0),
        "total_wanted": int(wanted_issue_total or 0),
        "downloading_series": int(downloading_series_total or 0),
        "completion_pct": completion_pct,
        "wanted_ratio": min(int(wanted_issue_total or 0) / max_monitored_issue_metric, 1.0),
        "downloading_ratio": min(int(downloading_series_total or 0) / max_series_metric, 1.0),
        "paused_ratio": min(int(paused_total or 0) / max_series_metric, 1.0),
    }
    pull_list_return_url = "/pull-list?" + urlencode(
        {
            "filter": filter_value,
            "search": search_query,
            "sort": sort_value,
            "page": page,
            "per_page": per_page,
        }
    )

    ctx = _ctx(
        request,
        user,
        pull_data=pull_data,
        pull_metrics=pull_metrics,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        filter_value=filter_value,
        search_query=search_query,
        sort=sort_value,
        pull_list_return_url=pull_list_return_url,
        watching=await active_watches(session)
        if get_settings().metadata_whats_new_actions_enabled
        else [],
        whats_new_actions_enabled=get_settings().metadata_whats_new_actions_enabled,
    )

    if request.headers.get("HX-Request"):
        return _templates().TemplateResponse(
            request,
            "partials/pull_list_content_bundle.html",
            ctx,
        )

    return _templates().TemplateResponse(
        request,
        "pages/pull_list.html",
        ctx,
    )


@router.post(
    "/pull-list/{series_id}/monitoring",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def update_pull_list_monitoring(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    series_id: int,
    monitored: bool = Form(...),
    filter: str | None = Form(None),
    search: str | None = Form(None),
    sort: str | None = Form("title"),
    page: int = Form(1),
    per_page: Annotated[int, Form(ge=1, le=100)] = 25,
) -> Response:
    """Update monitoring from the pull list and return the refreshed list fragment."""
    from pullbox.composition.services import build_domain_series_service

    series_svc = await build_domain_series_service(session)
    await series_svc.toggle_monitoring(session, series_id, monitored)
    await session.commit()

    return await pull_list(
        request,
        user=user,
        session=session,
        filter=filter,
        search=search,
        sort=sort,
        page=max(page, 1),
        per_page=per_page,
    )
