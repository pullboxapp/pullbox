"""Dashboard page and HTMX UI routes."""

import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from starlette.responses import Response

from pullbox.api.deps import AuthenticatedUser, DbSession
from pullbox.config import get_settings
from pullbox.models.download import DownloadHistory, DownloadState
from pullbox.models.import_job import ImportJob, ImportJobStatus
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import LibraryFile
from pullbox.models.search_log import SearchLog
from pullbox.models.series import Series
from pullbox.services.dashboard_intelligence_service import DashboardIntelligence
from pullbox.services.dashboard_storage_path import resolve_dashboard_storage_path
from pullbox.services.reading_query_service import list_continue_reading
from pullbox.ui.dashboard_display import (
    dashboard_completion_tone as dashboard_completion_tone,
)
from pullbox.ui.dashboard_display import (
    dashboard_gauge_offset as dashboard_gauge_offset,
)
from pullbox.ui.dashboard_display import (
    dashboard_led_tone as dashboard_led_tone,
)
from pullbox.ui.dashboard_display import (
    dashboard_relative_time_label as dashboard_relative_time_label,
)
from pullbox.ui.dashboard_display import (
    dashboard_weekly_count_delta as dashboard_weekly_count_delta,
)
from pullbox.ui.dashboard_recent_activity import (
    DashboardRecentActivityItemView as DashboardRecentActivityItemView,
)
from pullbox.ui.dashboard_recent_activity import (
    build_download_recent_activity_item,
    build_import_recent_activity_item,
)
from pullbox.ui.reading_presenters import ReadingIssueCardView, present_reading_issues

logger = structlog.get_logger(__name__)

router = APIRouter()


@dataclass(frozen=True)
class DashboardGaugeView:
    """Mission-control gauge tile."""

    key: str
    label: str
    value_label: str
    tone: str
    stroke_offset: float


@dataclass(frozen=True)
class DashboardScoreboardItemView:
    """Horizontal scoreboard metric."""

    key: str
    label: str
    value_label: str
    delta_label: str


@dataclass(frozen=True)
class DashboardAlertRowView:
    """Alerts table row."""

    key: str
    led_tone: str
    title: str
    detail: str
    state: str
    state_label: str
    timing_label: str
    href: str


@dataclass(frozen=True)
class DashboardFooterStripView:
    """Footer strip metrics for the dashboard."""

    monitored_count: int
    alert_count: int
    active_download_count: int
    completion_percent: int


@dataclass(frozen=True)
class DashboardMissionControlView:
    """Aggregated dashboard presenter for the mission-control layout."""

    gauges: tuple[DashboardGaugeView, ...]
    scoreboard: tuple[DashboardScoreboardItemView, ...]
    alerts: tuple[DashboardAlertRowView, ...]
    download_exceptions: tuple[DashboardAlertRowView, ...]
    recent_activity: tuple[DashboardRecentActivityItemView, ...]
    footer: DashboardFooterStripView
    all_clear: bool


_GetTemplates = Callable[[], Jinja2Templates]
_BuildContext = Callable[..., dict[str, object]]
_FormatIssueNumber = Callable[[float], str]
_FormatFileSize = Callable[[int], str]
_DownloadClientLabel = Callable[[str], str]

_get_templates: _GetTemplates | None = None
_build_context: _BuildContext | None = None
_format_issue_number: _FormatIssueNumber | None = None
_format_filesize: _FormatFileSize | None = None
_download_client_label: _DownloadClientLabel | None = None


def configure_dashboard_routes(
    *,
    get_templates: _GetTemplates,
    build_context: _BuildContext,
    format_issue_number: _FormatIssueNumber,
    format_filesize: _FormatFileSize,
    download_client_label: _DownloadClientLabel,
) -> None:
    """Provide shared UI runtime dependencies from the facade module."""
    global _get_templates
    global _build_context
    global _format_issue_number
    global _format_filesize
    global _download_client_label
    _get_templates = get_templates
    _build_context = build_context
    _format_issue_number = format_issue_number
    _format_filesize = format_filesize
    _download_client_label = download_client_label


