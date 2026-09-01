"""Confirmation workflow for import jobs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import func as sa_func
from sqlalchemy import select as sa_select

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.models.issue import Issue
from pullbox.services.import_managed_copy_preflight import (
    ManagedCopyPreflightError,
    reopen_review_after_managed_copy_preflight_failure,
    validate_managed_copy_preflight,
)
from pullbox.services.import_split_series import (
    require_preferred_managed_root_for_selected_split_series,
)
from pullbox.services.import_story_arc_review import confirm_import_story_arcs
from pullbox.services.import_workflow_state import initialize_progress_snapshot

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.schemas.import_job import ConfirmImportRequest

    RecomputeFileCountersFunc = Callable[..., Awaitable[None]]
    ApplyConfirmPolicyFunc = Callable[
        [AsyncSession, ImportJob, ConfirmImportRequest],
        Awaitable[None],
    ]
    LogEventFunc = Callable[..., Awaitable[None]]


class ApplyManualFileMatchFunc(Protocol):
    def __call__(
        self,
        session: AsyncSession,
        imp_file: ImportedFile,
        issue: Issue,
        *,
        method: str,
    ) -> Awaitable[Any]: ...


async def confirm_import_job(
    session: AsyncSession,
    job_id: int,
    request: ConfirmImportRequest,
    *,
    apply_manual_file_match: ApplyManualFileMatchFunc,
    recompute_file_counters: RecomputeFileCountersFunc,
    apply_confirm_policy: ApplyConfirmPolicyFunc,
    log_event: LogEventFunc,
) -> ImportJob:
    """Confirm selected import-review rows and transition the job to importing."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)

    if job.status != ImportJobStatus.REVIEW:
        raise ValidationError(f"Job must be in REVIEW state to confirm (current: {job.status})")

    affected_series_ids: set[int] = set()

    await _apply_conflict_resolutions(session, job_id, request, affected_series_ids)
    await _apply_file_overrides(
        session,
        job_id,
        request,
        affected_series_ids,
        apply_manual_file_match=apply_manual_file_match,
    )

    if affected_series_ids:
        await recompute_file_counters(
            session,
            job,
            series_ids=sorted(affected_series_ids),
        )

    items = await _load_confirmed_series(session, job_id, request)

    await require_preferred_managed_root_for_selected_split_series(
        session,
        job,
        preferred_library_root_id=(
            request.target_library_root_id
            if request.target_library_root_id is not None
            else job.target_library_root_id
        ),
    )

    for item in items:
        item.status = ImportSeriesStatus.CONFIRMED
        item.selected_for_import = False
    await session.flush()

    await _confirm_matched_files(session, items, affected_series_ids)

    if affected_series_ids:
        await recompute_file_counters(
            session,
            job,
            series_ids=sorted(affected_series_ids),
        )

    await apply_confirm_policy(session, job, request)

    duplicate_selected_count = await _count_selected_duplicate_files(session, job_id)
    confirmed_story_arc_count = await confirm_import_story_arcs(
        session,
        job_id,
        story_arc_ids=request.story_arc_ids,
        decisions=[
            (
                decision.imported_story_arc_id,
                decision.action,
                decision.proposed_story_arc_id,
            )
            for decision in request.story_arc_decisions
        ],
    )
    if not items and duplicate_selected_count == 0 and confirmed_story_arc_count == 0:
        raise ValidationError(
            "Select at least one matched series, duplicate importable file, or story arc "
            "before importing"
        )

    try:
        capacity_snapshot = await validate_managed_copy_preflight(
            session,
            job,
            stage="confirmation",
        )
    except ManagedCopyPreflightError as exc:
        await reopen_review_after_managed_copy_preflight_failure(session, job, exc)
        raise

    job.status = ImportJobStatus.IMPORTING
    job.control_request = ImportControlRequest.NONE
    progress_snapshot = initialize_progress_snapshot(
        job,
        mode="import",
        phase="queued",
        progress=0,
        message="Preparing the selected import items...",
        status=ImportJobStatus.IMPORTING,
    )
    if capacity_snapshot is not None:
        progress_snapshot["managed_copy_capacity"] = capacity_snapshot.as_dict()
    job.progress_snapshot = progress_snapshot
    await session.flush()

    await log_event(
        session,
        job_id,
        "INFO",
        "import_confirmed",
        message=(
            f"User confirmed {len(items)} series and "
            f"{duplicate_selected_count} duplicate-series files and "
            f"{confirmed_story_arc_count} story arcs for import"
        ),
        confirmed_count=len(items),
        duplicate_file_count=duplicate_selected_count,
        story_arc_count=confirmed_story_arc_count,
    )

    return job


