"""Post-import orphaned-series and failed-series recovery helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import and_, or_
from sqlalchemy import func as sa_func
from sqlalchemy import select as sa_select

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.models.series import Series
from pullbox.services.import_counters import recompute_file_counters, recompute_series_counters
from pullbox.services.import_file_resolution import load_issue_lookup_for_series
from pullbox.services.import_job_actions import build_series_created_action_payload
from pullbox.services.import_job_execution_items import (
    ensure_target_issue_summary_for_import_file,
)
from pullbox.services.import_review_recheck import prepare_retryable_failed_sources_for_retry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import Select
    from sqlalchemy.sql.elements import ColumnElement

    from pullbox.models.issue import Issue
    from pullbox.providers.base import SeriesMetadata
    from pullbox.schemas.import_job import OrphanRecoveryDecision, RecoverOrphanRequest

    ProgressCallback = Callable[..., Awaitable[None] | None]
    ProcessSeriesFilesFunc = Callable[..., Awaitable[tuple[int, int]]]
    RecordActionFunc = Callable[..., Awaitable[Any]]
    RecomputeFileCountersFunc = Callable[..., Awaitable[None]]
    RecomputeSeriesCountersFunc = Callable[..., Awaitable[None]]


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


class MetadataServiceLike(Protocol):
    """Subset of MetadataService used by delayed orphan assignment."""

    async def get_series_metadata(
        self,
        comicvine_id: int,
    ) -> SeriesMetadata: ...


class RecoverySeriesServiceLike(Protocol):
    """Subset of SeriesService needed to create the target library series."""

    async def add_from_comicvine(
        self,
        session: AsyncSession,
        comicvine_id: int,
        library_root_id: int | None = None,
        *,
        search_on_add: bool = False,
    ) -> Series: ...


_ACTIVE_ORPHAN_STATUSES = (
    ImportSeriesStatus.NO_MATCH,
    ImportSeriesStatus.RECOVERY_PENDING,
)


def _active_issue_recovery_clause() -> ColumnElement[bool]:
    """Return the query clause for issue-only follow-up under imported series."""
    return and_(
        ImportedSeries.status == ImportSeriesStatus.IMPORTED,
        sa_select(ImportedFile.id)
        .where(
            ImportedFile.import_series_id == ImportedSeries.id,
            ImportedFile.status == ImportedFileStatus.NO_MATCH,
        )
        .exists(),
    )


def _active_orphan_clause() -> ColumnElement[bool]:
    """Return the combined query clause for all active unmatched follow-up rows."""
    return or_(
        ImportedSeries.status.in_(_ACTIVE_ORPHAN_STATUSES),
        _active_issue_recovery_clause(),
    )


def is_active_orphan_row(item: ImportedSeries | None) -> bool:
    """Return True when a row should appear in the active Unmatched queue."""
    return bool(
        item is not None
        and (
            item.status in _ACTIVE_ORPHAN_STATUSES
            or (item.status == ImportSeriesStatus.IMPORTED and int(item.files_no_match or 0) > 0)
        )
    )


def apply_orphan_recovery_decisions(
    *,
    item: ImportedSeries,
    files: list[ImportedFile],
    decisions: list[OrphanRecoveryDecision],
    cv_id_to_issue: dict[int, Issue],
) -> None:
    """Apply delayed unmatched-recovery file decisions to import file rows."""
    files_by_id = {imp_file.id: imp_file for imp_file in files}
    for decision in decisions:
        imp_file = files_by_id.get(decision.imported_file_id)
        if imp_file is None or imp_file.import_series_id != item.id:
            raise NotFoundError("ImportedFile", decision.imported_file_id)

        if decision.action == "skip":
            imp_file.status = ImportedFileStatus.SKIPPED
            imp_file.matched_issue_id = None
            imp_file.matched_issue_cv_id = None
            imp_file.match_confidence = None
            imp_file.match_method = "orphan_recovery_skip"
            imp_file.error_message = None
            imp_file.diagnostics = {
                **dict(imp_file.diagnostics or {}),
                "kind": "orphan_recovery",
                "resolution": "skipped",
            }
            continue

        if decision.issue_cv_id is None:
            raise ValidationError("Assigned recovery decisions require a ComicVine issue.")
        issue = cv_id_to_issue.get(decision.issue_cv_id)
        if issue is None:
            raise ValidationError(
                f"ComicVine issue {decision.issue_cv_id} is not available for this series."
            )

        imp_file.status = ImportedFileStatus.MATCHED
        imp_file.matched_issue_id = issue.id
        imp_file.matched_issue_cv_id = issue.comicvine_id
        imp_file.match_confidence = "manual"
        imp_file.match_method = "orphan_recovery"
        imp_file.error_message = None
        imp_file.diagnostics = {
            **dict(imp_file.diagnostics or {}),
            "kind": "orphan_recovery",
            "resolution": "assigned",
        }


def summarize_orphan_recovery_result(
    *,
    item: ImportedSeries,
    files: list[ImportedFile],
) -> dict[str, Any]:
    """Summarize delayed orphan recovery after file processing finishes."""
    imported_count = sum(1 for imp_file in files if imp_file.status == ImportedFileStatus.IMPORTED)
    skipped_count = sum(1 for imp_file in files if imp_file.status == ImportedFileStatus.SKIPPED)
    failed_count = sum(1 for imp_file in files if imp_file.status == ImportedFileStatus.FAILED)
    files_remaining = sum(
        1
        for imp_file in files
        if imp_file.status not in {ImportedFileStatus.IMPORTED, ImportedFileStatus.SKIPPED}
    )

    item.status = (
        ImportSeriesStatus.IMPORTED
        if imported_count > 0 and files_remaining == 0
        else ImportSeriesStatus.RECOVERY_PENDING
    )
    return {
        "imported_series_id": item.id,
        "status": item.status,
        "series_id": item.series_id,
        "imported_count": imported_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "files_remaining": files_remaining,
    }


async def recover_orphan(
    session: AsyncSession,
    job: ImportJob,
    item: ImportedSeries,
    request: RecoverOrphanRequest,
    *,
    series_service: RecoverySeriesServiceLike,
    process_series_files: ProcessSeriesFilesFunc,
    record_action: RecordActionFunc,
    recompute_file_counters: RecomputeFileCountersFunc,
    recompute_series_counters: RecomputeSeriesCountersFunc,
    log_event: ImportEventLogger,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Create/reuse the local series and import selected files for delayed recovery."""
    job_id = job.id
    item_id = item.id
    has_identified_series = (
        item.status == ImportSeriesStatus.RECOVERY_PENDING and item.cv_id is not None
    ) or (item.status == ImportSeriesStatus.IMPORTED and item.series_id is not None)
    if not has_identified_series:
        raise ValidationError("Choose a ComicVine match before saving recovery decisions.")

    files_result = await session.execute(
        sa_select(ImportedFile)
        .where(ImportedFile.import_series_id == item.id)
        .order_by(ImportedFile.id.asc())
    )
    files = list(files_result.scalars().all())
    active_files = [
        imp_file
        for imp_file in files
        if imp_file.status not in {ImportedFileStatus.IMPORTED, ImportedFileStatus.SKIPPED}
    ]
    decision_by_id = {decision.imported_file_id: decision for decision in request.decisions}

    missing_ids = [imp_file.id for imp_file in active_files if imp_file.id not in decision_by_id]
    if missing_ids:
        raise ValidationError("Every remaining file needs an assign or skip decision.")

    target_library_root_id = job.target_library_root_id or request.target_library_root_id
    if item.series_id is None and target_library_root_id is None:
        raise ValidationError("Choose a library root before completing recovery.")
    if item.series_id is None and job.target_library_root_id is None:
        job.target_library_root_id = target_library_root_id

    has_existing_import = any(imp_file.status == ImportedFileStatus.IMPORTED for imp_file in files)
    assign_count = sum(1 for decision in request.decisions if decision.action == "assign")
    if assign_count == 0 and not has_existing_import:
        raise ValidationError("Assign at least one file before completing recovery.")

    if item.series_id is not None:
        series = await session.get(Series, item.series_id)
        if series is None:
            raise ValidationError("The target library series for this recovery no longer exists.")
    else:
        if item.cv_id is None:
            raise ValidationError("Choose a ComicVine match before saving recovery decisions.")

        existing_series_id = await session.scalar(
            sa_select(Series.id).where(Series.comicvine_id == item.cv_id)
        )
        series = await series_service.add_from_comicvine(
            session,
            item.cv_id,
            library_root_id=target_library_root_id,
            search_on_add=job.search_on_add,
        )
        item.series_id = series.id
        if existing_series_id is None:
            await record_action(
                session,
                job,
                phase="import",
                action_type="series_created",
                payload=await build_series_created_action_payload(
                    session,
                    series_id=series.id,
                    import_series_id=item.id,
                ),
            )

    cv_id_to_issue, _, _ = await load_issue_lookup_for_series(session, series.id)
    apply_orphan_recovery_decisions(
        item=item,
        files=files,
        decisions=request.decisions,
        cv_id_to_issue=cv_id_to_issue,
    )

    await process_series_files(
        session,
        job,
        item,
        report_file_progress=progress_callback,
    )
    refreshed_job = await session.get(ImportJob, job_id)
    if refreshed_job is None:
        raise NotFoundError("ImportJob", job_id)
    refreshed_item = await session.get(ImportedSeries, item_id)
    if refreshed_item is None:
        raise NotFoundError("ImportedSeries", item_id)
    job = refreshed_job
    item = refreshed_item
    await recompute_file_counters(session, job, series_ids=[item.id])
    await recompute_series_counters(session, job)

    remaining_result = await session.execute(
        sa_select(ImportedFile)
        .where(ImportedFile.import_series_id == item.id)
        .order_by(ImportedFile.id.asc())
    )
    refreshed_files = list(remaining_result.scalars().all())
    recovery_summary = summarize_orphan_recovery_result(item=item, files=refreshed_files)
    await log_event(
        session,
        job.id,
        "INFO",
        "import_orphan_recovery_saved",
        message=f"Recovered unmatched series '{item.raw_series_name}'",
        imported_series_id=item.id,
        series_id=item.series_id,
        imported_count=recovery_summary["imported_count"],
        skipped_count=recovery_summary["skipped_count"],
        failed_count=recovery_summary["failed_count"],
        files_remaining=recovery_summary["files_remaining"],
    )

    return recovery_summary


