"""Import job creation helpers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import select as sa_select

from pullbox.core.exceptions import ConfigurationError, ValidationError
from pullbox.core.library_file_ownership import (
    ReferencedFileValidationError,
    resolve_referenced_source_root,
)
from pullbox.core.library_layout import resolve_source_layout_spec
from pullbox.core.library_policy import (
    load_effective_library_ingest_policy,
    load_library_ingest_policy,
    load_search_on_add_default,
)
from pullbox.core.library_root_resolution import resolve_library_root
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportFileHandlingMode,
    ImportJob,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.services.import_mylar3_path_preflight import Mylar3PathPreflightAnalyzer
from pullbox.services.import_mylar3_path_validation import validate_mylar3_path_map_targets
from pullbox.services.import_policy_snapshot import apply_ingest_policy_to_import_job
from pullbox.services.import_root_policy_activation import (
    apply_future_root_policy_to_ingest_policy,
    build_future_root_policy_snapshot,
)
from pullbox.services.import_workflow_state import (
    ACTIVE_IMPORT_JOB_STATUSES,
    initialize_progress_snapshot,
)
from pullbox.services.library_root_management import validate_managed_library_root

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.library import LibraryRoot
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
    if (
        request.file_handling_mode == ImportFileHandlingMode.IN_PLACE
        and request.source_type == ImportSourceType.FILESYSTEM
    ):
        try:
            await resolve_referenced_source_root(
                session,
                Path(request.source_path),
                None,
            )
        except ReferencedFileValidationError as exc:
            raise ValidationError(exc.message) from exc

    target_root: LibraryRoot | None = None
    requires_managed_destination = (
        request.file_handling_mode == ImportFileHandlingMode.MANAGED_COPY
        or request.target_library_root_id is not None
        or request.future_layout_requested
    )
    if requires_managed_destination:
        try:
            target_root = await resolve_library_root(
                session,
                Path(request.source_path),
                request.target_library_root_id,
            )
        except ConfigurationError as exc:
            raise ValidationError(exc.message) from exc
    if target_root is not None:
        await validate_managed_library_root(target_root)
    resolved_target_library_root_id = target_root.id if target_root is not None else None
    if request.future_layout_requested and resolved_target_library_root_id is None:
        raise ValidationError("Future library layout requires a target library root.")
    if request.source_type == ImportSourceType.MYLAR3 and request.mylar3_path_map_confirmed:
        await validate_mylar3_path_map_targets(
            session,
            request.mylar3_path_map,
            file_handling_mode=request.file_handling_mode,
        )
        from pullbox.schemas.import_mylar3_path_preflight import MylarPathMappingDraft

        try:
            preview = await Mylar3PathPreflightAnalyzer().analyze(
                session,
                request.source_path,
                auto_detect=False,
                file_handling_mode=request.file_handling_mode,
                mappings=[
                    MylarPathMappingDraft(
                        stored_prefix=stored_prefix,
                        pullbox_prefix=pullbox_prefix,
                    )
                    for stored_prefix, pullbox_prefix in request.mylar3_path_map.items()
                ],
            )
        except (OSError, ValueError) as exc:
            raise ValidationError(
                "The confirmed Mylar path mapping could not be revalidated."
            ) from exc
        if not preview.can_confirm or preview.path_map != request.mylar3_path_map:
            raise ValidationError(
                "The confirmed Mylar path mapping preview is blocked. Analyze the paths again."
            )

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
    baseline_ingest_policy = (
        await load_effective_library_ingest_policy(
            session,
            resolved_target_library_root_id,
        )
        if resolved_target_library_root_id is not None
        else await load_library_ingest_policy(session)
    )
    future_root_policy_snapshot = (
        build_future_root_policy_snapshot(
            request.future_root_policy.model_dump(mode="json"),
            baseline_ingest_policy,
        )
        if request.future_root_policy is not None
        else None
    )
    ingest_policy = (
        apply_future_root_policy_to_ingest_policy(
            baseline_ingest_policy,
            future_root_policy_snapshot,
        )
        if future_root_policy_snapshot is not None
        else baseline_ingest_policy
    )
    source_layout_snapshot = resolve_source_layout_spec(request.source_layout.to_core()).to_dict()

    job = ImportJob(
        source_path=request.source_path,
        selected_file_paths=list(request.file_paths or []),
        source_type=request.source_type,
        status=ImportJobStatus.PENDING,
        monitored=monitored,
        search_on_add=search_on_add,
        target_library_root_id=resolved_target_library_root_id,
        mylar3_path_map=request.mylar3_path_map,
        mylar3_path_map_confirmed=request.mylar3_path_map_confirmed,
        cv_match_threshold=request.cv_match_threshold,
        min_files_per_series=request.min_files_per_series,
        file_formats=request.file_formats,
        progress_snapshot={},
        progress_revision=0,
        control_request=ImportControlRequest.NONE,
        file_handling_mode=request.file_handling_mode,
        source_layout_snapshot=source_layout_snapshot,
        future_layout_requested=request.future_layout_requested,
        future_root_policy_snapshot=future_root_policy_snapshot,
        future_root_policy_applied_at=None,
        story_arc_import_requested=request.story_arc_import_requested,
        story_arc_materialization_requested=request.story_arc_materialization_requested,
    )
    apply_ingest_policy_to_import_job(job, ingest_policy)
    session.add(job)
    await session.flush()
    if future_root_policy_snapshot is not None:
        apply_ingest_policy_to_import_job(
            job,
            apply_future_root_policy_to_ingest_policy(
                baseline_ingest_policy,
                future_root_policy_snapshot,
                source_import_job_id=job.id,
            ),
        )
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
