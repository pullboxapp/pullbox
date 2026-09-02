"""Preview-first Comic Vine arc discovery and explicit membership refresh."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated
from urllib.parse import urlencode

import structlog
from fastapi import APIRouter, BackgroundTasks, Form, HTTPException, Path, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.responses import Response

from pullbox.api.deps import AuthenticatedUser, DbSession  # noqa: TC001
from pullbox.core.issue_numbers import format_issue_number
from pullbox.core.library_policy import load_search_on_add_default
from pullbox.models.story_arc import StoryArc, StoryArcLifecycle
from pullbox.providers.metadata.comicvine import ComicVineError
from pullbox.services.story_arc_placement_integration import StoryArcPlacementIntegrationError
from pullbox.services.story_arc_service import StoryArcServiceError, StoryArcValidationError
from pullbox.ui.story_arc_catalog_forms import StoryArcCatalogAddForm  # noqa: TC001
from pullbox.ui.story_arc_presenters import load_story_arc_placement_roots

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from fastapi.templating import Jinja2Templates

    from pullbox.services.story_arc_catalog import (
        StoryArcCatalogPreview,
        StoryArcCatalogService,
    )

logger = structlog.get_logger(__name__)
router = APIRouter()
_get_templates: Callable[[], Jinja2Templates] | None = None
_build_context: Callable[..., dict[str, object]] | None = None
_ProviderId = Annotated[str, Path(pattern=r"^[1-9][0-9]{0,18}$")]
_ERRORS = {
    "provider": "Comic Vine couldn't load this arc. Check the provider settings and retry preview.",
    "validation": (
        "The arc wasn't added. Review the reading order and storage choices, then confirm again."
    ),
    "stale": (
        "The provider membership changed after preview. Review the latest list before confirming."
    ),
    "conflict": "This arc changed in another tab. Review the latest changes before confirming.",
    "catalog_limit_exceeded": (
        "This arc exceeds the supported review limit. Nothing was added or changed."
    ),
    "incomplete_hydration": (
        "Some member details couldn't be loaded. Nothing was added or changed; retry preview."
    ),
    "canonical_root_required": (
        "Choose a library root for new series before saving provider changes."
    ),
    "canonical_root_unavailable": (
        "Restore or enable the saved library root in Settings, then retry provider changes. "
        "Existing series paths and arc storage haven't changed."
    ),
}


@dataclass(frozen=True)
class _TemplateUser:
    username: str


def configure_story_arc_catalog_routes(
    *, get_templates: Callable[[], Jinja2Templates], build_context: Callable[..., dict[str, object]]
) -> None:
    global _get_templates, _build_context
    _get_templates, _build_context = get_templates, build_context


def _render(request: Request, username: str, template: str, **values: object) -> Response:
    if _get_templates is None or _build_context is None:
        raise RuntimeError("Story Arc catalog routes are not configured")
    context = _build_context(request, _TemplateUser(username), **values)
    return _get_templates().TemplateResponse(request, template, context)


def _redirect(request: Request, url: str) -> Response:
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": url})
    # All callers build /story-arcs routes from integer IDs or the numeric
    # _ProviderId validator; no request value supplies an origin or URL prefix.
    # codeql[py/url-redirection]
    return RedirectResponse(url, status_code=303)


@asynccontextmanager
async def _catalog_service(session: DbSession) -> AsyncIterator[StoryArcCatalogService]:
    from pullbox.core.comicvine_key import get_comicvine_api_key
    from pullbox.providers.metadata.comicvine import ComicVineProvider
    from pullbox.services.story_arc_catalog import StoryArcCatalogService

    api_key = await get_comicvine_api_key(session)
    # Authentication/config reads must not keep a transaction open during provider I/O.
    await session.rollback()
    if not api_key:
        raise StoryArcValidationError("Comic Vine is not configured")
    provider = ComicVineProvider(api_key=api_key)
    try:
        yield StoryArcCatalogService(provider)
    finally:
        await provider.close()


def _failure_code(exc: Exception) -> str:
    code = getattr(exc, "code", "")
    if code in ("catalog_limit_exceeded", "incomplete_hydration"):
        return str(code)
    return "provider"


async def _existing(session: DbSession, provider_id: str) -> int | None:
    if int(provider_id) > 2**63 - 1:
        raise HTTPException(status_code=404, detail="Story Arc provider identity not found")
    existing_id = await session.scalar(
        select(StoryArc.id).where(StoryArc.comicvine_id == int(provider_id))
    )
    return int(existing_id) if existing_id is not None else None


async def load_story_arc_catalog_search_context(
    session: DbSession,
    *,
    q: str,
    page: int,
    base_url: str,
) -> dict[str, object]:
    """Load one bounded Comic Vine arc result page for full or HTMX shells."""
    query = q.strip()
    results: list[dict[str, object]] = []
    total = 0
    error = ""
    if len(query) >= 2:
        try:
            async with _catalog_service(session) as service:
                found, total = await service.search(query, limit=20, offset=(page - 1) * 20)
                existing = await service.find_existing(
                    session, [item.provider_id for item in found]
                )
                results = [
                    {"metadata": item, "existing_id": existing.get(item.provider_id)}
                    for item in found
                ]
        except (ComicVineError, StoryArcServiceError):
            await session.rollback()
            error = "Comic Vine search failed. Check the provider settings and try again."
            logger.warning("story_arc_catalog_search_failed")

    total_pages = max(1, (total + 19) // 20)
    return {
        "query": query,
        "catalog_query": query,
        "results": results,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "shown_count": len(results),
        "in_library_count": sum(1 for result in results if result["existing_id"]),
        "error_message": error,
        "pagination_base_url": (f"{base_url}?{urlencode({'q': query})}" if query else base_url),
        "next_url": (
            base_url + "?" + urlencode({"q": query, "page": page + 1}) if page * 20 < total else ""
        ),
        "previous_url": (
            base_url + "?" + urlencode({"q": query, "page": page - 1}) if page > 1 else ""
        ),
    }


def _members(preview: StoryArcCatalogPreview) -> list[dict[str, str]]:
    titles = {series.provider_id: series.title for series in preview.series}
    return [
        {
            "provider_id": issue.provider_id,
            "series_name": titles.get(
                issue.series_provider_id, f"Series {issue.series_provider_id}"
            ),
            "issue_number": issue.issue_number_text or format_issue_number(issue.issue_number),
            "title": issue.title or "Untitled issue",
        }
        for issue in preview.issues
    ]


@router.get("/story-arcs/catalog", include_in_schema=False)
async def story_arc_catalog_search(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    q: Annotated[str, Query(max_length=500)] = "",
    page: Annotated[int, Query(ge=1, le=100)] = 1,
) -> Response:
    username = user.username
    context = await load_story_arc_catalog_search_context(
        session,
        q=q,
        page=page,
        base_url="/story-arcs/catalog",
    )
    return _render(
        request,
        username,
        "partials/story_arc_catalog_results.html"
        if request.headers.get("HX-Request")
        else "pages/story_arc_catalog.html",
        **context,
    )


@router.get("/story-arcs/catalog/{provider_id}", include_in_schema=False)
async def story_arc_catalog_preview(
    provider_id: _ProviderId,
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    error: str = Query(""),
) -> Response:
    username = user.username
    existing_id = await _existing(session, provider_id)
    if existing_id is not None:
        return _redirect(request, f"/story-arcs/{existing_id}")
    preview = None
    message = _ERRORS.get(error, "")
    try:
        async with _catalog_service(session) as service:
            preview = await service.preview(provider_id)
    except (ComicVineError, StoryArcServiceError) as exc:
        await session.rollback()
        message = _ERRORS[_failure_code(exc)]
        logger.warning("story_arc_catalog_preview_failed", category=_failure_code(exc))
    roots, truncated = await load_story_arc_placement_roots(session, selected_root_id=None)
    managed_roots = tuple(root for root in roots if root.can_manage)
    return _render(
        request,
        username,
        "pages/story_arc_catalog_preview.html",
        preview=preview,
        members=_members(preview) if preview else [],
        provider_id=provider_id,
        error_message=message,
        placement_roots=roots,
        managed_roots=managed_roots,
        placement_roots_truncated=truncated,
    )


@router.post("/story-arcs/catalog/{provider_id}", include_in_schema=False)
async def story_arc_catalog_add(
    provider_id: _ProviderId,
    request: Request,
    _user: AuthenticatedUser,
    session: DbSession,
    background_tasks: BackgroundTasks,
    form: Annotated[StoryArcCatalogAddForm, Form()],
) -> Response:
    existing_id = await _existing(session, provider_id)
    if existing_id is not None:
        return _redirect(request, f"/story-arcs/{existing_id}")
    code = "validation"
    try:
        order = form.reviewed_order()
        policy = form.placement_policy()
        async with _catalog_service(session) as service:
            preview = await service.preview(provider_id)
            if preview.fingerprint != form.fingerprint:
                code = "stale"
                raise StoryArcValidationError("Preview changed")
        arc = await service.add(
            session,
            preview,
            ordered_issue_provider_ids=order,
            skipped_issue_provider_ids=form.skipped_issue_provider_ids,
            library_root_id=form.library_root_id,
            monitored=form.monitored,
            search_missing=form.monitored,
            include_upcoming=form.monitored,
            placement_policy=policy,
        )
        arc_id = arc.id
        initial_pending = _has_initial_work(arc)
        search_on_add = arc.monitored and await load_search_on_add_default(session)
        await session.commit()
    except (StoryArcServiceError, StoryArcPlacementIntegrationError, IntegrityError):
        await session.rollback()
        return _redirect(request, f"/story-arcs/catalog/{provider_id}?error={code}")
    except ComicVineError:
        await session.rollback()
        return _redirect(request, f"/story-arcs/catalog/{provider_id}?error=provider")
    if search_on_add:
        from pullbox.tasks.story_arc_search_task import schedule_story_arc_search

        schedule_story_arc_search(arc_id)
    if initial_pending:
        from pullbox.services.story_arc_catalog_placement import run_catalog_initial_placements

        background_tasks.add_task(
            run_catalog_initial_placements,
            arc_id,
            session_factory=request.app.state.db_session_factory,
        )
    return _redirect(request, f"/story-arcs/{arc_id}?notice=catalog-added")


async def _provider_arc(session: DbSession, arc_id: int) -> StoryArc:
    arc = await session.get(StoryArc, arc_id)
    if arc is None or arc.comicvine_id is None or arc.lifecycle is not StoryArcLifecycle.ACTIVE:
        raise HTTPException(status_code=404, detail="Active provider Story Arc not found")
    return arc


def _has_initial_work(arc: StoryArc) -> bool:
    marker = (arc.diagnostics or {}).get("catalog_initial_placements")
    if not isinstance(marker, dict):
        return False
    return any(type(value := marker.get(key)) is int and value > 0 for key in ("pending", "failed"))


@router.post("/story-arcs/{story_arc_id}/initial-placements/retry", include_in_schema=False)
async def story_arc_catalog_initial_placements_retry(
    story_arc_id: int,
    request: Request,
    _user: AuthenticatedUser,
    session: DbSession,
    background_tasks: BackgroundTasks,
) -> Response:
    """Resume only the frozen creation work, never migrate an established policy."""
    from pullbox.services.story_arc_catalog_placement import run_catalog_initial_placements

    arc = await _provider_arc(session, story_arc_id)
    if not _has_initial_work(arc):
        return _redirect(request, f"/story-arcs/{story_arc_id}")
    await session.commit()
    background_tasks.add_task(
        run_catalog_initial_placements,
        story_arc_id,
        retry_failed=True,
        session_factory=request.app.state.db_session_factory,
    )
    return _redirect(request, f"/story-arcs/{story_arc_id}?notice=catalog-placements-started")


@router.get("/story-arcs/{story_arc_id}/catalog-refresh", include_in_schema=False)
async def story_arc_catalog_refresh_preview(
    story_arc_id: int,
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    error: str = Query(""),
) -> Response:
    username = user.username
    arc = await _provider_arc(session, story_arc_id)
    name, provider_id = arc.name, str(arc.comicvine_id)
    catalog = (arc.diagnostics or {}).get("provider_catalog")
    saved_root = catalog.get("canonical_library_root_id") if isinstance(catalog, dict) else None
    needs_library_root = not (type(saved_root) is int and saved_root > 0)
    preview = None
    changes = None
    message = _ERRORS.get(error, "")
    try:
        async with _catalog_service(session) as service:
            preview = await service.preview(provider_id)
            if preview.membership_complete:
                changes = await service.preview_refresh(session, story_arc_id, preview)
    except (ComicVineError, StoryArcServiceError) as exc:
        await session.rollback()
        message = _ERRORS[_failure_code(exc)]
    roots, truncated = await load_story_arc_placement_roots(session, selected_root_id=None)
    managed_roots = tuple(root for root in roots if root.can_manage)
    return _render(
        request,
        username,
        "pages/story_arc_catalog_refresh.html",
        story_arc_id=story_arc_id,
        arc_name=name,
        preview=preview,
        changes=changes,
        error_message=message,
        members=_members(preview) if preview else [],
        needs_library_root=needs_library_root,
        placement_roots=managed_roots,
        placement_roots_truncated=truncated,
    )


@router.post("/story-arcs/{story_arc_id}/catalog-refresh", include_in_schema=False)
async def story_arc_catalog_refresh(
    story_arc_id: int,
    request: Request,
    _user: AuthenticatedUser,
    session: DbSession,
    expected_revision: Annotated[int, Form(ge=1)],
    fingerprint: Annotated[str, Form(max_length=128)],
    confirm_refresh: bool = Form(False),
    library_root_id: Annotated[int | None, Form(ge=1)] = None,
) -> Response:
    arc = await _provider_arc(session, story_arc_id)
    provider_id = str(arc.comicvine_id)
    code = "conflict"
    try:
        if not confirm_refresh:
            raise StoryArcValidationError("Confirm the refresh preview")
        async with _catalog_service(session) as service:
            preview = await service.preview(provider_id)
            if preview.fingerprint != fingerprint:
                code = "stale"
                raise StoryArcValidationError("Preview changed")
        result = await service.refresh(
            session,
            story_arc_id,
            preview,
            expected_revision=expected_revision,
            library_root_id=library_root_id,
        )
        search_on_add = result.story_arc.monitored and await load_search_on_add_default(session)
        await session.commit()
    except (StoryArcServiceError, IntegrityError) as exc:
        await session.rollback()
        root_error = getattr(exc, "code", "")
        if root_error in {"canonical_root_required", "canonical_root_unavailable"}:
            code = root_error
        return _redirect(request, f"/story-arcs/{story_arc_id}/catalog-refresh?error={code}")
    except ComicVineError:
        await session.rollback()
        return _redirect(request, f"/story-arcs/{story_arc_id}/catalog-refresh?error=provider")
    if search_on_add:
        from pullbox.tasks.story_arc_search_task import schedule_story_arc_search

        schedule_story_arc_search(story_arc_id)
    return _redirect(request, f"/story-arcs/{story_arc_id}?notice=catalog-refreshed")