async def _load_confirmed_series(
    session: AsyncSession,
    job_id: int,
    request: ConfirmImportRequest,
) -> list[ImportedSeries]:
    persisted_result = await session.execute(
        sa_select(ImportedSeries).where(
            ImportedSeries.import_job_id == job_id,
            ImportedSeries.selected_for_import.is_(True),
        )
    )
    persisted_items = list(persisted_result.scalars().all())

    if not persisted_items and request.series_ids:
        compatibility_result = await session.execute(
            sa_select(ImportedSeries).where(
                ImportedSeries.import_job_id == job_id,
                ImportedSeries.id.in_(request.series_ids),
                ImportedSeries.status == ImportSeriesStatus.MATCHED,
            )
        )
        persisted_items = list(compatibility_result.scalars().all())
        for item in persisted_items:
            item.selected_for_import = True
        await session.flush()

    valid_items: list[ImportedSeries] = []
    invalid_items: list[ImportedSeries] = []
    has_unresolved_conflicts = False
    for item in persisted_items:
        if item.status == ImportSeriesStatus.MATCHED and (item.files_conflict or 0) == 0:
            valid_items.append(item)
            continue
        if (item.files_conflict or 0) > 0:
            has_unresolved_conflicts = True
        invalid_items.append(item)

    for item in invalid_items:
        item.selected_for_import = False

    if invalid_items:
        await session.flush()

    if has_unresolved_conflicts:
        raise ValidationError("Resolve file conflicts before importing")

    return valid_items


async def _apply_conflict_resolutions(
    session: AsyncSession,
    job_id: int,
    request: ConfirmImportRequest,
    affected_series_ids: set[int],
) -> None:
    if not request.conflict_resolutions:
        return

    resolution_map = {r.conflict_group_id: r.chosen_file_id for r in request.conflict_resolutions}
    conflict_result = await session.execute(
        sa_select(ImportedFile).where(
            ImportedFile.import_job_id == job_id,
            ImportedFile.conflict_group_id.in_(resolution_map.keys()),
            ImportedFile.status == ImportedFileStatus.CONFLICT,
        )
    )
    conflict_files = conflict_result.scalars().all()
    for conflict_file in conflict_files:
        affected_series_ids.add(conflict_file.import_series_id)
        conflict_group_id = conflict_file.conflict_group_id
        if conflict_group_id is not None and conflict_file.id == resolution_map.get(
            conflict_group_id
        ):
            conflict_file.status = ImportedFileStatus.MATCHED
            conflict_file.is_preferred = True
            conflict_file.include_in_import = True
        else:
            conflict_file.status = ImportedFileStatus.SKIPPED
            conflict_file.is_preferred = False
            conflict_file.include_in_import = False
        conflict_file.conflict_group_id = None
    await session.flush()


async def _apply_file_overrides(
    session: AsyncSession,
    job_id: int,
    request: ConfirmImportRequest,
    affected_series_ids: set[int],
    *,
    apply_manual_file_match: ApplyManualFileMatchFunc,
) -> None:
    if not request.file_overrides:
        return

    for override in request.file_overrides:
        override_file = await session.get(ImportedFile, override.imported_file_id)
        if override_file is None or override_file.import_job_id != job_id:
            raise NotFoundError("ImportedFile", override.imported_file_id)
        issue = await session.get(Issue, override.issue_id)
        if issue is None:
            raise NotFoundError("Issue", override.issue_id)
        await apply_manual_file_match(
            session,
            override_file,
            issue,
            method="manual_override",
        )
        affected_series_ids.add(override_file.import_series_id)


async def _confirm_matched_files(
    session: AsyncSession,
    items: list[ImportedSeries],
    affected_series_ids: set[int],
) -> None:
    confirmed_series_ids = [item.id for item in items]
    if not confirmed_series_ids:
        return

    matched_result = await session.execute(
        sa_select(ImportedFile).where(
            ImportedFile.import_series_id.in_(confirmed_series_ids),
            ImportedFile.status == ImportedFileStatus.MATCHED,
        )
    )
    for matched_file in matched_result.scalars().all():
        matched_file.status = ImportedFileStatus.CONFIRMED
        affected_series_ids.add(matched_file.import_series_id)
    await session.flush()


async def _count_selected_duplicate_files(session: AsyncSession, job_id: int) -> int:
    return int(
        await session.scalar(
            sa_select(sa_func.count(ImportedFile.id))
            .join(ImportedSeries, ImportedFile.import_series_id == ImportedSeries.id)
            .where(
                ImportedFile.import_job_id == job_id,
                ImportedSeries.status == ImportSeriesStatus.DUPLICATE,
                ImportedFile.include_in_import.is_(True),
                ImportedFile.status.in_([ImportedFileStatus.MATCHED, ImportedFileStatus.CONFIRMED]),
            )
        )
        or 0
    )
