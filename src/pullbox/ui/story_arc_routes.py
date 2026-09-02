"""Normal-navigation Story Arc management UI routes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.responses import Response

from pullbox.api.deps import AuthenticatedUser, DbSession  # noqa: TC001
from pullbox.config import get_settings
from pullbox.core.story_arc_naming import (
    DEFAULT_STORY_ARC_FILE_TEMPLATE,
    DEFAULT_STORY_ARC_FOLDER_TEMPLATE,
    ORIGINAL_STORY_ARC_FILE_TEMPLATE,
)
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcLifecycle,
    StoryArcSourceKind,
)
from pullbox.services.story_arc_managed_reorder import (
    StoryArcManagedReorderError,
    StoryArcManagedReorderService,
    StoryArcReorderPreview,
)
from pullbox.services.story_arc_placement_integration import (
    StoryArcPlacementIntegrationError,
    StoryArcPlacementPolicyInput,
    StoryArcPlacementPolicyMode,
    StoryArcPlacementSyncService,
    validate_story_arc_placement_policy_input,
)
from pullbox.services.story_arc_service import (
    StoryArcConflictError,
    StoryArcNotFoundError,
    StoryArcService,
    StoryArcServiceError,
    StoryArcValidationError,
)
from pullbox.ui import story_arc_catalog_routes
from pullbox.ui.story_arc_local_issue_search import search_story_arc_local_issues
from pullbox.ui.story_arc_presenters import (
    StoryArcPlacementRootView,
    load_story_arc_detail,
    load_story_arc_list_page,
    load_story_arc_placement_context,
    load_story_arc_placement_roots,
)

router = APIRouter()
router.include_router(story_arc_catalog_routes.router)

_GetTemplates = Callable[[], Jinja2Templates]
_BuildContext = Callable[..., dict[str, object]]

_get_templates: _GetTemplates | None = None
_build_context: _BuildContext | None = None
_story_arc_service = StoryArcService()
_placement_service = StoryArcPlacementSyncService()
_managed_reorder_service = StoryArcManagedReorderService()
_STORY_ARC_VIEW_MODES = {"list", "grid"}


@dataclass(frozen=True, slots=True)
class _StoryArcTemplateUser:
    """Session-independent user fields needed by the shared page shell."""

    username: str


_NOTICE_MESSAGES = {
    "catalog-added": (
        "Story arc added with your reviewed reading order. "
        "Canonical files stay in their series folders."
    ),
    "catalog-refreshed": (
        "Provider changes saved. New members need review; existing order, memberships, "
        "and files were preserved."
    ),
    "search-started": (
        "Missing-issue search started. Check each issue for results and download status."
    ),
    "search-running": "A missing-issue search is already running for this arc.",
    "catalog-placements-started": (
        "Initial arc-file work queued. Refresh to check progress; canonical files stay unchanged."
    ),
    "created": "Story arc created. Add issues when you are ready.",
    "updated": "Story arc monitoring updated.",
    "membership-added": "Issue added to the story arc.",
    "moved-up": "Issue moved up in the reading order.",
    "moved-down": "Issue moved down in the reading order.",
    "already-first": "That issue is already first in the reading order.",
    "already-last": "That issue is already last in the reading order.",
    "resolved": "Story arc entry matched to the canonical issue.",
    "membership-removed": "Issue removed from the story arc. Its canonical record was preserved.",
    "archived": "Story arc archived. Canonical issues and files were preserved.",
    "placement-policy-updated": "Story Arc placement policy saved.",
    "placement-synchronized": "Story Arc placement synchronized.",
    "placement-retried": "Story Arc placement retry completed.",
    "placement-repaired": "Managed Story Arc placement repaired.",
    "placement-managed-removed": (
        "Pullbox-managed Story Arc artifact removed. The canonical library file was preserved."
    ),
    "placement-reference-forgotten": (
        "Story Arc reference forgotten. The user-owned artifact was preserved."
    ),
}
_ERROR_MESSAGES = {
    "conflict": "This story arc changed in another tab. Review the latest state and try again.",
    "not-found": "That story arc entry is no longer available.",
    "validation": "That change could not be saved. Review the fields and try again.",
    "placement": "Placement failed. Review the policy and placement state.",
    "reorder": "That reorder could not be completed. Review the placement state and try again.",
    "reorder-recovery": (
        "This reorder has unfinished managed-file work. Use the same preview below to retry "
        "recovery; canonical and referenced files remain protected."
    ),
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
    story_arc_catalog_routes.configure_story_arc_catalog_routes(
        get_templates=get_templates, build_context=build_context
    )


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
    # All callers use fixed local routes with integer IDs; _detail_url and
    # _list_url encode query values so they cannot replace the destination.
    # codeql[py/url-redirection]
    return RedirectResponse(url=url, status_code=303)


def _detail_url(
    story_arc_id: int,
    *,
    notice: str | None = None,
    error: str | None = None,
    page: int | None = None,
    per_page: int | None = None,
    placement_page: int | None = None,
) -> str:
    params: list[tuple[str, str | int]] = []
    if page is not None:
        params.append(("page", page))
    if per_page is not None:
        params.append(("per_page", per_page))
    if placement_page is not None:
        params.append(("placement_page", placement_page))
    if error is not None:
        params.append(("error", error))
    if notice is not None:
        params.append(("notice", notice))
    query = urlencode(params)
    return f"/story-arcs/{story_arc_id}" + (f"?{query}" if query else "")


def _list_url(*, error: str | None = None) -> str:
    return f"/story-arcs?{urlencode({'error': error})}" if error is not None else "/story-arcs"


def _add_url(*, error: str | None = None) -> str:
    return (
        f"/story-arcs/add?{urlencode({'error': error})}" if error is not None else "/story-arcs/add"
    )


def _error_code(
    exc: StoryArcServiceError | StoryArcPlacementIntegrationError | IntegrityError,
) -> str:
    if isinstance(exc, (StoryArcConflictError, IntegrityError)):
        return "conflict"
    if isinstance(exc, StoryArcNotFoundError):
        return "not-found"
    if isinstance(exc, StoryArcPlacementIntegrationError):
        return "placement"
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


def resolve_story_arc_view(request: Request, view_mode: str | None) -> str:
    """Resolve the active registry view from the query or saved preference."""
    if view_mode in _STORY_ARC_VIEW_MODES:
        return view_mode
    cookie_view = request.cookies.get("story_arc_view")
    return cookie_view if cookie_view in _STORY_ARC_VIEW_MODES else "list"


def _placement_policy_input(
    *,
    mode: str,
    target_library_root_id: str,
    destination_root: str,
    folder_template: str,
    file_template: str,
    symlink_style: str,
    synchronize: bool,
) -> StoryArcPlacementPolicyInput:
    """Normalize browser blanks while keeping the complete policy explicit."""
    try:
        policy_mode = StoryArcPlacementPolicyMode(mode)
    except ValueError as exc:
        raise StoryArcPlacementIntegrationError(
            "unsupported_mode",
            "Unsupported Story Arc placement mode",
        ) from exc
    root_id = _optional_issue_id(target_library_root_id)
    destination = destination_root.strip() or None
    style = symlink_style.strip() or None
    if policy_mode is StoryArcPlacementPolicyMode.LOGICAL:
        root_id = None
        destination = None
        style = None
        synchronize = False
    elif policy_mode is not StoryArcPlacementPolicyMode.SYMLINK:
        style = None
    return StoryArcPlacementPolicyInput(
        mode=policy_mode,
        target_library_root_id=root_id,
        destination_root=destination,
        folder_template=folder_template,
        file_template=file_template,
        symlink_style=style,
        synchronize=synchronize,
    )


async def _render_story_arc_detail(
    *,
    story_arc_id: int,
    request: Request,
    username: str,
    user_id: int,
    session: DbSession,
    page: int,
    per_page: int,
    placement_page: int,
    notice_message: str = "",
    error_message: str = "",
    placement_proposal: StoryArcPlacementPolicyInput | None = None,
    placement_message: str = "",
    reorder_preview: StoryArcReorderPreview | None = None,
) -> Response:
    if reorder_preview is None:
        try:
            reorder_preview = await _managed_reorder_service.load_pending_preview(
                session,
                story_arc_id,
            )
        except StoryArcManagedReorderError:
            # A malformed/incomplete journal is still important recovery truth
            # even when it cannot safely mint a confirmation form.
            error_message = error_message or _ERROR_MESSAGES["reorder-recovery"]
    detail = await load_story_arc_detail(
        session,
        story_arc_id=story_arc_id,
        page=page,
        per_page=per_page,
        user_id=user_id,
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Story arc not found")
    placement_ui = await load_story_arc_placement_context(
        session,
        story_arc_id=story_arc_id,
        page=placement_page,
        proposal=placement_proposal,
    )
    context = _ctx(
        request,
        _StoryArcTemplateUser(username=username),
        story_arc=detail,
        placement_ui=placement_ui,
        pagination_base_url=f"/story-arcs/{story_arc_id}?{urlencode({'per_page': per_page})}",
        placement_pagination_base_url=(
            f"/story-arcs/{story_arc_id}?{urlencode({'page': page, 'per_page': per_page})}"
        ),
        notice_message=notice_message,
        error_message=error_message,
        placement_message=placement_message,
        reorder_preview=reorder_preview,
    )
    return _templates().TemplateResponse(request, "pages/story_arc_detail.html", context)


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


async def _bounded_nested_membership(
    session: DbSession,
    *,
    story_arc_id: int,
    membership_id: int,
) -> IssueStoryArc:
    """Load one membership while rejecting cross-arc nested access."""
    membership = await session.scalar(
        select(IssueStoryArc).where(
            IssueStoryArc.id == membership_id,
            IssueStoryArc.story_arc_id == story_arc_id,
        )
    )
    if membership is None:
        raise HTTPException(status_code=404, detail="Story arc entry not found")
    return membership


@router.get("/story-arcs", response_class=HTMLResponse, include_in_schema=False)
async def story_arc_list(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    q: Annotated[str | None, Query(max_length=500)] = None,
    lifecycle: Annotated[str | None, Query(max_length=20)] = None,
    monitored: Annotated[str | None, Query(max_length=5)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 25,
    view_mode: str | None = Query(None),
    error: str | None = Query(None),
) -> Response:
    """Render a bounded, searchable Story Arc registry."""
    active_view = resolve_story_arc_view(request, view_mode)
    lifecycle_value = (
        StoryArcLifecycle(lifecycle)
        if lifecycle in {item.value for item in StoryArcLifecycle}
        else None
    )
    monitored_value = monitored == "true" if monitored in {"true", "false"} else None
    result = await load_story_arc_list_page(
        session,
        q=q,
        lifecycle=lifecycle_value,
        monitored=monitored_value,
        page=page,
        per_page=per_page,
        user_id=user.id,
    )
    base_params: list[tuple[str, str | int]] = [
        ("per_page", per_page),
        ("view_mode", active_view),
    ]
    if q is not None and q.strip():
        base_params.append(("q", q.strip()))
    if lifecycle_value is not None:
        base_params.append(("lifecycle", lifecycle_value.value))
    if monitored_value is not None:
        base_params.append(("monitored", str(monitored_value).lower()))
    context = _ctx(
        request,
        user,
        story_arc_page=result,
        query=q or "",
        lifecycle_filter=lifecycle_value.value if lifecycle_value is not None else "",
        monitored_filter=(str(monitored_value).lower() if monitored_value is not None else ""),
        active_view=active_view,
        pagination_base_url=f"/story-arcs?{urlencode(base_params)}",
        error_message=_ERROR_MESSAGES.get(error or "", ""),
    )
    template = (
        "partials/story_arc_results_bundle.html"
        if request.headers.get("HX-Request")
        else "pages/story_arcs.html"
    )
    response = _templates().TemplateResponse(request, template, context)
    if view_mode in _STORY_ARC_VIEW_MODES:
        response.set_cookie(
            "story_arc_view",
            active_view,
            max_age=31_536_000,
            path="/",
            samesite="lax",
        )
    return response


@router.get("/story-arcs/add", response_class=HTMLResponse, include_in_schema=False)
async def story_arc_add(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    q: Annotated[str, Query(max_length=500)] = "",
    page: Annotated[int, Query(ge=1, le=100)] = 1,
    error: str | None = Query(None),
) -> Response:
    """Render provider-first Story Arc discovery on its own add page."""
    manual_create_enabled = get_settings().story_arc_manual_create_enabled
    placement_roots: tuple[StoryArcPlacementRootView, ...] = ()
    placement_roots_truncated = False
    if manual_create_enabled:
        placement_roots, placement_roots_truncated = await load_story_arc_placement_roots(
            session, selected_root_id=None
        )
    search_context = await story_arc_catalog_routes.load_story_arc_catalog_search_context(
        session,
        q=q,
        page=page,
        base_url="/story-arcs/add",
    )
    search_context["error_message"] = _ERROR_MESSAGES.get(error or "", "") or str(
        search_context["error_message"]
    )
    context = _ctx(
        request,
        user,
        **search_context,
        placement_roots=placement_roots,
        placement_roots_truncated=placement_roots_truncated,
        story_arc_manual_create_enabled=manual_create_enabled,
    )
    template = (
        "partials/story_arc_add_results_bundle.html"
        if request.headers.get("HX-Request")
        else "pages/story_arc_add.html"
    )
    return _templates().TemplateResponse(request, template, context)


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
    mode: Annotated[str, Form(max_length=50)] = "logical",
    target_library_root_id: str = Form(""),
    destination_root: Annotated[str, Form(max_length=1000)] = "",
    folder_template: Annotated[str, Form(max_length=1024)] = DEFAULT_STORY_ARC_FOLDER_TEMPLATE,
    filename_style: Literal["original", "custom"] = Form("original"),
    prefix_reading_order: bool = Form(False),
    reading_order_width: Annotated[int, Form(ge=2, le=6)] = 2,
    file_template: Annotated[str, Form(max_length=1024)] = DEFAULT_STORY_ARC_FILE_TEMPLATE,
    symlink_style: Annotated[str, Form(max_length=50)] = "",
    synchronize: bool = Form(False),
) -> Response:
    """Create an empty arc with an optional validated, source-preserving storage policy."""
    if not get_settings().story_arc_manual_create_enabled:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        if filename_style == "original":
            file_template = ORIGINAL_STORY_ARC_FILE_TEMPLATE
            if prefix_reading_order:
                file_template = f"{{ReadingOrder:0{reading_order_width}d}} - {file_template}"
        proposal = _placement_policy_input(
            mode=mode,
            target_library_root_id=target_library_root_id,
            destination_root=destination_root,
            folder_template=folder_template,
            file_template=file_template,
            symlink_style=symlink_style,
            synchronize=synchronize,
        )
        # Validate before creating anything, so an invalid destination cannot leave an arc behind.
        await validate_story_arc_placement_policy_input(session, proposal, revision=1)
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
        if proposal.mode is not StoryArcPlacementPolicyMode.LOGICAL:
            await _placement_service.update_policy(
                session, story_arc_id, expected_revision=arc.revision, proposal=proposal
            )
        await session.commit()
    except (StoryArcServiceError, StoryArcPlacementIntegrationError, IntegrityError) as exc:
        await session.rollback()
        return _redirect(request, _add_url(error=_error_code(exc)))
    return _redirect(request, _detail_url(story_arc_id, notice="created"))


@router.get("/story-arcs/{story_arc_id}", response_class=HTMLResponse, include_in_schema=False)
async def story_arc_detail(
    story_arc_id: int,
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 25,
    placement_page: Annotated[int, Query(ge=1)] = 1,
    notice: str | None = Query(None),
    error: str | None = Query(None),
) -> Response:
    """Render arc metadata and one ordered, bounded membership page."""
    return await _render_story_arc_detail(
        story_arc_id=story_arc_id,
        request=request,
        username=user.username,
        user_id=user.id,
        session=session,
        page=page,
        per_page=per_page,
        placement_page=placement_page,
        notice_message=_NOTICE_MESSAGES.get(notice or "", ""),
        error_message=_ERROR_MESSAGES.get(error or "", ""),
    )


@router.post(
    "/story-arcs/{story_arc_id}/placement-policy/preview",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def story_arc_placement_policy_preview(
    story_arc_id: int,
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    expected_revision: Annotated[int, Form(ge=1)],
    mode: Annotated[str, Form(max_length=50)],
    target_library_root_id: str = Form(""),
    destination_root: Annotated[str, Form(max_length=1000)] = "",
    folder_template: Annotated[str, Form(max_length=1024)] = DEFAULT_STORY_ARC_FOLDER_TEMPLATE,
    file_template: Annotated[str, Form(max_length=1024)] = DEFAULT_STORY_ARC_FILE_TEMPLATE,
    symlink_style: Annotated[str, Form(max_length=50)] = "",
    synchronize: bool = Form(False),
) -> Response:
    """Render a bounded candidate preview without persisting policy or files."""
    del expected_revision  # Preview is deliberately write-free; save enforces the revision.
    try:
        proposal = _placement_policy_input(
            mode=mode,
            target_library_root_id=target_library_root_id,
            destination_root=destination_root,
            folder_template=folder_template,
            file_template=file_template,
            symlink_style=symlink_style,
            synchronize=synchronize,
        )
        return await _render_story_arc_detail(
            story_arc_id=story_arc_id,
            request=request,
            username=user.username,
            user_id=user.id,
            session=session,
            page=1,
            per_page=25,
            placement_page=1,
            placement_proposal=proposal,
            placement_message="Preview only — no policy or files were changed.",
        )
    except (StoryArcPlacementIntegrationError, StoryArcValidationError) as exc:
        await session.rollback()
        return await _render_story_arc_detail(
            story_arc_id=story_arc_id,
            request=request,
            username=user.username,
            user_id=user.id,
            session=session,
            page=1,
            per_page=25,
            placement_page=1,
            placement_message=str(exc),
        )


@router.post("/story-arcs/{story_arc_id}/placement-policy", include_in_schema=False)
async def story_arc_placement_policy_update(
    story_arc_id: int,
    request: Request,
    _user: AuthenticatedUser,
    session: DbSession,
    expected_revision: Annotated[int, Form(ge=1)],
    mode: Annotated[str, Form(max_length=50)],
    target_library_root_id: str = Form(""),
    destination_root: Annotated[str, Form(max_length=1000)] = "",
    folder_template: Annotated[str, Form(max_length=1024)] = DEFAULT_STORY_ARC_FOLDER_TEMPLATE,
    file_template: Annotated[str, Form(max_length=1024)] = DEFAULT_STORY_ARC_FILE_TEMPLATE,
    symlink_style: Annotated[str, Form(max_length=50)] = "",
    synchronize: bool = Form(False),
) -> Response:
    """Freeze one complete policy using the arc revision shown by the form."""
    try:
        await _placement_service.update_policy(
            session,
            story_arc_id,
            expected_revision=expected_revision,
            proposal=_placement_policy_input(
                mode=mode,
                target_library_root_id=target_library_root_id,
                destination_root=destination_root,
                folder_template=folder_template,
                file_template=file_template,
                symlink_style=symlink_style,
                synchronize=synchronize,
            ),
        )
    except (StoryArcPlacementIntegrationError, StoryArcValidationError, IntegrityError):
        await session.rollback()
        return _redirect(request, _detail_url(story_arc_id, error="placement"))
    return _redirect(
        request,
        _detail_url(story_arc_id, notice="placement-policy-updated"),
    )


@router.post(
    "/story-arcs/{story_arc_id}/memberships/{membership_id}/placement-sync",
    include_in_schema=False,
)
async def story_arc_placement_sync(
    story_arc_id: int,
    membership_id: int,
    request: Request,
    _user: AuthenticatedUser,
    session: DbSession,
    adopt_identical_existing: bool = Form(False),
) -> Response:
    """Synchronize one preview row without changing its canonical file."""
    try:
        await _placement_service.sync_membership(
            session,
            story_arc_id,
            membership_id,
            adopt_identical_existing=adopt_identical_existing,
        )
    except StoryArcPlacementIntegrationError:
        await session.rollback()
        return _redirect(request, _detail_url(story_arc_id, error="placement"))
    return _redirect(request, _detail_url(story_arc_id, notice="placement-synchronized"))


@router.post(
    "/story-arcs/{story_arc_id}/placements/{placement_id}/retry",
    include_in_schema=False,
)
async def story_arc_placement_retry(
    story_arc_id: int,
    placement_id: int,
    request: Request,
    _user: AuthenticatedUser,
    session: DbSession,
    adopt_identical_existing: bool = Form(False),
) -> Response:
    """Retry one durable placement while preserving referenced ownership."""
    try:
        await _placement_service.retry_placement(
            session,
            story_arc_id,
            placement_id,
            adopt_identical_existing=adopt_identical_existing,
        )
    except StoryArcPlacementIntegrationError:
        await session.rollback()
        return _redirect(request, _detail_url(story_arc_id, error="placement"))
    return _redirect(request, _detail_url(story_arc_id, notice="placement-retried"))


@router.post(
    "/story-arcs/{story_arc_id}/placements/{placement_id}/repair",
    include_in_schema=False,
)
async def story_arc_placement_repair(
    story_arc_id: int,
    placement_id: int,
    request: Request,
    _user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Repair only a Pullbox-managed placement with durable ownership evidence."""
    try:
        await _placement_service.repair_placement(session, story_arc_id, placement_id)
    except StoryArcPlacementIntegrationError:
        await session.rollback()
        return _redirect(request, _detail_url(story_arc_id, error="placement"))
    return _redirect(request, _detail_url(story_arc_id, notice="placement-repaired"))