def _templates() -> Jinja2Templates:
    if _get_templates is None:
        msg = "dashboard routes have not been configured with templates"
        raise RuntimeError(msg)
    return _get_templates()


def _ctx(request: Request, user: object | None = None, **kwargs: object) -> dict[str, object]:
    if _build_context is None:
        msg = "dashboard routes have not been configured with a context builder"
        raise RuntimeError(msg)
    context: Mapping[str, object] = _build_context(request, user, **kwargs)
    return dict(context)


def _issue_number(value: float) -> str:
    if _format_issue_number is None:
        msg = "dashboard routes have not been configured with an issue formatter"
        raise RuntimeError(msg)
    return _format_issue_number(value)


def _filesize(value: int) -> str:
    if _format_filesize is None:
        msg = "dashboard routes have not been configured with a filesize formatter"
        raise RuntimeError(msg)
    return _format_filesize(value)


def _client_label(client_type: str) -> str:
    if _download_client_label is None:
        msg = "dashboard routes have not been configured with a client labeler"
        raise RuntimeError(msg)
    return _download_client_label(client_type)


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Render the dashboard as an executive operations briefing."""
    dashboard_payload = await load_dashboard_intelligence(session, allow_rollup_refresh=True)
    dashboard_view = await build_dashboard_view(session, dashboard_payload)
    continue_reading = await load_dashboard_continue_reading(session, user_id=user.id)
    return _templates().TemplateResponse(
        request,
        "pages/dashboard.html",
        _ctx(
            request,
            user,
            dashboard=dashboard_payload,
            dashboard_view=dashboard_view,
            continue_reading=continue_reading,
        ),
    )


async def load_dashboard_continue_reading(
    session: AsyncSession,
    *,
    user_id: int,
) -> tuple[ReadingIssueCardView, ...]:
    """Load the bounded dashboard shelf without coupling it to operations data."""
    if not get_settings().reader_enabled:
        return ()
    page = await list_continue_reading(
        session,
        user_id=user_id,
        page=1,
        per_page=8,
    )
    return present_reading_issues(page.items, density="dashboard")


@router.get(
    "/htmx/dashboard/continue-reading",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def dashboard_continue_reading_partial(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Render the non-polling private Continue Reading shelf fragment."""
    continue_reading = await load_dashboard_continue_reading(session, user_id=user.id)
    return _templates().TemplateResponse(
        request,
        "partials/dashboard_continue_reading.html",
        _ctx(request, user, continue_reading=continue_reading),
    )


async def load_dashboard_intelligence(
    session: AsyncSession,
    *,
    allow_rollup_refresh: bool,
) -> DashboardIntelligence:
    """Build the dashboard intelligence payload for a route render."""
    from pullbox.services.dashboard_intelligence_service import DashboardIntelligenceService

    service = DashboardIntelligenceService(session)
    return await service.build_dashboard(allow_rollup_refresh=allow_rollup_refresh)


async def load_dashboard_active_download_count(session: AsyncSession) -> int:
    """Return the count used by the downloads gauge and footer strip."""
    active_states = (
        DownloadState.QUEUED,
        DownloadState.SENT,
        DownloadState.DOWNLOADING,
        DownloadState.FINALIZING,
    )
    return int(
        (
            await session.execute(
                select(func.count(DownloadHistory.id)).where(
                    DownloadHistory.state.in_(active_states)
                )
            )
        ).scalar_one()
        or 0
    )


