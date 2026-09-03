"""Overall import metadata activity, derived from durable catalog and file state."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import func, select

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
)
from pullbox.models.operation_progress import (
    OperationProgress,
    OperationProgressState,
    OperationProgressTone,
    OperationProgressType,
)
from pullbox.models.series import IssueCatalogState, Series
from pullbox.services.operation_progress import (
    OperationProgressMeasure,
    OperationProgressUpdate,
    publish_operation_progress,
)
from pullbox.services.operation_progress_dispatch import notify_activity_changed

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)
_REFRESH_SECONDS = 5.0


@dataclass
class _MetadataActivity:
    factory: async_sessionmaker[AsyncSession]
    job_id: int
    workers: int = 0
    task: asyncio.Task[None] | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def refresh(self) -> None:
        try:
            async with self.lock:
                async with self.factory() as session:
                    update = await build_import_metadata_progress(
                        session,
                        job_id=self.job_id,
                        running=self.workers > 0,
                    )
                    if update is None:
                        return
                    existing = await session.scalar(
                        select(OperationProgress).where(
                            OperationProgress.operation_type == update.operation_type,
                            OperationProgress.operation_key == update.operation_key,
                        )
                    )
                    if existing is not None and (
                        existing.state == update.state
                        and existing.message == update.message
                        and existing.overall_current == update.overall.current
                        and existing.overall_total == update.overall.total
                    ):
                        return
                    await publish_operation_progress(session, update)
                    await session.commit()
                await notify_activity_changed(update)
        except Exception:
            # Progress failure must never interrupt a catalog fetch or archive write.
            logger.exception("import_metadata_activity_update_failed", job_id=self.job_id)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.refresh()
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=_REFRESH_SECONDS)


_activities: dict[tuple[asyncio.AbstractEventLoop, int], _MetadataActivity] = {}


@asynccontextmanager
async def track_import_metadata_progress(
    factory: async_sessionmaker[AsyncSession],
    *,
    job_ids: list[int],
) -> AsyncIterator[None]:
    """Share one periodic summary across both metadata lanes for each import."""
    loop = asyncio.get_running_loop()
    tracked = []
    for job_id in set(job_ids):
        key = (loop, job_id)
        activity = _activities.setdefault(key, _MetadataActivity(factory, job_id))
        activity.workers += 1
        if activity.task is None:
            activity.stop = asyncio.Event()
            activity.task = asyncio.create_task(activity.run(activity.stop))
        tracked.append((key, activity))
    try:
        yield
    finally:
        for key, activity in tracked:
            activity.workers -= 1
            if activity.workers:
                continue
            task, activity.task = activity.task, None
            activity.stop.set()
            if task is not None:
                await task
            await activity.refresh()
            if activity.workers == 0:
                _activities.pop(key, None)


async def catalog_hydration_import_job_ids(
    factory: async_sessionmaker[AsyncSession],
    *,
    series_id: int | None = None,
) -> list[int]:
    """Find import owners without expanding large series lists into SQL parameters."""
    try:
        async with factory() as session:
            statement = (
                select(ImportedSeries.import_job_id)
                .join(ImportJob, ImportJob.id == ImportedSeries.import_job_id)
                .join(Series, Series.id == ImportedSeries.series_id)
                .where(ImportJob.status.in_([ImportJobStatus.IMPORTING, ImportJobStatus.COMPLETED]))
                .distinct()
            )
            if series_id is not None:
                statement = statement.where(Series.id == series_id)
            else:
                statement = statement.where(
                    Series.issue_catalog_state == IssueCatalogState.HYDRATING
                )
            return list((await session.scalars(statement)).all())
    except Exception:
        # Activity ownership is observability only and cannot block real metadata work.
        logger.exception("import_metadata_owner_lookup_failed", series_id=series_id)
        return []


async def build_import_metadata_progress(
    session: AsyncSession,
    *,
    job_id: int,
    running: bool,
) -> OperationProgressUpdate | None:
    """Count finished work, not individual-file bytes or elapsed-time guesses."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        return None
    key = f"metadata:{job_id}"
    if job.status != ImportJobStatus.COMPLETED:
        existing = await session.scalar(
            select(OperationProgress.id).where(
                OperationProgress.operation_type == OperationProgressType.IMPORT,
                OperationProgress.operation_key == key,
            )
        )
        if existing is None:
            return None
        return OperationProgressUpdate(
            operation_type=OperationProgressType.IMPORT,
            operation_key=key,
            revision=None,
            state=OperationProgressState.CANCELLED,
            phase="metadata_sync",
            title="Import metadata sync",
            message="Metadata sync stopped because the import is no longer complete.",
            source_label=f"Import #{job_id}",
            group_key="import_metadata",
        )

    series_ids = select(ImportedSeries.series_id).where(ImportedSeries.import_job_id == job_id)
    catalog_rows = await session.execute(
        select(Series.issue_catalog_state, func.count())
        .where(Series.id.in_(series_ids), Series.comicvine_id.isnot(None))
        .group_by(Series.issue_catalog_state)
    )
    catalogs = {state: count for state, count in catalog_rows.all()}
    status = ImportedFile.diagnostics["comicinfo_enrichment"]["status"].as_string()
    file_rows = await session.execute(
        select(status, func.count())
        .where(
            ImportedFile.import_job_id == job_id,
            ImportedFile.status == ImportedFileStatus.IMPORTED,
            status.in_(["pending", "complete", "failed"]),
        )
        .group_by(status)
    )
    files = {state: count for state, count in file_rows.all()}
    catalog_total = sum(catalogs.values())
    file_total = sum(files.values())
    total = catalog_total + file_total
    if not total:
        return None
    catalog_done = catalogs.get(IssueCatalogState.COMPLETE, 0)
    file_done = files.get("complete", 0)
    failed = catalogs.get(IssueCatalogState.FAILED, 0) + files.get("failed", 0)
    finished = catalog_done + file_done + failed
    pending = total - finished
    state = OperationProgressState.RUNNING
    tone = OperationProgressTone.INFO
    attention = False
    summary = (
        f"Series catalogs: {catalog_done:,} of {catalog_total:,}. "
        f"ComicInfo files: {file_done:,} of {file_total:,}."
    )
    if not pending:
        state = OperationProgressState.FAILED if failed else OperationProgressState.COMPLETED
        tone = OperationProgressTone.WARNING if failed else OperationProgressTone.SUCCESS
        attention = bool(failed)
        summary = (
            f"Metadata sync finished with {failed:,} failed updates. "
            if failed
            else "Metadata sync complete. "
        ) + summary
    elif not running:
        state = OperationProgressState.PAUSED
        tone = OperationProgressTone.WARNING
        attention = True
        summary = "Metadata sync paused. Check import logs for provider or file errors. " + summary
    else:
        summary = "Syncing metadata in the background. " + summary
    return OperationProgressUpdate(
        operation_type=OperationProgressType.IMPORT,
        operation_key=key,
        revision=None,
        state=state,
        phase="metadata_sync",
        title="Import metadata sync",
        message=summary,
        source_label=f"Import #{job_id}",
        group_key="import_metadata",
        detail_url="/import?tab=history",
        tone=tone,
        attention_required=attention,
        overall=OperationProgressMeasure(current=finished, total=total, unit="updates"),
        detail_snapshot={
            "job_id": job_id,
            "catalogs_complete": catalog_done,
            "catalogs_total": catalog_total,
            "files_complete": file_done,
            "files_total": file_total,
            "failed": failed,
            "pending": pending,
        },
    )
