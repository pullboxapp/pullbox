"""
Pullbox UI routes — server-rendered HTML pages.

Renders full page templates for direct browser navigation.
Authenticated routes use require_auth; unauthenticated routes
(login, setup) are accessible without credentials.
"""

import json
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

import structlog
from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

import pullbox
from pullbox.api.deps import load_sidebar_health_counts
from pullbox.config import get_settings
from pullbox.core.naming import (
    resolve_collection_non_standard_file_template,
    resolve_single_non_standard_file_template,
)
from pullbox.models.config import SystemConfig
from pullbox.models.download import DownloadHistory
from pullbox.models.health import HealthStatus
from pullbox.services.cover_url_service import build_series_cover_url
from pullbox.ui import (
    blocklist_routes,
    dashboard_routes,
    downloads_routes,
    health_routes,
    import_orphaned_routes,
    import_redirect_routes,
    import_routes,
    intervention_routes,
    library_routes,
    matching_routes,
    post_processing_routes,
    public_routes,
    pull_list_routes,
    reading_routes,
    search_history_routes,
    security_routes,
    series_detail_routes,
    series_routes,
    settings_routes,
    story_arc_routes,
    system_routes,
    utilities_routes,
    whats_new_routes,
)
from pullbox.ui.download_queue_names import build_queue_names as _build_queue_names
from pullbox.ui.formatters import (
    dashboard_state_pill_tone as _dashboard_state_pill_tone,
)
from pullbox.ui.formatters import (
    format_dlspeed as _format_dlspeed,
)
from pullbox.ui.formatters import (
    format_duration_ms as _format_duration_ms,
)
from pullbox.ui.formatters import (
    format_eta as _format_eta,
)
from pullbox.ui.formatters import (
    format_filesize as _format_filesize,
)
from pullbox.ui.formatters import (
    format_issue_number as _format_issue_number,
)
from pullbox.ui.formatters import (
    format_localtime as _format_localtime,
)
from pullbox.ui.formatters import (
    format_localtime_time as _format_localtime_time,
)
from pullbox.ui.formatters import (
    format_series_year_label as _format_series_year_label,
)
from pullbox.ui.formatters import (
    format_type_display as _format_type_display,
)
from pullbox.ui.formatters import (
    humanize_download_error as _humanize_download_error,
)
from pullbox.ui.formatters import (
    sanitize_rich_html_filter as _sanitize_rich_html_filter,
)
from pullbox.ui.standalone_shell import main_shell_asset_version

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["UI"])

_TEMPLATE_DIR = Path(__file__).parent / "templates"
# Jinja2Templates enables autoescape for .html by default (XSS protection)
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

# ── Cached instance name (populated at startup, updated on settings save) ──
_cached_instance_name: str = "Pullbox"


def get_instance_name() -> str:
    """Return cached instance name for use in templates."""
    return _cached_instance_name


templates.env.globals["instance_name"] = get_instance_name
templates.env.filters["sanitize_rich_html"] = _sanitize_rich_html_filter

# ── Cached base URL (populated at startup, updated on settings save) ──
_cached_base_url: str = "http://localhost:8585"


def get_base_url() -> str:
    """Return cached base URL for use in templates and API responses."""
    return _cached_base_url


templates.env.globals["base_url"] = get_base_url


def _set_hx_toast(response: Response, message: str, level: str = "info") -> Response:
    """Attach a server-triggered toast event for HTMX responses."""
    response.headers["HX-Trigger"] = json.dumps(
        {
            "toast": {
                "message": message,
                "level": level,
            }
        }
    )
    return response


def _load_dashboard_storage_health_status() -> str:
    """Mirror the health page disk thresholds for the dashboard storage card."""
    from pullbox.config import get_settings as _get_settings

    disk_path = Path.cwd()

    try:
        settings = _get_settings()
        if settings.data_dir != Path("/data"):
            disk_path = settings.data_dir
    except Exception:  # pragma: no cover - defensive fallback to cwd
        logger.warning("dashboard.storage_health.settings_load_failed")

    try:
        usage = shutil.disk_usage(disk_path)
        disk_pct = (usage.used / usage.total) * 100

        if disk_pct > 95:
            return HealthStatus.UNHEALTHY.value
        if disk_pct > 80:
            return HealthStatus.DEGRADED.value
        return HealthStatus.HEALTHY.value
    except OSError as exc:
        logger.warning("dashboard.storage_health.disk_usage_failed", error=str(exc))
        return HealthStatus.UNHEALTHY.value