async def load_dashboard_download_exceptions(
    session: AsyncSession,
    current_time: datetime,
) -> list[DashboardAlertRowView]:
    """Return recent download failures that need dashboard attention."""
    exception_states = (DownloadState.FAILED, DownloadState.RETRY_PENDING)
    exception_rows = list(
        (
            await session.execute(
                select(DownloadHistory)
                .options(joinedload(DownloadHistory.issue).joinedload(Issue.series))
                .where(
                    DownloadHistory.state.in_(exception_states),
                    or_(
                        DownloadHistory.error_message.is_(None),
                        DownloadHistory.error_message != "Cancelled by user",
                    ),
                )
                .order_by(DownloadHistory.updated_at.desc())
                .limit(5)
            )
        ).scalars()
    )

    rows: list[DashboardAlertRowView] = []
    for download in exception_rows:
        issue_label = "Issue context unavailable."
        if download.issue is not None and download.issue.series is not None:
            issue_label = (
                f"{download.issue.series.title} · #{_issue_number(download.issue.issue_number)}"
            )

        if download.state == DownloadState.RETRY_PENDING:
            state = "watch"
            state_label = "Retry pending"
            led_tone = "amber"
            detail = (
                f"{issue_label} Retrying after: "
                f"{download.error_message or 'Download client reported a transient failure.'}"
            )
            when = download.next_retry_at or download.updated_at
        else:
            state = "critical"
            state_label = "Failed"
            led_tone = "red"
            detail = (
                f"{issue_label} "
                f"{download.error_message or 'Download processing stopped with an unknown error.'}"
            )
            when = download.completed_at or download.updated_at

        rows.append(
            DashboardAlertRowView(
                key=f"download-{download.id}",
                led_tone=led_tone,
                title=download.title,
                detail=detail,
                state=state,
                state_label=state_label,
                timing_label=dashboard_relative_time_label(when, current_time),
                href="/downloads?tab=history",
            )
        )

    return rows


async def load_dashboard_first_run_counts(session: AsyncSession) -> dict[str, int]:
    """Return first-run summary counts for the onboarding dashboard state."""
    total_series = int((await session.execute(select(func.count(Series.id)))).scalar_one() or 0)
    total_issues = int((await session.execute(select(func.count(Issue.id)))).scalar_one() or 0)
    wanted_count = int(
        (
            await session.execute(
                select(func.count(Issue.id)).where(Issue.status == IssueStatus.WANTED)
            )
        ).scalar_one()
        or 0
    )
    owned_count = int(
        (
            await session.execute(
                select(func.count(Issue.id)).where(Issue.status == IssueStatus.OWNED)
            )
        ).scalar_one()
        or 0
    )
    return {
        "total_series": total_series,
        "total_issues": total_issues,
        "wanted_count": wanted_count,
        "owned_count": owned_count,
    }