def _orphaned_series_query() -> Select[tuple[ImportedSeries]]:
    """Build the shared unresolved-orphan filter used by list and count queries."""
    return (
        sa_select(ImportedSeries)
        .join(ImportJob, ImportedSeries.import_job_id == ImportJob.id)
        .where(
            _active_orphan_clause(),
            ImportJob.status == ImportJobStatus.COMPLETED,
        )
    )


async def get_orphaned_series(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 25,
    sort: str = "file_count_desc",
) -> tuple[list[ImportedSeries], int]:
    """Return paginated active orphaned import series from completed jobs."""
    count_q = (
        sa_select(sa_func.count())
        .select_from(ImportedSeries)
        .join(ImportJob, ImportedSeries.import_job_id == ImportJob.id)
        .where(
            _active_orphan_clause(),
            ImportJob.status == ImportJobStatus.COMPLETED,
        )
    )
    total_result = await session.execute(count_q)
    total = total_result.scalar() or 0

    query = _orphaned_series_query()
    if sort == "series_name_asc":
        query = query.order_by(ImportedSeries.raw_series_name.asc())
    elif sort == "date_found_desc":
        query = query.order_by(ImportedSeries.created_at.desc())
    else:
        query = query.order_by(
            ImportedSeries.files_no_match.desc(),
            ImportedSeries.file_count.desc(),
        )

    offset = (page - 1) * page_size
    result = await session.execute(query.offset(offset).limit(page_size))
    return list(result.scalars().all()), total


