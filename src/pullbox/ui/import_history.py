"""Import workspace history context helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, String, case, cast, func, or_, select

from pullbox.models.import_job import ImportedSeries, ImportJob, ImportJobStatus, ImportSeriesStatus
from pullbox.services.import_workflow_state import (
    import_control_state_for_job,
    snapshot_mode_for_job,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_IMPORT_HISTORY_CLEARABLE_STATUSES = (
    ImportJobStatus.COMPLETED,
    ImportJobStatus.FAILED,
    ImportJobStatus.CANCELLED,
    ImportJobStatus.ROLLED_BACK,
)

_IMPORT_HISTORY_PER_PAGE = 25
_IMPORT_HISTORY_LIMIT = _IMPORT_HISTORY_PER_PAGE
_IMPORT_HISTORY_SORT_OPTIONS = {
    "source_path",
    "source_type",
    "status",
    "series_found",
    "series_imported",
    "series_failed",
    "series_no_match",
    "created_at",
}


def _history_resume_step_for_job(job: ImportJob) -> int | None:
    """Return the explicit collection step that should reopen this history job."""
    if job.status == ImportJobStatus.REVIEW and job.import_started_at is None:
        return 3

    mode = snapshot_mode_for_job(job)
    if job.status in {ImportJobStatus.PAUSED, ImportJobStatus.STALLED}:
        if mode in {"import", "rollback"} or job.import_started_at is not None:
            return 4
        return 2

    if (
        job.status
        in {
            ImportJobStatus.PENDING,
            ImportJobStatus.SCANNING,
            ImportJobStatus.PAUSING,
            ImportJobStatus.ANALYZING,
            ImportJobStatus.MATCHING,
            ImportJobStatus.FILE_MATCHING,
        }
        and job.import_started_at is None
    ):
        return 2

    if job.status in {
        ImportJobStatus.IMPORTING,
        ImportJobStatus.CANCELLING,
        ImportJobStatus.ROLLING_BACK,
    }:
        return 4

    return None


def _normalize_import_history_sort(sort: str | None) -> str:
    """Return a safe import history sort key."""
    if not sort:
        return "-created_at"
    field = sort.lstrip("-")
    if field not in _IMPORT_HISTORY_SORT_OPTIONS:
        return "-created_at"
    return f"-{field}" if sort.startswith("-") else field


def _get_import_history_order_by(sort: str) -> list[ColumnElement[object]]:
    """Build stable order clauses for import history sorting."""
    normalized_sort = _normalize_import_history_sort(sort)
    sort_desc = normalized_sort.startswith("-")
    sort_field = normalized_sort.lstrip("-")

    status_sort = case(
        (ImportJob.status == ImportJobStatus.PENDING, 0),
        (ImportJob.status == ImportJobStatus.SCANNING, 1),
        (ImportJob.status == ImportJobStatus.ANALYZING, 2),
        (ImportJob.status == ImportJobStatus.MATCHING, 3),
        (ImportJob.status == ImportJobStatus.FILE_MATCHING, 4),
        (ImportJob.status == ImportJobStatus.REVIEW, 5),
        (ImportJob.status == ImportJobStatus.IMPORTING, 6),
        (ImportJob.status == ImportJobStatus.STALLED, 7),
        (ImportJob.status == ImportJobStatus.COMPLETED, 8),
        (ImportJob.status == ImportJobStatus.FAILED, 9),
        (ImportJob.status == ImportJobStatus.CANCELLED, 10),
        else_=99,
    )

    sort_map: dict[str, list[object]] = {
        "source_path": [ImportJob.source_path],
        "source_type": [cast(ImportJob.source_type, String), ImportJob.created_at],
        "status": [status_sort, ImportJob.created_at],
        "series_found": [ImportJob.series_found],
        "series_imported": [ImportJob.series_imported],
        "series_failed": [ImportJob.series_failed],
        "series_no_match": [ImportJob.series_no_match],
        "created_at": [ImportJob.created_at],
    }
    sort_columns = sort_map.get(sort_field, [ImportJob.created_at])

    order_by: list[ColumnElement[object]] = []
    for col in sort_columns:
        column: ColumnElement[object] = col  # type: ignore[assignment]
        order_by.append(column.desc().nullslast() if sort_desc else column.asc().nullslast())
    order_by.append(ImportJob.id.desc())  # type: ignore[arg-type]
    return order_by


async def _load_import_history_context(
    session: AsyncSession,
    *,
    search_query: str = "",
    sort: str = "",
    requested_page: int = 1,
    show_archived: bool = False,
) -> dict[str, object]:
    """Load the import history page context."""
    archive_filter = (
        ImportJob.archived_at.is_not(None) if show_archived else ImportJob.archived_at.is_(None)
    )
    clearable_jobs_total = 0
    if not show_archived:
        clearable_jobs_total = int(
            (
                await session.execute(
                    select(func.count(ImportJob.id)).where(
                        ImportJob.status.in_(_IMPORT_HISTORY_CLEARABLE_STATUSES),
                        archive_filter,
                    )
                )
            ).scalar_one()
            or 0
        )

    normalized_search = (search_query or "").strip()
    normalized_sort = _normalize_import_history_sort(sort)
    history_filters: list[ColumnElement[bool]] = [archive_filter]
    if normalized_search:
        search_pattern = f"%{normalized_search}%"
        history_filters.append(
            or_(
                ImportJob.source_path.ilike(search_pattern),
                cast(ImportJob.status, String).ilike(search_pattern),
                cast(ImportJob.source_type, String).ilike(search_pattern),
            )
        )
    jobs_stmt = select(ImportJob).where(*history_filters)

    total_jobs = int(
        (
            await session.execute(select(func.count(ImportJob.id)).where(*history_filters))
        ).scalar_one()
        or 0
    )
    total_pages = max(1, (total_jobs + _IMPORT_HISTORY_PER_PAGE - 1) // _IMPORT_HISTORY_PER_PAGE)
    page = min(max(1, requested_page), total_pages)
    offset = (page - 1) * _IMPORT_HISTORY_PER_PAGE

    active_statuses = (
        ImportJobStatus.PENDING,
        ImportJobStatus.SCANNING,
        ImportJobStatus.PAUSING,
        ImportJobStatus.ANALYZING,
        ImportJobStatus.MATCHING,
        ImportJobStatus.FILE_MATCHING,
        ImportJobStatus.IMPORTING,
        ImportJobStatus.STALLED,
        ImportJobStatus.CANCELLING,
        ImportJobStatus.ROLLING_BACK,
    )
    resumable_clause = or_(
        ImportJob.status == ImportJobStatus.PAUSED,
        ImportJob.status == ImportJobStatus.STALLED,
        ((ImportJob.status == ImportJobStatus.REVIEW) & ImportJob.import_started_at.is_(None)),
    )
    results_ready_statuses = (
        ImportJobStatus.COMPLETED,
        ImportJobStatus.FAILED,
    )
    stats_result = await session.execute(
        select(
            func.coalesce(
                func.sum(case((ImportJob.status.in_(active_statuses), 1), else_=0)),
                0,
            ),
            func.coalesce(func.sum(case((resumable_clause, 1), else_=0)), 0),
            func.coalesce(
                func.sum(case((ImportJob.status.in_(results_ready_statuses), 1), else_=0)),
                0,
            ),
        ).where(*history_filters)
    )
    active_count, resumable_count, results_ready_count = stats_result.one()

    jobs_result = await session.execute(
        jobs_stmt.order_by(*_get_import_history_order_by(normalized_sort))
        .limit(_IMPORT_HISTORY_PER_PAGE)
        .offset(offset)
    )
    jobs = list(jobs_result.scalars().all())
    job_history_metrics = {
        job.id: {
            "series_found": int(job.series_found or 0),
            "series_imported": int(job.series_imported or 0),
            "series_failed": int(job.series_failed or 0),
            "series_no_match": int(job.series_no_match or 0),
        }
        for job in jobs
    }
    terminal_result_statuses = {
        ImportJobStatus.COMPLETED,
        ImportJobStatus.FAILED,
        ImportJobStatus.CANCELLED,
        ImportJobStatus.ROLLED_BACK,
    }
    live_history_statuses = {
        ImportJobStatus.PENDING,
        ImportJobStatus.SCANNING,
        ImportJobStatus.PAUSING,
        ImportJobStatus.ANALYZING,
        ImportJobStatus.MATCHING,
        ImportJobStatus.FILE_MATCHING,
        ImportJobStatus.IMPORTING,
        ImportJobStatus.STALLED,
        ImportJobStatus.CANCELLING,
        ImportJobStatus.ROLLING_BACK,
    }
    if jobs:
        job_ids = [job.id for job in jobs]
        series_counts_result = await session.execute(
            select(
                ImportedSeries.import_job_id,
                ImportedSeries.status,
                func.count(ImportedSeries.id),
            )
            .where(ImportedSeries.import_job_id.in_(job_ids))
            .group_by(ImportedSeries.import_job_id, ImportedSeries.status)
        )
        series_counts_by_job: dict[int, dict[ImportSeriesStatus, int]] = {}
        for import_job_id, series_status, count in series_counts_result.all():
            series_counts_by_job.setdefault(import_job_id, {})[series_status] = int(count)

        for job in jobs:
            if job.status not in terminal_result_statuses:
                continue
            series_counts = series_counts_by_job.get(job.id)
            if not series_counts:
                continue
            metrics = job_history_metrics[job.id]
            metrics["series_found"] = sum(series_counts.values())
            metrics["series_imported"] = int(series_counts.get(ImportSeriesStatus.IMPORTED, 0))
            metrics["series_failed"] = int(series_counts.get(ImportSeriesStatus.FAILED, 0))
            metrics["series_no_match"] = int(
                series_counts.get(
                    ImportSeriesStatus.NO_MATCH,
                    metrics["series_no_match"],
                )
            )
    job_control_states = {job.id: import_control_state_for_job(job) for job in jobs}
    job_resume_steps = {job.id: _history_resume_step_for_job(job) for job in jobs}
    return {
        "jobs": jobs,
        "job_history_metrics": job_history_metrics,
        "job_control_states": job_control_states,
        "job_resume_steps": job_resume_steps,
        "history_has_live_jobs": any(job.status in live_history_statuses for job in jobs),
        "history_stats": {
            "active": int(active_count or 0),
            "resumable": int(resumable_count or 0),
            "results_ready": int(results_ready_count or 0),
        },
        "total_jobs": total_jobs,
        "page": page,
        "total_pages": total_pages,
        "clearable_jobs_total": clearable_jobs_total,
        "search_query": normalized_search,
        "sort": normalized_sort,
        "show_archived": show_archived,
    }
