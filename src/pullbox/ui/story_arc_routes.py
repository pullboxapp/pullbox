"""Normal-navigation Story Arc management UI routes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from starlette.responses import Response

from pullbox.api.deps import AuthenticatedUser, DbSession  # noqa: TC001
from pullbox.models.story_arc import StoryArc, StoryArcLifecycle, StoryArcSourceKind
from pullbox.services.story_arc_service import (
    StoryArcConflictError,
    StoryArcNotFoundError,
    StoryArcService,
    StoryArcServiceError,
    StoryArcValidationError,
)
from pullbox.ui.story_arc_presenters import (
    load_story_arc_detail,
    load_story_arc_list_page,
)

if TYPE_CHECKING:
    from pullbox.models.story_arc import IssueStoryArc

router = APIRouter()

_GetTemplates = Callable[[], Jinja2Templates]
_BuildContext = Callable[..., dict[str, object]]

_get_templates: _GetTemplates | None = None
_build_context: _BuildContext | None = None
_story_arc_service = StoryArcService()

_NOTICE_MESSAGES = {
    "created": "Story arc created. Add issues when you are ready.",
    "updated": "Story arc settings saved.",
    "membership-added": "Issue added to the story arc.",
    "moved-up": "Issue moved up in the reading order.",
    "moved-down": "Issue moved down in the reading order.",
    "already-first": "That issue is already first in the reading order.",
    "already-last": "That issue is already last in the reading order.",
    "resolved": "Story arc entry matched to the canonical issue.",
    "membership-removed": "Issue removed from the story arc. Its canonical record was preserved.",
    "archived": "Story arc archived. Canonical issues and files were preserved.",
}
_ERROR_MESSAGES = {
    "conflict": "This story arc changed in another tab. Review the latest state and try again.",
    "not-found": "That story arc entry is no longer available.",
    "validation": "That change could not be saved. Review the fields and try again.",
}


def configure_story_arc_routes(
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
        raise RuntimeError("story arc routes have not been configured with templates")
    return _get_templates()


def _ctx(request: Request, user: object | None = None, **kwargs: object) -> dict[str, object]:
    if _build_context is None:
        raise RuntimeError("story arc routes have not been configured with a context builder")
    context: Mapping[str, object] = _build_context(request, user, **kwargs)
    return dict(context)


def _redirect(request: Request, url: str) -> Response:
    """Redirect both progressive-enhancement and plain form submissions."""
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url=url, status_code=303)


def _detail_url(
    story_arc_id: int,
    *,
    notice: str | None = None,
    error: str | None = None,
    page: int | None = None,
    per_page: int | None = None,
) -> str:
    params: list[tuple[str, str | int]] = []
    if page is not None:
        params.append(("page", page))
    if per_page is not None:
        params.append(("per_page", per_page))
    if error is not None:
        params.append(("error", error))
    if notice is not None:
        params.append(("notice", notice))
    query = urlencode(params)
    return f"/story-arcs/{story_arc_id}" + (f"?{query}" if query else "")


def _list_url(*, error: str | None = None) -> str:
    return f"/story-arcs?{urlencode({'error': error})}" if error is not None else "/story-arcs"


def _error_code(exc: StoryArcServiceError | IntegrityError) -> str:
    if isinstance(exc, (StoryArcConflictError, IntegrityError)):
        return "conflict"
    if isinstance(exc, StoryArcNotFoundError):
        return "not-found"
    return "validation"


def _optional_issue_id(value: str) -> int | None:
    """Parse a browser's blank optional number field without turning it into a 422."""
    text = value.strip()
    if not text:
        return None
    try:
        issue_id = int(text)
    except ValueError as exc:
        raise StoryArcValidationError("Issue ID must be a positive integer") from exc
    if issue_id < 1:
        raise StoryArcValidationError("Issue ID must be a positive integer")
    return issue_id


async def _nested_memberships(
    session: DbSession,
    *,
    story_arc_id: int,
    membership_id: int,
) -> tuple[list[IssueStoryArc], int]:
    """Load stable order and reject cross-arc nested mutations."""
    memberships = await _story_arc_service.list_memberships(session, story_arc_id)
    for index, membership in enumerate(memberships):
        if membership.id == membership_id:
            return memberships, index
    raise StoryArcNotFoundError(f"Story-arc membership {membership_id} was not found")


