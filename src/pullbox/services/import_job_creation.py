"""Import job creation helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import select as sa_select

from pullbox.core.exceptions import ValidationError
from pullbox.core.library_layout import ImportLayoutMode, resolve_source_layout_spec
from pullbox.core.library_policy import load_library_ingest_policy, load_search_on_add_default
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportFileHandlingMode,
    ImportJob,
    ImportJobStatus,
)
from pullbox.services.import_policy_snapshot import apply_ingest_policy_to_import_job
from pullbox.services.import_workflow_state import (
    ACTIVE_IMPORT_JOB_STATUSES,
    initialize_progress_snapshot,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.schemas.import_job import ImportJobCreate


class ImportEventLogger(Protocol):
    """Callable contract for writing structured import-job events."""

    def __call__(
        self,
        session: AsyncSession,
        job_id: int,
        level: str,
        event: str,
        message: str | None = None,
        **kwargs: Any,
    ) -> Awaitable[None]: ...


async def create_job(
    session: AsyncSession,
    request: ImportJobCreate,
    *,
    log_event: ImportEventLogger,
) -> ImportJob:
    """Create an import job record without starting scan execution."""
    if request.file_handling_mode == ImportFileHandlingMode.IN_PLACE:
        raise ValidationError("In-place import is not available yet.")
    if request.source_layout.mode != ImportLayoutMode.AUTO:
        raise ValidationError("Selected source layouts are not available yet.")
    if request.future_layout_requested:
        raise ValidationError("Future library layout is not available yet.")

    active_job_id = await session.scalar(
        sa_select(ImportJob.id)
        .where(ImportJob.status.in_(ACTIVE_IMPORT_JOB_STATUSES))
        .order_by(ImportJob.created_at.desc())
        .limit(1)
    )
    if active_job_id is not None:
        raise ValidationError(
            "Only one import can be active at a time. "
            "Finish, discard, or roll back the current import first."
        )

    search_on_add = await load_search_on_add_default(session)
    if request.search_on_add is not None and request.search_on_add != search_on_add:
        raise ValidationError("Search on add is now controlled by the global import policy.")

    monitored = request.monitored or search_on_add
    ingest_policy = await load_library_ingest_policy(session)
    source_layout_snapshot = resolve_source_layout_spec(request.source_layout.to_core()).to_dict()

    job = ImportJob(
        source_path=request.source_path,
        selected_file_paths=list(request.file_paths or []),
        source_type=request.source_type,
        status=ImportJobStatus.PENDING,
        monitored=monitored,
        search_on_add=search_on_add,
        target_library_root_id=request.target_library_root_id,
        mylar3_path_map=request.mylar3_path_map,
        cv_match_threshold=request.cv_match_threshold,
        min_files_per_series=request.min_files_per_series,
        file_formats=request.file_formats,
        progress_snapshot={},
        progress_revision=0,
        control_request=ImportControlRequest.NONE,
        file_handling_mode=request.file_handling_mode,
        source_layout_snapshot=source_layout_snapshot,
        future_layout_requested=request.future_layout_requested,
        future_root_policy_snapshot=(
            request.future_root_policy.model_dump(mode="json")
            if request.future_root_policy is not None
            else None
        ),
        future_root_policy_applied_at=None,
    )
    apply_ingest_policy_to_import_job(job, ingest_policy)
    session.add(job)
    await session.flush()
    job.progress_snapshot = initialize_progress_snapshot(
        job,
        mode="scan",
        phase="inventory",
        progress=0,
        message="Preparing scan inventory...",
        status=ImportJobStatus.PENDING,
    )

    await log_event(
        session,
        job.id,
        "INFO",
        "import_job_created",
        message=f"Import job created for {request.source_type.value} source",
        source_path=request.source_path,
        selected_file_count=len(request.file_paths or []),
    )
    return job