async def get_orphaned_count(session: AsyncSession) -> int:
    """Return total count of active orphaned series from completed jobs."""
    count_q = (
        sa_select(sa_func.count())
        .select_from(ImportedSeries)
        .join(ImportJob, ImportedSeries.import_job_id == ImportJob.id)
        .where(
            _active_orphan_clause(),
            ImportJob.status == ImportJobStatus.COMPLETED,
        )
    )
    result = await session.execute(count_q)
    return result.scalar() or 0


async def assign_cv_to_orphan(
    session: AsyncSession,
    imported_series_id: int,
    cv_id: int,
    *,
    metadata_service: MetadataServiceLike,
    log_event: ImportEventLogger,
) -> ImportedSeries:
    """Persist a chosen ComicVine series on an orphaned row and start recovery."""
    item = await session.get(ImportedSeries, imported_series_id)
    if item is None:
        raise NotFoundError("ImportedSeries", imported_series_id)

    if item.status == ImportSeriesStatus.SKIPPED:
        raise ValidationError("Dismissed unmatched rows cannot be recovered.")

    if item.status == ImportSeriesStatus.IMPORTED and int(item.files_no_match or 0) > 0:
        raise ValidationError(
            "This queue item already knows its series. "
            "Open recovery directly to finish file decisions."
        )

    if item.files_imported > 0:
        raise ValidationError(
            "This unmatched group already imported files and cannot change series matches."
        )

    try:
        metadata = await metadata_service.get_series_metadata(cv_id)
        item.status = ImportSeriesStatus.RECOVERY_PENDING
        item.cv_id = cv_id
        item.cv_title = metadata.title
        item.cv_year = metadata.year_start
        item.cv_publisher = metadata.publisher
        item.cv_issue_count = metadata.issue_count
        item.cv_url = metadata.comicvine_url
        item.user_selected_cv_id = cv_id
        await session.flush()

        await log_event(
            session,
            item.import_job_id,
            "INFO",
            "import_orphan_recovery_started",
            message=f"Orphan '{item.raw_series_name}' assigned CV {cv_id} for recovery",
            imported_series_id=imported_series_id,
            cv_id=cv_id,
            cv_title=metadata.title,
        )
        return item

    except Exception:
        await log_event(
            session,
            item.import_job_id,
            "ERROR",
            "import_orphan_assign_failed",
            message=f"Failed to assign CV {cv_id} to '{item.raw_series_name}'",
            imported_series_id=imported_series_id,
            cv_id=cv_id,
        )
        raise


