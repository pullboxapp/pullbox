"""Utility API endpoints — job CRUD, controls, queue status, and SSE stream."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Query
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import delete, select
from sqlalchemy import func as sa_func

from pullbox.api.deps import DbSession, InteractiveOperatorUser  # noqa: TC001
from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.core.library_root_resolution import resolve_path_inside_roots
from pullbox.models.config import DEFAULT_SYSTEM_CONFIG, SystemConfig
from pullbox.models.library import LibraryFile, LibraryFileStorageMode, LibraryRoot
from pullbox.services.database_optimization_service import DatabaseOptimizationService
from pullbox.services.health_helpers import _sqlite_database_path
from pullbox.utilities.import_guards import (
    ensure_no_active_import_file_mutation,
    ensure_utility_job_allowed_during_import,
)
from pullbox.utilities.job_log_api import (
    build_job_logs_download_response,
    build_job_logs_response,
)
from pullbox.utilities.job_queue import JobQueueManager  # noqa: TC001
from pullbox.utilities.job_responses import (
    job_detail_to_response,
    job_to_response,
    queue_status_to_response,
)
from pullbox.utilities.models import (
    JobState,
    JobType,
    UtilityJob,
    UtilityJobItem,
    UtilityJobLog,
)
from pullbox.utilities.preview_builders import (
    build_convert_preview_response,
    build_library_permissions_preview,
    build_mass_convert_preview,
    build_mass_rename_preview,
)
from pullbox.utilities.schemas import (
    ConvertPreviewRequest,
    ConvertPreviewResponse,
    DBCheckPreviewFinding,
    DBCheckPreviewRequest,
    DBCheckPreviewResponse,
    JobCreateRequest,
    JobDetailResponse,
    JobItemResponse,
    JobListResponse,
    JobLogListResponse,
    JobResponse,
    LibraryPermissionsPreviewRequest,
    LibraryPermissionsPreviewResponse,
    MassConvertPreviewRequest,
    MassConvertPreviewResponse,
    MassRenamePreviewRequest,
    MassRenamePreviewResponse,
    QueueStatusResponse,
)
from pullbox.utilities.settings import (
    cleanup_utility_trash_retention,
    empty_utility_trash,
    resolve_utility_directory,
)
from pullbox.utilities.sse import subscribe

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/utilities", tags=["utilities"], include_in_schema=False)

# Module-level reference — set during app startup
_queue_manager: JobQueueManager | None = None
_background_tasks: set[asyncio.Task[None]] = set()


async def _resolve_enabled_library_scan_root(session: DbSession, raw_path: str) -> Path:
    """Resolve a requested utility scan root under enabled library roots."""
    roots = await _load_enabled_library_roots(session)
    try:
        return resolve_path_inside_roots(raw_path, roots, require_dir=True)
    except ValueError as exc:
        raise ValidationError(str(exc)) from None


async def _load_enabled_library_roots(session: DbSession) -> list[Path]:
    """Load enabled library roots for utility path validation."""
    roots_result = await session.execute(
        select(LibraryRoot.path).where(LibraryRoot.enabled.is_(True))
    )
    roots = [Path(row[0]) for row in roots_result.all() if row[0]]
    if not roots:
        raise ValidationError("No enabled library roots are available.")
    return roots


def set_queue_manager(mgr: JobQueueManager) -> None:
    """Set the module-level queue manager reference (called at startup)."""
    global _queue_manager
    _queue_manager = mgr


def _get_manager() -> JobQueueManager:
    if _queue_manager is None:
        raise RuntimeError("JobQueueManager not initialized")
    return _queue_manager


def _schedule_dispatch(mgr: JobQueueManager) -> None:
    """Kick the serial utility queue after the current response completes."""

    async def _deferred_dispatch() -> None:
        try:
            await asyncio.sleep(0.1)
            await mgr.dispatch_next()
        except Exception:
            logger.exception("utility_dispatch_background_failed")

    task = asyncio.create_task(_deferred_dispatch())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _load_utility_config_values(
    session: DbSession,
    keys: Sequence[str],
) -> dict[str, str]:
    defaults = {key: DEFAULT_SYSTEM_CONFIG[key][0] for key in keys}
    result = await session.execute(select(SystemConfig).where(SystemConfig.key.in_(keys)))
    defaults.update({row.key: row.value for row in result.scalars().all()})
    return defaults


async def _resolve_utility_trash_context(session: DbSession) -> tuple[Path, int]:
    from pullbox.config import get_settings

    settings = get_settings()
    configs = await _load_utility_config_values(
        session,
        ["utility_trash_folder", "utility_trash_retention_days"],
    )
    trash_dir = resolve_utility_directory(
        db_value=configs.get("utility_trash_folder", ""),
        default_parent=settings.library_root,
        default_subdir=".trash",
        library_root=settings.library_root,
        data_dir=settings.data_dir,
    )
    retention_days = int(
        configs.get(
            "utility_trash_retention_days",
            DEFAULT_SYSTEM_CONFIG["utility_trash_retention_days"][0],
        )
    )
    return trash_dir, retention_days


async def _enforce_utility_trash_retention(session: DbSession) -> int:
    trash_dir, retention_days = await _resolve_utility_trash_context(session)
    return cleanup_utility_trash_retention(trash_dir, retention_days)


async def _delete_job_records(
    session: DbSession,
    job: UtilityJob,
) -> None:
    """Delete a job and all dependent records in a consistent order."""
    await session.execute(delete(UtilityJobLog).where(UtilityJobLog.job_id == job.id))
    await session.execute(delete(UtilityJobItem).where(UtilityJobItem.job_id == job.id))
    await session.delete(job)


# ── Job CRUD ───────────────────────────────────────────────────


@router.post("/jobs", response_model=JobResponse, status_code=201)
async def create_job(
    body: JobCreateRequest,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> JobResponse:
    """Submit a new utility job to the queue."""
    valid_types = {t.value for t in JobType}
    if body.job_type not in valid_types:
        raise ValidationError(
            f"Invalid job_type: {body.job_type}. Valid types: {', '.join(sorted(valid_types))}"
        )

    mgr = _get_manager()

    # Run executor-level config validation if registered
    executor = mgr.get_executor(body.job_type)
    if executor:
        errors = executor.validate_config(body.config)
        if errors:
            raise ValidationError(f"Invalid config: {'; '.join(errors)}")

    await ensure_utility_job_allowed_during_import(
        session,
        job_type=body.job_type,
        config=body.config,
    )
    await _enforce_utility_trash_retention(session)

    job = await mgr.create_job(
        session=session,
        job_type=body.job_type,
        display_name=body.display_name,
        config=body.config,
        created_by=_user.username,
    )

    _schedule_dispatch(mgr)

    return job_to_response(job)


@router.post("/trash/empty")
async def empty_trash(
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> dict[str, int | str]:
    """Delete all contents from the configured utility trash directory."""
    await ensure_no_active_import_file_mutation(session)
    trash_dir, _retention_days = await _resolve_utility_trash_context(session)
    trash_dir.mkdir(parents=True, exist_ok=True)
    deleted_entries = empty_utility_trash(trash_dir)
    return {
        "message": "Trash emptied.",
        "deleted_entries": deleted_entries,
    }


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    _user: InteractiveOperatorUser,
    session: DbSession,
    state: str | None = Query(None, description="Filter by state"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> JobListResponse:
    """List utility jobs with optional state filter and pagination."""
    query = select(UtilityJob)
    count_query = select(sa_func.count()).select_from(UtilityJob)

    if state:
        state_upper = state.upper()
        valid_states = {s.value for s in JobState}
        if state_upper not in valid_states:
            raise ValidationError(
                f"Invalid state: {state}. Valid states: {', '.join(sorted(valid_states))}"
            )
        query = query.where(UtilityJob.state == state_upper)
        count_query = count_query.where(UtilityJob.state == state_upper)

    total = (await session.execute(count_query)).scalar_one()

    result = await session.execute(
        query.order_by(UtilityJob.created_at.desc()).limit(limit).offset(offset)
    )
    jobs = list(result.scalars().all())

    return JobListResponse(
        jobs=[job_to_response(j) for j in jobs],
        total=total,
    )


@router.get("/jobs/{job_id}", response_model=JobDetailResponse)
async def get_job(
    job_id: str,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> JobDetailResponse:
    """Get full details for a specific job."""
    job = await session.get(UtilityJob, job_id)
    if not job:
        raise NotFoundError("UtilityJob", job_id)
    return job_detail_to_response(job)


@router.delete("/jobs/{job_id}", status_code=204)
async def delete_job(
    job_id: str,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> None:
    """Delete a job and its items/logs. Only terminal-state jobs can be deleted."""
    job = await session.get(UtilityJob, job_id)
    if not job:
        raise NotFoundError("UtilityJob", job_id)
    if not job.is_terminal:
        raise ValidationError(
            f"Cannot delete job in state {job.state}. "
            f"Only completed, failed, cancelled, or rolled-back jobs can be deleted."
        )
    await _delete_job_records(session, job)
    await session.flush()


@router.delete("/history")
async def clear_history(
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> dict[str, int]:
    """Delete all terminal utility jobs and their dependent records."""
    result = await session.execute(
        select(UtilityJob).where(
            UtilityJob.state.in_(
                [
                    JobState.COMPLETED,
                    JobState.FAILED,
                    JobState.CANCELLED,
                    JobState.ROLLED_BACK,
                ]
            )
        )
    )
    jobs = list(result.scalars().all())
    for job in jobs:
        await _delete_job_records(session, job)
    await session.flush()
    return {"deleted": len(jobs)}


# ── Job Items & Logs ───────────────────────────────────────────


@router.get("/jobs/{job_id}/items", response_model=list[JobItemResponse])
async def get_job_items(
    job_id: str,
    _user: InteractiveOperatorUser,
    session: DbSession,
    state: str | None = Query(None, description="Filter by item state"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[JobItemResponse]:
    """Get items for a job with optional state filter."""
    job = await session.get(UtilityJob, job_id)
    if not job:
        raise NotFoundError("UtilityJob", job_id)

    query = select(UtilityJobItem).where(UtilityJobItem.job_id == job_id)
    if state:
        query = query.where(UtilityJobItem.state == state.upper())
    query = query.order_by(UtilityJobItem.item_index).limit(limit).offset(offset)

    result = await session.execute(query)
    items = list(result.scalars().all())
    return [JobItemResponse.model_validate(item) for item in items]


@router.get("/jobs/{job_id}/logs", response_model=JobLogListResponse)
async def get_job_logs(
    job_id: str,
    _user: InteractiveOperatorUser,
    session: DbSession,
    level: str | None = Query(None, description="Filter by log level"),
    search: str | None = Query(None, description="Search log messages"),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> JobLogListResponse:
    """Get log entries for a job with optional level/search filtering."""
    return await build_job_logs_response(
        session=session,
        job_id=job_id,
        level=level,
        search=search,
        limit=limit,
        offset=offset,
    )


@router.get("/jobs/{job_id}/logs/download")
async def download_job_logs(
    job_id: str,
    _user: InteractiveOperatorUser,
    session: DbSession,
    level: str | None = Query(None, description="Filter by log level"),
    search: str | None = Query(None, description="Search log messages"),
) -> Response:
    """Download filtered job log entries as a JSON attachment."""
    return await build_job_logs_download_response(
        session=session,
        job_id=job_id,
        level=level,
        search=search,
        job_to_response=job_to_response,
    )


# ── Job Controls ───────────────────────────────────────────────


@router.post("/jobs/{job_id}/pause", status_code=200)
async def pause_job(
    job_id: str,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> dict[str, str]:
    """Pause a running job at the next batch boundary."""
    mgr = _get_manager()
    try:
        await mgr.pause_job(session, job_id)
    except ValueError as exc:
        raise ValidationError(str(exc)) from None
    return {"status": "pausing", "job_id": job_id}


@router.post("/jobs/{job_id}/resume", status_code=200)
async def resume_job(
    job_id: str,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> dict[str, str]:
    """Resume a paused job."""
    mgr = _get_manager()
    try:
        await mgr.resume_job(session, job_id)
    except ValueError as exc:
        raise ValidationError(str(exc)) from None

    _schedule_dispatch(mgr)

    return {"status": "queued", "job_id": job_id}


@router.post("/jobs/{job_id}/cancel", status_code=200)
async def cancel_job(
    job_id: str,
    _user: InteractiveOperatorUser,
    session: DbSession,
    rollback: bool = Query(False, description="Queue a rollback job after cancellation"),
) -> dict[str, str]:
    """Cancel a job. Optionally queue a rollback."""
    mgr = _get_manager()
    try:
        await mgr.cancel_job(session, job_id, rollback=rollback)
    except ValueError as exc:
        raise ValidationError(str(exc)) from None
    return {"status": "cancelling" if rollback else "cancelled", "job_id": job_id}


@router.post("/jobs/{job_id}/rollback", status_code=200)
async def rollback_job(
    job_id: str,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> dict[str, str]:
    """Queue a rollback child job for a completed or cancelled parent job."""
    mgr = _get_manager()
    try:
        await mgr.queue_rollback_job(session, job_id, created_by=_user.username)
    except ValueError as exc:
        raise ValidationError(str(exc)) from None

    _schedule_dispatch(mgr)

    return {"status": "queued", "job_id": job_id}


# ── Queue Status ───────────────────────────────────────────────


@router.get("/queue", response_model=QueueStatusResponse)
async def get_queue_status(
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> QueueStatusResponse:
    """Get current queue status counts."""
    result = await session.execute(
        select(UtilityJob.state, sa_func.count()).group_by(UtilityJob.state)
    )
    counts: dict[str, int] = {row[0]: row[1] for row in result.all()}

    return queue_status_to_response(counts)


# ── Converter Preview ──────────────────────────────────────────


@router.post("/convert/preview", response_model=ConvertPreviewResponse)
async def convert_preview(
    body: ConvertPreviewRequest,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> ConvertPreviewResponse:
    """Preview files that would be converted without submitting a job."""
    referenced_result = await session.execute(
        select(LibraryFile.file_path).where(
            LibraryFile.storage_mode == LibraryFileStorageMode.REFERENCED
        )
    )
    return build_convert_preview_response(
        body,
        allowed_roots=await _load_enabled_library_roots(session),
        excluded_paths=frozenset(referenced_result.scalars().all()),
    )


@router.post("/mass-convert/preview", response_model=MassConvertPreviewResponse)
async def mass_convert_preview(
    body: MassConvertPreviewRequest,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> MassConvertPreviewResponse:
    """Preview files that would be queued by the mass-convert workflow."""
    return await build_mass_convert_preview(
        body,
        session=session,
        load_trash_context=_resolve_utility_trash_context,
    )


@router.post("/permissions/preview", response_model=LibraryPermissionsPreviewResponse)
async def library_permissions_preview(
    body: LibraryPermissionsPreviewRequest,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> LibraryPermissionsPreviewResponse:
    """Preview recursive chmod scope before queueing the permissions job."""
    return await build_library_permissions_preview(body, session=session)


@router.post("/rename/preview", response_model=MassRenamePreviewResponse)
async def mass_rename_preview(
    body: MassRenamePreviewRequest,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> MassRenamePreviewResponse:
    """Preview proposed Mass Rename results using current naming settings."""
    return await build_mass_rename_preview(body, session=session)


@router.post("/db-check/preview", response_model=DBCheckPreviewResponse)
async def db_check_preview(
    body: DBCheckPreviewRequest,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> DBCheckPreviewResponse:
    """Preview DB cleanup findings before the execute step is queued."""
    from pullbox.services.db_check_service import build_referential_findings
    from pullbox.utilities.executors.db_check_cleanup import (
        DBCheckCleanupExecutor,
        detect_orphaned_records,
        detect_stale_files,
    )

    executor = DBCheckCleanupExecutor()
    errors = executor.validate_config({"checks": body.checks})
    if errors:
        raise ValidationError(f"Invalid config: {'; '.join(errors)}")

    checks = list(dict.fromkeys(body.checks))
    if any(check in checks for check in {"stale", "reindex"}) and not body.library_root:
        raise ValidationError(
            "library_root is required when stale-file or metadata refresh checks are selected."
        )

    findings: list[DBCheckPreviewFinding] = []

    if "orphans" in checks:
        orphan_result = await session.execute(
            select(LibraryFile.id, LibraryFile.file_path, LibraryFile.file_name)
        )
        orphan_candidates = [
            {
                "id": row.id,
                "file_path": row.file_path,
                "file_name": row.file_name,
            }
            for row in orphan_result.all()
        ]
        orphaned = detect_orphaned_records(orphan_candidates)
        for row in orphaned:
            file_path = str(row.get("file_path") or "")
            findings.append(
                DBCheckPreviewFinding(
                    finding_id=f"orphans-{row['id']}",
                    check_type="orphans",
                    record_id=int(row["id"]),
                    record_type="library_file",
                    file_path=file_path,
                    description=(
                        f"Orphaned library record: {Path(file_path).name} is missing from disk."
                    ),
                    suggested_action="delete",
                    allowed_actions=["delete", "skip"],
                )
            )

    if "stale" in checks and body.library_root:
        known_paths_result = await session.execute(select(LibraryFile.file_path))
        known_paths = {row[0] for row in known_paths_result.all()}
        scan_root = await _resolve_enabled_library_scan_root(session, body.library_root)
        stale_files = detect_stale_files(scan_root, known_paths)
        for idx, stale in enumerate(stale_files, start=1):
            file_path = str(stale.get("path") or "")
            findings.append(
                DBCheckPreviewFinding(
                    finding_id=f"stale-{idx}",
                    check_type="stale",
                    record_id=None,
                    record_type="file",
                    file_path=file_path,
                    description=(
                        f"Stale file on disk: {Path(file_path).name} "
                        "is not registered in the database."
                    ),
                    suggested_action="add",
                    allowed_actions=["add", "skip"],
                )
            )

    if "referential" in checks:
        for finding in await build_referential_findings(session):
            findings.append(DBCheckPreviewFinding(**finding))

    if "reindex" in checks and body.library_root:
        findings.append(
            DBCheckPreviewFinding(
                finding_id="reindex-1",
                check_type="reindex",
                record_id=None,
                record_type="library",
                file_path=body.library_root,
                description=(
                    "Refresh tracked file metadata and parsed fields for the selected root."
                ),
                suggested_action="reindex",
                allowed_actions=["reindex", "skip"],
                context={
                    "repair_kind": "reindex_root",
                    "target_root_path": body.library_root,
                },
            )
        )

    if "optimize" in checks:
        db_path = _sqlite_database_path(session)
        if db_path is None:
            raise ValidationError(
                "Database optimization is only available for file-backed SQLite databases."
            )
        preview = await asyncio.to_thread(DatabaseOptimizationService(db_path).preview)
        findings.append(
            DBCheckPreviewFinding(
                finding_id="optimize-database",
                check_type="optimize",
                record_id=None,
                record_type="database",
                description=(
                    f"Reclaim about {preview.reclaimable_bytes / (1024 * 1024):.1f} MB "
                    f"from {preview.free_pages:,} SQLite free pages."
                ),
                suggested_action="optimize",
                allowed_actions=["optimize", "skip"],
                context={
                    "reclaimable_bytes": preview.reclaimable_bytes,
                    "required_free_bytes": preview.required_free_bytes,
                    "available_free_bytes": preview.available_free_bytes,
                    "integrity_result": preview.integrity_result,
                },
            )
        )

    return DBCheckPreviewResponse(
        checks=checks,
        finding_count=len(findings),
        findings=findings,
    )


# ── SSE Stream ─────────────────────────────────────────────────


@router.get("/jobs/{job_id}/stream")
async def job_event_stream(
    job_id: str,
    _user: InteractiveOperatorUser,
) -> StreamingResponse:
    """SSE stream for real-time progress of a specific utility job."""

    async def event_generator() -> AsyncGenerator[str, None]:
        async with subscribe(f"utility:{job_id}") as queue:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    if event is None:
                        break
                    yield event.format_sse()
                except TimeoutError:
                    yield "event: heartbeat\ndata: {}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Helpers ────────────────────────────────────────────────────