@router.post(
    "/story-arcs/{story_arc_id}/placements/{placement_id}/remove",
    include_in_schema=False,
)
async def story_arc_placement_remove(
    story_arc_id: int,
    placement_id: int,
    request: Request,
    _user: AuthenticatedUser,
    session: DbSession,
    confirm_managed_artifact_removal: bool = Form(False),
) -> Response:
    """Remove managed artifacts or forget references under ownership safeguards."""
    try:
        removed = await _placement_service.remove_placement(
            session,
            story_arc_id,
            placement_id,
            confirm_managed_artifact_removal=confirm_managed_artifact_removal,
        )
    except StoryArcPlacementIntegrationError:
        await session.rollback()
        return _redirect(request, _detail_url(story_arc_id, error="placement"))
    notice = (
        "placement-reference-forgotten"
        if removed.referenced_artifact_preserved
        else "placement-managed-removed"
    )
    return _redirect(request, _detail_url(story_arc_id, notice=notice))


@router.post("/story-arcs/{story_arc_id}/monitor", include_in_schema=False)
async def story_arc_monitor(
    story_arc_id: int,
    request: Request,
    _user: AuthenticatedUser,
    session: DbSession,
    expected_revision: Annotated[int, Form(ge=1)],
    monitored: bool = Form(False),
) -> Response:
    """Change monitoring without editing metadata, storage, or parent series."""
    try:
        await _story_arc_service.update(
            session,
            story_arc_id,
            expected_revision=expected_revision,
            monitored=monitored,
        )
        await session.commit()
    except (StoryArcServiceError, IntegrityError) as exc:
        await session.rollback()
        return _redirect(
            request,
            _detail_url(story_arc_id, error=_error_code(exc)),
        )
    return _redirect(request, _detail_url(story_arc_id, notice="updated"))