async def dismiss_orphan(
    session: AsyncSession,
    imported_series_id: int,
    *,
    log_event: ImportEventLogger,
) -> None:
    """Mark an unresolved orphan as skipped so it no longer appears."""
    item = await session.get(ImportedSeries, imported_series_id)
    if item is None:
        raise NotFoundError("ImportedSeries", imported_series_id)

    if item.status == ImportSeriesStatus.IMPORTED and int(item.files_no_match or 0) > 0:
        job = await session.get(ImportJob, item.import_job_id)
        if job is None:
            raise NotFoundError("ImportJob", item.import_job_id)

        result = await session.execute(
            sa_select(ImportedFile).where(
                ImportedFile.import_series_id == imported_series_id,
                ImportedFile.status == ImportedFileStatus.NO_MATCH,
            )
        )
        skipped_files = list(result.scalars().all())
        for imp_file in skipped_files:
            imp_file.status = ImportedFileStatus.SKIPPED
            imp_file.matched_issue_id = None
            imp_file.matched_issue_cv_id = None
            imp_file.match_confidence = None
            imp_file.match_method = "orphan_recovery_skip"
            imp_file.error_message = None
            imp_file.diagnostics = {
                **dict(imp_file.diagnostics or {}),
                "kind": "orphan_recovery",
                "resolution": "skipped",
            }

        await recompute_file_counters(session, job, series_ids=[item.id])
        await recompute_series_counters(session, job)
        await session.flush()

        await log_event(
            session,
            item.import_job_id,
            "INFO",
            "import_orphan_dismissed",
            message=f"Dismissed unresolved files for '{item.raw_series_name}'",
            imported_series_id=imported_series_id,
            skipped_file_count=len(skipped_files),
        )
        return

    item.status = ImportSeriesStatus.SKIPPED
    await session.flush()

    await log_event(
        session,
        item.import_job_id,
        "INFO",
        "import_orphan_dismissed",
        message=f"Orphan '{item.raw_series_name}' dismissed",
        imported_series_id=imported_series_id,
    )


