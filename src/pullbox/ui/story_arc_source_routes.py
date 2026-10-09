"""Browser adapters for the shared source-bound Story Arc catalog commands."""

from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Form, Path, Request
from pydantic import Field, ValidationError
from sqlalchemy.exc import IntegrityError
from starlette.responses import Response

from pullbox.api.deps import AuthenticatedUser, DbSession, get_request_session_factory
from pullbox.config import get_settings
from pullbox.core.metadata_identity import MetadataEntityKind, MetadataSource
from pullbox.models.story_arc import StoryArc
from pullbox.schemas.metadata_arc_catalog import (
    ArcCatalogAdd,
    ArcCatalogRefresh,
    ArcCatalogSelection,
)
from pullbox.schemas.metadata_sources import StoryArcPreviewQuery
from pullbox.services.cover_url_service import build_story_arc_cover_url
from pullbox.services.metadata_arc_commands import (
    ArcCommandResult,
    catalog_writer,
    describe_arc_catalog,
    fetch_current_arc_catalog,
    source_arc_add_transaction,
    source_arc_refresh_transaction,
)
from pullbox.services.metadata_sources import read_source_policies
from pullbox.services.provider_artwork import allowed_artwork_url
from pullbox.services.story_arc_catalog_identity import catalog_owners
from pullbox.services.story_arc_catalog_types import StoryArcCatalogError
from pullbox.services.story_arc_file_defaults import load_story_arc_file_defaults
from pullbox.services.story_arc_placement_integration import StoryArcPlacementIntegrationError
from pullbox.services.story_arc_service import StoryArcServiceError
from pullbox.ui.metadata_series_search import SOURCE_LABELS
from pullbox.ui.story_arc_catalog_forms import StoryArcCatalogAddForm
from pullbox.ui.story_arc_catalog_routes import _members, _redirect, _render
from pullbox.ui.story_arc_presenters import load_story_arc_placement_roots

router = APIRouter()
_NativeId = Annotated[str, Path(pattern=r"^[1-9][0-9]{0,254}$")]


class SourceArcAddForm(StoryArcCatalogAddForm):
    source_revision: int = Field(ge=1, lt=2**63)


def command_message(exc: Exception | None = None, *, code: str = "") -> str:
    if isinstance(exc, StoryArcCatalogError):
        if exc.code == "incomplete_membership":
            return "Incomplete member list. Nothing was changed; retry the preview."
        return str(exc)
    messages = {
        "file_defaults_changed": (
            "Story Arc file defaults changed. Review Settings > Media Management, "
            "then preview this arc again."
        ),
        "canonical_root_required": (
            "Choose a library root for new series before saving provider changes."
        ),
        "canonical_root_unavailable": (
            "Restore or enable the saved library root in Settings, then retry provider changes. "
            "Existing series paths and arc storage haven't changed."
        ),
    }
    if code in messages:
        return messages[code]
    return (
        "The arc wasn't changed. Review the current source and file settings, then retry preview."
    )


async def current_selection(
    session: DbSession, source: MetadataSource, identifier: str
) -> ArcCatalogSelection:
    policies = await read_source_policies(session)
    policy = next(row for row in policies if row.source is source)
    if not policy.enabled or policy.revision < 1:
        raise StoryArcCatalogError(
            "source_changed",
            "Enable and save this source in Metadata settings, then retry preview.",
        )
    return ArcCatalogSelection(
        source=source, external_id=identifier, source_revision=policy.revision
    )


def schedule_work(result: ArcCommandResult, request: Request, tasks: BackgroundTasks) -> None:
    if result.search_on_add:
        from pullbox.tasks.story_arc_search_task import schedule_story_arc_search

        schedule_story_arc_search(result.arc.id)
    if result.initial_placements:
        from pullbox.services.story_arc_catalog_placement import run_catalog_initial_placements

        tasks.add_task(
            run_catalog_initial_placements,
            result.arc.id,
            session_factory=get_request_session_factory(request),
        )


@router.get("/story-arcs/catalog/{source}/{provider_id}", include_in_schema=False)
async def source_arc_preview(
    source: MetadataSource,
    provider_id: _NativeId,
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    error: str = "",
) -> Response:
    username = user.username
    preview = None
    message = command_message(code=error) if error else ""
    revision = 0
    try:
        selection = await current_selection(session, source, provider_id)
        revision = selection.source_revision
        owners = await catalog_owners(session, MetadataEntityKind.STORY_ARC, [provider_id], source)
        if provider_id in owners:
            return _redirect(request, f"/story-arcs/{owners[provider_id]}")
        preview = await fetch_current_arc_catalog(
            session, selection, gcd_api_enabled=get_settings().metadata_gcd_api_v2_enabled
        )
        await describe_arc_catalog(session, preview)
    except (StoryArcServiceError, StoryArcPlacementIntegrationError, ValidationError) as exc:
        await session.rollback()
        preview, message = None, command_message(exc)
    roots, truncated = await load_story_arc_placement_roots(session, selected_root_id=None)
    cover = preview.metadata.cover_url if preview else None
    return _render(
        request,
        username,
        "pages/story_arc_catalog_preview.html",
        preview=preview,
        members=_members(preview) if preview else [],
        provider_id=provider_id,
        preview_url=f"/story-arcs/catalog/{source.value}/{provider_id}",
        source_revision=revision,
        source_label=SOURCE_LABELS[source],
        source_key=source.value,
        preview_cover_url=cover if cover and allowed_artwork_url(cover) else "",
        error_message=message,
        placement_roots=roots,
        managed_roots=tuple(r for r in roots if r.can_manage),
        placement_roots_truncated=truncated,
        arc_file_defaults=await load_story_arc_file_defaults(session),
    )


