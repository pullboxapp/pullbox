"""Import Jobs REST API — create, manage, and stream collection imports."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from sqlalchemy.exc import OperationalError

from pullbox.api.deps import AuthenticatedStreamUser, AuthenticatedUser, DbSession  # noqa: TC001
from pullbox.api.v1.import_job_activity import get_active_import_activity_response
from pullbox.api.v1.import_job_control_actions import (
    allow_safety_blocked_file_once_and_retry_response,
    cancel_import_job_response,
    clear_import_history_response,
    confirm_import_response,
    create_import_job_response,
    get_import_job_response,
    get_import_preview_response,
    pause_import_job_response,
    request_cancel_import_job_response,
    resume_import_job_response,
    retry_failed_series_response,
    retry_import_job_response,
    retry_story_arc_placements_response,
    rollback_import_job_response,
)
from pullbox.api.v1.import_job_logs import (
    build_import_job_logs_download_response,
    build_import_job_logs_response,
    log_levels_at_or_above,
)
from pullbox.api.v1.import_job_orphaned_routes import router as orphaned_router
from pullbox.api.v1.import_job_review_actions import (
    allow_safety_blocked_file_once_response,
    bulk_update_file_selection_response,
    bulk_update_series_selection_response,
    confirm_story_arc_policy_response,
    get_selection_state_response,
    list_conflicts_response,
    list_import_files_response,
    override_file_match_response,
    repair_file_metadata_response,
    reset_conflicts_response,
    resolve_conflict_response,
    resolve_conflicts_bulk_response,
    skip_safety_blocked_file_response,
    unmatch_duplicate_series_response,
    unmatch_series_match_response,
    update_file_selection_response,
    update_series_selection_response,
    update_story_arc_decision_response,
)
from pullbox.api.v1.import_job_streams import (
    ensure_import_job_exists_for_stream as _ensure_import_job_exists_for_stream,
)
from pullbox.api.v1.import_job_streams import (
    iter_import_log_sse,
    iter_import_progress_sse,
    make_sse_streaming_response,
)
from pullbox.api.v1.import_job_streams import (
    load_initial_import_progress_sse as _load_initial_import_progress_sse,
)
from pullbox.core.exceptions import MylarReadError, ValidationError
from pullbox.core.library_file_ownership import (
    ReferencedFileValidationError,
    resolve_referenced_source_root,
)
from pullbox.core.sqlite_lock import (
    SQLITE_LOCK_RETRY_ATTEMPTS,
    is_sqlite_locked_error,
    sqlite_lock_retry_delay,
)
from pullbox.schemas.import_job import (
    ConfirmImportRequest,
    ConflictGroupsResponse,
    ConflictResetRequest,
    ConflictResolveBulkRequest,
    ConflictResolveBulkResponse,
    ConflictResolveRequest,
    FileConflictGroup,
    FileMatchUpdateRequest,
    FileMetadataRepairRequest,
    FileSelectionBulkUpdateRequest,
    FileSelectionUpdateRequest,
    ImportActivityRead,
    ImportedFileRead,
    ImportedFilesResponse,
    ImportedSeriesRead,
    ImportJobCreate,
    ImportJobDeleteResponse,
    ImportJobLogsResponse,
    ImportJobRead,
    ImportPreviewResponse,
    ImportReconcileRequest,
    ImportReviewSelectionState,
    ImportSelectionBulkUpdateResponse,
    RetryFailedResponse,
    RetryImportResponse,
    RetryStoryArcPlacementsResponse,
    SeriesSelectionBulkUpdateRequest,
    SeriesSelectionUpdateRequest,
    StoryArcPolicyConfirmationRequest,
    StoryArcPolicyConfirmationResponse,
    StoryArcReviewDecisionRequest,
    StoryArcReviewDecisionResponse,
)
from pullbox.schemas.import_layout import LayoutAnalysisResponse, LayoutPreviewRequest
from pullbox.schemas.import_mylar3_path_preflight import (
    MylarPathPreviewRequest,
    MylarPathPreviewResponse,
)
from pullbox.schemas.import_story_arc_preflight import (
    StoryArcPreflightRequest,
    StoryArcPreflightResponse,
)
from pullbox.services import import_mylar3_path_reports as path_reports
from pullbox.services.import_layout_analysis import ImportLayoutAnalyzer
from pullbox.services.import_mylar3_path_preflight import Mylar3PathPreflightAnalyzer
from pullbox.services.import_story_arc_preflight import StoryArcPreflightAnalyzer
from pullbox.tasks.import_task import (
    purge_import_runtime_state,
    trigger_import_execute,
    trigger_import_resume,
    trigger_import_rollback,
    trigger_import_scan,
    trigger_import_series_rematch,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/import", tags=["import"])

_INTERACTIVE_IMPORT_CV_BURST = 3

# ── Service Construction ─────────────────────────────────────────────────


async def _build_import_service(session: AsyncSession) -> Any:
    """Build ImportService with dependencies for route handlers."""
    from pullbox.composition.services import build_import_service

    return await build_import_service(session)


async def _build_interactive_import_service(session: AsyncSession) -> Any:
    """Build ImportService for user-driven ComicVine review actions."""
    from pullbox.composition.services import build_import_service

    return await build_import_service(
        session,
        min_burst_limit=_INTERACTIVE_IMPORT_CV_BURST,
    )


def _make_import_service() -> Any:
    """Create a lightweight ImportService for operations that don't need CV."""
    from pullbox.composition.services import build_import_control_service

    return build_import_control_service()


