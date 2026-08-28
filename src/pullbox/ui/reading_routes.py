"""Reading workspace UI routes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from pullbox.api.deps import AuthenticatedUser, DbSession  # noqa: TC001
from pullbox.config import get_settings
from pullbox.services.reading_query_service import (
    ReadingPage,
    list_continue_reading,
    list_read_issues,
    list_want_to_read,
)
from pullbox.ui.reading_presenters import present_reading_issues

if TYPE_CHECKING:
    from starlette.responses import Response

ReadingView = Literal["continue", "want-to-read", "read"]

router = APIRouter()

_READING_VIEWS: tuple[ReadingView, ...] = ("continue", "want-to-read", "read")
_READING_PAGE_SIZES = (24, 48, 100)
_EMPTY_STATES: dict[ReadingView, tuple[str, str]] = {
    "continue": (
        "Nothing to pick up yet.",
        "Open a downloaded issue and Pullbox will save your place.",
    ),
    "want-to-read": (
        "Your reading queue is clear.",
        "Add a downloaded issue when you want it waiting here.",
    ),
    "read": (
        "No finished comics yet.",
        "Read through the final page or mark an issue read.",
    ),
}

_GetTemplates = Callable[[], Jinja2Templates]
_BuildContext = Callable[..., dict[str, object]]

_get_templates: _GetTemplates | None = None
_build_context: _BuildContext | None = None


def configure_reading_routes(
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
        raise RuntimeError("reading routes have not been configured with templates")
    return _get_templates()


def _ctx(request: Request, user: object | None = None, **kwargs: object) -> dict[str, object]:
    if _build_context is None:
        raise RuntimeError("reading routes have not been configured with a context builder")
    context: Mapping[str, object] = _build_context(request, user, **kwargs)
    return dict(context)


def normalize_reading_view(value: str | None) -> ReadingView:
    """Normalize arbitrary URL input to one supported reading view."""
    if value == "want-to-read":
        return "want-to-read"
    if value == "read":
        return "read"
    return "continue"


def normalize_reading_page_size(value: int) -> int:
    """Normalize page size to the documented Reading choices."""
    return value if value in _READING_PAGE_SIZES else 24


async def load_reading_page(
    session: DbSession,
    *,
    user_id: int,
    view: ReadingView,
    page: int,
    per_page: int,
) -> ReadingPage:
    """Load one bounded Reading view through the shared query service."""
    if view == "want-to-read":
        return await list_want_to_read(
            session,
            user_id=user_id,
            page=page,
            per_page=per_page,
        )
    if view == "read":
        return await list_read_issues(
            session,
            user_id=user_id,
            page=page,
            per_page=per_page,
        )
    return await list_continue_reading(
        session,
        user_id=user_id,
        page=page,
        per_page=per_page,
    )


@router.get("/reading", response_class=HTMLResponse, include_in_schema=False)
async def reading_workspace(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    view: str | None = Query(None),
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 24,
) -> Response:
    """Render one private, URL-addressable Reading workspace view."""
    if not get_settings().reader_enabled:
        raise HTTPException(status_code=404, detail="Not Found")
    view_value = normalize_reading_view(view)
    page_size = normalize_reading_page_size(per_page)
    result = await load_reading_page(
        session,
        user_id=user.id,
        view=view_value,
        page=page,
        per_page=page_size,
    )
    total_pages = max(result.page_count, 1)
    if result.page > total_pages:
        result = await load_reading_page(
            session,
            user_id=user.id,
            view=view_value,
            page=total_pages,
            per_page=page_size,
        )
    cards = present_reading_issues(result.items, view=view_value)
    total_pages = max(result.page_count, 1)
    base_url = "/reading?" + urlencode(
        {
            "view": view_value,
            "per_page": page_size,
        }
    )
    empty_title, empty_copy = _EMPTY_STATES[view_value]
    context = _ctx(
        request,
        user,
        reading_cards=cards,
        reading_view=view_value,
        page=result.page,
        per_page=page_size,
        total=result.total,
        total_pages=total_pages,
        pagination_base_url=base_url,
        empty_title=empty_title,
        empty_copy=empty_copy,
    )
    if request.headers.get("HX-Request"):
        return _templates().TemplateResponse(
            request,
            "partials/reading_content_bundle.html",
            context,
        )
    return _templates().TemplateResponse(
        request,
        "pages/reading.html",
        context,
    )