templates.env.filters["issue_num"] = _format_issue_number


templates.env.filters["filesize"] = _format_filesize


templates.env.filters["dlspeed"] = _format_dlspeed


templates.env.filters["eta"] = _format_eta


templates.env.filters["duration_ms"] = _format_duration_ms


templates.env.globals["format_series_year_label"] = _format_series_year_label
templates.env.globals["series_cover_url"] = build_series_cover_url


templates.env.filters["localtime"] = _format_localtime


templates.env.filters["localtime_time"] = _format_localtime_time


templates.env.globals["dashboard_state_pill_tone"] = _dashboard_state_pill_tone


templates.env.filters["humanize_error"] = _humanize_download_error


templates.env.filters["type_display"] = _format_type_display


def _ctx(request: Request, user: object | None = None, **kwargs: object) -> dict[str, object]:
    """Build standard template context."""
    csrf_token = getattr(request.state, "csrf_token", None) or ""
    sidebar_context = getattr(request.state, "sidebar_context", None) or {}
    app_shell_asset_version = main_shell_asset_version()
    # Display settings — read from cache, mapped to camelCase for JavaScript
    try:
        from pullbox.core.display_time import get_cached_display_settings

        s = get_cached_display_settings()
        display_config = {
            "timezone": s.get("timezone", "browser"),
            "dateFormat": s.get("date_format", "MMM DD, YYYY"),
            "timeFormat": s.get("time_format", "24h"),
            "showSeconds": s.get("show_seconds", False),
            "showTimezone": s.get("show_timezone", True),
            "showAmPm": s.get("show_ampm", True),
        }
    except Exception:
        display_config = {
            "timezone": "browser",
            "dateFormat": "MMM DD, YYYY",
            "timeFormat": "24h",
            "showSeconds": False,
            "showTimezone": True,
            "showAmPm": True,
        }
    return {
        "request": request,
        "version": pullbox.__version__,
        "app_shell_asset_version": app_shell_asset_version,
        "user": user,
        "csrf_token": csrf_token,
        "display_config": display_config,
        "reader_enabled": get_settings().reader_enabled,
        **sidebar_context,
        **kwargs,
    }


async def _load_system_config_values(
    session: AsyncSession,
    keys: Sequence[str],
) -> dict[str, str]:
    """Load selected SystemConfig values into a flat dict."""
    result = await session.execute(select(SystemConfig).where(SystemConfig.key.in_(list(keys))))
    return {cfg.key: cfg.value for cfg in result.scalars().all()}


def _resolve_utility_browse_paths(configs: dict[str, str]) -> dict[str, str]:
    """Resolve effective utility browse directories for shared file browser launchers."""
    try:
        from pullbox.config import get_settings as _get_settings
        from pullbox.utilities.settings import resolve_utility_directory

        settings = _get_settings()
        return {
            "trash_folder": str(
                resolve_utility_directory(
                    db_value=configs.get("utility_trash_folder", ""),
                    default_parent=settings.library_root,
                    default_subdir=".trash",
                    library_root=settings.library_root,
                    data_dir=settings.data_dir,
                )
            ),
            "export_folder": str(
                resolve_utility_directory(
                    db_value=configs.get("utility_export_folder", ""),
                    default_parent=settings.data_dir,
                    default_subdir="exports",
                    library_root=settings.library_root,
                    data_dir=settings.data_dir,
                )
            ),
        }
    except Exception:
        logger.warning("ui.utility_browse_paths_resolve_failed", exc_info=True)
        return {
            "trash_folder": configs.get("utility_trash_folder", ""),
            "export_folder": configs.get("utility_export_folder", ""),
        }


def _build_rename_templates(configs: Mapping[str, str]) -> dict[str, str]:
    """Normalize the naming template payload shared by rename-driven UIs."""
    return {
        "folder": configs.get("series_folder_template", "{Series} ({Year})"),
        "issue": configs.get("comic_file_template", "{Series} ({Year}) #{Issue:03d}"),
        "annual": configs.get("annual_file_template", "{Series} ({Year}) Annual #{Issue:03d}"),
        "collectionNonStandard": resolve_collection_non_standard_file_template(
            configs.get("non_standard_file_template")
        ),
        "singleNonStandard": resolve_single_non_standard_file_template(
            configs.get("single_non_standard_file_template")
        ),
        "replaceIllegalCharacters": configs.get("replace_illegal_characters", "true"),
        "colonReplacement": configs.get("colon_replacement", "dash"),
    }


