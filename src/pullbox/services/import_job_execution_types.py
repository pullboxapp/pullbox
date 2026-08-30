"""Type contracts for Step 4 import job execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.import_job import (
        ImportedFile,
        ImportedSeries,
        ImportJob,
        ImportJobAction,
    )
    from pullbox.models.series import Series
    from pullbox.providers.base import IssueSummary
    from pullbox.schemas.import_job import ImportProgressEvent
    from pullbox.services.import_job_actions import ImportJobActionSpec


class SeriesServiceFunc(Protocol):
    async def add_from_comicvine(
        self,
        session: AsyncSession,
        *,
        comicvine_id: int,
        library_root_id: int | None,
        search_on_add: bool,
    ) -> Series: ...

    async def prefetch_comicvine_bundle(
        self,
        comicvine_id: int,
    ) -> tuple[Any, list[Any]]: ...

    async def add_from_comicvine_prefetched(
        self,
        session: AsyncSession,
        *,
        comicvine_id: int,
        library_root_id: int | None,
        search_on_add: bool,
        series_meta: Any,
        issue_summaries: list[Any],
    ) -> Series: ...

    async def add_from_import_review_targeted(
        self,
        session: AsyncSession,
        *,
        import_series: ImportedSeries,
        library_root_id: int | None,
        search_on_add: bool,
        issue_summaries: list[IssueSummary],
    ) -> Series: ...


class ProcessSeriesFilesFunc(Protocol):
    async def __call__(
        self,
        session: AsyncSession,
        job: ImportJob,
        item: ImportedSeries,
        *,
        duplicate_mode: bool = False,
        series_id_override: int | None = None,
        report_file_progress: ReportFileProgressFunc | None = None,
    ) -> tuple[int, int]: ...


class ReportFileProgressFunc(Protocol):
    async def __call__(
        self,
        *,
        imp_file: ImportedFile,
        file_index: int,
        total_files: int,
        stage: str,
        current: int,
        total: int,
        unit: str,
        live_only: bool = False,
    ) -> None: ...


class RaiseIfCancelledFunc(Protocol):
    async def __call__(self, session: AsyncSession, job_id: int) -> None: ...


class RecordActionFunc(Protocol):
    async def __call__(
        self,
        session: AsyncSession,
        job: ImportJob,
        *,
        phase: str,
        action_type: str,
        payload: dict[str, Any],
    ) -> ImportJobAction: ...


class RecordActionsFunc(Protocol):
    async def __call__(
        self,
        session: AsyncSession,
        job: ImportJob,
        specs: Sequence[ImportJobActionSpec],
    ) -> list[ImportJobAction]: ...


class LogEventFunc(Protocol):
    async def __call__(
        self,
        session: AsyncSession,
        job_id: int,
        level: str,
        event: str,
        *,
        message: str,
        **details: Any,
    ) -> None: ...


class EmitProgressFunc(Protocol):
    async def __call__(
        self,
        session: AsyncSession,
        job: ImportJob,
        event: ImportProgressEvent,
        progress_callback: Callable[[ImportProgressEvent], Awaitable[None]],
    ) -> None: ...


class EstimateRemainingFunc(Protocol):
    def __call__(
        self,
        started_at: datetime | None,
        progress: int,
    ) -> int | None: ...


class SlowItemDelayFunc(Protocol):
    async def __call__(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ExecutionItemPlan:
    mode: Literal["new", "duplicate"]
    item_id: int
    raw_series_name: str
    cv_id: int | None
    existing_series_id: int | None


@dataclass(frozen=True, slots=True)
class CatalogHydrationRequest:
    series_id: int
    search_on_add: bool
