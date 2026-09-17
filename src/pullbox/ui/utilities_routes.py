"""Utilities UI routes and tab partials."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, case, func, not_, select
from starlette.responses import Response

from pullbox.api.deps import AuthenticatedUser, DbSession
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile, LibraryRoot
from pullbox.models.series import Series
from pullbox.utilities.models import JobState, JobType, UtilityJob

page_router = APIRouter()
htmx_router = APIRouter()

_UTILITY_TOOL_COUNT = 7
_UTILITY_HISTORY_PER_PAGE = 25
_UTILITY_HISTORY_TERMINAL_STATES = (
    JobState.COMPLETED,
    JobState.FAILED,
    JobState.CANCELLED,
    JobState.ROLLED_BACK,
)
_UTILITY_HISTORY_SORT_OPTIONS = {"job", "status", "items", "completed_at"}
_UTILITY_HISTORY_STATUS_OPTIONS = {"completed", "partial", "failed", "cancelled", "rolled_back"}

_UTILITY_EXPORT_FIELD_GROUPS: list[dict[str, object]] = [
    {
        "key": "series",
        "label": "Series",
        "description": "Series-level metadata pulled from the library catalog.",
        "fields": [
            {"key": "series_title", "label": "Series Title", "source": "Series.title"},
            {"key": "series_sort_title", "label": "Sort Title", "source": "Series.sort_title"},
            {"key": "series_year_start", "label": "Start Year", "source": "Series.year_start"},
            {"key": "series_year_end", "label": "End Year", "source": "Series.year_end"},
            {"key": "series_status", "label": "Series Status", "source": "Series.status"},
            {"key": "series_type", "label": "Series Type", "source": "Series.series_type"},
            {"key": "series_description", "label": "Description", "source": "Series.description"},
            {
                "key": "series_comicvine_id",
                "label": "ComicVine ID",
                "source": "Series.comicvine_id",
            },
            {
                "key": "series_comicvine_url",
                "label": "ComicVine URL",
                "source": "Series.comicvine_url",
            },
            {"key": "series_monitored", "label": "Monitored", "source": "Series.monitored"},
            {"key": "series_path", "label": "Series Path", "source": "Series.path"},
        ],
    },
    {
        "key": "issue",
        "label": "Issue",
        "description": "Issue-level metadata and release data.",
        "fields": [
            {"key": "issue_number", "label": "Issue Number", "source": "Issue.issue_number"},
            {"key": "issue_title", "label": "Issue Title", "source": "Issue.title"},
            {"key": "issue_type", "label": "Issue Type", "source": "Issue.issue_type"},
            {"key": "issue_status", "label": "Issue Status", "source": "Issue.status"},
            {"key": "release_date", "label": "Release Date", "source": "Issue.release_date"},
            {"key": "store_date", "label": "Store Date", "source": "Issue.store_date"},
            {"key": "page_count", "label": "Page Count", "source": "Issue.page_count"},
            {"key": "issue_comicvine_id", "label": "ComicVine ID", "source": "Issue.comicvine_id"},
            {
                "key": "issue_comicvine_url",
                "label": "ComicVine URL",
                "source": "Issue.comicvine_url",
            },
        ],
    },
    {
        "key": "file",
        "label": "File",
        "description": "On-disk file information tracked by Pullbox.",
        "fields": [
            {"key": "file_path", "label": "File Path", "source": "LibraryFile.file_path"},
            {"key": "file_name", "label": "File Name", "source": "LibraryFile.file_name"},
            {"key": "file_size", "label": "File Size", "source": "LibraryFile.file_size"},
            {"key": "file_format", "label": "File Format", "source": "LibraryFile.file_format"},
            {"key": "file_hash", "label": "File Hash", "source": "LibraryFile.file_hash"},
        ],
    },
    {
        "key": "publisher",
        "label": "Publisher",
        "description": "Publisher metadata reachable from each series.",
        "fields": [
            {"key": "publisher", "label": "Publisher", "source": "Publisher.name"},
            {
                "key": "publisher_comicvine_id",
                "label": "Publisher CV ID",
                "source": "Publisher.comicvine_id",
            },
        ],
    },
]

_UTILITY_EXPORT_DEFAULT_FIELDS = [
    "series_title",
    "publisher",
    "series_year_start",
    "series_status",
    "series_comicvine_id",
    "issue_number",
    "issue_title",
    "issue_status",
    "release_date",
    "file_path",
    "file_size",
    "file_format",
]

_GetTemplates = Callable[[], Jinja2Templates]
_BuildContext = Callable[..., dict[str, object]]
_LoadSystemConfigValues = Callable[[DbSession, Sequence[str]], Awaitable[dict[str, str]]]
_ResolveUtilityBrowsePaths = Callable[[dict[str, str]], dict[str, str]]
_BuildRenameTemplates = Callable[[Mapping[str, str]], dict[str, str]]

_get_templates: _GetTemplates | None = None
_build_context: _BuildContext | None = None
_load_system_config_values: _LoadSystemConfigValues | None = None
_resolve_utility_browse_paths: _ResolveUtilityBrowsePaths | None = None
_build_rename_templates: _BuildRenameTemplates | None = None


def configure_utilities_routes(
    *,
    get_templates: _GetTemplates,
    build_context: _BuildContext,
    load_system_config_values: _LoadSystemConfigValues,
    resolve_utility_browse_paths: _ResolveUtilityBrowsePaths,
    build_rename_templates: _BuildRenameTemplates,
) -> None:
    """Provide shared UI runtime dependencies from the facade module."""
    global _get_templates
    global _build_context
    global _load_system_config_values
    global _resolve_utility_browse_paths
    global _build_rename_templates
    _get_templates = get_templates
    _build_context = build_context
    _load_system_config_values = load_system_config_values
    _resolve_utility_browse_paths = resolve_utility_browse_paths
    _build_rename_templates = build_rename_templates


def _templates() -> Jinja2Templates:
    if _get_templates is None:
        msg = "utilities routes have not been configured with templates"
        raise RuntimeError(msg)
    return _get_templates()


def _ctx(request: Request, user: object | None = None, **kwargs: object) -> dict[str, object]:
    if _build_context is None:
        msg = "utilities routes have not been configured with a context builder"
        raise RuntimeError(msg)
    context: Mapping[str, object] = _build_context(request, user, **kwargs)
    return dict(context)


async def _config_values(session: DbSession, keys: Sequence[str]) -> dict[str, str]:
    if _load_system_config_values is None:
        msg = "utilities routes have not been configured with config loading"
        raise RuntimeError(msg)
    return await _load_system_config_values(session, keys)


def _browse_paths(configs: dict[str, str]) -> dict[str, str]:
    if _resolve_utility_browse_paths is None:
        msg = "utilities routes have not been configured with browse path resolution"
        raise RuntimeError(msg)
    return _resolve_utility_browse_paths(configs)


def _rename_templates(configs: Mapping[str, str]) -> dict[str, str]:
    if _build_rename_templates is None:
        msg = "utilities routes have not been configured with rename template building"
        raise RuntimeError(msg)
    return _build_rename_templates(configs)


def _normalize_utility_history_sort(sort: str | None) -> str:
    requested = (sort or "-completed_at").strip()
    desc = requested.startswith("-")
    field = requested[1:] if desc else requested
    if field not in _UTILITY_HISTORY_SORT_OPTIONS:
        return "-completed_at"
    return f"-{field}" if desc else field


def _normalize_utility_history_status(status: str | None) -> str:
    requested = (status or "").strip().lower()
    return requested if requested in _UTILITY_HISTORY_STATUS_OPTIONS else ""


def _utility_history_partial_clause() -> Any:
    return and_(
        UtilityJob.state == JobState.COMPLETED,
        (
            (func.coalesce(UtilityJob.failed_items, 0) > 0)
            | (func.coalesce(UtilityJob.error_message, "").like("NEEDS_ATTENTION:%"))
        ),
    )


def _utility_history_filters(status: str) -> list[Any]:
    filters: list[Any] = [UtilityJob.state.in_(_UTILITY_HISTORY_TERMINAL_STATES)]
    partial_clause = _utility_history_partial_clause()
    if status == "partial":
        filters.append(partial_clause)
    elif status == "completed":
        filters.extend([UtilityJob.state == JobState.COMPLETED, not_(partial_clause)])
    elif status == "failed":
        filters.append(UtilityJob.state == JobState.FAILED)
    elif status == "cancelled":
        filters.append(UtilityJob.state == JobState.CANCELLED)
    elif status == "rolled_back":
        filters.append(UtilityJob.state == JobState.ROLLED_BACK)
    return filters


def _get_utility_history_order_by(sort: str) -> list[Any]:
    desc = sort.startswith("-")
    field = sort[1:] if desc else sort
    partial_clause = _utility_history_partial_clause()
    status_order = case(
        (partial_clause, 1),
        (UtilityJob.state == JobState.COMPLETED, 0),
        (UtilityJob.state == JobState.FAILED, 2),
        (UtilityJob.state == JobState.CANCELLED, 3),
        (UtilityJob.state == JobState.ROLLED_BACK, 4),
        else_=99,
    )
    completed_expr = func.coalesce(UtilityJob.completed_at, UtilityJob.created_at)

    primary: Any
    if field == "job":
        primary = func.lower(UtilityJob.display_name)
    elif field == "status":
        primary = status_order
    elif field == "items":
        primary = func.coalesce(UtilityJob.total_items, 0)
    else:
        primary = completed_expr

    ordered = primary.desc().nullslast() if desc else primary.asc().nullslast()
    return [ordered, completed_expr.desc().nullslast(), UtilityJob.id.desc()]


def _utility_history_status_key(job: UtilityJob) -> str:
    if job.state == JobState.COMPLETED and (
        (job.failed_items or 0) > 0
        or bool(job.error_message and job.error_message.startswith("NEEDS_ATTENTION:"))
    ):
        return "partial"
    if job.state == JobState.COMPLETED:
        return "completed"
    if job.state == JobState.FAILED:
        return "failed"
    if job.state == JobState.CANCELLED:
        return "cancelled"
    if job.state == JobState.ROLLED_BACK:
        return "rolled_back"
    return str(job.state or "").lower()


def _utility_history_status_label(status_key: str, state: str | None) -> str:
    labels = {
        "partial": "Partial",
        "completed": "Completed",
        "failed": "Failed",
        "cancelled": "Cancelled",
        "rolled_back": "Rolled Back",
    }
    return labels.get(status_key, str(state or "").replace("_", " ").title())


def _utility_history_status_pill_class(status_key: str) -> str:
    classes = {
        "partial": "pill-warning",
        "completed": "pill-success",
        "failed": "pill-error",
        "cancelled": "pill-neutral",
        "rolled_back": "pill-info",
    }
    return classes.get(status_key, "pill-neutral")


def _utility_history_timestamp_label(value: str | None) -> str:
    """Format utility timestamp strings using the shared display preferences."""
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)

    from pullbox.core.display_time import (
        format_datetime,
        get_cached_display_settings,
        get_display_timezone,
    )
    from pullbox.core.timezone import get_timezone

    settings = get_cached_display_settings()
    tz = get_display_timezone(db_value=str(settings.get("timezone", "browser"))) or get_timezone()
    return format_datetime(
        parsed,
        timezone=tz,
        date_format=str(settings.get("date_format", "MMM DD, YYYY")),
        time_format=str(settings.get("time_format", "24h")),
        show_seconds=bool(settings.get("show_seconds", False)),
        show_timezone=bool(settings.get("show_timezone", True)),
        show_ampm=bool(settings.get("show_ampm", True)),
    )


def _utility_job_payload(job: UtilityJob, *, can_rollback: bool = False) -> dict[str, object]:
    status_key = _utility_history_status_key(job)
    return {
        "id": job.id,
        "job_type": job.job_type,
        "display_name": job.display_name,
        "state": job.state,
        "total_items": job.total_items,
        "completed_items": job.completed_items,
        "failed_items": job.failed_items,
        "skipped_items": job.skipped_items,
        "warning_count": job.warning_count,
        "queue_position": job.queue_position,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "completed_at_label": _utility_history_timestamp_label(job.completed_at),
        "created_by": job.created_by,
        "error_message": job.error_message,
        "parent_job_id": job.parent_job_id,
        "progress_pct": job.progress_pct,
        "history_status_key": status_key,
        "history_status_label": _utility_history_status_label(status_key, job.state),
        "history_status_pill_class": _utility_history_status_pill_class(status_key),
        "can_rollback": can_rollback,
    }


async def load_utility_history_context(
    session: DbSession,
    *,
    status: str = "",
    sort: str = "-completed_at",
    requested_page: int = 1,
) -> dict[str, object]:
    """Return server-sorted, paginated utility history context."""
    normalized_status = _normalize_utility_history_status(status)
    normalized_sort = _normalize_utility_history_sort(sort)
    filters = _utility_history_filters(normalized_status)
    total_history_jobs = int(
        (await session.scalar(select(func.count()).select_from(UtilityJob).where(*filters))) or 0
    )
    total_pages = max(
        1,
        (total_history_jobs + _UTILITY_HISTORY_PER_PAGE - 1) // _UTILITY_HISTORY_PER_PAGE,
    )
    page = max(1, min(requested_page, total_pages))
    offset = (page - 1) * _UTILITY_HISTORY_PER_PAGE

    jobs_result = await session.execute(
        select(UtilityJob)
        .where(*filters)
        .order_by(*_get_utility_history_order_by(normalized_sort))
        .limit(_UTILITY_HISTORY_PER_PAGE)
        .offset(offset)
    )
    jobs = list(jobs_result.scalars().all())
    job_ids = [job.id for job in jobs]

    blocked_rollback_parent_ids: set[str] = set()
    if job_ids:
        rollback_result = await session.execute(
            select(UtilityJob.parent_job_id).where(
                UtilityJob.job_type == JobType.ROLLBACK,
                UtilityJob.parent_job_id.in_(job_ids),
                UtilityJob.state.notin_([JobState.FAILED, JobState.CANCELLED]),
            )
        )
        blocked_rollback_parent_ids = {
            parent_id for parent_id in rollback_result.scalars().all() if parent_id
        }

    clearable_jobs_total = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(UtilityJob)
                .where(UtilityJob.state.in_(_UTILITY_HISTORY_TERMINAL_STATES))
            )
        )
        or 0
    )

    history_jobs = [
        _utility_job_payload(
            job,
            can_rollback=(
                job.job_type not in {JobType.ROLLBACK, JobType.SERIES_RESCAN}
                and job.state in (JobState.COMPLETED, JobState.CANCELLED)
                and job.id not in blocked_rollback_parent_ids
            ),
        )
        for job in jobs
    ]
    return {
        "history_jobs": history_jobs,
        "utility_history_status": normalized_status,
        "history_sort": normalized_sort,
        "page": page,
        "total_pages": total_pages,
        "total_history_jobs": total_history_jobs,
        "clearable_jobs_total": clearable_jobs_total,
    }


async def load_utility_queue_snapshot(
    session: DbSession,
) -> tuple[list[dict[str, object]], dict[str, int]]:
    """Return recent utility jobs together with summary counts."""
    jobs_result = await session.execute(
        select(UtilityJob).order_by(UtilityJob.created_at.desc()).limit(50)
    )
    all_jobs = jobs_result.scalars().all()

    jobs: list[dict[str, object]] = [_utility_job_payload(job) for job in all_jobs]
    stats = await load_utility_queue_stats(session)
    return jobs, stats


async def load_utility_queue_stats(session: DbSession) -> dict[str, int]:
    """Return utility queue summary counts without hydrating job payloads."""
    stats_result = await session.execute(
        select(UtilityJob.state, func.count(UtilityJob.id)).group_by(UtilityJob.state)
    )
    counts_by_state = {state: int(count) for state, count in stats_result.all()}
    return {
        "running": counts_by_state.get(JobState.RUNNING.value, 0),
        "queued": counts_by_state.get(JobState.QUEUED.value, 0),
        "paused": counts_by_state.get(JobState.PAUSED.value, 0),
        "total_completed": sum(
            counts_by_state.get(state.value, 0) for state in _UTILITY_HISTORY_TERMINAL_STATES
        ),
    }


async def _load_utilities_context(
    request: Request,
    user: object,
    session: DbSession,
    *,
    tab: str,
    utility_history_status: str = "",
    history_sort: str = "-completed_at",
    history_page: int = 1,
) -> dict[str, object]:
    valid_tabs = ("utilities", "queue", "history")
    if tab not in valid_tabs:
        tab = "utilities"
    ctx = _ctx(request, user, tab=tab)
    initial_jobs: list[dict[str, object]] = []
    if tab == "queue":
        initial_jobs, initial_stats = await load_utility_queue_snapshot(session)
    else:
        initial_stats = await load_utility_queue_stats(session)
    ctx["initial_stats"] = initial_stats
    ctx["utilities_tool_count"] = _UTILITY_TOOL_COUNT
    if tab == "queue":
        ctx["initial_jobs"] = initial_jobs
    if tab == "history":
        ctx.update(
            await load_utility_history_context(
                session,
                status=utility_history_status,
                sort=history_sort,
                requested_page=history_page,
            )
        )
    return ctx


@page_router.get("/utilities", response_class=HTMLResponse, include_in_schema=False)
async def utilities_page(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    tab: str = Query("utilities"),
    utility_history_status: str = Query(""),
    sort: str = Query("-completed_at"),
    page: int = Query(1, ge=1),
) -> Response:
    """Render the utilities page with overview and job queue tabs."""
    ctx = await _load_utilities_context(
        request,
        user,
        session,
        tab=tab,
        utility_history_status=utility_history_status,
        history_sort=sort,
        history_page=page,
    )

    if request.headers.get("HX-Request"):
        if tab == "history" and request.headers.get("HX-Target") == "utilities-history-section":
            return _templates().TemplateResponse(
                request,
                "partials/utilities_history_section_bundle.html",
                ctx,
            )
        return _templates().TemplateResponse(
            request,
            "partials/utilities_content_bundle.html",
            ctx,
        )

    return _templates().TemplateResponse(request, "pages/utilities.html", ctx)


@page_router.get("/utilities/converter", response_class=HTMLResponse, include_in_schema=False)
async def utilities_converter(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Render the file converter form page."""
    configs = await _config_values(session, ["utility_trash_folder"])
    utility_browse_paths = _browse_paths(configs)
    return _templates().TemplateResponse(
        request,
        "pages/utilities_converter.html",
        _ctx(
            request,
            user,
            utility_trash_folder=utility_browse_paths["trash_folder"],
            utility_trash_folder_browse_path=utility_browse_paths["trash_folder"],
        ),
    )