_SIDEBAR_BADGE_NO_STORE_HEADERS = {
    "Cache-Control": "no-store, max-age=0, must-revalidate",
    "Pragma": "no-cache",
}


def _sidebar_badge_response(
    request: Request,
    user: object | None,
    *,
    count: int,
    badge_classes: str,
    template: str = "partials/sidebar_count_badge.html",
) -> Response:
    """Return an uncacheable HTML badge fragment for live HTMX polling."""
    return templates.TemplateResponse(
        request,
        template,
        _ctx(request, user, count=count, badge_classes=badge_classes),
        headers=_SIDEBAR_BADGE_NO_STORE_HEADERS,
    )


# ── Authenticated routes ──────────────────────────────────────────


dashboard_routes.configure_dashboard_routes(
    get_templates=lambda: templates,
    build_context=_ctx,
    format_issue_number=_format_issue_number,
    format_filesize=_format_filesize,
    download_client_label=downloads_routes.download_client_type_label,
)
router.include_router(dashboard_routes.router)

DashboardGaugeView = dashboard_routes.DashboardGaugeView
DashboardScoreboardItemView = dashboard_routes.DashboardScoreboardItemView
DashboardAlertRowView = dashboard_routes.DashboardAlertRowView
DashboardRecentActivityItemView = dashboard_routes.DashboardRecentActivityItemView
DashboardFooterStripView = dashboard_routes.DashboardFooterStripView
DashboardMissionControlView = dashboard_routes.DashboardMissionControlView
dashboard = dashboard_routes.dashboard
_load_dashboard_intelligence = dashboard_routes.load_dashboard_intelligence
_load_dashboard_active_download_count = dashboard_routes.load_dashboard_active_download_count
_load_dashboard_download_exceptions = dashboard_routes.load_dashboard_download_exceptions
_load_dashboard_first_run_counts = dashboard_routes.load_dashboard_first_run_counts
_build_dashboard_view = dashboard_routes.build_dashboard_view
_build_dashboard_alert_rows = dashboard_routes.build_dashboard_alert_rows
_load_dashboard_search_quality = dashboard_routes.load_dashboard_search_quality
_load_dashboard_storage_strip = dashboard_routes.load_dashboard_storage_strip
_load_dashboard_recent_activity = dashboard_routes.load_dashboard_recent_activity
_dashboard_completion_tone = dashboard_routes.dashboard_completion_tone
_dashboard_led_tone = dashboard_routes.dashboard_led_tone
_dashboard_gauge_offset = dashboard_routes.dashboard_gauge_offset
_dashboard_relative_time_label = dashboard_routes.dashboard_relative_time_label
_dashboard_weekly_count_delta = dashboard_routes.dashboard_weekly_count_delta
dashboard_briefing_partial = dashboard_routes.dashboard_briefing_partial
dashboard_live_pulse_partial = dashboard_routes.dashboard_live_pulse_partial
dashboard_scoreboard_partial = dashboard_routes.dashboard_scoreboard_partial
dashboard_priorities_partial = dashboard_routes.dashboard_priorities_partial
dashboard_watchlist_partial = dashboard_routes.dashboard_watchlist_partial
dashboard_exceptions_partial = dashboard_routes.dashboard_exceptions_partial
dashboard_download_exceptions_panel_partial = (
    dashboard_routes.dashboard_download_exceptions_panel_partial
)
dashboard_recent_activity_partial = dashboard_routes.dashboard_recent_activity_partial


series_routes.configure_series_routes(
    get_templates=lambda: templates,
    build_context=_ctx,
    load_system_config_values=_load_system_config_values,
)
router.include_router(series_routes.router)

_resolve_series_view = series_routes.resolve_series_view
_series_type_code = series_routes.series_type_code
_build_series_filters = series_routes.build_series_filters
_series_cover_src = series_routes.series_cover_src
series_list = series_routes.series_list
series_selection_ids = series_routes.series_selection_ids
add_series_page = series_routes.add_series_page
_normalize_add_series_sort = series_routes.normalize_add_series_sort
_sort_add_series_results = series_routes.sort_add_series_results
_load_add_series_search_context = series_routes.load_add_series_search_context
htmx_search_series = series_routes.htmx_search_series


