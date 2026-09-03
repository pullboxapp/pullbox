"""Bounded, durable progress while preparing Mylar source pages."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pullbox.models.import_job import ImportJobStatus
from pullbox.schemas.import_job import ImportProgressEvent
from pullbox.services import import_progress_runtime
from pullbox.services.import_counters import job_stats
from pullbox.services.import_workflow_state import (
    SCAN_PROGRESS_MATERIALIZE_END,
    SCAN_PROGRESS_MATERIALIZE_START,
    emit_progress,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.import_job import ImportJob


@dataclass
class MylarScanProgress:
    session: AsyncSession
    job: ImportJob
    source_total: int
    cancellation_check: Callable[[], Awaitable[None]]
    callback: Callable[[ImportProgressEvent], Awaitable[None]] | None = None
    source_completed: int = 0
    source_page_end: int = 0
    last_report_at: float = 0.0
    checked_files: int = 0
    batch_files: int = 0
    work_started_at: datetime = field(default_factory=lambda: datetime.now(UTC), repr=False)
    estimated_seconds_remaining: int | None = field(default=None, init=False)

    async def report_safety(self, completed: int, total: int, file_path: str) -> None:
        self.checked_files = completed
        self.batch_files = total
        now = time.monotonic()
        if completed not in {0, total} and now - self.last_report_at < 1.0:
            return
        self.last_report_at = now
        await self.cancellation_check()
        message = f"Checking Mylar files: {completed}/{total} in this batch."
        await self._publish(completed, total, file_path, message)

    async def _publish(self, completed: int, total: int, file_path: str, message: str) -> None:
        fraction = completed / total if total else 0.0
        source_done = (
            self.source_completed + (self.source_page_end - self.source_completed) * fraction
        )
        progress = SCAN_PROGRESS_MATERIALIZE_START
        if self.source_total:
            progress += int(
                (SCAN_PROGRESS_MATERIALIZE_END - SCAN_PROGRESS_MATERIALIZE_START)
                * min(source_done / self.source_total, 1.0)
            )
        if self.source_total:
            message += f" {self.source_completed}/{self.source_total} source series saved."
        await emit_progress(
            self.session,
            self.job,
            ImportProgressEvent(
                job_id=self.job.id,
                status=ImportJobStatus.SCANNING,
                phase="scanning",
                progress=progress,
                message=message,
                estimated_seconds_remaining=self.estimated_seconds_remaining,
                current_item_kind="scan",
                current_item_stage="scanning",
                current_item_stage_label="Checking Mylar file batch",
                current_item_progress_pct=int(fraction * 100),
                current_item_detail=Path(file_path).name if file_path else message,
                **job_stats(self.job),
            ),
            self.callback,
        )

    async def checkpoint_page(self) -> None:
        self.source_completed = self.source_page_end
        if self.source_total > 0 and self.source_completed >= self.source_total:
            self.estimated_seconds_remaining = 0
        else:
            self.estimated_seconds_remaining = (
                import_progress_runtime.estimate_remaining_work_seconds(
                    self.work_started_at,
                    completed_units=self.source_completed,
                    total_units=self.source_total,
                )
            )
        # A single source page can emit extra Annual cohorts. Measure overall
        # progress using source rows, not the number of resulting review groups.
        await self._publish(
            self.checked_files,
            self.batch_files,
            "",
            (
                f"Prepared {self.job.series_found} series and "
                f"{self.job.scan_total_files} file records."
            ),
        )
