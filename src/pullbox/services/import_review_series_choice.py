"""Source-preserving series exclusion with actor-bound, revalidated previews."""

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportedFile,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.services.import_counters import recompute_series_counters
from pullbox.services.import_review_scope import review_scope, verify_review_scope


async def series_choice_scope(
    session: AsyncSession,
    job_id: int,
    series_id: int,
    action: str,
    actor_id: int,
) -> tuple[ImportJob, ImportedSeries, dict[str, object]]:
    job = await session.get(ImportJob, job_id)
    series = await session.get(ImportedSeries, series_id)
    if job is None or series is None or series.import_job_id != job_id:
        raise NotFoundError("ImportedSeries", series_id)
    if (
        job.status != ImportJobStatus.REVIEW
        or job.control_request != ImportControlRequest.NONE
        or (series.diagnostics or {}).get("rematch_pending")
    ):
        raise ValidationError("This import is busy or no longer ready for review.")
    prior = (series.diagnostics or {}).get("review_series_skip")
    if action == "restore":
        if series.status != ImportSeriesStatus.SKIPPED or not isinstance(prior, dict):
            raise ValidationError("This series has no review skip to undo.")
    elif action != "skip" or series.status in {
        ImportSeriesStatus.SKIPPED,
        ImportSeriesStatus.IMPORTED,
        ImportSeriesStatus.CONFIRMED,
    }:
        raise ValidationError("This series cannot be skipped from review.")
    files = (
        await session.scalars(
            select(ImportedFile)
            .where(
                ImportedFile.import_job_id == job_id,
                ImportedFile.import_series_id == series_id,
            )
            .order_by(ImportedFile.id)
        )
    ).all()
    if any(
        (f.diagnostics or {}).get("review_source_action", {}).get("state")
        in {"pending", "matching"}
        for f in files
    ):
        raise ValidationError("Wait for this series' source verification before changing it.")
    return (
        job,
        series,
        {
            "action": "review-series-" + action,
            "actor": actor_id,
            "job": job_id,
            "series": series_id,
            "status": series.status.value,
            "selected": series.selected_for_import,
            "diagnostics": series.diagnostics,
            "files": [review_scope(job, series, file) for file in files],
        },
    )


async def apply_series_choice(
    session: AsyncSession,
    job_id: int,
    series_id: int,
    action: str,
    actor_id: int,
    token: str,
) -> None:
    job, series, scope = await series_choice_scope(session, job_id, series_id, action, actor_id)
    await verify_review_scope(session, token, scope)
    diagnostics = dict(series.diagnostics or {})
    if action == "skip":
        diagnostics["review_series_skip"] = {"previous_status": series.status.value}
        series.status = ImportSeriesStatus.SKIPPED
    else:
        previous = diagnostics.pop("review_series_skip")
        series.status = ImportSeriesStatus(previous["previous_status"])
        if series.status == ImportSeriesStatus.DUPLICATE:
            # Duplicate-series eligibility is stored per file, not on the parent checkbox.
            await session.execute(
                update(ImportedFile)
                .where(
                    ImportedFile.import_job_id == job_id,
                    ImportedFile.import_series_id == series_id,
                )
                .values(include_in_import=False)
            )
    # Keep file matches and safety evidence intact. Restoring never auto-selects a series.
    series.diagnostics = diagnostics
    series.selected_for_import = False
    await recompute_series_counters(session, job)
    await session.flush()