series_detail_routes.configure_series_detail_routes(
    get_templates=lambda: templates,
    build_context=_ctx,
)
router.include_router(series_detail_routes.router)

_load_series_issues_context = series_detail_routes.load_series_issues_context
series_detail = series_detail_routes.series_detail
issue_detail = series_detail_routes.issue_detail
htmx_toggle_issue_status = series_detail_routes.htmx_toggle_issue_status
htmx_issue_search_results = series_detail_routes.htmx_issue_search_results
htmx_delete_series = series_detail_routes.htmx_delete_series
htmx_add_alternate_name = series_detail_routes.htmx_add_alternate_name
htmx_remove_alternate_name = series_detail_routes.htmx_remove_alternate_name
htmx_series_issues = series_detail_routes.htmx_series_issues


async def _download_progress_map_bridge(
    session: AsyncSession,
    queue_items: Sequence[DownloadHistory],
    *,
    fallback_progress: Mapping[int, object],
) -> dict[int, object]:
    """Route progress-map calls through the facade for compatibility tests."""
    return await _load_download_progress_map(
        session,
        queue_items,
        fallback_progress=fallback_progress,
    )


async def _download_queue_names_bridge(
    session: AsyncSession,
    downloads: Sequence[DownloadHistory],
) -> dict[int, str]:
    """Route queue-name calls through the facade for compatibility tests."""
    return await _build_queue_names(session, downloads)


downloads_routes.configure_downloads_routes(
    get_templates=lambda: templates,
    build_context=_ctx,
    format_eta=_format_eta,
    build_queue_names=_download_queue_names_bridge,
    load_download_progress_map=_download_progress_map_bridge,
    sidebar_badge_no_store_headers=_SIDEBAR_BADGE_NO_STORE_HEADERS,
)
router.include_router(downloads_routes.router)

DownloadQueueRowView = downloads_routes.DownloadQueueRowView
_get_download_queue_count = downloads_routes.get_download_queue_count
_get_download_history_count = downloads_routes.get_download_history_count
_normalize_download_history_sort = downloads_routes.normalize_download_history_sort
_get_download_history_order_by = downloads_routes.get_download_history_order_by
_get_download_history_filters = downloads_routes.get_download_history_filters
_download_client_type_label = downloads_routes.download_client_type_label
_normalize_download_queue_client_state = downloads_routes.normalize_download_queue_client_state
_download_queue_client_state_token = downloads_routes.download_queue_client_state_token
_is_download_queue_finalization_state = downloads_routes.is_download_queue_finalization_state
_snapshot_value = downloads_routes.snapshot_value
_snapshot_progress = downloads_routes.snapshot_progress
_build_live_progress_snapshot = downloads_routes.build_live_progress_snapshot
_build_download_queue_row_view = downloads_routes.build_download_queue_row_view
_build_download_queue_rows = downloads_routes.build_download_queue_rows
_load_download_queue_context = downloads_routes.load_download_queue_context
_load_download_progress_map = downloads_routes.load_download_progress_map
_load_download_history_context = downloads_routes.load_download_history_context
downloads = downloads_routes.downloads


def _post_processing_recent_completion_ids_bridge() -> set[int]:
    """Route recent-completion checks through the facade for compatibility tests."""
    return _get_recent_post_processing_completion_ids()


async def _post_processing_live_status_map_bridge(
    session: AsyncSession,
    active_items: Sequence[DownloadHistory],
) -> dict[int, dict[str, object]]:
    """Route live-status checks through the facade for compatibility tests."""
    return await _load_post_processing_live_status_map(session, active_items)


post_processing_routes.configure_post_processing_routes(
    get_templates=lambda: templates,
    build_context=_ctx,
    download_client_label=_download_client_type_label,
    get_recent_completion_ids=_post_processing_recent_completion_ids_bridge,
    load_live_status_map=_post_processing_live_status_map_bridge,
    sidebar_badge_no_store_headers=_SIDEBAR_BADGE_NO_STORE_HEADERS,
)
router.include_router(post_processing_routes.router)

