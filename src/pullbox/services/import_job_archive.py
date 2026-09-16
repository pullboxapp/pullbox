"""Non-destructive import history archival."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.models.import_job import ImportControlRequest, ImportJob, ImportJobStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_ARCHIVABLE_STATUSES = frozenset(
    {
        ImportJobStatus.COMPLETED,
        ImportJobStatus.FAILED,
        ImportJobStatus.CANCELLED,
        ImportJobStatus.ROLLED_BACK,
    }
)


async def set_import_job_archived(
    session: AsyncSession,
    job_id: int,
    *,
    archived: bool,
) -> ImportJob:
    """Hide or restore one finished import without deleting its evidence."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if (
        job.status not in _ARCHIVABLE_STATUSES
        or job.control_request is not ImportControlRequest.NONE
    ):
        raise ValidationError("Only an idle, finished import can be archived")
    job.archived_at = datetime.now(UTC) if archived else None
    await session.flush()
    return job