async def retry_failed_series(
    session: AsyncSession,
    job_id: int,
    *,
    log_event: ImportEventLogger,
) -> tuple[ImportJob, int]:
    """Reset failed import rows/files for re-execution."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)

    if job.status != ImportJobStatus.COMPLETED:
        raise ValidationError(f"Job must be in COMPLETED state to retry (current: {job.status})")

    source_recheck = await prepare_retryable_failed_sources_for_retry(session, job)

    result = await session.execute(
        sa_select(ImportedSeries).where(
            ImportedSeries.import_job_id == job_id,
            ImportedSeries.status == ImportSeriesStatus.FAILED,
        )
    )
    failed_items = list(result.scalars().all())

    failed_file_result = await session.execute(
        sa_select(ImportedSeries, ImportedFile)
        .join(ImportedFile, ImportedFile.import_series_id == ImportedSeries.id)
        .where(
            ImportedSeries.import_job_id == job_id,
            ImportedSeries.status.in_(
                [
                    ImportSeriesStatus.DUPLICATE,
                    ImportSeriesStatus.IMPORTED,
                ]
            ),
            ImportedFile.status == ImportedFileStatus.FAILED,
        )
    )
    failed_file_items = [
        item
        for item, imp_file in failed_file_result.all()
        if not isinstance(dict(imp_file.diagnostics or {}).get("source_revalidation"), dict)
    ]
    retry_items_by_id = {item.id: item for item in [*failed_items, *failed_file_items]}
    retry_items = list(retry_items_by_id.values())

    if not retry_items:
        if source_recheck["files_checked"] > 0:
            await session.flush()
            await log_event(
                session,
                job_id,
                "WARNING",
                "import_retry_failed_source_revalidation_blocked",
                message="No failed files passed source revalidation",
                source_files_checked=source_recheck["files_checked"],
                source_files_blocked=source_recheck["blocked_files"],
            )
            return job, 0
        raise ValidationError("No failed series or files to retry")

    retry_series_ids = [item.id for item in retry_items]
    for item in retry_items:
        if item.status in {ImportSeriesStatus.FAILED, ImportSeriesStatus.IMPORTED}:
            item.status = ImportSeriesStatus.CONFIRMED
        item.error_message = None

    failed_files_result = await session.execute(
        sa_select(ImportedFile).where(
            ImportedFile.import_series_id.in_(retry_series_ids),
            ImportedFile.status == ImportedFileStatus.FAILED,
        )
    )
    failed_files = [
        imp_file
        for imp_file in failed_files_result.scalars().all()
        if not isinstance(dict(imp_file.diagnostics or {}).get("source_revalidation"), dict)
    ]
    for imp_file in failed_files:
        imp_file.status = ImportedFileStatus.CONFIRMED
        imp_file.include_in_import = True
        imp_file.error_message = None

    retry_file_result = await session.execute(
        sa_select(ImportedFile).where(
            ImportedFile.import_series_id.in_(retry_series_ids),
            ImportedFile.status.in_([ImportedFileStatus.MATCHED, ImportedFileStatus.CONFIRMED]),
        )
    )
    repaired_target_count = 0
    for imp_file in retry_file_result.scalars().all():
        had_summary = isinstance(dict(imp_file.diagnostics or {}).get("target_issue_summary"), dict)
        if ensure_target_issue_summary_for_import_file(imp_file):
            if not had_summary and isinstance(
                dict(imp_file.diagnostics or {}).get("target_issue_summary"), dict
            ):
                repaired_target_count += 1
            continue
        imp_file.status = ImportedFileStatus.NO_MATCH
        imp_file.include_in_import = False

    count = len(retry_items)
    job.status = ImportJobStatus.IMPORTING
    await recompute_file_counters(session, job, series_ids=retry_series_ids)
    await recompute_series_counters(session, job)
    await session.flush()

    await log_event(
        session,
        job_id,
        "INFO",
        "import_retry_failed_started",
        message=f"Retrying {count} failed import item{'s' if count != 1 else ''}",
        retry_count=count,
        retry_file_count=len(failed_files),
        repaired_target_count=repaired_target_count,
    )

    return job, count