_load_post_processing_client_options = post_processing_routes.load_post_processing_client_options
_normalize_post_processing_result_filter = (
    post_processing_routes.normalize_post_processing_result_filter
)
_normalize_post_processing_tab = post_processing_routes.normalize_post_processing_tab
_normalize_post_processing_filter_alias = (
    post_processing_routes.normalize_post_processing_filter_alias
)
_resolve_post_processing_result_filter = (
    post_processing_routes.resolve_post_processing_result_filter
)
_normalize_post_processing_sort = post_processing_routes.normalize_post_processing_sort
_post_processing_history_completed_expr = (
    post_processing_routes.post_processing_history_completed_expr
)
_get_post_processing_history_order_by = post_processing_routes.get_post_processing_history_order_by
_post_processing_active_clause = post_processing_routes.post_processing_active_clause
_get_recent_post_processing_completion_ids = (
    post_processing_routes.get_recent_post_processing_completion_ids
)
_post_processing_imported_clause = post_processing_routes.post_processing_imported_clause
_post_processing_failed_clause = post_processing_routes.post_processing_failed_clause
_load_post_processing_status_context = post_processing_routes.load_post_processing_status_context
_load_post_processing_live_status_map = post_processing_routes.load_post_processing_live_status_map
_load_post_processing_history_context = post_processing_routes.load_post_processing_history_context
post_processing = post_processing_routes.post_processing
htmx_pp_queue = post_processing_routes.htmx_pp_queue
htmx_pp_queue_detail = post_processing_routes.htmx_pp_queue_detail
htmx_pp_status = post_processing_routes.htmx_pp_status
htmx_pp_history = post_processing_routes.htmx_pp_history


blocklist_routes.configure_blocklist_routes(
    get_templates=lambda: templates,
    build_context=_ctx,
)
router.include_router(blocklist_routes.router)

blocklist_page = blocklist_routes.blocklist_page
htmx_blocklist = blocklist_routes.htmx_blocklist
htmx_blocklist_error_detail = blocklist_routes.htmx_blocklist_error_detail
_load_blocklist_context = blocklist_routes.load_blocklist_context
_normalize_blocklist_sort = blocklist_routes.normalize_blocklist_sort
_parse_blocklist_reason = blocklist_routes.parse_blocklist_reason


matching_routes.configure_matching_routes(
    get_templates=lambda: templates,
    build_context=_ctx,
)
router.include_router(matching_routes.queue_router)

matching_queue = matching_routes.matching_queue


library_routes.configure_library_routes(
    get_templates=lambda: templates,
    build_context=_ctx,
    load_system_config_values=_load_system_config_values,
    build_rename_templates=_build_rename_templates,
    resolve_utility_browse_paths=_resolve_utility_browse_paths,
    format_filesize=_format_filesize,
    format_localtime=_format_localtime,
    dashboard_gauge_offset=dashboard_routes.dashboard_gauge_offset,
)
router.include_router(library_routes.router)


import_routes.configure_import_routes(
    get_templates=lambda: templates,
    build_context=_ctx,
)
router.include_router(import_routes.router)

import_page = import_routes.import_page
import_progress_partial = import_routes.import_progress_partial
import_progress_state = import_routes.import_progress_state
import_review_partial = import_routes.import_review_partial
import_series_reconcile = import_routes.import_series_reconcile
import_results_partial = import_routes.import_results_partial
import_series_details_partial = import_routes.import_series_details_partial
import_conflicts_partial = import_routes.import_conflicts_partial
import_log_panel = import_routes.import_log_panel
import_cv_search = import_routes.import_cv_search

LibraryGaugeView = library_routes.LibraryGaugeView
LibraryStatStripItemView = library_routes.LibraryStatStripItemView
LibraryFormatPillView = library_routes.LibraryFormatPillView
LibraryBrowserCatalogEntry = library_routes.LibraryBrowserCatalogEntry
LibraryBrowserTreeNodeView = library_routes.LibraryBrowserTreeNodeView
LibraryBrowserRowView = library_routes.LibraryBrowserRowView
LibraryBrowserSortableRow = library_routes.LibraryBrowserSortableRow
LibraryBreadcrumbView = library_routes.LibraryBreadcrumbView
LibraryWorkspaceView = library_routes.LibraryWorkspaceView