async def _bulk_update_series_selection_with_retry(
    session: AsyncSession,
    job_id: int,
    body: SeriesSelectionBulkUpdateRequest,
) -> ImportSelectionBulkUpdateResponse:
    """Retry a small Step 3 selection write after transient SQLite contention."""
    service = _make_import_service()
    for attempt in range(1, SQLITE_LOCK_RETRY_ATTEMPTS + 1):
        try:
            response = await bulk_update_series_selection_response(service, session, job_id, body)
            await session.commit()
            return response
        except OperationalError as exc:
            await session.rollback()
            if not is_sqlite_locked_error(exc) or attempt == SQLITE_LOCK_RETRY_ATTEMPTS:
                raise
            delay_seconds = sqlite_lock_retry_delay(attempt)
            logger.info(
                "import_review_selection_retrying_after_sqlite_lock",
                job_id=job_id,
                attempt=attempt,
                max_attempts=SQLITE_LOCK_RETRY_ATTEMPTS,
                delay_seconds=delay_seconds,
            )
            await asyncio.sleep(delay_seconds)

    raise RuntimeError("SQLite selection retry loop exhausted unexpectedly")


# ── Log Level Filtering ──────────────────────────────────────────────────


def _log_levels_at_or_above(level: str) -> list[str]:
    """Return list of log levels at or above the given severity."""
    return log_levels_at_or_above(level)


# ── Endpoints ────────────────────────────────────────────────────────────


@router.post("", response_model=ImportJobRead, status_code=201)
async def create_import_job(
    _user: AuthenticatedUser,
    session: DbSession,
    body: ImportJobCreate,
) -> ImportJobRead:
    """Create a new import job and trigger the scan task."""
    service = _make_import_service()
    return await create_import_job_response(
        service,
        session=session,
        body=body,
        trigger_import_scan=trigger_import_scan,
    )


@router.post("/layout-preview", response_model=LayoutAnalysisResponse)
async def preview_import_layout(
    _user: AuthenticatedUser,
    session: DbSession,
    body: LayoutPreviewRequest,
) -> LayoutAnalysisResponse:
    """Analyze a source layout without creating a job or mutating files."""
    analyzer = ImportLayoutAnalyzer()
    try:
        result = await analyzer.analyze(
            body.source_path,
            spec=body.layout.to_core(),
        )
    except (OSError, ValueError) as exc:
        raise ValidationError(
            "The layout preview source is no longer available or readable."
        ) from exc
    try:
        await resolve_referenced_source_root(session, Path(body.source_path), None)
    except ReferencedFileValidationError:
        warnings = list(result.warnings)
        if "source_outside_library_root" not in warnings:
            warnings.append("source_outside_library_root")
        result = replace(
            result,
            can_keep_in_place=False,
            warnings=warnings,
        )
    return LayoutAnalysisResponse.model_validate(result)


