"""Review recorded Mylar disagreements left behind in an existing library series."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import and_, select, update

from pullbox.core.exceptions import ValidationError
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue
from pullbox.services.import_file_selection import set_review_file_selection

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

    from pullbox.schemas.import_job import RecoverOrphanRequest


def conflict_file() -> ColumnElement[bool]:
    return and_(
        ImportedFile.status == ImportedFileStatus.NO_MATCH,
        ImportedFile.library_file_id.is_(None),
        ImportedFile.diagnostics["conflict_type"].as_string() == "mylar_native_identity_conflict",
    )


def mylar_duplicate(job: ImportJob, item: ImportedSeries) -> bool:
    return (
        job.source_type == ImportSourceType.MYLAR3
        and item.status == ImportSeriesStatus.DUPLICATE
        and item.cv_match_method == "mylar3_cv_id"
        and item.series_id is not None
    )


async def review_rows(
    session: AsyncSession, item: ImportedSeries
) -> list[tuple[ImportedFile, int | None]]:
    query = (
        select(ImportedFile, Issue.id)
        .outerjoin(
            Issue,
            and_(
                Issue.series_id == item.series_id,
                Issue.comicvine_id == ImportedFile.comicvine_issue_id,
            ),
        )
        .where(ImportedFile.import_series_id == item.id, conflict_file())
        .order_by(ImportedFile.id)
    )
    return [(file, issue_id) for file, issue_id in (await session.execute(query)).all()]


async def skip_conflicts(
    session: AsyncSession, job: ImportJob, item: ImportedSeries, request: RecoverOrphanRequest
) -> int:
    ids = {d.imported_file_id for d in request.decisions if d.action == "skip"}
    if not ids or len(ids) != len(request.decisions):
        raise ValidationError(
            "Select the conflicting files to skip. They cannot be assigned from this review."
        )
    # Start the caller's write transaction on SQLite and lock the job on PostgreSQL.
    # A SAVEPOINT before the first SQLite write could commit the skips on release.
    claimed = await session.scalar(
        update(ImportJob)
        .where(
            ImportJob.id == job.id,
            ImportJob.source_type == ImportSourceType.MYLAR3,
            ImportJob.status == ImportJobStatus.COMPLETED,
            ImportJob.control_request == ImportControlRequest.NONE,
            ImportJob.progress_revision == job.progress_revision,
            select(ImportedSeries.id)
            .where(
                ImportedSeries.id == item.id,
                ImportedSeries.import_job_id == job.id,
                ImportedSeries.series_id == item.series_id,
                ImportedSeries.status == ImportSeriesStatus.DUPLICATE,
                ImportedSeries.cv_match_method == "mylar3_cv_id",
            )
            .exists(),
        )
        .values(progress_revision=job.progress_revision)
        .returning(ImportJob.id)
    )
    if claimed is None:
        raise ValidationError("The import changed. Reopen Follow-up before saving.")
    files = (
        await session.scalars(
            select(ImportedFile)
            .where(
                ImportedFile.import_job_id == job.id,
                ImportedFile.import_series_id == item.id,
                ImportedFile.id.in_(ids),
                conflict_file(),
            )
            .order_by(ImportedFile.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    if {file.id for file in files} != ids:
        raise ValidationError("A file is no longer awaiting this decision. Reopen Follow-up.")
    for file in files:
        file.status = ImportedFileStatus.SKIPPED
        file.matched_issue_id = file.matched_issue_cv_id = None
        file.match_confidence = file.error_message = None
        file.match_method = "orphan_recovery_skip"
        set_review_file_selection(file, False)
    job.progress_revision += 1
    await session.flush()
    return len(files)
