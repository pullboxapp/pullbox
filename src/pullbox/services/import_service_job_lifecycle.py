"""Job lifecycle facade helpers for ``ImportService``."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.models.import_job import ImportJob, ImportJobStatus
from pullbox.services.import_job_controls import cancel_job as cancel_import_job
from pullbox.services.import_job_controls import pause_job as pause_import_job
from pullbox.services.import_job_controls import request_cancel as request_import_cancel
from pullbox.services.import_job_controls import request_rollback as request_import_rollback
from pullbox.services.import_job_controls import resume_job as resume_import_job
from pullbox.services.import_job_creation import create_job as create_import_job
from pullbox.services.import_retry_helpers import (
    build_retry_import_request,
    copy_retry_import_settings,
)
from pullbox.services.import_review_preview import get_preview as get_import_preview
from pullbox.services.story_arc_sync_queue import (
    retry_import_story_arc_sync_work as retry_import_story_arc_placements,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import Any, Protocol

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.import_job import ImportSeriesStatus
    from pullbox.schemas.import_job import ImportPreviewResponse, ImportProgressEvent

    class ImportServiceJobLifecycleContext(Protocol):
        async def rollback_import(
            self,
            session: AsyncSession,
            job_id: int,
            *,
            progress_callback: Callable[[ImportProgressEvent], Awaitable[None]] | None = None,
        ) -> bool: ...

        async def _log_event(
            self,
            session: AsyncSession,
            job_id: int,
            level: str,
            event: str,
            message: str | None = None,
            **kwargs: Any,
        ) -> None: ...

        def _log_job_deleted(self, job_id: int, status: ImportJobStatus) -> None: ...

else:
    ImportServiceJobLifecycleContext = object

logger = structlog.get_logger(__name__)


class ImportServiceJobLifecycleMixin:
    """Mixin for import job lifecycle and preview facade methods."""

    async def cancel_job(
        self: ImportServiceJobLifecycleContext,
        session: AsyncSession,
        job_id: int,
    ) -> str:
        """Discard a non-running import job or delete a terminal history row."""
        job = await session.get(ImportJob, job_id)
        if job is None:
            raise NotFoundError("ImportJob", job_id)

        if (
            job.status in {ImportJobStatus.PAUSED, ImportJobStatus.STALLED}
            and job.import_started_at is not None
        ):
            await request_import_cancel(
                session,
                job_id,
                log_event=self._log_event,
            )
            rollback_completed = await self.rollback_import(session, job_id)
            if not rollback_completed:
                logger.info(
                    "import_job_delete_waiting_for_story_arc_rollback",
                    job_id=job_id,
                )
                return "rollback_pending"
            reloaded = await session.get(ImportJob, job_id)
            if reloaded is not None:
                if (
                    reloaded.status == ImportJobStatus.FAILED
                    and dict(reloaded.progress_snapshot or {}).get("mode") == "rollback"
                ):
                    return "rollback_incomplete"
                self._log_job_deleted(job_id, reloaded.status)
                await session.delete(reloaded)
                await session.flush()
            return "deleted"

        return await cancel_import_job(
            session,
            job_id,
            log_job_deleted=self._log_job_deleted,
        )

    @staticmethod
    def _log_job_deleted(job_id: int, status: ImportJobStatus) -> None:
        """Compatibility adapter for import-job delete audit logging."""
        logger.info("import_job_deleted", job_id=job_id, mode="delete", status=status.value)

    async def pause_job(
        self: ImportServiceJobLifecycleContext,
        session: AsyncSession,
        job_id: int,
    ) -> ImportJob:
        """Request a cooperative pause at the next safe boundary."""
        return await pause_import_job(
            session,
            job_id,
            log_event=self._log_event,
        )

    async def resume_job(
        self: ImportServiceJobLifecycleContext,
        session: AsyncSession,
        job_id: int,
    ) -> ImportJob:
        """Resume a paused import from its last safe checkpoint."""
        return await resume_import_job(
            session,
            job_id,
            log_event=self._log_event,
        )

    async def request_cancel(
        self: ImportServiceJobLifecycleContext,
        session: AsyncSession,
        job_id: int,
    ) -> ImportJob:
        """Request cooperative cancellation or immediate discard for paused scans."""
        return await request_import_cancel(
            session,
            job_id,
            log_event=self._log_event,
        )

    async def request_rollback(
        self: ImportServiceJobLifecycleContext,
        session: AsyncSession,
        job_id: int,
    ) -> ImportJob:
        """Queue a rollback of previously executed import actions."""
        return await request_import_rollback(
            session,
            job_id,
            log_event=self._log_event,
        )

    async def retry_story_arc_placements(
        self: ImportServiceJobLifecycleContext,
        session: AsyncSession,
        job_id: int,
    ) -> tuple[ImportJob, int]:
        """Requeue exact failed/cancelled placement work without replaying import."""
        job, retrying_count = await retry_import_story_arc_placements(session, job_id)
        await self._log_event(
            session,
            job_id,
            "INFO",
            "import_story_arc_placements_retry_requested",
            message=(f"Retrying {retrying_count} failed or cancelled Story Arc placements."),
            retrying_count=retrying_count,
        )
        return job, retrying_count

    async def retry_job(
        self: ImportServiceJobLifecycleContext,
        session: AsyncSession,
        job_id: int,
    ) -> ImportJob:
        """Create a brand-new import job from a cancelled or rolled-back run."""
        original = await session.get(ImportJob, job_id)
        if original is None:
            raise NotFoundError("ImportJob", job_id)
        if original.status not in {
            ImportJobStatus.CANCELLED,
            ImportJobStatus.ROLLED_BACK,
        }:
            raise ValidationError(
                f"Only cancelled or rolled-back jobs can be retried, not {original.status}."
            )

        job = await create_import_job(
            session,
            build_retry_import_request(original),
            log_event=self._log_event,
        )
        copy_retry_import_settings(original, job)
        await self._log_event(
            session,
            job.id,
            "INFO",
            "import_retry_created",
            message=f"Created fresh retry from import job {job_id}.",
            retry_of_job_id=job_id,
        )
        return job

    async def get_preview(
        self,
        session: AsyncSession,
        job_id: int,
        status_filter: list[ImportSeriesStatus] | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> ImportPreviewResponse:
        """Return paginated ImportedSeries for the review step."""
        return await get_import_preview(
            session,
            job_id,
            status_filter=status_filter,
            page=page,
            page_size=page_size,
        )
