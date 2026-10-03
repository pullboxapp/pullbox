"""What's New UI route."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import date
from functools import cmp_to_key
from math import ceil
from typing import TYPE_CHECKING, Annotated, Any, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response  # noqa: TC002

from pullbox.api.deps import AuthenticatedUser, DbSession, InteractiveOperatorUser  # noqa: TC001
from pullbox.config import get_settings
from pullbox.core.metadata_identity import MetadataSource
from pullbox.services.series_interest import active_watches
from pullbox.services.whats_new_actions import (
    WhatsNewSelectionError,
    load_release_selection,
    local_release_series,
    positive_id,
    release_series_id,
)
from pullbox.services.whats_new_cache_service import WhatsNewCacheService
from pullbox.ui.series_routes import _request_search_cache, load_add_series_search_context

if TYPE_CHECKING:
    from pullbox.models.whats_new import WhatsNewReleaseCache

router = APIRouter()

_GetTemplates = Callable[[], Jinja2Templates]
_BuildContext = Callable[..., dict[str, object]]

_get_templates: _GetTemplates | None = None
_build_context: _BuildContext | None = None

StoreDateQuery = Annotated[date | None, Query(alias="date")]
SearchQuery = Annotated[str, Query(alias="q")]
PublisherQuery = Annotated[str, Query(alias="publisher")]
SortQuery = Annotated[str, Query(alias="sort")]
WindowQuery = Annotated[str, Query(alias="window")]
ReleaseWeekQuery = Annotated[str, Query(alias="release_week")]
PageQuery = Annotated[int, Query(ge=1)]
PerPageQuery = Annotated[int, Query(ge=1, le=100)]


@router.get("/whats-new/find-series/{cache_id}/{release_id}", include_in_schema=False)
async def find_release_series(
    request: Request,
    cache_id: Annotated[int, Path(gt=0, le=2**31 - 1)],
    release_id: Annotated[int, Path(gt=0, le=2**63 - 1)],
    user: InteractiveOperatorUser,
    session: DbSession,
    q: Annotated[str, Query(max_length=512)] = "",
    source: Annotated[MetadataSource | Literal["all"], Query()] = "all",
    page: PageQuery = 1,
) -> dict[str, object]:
    """Explicitly search through Add Series' cache, priority, sorting and owner checks."""
    if not get_settings().metadata_whats_new_actions_enabled:
        raise HTTPException(404, "Release discovery actions are not enabled.")
    try:
        await load_release_selection(session, cache_id, release_id)
    except WhatsNewSelectionError as exc:
        raise HTTPException(409, str(exc)) from exc
    return await load_add_series_search_context(
        session,
        q,
        "relevance",
        page,
        source=source if isinstance(source, MetadataSource) else None,
        cache=_request_search_cache(request),
    )


_DEFAULT_SORT = "release"
_VALID_WINDOWS = {"current", "upcoming"}
_VALID_SORTS = {
    "release",
    "-release",
    "date",
    "-date",
    "issue",
    "-issue",
    "publisher",
    "-publisher",
    "pulls",
    "-pulls",
    "rating",
    "-rating",
    "price",
    "-price",
    "variants",
    "-variants",
}