@page_router.get("/utilities/mass-convert", response_class=HTMLResponse, include_in_schema=False)
async def utilities_mass_convert(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Render the mass convert workflow page."""
    configs = await _config_values(session, ["utility_trash_folder"])
    utility_browse_paths = _browse_paths(configs)
    return _templates().TemplateResponse(
        request,
        "pages/utilities_mass_convert.html",
        _ctx(
            request,
            user,
            utility_trash_folder=utility_browse_paths["trash_folder"],
            utility_trash_folder_browse_path=utility_browse_paths["trash_folder"],
        ),
    )


@page_router.get("/utilities/mass-rename", response_class=HTMLResponse, include_in_schema=False)
async def utilities_mass_rename(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Render the mass rename workflow page."""
    configs = await _config_values(
        session,
        [
            "series_folder_template",
            "comic_file_template",
            "annual_file_template",
            "non_standard_file_template",
            "single_non_standard_file_template",
            "replace_illegal_characters",
            "colon_replacement",
        ],
    )
    rename_templates = _rename_templates(configs)
    library_roots_result = await session.execute(
        select(LibraryRoot.path).where(LibraryRoot.enabled.is_(True)).order_by(LibraryRoot.id)
    )
    allowed_browse_roots = [row[0] for row in library_roots_result.all() if row[0]]
    return _templates().TemplateResponse(
        request,
        "pages/utilities_mass_rename.html",
        _ctx(
            request,
            user,
            rename_templates=rename_templates,
            allowed_browse_roots=allowed_browse_roots,
        ),
    )


@page_router.get("/utilities/integrity", response_class=HTMLResponse, include_in_schema=False)
async def utilities_integrity(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Render the integrity checker workflow page."""
    configs = await _config_values(session, ["utility_trash_folder"])
    utility_browse_paths = _browse_paths(configs)
    return _templates().TemplateResponse(
        request,
        "pages/utilities_integrity.html",
        _ctx(
            request,
            user,
            utility_trash_folder=utility_browse_paths["trash_folder"],
            utility_trash_folder_browse_path=utility_browse_paths["trash_folder"],
        ),
    )


@page_router.get("/utilities/db-check", response_class=HTMLResponse, include_in_schema=False)
async def utilities_db_check(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    check: str | None = Query(default=None),
) -> Response:
    """Render the DB check & cleanup workflow page."""
    default_root = await session.scalar(
        select(LibraryRoot.path).where(LibraryRoot.enabled.is_(True)).order_by(LibraryRoot.id)
    )
    configs = await _config_values(session, ["comics_directory"])
    return _templates().TemplateResponse(
        request,
        "pages/utilities_db_check.html",
        _ctx(
            request,
            user,
            db_check_default_root=default_root or configs.get("comics_directory", ""),
            db_check_initial_optimize=check == "optimize",
        ),
    )


@page_router.get("/utilities/permissions", response_class=HTMLResponse, include_in_schema=False)
async def utilities_permissions(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Render the recursive library permissions workflow page."""
    roots_result = await session.execute(
        select(LibraryRoot.id, LibraryRoot.name, LibraryRoot.path)
        .where(LibraryRoot.enabled.is_(True))
        .order_by(LibraryRoot.id)
    )
    library_roots = [
        {"id": row[0], "name": row[1] or f"Root {row[0]}", "path": row[2]}
        for row in roots_result.all()
        if row[2]
    ]
    return _templates().TemplateResponse(
        request,
        "pages/utilities_permissions.html",
        _ctx(request, user, library_roots=library_roots),
    )


@page_router.get("/utilities/export", response_class=HTMLResponse, include_in_schema=False)
async def utilities_export(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Render the export library workflow page."""
    configs = await _config_values(session, ["utility_export_folder"])
    utility_browse_paths = _browse_paths(configs)
    export_folder = (
        configs.get("utility_export_folder", "") or utility_browse_paths["export_folder"]
    )
    total_series = int((await session.scalar(select(func.count(Series.id)))) or 0)
    total_issues = int((await session.scalar(select(func.count(Issue.id)))) or 0)
    total_files = int((await session.scalar(select(func.count(LibraryFile.id)))) or 0)
    return _templates().TemplateResponse(
        request,
        "pages/utilities_export.html",
        _ctx(
            request,
            user,
            utility_export_folder=export_folder,
            utility_export_folder_browse_path=utility_browse_paths["export_folder"],
            export_field_groups=_UTILITY_EXPORT_FIELD_GROUPS,
            export_default_fields=_UTILITY_EXPORT_DEFAULT_FIELDS,
            export_record_counts={
                "series": {"all": total_series},
                "issue": {"all": total_issues},
                "file": {"all": total_files},
            },
        ),
    )


@htmx_router.get("/htmx/utilities/{tab}", response_class=HTMLResponse, include_in_schema=False)
async def htmx_utilities_tab(
    request: Request,
    tab: str,
    user: AuthenticatedUser,
    session: DbSession,
    utility_history_status: str = Query(""),
    sort: str = Query("-completed_at"),
    page: int = Query(1, ge=1),
) -> Response:
    """Load a utilities tab partial via HTMX."""
    return _templates().TemplateResponse(
        request,
        "partials/utilities_content_bundle.html",
        await _load_utilities_context(
            request,
            user,
            session,
            tab=tab,
            utility_history_status=utility_history_status,
            history_sort=sort,
            history_page=page,
        ),
    )


@htmx_router.get(
    "/htmx/utilities/jobs/{job_id}/detail",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def htmx_utility_history_job_detail(
    request: Request,
    job_id: str,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Render the heavy history job detail panel only when a row is expanded."""
    job = await session.get(UtilityJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Utility job not found")
    return _templates().TemplateResponse(
        request,
        "partials/utilities_history_job_detail.html",
        _ctx(request, user, detail_job=_utility_job_payload(job)),
    )
