"""Private manifest and revisioned page endpoints for the embedded reader."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Never

import anyio
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from pullbox.api.deps import (
    AuthenticatedStreamUser,
    InteractiveOperatorUser,
    get_request_session_factory,
)
from pullbox.config import get_settings
from pullbox.core.events import (
    ReaderCompletionChanged,
    ReaderWantToReadChanged,
    get_event_bus,
)
from pullbox.core.issue_numbers import format_issue_number
from pullbox.core.page_sources import PageSourceError, PageSourceErrorCode, ReaderResourceLimits
from pullbox.core.page_sources.capabilities import inspect_reader_capabilities
from pullbox.schemas.reader import (
    ReaderAdjacentIssueResponse,
    ReaderCacheClearResponse,
    ReaderCacheDiagnosticsResponse,
    ReaderCapabilitiesResponse,
    ReaderCompletionUpdate,
    ReaderFormatCapabilityResponse,
    ReaderManifestResponse,
    ReaderProgressResponse,
    ReaderProgressUpdate,
    ReaderStateMutationResponse,
    ReaderStateResponse,
    ReaderStoryArcOption,
    ReaderStoryArcResponse,
    ReaderWantToReadUpdate,
)
from pullbox.services.reader_arc_bookmarks import save_arc_bookmark
from pullbox.services.reader_arc_order import (
    ArcNeighbor,
    ArcReaderOrder,
    list_reader_arcs,
    load_arc_reader_order,
)
from pullbox.services.reader_content_service import (
    ReaderContentService,
    ReaderWorkerBusyError,
    ResolvedReaderSource,
    StaleReaderRevisionError,
    load_reader_source_record,
    resolve_reader_source,
)
from pullbox.services.reader_state_service import (
    ReaderStateEventDescriptor,
    ReaderStateEventKind,
    ReaderStateSnapshot,
    ReaderStateTransition,
    ReaderStateValidationError,
    load_reader_state,
    save_series_bookmark,
    set_reader_completion,
    set_want_to_read,
    update_reader_progress,
)
from pullbox.services.reading_query_service import (
    AdjacentIssueReference,
    load_adjacent_readable_issues,
    load_reader_issue_access,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/reader", tags=["reader"], include_in_schema=False)

_ERROR_STATUS = {
    PageSourceErrorCode.MISSING_FILE: 404,
    PageSourceErrorCode.UNSUPPORTED_FORMAT: 415,
    PageSourceErrorCode.FORMAT_MISMATCH: 422,
    PageSourceErrorCode.CORRUPT_SOURCE: 422,
    PageSourceErrorCode.EMPTY_SOURCE: 422,
    PageSourceErrorCode.PAGE_OUT_OF_RANGE: 404,
    PageSourceErrorCode.RESOURCE_LIMIT: 413,
    PageSourceErrorCode.RENDERER_UNAVAILABLE: 503,
}


def _content_service(request: Request) -> ReaderContentService:
    app = request.app
    service = getattr(app.state, "reader_content_service", None)
    if isinstance(service, ReaderContentService):
        return service
    settings = get_settings()
    service = ReaderContentService(
        cache_dir=settings.data_dir / "reader-cache",
        limits=ReaderResourceLimits(
            max_entries=settings.reader_max_entries,
            max_page_bytes=settings.reader_max_page_mb * 1024 * 1024,
            max_total_uncompressed_bytes=settings.reader_max_expanded_mb * 1024 * 1024,
            max_compression_ratio=settings.reader_max_compression_ratio,
            compression_ratio_min_bytes=(settings.reader_compression_ratio_min_mb * 1024 * 1024),
            max_image_pixels=settings.reader_max_image_pixels,
            max_image_entries=settings.reader_max_image_entries,
            max_member_path_chars=settings.reader_max_member_path_chars,
            max_member_depth=settings.reader_max_member_depth,
            max_rendition_width=settings.reader_max_rendition_width,
            max_rendition_height=settings.reader_max_rendition_height,
            pdf_dpi=settings.reader_pdf_dpi,
            render_timeout_seconds=settings.reader_render_timeout_seconds,
        ),
        max_open_sources=settings.reader_open_source_cache_size,
        max_cache_bytes=settings.reader_cache_max_mb * 1024 * 1024,
        max_workers=settings.reader_worker_count,
        worker_wait_seconds=settings.reader_worker_wait_seconds,
    )
    app.state.reader_content_service = service
    return service


def _require_reader_enabled() -> None:
    if not get_settings().reader_enabled:
        raise HTTPException(status_code=404, detail="Not found")


async def _load_source(request: Request, issue_id: int) -> ResolvedReaderSource:
    async with get_request_session_factory(request)() as session:
        record = await load_reader_source_record(session, issue_id)
    return await anyio.to_thread.run_sync(resolve_reader_source, record, abandon_on_cancel=True)


async def _resolved_source(request: Request, issue_id: int) -> ResolvedReaderSource:
    try:
        return await _load_source(request, issue_id)
    except PageSourceError as exc:
        _raise_http_error(exc)


def _raise_http_error(exc: PageSourceError) -> Never:
    raise HTTPException(
        status_code=_ERROR_STATUS[exc.code],
        detail={"code": exc.code.value, "message": str(exc)},
    ) from exc


async def _import_source(request: Request, job_id: int, file_id: int) -> ResolvedReaderSource:
    from pullbox.core.exceptions import ConfigurationError, ValidationError
    from pullbox.services.import_reader_service import (
        load_import_reader_record,
        resolve_import_reader_source,
    )

    _require_reader_enabled()
    try:
        async with get_request_session_factory(request)() as session:
            record = await load_import_reader_record(session, job_id, file_id)
        return await anyio.to_thread.run_sync(resolve_import_reader_source, record)
    except (ValidationError, ConfigurationError, ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(
            status_code=409,
            detail="This file changed, is unavailable, or cannot be previewed safely. "
            "Close the reader and recheck it, or choose Skip.",
        ) from exc
    except PageSourceError as exc:
        _raise_import_reader_error(exc)


def _raise_import_reader_error(exc: PageSourceError) -> Never:
    message = str(exc)
    if exc.code in {PageSourceErrorCode.CORRUPT_SOURCE, PageSourceErrorCode.EMPTY_SOURCE}:
        message = "This archive or its image is damaged or contains no readable pages."
    raise HTTPException(
        status_code=_ERROR_STATUS[exc.code],
        detail={
            "code": exc.code.value,
            "message": message
            + " Close the reader and choose Skip if you do not want to import it.",
        },
    ) from exc


@router.get("/imports/{job_id}/files/{file_id}/manifest")
async def import_reader_manifest(
    request: Request, job_id: int, file_id: int, _user: InteractiveOperatorUser
) -> JSONResponse:
    """Preview a staged file without approval, registration, or reading-state writes."""
    source = await _import_source(request, job_id, file_id)
    try:
        service = _content_service(request)
        manifest = await service.get_manifest(source)
        if manifest.page_count != 1:
            raise HTTPException(
                status_code=409,
                detail="This file is no longer a one-page archive. "
                "Close the reader and recheck it, or choose Skip.",
            )
        # Validate the image too, so a broken page gets an actionable reader error.
        await service.get_page(source, page_index=0, revision=manifest.revision)
    except ReaderWorkerBusyError as exc:
        _raise_reader_busy(exc)
    except PageSourceError as exc:
        _raise_import_reader_error(exc)
    return JSONResponse(
        content={
            "title": manifest.title,
            "issue_label": "Import preview",
            "page_count": manifest.page_count,
            "revision": manifest.revision,
            "initial_page_index": 0,
            "page_url_template": (
                f"/api/v1/reader/imports/{job_id}/files/{file_id}/pages/{{page_index}}"
                f"?revision={manifest.revision}"
            ),
        },
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/imports/{job_id}/files/{file_id}/pages/{page_index}")
async def import_reader_page(
    request: Request,
    job_id: int,
    file_id: int,
    page_index: int,
    _user: InteractiveOperatorUser,
    revision: Annotated[str, Query(min_length=1, max_length=64)],
) -> Response:
    source = await _import_source(request, job_id, file_id)
    if page_index != 0:
        raise HTTPException(status_code=404, detail="Preview page not found.")
    try:
        page = await _content_service(request).get_page(source, page_index=0, revision=revision)
    except ReaderWorkerBusyError as exc:
        _raise_reader_busy(exc)
    except StaleReaderRevisionError as exc:
        raise HTTPException(
            status_code=409, detail="The file changed. Open View File again."
        ) from exc
    except PageSourceError as exc:
        _raise_import_reader_error(exc)
    return FileResponse(
        page.path,
        media_type=page.media_type,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


async def _validated_arc(
    session: AsyncSession,
    arc_id: int,
    issue_id: int,
    entry_id: int | None,
    after: int | None,
    before: int | None,
    across_gap_from_issue: int | None,
) -> ArcReaderOrder:
    arc = await load_arc_reader_order(session, arc_id, issue_id, entry_id)
    if arc is None:
        raise HTTPException(
            409,
            "This issue is no longer a confirmed entry in this story arc. "
            "Reopen the story arc to see its current reading order.",
        )
    if boundary := after or before:
        neighbor = arc.previous if after else arc.next
        if across_gap_from_issue is not None:
            # A chapter becoming readable between the two invalidates the old jump.
            neighbor = arc.previous_available if after else arc.next_available
        if (
            neighbor is None
            or neighbor.entry_id != boundary
            or (across_gap_from_issue is not None and neighbor.issue_id != across_gap_from_issue)
        ):
            raise HTTPException(
                409, "The arc reading order changed. Reopen the story arc to continue."
            )
    return arc


@router.get("/issues/{issue_id}/manifest", response_model=ReaderManifestResponse)
async def reader_manifest(
    request: Request,
    issue_id: int,
    _user: AuthenticatedStreamUser,
    story_arc_id: Annotated[int | None, Query(ge=1)] = None,
    entry_id: Annotated[int | None, Query(ge=1)] = None,
    after_entry: Annotated[int | None, Query(ge=1)] = None,
    before_entry: Annotated[int | None, Query(ge=1)] = None,
    across_gap_from_issue: Annotated[int | None, Query(ge=1)] = None,
    reading_series_id: Annotated[int | None, Query(ge=1)] = None,
) -> JSONResponse:
    """Return a side-effect-free reader manifest after closing the DB session."""
    _require_reader_enabled()
    if (after_entry is not None and before_entry is not None) or (
        story_arc_id is None and any((entry_id, after_entry, before_entry))
    ):
        raise HTTPException(422, "Invalid story arc navigation.")
    if across_gap_from_issue is not None and (
        entry_id is None or not (after_entry or before_entry)
    ):
        raise HTTPException(422, "Invalid story arc gap navigation.")
    factory = get_request_session_factory(request)
    directed = bool(after_entry or before_entry)

    async def raise_manifest_error(exc: PageSourceError) -> Never:
        if (
            not directed
            or story_arc_id is None
            or entry_id is None
            or exc.code
            not in {
                PageSourceErrorCode.MISSING_FILE,
                PageSourceErrorCode.UNSUPPORTED_FORMAT,
                PageSourceErrorCode.FORMAT_MISMATCH,
                PageSourceErrorCode.CORRUPT_SOURCE,
                PageSourceErrorCode.EMPTY_SOURCE,
                PageSourceErrorCode.RESOURCE_LIMIT,
            }
        ):
            _raise_http_error(exc)
        async with factory() as session:
            arc = await _validated_arc(
                session,
                story_arc_id,
                issue_id,
                entry_id,
                after_entry,
                before_entry,
                across_gap_from_issue,
            )
        neighbor = arc.next if after_entry else arc.previous
        boundary = f"{'after' if after_entry else 'before'}_entry={arc.entry_id}"
        if neighbor and neighbor.gap:
            neighbor = arc.next_available if after_entry else arc.previous_available
            boundary += f"&across_gap_from_issue={issue_id}"
        target = _arc_adjacent_response(neighbor, story_arc_id, boundary)
        raise HTTPException(
            _ERROR_STATUS[exc.code],
            {
                "code": exc.code.value,
                "message": str(exc),
                "arc_continuation": target.model_dump(mode="json") if target else None,
            },
            headers={"Cache-Control": "private, no-store"},
        ) from exc

    try:
        source = await _load_source(request, issue_id)
        if reading_series_id is not None and source.series_id != reading_series_id:
            raise HTTPException(
                409, "This issue changed series. Reopen the series to choose an issue."
            )
        manifest = await _content_service(request).get_manifest(source)
    except ReaderWorkerBusyError as exc:
        _raise_reader_busy(exc)
    except PageSourceError as exc:
        await raise_manifest_error(exc)
    async with factory() as session:
        state = await load_reader_state(session, user_id=_user.id, issue_id=issue_id)
        reading_arcs = [
            ReaderStoryArcOption(id=arc_id, entry_id=member_id, name=name)
            for arc_id, member_id, name in await list_reader_arcs(session, issue_id)
        ]
        arc_context = None
        detail_url = f"/issues/{issue_id}"
        if story_arc_id is not None:
            arc = await _validated_arc(
                session,
                story_arc_id,
                issue_id,
                entry_id,
                after_entry,
                before_entry,
                across_gap_from_issue,
            )
            arc_context = ReaderStoryArcResponse(
                id=story_arc_id,
                name=arc.name,
                entry_id=arc.entry_id,
                position=arc.position,
                total=arc.total,
                previous_gap=arc.previous.gap if arc.previous else None,
                next_gap=arc.next.gap if arc.next else None,
                previous_available=_arc_adjacent_response(
                    arc.previous_available,
                    story_arc_id,
                    f"before_entry={arc.entry_id}&across_gap_from_issue={issue_id}",
                ),
                next_available=_arc_adjacent_response(
                    arc.next_available,
                    story_arc_id,
                    f"after_entry={arc.entry_id}&across_gap_from_issue={issue_id}",
                ),
            )
            detail_url += f"?source=story-arc&story_arc_id={story_arc_id}&entry_id={arc.entry_id}"
            previous = _arc_adjacent_response(
                arc.previous, story_arc_id, f"before_entry={arc.entry_id}"
            )
            following = _arc_adjacent_response(
                arc.next, story_arc_id, f"after_entry={arc.entry_id}"
            )
        else:
            adjacent = await load_adjacent_readable_issues(
                session,
                series_id=source.series_id,
                current_issue_number=source.issue_number_value,
                current_issue_id=issue_id,
            )
            previous, following = (
                _adjacent_response(adjacent.previous),
                _adjacent_response(adjacent.next),
            )
    initial_page_index = _manifest_initial_page(state, current_page_count=manifest.page_count)
    if directed:
        # Validate and cache only the landing page, before replacing the displayed comic.
        try:
            await _content_service(request).get_page(
                source, page_index=initial_page_index, revision=manifest.revision
            )
        except ReaderWorkerBusyError as exc:
            _raise_reader_busy(exc)
        except PageSourceError as exc:
            await raise_manifest_error(exc)
    response = ReaderManifestResponse(
        issue_id=manifest.issue_id,
        reading_series_id=source.series_id,
        title=manifest.title,
        issue_label=manifest.issue_label,
        format=manifest.format,
        page_count=manifest.page_count,
        revision=manifest.revision,
        initial_page_index=initial_page_index,
        page_url_template=(
            f"/api/v1/reader/issues/{issue_id}/pages/{{page_index}}?revision={manifest.revision}"
        ),
        progress_url=f"/api/v1/reader/issues/{issue_id}/progress",
        completion_url=f"/api/v1/reader/issues/{issue_id}/completion",
        want_to_read_url=f"/api/v1/reader/issues/{issue_id}/want-to-read",
        issue_detail_url=detail_url,
        download_url=f"/api/v1/issues/{issue_id}/download-file",
        state=_state_response(state),
        previous_issue=previous,
        next_issue=following,
        story_arc=arc_context,
        reading_arcs=reading_arcs,
    )
    return JSONResponse(
        content=response.model_dump(mode="json"),
        headers={"Cache-Control": "private, no-cache"},
    )


@router.get("/issues/{issue_id}/pages/{page_index}")
async def reader_page(
    request: Request,
    issue_id: int,
    page_index: int,
    _user: AuthenticatedStreamUser,
    revision: Annotated[str, Query(min_length=1, max_length=64)],
) -> Response:
    """Stream one immutable cached page without a request-scoped DB session."""
    _require_reader_enabled()
    source = await _resolved_source(request, issue_id)
    try:
        page = await _content_service(request).get_page(
            source,
            page_index=page_index,
            revision=revision,
        )
    except ReaderWorkerBusyError as exc:
        _raise_reader_busy(exc)
    except StaleReaderRevisionError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "stale_revision", "message": "The comic file has changed."},
        ) from exc
    except PageSourceError as exc:
        _raise_http_error(exc)

    headers = {
        "Cache-Control": "private, max-age=3600, immutable",
        "ETag": page.etag,
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'inline; filename="page-{page_index + 1}{page.path.suffix}"',
    }
    if request.headers.get("if-none-match") == page.etag:
        return Response(status_code=304, headers=headers)
    return FileResponse(path=page.path, media_type=page.media_type, headers=headers)


@router.put(
    "/issues/{issue_id}/progress",
    response_model=ReaderProgressResponse,
)
async def reader_progress(
    request: Request,
    issue_id: int,
    payload: ReaderProgressUpdate,
    user: AuthenticatedStreamUser,
) -> ReaderProgressResponse:
    """Persist an explicit settled page without coupling state to page GETs."""
    _require_reader_enabled()
    source = await _resolved_source(request, issue_id)
    try:
        manifest = await _content_service(request).get_manifest(source)
    except ReaderWorkerBusyError as exc:
        _raise_reader_busy(exc)
    except PageSourceError as exc:
        _raise_http_error(exc)
    factory = get_request_session_factory(request)
    try:
        async with factory() as session:
            transition = await update_reader_progress(
                session,
                user_id=user.id,
                issue_id=issue_id,
                revision=payload.revision,
                page_index=payload.page_index,
                page_count=payload.page_count,
                completion_candidate=payload.completion_candidate,
                expected_revision=manifest.revision,
                expected_page_count=manifest.page_count,
                reread_started=payload.reread_started,
            )
            bookmark_saved = None
            series_saved = None
            if payload.story_arc is not None:
                bookmark_saved = await save_arc_bookmark(
                    session,
                    user_id=user.id,
                    arc_id=payload.story_arc.id,
                    entry_id=payload.story_arc.entry_id,
                    issue_id=issue_id,
                )
            else:
                series_saved = await save_series_bookmark(
                    session,
                    user_id=user.id,
                    series_id=payload.reading_series_id or source.series_id,
                    issue_id=issue_id,
                )
            await session.commit()
    except ReaderStateValidationError as exc:
        status_code = 409 if exc.code in {"stale_revision", "page_count_mismatch"} else 422
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    await _emit_reader_events(transition.events)
    snapshot = transition.after
    if (
        snapshot.last_page_index is None
        or snapshot.page_count is None
        or snapshot.content_revision is None
    ):  # pragma: no cover - a validated progress command always writes the triple
        raise RuntimeError("Progress command returned state without progress.")
    return ReaderProgressResponse(
        page_index=snapshot.last_page_index,
        page_count=snapshot.page_count,
        revision=snapshot.content_revision,
        completed_at=snapshot.completed_at,
        updated_at=snapshot.updated_at,
        arc_bookmark_saved=bookmark_saved,
        series_bookmark_saved=series_saved,
        state=_state_response(snapshot),
    )


@router.put(
    "/issues/{issue_id}/completion",
    response_model=ReaderStateMutationResponse,
)
async def reader_completion(
    request: Request,
    issue_id: int,
    payload: ReaderCompletionUpdate,
    user: AuthenticatedStreamUser,
) -> ReaderStateMutationResponse:
    """Set explicit completion intent for an existing catalog issue."""
    _require_reader_enabled()
    factory = get_request_session_factory(request)
    async with factory() as session:
        access = await load_reader_issue_access(session, issue_id=issue_id)
        if access is None:
            raise HTTPException(status_code=404, detail="Issue not found")
        transition = await set_reader_completion(
            session,
            user_id=user.id,
            issue_id=issue_id,
            completed=payload.completed,
        )
        await session.commit()
    await _emit_reader_events(transition.events)
    return _mutation_response(transition)


@router.put(
    "/issues/{issue_id}/want-to-read",
    response_model=ReaderStateMutationResponse,
)
async def reader_want_to_read(
    request: Request,
    issue_id: int,
    payload: ReaderWantToReadUpdate,
    user: AuthenticatedStreamUser,
) -> ReaderStateMutationResponse:
    """Set private queue intent, requiring readability only when adding."""
    _require_reader_enabled()
    factory = get_request_session_factory(request)
    async with factory() as session:
        access = await load_reader_issue_access(session, issue_id=issue_id)
        if access is None:
            raise HTTPException(status_code=404, detail="Issue not found")
        if payload.want_to_read and not access.readable:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "issue_not_readable",
                    "message": "This issue does not have a supported downloaded file.",
                },
            )
        transition = await set_want_to_read(
            session,
            user_id=user.id,
            issue_id=issue_id,
            enabled=payload.want_to_read,
        )
        await session.commit()
    await _emit_reader_events(transition.events)
    return _mutation_response(transition)


def _manifest_initial_page(
    state: ReaderStateSnapshot | None,
    *,
    current_page_count: int,
) -> int:
    if state is None or not state.has_progress:
        return 0
    if state.is_completed or state.last_page_index is None or state.page_count is None:
        return 0
    if state.last_page_index >= state.page_count - 1:
        return 0
    if state.page_count != current_page_count:
        return 0
    return max(0, min(state.last_page_index, current_page_count - 1))


def _state_response(state: ReaderStateSnapshot | None) -> ReaderStateResponse:
    if state is None:
        return ReaderStateResponse(
            page_index=None,
            page_count=None,
            progress_updated_at=None,
            last_opened_at=None,
            completed_at=None,
            completion_updated_at=None,
            want_to_read=False,
            want_to_read_updated_at=None,
            state_version=0,
        )
    return ReaderStateResponse(
        page_index=state.last_page_index,
        page_count=state.page_count,
        progress_updated_at=state.progress_updated_at,
        last_opened_at=state.last_opened_at,
        completed_at=state.completed_at,
        completion_updated_at=state.completion_updated_at,
        want_to_read=state.want_to_read,
        want_to_read_updated_at=state.want_to_read_updated_at,
        state_version=state.state_version,
    )


def _arc_adjacent_response(
    item: ArcNeighbor | None, arc_id: int, boundary: str
) -> ReaderAdjacentIssueResponse | None:
    if item is None or item.gap or item.issue_id is None:
        return None
    query = f"story_arc_id={arc_id}&entry_id={item.entry_id}"
    return ReaderAdjacentIssueResponse(
        issue_id=item.issue_id,
        issue_label=item.label,
        title=item.title,
        manifest_url=f"/api/v1/reader/issues/{item.issue_id}/manifest?{query}&{boundary}",
        issue_detail_url=f"/issues/{item.issue_id}?source=story-arc&{query}",
        download_url=f"/api/v1/issues/{item.issue_id}/download-file",
    )


def _adjacent_response(
    adjacent: AdjacentIssueReference | None,
) -> ReaderAdjacentIssueResponse | None:
    if adjacent is None:
        return None
    issue_id = adjacent.issue_id
    return ReaderAdjacentIssueResponse(
        issue_id=issue_id,
        issue_label=f"#{format_issue_number(adjacent.issue_number)}",
        title=adjacent.title,
        manifest_url=f"/api/v1/reader/issues/{issue_id}/manifest",
        issue_detail_url=f"/issues/{issue_id}",
        download_url=f"/api/v1/issues/{issue_id}/download-file",
    )


def _mutation_response(transition: ReaderStateTransition) -> ReaderStateMutationResponse:
    return ReaderStateMutationResponse(
        changed=transition.changed,
        state=_state_response(transition.after),
    )


async def _emit_reader_events(
    descriptors: tuple[ReaderStateEventDescriptor, ...],
) -> None:
    event_bus = get_event_bus()
    for descriptor in descriptors:
        if descriptor.kind is ReaderStateEventKind.COMPLETION_CHANGED:
            if descriptor.completed is None or descriptor.origin is None:
                continue
            await event_bus.emit(
                ReaderCompletionChanged(
                    user_id=descriptor.user_id,
                    issue_id=descriptor.issue_id,
                    state_version=descriptor.state_version,
                    completed=descriptor.completed,
                    occurred_at=descriptor.occurred_at,
                    origin=descriptor.origin.value,
                )
            )
        elif descriptor.kind is ReaderStateEventKind.WANT_TO_READ_CHANGED:
            if descriptor.want_to_read is None:
                continue
            await event_bus.emit(
                ReaderWantToReadChanged(
                    user_id=descriptor.user_id,
                    issue_id=descriptor.issue_id,
                    state_version=descriptor.state_version,
                    enabled=descriptor.want_to_read,
                    occurred_at=descriptor.occurred_at,
                )
            )


def _raise_reader_busy(exc: ReaderWorkerBusyError) -> Never:
    raise HTTPException(
        status_code=503,
        detail={"code": "reader_busy", "message": "The comic reader is busy. Try again shortly."},
        headers={"Retry-After": "1"},
    ) from exc


@router.get("/capabilities", response_model=ReaderCapabilitiesResponse)
async def reader_capabilities(
    request: Request,
    _user: AuthenticatedStreamUser,
) -> ReaderCapabilitiesResponse:
    """Return private runtime support and bounded cache diagnostics."""
    _require_reader_enabled()
    capabilities = await anyio.to_thread.run_sync(inspect_reader_capabilities)
    diagnostics = await _content_service(request).get_diagnostics()
    return ReaderCapabilitiesResponse(
        enabled=True,
        formats=[
            ReaderFormatCapabilityResponse(
                format=item.format,
                available=item.available,
                detail=item.detail,
            )
            for item in capabilities
        ],
        cache=ReaderCacheDiagnosticsResponse(
            cache_file_count=diagnostics.cache_file_count,
            cache_bytes=diagnostics.cache_bytes,
            max_cache_bytes=diagnostics.max_cache_bytes,
            open_source_count=diagnostics.open_source_count,
            max_open_sources=diagnostics.max_open_sources,
            max_workers=diagnostics.max_workers,
        ),
    )


@router.delete("/cache", response_model=ReaderCacheClearResponse)
async def clear_reader_cache(
    request: Request,
    _user: AuthenticatedStreamUser,
) -> ReaderCacheClearResponse:
    """Clear generated reader pages; source comic files are never targeted."""
    _require_reader_enabled()
    result = await _content_service(request).clear_cache()
    return ReaderCacheClearResponse(
        files_removed=result.files_removed,
        bytes_removed=result.bytes_removed,
    )
