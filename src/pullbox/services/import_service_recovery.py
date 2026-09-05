"""Post-import recovery and reconciliation facade helpers for ``ImportService``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pullbox.services.import_orphan_recovery_context import (
    build_orphan_recovery_context,
    load_orphan_recovery_item,
)
from pullbox.services.import_orphans import assign_cv_to_orphan as assign_import_cv_to_orphan
from pullbox.services.import_orphans import dismiss_orphan as dismiss_import_orphan
from pullbox.services.import_orphans import get_orphaned_count as get_import_orphaned_count
from pullbox.services.import_orphans import get_orphaned_series as get_import_orphaned_series
from pullbox.services.import_orphans import recover_orphan as recover_import_orphan
from pullbox.services.import_orphans import retry_failed_series as retry_import_failed_series
from pullbox.services.import_reconcile_service import (
    build_import_reconcile_context,
    reconcile_import_series_decisions,
)

if TYPE_CHECKING:
    from typing import Protocol

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.import_job import ImportedSeries, ImportJob
    from pullbox.schemas.import_job import ImportReconcileRequest, RecoverOrphanRequest
    from pullbox.services.import_file_execution_protocols import ReportFileProgressFunc
    from pullbox.services.metadata_service import MetadataService
    from pullbox.services.series_service import SeriesService

    class ImportServiceRecoveryContext(Protocol):
        _metadata_service: MetadataService
        _series_service: SeriesService

        async def _process_series_files(self, *args: Any, **kwargs: Any) -> tuple[int, int]: ...

        async def _load_orphan_recovery_item(
            self,
            session: AsyncSession,
            imported_series_id: int,
        ) -> tuple[ImportJob, ImportedSeries]: ...

        async def _record_action(self, *args: Any, **kwargs: Any) -> Any: ...

        async def _recompute_file_counters(self, *args: Any, **kwargs: Any) -> None: ...

        async def _recompute_series_counters(self, *args: Any, **kwargs: Any) -> None: ...

        async def _log_event(
            self,
            session: AsyncSession,
            job_id: int,
            level: str,
            event: str,
            message: str | None = None,
            **kwargs: Any,
        ) -> None: ...

else:
    ImportServiceRecoveryContext = object


class ImportServiceRecoveryMixin:
    """Mixin for unmatched recovery, Step 3 reconciliation, and failed-series retry."""

    async def get_orphaned_series(
        self,
        session: AsyncSession,
        page: int = 1,
        page_size: int = 25,
        sort: str = "file_count_desc",
    ) -> tuple[list[ImportedSeries], int]:
        """Return paginated ImportedSeries with status=NO_MATCH from COMPLETED jobs."""
        return await get_import_orphaned_series(
            session,
            page=page,
            page_size=page_size,
            sort=sort,
        )

    async def get_orphaned_count(
        self,
        session: AsyncSession,
    ) -> int:
        """Return total count of active unmatched series from COMPLETED jobs."""
        return await get_import_orphaned_count(session)

    async def _load_orphan_recovery_item(
        self,
        session: AsyncSession,
        imported_series_id: int,
    ) -> tuple[ImportJob, ImportedSeries]:
        """Load the completed import job + orphaned series row for delayed recovery."""
        return await load_orphan_recovery_item(session, imported_series_id)

    async def assign_cv_to_orphan(
        self: ImportServiceRecoveryContext,
        session: AsyncSession,
        imported_series_id: int,
        cv_id: int,
    ) -> ImportedSeries:
        """Persist a chosen ComicVine series and move an unmatched row into recovery."""
        await self._load_orphan_recovery_item(session, imported_series_id)
        return await assign_import_cv_to_orphan(
            session,
            imported_series_id,
            cv_id,
            metadata_service=self._metadata_service,
            log_event=self._log_event,
        )

    async def get_orphan_recovery_context(
        self: ImportServiceRecoveryContext,
        session: AsyncSession,
        imported_series_id: int,
    ) -> dict[str, Any]:
        """Build the delayed unmatched-recovery context for one orphaned series row."""
        job, item = await self._load_orphan_recovery_item(session, imported_series_id)
        return await build_orphan_recovery_context(
            session,
            job,
            item,
            metadata_service=self._metadata_service,
        )

    async def get_import_reconcile_context(
        self: ImportServiceRecoveryContext,
        session: AsyncSession,
        job_id: int,
        imported_series_id: int,
    ) -> dict[str, Any]:
        """Build the Step 3 issue-reconciliation context for one review row."""
        return await build_import_reconcile_context(
            session,
            job_id,
            imported_series_id,
            metadata_service=self._metadata_service,
        )

    async def reconcile_import_series(
        self: ImportServiceRecoveryContext,
        session: AsyncSession,
        job_id: int,
        imported_series_id: int,
        request: ImportReconcileRequest,
    ) -> ImportedSeries:
        """Save Step 3 issue decisions without importing or moving any files."""
        return await reconcile_import_series_decisions(
            session,
            job_id,
            imported_series_id,
            request,
            metadata_service=self._metadata_service,
            recompute_file_counters=self._recompute_file_counters,
            recompute_series_counters=self._recompute_series_counters,
            log_event=self._log_event,
        )

    async def recover_orphan(
        self: ImportServiceRecoveryContext,
        session: AsyncSession,
        imported_series_id: int,
        request: RecoverOrphanRequest,
        progress_callback: ReportFileProgressFunc | None = None,
    ) -> dict[str, Any]:
        """Create/reuse the local series and import the selected unmatched files."""
        job, item = await self._load_orphan_recovery_item(session, imported_series_id)
        return await recover_import_orphan(
            session,
            job,
            item,
            request,
            series_service=self._series_service,
            process_series_files=self._process_series_files,
            record_action=self._record_action,
            recompute_file_counters=self._recompute_file_counters,
            recompute_series_counters=self._recompute_series_counters,
            log_event=self._log_event,
            progress_callback=progress_callback,
        )

    async def dismiss_orphan(
        self: ImportServiceRecoveryContext,
        session: AsyncSession,
        imported_series_id: int,
    ) -> None:
        """Mark an active unmatched series as SKIPPED so it no longer appears."""
        await self._load_orphan_recovery_item(session, imported_series_id)
        await dismiss_import_orphan(
            session,
            imported_series_id,
            log_event=self._log_event,
        )

    async def retry_failed_series(
        self: ImportServiceRecoveryContext,
        session: AsyncSession,
        job_id: int,
        *,
        file_ids: list[int] | None = None,
    ) -> tuple[ImportJob, int]:
        """Reset failed import rows/files for a job back to retryable states."""
        return await retry_import_failed_series(
            session,
            job_id,
            log_event=self._log_event,
            file_ids=file_ids,
        )