async def build_dashboard_view(
    session: AsyncSession,
    dashboard: DashboardIntelligence,
) -> DashboardMissionControlView:
    """Build the mission-control dashboard presenter from live app data."""
    current_time = dashboard.freshness
    window_start = current_time - timedelta(days=7)

    series_counts = (
        await session.execute(
            select(
                func.count(Series.id),
                func.coalesce(func.sum(case((Series.monitored.is_(True), 1), else_=0)), 0),
            )
        )
    ).one()
    total_series = int(series_counts[0] or 0)
    monitored_count = int(series_counts[1] or 0)
    paused_count = max(0, total_series - monitored_count)

    issue_counts = (
        await session.execute(
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
            )
        )
    ).one()
    total_issues = int(issue_counts[0] or 0)
    owned_count = int(issue_counts[1] or 0)
    wanted_count = int(issue_counts[2] or 0)
    completion_percent = round((owned_count / total_issues) * 100) if total_issues > 0 else 0

    imported_time_expr = func.coalesce(
        DownloadHistory.imported_at,
        DownloadHistory.completed_at,
        DownloadHistory.updated_at,
    )
    imported_this_week = int(
        (
            await session.execute(
                select(func.count(DownloadHistory.id)).where(
                    or_(
                        DownloadHistory.imported_at.is_not(None),
                        DownloadHistory.state == DownloadState.IMPORTED,
                    ),
                    imported_time_expr >= window_start,
                    imported_time_expr < current_time,
                )
            )
        ).scalar_one()
        or 0
    )

    library_metrics = (
        await session.execute(
            select(
                func.coalesce(func.sum(LibraryFile.file_size), 0),
                func.coalesce(
                    func.sum(
                        case(
                            (LibraryFile.file_modified_at >= window_start, LibraryFile.file_size),
                            else_=0,
                        )
                    ),
                    0,
                ),
            )
        )
    ).one()
    library_size_bytes = int(library_metrics[0] or 0)
    weekly_library_growth_bytes = int(library_metrics[1] or 0)

    active_download_count = await load_dashboard_active_download_count(session)
    download_exceptions = tuple(await load_dashboard_download_exceptions(session, current_time))
    recent_activity = tuple(await load_dashboard_recent_activity(session, current_time))
    alerts = tuple(build_dashboard_alert_rows(dashboard))
    search_quality_percent, search_quality_delta = await load_dashboard_search_quality(
        session,
        window_start=window_start,
        current_time=current_time,
        first_run=dashboard.is_first_run,
    )
    storage_free_bytes, storage_delta = await load_dashboard_storage_strip(session, dashboard)

    gauges = (
        DashboardGaugeView(
            key="completion",
            label="Completion",
            value_label=f"{completion_percent}%",
            tone=dashboard_completion_tone(completion_percent),
            stroke_offset=dashboard_gauge_offset(
                (completion_percent / 100.0) if completion_percent > 0 else 0.0
            ),
        ),
        DashboardGaugeView(
            key="wanted",
            label="Wanted",
            value_label=str(wanted_count),
            tone="info",
            stroke_offset=dashboard_gauge_offset(
                (wanted_count / total_issues) if total_issues > 0 else 0.0
            ),
        ),
        DashboardGaugeView(
            key="downloads",
            label="Downloads",
            value_label=str(active_download_count),
            tone="warning" if active_download_count > 0 else "neutral",
            stroke_offset=dashboard_gauge_offset(min(active_download_count / 10.0, 1.0)),
        ),
        DashboardGaugeView(
            key="health",
            label="Health",
            value_label=(
                "✓"
                if dashboard.live_pulse.health_alerts <= 0
                else str(dashboard.live_pulse.health_alerts)
            ),
            tone="success" if dashboard.live_pulse.health_alerts <= 0 else "danger",
            stroke_offset=dashboard_gauge_offset(
                1.0
                if dashboard.live_pulse.health_alerts <= 0
                else min(dashboard.live_pulse.health_alerts / 6.0, 1.0)
            ),
        ),
    )

    scoreboard = (
        DashboardScoreboardItemView(
            key="series",
            label="Series",
            value_label=str(total_series),
            delta_label=(
                f"{monitored_count} monitored · {paused_count} paused"
                if total_series > 0
                else "Collecting baseline"
            ),
        ),
        DashboardScoreboardItemView(
            key="issues-owned",
            label="Issues Owned",
            value_label=f"{owned_count:,}",
            delta_label=dashboard_weekly_count_delta(imported_this_week, dashboard.is_first_run),
        ),
        DashboardScoreboardItemView(
            key="library-size",
            label="Library Size",
            value_label=_filesize(library_size_bytes),
            delta_label=(
                f"+{_filesize(weekly_library_growth_bytes)} this week"
                if weekly_library_growth_bytes > 0
                else (
                    "Collecting baseline" if dashboard.is_first_run else "No file growth this week"
                )
            ),
        ),
        DashboardScoreboardItemView(
            key="storage-free",
            label="Storage Free",
            value_label=_filesize(storage_free_bytes),
            delta_label=storage_delta,
        ),
        DashboardScoreboardItemView(
            key="search-quality",
            label="Search Quality",
            value_label=search_quality_percent,
            delta_label=search_quality_delta,
        ),
    )

    footer = DashboardFooterStripView(
        monitored_count=monitored_count,
        alert_count=len(alerts),
        active_download_count=active_download_count,
        completion_percent=completion_percent,
    )

    return DashboardMissionControlView(
        gauges=gauges,
        scoreboard=scoreboard,
        alerts=alerts,
        download_exceptions=download_exceptions,
        recent_activity=recent_activity,
        footer=footer,
        all_clear=(len(alerts) == 0 and len(download_exceptions) == 0),
    )


