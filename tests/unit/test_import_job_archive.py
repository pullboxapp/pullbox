"""Non-destructive import history archival contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from pullbox.core.exceptions import ValidationError
from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType
from pullbox.services.import_job_archive import set_import_job_archived

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_completed_import_can_be_archived_and_restored_without_deletion(
    db_session: AsyncSession,
) -> None:
    job = ImportJob(
        source_path="/imports/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.COMPLETED,
    )
    db_session.add(job)
    await db_session.flush()

    archived = await set_import_job_archived(db_session, job.id, archived=True)
    assert archived.archived_at is not None
    assert archived.archived_at <= datetime.now(UTC)
    assert await db_session.get(ImportJob, job.id) is archived

    restored = await set_import_job_archived(db_session, job.id, archived=False)
    assert restored.archived_at is None
    assert await db_session.get(ImportJob, job.id) is restored


@pytest.mark.asyncio
async def test_active_import_cannot_be_archived(db_session: AsyncSession) -> None:
    job = ImportJob(
        source_path="/imports",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.IMPORTING,
    )
    db_session.add(job)
    await db_session.flush()

    with pytest.raises(ValidationError, match="finished import"):
        await set_import_job_archived(db_session, job.id, archived=True)
