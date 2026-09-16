"""Import workflow counter helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func as sa_func
from sqlalchemy import select as sa_select

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportSeriesStatus,
)
from pullbox.services.import_duplicates import is_duplicate_series

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def job_stats(job: ImportJob) -> dict[str, int]:
    """Extract stat counters from an ImportJob for SSE progress events."""
    return {
        "scan_total_files": job.scan_total_files,
        "scan_total_dirs": job.scan_total_dirs,
        "series_found": job.series_found,
        "series_duplicate": job.series_duplicate,
        "series_matched": job.series_matched,
        "series_no_match": job.series_no_match,
        "series_new": job.series_new,
        "series_imported": job.series_imported,
        "series_failed": job.series_failed,
        "total_files_found": job.total_files_found,
        "total_files_matched": job.total_files_matched,
        "total_files_duplicate": job.total_files_duplicate,
        "total_files_already_owned": job.total_files_already_owned,
        "total_files_conflict": job.total_files_conflict,
        "total_files_no_match": job.total_files_no_match,
        "total_files_imported": job.total_files_imported,
        "total_files_failed": job.total_files_failed,
    }


async def recompute_series_counters(session: AsyncSession, job: ImportJob) -> None:
    """Rebuild per-job series counters from persisted ImportedSeries rows."""
    series_status_result = await session.execute(
        sa_select(ImportedSeries.status, sa_func.count(ImportedSeries.id))
        .where(ImportedSeries.import_job_id == job.id)
        .group_by(ImportedSeries.status)
    )
    series_counts = {status: count for status, count in series_status_result.all()}
    job.series_found = sum(series_counts.values())
    job.series_duplicate = series_counts.get(ImportSeriesStatus.DUPLICATE, 0)
    job.series_matched = series_counts.get(ImportSeriesStatus.MATCHED, 0)
    job.series_no_match = series_counts.get(ImportSeriesStatus.NO_MATCH, 0)
    job.series_imported = series_counts.get(ImportSeriesStatus.IMPORTED, 0)
    job.series_failed = series_counts.get(ImportSeriesStatus.FAILED, 0)
    job.series_new = max(job.series_found - job.series_duplicate, 0)


async def recompute_file_counters(
    session: AsyncSession,
    job: ImportJob,
    *,
    series_ids: list[int] | None = None,
) -> None:
    """Rebuild per-series and per-job file counters from persisted ImportedFile rows."""
    series_filters = [ImportedFile.import_job_id == job.id]
    if series_ids:
        series_filters.append(ImportedFile.import_series_id.in_(series_ids))

    totals_result = await session.execute(
        sa_select(
            ImportedFile.import_series_id,
            sa_func.count(ImportedFile.id),
        )
        .where(*series_filters)
        .group_by(ImportedFile.import_series_id)
    )
    totals_by_series = {series_id: count for series_id, count in totals_result.all()}

    status_result = await session.execute(
        sa_select(
            ImportedFile.import_series_id,
            ImportedFile.status,
            sa_func.count(ImportedFile.id),
        )
        .where(*series_filters)
        .group_by(ImportedFile.import_series_id, ImportedFile.status)
    )
    status_by_series: dict[int, dict[ImportedFileStatus, int]] = {}
    for series_id, status, count in status_result.all():
        status_by_series.setdefault(series_id, {})[status] = count

    series_query = sa_select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
    if series_ids:
        series_query = series_query.where(ImportedSeries.id.in_(series_ids))
    series_rows = list((await session.execute(series_query)).scalars().all())

    for series_item in series_rows:
        counts = status_by_series.get(series_item.id, {})
        series_item.files_total = totals_by_series.get(series_item.id, 0)
        series_item.files_matched = counts.get(ImportedFileStatus.MATCHED, 0) + counts.get(
            ImportedFileStatus.CONFIRMED,
            0,
        )
        series_item.files_duplicate = counts.get(ImportedFileStatus.DUPLICATE_FILE, 0)
        series_item.files_already_owned = counts.get(ImportedFileStatus.ALREADY_OWNED, 0)
        series_item.files_conflict = counts.get(ImportedFileStatus.CONFLICT, 0)
        series_item.files_no_match = counts.get(ImportedFileStatus.NO_MATCH, 0)
        series_item.files_imported = counts.get(ImportedFileStatus.IMPORTED, 0)
        series_item.files_failed = counts.get(ImportedFileStatus.FAILED, 0)

        safety_blocked = counts.get(ImportedFileStatus.SAFETY_BLOCKED, 0) + counts.get(
            ImportedFileStatus.SAFETY_APPROVED,
            0,
        )
        diagnostics = dict(series_item.diagnostics or {})
        if safety_blocked:
            diagnostics["safety_blocked_files"] = safety_blocked
        else:
            diagnostics.pop("safety_blocked_files", None)
        series_item.diagnostics = diagnostics

        if is_duplicate_series(series_item):
            diagnostics = dict(series_item.diagnostics or {})
            actionable_duplicate_merge = bool(
                series_item.files_matched > 0
                or series_item.files_conflict > 0
                or diagnostics.get("actionable_duplicate_merge", False)
            )
            diagnostics.update(
                {
                    "actionable_duplicate_merge": actionable_duplicate_merge,
                    "has_importable_files": series_item.files_matched > 0,
                    "importable_files": series_item.files_matched,
                    "duplicate_files": series_item.files_duplicate,
                    "already_owned_files": series_item.files_already_owned,
                    "no_match_files": series_item.files_no_match,
                    "conflict_files": series_item.files_conflict,
                }
            )
            series_item.diagnostics = diagnostics

    job_status_result = await session.execute(
        sa_select(ImportedFile.status, sa_func.count(ImportedFile.id))
        .where(ImportedFile.import_job_id == job.id)
        .group_by(ImportedFile.status)
    )
    job_status_counts = {status: count for status, count in job_status_result.all()}
    job.total_files_matched = job_status_counts.get(ImportedFileStatus.MATCHED, 0) + (
        job_status_counts.get(ImportedFileStatus.CONFIRMED, 0)
    )
    job.total_files_duplicate = job_status_counts.get(ImportedFileStatus.DUPLICATE_FILE, 0)
    job.total_files_already_owned = job_status_counts.get(ImportedFileStatus.ALREADY_OWNED, 0)
    job.total_files_conflict = job_status_counts.get(ImportedFileStatus.CONFLICT, 0)
    job.total_files_no_match = job_status_counts.get(ImportedFileStatus.NO_MATCH, 0)
    job.total_files_imported = job_status_counts.get(ImportedFileStatus.IMPORTED, 0)
    job.total_files_failed = job_status_counts.get(ImportedFileStatus.FAILED, 0)
    total_files_safety_blocked = job_status_counts.get(
        ImportedFileStatus.SAFETY_BLOCKED,
        0,
    ) + job_status_counts.get(ImportedFileStatus.SAFETY_APPROVED, 0)
    classified_file_count = (
        job.total_files_matched
        + job.total_files_duplicate
        + job.total_files_already_owned
        + job.total_files_conflict
        + job.total_files_no_match
        + job.total_files_imported
        + job.total_files_failed
        + total_files_safety_blocked
    )
    job.total_files_found = max(
        int(job.total_files_found or 0),
        int(job.scan_total_files or 0),
        classified_file_count,
    )

    await session.flush()