def build_dashboard_alert_rows(
    dashboard: DashboardIntelligence,
) -> list[DashboardAlertRowView]:
    """Convert ranked priorities into the prototype alerts table."""
    visible = [priority for priority in dashboard.priorities if priority.score >= 40]
    rows: list[DashboardAlertRowView] = []
    for priority in visible[:5]:
        rows.append(
            DashboardAlertRowView(
                key=priority.key,
                led_tone=dashboard_led_tone(priority.state),
                title=priority.title,
                detail=priority.evidence,
                state=priority.state,
                state_label=priority.state_label,
                timing_label=priority.time_label,
                href=priority.cta_href,
            )
        )
    return rows


async def load_dashboard_search_quality(
    session: AsyncSession,
    *,
    window_start: datetime,
    current_time: datetime,
    first_run: bool,
) -> tuple[str, str]:
    """Return the search-quality scoreboard value and delta label."""
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(SearchLog.results_found), 0),
                func.coalesce(func.sum(SearchLog.results_grabbed + SearchLog.results_queued), 0),
                func.coalesce(func.sum(SearchLog.results_rejected), 0),
            ).where(SearchLog.created_at >= window_start, SearchLog.created_at < current_time)
        )
    ).one()

    found_count = int(row[0] or 0)
    matched_count = int(row[1] or 0)
    rejected_count = int(row[2] or 0)
    if found_count <= 0:
        return ("—", "Collecting baseline" if first_run else "No search results this week")

    quality_rate = round((matched_count / found_count) * 100)
    rejection_rate = round((rejected_count / found_count) * 100)
    return (f"{quality_rate}%", f"{rejection_rate}% rejection rate")


async def load_dashboard_storage_strip(
    session: AsyncSession,
    dashboard: DashboardIntelligence,
) -> tuple[int, str]:
    """Return storage-free bytes plus the runway label for the scoreboard."""
    storage_path = await resolve_dashboard_storage_path(session)
    try:
        free_bytes = int(shutil.disk_usage(storage_path).free)
    except OSError as exc:
        logger.warning(
            "dashboard.storage_usage.disk_usage_failed",
            path=str(storage_path),
            error=str(exc),
        )
        free_bytes = 0
    storage_card = next(
        (scorecard for scorecard in dashboard.scorecards if scorecard.key == "storage-runway"),
        None,
    )
    if storage_card is None:
        return (free_bytes, "Runway unavailable")
    return (free_bytes, storage_card.value_label)


async def load_dashboard_recent_activity(
    session: AsyncSession,
    current_time: datetime,
) -> list[DashboardRecentActivityItemView]:
    """Build the recent outcomes feed shown under the dashboard tables."""
    recent_items: list[tuple[datetime, DashboardRecentActivityItemView]] = []

    download_rows = (
        (
            await session.execute(
                select(DownloadHistory)
                .options(joinedload(DownloadHistory.issue).joinedload(Issue.series))
                .where(
                    or_(
                        DownloadHistory.imported_at.is_not(None),
                        DownloadHistory.state.in_(
                            (DownloadState.COMPLETED, DownloadState.IMPORTED)
                        ),
                    )
                )
                .order_by(
                    func.coalesce(
                        DownloadHistory.imported_at,
                        DownloadHistory.completed_at,
                        DownloadHistory.updated_at,
                    ).desc()
                )
                .limit(6)
            )
        )
        .scalars()
        .all()
    )

    for download in download_rows:
        activity_item = build_download_recent_activity_item(
            download,
            current_time,
            format_issue_number=_issue_number,
            download_client_label=_client_label,
        )
        if activity_item is not None:
            recent_items.append(activity_item)

    import_rows = (
        (
            await session.execute(
                select(ImportJob)
                .where(ImportJob.status.in_((ImportJobStatus.COMPLETED, ImportJobStatus.FAILED)))
                .order_by(func.coalesce(ImportJob.import_completed_at, ImportJob.updated_at).desc())
                .limit(4)
            )
        )
        .scalars()
        .all()
    )

    for job in import_rows:
        activity_item = build_import_recent_activity_item(job, current_time)
        if activity_item is not None:
            recent_items.append(activity_item)

    recent_items.sort(key=lambda item: item[0], reverse=True)
    return [item for _, item in recent_items[:5]]


