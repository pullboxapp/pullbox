"""Persistent app-shell activity state for collection imports."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from pullbox.models.import_job import ImportJob, ImportJobStatus
from pullbox.schemas.import_job import ImportActivityRead, ImportJobRead

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_IMPORT_ACTIVITY_STATUSES = (
    ImportJobStatus.PENDING,
    ImportJobStatus.SCANNING,
    ImportJobStatus.PAUSING,
    ImportJobStatus.PAUSED,
    ImportJobStatus.ANALYZING,
    ImportJobStatus.MATCHING,
    ImportJobStatus.FILE_MATCHING,
    ImportJobStatus.IMPORTING,
    ImportJobStatus.STALLED,
    ImportJobStatus.CANCELLING,
    ImportJobStatus.ROLLING_BACK,
)


async def get_active_import_activity_response(
    session: AsyncSession,
) -> ImportActivityRead:
    """Return the oldest background import and count any work behind it."""
    result = await session.execute(
        select(ImportJob)
        .where(ImportJob.status.in_(_IMPORT_ACTIVITY_STATUSES))
        .order_by(ImportJob.created_at.asc(), ImportJob.id.asc())
    )
    jobs = list(result.scalars().all())
    if jobs:
        return ImportActivityRead(
            job=ImportJobRead.model_validate(jobs[0]),
            queued_count=max(0, len(jobs) - 1),
        )

    failed_result = await session.execute(
        select(ImportJob)
        .where(ImportJob.status == ImportJobStatus.FAILED)
        .order_by(ImportJob.created_at.desc(), ImportJob.id.desc())
        .limit(1)
    )
    failed_job = failed_result.scalar_one_or_none()
    return ImportActivityRead(
        job=ImportJobRead.model_validate(failed_job) if failed_job is not None else None,
    )