def configure_whats_new_routes(
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
        msg = "What's New routes have not been configured with templates"
        raise RuntimeError(msg)
    return _get_templates()


def _ctx(request: Request, user: object | None = None, **kwargs: object) -> dict[str, object]:
    if _build_context is None:
        msg = "What's New routes have not been configured with a context builder"
        raise RuntimeError(msg)
    context: Mapping[str, object] = _build_context(request, user, **kwargs)
    return dict(context)


@router.get("/whats-new", response_class=HTMLResponse, include_in_schema=False)
async def whats_new_page(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    store_date: StoreDateQuery = None,
    q: SearchQuery = "",
    publisher: PublisherQuery = "",
    sort: SortQuery = _DEFAULT_SORT,
    window: WindowQuery = "current",
    release_week: ReleaseWeekQuery = "",
    page: PageQuery = 1,
    per_page: PerPageQuery = 25,
) -> Response:
    """Render the read-only What's New page with cached release context."""
    service = WhatsNewCacheService()
    current_week = (
        await service.get_current_week(session, store_date)
        if store_date is not None
        else await service.get_latest_current_week(session)
    )
    upcoming = await service.get_upcoming(session)
    current_week_model = _view_model(service, current_week)
    upcoming_model = _view_model(service, upcoming)
    active_window = _active_window(window, current_week_model, upcoming_model)
    safe_sort = sort if sort in _VALID_SORTS else _DEFAULT_SORT
    upcoming_week_options = _upcoming_week_options(request, upcoming_model)
    selected_upcoming_week = _selected_upcoming_week(release_week, upcoming_week_options)
    upcoming_week_nav = _upcoming_week_nav(upcoming_week_options, selected_upcoming_week)
    upcoming_model = _with_selected_upcoming_week(upcoming_model, selected_upcoming_week)
    scoped_publisher_model = current_week_model if active_window == "current" else upcoming_model
    publisher_options = _publisher_options(scoped_publisher_model, q=q)
    safe_publisher = (
        publisher if any(option_value == publisher for option_value, _ in publisher_options) else ""
    )
    current_week_model = _filtered_view_model(
        current_week_model,
        q=q,
        publisher=safe_publisher,
        sort=safe_sort,
        page=page if active_window == "current" else 1,
        per_page=per_page,
    )
    upcoming_model = _filtered_view_model(
        upcoming_model,
        q=q,
        publisher=safe_publisher,
        sort=safe_sort,
        page=page if active_window == "upcoming" else 1,
        per_page=per_page,
    )
    active_model = current_week_model if active_window == "current" else upcoming_model
    actions_enabled = get_settings().metadata_whats_new_actions_enabled
    if actions_enabled:
        await _decorate_release_actions(session, [current_week_model, upcoming_model])
    ctx = _ctx(
        request,
        user,
        active_window=active_window,
        current_week=current_week_model,
        upcoming=upcoming_model,
        publisher_filter=safe_publisher,
        publisher_options=publisher_options,
        q=q,
        sort=safe_sort,
        page=_payload_int(active_model, "page", page),
        per_page=per_page,
        total=_payload_int(active_model, "total", 0),
        total_pages=_payload_int(active_model, "total_pages", 1),
        pagination_base_url=_pagination_base_url(request, publisher=safe_publisher),
        sort_base_url=_sort_base_url(request, publisher=safe_publisher),
        upcoming_week_options=upcoming_week_options,
        selected_upcoming_week=selected_upcoming_week,
        upcoming_week_nav=upcoming_week_nav,
        whats_new_actions_enabled=actions_enabled,
    )
    if request.headers.get("HX-Request") == "true":
        return _templates().TemplateResponse(
            request,
            "partials/whats_new_results_bundle.html",
            ctx,
        )
    return _templates().TemplateResponse(request, "pages/whats_new.html", ctx)


def _view_model(
    service: WhatsNewCacheService,
    row: WhatsNewReleaseCache | None,
) -> dict[str, object] | None:
    if row is None:
        return None
    return {
        "cache_id": row.id,
        "payload": _payload_view(row.payload),
        "store_date": row.store_date,
        "publisher": row.publisher,
        "cache": {
            "status": service.cache_status_label(row),
            "stale": service.is_stale(row),
            "fetched_at": row.fetched_at,
            "last_successful_refresh_at": row.last_successful_refresh_at,
        },
    }


async def _decorate_release_actions(
    session: DbSession, models: list[dict[str, object] | None]
) -> None:
    """One bounded ownership lookup for the visible release rows; no provider I/O."""
    releases: list[dict[str, Any]] = []
    for model in models:
        if model is None or not isinstance(model.get("payload"), dict):
            continue
        payload = model["payload"]
        assert isinstance(payload, dict)
        for release in payload.get("issues", []):
            release["discovery_cache_id"] = model["cache_id"]
            release["discovery_release_id"] = positive_id(release.get("locg_issue_id"))
            try:
                release["discovery_series_id"] = release_series_id(release)
            except WhatsNewSelectionError:
                release["discovery_release_id"] = None
                release["discovery_series_id"] = None
            releases.append(release)
    owners = await local_release_series(
        session, (r["discovery_series_id"] for r in releases if r["discovery_series_id"])
    )
    watches = {
        watch.source_series_id: watch
        for watch in await active_watches(
            session, (r["discovery_series_id"] for r in releases if r["discovery_series_id"])
        )
    }
    for release in releases:
        series = owners.get(release["discovery_series_id"])
        if series is not None:
            release["local_series"] = {"id": series.id, "monitored": series.monitored}
        watch = watches.get(release["discovery_series_id"])
        if watch is not None:
            release["watch_id"] = watch.id
            release["watch_state"] = watch.state.value
        store_date = release.get("store_date")
        release["watchable"] = bool(
            release["discovery_series_id"]
            and isinstance(store_date, date)
            and store_date > date.today()
        )


def _payload_view(payload: dict[str, Any]) -> dict[str, Any]:
    view = dict(payload)
    store_date = None
    if "store_date" in view:
        view["store_date"] = _parse_date(view["store_date"])
        store_date = view["store_date"] if isinstance(view["store_date"], date) else None
    if "issues" in view and isinstance(view["issues"], list):
        releases = _current_week_release_values(view["issues"], store_date)
        raw_count = len(releases)
        view["issues"] = _release_list_view(releases)
        view["count"] = len(view["issues"])
        view["variant_rows_hidden"] = raw_count - len(view["issues"])
    if "weeks" in view and isinstance(view["weeks"], list):
        view["weeks"] = [_week_view(week) for week in view["weeks"]]
        week_values = [week for week in view["weeks"] if isinstance(week, dict)]
        view["issues"] = _upcoming_release_list_view(week_values)
        view["count"] = len(view["issues"])
        view["variant_rows_hidden"] = sum(
            _parse_int(week.get("variant_rows_hidden")) or 0 for week in week_values
        )
    return view


def _active_window(
    requested_window: str,
    current_week: dict[str, object] | None,
    upcoming: dict[str, object] | None,
) -> str:
    if requested_window == "upcoming" and upcoming is not None:
        return requested_window
    if requested_window == "current" and current_week is not None:
        return requested_window
    if current_week is None and upcoming is not None:
        return "upcoming"
    return "current"


def _filtered_view_model(
    model: dict[str, object] | None,
    *,
    q: str,
    publisher: str,
    sort: str,
    page: int,
    per_page: int,
) -> dict[str, object] | None:
    if model is None:
        return None
    raw_payload = model.get("payload")
    payload: dict[str, Any] = dict(raw_payload) if isinstance(raw_payload, dict) else {}
    issues = [issue for issue in payload.get("issues", []) if isinstance(issue, dict)]
    filtered = _filter_releases(issues, q=q, publisher=publisher)
    filtered = _sort_releases(filtered, sort)
    total = len(filtered)
    total_pages = max(1, ceil(total / per_page)) if per_page > 0 else 1
    safe_page = min(max(page, 1), total_pages)
    start = (safe_page - 1) * per_page
    payload["issues"] = filtered[start : start + per_page]
    payload["count"] = total
    payload["total"] = total
    payload["unfiltered_count"] = len(issues)
    payload["page"] = safe_page
    payload["per_page"] = per_page
    payload["total_pages"] = total_pages
    return {**model, "payload": payload}


def _filter_releases(
    releases: Iterable[dict[str, Any]],
    *,
    q: str,
    publisher: str,
) -> list[dict[str, Any]]:
    normalized_query = _normalize_text(q)
    normalized_publisher = _normalize_text(publisher)
    filtered: list[dict[str, Any]] = []
    for release in releases:
        if normalized_publisher and _release_publisher_name(release) != normalized_publisher:
            continue
        if normalized_query and normalized_query not in _release_search_blob(release):
            continue
        filtered.append(release)
    return filtered


def _sort_releases(releases: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    descending = sort.startswith("-")
    field = sort[1:] if descending else sort

    def comparator(left: dict[str, Any], right: dict[str, Any]) -> int:
        return _compare_releases(left, right, field, descending)

    return sorted(releases, key=cmp_to_key(comparator))


def _publisher_options(
    model: dict[str, object] | None,
    *,
    q: str,
) -> list[tuple[str, str]]:
    names: set[str] = set()
    payload = model.get("payload") if model else None
    if not isinstance(payload, dict):
        return [("", "All Publishers")]
    issues = [issue for issue in payload.get("issues", []) if isinstance(issue, dict)]
    for release in _filter_releases(issues, q=q, publisher=""):
        name = _release_publisher_label(release)
        if name:
            names.add(name)
    return [("", "All Publishers"), *[(name, name) for name in sorted(names, key=str.casefold)]]


def _upcoming_week_options(
    request: Request,
    upcoming: dict[str, object] | None,
) -> list[dict[str, object]]:
    if upcoming is None:
        return []
    payload = upcoming.get("payload")
    if not isinstance(payload, dict):
        return []
    weeks = payload.get("weeks")
    if not isinstance(weeks, list):
        return []
    options: list[dict[str, object]] = []
    seen: set[str] = set()
    for week in weeks:
        if not isinstance(week, dict):
            continue
        store_date = _parse_date(week.get("store_date"))
        if not isinstance(store_date, date):
            continue
        value = store_date.isoformat()
        if value in seen:
            continue
        seen.add(value)
        count = _parse_int(week.get("count")) or 0
        options.append(
            {
                "value": value,
                "label": f"{store_date.strftime('%b %d, %Y')} ({count})",
                "count": count,
                "url": _replace_query_url(
                    request,
                    {"window": "upcoming", "release_week": value},
                    drop={"page", "publisher"},
                ),
            }
        )
    return sorted(options, key=lambda option: str(option["value"]))


def _selected_upcoming_week(
    requested_week: str,
    options: list[dict[str, object]],
) -> str:
    values = [str(option["value"]) for option in options]
    requested = requested_week.strip()
    if requested in values:
        return requested
    return values[0] if values else ""


def _upcoming_week_nav(
    options: list[dict[str, object]],
    selected_week: str,
) -> dict[str, object]:
    values = [str(option["value"]) for option in options]
    selected_index = values.index(selected_week) if selected_week in values else -1
    selected = options[selected_index] if selected_index >= 0 else None
    previous_option = options[selected_index - 1] if selected_index > 0 else None
    next_option = options[selected_index + 1] if 0 <= selected_index < len(options) - 1 else None
    return {
        "selected": selected,
        "previous": previous_option,
        "next": next_option,
        "position": selected_index + 1 if selected_index >= 0 else 0,
        "total": len(options),
    }


def _with_selected_upcoming_week(
    upcoming: dict[str, object] | None,
    selected_week: str,
) -> dict[str, object] | None:
    if upcoming is None or not selected_week:
        return upcoming
    raw_payload = upcoming.get("payload")
    if not isinstance(raw_payload, dict):
        return upcoming
    payload = dict(raw_payload)
    weeks = payload.get("weeks")
    if not isinstance(weeks, list):
        return upcoming
    selected = next(
        (
            week
            for week in weeks
            if isinstance(week, dict) and _week_option_value(week) == selected_week
        ),
        None,
    )
    if selected is None:
        return upcoming
    issues = _upcoming_release_list_view([selected])
    payload["issues"] = issues
    payload["count"] = len(issues)
    payload["selected_week"] = selected_week
    payload["selected_week_date"] = _parse_date(selected.get("store_date"))
    payload["variant_rows_hidden"] = _parse_int(selected.get("variant_rows_hidden")) or 0
    return {**upcoming, "payload": payload}


def _week_option_value(week: dict[str, object]) -> str:
    store_date = _parse_date(week.get("store_date"))
    return store_date.isoformat() if isinstance(store_date, date) else ""


def _release_search_blob(release: dict[str, Any]) -> str:
    series = release.get("series")
    publisher = release.get("publisher")
    values = [
        release.get("display_title"),
        release.get("title"),
        release.get("issue_number"),
        series.get("title") if isinstance(series, dict) else None,
        publisher.get("name") if isinstance(publisher, dict) else None,
    ]
    return " ".join(_normalize_text(value) for value in values)


def _compare_releases(
    left: dict[str, Any],
    right: dict[str, Any],
    field: str,
    descending: bool,
) -> int:
    if field == "date":
        primary = _compare_optional_date(
            _coerced_store_date(left),
            _coerced_store_date(right),
            descending=descending,
        )
    elif field == "issue":
        primary = _compare_optional_number(
            _parse_float(left.get("issue_number")),
            _parse_float(right.get("issue_number")),
            descending=descending,
        )
    elif field == "publisher":
        primary = _compare_text(
            _release_publisher_name(left),
            _release_publisher_name(right),
            descending=descending,
        )
    elif field == "pulls":
        primary = _compare_optional_number(
            float(_release_community_count(left, "pull")),
            float(_release_community_count(right, "pull")),
            descending=descending,
        )
    elif field == "rating":
        primary = _compare_optional_number(
            _parse_float(left.get("community_rating")),
            _parse_float(right.get("community_rating")),
            descending=descending,
        )
    elif field == "price":
        primary = _compare_optional_number(
            _parse_float(left.get("price")),
            _parse_float(right.get("price")),
            descending=descending,
        )
    elif field == "variants":
        primary = _compare_optional_number(
            float(_parse_int(left.get("variant_count")) or 0),
            float(_parse_int(right.get("variant_count")) or 0),
            descending=descending,
        )
    else:
        primary = _compare_text(
            _release_title_sort_value(left),
            _release_title_sort_value(right),
            descending=descending,
        )

    if primary != 0:
        return primary

    return _compare_text(
        _release_title_sort_value(left),
        _release_title_sort_value(right),
        descending=False,
    )


def _coerced_store_date(release: dict[str, Any]) -> date | None:
    parsed_date = _parse_date(release.get("store_date"))
    return parsed_date if isinstance(parsed_date, date) else None


def _release_title_sort_value(release: dict[str, Any]) -> str:
    return _normalize_text(release.get("display_title") or release.get("title"))


def _compare_optional_date(
    left: date | None,
    right: date | None,
    *,
    descending: bool,
) -> int:
    if left is None and right is None:
        return 0
    if left is None:
        return 1
    if right is None:
        return -1
    if left < right:
        return 1 if descending else -1
    if left > right:
        return -1 if descending else 1
    return 0


def _compare_optional_number(
    left: float | None,
    right: float | None,
    *,
    descending: bool,
) -> int:
    if left is None and right is None:
        return 0
    if left is None:
        return 1
    if right is None:
        return -1
    if left < right:
        return 1 if descending else -1
    if left > right:
        return -1 if descending else 1
    return 0


def _compare_text(left: str, right: str, *, descending: bool) -> int:
    if left < right:
        return 1 if descending else -1
    if left > right:
        return -1 if descending else 1
    return 0


def _release_publisher_label(release: dict[str, Any]) -> str:
    publisher = release.get("publisher")
    name = publisher.get("name") if isinstance(publisher, dict) else ""
    return str(name).strip() if name else ""


def _release_publisher_name(release: dict[str, Any]) -> str:
    return _normalize_text(_release_publisher_label(release))


def _release_community_count(release: dict[str, Any], key: str) -> int:
    counts = release.get("community_counts")
    if not isinstance(counts, dict):
        return 0
    return _parse_int(counts.get(key)) or 0


def _payload_int(model: dict[str, object] | None, key: str, fallback: int) -> int:
    if model is None:
        return fallback
    payload = model.get("payload")
    if not isinstance(payload, dict):
        return fallback
    parsed = _parse_int(payload.get(key))
    return parsed if parsed is not None else fallback


def _pagination_base_url(request: Request, *, publisher: str) -> str:
    params = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key not in {"page", "publisher"} and value != ""
    ]
    if publisher:
        params.append(("publisher", publisher))
    query = urlencode(params)
    return f"{request.url.path}?{query}" if query else request.url.path


def _sort_base_url(request: Request, *, publisher: str) -> str:
    params = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key not in {"page", "publisher", "sort"} and value != ""
    ]
    if publisher:
        params.append(("publisher", publisher))
    query = urlencode(params)
    return f"{request.url.path}?{query}" if query else request.url.path


