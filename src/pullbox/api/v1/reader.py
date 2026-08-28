"""Private manifest and revisioned page endpoints for the embedded reader."""

from __future__ import annotations

from typing import Annotated, Never

import anyio
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from pullbox.api.deps import AuthenticatedStreamUser, get_request_session_factory
from pullbox.config import get_settings
from pullbox.core.events import (
    ReaderCompletionChanged,
    ReaderWantToReadChanged,
    get_event_bus,
)
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
    ReaderWantToReadUpdate,
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
    set_reader_completion,
    set_want_to_read,
    update_reader_progress,
)
from pullbox.services.reading_query_service import (
    AdjacentIssueReference,
    load_adjacent_readable_issues,
    load_reader_issue_access,
)

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


async def _resolved_source(request: Request, issue_id: int) -> ResolvedReaderSource:
    factory = get_request_session_factory(request)
    try:
        async with factory() as session:
            record = await load_reader_source_record(session, issue_id)
        return await anyio.to_thread.run_sync(resolve_reader_source, record, abandon_on_cancel=True)
    except PageSourceError as exc:
        _raise_http_error(exc)


def _raise_http_error(exc: PageSourceError) -> Never:
    raise HTTPException(
        status_code=_ERROR_STATUS[exc.code],
        detail={"code": exc.code.value, "message": str(exc)},
    ) from exc


@router.get("/issues/{issue_id}/manifest", response_model=ReaderManifestResponse)
async def reader_manifest(
    request: Request,
    issue_id: int,
    _user: AuthenticatedStreamUser,
) -> JSONResponse:
    """Return a side-effect-free reader manifest after closing the DB session."""
    _require_reader_enabled()
    source = await _resolved_source(request, issue_id)
    try:
        manifest = await _content_service(request).get_manifest(source)
    except ReaderWorkerBusyError as exc:
        _raise_reader_busy(exc)
    except PageSourceError as exc:
        _raise_http_error(exc)
    factory = get_request_session_factory(request)
    async with factory() as session:
        state = await load_reader_state(session, user_id=_user.id, issue_id=issue_id)
        adjacent = await load_adjacent_readable_issues(
            session,
            series_id=source.series_id,
            current_issue_number=source.issue_number_value,
            current_issue_id=issue_id,
        )
    initial_page_index = _manifest_initial_page(state, current_page_count=manifest.page_count)
    response = ReaderManifestResponse(
        issue_id=manifest.issue_id,
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
        issue_detail_url=f"/issues/{issue_id}",
        download_url=f"/api/v1/issues/{issue_id}/download-file",
        state=_state_response(state),
        previous_issue=_adjacent_response(adjacent.previous),
        next_issue=_adjacent_response(adjacent.next),
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


def _adjacent_response(
    adjacent: AdjacentIssueReference | None,
) -> ReaderAdjacentIssueResponse | None:
    if adjacent is None:
        return None
    issue_id = adjacent.issue_id
    return ReaderAdjacentIssueResponse(
        issue_id=issue_id,
        issue_label=f"#{adjacent.issue_number:g}",
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