@router.post("/mylar-path-preview", response_model=MylarPathPreviewResponse)
async def preview_import_mylar_paths(
    _user: AuthenticatedUser,
    session: DbSession,
    body: MylarPathPreviewRequest,
) -> MylarPathPreviewResponse:
    """Analyze source paths read-only and retain a private report without creating a job."""
    analyzer = Mylar3PathPreflightAnalyzer()
    try:
        result = await analyzer.analyze(
            session,
            body.source_path,
            auto_detect=body.auto_detect,
            mappings=body.mappings,
            file_handling_mode=body.file_handling_mode,
        )
    except (OSError, ValueError) as exc:
        raise ValidationError(
            "The Mylar path preview source is no longer available or readable."
        ) from exc
    try:
        result.report_id = await asyncio.to_thread(
            path_reports.save_report, result, body.source_path
        )
    except (OSError, ValueError):
        logger.warning("mylar_preflight_report_unavailable", exc_info=True)
        result.warnings.append("report_unavailable")
    logger.info(
        "mylar_path_preflight_completed",
        report_id=result.report_id,
        resolution=result.resolution.model_dump(),
        warnings=result.warnings,
        can_confirm=result.can_confirm,
        can_continue_with_unresolved=result.can_continue_with_unresolved,
    )
    return result.model_copy(update={"exceptions": result.exceptions[: path_reports.PAGE_SIZE]})


