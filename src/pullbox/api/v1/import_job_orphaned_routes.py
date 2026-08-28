"""Import orphaned/recovery API routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from pullbox.api.deps import AuthenticatedUser, DbSession  # noqa: TC001
from pullbox.core.exceptions import ProviderError
from pullbox.schemas.import_job import (
    AssignOrphanRequest,
    AssignOrphanResponse,
    ImportedSeriesRead,
    OrphanedSeriesResponse,
    OrphanRecoveryProgressResponse,
    OrphanRecoveryResponse,
    RecoverOrphanRequest,
    RecoverOrphanResponse,
)
from pullbox.services.secondary_operation_progress import (
    project_orphan_recovery_operation_progress,
)
from pullbox.tasks.import_orphan_recovery_task import (
    get_orphan_recovery_progress_state,
    start_orphan_recovery_run,
)

router = APIRouter()


async def _build_orphan_import_service(session: Any) -> Any:
    """Build ImportService with dependencies for orphaned review actions."""
    from pullbox.composition.services import build_import_service

    return await build_import_service(session)


def _make_import_service() -> Any:
    """Create a lightweight ImportService for orphaned control operations."""
    from pullbox.composition.services import build_import_control_service

    return build_import_control_service()


@router.get("/orphaned", response_model=OrphanedSeriesResponse)
async def list_orphaned_series(
    _user: AuthenticatedUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    sort: str = Query(
        "file_count_desc",
        description="Sort: file_count_desc, date_found_desc, series_name_asc",
    ),
) -> OrphanedSeriesResponse:
    """Paginated list of active unmatched series from completed import jobs."""
    service = _make_import_service()
    items, total = await service.get_orphaned_series(session, page, page_size, sort)

    return OrphanedSeriesResponse(
        items=[ImportedSeriesRead.model_validate(i) for i in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/orphaned/count")
async def get_orphaned_count(
    _user: AuthenticatedUser,
    session: DbSession,
) -> dict[str, int]:
    """Lightweight count of orphaned series for the sidebar badge."""
    service = _make_import_service()
    count = await service.get_orphaned_count(session)
    return {"count": count}


@router.post(
    "/orphaned/{imported_series_id}/assign",
    response_model=AssignOrphanResponse,
)
async def assign_orphan(
    imported_series_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
    body: AssignOrphanRequest,
) -> AssignOrphanResponse:
    """Assign a ComicVine series to an orphaned import row and start recovery."""
    service = await _build_orphan_import_service(session)

    try:
        item = await service.assign_cv_to_orphan(session, imported_series_id, body.cv_id)
    except ProviderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await session.commit()
    return AssignOrphanResponse(
        imported_series_id=item.id,
        status=item.status,
        cv_title=item.cv_title or item.raw_series_name,
        recovery_required=True,
        files_remaining=item.files_total,
    )


@router.get(
    "/orphaned/{imported_series_id}/recovery",
    response_model=OrphanRecoveryResponse,
)
async def get_orphan_recovery(
    imported_series_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> OrphanRecoveryResponse:
    """Return the guided recovery context for one unmatched import row."""
    service = await _build_orphan_import_service(session)
    payload = await service.get_orphan_recovery_context(session, imported_series_id)
    return OrphanRecoveryResponse(
        imported_series=ImportedSeriesRead.model_validate(payload["imported_series"]),
        issue_options=payload["issue_options"],
        files=payload["files"],
        requires_library_root=bool(payload["requires_library_root"]),
        selected_library_root_id=payload["selected_library_root_id"],
        available_library_roots=payload["available_library_roots"],
        files_remaining=int(payload["files_remaining"]),
        files_completed=int(payload["files_completed"]),
    )


@router.post(
    "/orphaned/{imported_series_id}/recover",
    response_model=RecoverOrphanResponse,
)
async def recover_orphan(
    imported_series_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
    body: RecoverOrphanRequest,
) -> RecoverOrphanResponse:
    """Finalize one guided unmatched recovery pass."""
    service = await _build_orphan_import_service(session)
    payload = await service.recover_orphan(session, imported_series_id, body)
    await session.commit()
    return RecoverOrphanResponse(**payload)


@router.post(
    "/orphaned/{imported_series_id}/recover/start",
    response_model=OrphanRecoveryProgressResponse,
    status_code=202,
)
async def start_orphan_recovery(
    imported_series_id: int,
    _user: AuthenticatedUser,
    body: RecoverOrphanRequest,
    session: DbSession,
) -> OrphanRecoveryProgressResponse:
    """Start a background orphan recovery run and return its initial progress state."""
    progress = await start_orphan_recovery_run(imported_series_id, body)
    await project_orphan_recovery_operation_progress(session, progress)
    await session.commit()
    return progress


@router.get(
    "/orphaned/{imported_series_id}/recover/progress",
    response_model=OrphanRecoveryProgressResponse,
)
async def get_orphan_recovery_progress(
    imported_series_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> OrphanRecoveryProgressResponse:
    """Return the latest live progress snapshot for an orphan recovery run."""
    progress = get_orphan_recovery_progress_state(imported_series_id)
    if progress is not None:
        await project_orphan_recovery_operation_progress(session, progress)
        await session.commit()
        return progress
    return OrphanRecoveryProgressResponse(
        imported_series_id=imported_series_id,
        state="idle",
        message="Recovery has not started.",
    )


@router.post("/orphaned/{imported_series_id}/dismiss", status_code=204)
async def dismiss_orphan(
    imported_series_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> None:
    """Mark an orphaned series as SKIPPED so it no longer appears."""
    service = _make_import_service()
    await service.dismiss_orphan(session, imported_series_id)
    await session.commit()