@router.post("/story-arcs/catalog/{source}/{provider_id}", include_in_schema=False)
async def source_arc_add(
    source: MetadataSource,
    provider_id: _NativeId,
    request: Request,
    _user: AuthenticatedUser,
    session: DbSession,
    background_tasks: BackgroundTasks,
    form: Annotated[SourceArcAddForm, Form()],
) -> Response:
    try:
        decision = ArcCatalogAdd(
            source=source,
            external_id=provider_id,
            source_revision=form.source_revision,
            fingerprint=form.fingerprint,
            file_defaults_fingerprint=form.file_defaults_fingerprint,
            ordered_issue_ids=form.reviewed_order(),
            skipped_issue_ids=form.skipped_issue_provider_ids,
            library_root_id=form.library_root_id,
            monitored=form.monitored,
        )
        preview = await fetch_current_arc_catalog(
            session, decision, gcd_api_enabled=get_settings().metadata_gcd_api_v2_enabled
        )
        async with source_arc_add_transaction(session, preview, decision) as result:
            destination = f"/story-arcs/{result.arc.id}?notice=catalog-added"
    except (
        StoryArcServiceError,
        StoryArcPlacementIntegrationError,
        IntegrityError,
        ValidationError,
    ) as exc:
        await session.rollback()
        code = exc.code if isinstance(exc, StoryArcCatalogError) else "review"
        return _redirect(
            request,
            f"/story-arcs/catalog/{source.value}/{provider_id}?{urlencode({'error': code})}",
        )
    schedule_work(result, request, background_tasks)
    return _redirect(request, destination)


async def source_refresh_preview(
    arc: StoryArc,
    selection: StoryArcPreviewQuery,
    request: Request,
    username: str,
    session: DbSession,
    error: str,
) -> Response:
    arc_id, name = arc.id, arc.name
    cover_src = build_story_arc_cover_url(arc)
    resource_url = (
        f"https://comicvine.gamespot.com/story-arc/4045-{selection.external_id}/"
        if selection.source is MetadataSource.COMICVINE_API
        else None
    )
    catalog = arc.diagnostics.get("provider_catalog", {})
    root = catalog.get("canonical_library_root_id") if isinstance(catalog, dict) else None
    preview = changes = None
    message = command_message(code=error) if error else ""
    revision = 0
    try:
        current = await current_selection(session, selection.source, selection.external_id)
        revision = current.source_revision
        preview = await fetch_current_arc_catalog(
            session, current, gcd_api_enabled=get_settings().metadata_gcd_api_v2_enabled
        )
        changes = await catalog_writer(preview).preview_refresh(session, arc_id, preview)
    except (StoryArcServiceError, ValidationError) as exc:
        await session.rollback()
        preview, changes, message = None, None, command_message(exc)
    roots, truncated = await load_story_arc_placement_roots(session, selected_root_id=None)
    return _render(
        request,
        username,
        "pages/story_arc_catalog_refresh.html",
        story_arc_id=arc_id,
        arc_name=name,
        arc_cover_src=cover_src,
        arc_comicvine_url=resource_url,
        source_label=SOURCE_LABELS[selection.source],
        source_revision=revision,
        preview=preview,
        changes=changes,
        error_message=message,
        members=_members(preview) if preview else [],
        needs_library_root=not (type(root) is int and root > 0),
        placement_roots=tuple(r for r in roots if r.can_manage),
        placement_roots_truncated=truncated,
    )


async def source_refresh(
    arc_id: int,
    selection: StoryArcPreviewQuery,
    request: Request,
    session: DbSession,
    *,
    source_revision: int | None,
    expected_revision: int,
    fingerprint: str,
    confirm_refresh: bool,
    library_root_id: int | None,
) -> Response:
    try:
        if not confirm_refresh or source_revision is None:
            raise StoryArcCatalogError("review_required", "Review the source changes first")
        decision = ArcCatalogRefresh(
            **selection.model_dump(),
            source_revision=source_revision,
            expected_revision=expected_revision,
            fingerprint=fingerprint,
            library_root_id=library_root_id,
        )
        preview = await fetch_current_arc_catalog(
            session, decision, gcd_api_enabled=get_settings().metadata_gcd_api_v2_enabled
        )
        async with source_arc_refresh_transaction(session, arc_id, preview, decision) as result:
            destination = f"/story-arcs/{arc_id}?notice=catalog-refreshed"
    except (
        StoryArcServiceError,
        StoryArcPlacementIntegrationError,
        IntegrityError,
        ValidationError,
    ) as exc:
        await session.rollback()
        code = exc.code if isinstance(exc, StoryArcCatalogError) else "review"
        return _redirect(
            request, f"/story-arcs/{arc_id}/catalog-refresh?{urlencode({'error': code})}"
        )
    schedule_work(result, request, BackgroundTasks())
    return _redirect(request, destination)