def _replace_query_url(
    request: Request,
    replacements: dict[str, str],
    *,
    drop: set[str],
) -> str:
    params = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key not in drop and key not in replacements and value != ""
    ]
    params.extend((key, value) for key, value in replacements.items() if value)
    query = urlencode(params)
    return f"{request.url.path}?{query}" if query else request.url.path


def _week_view(value: object) -> object:
    if not isinstance(value, dict):
        return value
    week = dict(value)
    if "store_date" in week:
        week["store_date"] = _parse_date(week["store_date"])
    if "issues" in week and isinstance(week["issues"], list):
        releases = _supported_release_values(week["issues"])
        raw_count = len(releases)
        week["issues"] = _release_list_view(releases)
        week["count"] = len(week["issues"])
        week["variant_rows_hidden"] = raw_count - len(week["issues"])
    return week


def _current_week_release_values(
    values: list[object],
    store_date: date | None,
) -> list[dict[str, Any]]:
    releases = _supported_release_values(values)
    if store_date is None:
        return releases
    return [release for release in releases if _parse_date(release.get("store_date")) == store_date]


def _supported_release_values(values: list[object]) -> list[dict[str, Any]]:
    releases: list[dict[str, Any]] = []
    for value in values:
        release = _release_view(value)
        if not isinstance(release, dict):
            continue
        releases.append(release)
    return releases