@router.get("/mylar-path-reports/{report_id}/export")
async def export_mylar_path_report(_user: AuthenticatedUser, report_id: str) -> JSONResponse:
    try:
        report = await asyncio.to_thread(path_reports.load_report, report_id)
    except (OSError, ValueError):
        return JSONResponse(
            {"detail": "Report unavailable or expired. Analyze Mylar paths again."}, status_code=404
        )
    return JSONResponse(
        report,
        headers={
            "Content-Disposition": 'attachment; filename="mylar-path-preflight.json"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/mylar-path-reports/{report_id}")
async def get_mylar_path_report(
    _user: AuthenticatedUser,
    report_id: str,
    page: int = Query(1, ge=1),
    search: str = Query("", max_length=200),
) -> JSONResponse:
    try:
        result = await asyncio.to_thread(path_reports.report_page, report_id, page, search)
    except (OSError, ValueError):
        return JSONResponse(
            {"detail": "Report unavailable or expired. Analyze Mylar paths again."}, status_code=404
        )
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/story-arc-preview", response_model=StoryArcPreflightResponse)
async def preview_import_story_arcs(
    _user: AuthenticatedUser,
    body: StoryArcPreflightRequest,
) -> StoryArcPreflightResponse:
    """Inspect local Story Arc evidence without creating a job or changing files."""
    analyzer = StoryArcPreflightAnalyzer()
    try:
        return await analyzer.analyze(
            body.source_path,
            source_type=body.source_type,
        )
    except (MylarReadError, OSError, ValueError) as exc:
        raise ValidationError(
            "The Story Arc preview source is no longer available or readable."
        ) from exc


# ── Post-Import Recovery (orphaned routes before /{job_id}) ──────────


router.include_router(orphaned_router)


# ── Job Detail & Preview ─────────────────────────────────────────────


@router.get("/active", response_model=ImportActivityRead)
async def get_active_import_activity(
    _user: AuthenticatedUser,
    session: DbSession,
) -> ImportActivityRead:
    """Return background import activity for the persistent app header."""
    return await get_active_import_activity_response(session)


@router.get("/{job_id}", response_model=ImportJobRead)
async def get_import_job(
    job_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> ImportJobRead:
    """Get import job detail."""
    return await get_import_job_response(session, job_id)


@router.get("/{job_id}/preview", response_model=ImportPreviewResponse)
async def get_import_preview(
    job_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
    status: str | None = Query(None, description="Filter by ImportSeriesStatus"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> ImportPreviewResponse:
    """Paginated series preview for the review step."""
    service = _make_import_service()
    return await get_import_preview_response(
        service,
        session=session,
        job_id=job_id,
        status=status,
        page=page,
        page_size=page_size,
    )


@router.post("/{job_id}/confirm", response_model=ImportJobRead)
async def confirm_import(
    job_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
    body: ConfirmImportRequest,
) -> ImportJobRead:
    """Confirm selected series for import and trigger the execute task."""
    service = _make_import_service()
    return await confirm_import_response(
        service,
        session=session,
        job_id=job_id,
        body=body,
        trigger_import_execute=trigger_import_execute,
    )


@router.post(
    "/{job_id}/series/{imported_series_id}/override",
    response_model=ImportedSeriesRead,
)
async def override_series_cv_id(
    job_id: int,
    imported_series_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
    body: dict[str, int],
) -> ImportedSeriesRead:
    """Set a user-chosen ComicVine ID for an imported series candidate."""
    cv_id = body.get("cv_id")
    if not cv_id or cv_id <= 0:
        raise ValidationError("cv_id must be a positive integer")

    service = await _build_interactive_import_service(session)
    item = await service.override_cv_id(session, imported_series_id, cv_id, rematch_files=True)
    await session.commit()

    logger.info(
        "import_series_cv_override",
        job_id=job_id,
        imported_series_id=imported_series_id,
        rematched_series_id=item.id,
        cv_id=cv_id,
    )
    return ImportedSeriesRead.model_validate(item)


@router.post(
    "/{job_id}/series/{imported_series_id}/reconcile",
    response_model=ImportedSeriesRead,
)
async def reconcile_import_series(
    job_id: int,
    imported_series_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
    body: ImportReconcileRequest,
) -> ImportedSeriesRead:
    """Save Step 3 file-to-issue reconciliation decisions for an import row."""
    from fastapi import HTTPException

    service = await _build_interactive_import_service(session)
    try:
        item = await service.reconcile_import_series(session, job_id, imported_series_id, body)
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc

    await session.commit()
    logger.info(
        "import_series_reconciled",
        job_id=job_id,
        imported_series_id=imported_series_id,
        decisions=len(body.decisions),
    )
    return ImportedSeriesRead.model_validate(item)


@router.delete("/history")
async def clear_import_history(
    _user: AuthenticatedUser,
    session: DbSession,
) -> dict[str, int]:
    """Delete terminal import history records while leaving active jobs intact."""
    return await clear_import_history_response(session)


@router.delete(
    "/{job_id}",
    status_code=204,
    responses={
        202: {
            "model": ImportJobDeleteResponse,
            "description": "Deletion is waiting for cooperative Story Arc rollback.",
        }
    },
)
async def cancel_import_job(
    job_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Cancel an active import job or delete a finished one from history."""
    service = _make_import_service()
    pending = await cancel_import_job_response(
        service,
        session=session,
        job_id=job_id,
        purge_import_runtime_state=purge_import_runtime_state,
    )
    if pending is not None:
        return JSONResponse(status_code=202, content=pending.model_dump(mode="json"))
    return Response(status_code=204)


@router.post("/{job_id}/pause", response_model=ImportJobRead)
async def pause_import_job(
    job_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> ImportJobRead:
    """Request a cooperative pause for a running import job."""
    service = _make_import_service()
    return await pause_import_job_response(service, session=session, job_id=job_id)


@router.post("/{job_id}/resume", response_model=ImportJobRead)
async def resume_import_job(
    job_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> ImportJobRead:
    """Resume a paused import job."""
    service = _make_import_service()
    return await resume_import_job_response(
        service,
        session=session,
        job_id=job_id,
        trigger_import_resume=trigger_import_resume,
    )


@router.post("/{job_id}/cancel", response_model=ImportJobRead | None)
async def request_cancel_import_job(
    job_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> ImportJobRead | None:
    """Request cancellation of a running import job."""
    service = _make_import_service()
    return await request_cancel_import_job_response(
        service,
        session=session,
        job_id=job_id,
        trigger_import_rollback=trigger_import_rollback,
        purge_import_runtime_state=purge_import_runtime_state,
    )


@router.post("/{job_id}/rollback", response_model=ImportJobRead)
async def rollback_import_job(
    job_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> ImportJobRead:
    """Start rollback for a previously executed import job."""
    service = _make_import_service()
    return await rollback_import_job_response(
        service,
        session=session,
        job_id=job_id,
        trigger_import_rollback=trigger_import_rollback,
    )


@router.post("/{job_id}/retry-failed", status_code=202, response_model=RetryFailedResponse)
async def retry_failed_series(
    job_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> RetryFailedResponse:
    """Reset failed import work and re-trigger the import task."""
    service = await _build_import_service(session)
    return await retry_failed_series_response(
        service,
        session=session,
        job_id=job_id,
        trigger_import_execute=trigger_import_execute,
    )


@router.post(
    "/{job_id}/story-arc-placements/retry",
    status_code=202,
    response_model=RetryStoryArcPlacementsResponse,
)
async def retry_import_story_arc_placements(
    job_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> RetryStoryArcPlacementsResponse:
    """Retry only failed/cancelled placement work for a stalled import."""
    service = _make_import_service()
    return await retry_story_arc_placements_response(
        service,
        session=session,
        job_id=job_id,
        trigger_story_arc_sync=service.schedule_story_arc_sync,
    )


@router.post(
    "/{job_id}/files/{file_id}/safety/allow-once-and-retry",
    status_code=202,
    response_model=RetryFailedResponse,
)
async def allow_safety_blocked_file_once_and_retry(
    job_id: int,
    file_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> RetryFailedResponse:
    """Approve one resource safety exception and re-trigger Step 4 for that file."""
    service = await _build_import_service(session)
    return await allow_safety_blocked_file_once_and_retry_response(
        service,
        session=session,
        job_id=job_id,
        file_id=file_id,
        trigger_import_execute=trigger_import_execute,
    )


@router.post("/{job_id}/retry", status_code=202, response_model=RetryImportResponse)
async def retry_import_job(
    job_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> RetryImportResponse:
    """Create a brand-new import job from a cancelled or rolled-back history row."""
    service = _make_import_service()
    return await retry_import_job_response(
        service,
        session=session,
        job_id=job_id,
        trigger_import_scan=trigger_import_scan,
    )


# ── File-Level Endpoints ──────────────────────────────────────────────


@router.get(
    "/{job_id}/series/{series_id}/files",
    response_model=ImportedFilesResponse,
)
async def list_import_files(
    job_id: int,
    series_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
    status: str | None = Query(None, description="Filter by ImportedFileStatus"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> ImportedFilesResponse:
    """Paginated file list for a series within an import job."""
    service = _make_import_service()
    return await list_import_files_response(
        service,
        session,
        job_id,
        series_id,
        status=status,
        page=page,
        page_size=page_size,
    )


@router.get("/{job_id}/conflicts", response_model=ConflictGroupsResponse)
async def list_conflicts(
    job_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
) -> ConflictGroupsResponse:
    """List one bounded page of conflict groups for an import job."""
    service = _make_import_service()
    return await list_conflicts_response(
        service,
        session,
        job_id,
        page=page,
        page_size=page_size,
    )


@router.put(
    "/{job_id}/files/{file_id}/match",
    response_model=ImportedFileRead,
)
async def override_file_match(
    job_id: int,
    file_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
    body: FileMatchUpdateRequest,
) -> ImportedFileRead:
    """Manually override a file's issue match."""
    service = _make_import_service()
    response = await override_file_match_response(service, session, job_id, file_id, body)
    await session.commit()

    logger.info(
        "import_file_match_override",
        job_id=job_id,
        file_id=file_id,
        issue_id=body.issue_id,
    )
    return response


@router.post(
    "/{job_id}/files/{file_id}/repair-metadata",
    response_model=ImportedFileRead,
)
async def repair_file_metadata(
    job_id: int,
    file_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
    body: FileMetadataRepairRequest | None = None,
) -> ImportedFileRead:
    """Repair embedded ComicInfo.xml for an imported file."""
    service = _make_import_service()
    response = await repair_file_metadata_response(service, session, job_id, file_id, body)
    await session.commit()

    logger.info(
        "import_file_metadata_repair_requested",
        job_id=job_id,
        file_id=file_id,
        issue_id=body.issue_id if body is not None else None,
    )
    return response


@router.put(
    "/{job_id}/series/{imported_series_id}/selection",
    response_model=ImportedSeriesRead,
)
async def update_series_selection(
    job_id: int,
    imported_series_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
    body: SeriesSelectionUpdateRequest,
) -> ImportedSeriesRead:
    """Persist include/exclude state for a matched review series."""
    service = _make_import_service()
    response = await update_series_selection_response(
        service,
        session,
        job_id,
        imported_series_id,
        body,
    )
    await session.commit()
    return response


@router.put(
    "/{job_id}/story-arcs/{imported_story_arc_id}/decision",
    response_model=StoryArcReviewDecisionResponse,
)
async def update_story_arc_decision(
    job_id: int,
    imported_story_arc_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
    body: StoryArcReviewDecisionRequest,
) -> StoryArcReviewDecisionResponse:
    """Persist an explicit Step 3 select/skip decision for one staged story arc."""
    service = _make_import_service()
    response = await update_story_arc_decision_response(
        service,
        session,
        job_id,
        imported_story_arc_id,
        body,
    )
    await session.commit()
    return response


@router.put(
    "/{job_id}/story-arcs/{imported_story_arc_id}/policy-confirmation",
    response_model=StoryArcPolicyConfirmationResponse,
)
async def confirm_story_arc_policy(
    job_id: int,
    imported_story_arc_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
    body: StoryArcPolicyConfirmationRequest,
) -> StoryArcPolicyConfirmationResponse:
    """Explicitly confirm one complete Step 3 story-arc policy."""
    response = await confirm_story_arc_policy_response(
        session,
        job_id,
        imported_story_arc_id,
        body,
    )
    await session.commit()
    return response


@router.get(
    "/{job_id}/selection-state",
    response_model=ImportReviewSelectionState,
)
async def get_selection_state(
    job_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> ImportReviewSelectionState:
    """Return canonical Step 3 selection state."""
    service = _make_import_service()
    return await get_selection_state_response(service, session, job_id)


@router.post(
    "/{job_id}/series/{imported_series_id}/unmatch",
    response_model=ImportedSeriesRead,
)
async def unmatch_series_match(
    job_id: int,
    imported_series_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> ImportedSeriesRead:
    """Reject an incorrect Step 3 ComicVine/library match."""
    service = _make_import_service()
    response = await unmatch_series_match_response(service, session, job_id, imported_series_id)
    await session.commit()
    return response


@router.post(
    "/{job_id}/series/{imported_series_id}/unmatch-duplicate",
    response_model=ImportedSeriesRead,
)
async def unmatch_duplicate_series(
    job_id: int,
    imported_series_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> ImportedSeriesRead:
    """Reject an incorrect existing-library duplicate match."""
    service = _make_import_service()
    response = await unmatch_duplicate_series_response(service, session, job_id, imported_series_id)
    await session.commit()
    return response


@router.post(
    "/{job_id}/files/{file_id}/safety/allow-once",
    response_model=ImportedSeriesRead,
)
async def allow_safety_blocked_file_once(
    job_id: int,
    file_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> ImportedSeriesRead:
    """Allow one safety-blocked import file to re-enter normal matching."""
    service = _make_import_service()
    result = await allow_safety_blocked_file_once_response(service, session, job_id, file_id)
    await session.commit()
    if result.rematch_series_id is not None:
        trigger_import_series_rematch(job_id, result.rematch_series_id)
    return result.payload


@router.post(
    "/{job_id}/files/{file_id}/safety/skip",
    response_model=ImportedSeriesRead,
)
async def skip_safety_blocked_file(
    job_id: int,
    file_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> ImportedSeriesRead:
    """Skip one safety-blocked import file from the active review."""
    service = _make_import_service()
    response = await skip_safety_blocked_file_response(service, session, job_id, file_id)
    await session.commit()
    return response


@router.post(
    "/{job_id}/series/selection-bulk",
    response_model=ImportSelectionBulkUpdateResponse,
)
async def bulk_update_series_selection(
    job_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
    body: SeriesSelectionBulkUpdateRequest,
) -> ImportSelectionBulkUpdateResponse:
    """Persist include/exclude state for matched review series in the current scope."""
    return await _bulk_update_series_selection_with_retry(session, job_id, body)


@router.put(
    "/{job_id}/files/{file_id}/selection",
    response_model=ImportedFileRead,
)
async def update_file_selection(
    job_id: int,
    file_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
    body: FileSelectionUpdateRequest,
) -> ImportedFileRead:
    """Persist include/exclude state for a duplicate-series review file."""
    service = _make_import_service()
    response = await update_file_selection_response(service, session, job_id, file_id, body)
    await session.commit()
    return response


@router.post(
    "/{job_id}/files/selection-bulk",
    response_model=ImportSelectionBulkUpdateResponse,
)
async def bulk_update_file_selection(
    job_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
    body: FileSelectionBulkUpdateRequest,
) -> ImportSelectionBulkUpdateResponse:
    """Persist include/exclude state for duplicate-series files in the current scope."""
    service = _make_import_service()
    response = await bulk_update_file_selection_response(service, session, job_id, body)
    await session.commit()
    return response


@router.put(
    "/{job_id}/conflicts/{group_id}/resolve",
    response_model=FileConflictGroup,
)
async def resolve_conflict(
    job_id: int,
    group_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
    body: ConflictResolveRequest,
) -> FileConflictGroup:
    """Resolve a conflict group by choosing which file to keep."""
    service = _make_import_service()
    response = await resolve_conflict_response(service, session, job_id, group_id, body)
    await session.commit()

    logger.info(
        "import_conflict_resolved",
        job_id=job_id,
        group_id=group_id,
        chosen_file_id=body.chosen_file_id,
    )
    return response


@router.post(
    "/{job_id}/conflicts/resolve-bulk",
    response_model=ConflictResolveBulkResponse,
)
async def resolve_conflicts_bulk(
    job_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
    body: ConflictResolveBulkRequest,
) -> ConflictResolveBulkResponse:
    """Resolve multiple conflict groups in one request."""
    service = _make_import_service()
    response = await resolve_conflicts_bulk_response(service, session, job_id, body)
    await session.commit()

    logger.info(
        "import_conflicts_bulk_resolved",
        job_id=job_id,
        group_ids=response.resolved_group_ids,
        resolved_count=response.resolved_count,
    )
    return response


@router.post("/{job_id}/conflicts/reset")
async def reset_conflicts(
    job_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
    body: ConflictResetRequest | None = None,
) -> dict[str, object]:
    """Reset resolved conflict groups back to review state."""
    service = _make_import_service()
    response = await reset_conflicts_response(service, session, job_id, body)
    await session.commit()

    logger.info(
        "import_conflicts_reset",
        job_id=job_id,
        group_ids=response["reset_group_ids"],
    )
    return response


@router.get("/{job_id}/stream")
async def stream_import_progress(
    job_id: int,
    request: Request,
    _user: AuthenticatedStreamUser,
) -> StreamingResponse:
    """Server-Sent Events stream for import progress."""
    initial_event, initial_is_terminal = await _load_initial_import_progress_sse(request, job_id)

    return make_sse_streaming_response(
        iter_import_progress_sse(
            job_id,
            initial_event=initial_event,
            initial_is_terminal=initial_is_terminal,
        )
    )


# ── Per-Job Log Endpoints ────────────────────────────────────────────────


@router.get("/{job_id}/logs/stream")
async def stream_import_logs(
    job_id: int,
    request: Request,
    _user: AuthenticatedStreamUser,
) -> StreamingResponse:
    """Server-Sent Events stream for live import log entries."""
    await _ensure_import_job_exists_for_stream(request, job_id)

    return make_sse_streaming_response(iter_import_log_sse(job_id))


@router.get("/{job_id}/logs", response_model=ImportJobLogsResponse)
async def get_import_job_logs(
    job_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    level: str | None = Query(None, description="Minimum log level: DEBUG/INFO/WARNING/ERROR"),
    after_id: int | None = Query(
        None,
        ge=1,
        description="Return only log rows with id greater than this value.",
    ),
    order: Literal["asc", "desc"] = Query(
        "asc",
        description="Sort direction for log timestamps.",
    ),
) -> ImportJobLogsResponse:
    """Paginated log entries for an import job."""
    return await build_import_job_logs_response(
        session=session,
        job_id=job_id,
        page=page,
        page_size=page_size,
        level=level,
        after_id=after_id,
        order=order,
    )


@router.get("/{job_id}/logs/download")
async def download_import_job_logs(
    job_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> PlainTextResponse:
    """Download all log entries for a job as plain text."""
    return await build_import_job_logs_download_response(
        session=session,
        job_id=job_id,
    )