library = library_routes.library
_library_format_pill_tone = library_routes.library_format_pill_tone
_library_stat_tone = library_routes.library_stat_tone
_library_file_type_tone = library_routes.library_file_type_tone
_library_file_format_label = library_routes.library_file_format_label
_library_is_convertible_file_format = library_routes.library_is_convertible_file_format
_library_mix_label = library_routes.library_mix_label
_normalize_library_browser_sort = library_routes.normalize_library_browser_sort
_library_browser_sort_value = library_routes.library_browser_sort_value
_library_href = library_routes.library_href
_library_clamp_browse_path = library_routes.library_clamp_browse_path
_load_library_series_preview_metrics = library_routes.load_library_series_preview_metrics
_load_library_browser_catalog_entries = library_routes.load_library_browser_catalog_entries
_build_library_browser_snapshot = library_routes.build_library_browser_snapshot
_library_browser_empty_state = library_routes.library_browser_empty_state
_build_library_workspace_view = library_routes.build_library_workspace_view


# ── Import History ────────────────────────────────────────────────────────


router.include_router(import_redirect_routes.router)

import_history_redirect = import_redirect_routes.import_history_redirect
import_orphaned_redirect = import_redirect_routes.import_orphaned_redirect


import_orphaned_routes.configure_import_orphaned_routes(
    get_templates=lambda: templates,
    build_context=_ctx,
)
router.include_router(import_orphaned_routes.router)

import_orphaned_cv_search = import_orphaned_routes.import_orphaned_cv_search
import_orphaned_recovery = import_orphaned_routes.import_orphaned_recovery
_load_import_orphaned_context = import_orphaned_routes.load_import_orphaned_context


utilities_routes.configure_utilities_routes(
    get_templates=lambda: templates,
    build_context=_ctx,
    load_system_config_values=_load_system_config_values,
    resolve_utility_browse_paths=_resolve_utility_browse_paths,
    build_rename_templates=_build_rename_templates,
)
router.include_router(utilities_routes.page_router)

utilities_page = utilities_routes.utilities_page
utilities_converter = utilities_routes.utilities_converter
utilities_mass_convert = utilities_routes.utilities_mass_convert
utilities_mass_rename = utilities_routes.utilities_mass_rename
utilities_integrity = utilities_routes.utilities_integrity
utilities_db_check = utilities_routes.utilities_db_check
utilities_export = utilities_routes.utilities_export
_load_utility_queue_snapshot = utilities_routes.load_utility_queue_snapshot


settings_routes.configure_settings_routes(
    get_templates=lambda: templates,
    build_context=_ctx,
    resolve_utility_browse_paths=_resolve_utility_browse_paths,
)
router.include_router(settings_routes.page_router)
settings = settings_routes.settings
_load_settings_tab = settings_routes.load_settings_tab
_load_client_status_seed = settings_routes.load_client_status_seed
_load_indexer_status_seed = settings_routes.load_indexer_status_seed
SETTINGS_TABS = settings_routes.SETTINGS_TABS


system_routes.configure_system_routes(
    get_templates=lambda: templates,
    build_context=_ctx,
)
router.include_router(system_routes.page_router)
system_page = system_routes.system_page
_load_system_tab = system_routes.load_system_tab
SYSTEM_TABS = system_routes.SYSTEM_TABS


async def _load_sidebar_health_counts_bridge(session: AsyncSession) -> tuple[int, int]:
    """Route health badge counts through the facade for compatibility tests."""
    return await load_sidebar_health_counts(session)


health_routes.configure_health_routes(
    get_templates=lambda: templates,
    build_context=_ctx,
    dashboard_gauge_offset=dashboard_routes.dashboard_gauge_offset,
    dashboard_relative_time_label=dashboard_routes.dashboard_relative_time_label,
    download_client_type_label=downloads_routes.download_client_type_label,
    sidebar_badge_response=_sidebar_badge_response,
    load_sidebar_health_counts=_load_sidebar_health_counts_bridge,
)
router.include_router(health_routes.router)

HealthGaugeView = health_routes.HealthGaugeView
HealthScoreboardItemView = health_routes.HealthScoreboardItemView
HealthComponentStatView = health_routes.HealthComponentStatView
HealthCheckItemView = health_routes.HealthCheckItemView
HealthHistoryRowView = health_routes.HealthHistoryRowView
HealthSubjectSummaryView = health_routes.HealthSubjectSummaryView
HealthComponentView = health_routes.HealthComponentView
HealthFooterStripView = health_routes.HealthFooterStripView
HealthMonitoringView = health_routes.HealthMonitoringView