def _upcoming_release_list_view(weeks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    releases: list[dict[str, Any]] = []
    for week in weeks:
        week_store_date = week.get("store_date")
        issues = week.get("issues", [])
        if not isinstance(issues, list):
            continue
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            release = dict(issue)
            release["store_date"] = _parse_date(week_store_date or release.get("store_date"))
            releases.append(release)
    releases.sort(key=_release_store_date_sort_key)
    return releases


def _release_list_view(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[object, ...], list[dict[str, Any]]] = {}
    passthrough: list[dict[str, Any]] = []
    for release in values:
        key = _release_group_key(release)
        if key is None:
            passthrough.append(release)
            continue
        grouped.setdefault(key, []).append(release)

    releases = [_primary_release(group) for group in grouped.values()]
    releases.extend(passthrough)
    return releases


def _release_store_date_sort_key(release: dict[str, Any]) -> tuple[str, str]:
    store_date = _parse_date(release.get("store_date"))
    date_value = store_date.isoformat() if isinstance(store_date, date) else ""
    return (date_value, _normalize_text(release.get("display_title") or release.get("title")))


def _release_view(value: object) -> object:
    if not isinstance(value, dict):
        return value
    release = dict(value)
    release["price"] = _parse_float(release.get("price"))
    if "store_date" in release:
        release["store_date"] = _parse_date(release["store_date"])
    return release


def _release_group_key(release: dict[str, Any]) -> tuple[object, ...] | None:
    series = release.get("series")
    series_id = series.get("locg_series_id") if isinstance(series, dict) else None
    series_title = series.get("title") if isinstance(series, dict) else None
    issue_number = _normalize_text(release.get("issue_number"))
    store_date = _normalize_text(release.get("store_date"))
    if series_id is not None and issue_number:
        return ("series_id", series_id, issue_number, store_date)
    if series_title and issue_number:
        return ("series_title", _normalize_text(series_title), issue_number, store_date)
    title = _normalize_text(release.get("title"))
    if title:
        return ("title", title, store_date)
    return None


def _primary_release(group: list[dict[str, Any]]) -> dict[str, Any]:
    if len(group) == 1:
        release = dict(group[0])
        release["variant_count"] = _parse_int(release.get("variant_count")) or 0
        return release

    primary = next((release for release in group if _is_primary_cover(release)), group[0])
    release = dict(primary)
    existing_variant_count = _parse_int(release.get("variant_count")) or 0
    release["variant_count"] = max(existing_variant_count, len(group) - 1)
    release["variant_rows_hidden"] = len(group) - 1
    return release


def _is_primary_cover(release: dict[str, Any]) -> bool:
    title = _normalize_text(release.get("title"))
    display_title = _normalize_text(release.get("display_title"))
    return not (_looks_like_variant(title) or _looks_like_variant(display_title))


def _looks_like_variant(value: str) -> bool:
    variant_terms = (
        " variant",
        " virgin",
        " foil",
        " cover b",
        " cover c",
        " cover d",
        " cover e",
        " cover f",
        " cover g",
        " cover h",
        " exclusive",
        " incentive",
    )
    return any(term in f" {value}" for term in variant_terms)


def _parse_date(value: object) -> object:
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return value
    return value


def _parse_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


def _parse_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return int(stripped)
        except ValueError:
            return None
    return None


def _normalize_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()
