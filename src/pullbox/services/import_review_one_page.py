"""Exact, source-preserving decisions for one series' one-page archives."""

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.services.import_counters import recompute_file_counters, recompute_series_counters
from pullbox.services.import_review_actions import (
    apply_safety_allow_once_to_file,
    apply_safety_skip_to_file,
    prepare_series_for_safety_rematch,
)
from pullbox.services.import_review_scope import review_scope, verify_review_scope
from pullbox.services.import_safety_diagnostics import normalize_import_safety_diagnostics
from pullbox.services.import_story_arc_resolution import refresh_story_arc_entries_for_import_files

OnePageAction = Literal["allow", "skip"]


def one_page_files(files: Sequence[ImportedFile]) -> list[ImportedFile]:
    """Exclude every other safety category, even in the same series folder."""
    result = []
    for file in files:
        block = (file.diagnostics or {}).get("safety_block")
        if (
            file.status == ImportedFileStatus.SAFETY_BLOCKED
            and isinstance(block, Mapping)
            and normalize_import_safety_diagnostics(block)["category"] == "single_page_comic"
        ):
            result.append(file)
    return sorted(result, key=lambda file: file.id)


def one_page_scope(
    job: ImportJob,
    series: ImportedSeries,
    files: Sequence[ImportedFile],
    *,
    actor_id: int,
    action: OnePageAction,
) -> dict[str, Any]:
    return {
        "actor": actor_id,
        "action": f"one-page-{action}",
        "job": job.id,
        "series": series.id,
        "files": [review_scope(job, series, file) for file in files],
    }


async def decide_one_page_series(
    session: AsyncSession,
    job_id: int,
    series_id: int,
    *,
    actor_id: int,
    action: OnePageAction,
    token: str,
) -> ImportedSeries:
    job = await session.get(ImportJob, job_id)
    series = await session.get(ImportedSeries, series_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if series is None or series.import_job_id != job_id:
        raise NotFoundError("ImportedSeries", series_id)
    if job.status != ImportJobStatus.REVIEW or job.control_request != ImportControlRequest.NONE:
        raise ValidationError("This import is no longer ready for review.")
    files = one_page_files(
        (
            await session.scalars(
                select(ImportedFile).where(
                    ImportedFile.import_job_id == job_id,
                    ImportedFile.import_series_id == series_id,
                    ImportedFile.status == ImportedFileStatus.SAFETY_BLOCKED,
                )
            )
        ).all()
    )
    if not files:
        raise ValidationError("These one-page files have already changed. Refresh the review.")
    await verify_review_scope(
        session, token, one_page_scope(job, series, files, actor_id=actor_id, action=action)
    )
    if action == "allow" and any(
        normalize_import_safety_diagnostics(file.diagnostics["safety_block"])["overrideable"]
        is not True
        for file in files
    ):
        raise ValidationError("One or more of these files cannot be allowed once.")

    # Verify the complete visible scope before making any changes, and rematch only once.
    for file in files:
        if action == "allow":
            apply_safety_allow_once_to_file(file)
        else:
            apply_safety_skip_to_file(file)
    if action == "allow":
        prepare_series_for_safety_rematch(series)
    series.selected_for_import = False
    await refresh_story_arc_entries_for_import_files(
        session, import_job_id=job_id, import_file_ids=[file.id for file in files]
    )
    await recompute_file_counters(session, job, series_ids=[series.id])
    if action == "skip" and not (
        series.files_matched
        or series.files_conflict
        or series.files_no_match
        or (series.diagnostics or {}).get("safety_blocked_files")
    ):
        series.status = ImportSeriesStatus.SKIPPED
    await recompute_series_counters(session, job)
    await session.flush()
    return series