health_page = health_routes.health_page
health_status_partial = health_routes.health_status_partial
health_badge_partial = health_routes.health_badge_partial
health_download_clients_status_partial = health_routes.health_download_clients_status_partial
health_download_clients_page = health_routes.health_download_clients_page
health_download_client_status_partial = health_routes.health_download_client_status_partial
health_download_client_page = health_routes.health_download_client_page
health_indexers_status_partial = health_routes.health_indexers_status_partial
health_indexers_page = health_routes.health_indexers_page
health_indexer_status_partial = health_routes.health_indexer_status_partial
health_indexer_page = health_routes.health_indexer_page
health_component_status_partial = health_routes.health_component_status_partial
health_component_page = health_routes.health_component_page

_object_to_int = health_routes._object_to_int
_load_health_data = health_routes._load_health_data
_normalize_health_history_sort = health_routes._normalize_health_history_sort
_health_history_order_by = health_routes._health_history_order_by
_health_history_url = health_routes._health_history_url
_health_history_prefers_subchecks = health_routes._health_history_prefers_subchecks
_build_health_view = health_routes._build_health_view
_build_health_component_view = health_routes._build_health_component_view
_select_health_component_view = health_routes._select_health_component_view
_build_health_component_footer_items = health_routes._build_health_component_footer_items
_parse_health_details_json = health_routes._parse_health_details_json
_health_detail_checks = health_routes._health_detail_checks
_load_latest_health_subject_summary_rows = health_routes._load_latest_health_subject_summary_rows
_download_client_endpoint_summary = health_routes._download_client_endpoint_summary
_download_client_placeholder_checks = health_routes._download_client_placeholder_checks
_health_response_or_dash = health_routes._health_response_or_dash
_load_prowlarr_route_config = health_routes._load_prowlarr_route_config
_indexer_endpoint_summary = health_routes._indexer_endpoint_summary
_indexer_kind_detail_label = health_routes._indexer_kind_detail_label
_indexer_content_type_label = health_routes._indexer_content_type_label
_prowlarr_placeholder_checks = health_routes._prowlarr_placeholder_checks
_indexer_placeholder_checks = health_routes._indexer_placeholder_checks
_build_download_client_registry_rows = health_routes._build_download_client_registry_rows
_build_indexer_registry_rows = health_routes._build_indexer_registry_rows
_build_download_client_detail_view = health_routes._build_download_client_detail_view
_build_indexer_detail_view = health_routes._build_indexer_detail_view
_health_checks_from_details = health_routes._health_checks_from_details
_health_component_card_stats = health_routes._health_component_card_stats
_health_component_detail_stats = health_routes._health_component_detail_stats
_health_attention_label = health_routes._health_attention_label
_health_component_sublabel = health_routes._health_component_sublabel
_health_pill_tone = health_routes._health_pill_tone
_health_led_tone = health_routes._health_led_tone
_health_card_tone = health_routes._health_card_tone
_health_response_label = health_routes._health_response_label
_health_check_response_label = health_routes._health_check_response_label
_health_parenthetical_next_line = health_routes._health_parenthetical_next_line
_mapping_text = health_routes._mapping_text


security_routes.configure_security_routes(
    get_templates=lambda: templates,
    build_context=_ctx,
)
router.include_router(security_routes.page_router)

security_page = security_routes.security_page
_load_security_tab = security_routes.load_security_tab
SECURITY_TABS = security_routes.SECURITY_TABS


pull_list_routes.configure_pull_list_routes(
    get_templates=lambda: templates,
    build_context=_ctx,
)
router.include_router(pull_list_routes.router)

pull_list = pull_list_routes.pull_list


reading_routes.configure_reading_routes(
    get_templates=lambda: templates,
    build_context=_ctx,
)
router.include_router(reading_routes.router)

reading_workspace = reading_routes.reading_workspace


story_arc_routes.configure_story_arc_routes(
    get_templates=lambda: templates,
    build_context=_ctx,
)
router.include_router(story_arc_routes.router)

story_arc_list = story_arc_routes.story_arc_list
story_arc_detail = story_arc_routes.story_arc_detail


router.include_router(series_routes.htmx_router)


router.include_router(series_detail_routes.issue_router)


router.include_router(downloads_routes.htmx_router)
htmx_download_queue = downloads_routes.htmx_download_queue
htmx_download_history = downloads_routes.htmx_download_history
htmx_download_history_error_detail = downloads_routes.htmx_download_history_error_detail


