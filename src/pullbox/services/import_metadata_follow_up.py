"""Bounded follow-up and explicit retries for imported archive metadata writes."""

from datetime import UTC, datetime

from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.models import LibraryFile
from pullbox.models.archive_metadata_publication import ArchiveMetadataPublication
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportedFile,
    ImportedFileStatus,
    ImportJob,
    ImportJobLog,
    ImportJobStatus,
)
from pullbox.services.library_mutation_coordination import lock_file_mutation_admission

PAGE_SIZE = 25


def failed_metadata_write_filter() -> ColumnElement[bool]:
    return and_(
        ImportedFile.status == ImportedFileStatus.IMPORTED,
        ImportedFile.diagnostics["comicinfo_enrichment"]["status"].as_string() == "failed",
    )


async def count_failed_metadata_writes(session: AsyncSession, job_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(ImportedFile)
            .where(
                ImportedFile.import_job_id == job_id,
                failed_metadata_write_filter(),
            )
        )
        or 0
    )


async def outstanding_metadata_write_counts(session: AsyncSession, job_id: int) -> dict[str, int]:
    """Count unfinished archive updates without loading individual file diagnostics."""
    status = ImportedFile.diagnostics["comicinfo_enrichment"]["status"].as_string()
    rows = await session.execute(
        select(status, func.count())
        .where(
            ImportedFile.import_job_id == job_id,
            ImportedFile.status == ImportedFileStatus.IMPORTED,
            status.in_(("pending", "failed")),
        )
        .group_by(status)
    )
    return {state: count for state, count in rows}


async def load_metadata_write_follow_up(
    session: AsyncSession,
    job_id: int,
    *,
    page: int = 1,
) -> dict[str, object]:
    job = await session.get(ImportJob, job_id)
    if job is None or job.archived_at is not None:
        raise NotFoundError("ImportJob", job_id)
    total = await count_failed_metadata_writes(session, job_id)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 1), pages)
    files = list(
        await session.scalars(
            select(ImportedFile)
            .where(
                ImportedFile.import_job_id == job_id,
                failed_metadata_write_filter(),
            )
            .order_by(ImportedFile.id)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
    )
    return {
        "job": job,
        "metadata_files": files,
        "metadata_total": total,
        "metadata_page": page,
        "metadata_pages": pages,
        "metadata_retry_allowed": job.status is ImportJobStatus.COMPLETED
        and job.control_request is ImportControlRequest.NONE,
    }


async def retry_import_metadata_write(
    session: AsyncSession,
    job_id: int,
    file_id: int,
    *,
    actor: str,
) -> bool:
    """Queue only failed metadata work; preserve placement and publication evidence.

    The caller commits before scheduling the existing background job. All file,
    identity and root checks still run in that job, never in the request's write
    transaction. This does not approve new file contents or change a match.
    """
    await lock_file_mutation_admission(session)
    job = await session.scalar(
        select(ImportJob)
        .where(ImportJob.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if job is None or job.archived_at is not None:
        raise NotFoundError("ImportJob", job_id)
    if (
        job.status is not ImportJobStatus.COMPLETED
        or job.control_request is not ImportControlRequest.NONE
    ):
        raise ValidationError("This import is no longer ready for a metadata retry.")
    file = await session.scalar(
        select(ImportedFile)
        .where(
            ImportedFile.id == file_id,
            ImportedFile.import_job_id == job_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if file is None:
        raise NotFoundError("ImportedFile", file_id)
    details = file.diagnostics.get("comicinfo_enrichment", {})
    if not isinstance(details, dict) or file.status is not ImportedFileStatus.IMPORTED:
        raise ValidationError("This file has no imported metadata work to retry.")
    if details.get("status") in {"pending", "complete"}:
        return False
    if details.get("status") != "failed":
        raise ValidationError("This file has no failed metadata write to retry.")
    if file.library_file_id is None or await session.get(LibraryFile, file.library_file_id) is None:
        raise ValidationError("The library file is no longer registered. Rescan its series first.")
    active = await session.scalar(
        select(ArchiveMetadataPublication.id)
        .where(
            ArchiveMetadataPublication.active_file_id == file.library_file_id,
            ArchiveMetadataPublication.active_path_key.is_not(None),
        )
        .limit(1)
    )
    if active is not None:
        raise ValidationError(
            "An archive write still needs recovery. Retry after recovery completes."
        )
    file.diagnostics = {
        **file.diagnostics,
        "comicinfo_enrichment": {
            **details,
            "status": "pending",
            "retry_requested_at": datetime.now(UTC).isoformat(),
        },
    }
    session.add(
        ImportJobLog(
            import_job_id=job_id,
            level="INFO",
            event="import_archive_metadata_retry_requested",
            message=f"Archive metadata retry requested for {file.file_name}",
            data={"imported_file_id": file_id, "actor": actor},
        )
    )
    await session.flush()
    return True
