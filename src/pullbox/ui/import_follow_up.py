"""Job-grouped context for import follow-up work."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import and_, func, or_, select

from pullbox.core.exceptions import NotFoundError
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.models.series import IssueCatalogState, Series
from pullbox.models.story_arc import ImportedStoryArcStatus
from pullbox.models.story_arc_import import ImportedStoryArc
from pullbox.ui.import_orphaned_routes import load_import_orphaned_context
from pullbox.ui.import_results_context import load_import_results_context

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import Select
    from sqlalchemy.sql.elements import ColumnElement


_FOLLOW_UP_PAGE_SIZE = 25
_FOLLOW_UP_JOB_STATUSES = (ImportJobStatus.COMPLETED, ImportJobStatus.FAILED)
_FOLLOW_UP_SERIES_STATUSES = (
    ImportSeriesStatus.NO_MATCH,
    ImportSeriesStatus.RECOVERY_PENDING,
    ImportSeriesStatus.FAILED,
)
_FOLLOW_UP_FILE_STATUSES = (
    ImportedFileStatus.NO_MATCH,
    ImportedFileStatus.FAILED,
    ImportedFileStatus.SAFETY_BLOCKED,
    ImportedFileStatus.CONFLICT,
)
_VERIFIED_CROSS_FOLDER_METHODS = (
    "verified_cross_folder_issue_identity",
    "verified_cross_folder_series_issue_filename",
)


def _follow_up_job_filter() -> ColumnElement[bool]:
    misplaced = ImportedFile.diagnostics["mylar3_cross_folder_reconciliation"]
    cleanup = ImportedFile.diagnostics["misplaced_source_cleanup"]
    unresolved_series = (
        select(ImportedSeries.id)
        .where(
            ImportedSeries.import_job_id == ImportJob.id,
            ImportedSeries.status.in_(_FOLLOW_UP_SERIES_STATUSES),
        )
        .exists()
    )
    unresolved_files = (
        select(ImportedFile.id)
        .where(
            ImportedFile.import_job_id == ImportJob.id,
            ImportedFile.status.in_(_FOLLOW_UP_FILE_STATUSES),
        )
        .exists()
    )
    misplaced_files = (
        select(ImportedFile.id)
        .where(
            ImportedFile.import_job_id == ImportJob.id,
            misplaced["method"].as_string().in_(_VERIFIED_CROSS_FOLDER_METHODS),
            or_(
                and_(
                    ImportedFile.status == ImportedFileStatus.IMPORTED,
                    misplaced["role"].as_string() == "canonical",
                    misplaced["restored_at"].as_string().is_(None),
                    ImportedFile.library_file_id.is_not(None),
                ),
                and_(
                    ImportedFile.status == ImportedFileStatus.DUPLICATE_FILE,
                    misplaced["role"].as_string() == "identical_duplicate",
                    cleanup["action"].as_string().is_(None),
                    ImportedFile.duplicate_of_file_id.is_not(None),
                    ImportedFile.content_hash.is_not(None),
                ),
            ),
        )
        .exists()
    )
    unresolved_story_arcs = (
        select(ImportedStoryArc.id)
        .where(
            ImportedStoryArc.import_job_id == ImportJob.id,
            ImportedStoryArc.materialized_story_arc_id.is_(None),
            ImportedStoryArc.status != ImportedStoryArcStatus.SKIPPED,
        )
        .exists()
    )
    failed_metadata = (
        select(ImportedSeries.id)
        .join(Series, Series.id == ImportedSeries.series_id)
        .where(
            ImportedSeries.import_job_id == ImportJob.id,
            ImportedSeries.status == ImportSeriesStatus.IMPORTED,
            Series.issue_catalog_state == IssueCatalogState.FAILED,
        )
        .exists()
    )
    return or_(
        unresolved_series,
        unresolved_files,
        misplaced_files,
        unresolved_story_arcs,
        failed_metadata,
    )


def _follow_up_jobs_statement() -> Select[tuple[ImportJob]]:
    return select(ImportJob).where(
        ImportJob.status.in_(_FOLLOW_UP_JOB_STATUSES),
        ImportJob.archived_at.is_(None),
        _follow_up_job_filter(),
    )


async def count_import_follow_up_jobs(session: AsyncSession) -> int:
    """Return the number of imports with actionable follow-up."""
    return int(
        (
            await session.scalar(
                select(func.count()).select_from(_follow_up_jobs_statement().subquery())
            )
        )
        or 0
    )


async def _count_dismissed_import_series(session: AsyncSession) -> int:
    return int(
        (
            await session.scalar(
                select(func.count())
                .select_from(ImportedSeries)
                .join(ImportJob, ImportedSeries.import_job_id == ImportJob.id)
                .where(
                    ImportedSeries.status == ImportSeriesStatus.SKIPPED,
                    ImportJob.status == ImportJobStatus.COMPLETED,
                )
            )
        )
        or 0
    )


async def load_import_follow_up_context(
    session: AsyncSession,
    *,
    view: str,
    requested_page: int,
    job_id: int | None,
) -> dict[str, object]:
    """Load either grouped import jobs or one job's actionable follow-up."""
    normalized_view = "dismissed" if view == "dismissed" else "all"
    follow_up_job_count = await count_import_follow_up_jobs(session)

    if normalized_view == "dismissed":
        orphaned = await load_import_orphaned_context(
            session,
            view="dismissed",
            requested_page=requested_page,
        )
        return {
            **orphaned,
            "selected_follow_up_job": None,
            "follow_up_jobs": [],
            "follow_up_job_count": follow_up_job_count,
        }

    if job_id is not None:
        job = await session.get(ImportJob, job_id)
        if job is None or job.status not in _FOLLOW_UP_JOB_STATUSES or job.archived_at is not None:
            raise NotFoundError("ImportJob", job_id)
        orphaned = await load_import_orphaned_context(
            session,
            view="all",
            requested_page=requested_page,
            job_id=job_id,
        )
        return {
            **orphaned,
            **await load_import_results_context(
                session,
                job,
                include_clean_library=False,
            ),
            "selected_follow_up_job": job,
            "follow_up_jobs": [],
            "follow_up_job_count": follow_up_job_count,
        }

    total_pages = max(
        1,
        (follow_up_job_count + _FOLLOW_UP_PAGE_SIZE - 1) // _FOLLOW_UP_PAGE_SIZE,
    )
    page = min(max(1, requested_page), total_pages)
    jobs = list(
        (
            await session.scalars(
                _follow_up_jobs_statement()
                .order_by(ImportJob.created_at.desc(), ImportJob.id.desc())
                .offset((page - 1) * _FOLLOW_UP_PAGE_SIZE)
                .limit(_FOLLOW_UP_PAGE_SIZE)
            )
        ).all()
    )
    return {
        "items": [],
        "total": follow_up_job_count,
        "page": page,
        "page_size": _FOLLOW_UP_PAGE_SIZE,
        "view": "all",
        "orphaned_count": 0,
        "dismissed_count": await _count_dismissed_import_series(session),
        "total_pages": total_pages,
        "selected_follow_up_job": None,
        "follow_up_jobs": jobs,
        "follow_up_job_count": follow_up_job_count,
    }
