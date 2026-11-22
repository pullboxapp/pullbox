"""Guards that keep mutating utility jobs away from active imports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from pullbox.core.exceptions import ValidationError
from pullbox.models.import_job import ImportJob, ImportJobStatus
from pullbox.utilities.models import JobType

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_IMPORT_FILE_MUTATION_STATUSES = frozenset(
    {
        ImportJobStatus.IMPORTING,
        ImportJobStatus.STALLED,
        ImportJobStatus.ROLLING_BACK,
    }
)


def utility_job_mutates_library(job_type: str, config: dict[str, Any]) -> bool:
    """Return whether a utility job may mutate library files/folders."""
    normalized_job_type = str(job_type)
    if normalized_job_type in {
        JobType.FILE_CONVERT.value,
        JobType.MASS_CONVERT_PIPELINE.value,
        JobType.MASS_RENAME.value,
        JobType.ROLLBACK.value,
        JobType.FILE_METADATA.value,
        JobType.STORY_ARC_CLEANUP.value,
    }:
        return True

    if normalized_job_type == JobType.LIBRARY_PERMISSIONS.value:
        return str(config.get("run_mode", "dry_run") or "dry_run") == "apply"

    if normalized_job_type == JobType.INTEGRITY_CHECK.value:
        return str(config.get("corrupt_action", "report") or "report").lower() == "quarantine"

    if normalized_job_type == JobType.DB_CHECK_CLEANUP.value:
        return str(config.get("mode", "preview") or "preview").lower() != "preview"

    return False


async def active_import_file_mutation_job_id(session: AsyncSession) -> int | None:
    """Return an active import/rollback job id that is mutating library files."""
    job_id = await session.scalar(
        select(ImportJob.id)
        .where(ImportJob.status.in_(_IMPORT_FILE_MUTATION_STATUSES))
        .order_by(ImportJob.updated_at.desc())
        .limit(1)
    )
    return int(job_id) if job_id is not None else None


async def ensure_utility_job_allowed_during_import(
    session: AsyncSession,
    *,
    job_type: str,
    config: dict[str, Any],
) -> None:
    """Block library-mutating utility jobs while step 4 import work is active."""
    if not utility_job_mutates_library(job_type, config):
        return

    await ensure_no_active_import_file_mutation(session)


async def ensure_no_active_import_file_mutation(session: AsyncSession) -> None:
    """Block a library file mutation while step 4 import work is active."""
    active_job_id = await active_import_file_mutation_job_id(session)
    if active_job_id is None:
        return

    raise ValidationError(
        "A collection import is currently writing files to the library. "
        "Wait for the import to finish before starting utilities that rename, convert, "
        "quarantine, chmod, or roll back library files."
    )
