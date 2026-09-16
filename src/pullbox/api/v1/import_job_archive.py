"""Authenticated import history archive endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from pullbox.api.deps import DbSession, InteractiveOperatorUser  # noqa: TC001
from pullbox.schemas.import_job_archive import ImportJobArchiveResponse
from pullbox.services.import_job_archive import set_import_job_archived

router = APIRouter(prefix="/import", tags=["import"])


async def _set_archive_state(
    session: DbSession,
    job_id: int,
    *,
    archived: bool,
) -> ImportJobArchiveResponse:
    job = await set_import_job_archived(session, job_id, archived=archived)
    await session.commit()
    return ImportJobArchiveResponse(
        job_id=job.id,
        archived=job.archived_at is not None,
        archived_at=job.archived_at,
    )


@router.post("/{job_id}/archive", response_model=ImportJobArchiveResponse)
async def archive_import_job(
    job_id: int,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> ImportJobArchiveResponse:
    """Archive one finished import without deleting it."""
    return await _set_archive_state(session, job_id, archived=True)


@router.post("/{job_id}/restore", response_model=ImportJobArchiveResponse)
async def restore_import_job(
    job_id: int,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> ImportJobArchiveResponse:
    """Restore one archived import to the normal history view."""
    return await _set_archive_state(session, job_id, archived=False)