@router.get(
    "/htmx/dashboard/briefing",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def dashboard_briefing_partial(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Return the mission-control header fragment for HTMX refreshes."""
    dashboard_payload = await load_dashboard_intelligence(session, allow_rollup_refresh=True)
    dashboard_view = await build_dashboard_view(session, dashboard_payload)
    return _templates().TemplateResponse(
        request,
        "partials/dashboard_briefing.html",
        _ctx(request, user, dashboard=dashboard_payload, dashboard_view=dashboard_view),
    )


@router.get(
    "/htmx/dashboard/live-pulse",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def dashboard_live_pulse_partial(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Return the compact live-pulse fragment for HTMX refreshes."""
    dashboard_payload = await load_dashboard_intelligence(session, allow_rollup_refresh=False)
    return _templates().TemplateResponse(
        request,
        "partials/dashboard_live_pulse.html",
        _ctx(request, user, dashboard=dashboard_payload),
    )


@router.get(
    "/htmx/dashboard/scoreboard",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def dashboard_scoreboard_partial(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Return the operating scoreboard fragment for HTMX refreshes."""
    dashboard_payload = await load_dashboard_intelligence(session, allow_rollup_refresh=False)
    dashboard_view = await build_dashboard_view(session, dashboard_payload)
    return _templates().TemplateResponse(
        request,
        "partials/dashboard_scoreboard.html",
        _ctx(request, user, dashboard=dashboard_payload, dashboard_view=dashboard_view),
    )


@router.get(
    "/htmx/dashboard/priorities",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def dashboard_priorities_partial(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Return the mission-control alerts table fragment for HTMX refreshes."""
    dashboard_payload = await load_dashboard_intelligence(session, allow_rollup_refresh=False)
    dashboard_view = await build_dashboard_view(session, dashboard_payload)
    return _templates().TemplateResponse(
        request,
        "partials/dashboard_priorities.html",
        _ctx(request, user, dashboard=dashboard_payload, dashboard_view=dashboard_view),
    )


@router.get(
    "/htmx/dashboard/watchlist",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def dashboard_watchlist_partial(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Return the 7-day watchlist fragment for HTMX refreshes."""
    dashboard_payload = await load_dashboard_intelligence(session, allow_rollup_refresh=False)
    return _templates().TemplateResponse(
        request,
        "partials/dashboard_watchlist.html",
        _ctx(request, user, dashboard=dashboard_payload),
    )


@router.get(
    "/htmx/dashboard/exceptions",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def dashboard_exceptions_partial(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Return the aggregated exceptions fragment for HTMX refreshes."""
    dashboard_payload = await load_dashboard_intelligence(session, allow_rollup_refresh=False)
    return _templates().TemplateResponse(
        request,
        "partials/dashboard_exceptions.html",
        _ctx(request, user, dashboard=dashboard_payload),
    )


@router.get(
    "/htmx/dashboard/download-exceptions-panel",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def dashboard_download_exceptions_panel_partial(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Return the dashboard download-exceptions fragment for HTMX refreshes."""
    dashboard_payload = await load_dashboard_intelligence(session, allow_rollup_refresh=False)
    dashboard_view = await build_dashboard_view(session, dashboard_payload)
    return _templates().TemplateResponse(
        request,
        "partials/dashboard_download_exceptions_panel.html",
        _ctx(request, user, dashboard=dashboard_payload, dashboard_view=dashboard_view),
    )


@router.get(
    "/htmx/dashboard/recent-activity",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def dashboard_recent_activity_partial(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Return the recent activity feed fragment for HTMX refreshes."""
    dashboard_payload = await load_dashboard_intelligence(session, allow_rollup_refresh=False)
    dashboard_view = await build_dashboard_view(session, dashboard_payload)
    return _templates().TemplateResponse(
        request,
        "partials/dashboard_recent_activity.html",
        _ctx(request, user, dashboard=dashboard_payload, dashboard_view=dashboard_view),
    )