router.include_router(utilities_routes.htmx_router)
htmx_utilities_tab = utilities_routes.htmx_utilities_tab


router.include_router(settings_routes.htmx_router)
htmx_settings_tab = settings_routes.htmx_settings_tab


router.include_router(system_routes.htmx_router)
htmx_system_tab = system_routes.htmx_system_tab


router.include_router(security_routes.htmx_router)
htmx_security_tab = security_routes.htmx_security_tab


router.include_router(matching_routes.htmx_router)
htmx_matching_series_search = matching_routes.htmx_matching_series_search
htmx_matching_issues = matching_routes.htmx_matching_issues


# ── Unauthenticated routes ────────────────────────────────────────


router.include_router(public_routes.session_router)
ui_logout = public_routes.ui_logout


# ── Intervention Queue ─────────────────────────────────────────────


intervention_routes.configure_intervention_routes(
    get_templates=lambda: templates,
    build_context=_ctx,
    sidebar_badge_response=_sidebar_badge_response,
    set_hx_toast=_set_hx_toast,
    sidebar_badge_no_store_headers=_SIDEBAR_BADGE_NO_STORE_HEADERS,
)
router.include_router(intervention_routes.router)

intervention_page = intervention_routes.intervention_page
htmx_intervention_content = intervention_routes.htmx_intervention_content
htmx_intervention_history_detail = intervention_routes.htmx_intervention_history_detail
htmx_intervention_list = intervention_routes.htmx_intervention_list
intervention_selection_ids = intervention_routes.intervention_selection_ids
htmx_intervention_count = intervention_routes.htmx_intervention_count
htmx_intervention_count_text = intervention_routes.htmx_intervention_count_text
htmx_intervention_bulk_approve = intervention_routes.htmx_intervention_bulk_approve
htmx_intervention_bulk_reject = intervention_routes.htmx_intervention_bulk_reject
htmx_intervention_approve = intervention_routes.htmx_intervention_approve
htmx_intervention_reject = intervention_routes.htmx_intervention_reject

_normalize_intervention_tab = intervention_routes.normalize_intervention_tab
_normalize_intervention_confidence_filter = (
    intervention_routes.normalize_intervention_confidence_filter
)
_normalize_intervention_reason_filter = intervention_routes.normalize_intervention_reason_filter
_normalize_intervention_protocol_filter = intervention_routes.normalize_intervention_protocol_filter
_normalize_intervention_outcome_filter = intervention_routes.normalize_intervention_outcome_filter
_normalize_intervention_history_sort = intervention_routes.normalize_intervention_history_sort
_intervention_source_expr = intervention_routes.intervention_source_expr
_intervention_review_reason_codes = intervention_routes.intervention_review_reason_codes
_intervention_review_reason_summary = intervention_routes.intervention_review_reason_summary
_intervention_protocol_label = intervention_routes.intervention_protocol_label
_intervention_outcome_label = intervention_routes.intervention_outcome_label
_intervention_match_type_label = intervention_routes.intervention_match_type_label
_intervention_review_reason_clause = intervention_routes.intervention_review_reason_clause
_intervention_resolved_expr = intervention_routes.intervention_resolved_expr
_get_intervention_history_order_by = intervention_routes.get_intervention_history_order_by
_build_intervention_item_meta = intervention_routes.build_intervention_item_meta
_load_intervention_source_options = intervention_routes.load_intervention_source_options
_build_intervention_queue_filters = intervention_routes.build_intervention_queue_filters
_load_intervention_queue_context = intervention_routes.load_intervention_queue_context
_load_intervention_history_context = intervention_routes.load_intervention_history_context
_load_intervention_context = intervention_routes.load_intervention_context


# ── Search History ──────────────────────────────────────────────


search_history_routes.configure_search_history_routes(
    get_templates=lambda: templates,
    build_context=_ctx,
)
router.include_router(search_history_routes.router)

search_history_page = search_history_routes.search_history_page
_load_search_history_context = search_history_routes.load_search_history_context


whats_new_routes.configure_whats_new_routes(
    get_templates=lambda: templates,
    build_context=_ctx,
)
router.include_router(whats_new_routes.router)

whats_new_page = whats_new_routes.whats_new_page


public_routes.configure_public_routes(
    get_templates=lambda: templates,
    build_context=_ctx,
)
router.include_router(public_routes.router)

login_page = public_routes.login_page
setup_page = public_routes.setup_page