@router.post("/story-arcs/{story_arc_id}/search", include_in_schema=False)
async def story_arc_search(
    story_arc_id: int, request: Request, _user: AuthenticatedUser, session: DbSession
) -> Response:
    """Use the shared acquisition flow, scoped to this arc's eligible canonical issues."""
    from pullbox.tasks.story_arc_search_task import schedule_story_arc_search

    arc = await session.get(StoryArc, story_arc_id)
    if arc is None or arc.lifecycle is not StoryArcLifecycle.ACTIVE:
        raise HTTPException(status_code=404, detail="Active story arc not found")
    await session.commit()
    started = schedule_story_arc_search(story_arc_id)
    return _redirect(
        request, _detail_url(story_arc_id, notice="search-started" if started else "search-running")
    )


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
    user: AuthenticatedUser,
    session: DbSession,
    direction: Annotated[Literal["up", "down"], Form()],
    expected_revision: Annotated[int, Form(ge=1)],
    return_page: Annotated[int | None, Form(ge=1)] = None,
    return_per_page: Annotated[int | None, Form(ge=1, le=100)] = None,
    preview_token: Annotated[str, Form(max_length=200_000)] = "",
    confirm_reorder: bool = Form(False),
) -> Response:
    """Preview, then confirm, one keyboard-accessible reading-order step."""
    try:
        if preview_token:
            if not confirm_reorder:
                raise StoryArcManagedReorderError(
                    "confirmation_required",
                    "Review and explicitly confirm the reorder preview",
                )
            await _managed_reorder_service.confirm_adjacent_move(
                session,
                story_arc_id=story_arc_id,
                membership_id=membership_id,
                direction=direction,
                expected_revision=expected_revision,
                preview_token=preview_token,
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
        preview = await _managed_reorder_service.preview_adjacent_move(
            session,
            story_arc_id,
            membership_id,
            direction=direction,
            expected_revision=expected_revision,
        )
        return await _render_story_arc_detail(
            story_arc_id=story_arc_id,
            request=request,
            username=user.username,
            user_id=user.id,
            session=session,
            page=return_page or 1,
            per_page=return_per_page or 25,
            placement_page=1,
            reorder_preview=preview,
        )
    except StoryArcManagedReorderError as exc:
        await session.rollback()
        if exc.code in {"already_first", "already_last"}:
            return _redirect(
                request,
                _detail_url(
                    story_arc_id,
                    notice=exc.code.replace("_", "-"),
                    page=return_page,
                    per_page=return_per_page,
                ),
            )
        if exc.category == "recovery" and preview_token:
            try:
                recovery_preview = _managed_reorder_service.inspect_preview_token(
                    story_arc_id=story_arc_id,
                    membership_id=membership_id,
                    direction=direction,
                    expected_revision=expected_revision,
                    preview_token=preview_token,
                    recovery_pending=True,
                )
            except StoryArcManagedReorderError:
                recovery_preview = None
            if recovery_preview is not None:
                return await _render_story_arc_detail(
                    story_arc_id=story_arc_id,
                    request=request,
                    username=user.username,
                    user_id=user.id,
                    session=session,
                    page=return_page or 1,
                    per_page=return_per_page or 25,
                    placement_page=1,
                    error_message=_ERROR_MESSAGES["reorder-recovery"],
                    reorder_preview=recovery_preview,
                )
        error_code = (
            "conflict"
            if exc.category == "conflict"
            else "not-found"
            if exc.category == "not_found"
            else "reorder-recovery"
            if exc.category == "recovery"
            else "reorder"
        )
        return _redirect(
            request,
            _detail_url(
                story_arc_id,
                error=error_code,
                page=return_page,
                per_page=return_per_page,
            ),
        )
    except IntegrityError:
        await session.rollback()
        return _redirect(
            request,
            _detail_url(
                story_arc_id,
                error="conflict",
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


@router.get(
    "/story-arcs/{story_arc_id}/memberships/{membership_id}/local-issues",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def story_arc_local_issue_search(
    story_arc_id: int,
    membership_id: int,
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    return_page: Annotated[int, Query(ge=1)] = 1,
    return_per_page: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Response:
    """Return a bounded, provider-free local issue picker for one entry."""
    membership = await _bounded_nested_membership(
        session,
        story_arc_id=story_arc_id,
        membership_id=membership_id,
    )
    result = await search_story_arc_local_issues(
        session,
        query=q,
        source_series_name=membership.source_series_name,
        source_issue_number_text=membership.source_issue_number_text,
    )
    return _templates().TemplateResponse(
        request,
        "partials/story_arc_local_issue_results.html",
        _ctx(
            request,
            user,
            story_arc_id=story_arc_id,
            membership_id=membership_id,
            result=result,
            return_page=return_page,
            return_per_page=return_per_page,
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
