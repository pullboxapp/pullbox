"""System UI routes and tab loading."""

from collections.abc import Callable, Mapping

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from pullbox.api.deps import AuthenticatedUser, DbSession

page_router = APIRouter()
htmx_router = APIRouter()

SYSTEM_TABS: tuple[dict[str, str], ...] = (
    {
        "key": "about",
        "label": "About",
        "description": "Version details, runtime facts, and project links.",
        "icon": "M11.25 11.25l.041-.02a.75.75 0 011.063.852l-.708 2.836a.75.75 0 001.063.853l.041-.021M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9-3.75h.008v.008H12V8.25z",  # noqa: E501
    },
    {
        "key": "backup",
        "label": "Backup",
        "description": "Create restore points and recover safely when needed.",
        "icon": "M20.25 7.5l-.625 10.632a2.25 2.25 0 01-2.247 2.118H6.622a2.25 2.25 0 01-2.247-2.118L3.75 7.5m8.25 3v6.75m0 0l-3-3m3 3l3-3M3.375 7.5h17.25c.621 0 1.125-.504 1.125-1.125v-1.5c0-.621-.504-1.125-1.125-1.125H3.375c-.621 0-1.125.504-1.125 1.125v1.5c0 .621.504 1.125 1.125 1.125z",  # noqa: E501
    },
    {
        "key": "tasks",
        "label": "Tasks",
        "description": "Review schedules, queues, and manual task runs.",
        "icon": "M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z",
    },
    {
        "key": "logs",
        "label": "Log Files",
        "description": "Inspect recent events and collect file-based diagnostics.",
        "icon": "M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z",  # noqa: E501
    },
    {
        "key": "support",
        "label": "Support",
        "description": "Capture evidence and reach the right support channel.",
        "icon": "M9.879 7.519c1.171-1.025 3.071-1.025 4.242 0 1.172 1.025 1.172 2.687 0 3.712-.203.179-.43.326-.67.442-.745.361-1.45.999-1.45 1.827v.75M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9 5.25h.008v.008H12v-.008z",  # noqa: E501
    },
)

_SYSTEM_TABS = ("about", "backup", "tasks", "logs", "support")

_GetTemplates = Callable[[], Jinja2Templates]
_BuildContext = Callable[..., dict[str, object]]

_get_templates: _GetTemplates | None = None
_build_context: _BuildContext | None = None


def configure_system_routes(
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
        msg = "system routes have not been configured with templates"
        raise RuntimeError(msg)
    return _get_templates()


def _ctx(request: Request, user: object | None = None, **kwargs: object) -> dict[str, object]:
    if _build_context is None:
        msg = "system routes have not been configured with a context builder"
        raise RuntimeError(msg)
    context: Mapping[str, object] = _build_context(request, user, **kwargs)
    return dict(context)


async def load_system_tab(
    user: AuthenticatedUser,
    session: DbSession,
    tab: str,
) -> dict[str, object]:
    """Load data needed for a system tab."""
    if tab == "backup":
        from pullbox.config import get_settings
        from pullbox.core.backup_storage import sqlite_backup_path

        return {"sqlite_zip_available": sqlite_backup_path(get_settings().db_url) is not None}
    if tab == "about":
        from pullbox.api.v1.system import get_about as _get_about

        return {"system_about_info": await _get_about(user, session)}
    if tab == "tasks":
        from pullbox.api.v1.system import list_tasks as _list_tasks

        tasks = await _list_tasks(user, session)
        return {"system_tasks": tasks.get("scheduled", [])}
    if tab == "logs":
        from pullbox.api.v1.system import list_log_files as _list_log_files

        log_files = await _list_log_files(user, session)
        return {
            "system_log_files": [
                log_file.model_dump(mode="json") if hasattr(log_file, "model_dump") else log_file
                for log_file in log_files
            ]
        }
    return {}


def _normalize_system_tab(tab: str) -> str:
    if tab not in _SYSTEM_TABS:
        return "about"
    return tab


@page_router.get("/system", response_class=HTMLResponse, include_in_schema=False)
async def system_page(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    tab: str = Query("about"),
) -> Response:
    """Render the system page with tabbed interface."""
    tab = _normalize_system_tab(tab)
    tab_data = await load_system_tab(user, session, tab)
    return _templates().TemplateResponse(
        request,
        "pages/system.html",
        _ctx(request, user, tab=tab, system_tabs=SYSTEM_TABS, **tab_data),
    )


@htmx_router.get("/htmx/system/{tab}", response_class=HTMLResponse, include_in_schema=False)
async def htmx_system_tab(
    request: Request,
    tab: str,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Load a system tab partial via HTMX."""
    tab = _normalize_system_tab(tab)
    tab_data = await load_system_tab(user, session, tab)
    return _templates().TemplateResponse(
        request,
        "partials/system_content_bundle.html",
        _ctx(request, user, tab=tab, system_tabs=SYSTEM_TABS, **tab_data),
    )