@router.get("/story-arcs", response_class=HTMLResponse, include_in_schema=False)
async def story_arc_list(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    q: Annotated[str | None, Query(max_length=500)] = None,
    lifecycle: Annotated[StoryArcLifecycle | None, Query()] = None,
    monitored: Annotated[bool | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 25,
    error: str | None = Query(None),
) -> Response:
    """Render a bounded, searchable Story Arc registry."""
    result = await load_story_arc_list_page(
        session,
        q=q,
        lifecycle=lifecycle,
        monitored=monitored,
        page=page,
        per_page=per_page,
    )
    base_params: list[tuple[str, str | int]] = [("per_page", per_page)]
    if q is not None and q.strip():
        base_params.append(("q", q.strip()))
    if lifecycle is not None:
        base_params.append(("lifecycle", lifecycle.value))
    if monitored is not None:
        base_params.append(("monitored", str(monitored).lower()))
    context = _ctx(
        request,
        user,
        story_arc_page=result,
        query=q or "",
        lifecycle_filter=lifecycle.value if lifecycle is not None else "",
        monitored_filter=(str(monitored).lower() if monitored is not None else ""),
        pagination_base_url=f"/story-arcs?{urlencode(base_params)}",
        error_message=_ERROR_MESSAGES.get(error or "", ""),
    )
    return _templates().TemplateResponse(request, "pages/story_arcs.html", context)


@router.post("/story-arcs", include_in_schema=False)
async def story_arc_create(
    request: Request,
    _user: AuthenticatedUser,
    session: DbSession,
    name: str = Form(""),
    description: str = Form(""),
    monitored: bool = Form(False),
    search_missing: bool = Form(False),
    include_upcoming: bool = Form(False),
) -> Response:
    """Create and commit one empty Pullbox-owned Story Arc."""
    try:
        arc = await _story_arc_service.create(
            session,
            name=name,
            description=description.strip() or None,
            monitored=monitored,
            search_missing=search_missing,
            include_upcoming=include_upcoming,
            source_kind=StoryArcSourceKind.PULLBOX,
        )
        story_arc_id = arc.id
        await session.commit()
    except (StoryArcServiceError, IntegrityError) as exc:
        await session.rollback()
        return _redirect(request, _list_url(error=_error_code(exc)))
    return _redirect(request, _detail_url(story_arc_id, notice="created"))


@router.get("/story-arcs/{story_arc_id}", response_class=HTMLResponse, include_in_schema=False)
async def story_arc_detail(
    story_arc_id: int,
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 25,
    notice: str | None = Query(None),
    error: str | None = Query(None),
) -> Response:
    """Render arc metadata and one ordered, bounded membership page."""
    detail = await load_story_arc_detail(
        session,
        story_arc_id=story_arc_id,
        page=page,
        per_page=per_page,
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Story arc not found")
    context = _ctx(
        request,
        user,
        story_arc=detail,
        pagination_base_url=f"/story-arcs/{story_arc_id}?{urlencode({'per_page': per_page})}",
        notice_message=_NOTICE_MESSAGES.get(notice or "", ""),
        error_message=_ERROR_MESSAGES.get(error or "", ""),
    )
    return _templates().TemplateResponse(request, "pages/story_arc_detail.html", context)


@router.post("/story-arcs/{story_arc_id}/edit", include_in_schema=False)
async def story_arc_edit(
    story_arc_id: int,
    request: Request,
    _user: AuthenticatedUser,
    session: DbSession,
    expected_revision: Annotated[int, Form(ge=1)],
    name: str = Form(""),
    description: str = Form(""),
    monitored: bool = Form(False),
    search_missing: bool = Form(False),
    include_upcoming: bool = Form(False),
) -> Response:
    """Save metadata and monitoring controls under optimistic locking."""
    try:
        existing = await session.get(StoryArc, story_arc_id)
        if existing is None:
            raise StoryArcNotFoundError(f"Story arc {story_arc_id} was not found")
        await _story_arc_service.update(
            session,
            story_arc_id,
            expected_revision=expected_revision,
            name=name,
            description=description.strip() or None,
            monitored=monitored,
            search_missing=search_missing,
            include_upcoming=include_upcoming,
            sync_enabled=existing.sync_enabled,
        )
        await session.commit()
    except (StoryArcServiceError, IntegrityError) as exc:
        await session.rollback()
        return _redirect(
            request,
            _detail_url(story_arc_id, error=_error_code(exc)),
        )
    return _redirect(request, _detail_url(story_arc_id, notice="updated"))


@router.post("/story-arcs/{story_arc_id}/memberships", include_in_schema=False)
async def story_arc_add_membership(
    story_arc_id: int,
    request: Request,
    _user: AuthenticatedUser,
    session: DbSession,
    sequence_number: Annotated[int, Form(ge=0)],
    issue_id: str = Form(""),
    source_issue_number_text: Annotated[str | None, Form(max_length=320)] = None,
) -> Response:
    """Add one resolved or unresolved logical membership."""
    try:
        await _story_arc_service.add_membership(
            session,
            story_arc_id,
            issue_id=_optional_issue_id(issue_id),
            sequence_number=sequence_number,
            source_issue_number_text=(
                source_issue_number_text.strip() if source_issue_number_text else None
            ),
            source_kind=StoryArcSourceKind.PULLBOX,
        )
        await session.commit()
    except (StoryArcServiceError, IntegrityError) as exc:
        await session.rollback()
        return _redirect(request, _detail_url(story_arc_id, error=_error_code(exc)))
    return _redirect(request, _detail_url(story_arc_id, notice="membership-added"))


@router.post(
    "/story-arcs/{story_arc_id}/memberships/{membership_id}/move",
    include_in_schema=False,
)
async def story_arc_move_membership(
    story_arc_id: int,
    membership_id: int,
    request: Request,
    _user: AuthenticatedUser,
    session: DbSession,
    direction: Annotated[Literal["up", "down"], Form()],
    expected_revision: Annotated[int, Form(ge=1)],
    return_page: Annotated[int | None, Form(ge=1)] = None,
    return_per_page: Annotated[int | None, Form(ge=1, le=100)] = None,
) -> Response:
    """Move one entry by one keyboard-accessible reading-order step."""
    try:
        memberships, index = await _nested_memberships(
            session,
            story_arc_id=story_arc_id,
            membership_id=membership_id,
        )
        target_index = index - 1 if direction == "up" else index + 1
        if target_index < 0 or target_index >= len(memberships):
            edge_notice = "already-first" if direction == "up" else "already-last"
            return _redirect(
                request,
                _detail_url(
                    story_arc_id,
                    notice=edge_notice,
                    page=return_page,
                    per_page=return_per_page,
                ),
            )
        ordered_ids = [membership.id for membership in memberships]
        ordered_ids[index], ordered_ids[target_index] = (
            ordered_ids[target_index],
            ordered_ids[index],
        )
        await _story_arc_service.reorder_memberships(
            session,
            story_arc_id,
            ordered_membership_ids=ordered_ids,
            expected_revision=expected_revision,
        )
        await session.commit()
    except (StoryArcServiceError, IntegrityError) as exc:
        await session.rollback()
        return _redirect(
            request,
            _detail_url(
                story_arc_id,
                error=_error_code(exc),
                page=return_page,
                per_page=return_per_page,
            ),
        )
    return _redirect(
        request,
        _detail_url(
            story_arc_id,
            notice=f"moved-{direction}",
            page=return_page,
            per_page=return_per_page,
        ),
    )


@router.post(
    "/story-arcs/{story_arc_id}/memberships/{membership_id}/resolve",
    include_in_schema=False,
)
async def story_arc_resolve_membership(
    story_arc_id: int,
    membership_id: int,
    request: Request,
    _user: AuthenticatedUser,
    session: DbSession,
    issue_id: Annotated[int, Form(ge=1)],
    return_page: Annotated[int | None, Form(ge=1)] = None,
    return_per_page: Annotated[int | None, Form(ge=1, le=100)] = None,
) -> Response:
    """Resolve one entry to an existing canonical issue record."""
    try:
        await _nested_memberships(
            session,
            story_arc_id=story_arc_id,
            membership_id=membership_id,
        )
        await _story_arc_service.resolve_membership(session, membership_id, issue_id=issue_id)
        await session.commit()
    except (StoryArcServiceError, IntegrityError) as exc:
        await session.rollback()
        return _redirect(
            request,
            _detail_url(
                story_arc_id,
                error=_error_code(exc),
                page=return_page,
                per_page=return_per_page,
            ),
        )
    return _redirect(
        request,
        _detail_url(
            story_arc_id,
            notice="resolved",
            page=return_page,
            per_page=return_per_page,
        ),
    )


@router.post(
    "/story-arcs/{story_arc_id}/memberships/{membership_id}/remove",
    include_in_schema=False,
)
async def story_arc_remove_membership(
    story_arc_id: int,
    membership_id: int,
    request: Request,
    _user: AuthenticatedUser,
    session: DbSession,
    return_page: Annotated[int | None, Form(ge=1)] = None,
    return_per_page: Annotated[int | None, Form(ge=1, le=100)] = None,
) -> Response:
    """Remove an association while preserving its canonical issue and file."""
    try:
        await _nested_memberships(
            session,
            story_arc_id=story_arc_id,
            membership_id=membership_id,
        )
        await _story_arc_service.remove_membership(session, membership_id)
        await session.commit()
    except (StoryArcServiceError, IntegrityError) as exc:
        await session.rollback()
        return _redirect(
            request,
            _detail_url(
                story_arc_id,
                error=_error_code(exc),
                page=return_page,
                per_page=return_per_page,
            ),
        )
    return _redirect(
        request,
        _detail_url(
            story_arc_id,
            notice="membership-removed",
            page=return_page,
            per_page=return_per_page,
        ),
    )


@router.post("/story-arcs/{story_arc_id}/archive", include_in_schema=False)
async def story_arc_archive(
    story_arc_id: int,
    request: Request,
    _user: AuthenticatedUser,
    session: DbSession,
    expected_revision: Annotated[int, Form(ge=1)],
) -> Response:
    """Soft-archive an arc without deleting canonical issues, files, or memberships."""
    try:
        await _story_arc_service.archive(
            session,
            story_arc_id,
            expected_revision=expected_revision,
        )
        await session.commit()
    except (StoryArcServiceError, IntegrityError) as exc:
        await session.rollback()
        return _redirect(request, _detail_url(story_arc_id, error=_error_code(exc)))
    return _redirect(request, _detail_url(story_arc_id, notice="archived"))
