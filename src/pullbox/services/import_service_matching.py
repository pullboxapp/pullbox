"""Scan matching and conflict facade helpers for ``ImportService``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from pullbox.services.import_counters import (
    recompute_file_counters as recompute_import_file_counters,
)
from pullbox.services.import_counters import (
    recompute_series_counters as recompute_import_series_counters,
)
from pullbox.services.import_duplicate_copies import compute_content_hash, mark_duplicate_copy
from pullbox.services.import_duplicate_copies import (
    detect_duplicate_copies as detect_import_duplicate_copies,
)
from pullbox.services.import_duplicate_copies import (
    record_duplicate_copy_cluster as record_import_duplicate_copy_cluster,
)
from pullbox.services.import_duplicates import (
    build_duplicate_merge_profile,
    duplicate_merge_is_actionable,
    duplicate_target_key,
    duplicate_target_state,
    is_duplicate_series,
    logical_series_group_key,
    preferred_file_sort_key,
)
from pullbox.services.import_file_conflicts import (
    detect_conflicts,
    preferred_conflict_reasons,
    rejected_conflict_reasons,
)
from pullbox.services.import_file_matching import run_import_file_matching
from pullbox.services.import_file_review import (
    apply_manual_file_match as apply_import_manual_file_match,
)
from pullbox.services.import_logical_groups import (
    consolidate_logical_series_groups as consolidate_import_logical_series_groups,
)
from pullbox.services.import_logical_groups import (
    logical_group_series_ids as import_logical_group_series_ids,
)
from pullbox.services.import_logical_groups import (
    reclassify_matched_series_duplicates as reclassify_import_matched_series_duplicates,
)
from pullbox.services.import_logical_groups import (
    reset_series_group_files as reset_import_series_group_files,
)
from pullbox.services.import_mylar3_paths import auto_detect_mylar3_path_map
from pullbox.services.import_progress_runtime import estimate_remaining_work_seconds
from pullbox.services.import_series_deduplication import deduplicate_import_series
from pullbox.services.import_series_matching import run_import_series_matching
from pullbox.services.import_source_metadata import (
    build_import_metadata_conflict,
    load_deferred_source_metadata_for_import_file,
    normalized_release_name,
    source_metadata_for_import_file,
    source_metadata_for_import_series,
    source_metadata_for_matching_series,
    sync_import_file_source_metadata,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime
    from pathlib import Path
    from typing import Protocol

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.import_job import ImportedFile, ImportedSeries, ImportJob
    from pullbox.models.issue import Issue
    from pullbox.schemas.import_job import ImportProgressEvent

    class ImportServiceMatchingContext(Protocol):
        _metadata_service: Any
        _scan_provider_cache_by_job: dict[int, Any]
        _semantic_match_engine: Any
        _is_duplicate_series: Any
        _duplicate_target_state: Any
        _duplicate_merge_is_actionable: Any
        _build_duplicate_merge_profile: Any
        _logical_series_group_key: Any
        _preferred_file_sort_key: Any
        _duplicate_target_key: Any
        _normalized_release_name: Any
        _source_metadata_for_import_file: Any
        _load_deferred_source_metadata_for_import_file: Any
        _source_metadata_for_matching_series: Any
        _load_deferred_source_metadata_for_matching_series: Any
        _build_import_metadata_conflict: Any

        async def _raise_if_job_cancelled(
            self,
            session: AsyncSession,
            job_id: int,
        ) -> None: ...

        async def _emit_progress(
            self,
            session: AsyncSession,
            job: ImportJob,
            event: ImportProgressEvent,
            progress_callback: Callable[[ImportProgressEvent], Awaitable[None]] | None = None,
        ) -> None: ...

        def _phase_progress(self, start: int, end: int, completed: int, total: int) -> int: ...

        def _estimate_remaining_seconds(
            self,
            started_at: datetime | None,
            progress: int,
        ) -> int | None: ...

        def _job_stats(self, job: ImportJob) -> dict[str, int]: ...

        async def _maybe_slow_item_delay(self) -> None: ...

        async def _evaluate_comicvine_match(self, **kwargs: Any) -> Any: ...

        async def _log_event(
            self,
            session: AsyncSession,
            job_id: int,
            level: str,
            event: str,
            message: str | None = None,
            **kwargs: Any,
        ) -> None: ...

        async def _recompute_series_counters(
            self,
            session: AsyncSession,
            job: ImportJob,
        ) -> None: ...

        async def _recompute_file_counters(
            self,
            session: AsyncSession,
            job: ImportJob,
            *,
            series_ids: list[int] | None = None,
        ) -> None: ...

        async def _apply_manual_file_match(
            self,
            session: AsyncSession,
            imp_file: ImportedFile,
            issue: Issue,
            *,
            method: str,
            confidence: str = "high",
        ) -> tuple[str, str | None]: ...

        def _compute_content_hash(self, path_str: str) -> str | None: ...

        def _mark_duplicate_copy(
            self,
            duplicate_file: ImportedFile,
            representative: ImportedFile,
            *,
            duplicate_group_id: int,
            duplicate_reason: str,
            target_state: str | None = None,
        ) -> None: ...

        async def _record_duplicate_copy_cluster(
            self,
            session: AsyncSession,
            job: ImportJob,
            imp_series: ImportedSeries,
            representative: ImportedFile,
            duplicates: list[ImportedFile],
            *,
            duplicate_group_id: int,
            duplicate_reason: str,
            event_name: str,
            message: str,
            target_state: str | None = None,
        ) -> dict[str, Any]: ...

        async def _reclassify_matched_series_duplicates(
            self,
            session: AsyncSession,
            job: ImportJob,
            *,
            series_ids: list[int] | None = None,
        ) -> int: ...

        async def _detect_duplicate_copies(
            self,
            session: AsyncSession,
            job: ImportJob,
            imp_series: ImportedSeries,
            files: list[ImportedFile],
            duplicate_group_counter: int,
        ) -> tuple[int, int, list[dict[str, Any]]]: ...

        def _detect_conflicts(
            self,
            files: list[ImportedFile],
            group_counter: int,
        ) -> tuple[int, int, list[dict[str, Any]]]: ...

        def _metadata_provider_for_job(self, job_id: int) -> Any: ...

else:
    ImportServiceMatchingContext = object

logger = structlog.get_logger(__name__)

_TRUSTED_FOLDER_IDENTITY_PROBE_LIMIT = 3


class ImportServiceMatchingMixin:
    """Mixin for Step 2 series matching, file matching, and conflict detection."""

    _is_duplicate_series = staticmethod(is_duplicate_series)
    _duplicate_target_state = staticmethod(duplicate_target_state)
    _duplicate_merge_is_actionable = staticmethod(duplicate_merge_is_actionable)
    _build_duplicate_merge_profile = staticmethod(build_duplicate_merge_profile)
    _logical_series_group_key = staticmethod(logical_series_group_key)
    _preferred_file_sort_key = staticmethod(preferred_file_sort_key)
    _duplicate_target_key = staticmethod(duplicate_target_key)
    _normalized_release_name = staticmethod(normalized_release_name)
    _source_metadata_for_import_file = staticmethod(source_metadata_for_import_file)
    _load_deferred_source_metadata_for_import_file = staticmethod(
        load_deferred_source_metadata_for_import_file
    )
    _source_metadata_for_import_series = staticmethod(source_metadata_for_import_series)
    _source_metadata_for_matching_series = staticmethod(
        lambda session, imp_series: source_metadata_for_matching_series(
            session,
            imp_series,
            trusted_identity_probe_limit=_TRUSTED_FOLDER_IDENTITY_PROBE_LIMIT,
        )
    )
    _load_deferred_source_metadata_for_matching_series = staticmethod(
        lambda session, imp_series: source_metadata_for_matching_series(
            session,
            imp_series,
            load_deferred_archive_metadata=True,
        )
    )
    _build_import_metadata_conflict = staticmethod(build_import_metadata_conflict)
    _sync_import_file_source_metadata = staticmethod(sync_import_file_source_metadata)

    @staticmethod
    def _compute_content_hash(path_str: str) -> str | None:
        """Hash a file only when a same-target cluster needs stronger confirmation."""
        return compute_content_hash(path_str)

    @classmethod
    def _mark_duplicate_copy(
        cls,
        duplicate_file: ImportedFile,
        representative: ImportedFile,
        *,
        duplicate_group_id: int,
        duplicate_reason: str,
        target_state: str | None = None,
    ) -> None:
        """Turn a redundant file into a durable read-only duplicate-copy row."""
        mark_duplicate_copy(
            duplicate_file,
            representative,
            duplicate_group_id=duplicate_group_id,
            duplicate_reason=duplicate_reason,
            target_state=target_state,
        )

    async def _recompute_series_counters(
        self,
        session: AsyncSession,
        job: ImportJob,
    ) -> None:
        """Rebuild per-job series counters from persisted ImportedSeries rows."""
        await recompute_import_series_counters(session, job)

    async def _reclassify_matched_series_duplicates(
        self: ImportServiceMatchingContext,
        session: AsyncSession,
        job: ImportJob,
        *,
        series_ids: list[int] | None = None,
    ) -> int:
        """Convert exact post-match ComicVine hits into existing-library duplicates."""
        return await reclassify_import_matched_series_duplicates(
            session,
            job,
            series_ids=series_ids,
            log_event=self._log_event,
            recompute_series_counters=self._recompute_series_counters,
        )

    async def _consolidate_logical_series_groups(
        self: ImportServiceMatchingContext,
        session: AsyncSession,
        job: ImportJob,
        *,
        series_ids: list[int] | None = None,
        prefer_resolved_cv_only: bool = False,
    ) -> dict[int, int]:
        """Physically merge repeated import buckets that resolve to the same logical series."""
        return await consolidate_import_logical_series_groups(
            session,
            job,
            series_ids=series_ids,
            prefer_resolved_cv_only=prefer_resolved_cv_only,
            log_event=self._log_event,
            logical_series_group_key=self._logical_series_group_key,
            recompute_series_counters=self._recompute_series_counters,
            recompute_file_counters=self._recompute_file_counters,
        )

    async def _logical_group_series_ids(
        self: ImportServiceMatchingContext,
        session: AsyncSession,
        job_id: int,
        group_key: tuple[object, ...],
        *,
        prefer_resolved_cv_only: bool = False,
    ) -> list[int]:
        """Return import-series ids that currently resolve to the same logical group."""
        return await import_logical_group_series_ids(
            session,
            job_id,
            group_key,
            prefer_resolved_cv_only=prefer_resolved_cv_only,
            logical_series_group_key=self._logical_series_group_key,
        )

    async def _reset_series_group_files(
        self,
        session: AsyncSession,
        *,
        job_id: int,
        series_ids: list[int],
    ) -> None:
        """Clear file-level review state so a logical group can be rematched cleanly."""
        await reset_import_series_group_files(
            session,
            job_id=job_id,
            series_ids=series_ids,
        )

    async def _recompute_file_counters(
        self,
        session: AsyncSession,
        job: ImportJob,
        *,
        series_ids: list[int] | None = None,
    ) -> None:
        """Rebuild per-series and per-job file counters from persisted ImportedFile rows."""
        await recompute_import_file_counters(session, job, series_ids=series_ids)

    async def _apply_manual_file_match(
        self: ImportServiceMatchingContext,
        session: AsyncSession,
        imp_file: ImportedFile,
        issue: Issue,
        *,
        method: str,
        confidence: str = "high",
    ) -> tuple[str, str | None]:
        """Apply a manual issue assignment, respecting duplicate-series merge rules."""
        return await apply_import_manual_file_match(
            session,
            imp_file,
            issue,
            method=method,
            confidence=confidence,
            is_duplicate_series=self._is_duplicate_series,
            duplicate_merge_is_actionable=self._duplicate_merge_is_actionable,
            duplicate_target_state=self._duplicate_target_state,
        )

    async def _apply_manual_file_match_for_override(
        self: ImportServiceMatchingContext,
        session: AsyncSession,
        imp_file: ImportedFile,
        issue: Issue,
    ) -> tuple[str, str | None]:
        """Apply the standard manual-override match used by review actions."""
        return await self._apply_manual_file_match(
            session,
            imp_file,
            issue,
            method="manual_override",
        )

    async def _deduplicate_series(
        self: ImportServiceMatchingContext,
        session: AsyncSession,
        job: ImportJob,
        *,
        progress_callback: Callable[[ImportProgressEvent], Awaitable[None]] | None = None,
    ) -> None:
        """Tag ImportedSeries as DUPLICATE if already in Pullbox."""
        await deduplicate_import_series(
            session,
            job,
            raise_if_cancelled=self._raise_if_job_cancelled,
            log_event=self._log_event,
            emit_progress=self._emit_progress,
            phase_progress=self._phase_progress,
            estimate_remaining_seconds=self._estimate_remaining_seconds,
            job_stats=self._job_stats,
            progress_callback=progress_callback,
        )

    async def _run_matching(
        self: ImportServiceMatchingContext,
        session: AsyncSession,
        job: ImportJob,
        *,
        progress_callback: Callable[[ImportProgressEvent], Awaitable[None]] | None = None,
    ) -> None:
        """Run ComicVine matching for all PENDING ImportedSeries."""
        await run_import_series_matching(
            session,
            job,
            metadata_provider=self._metadata_provider_for_job(job.id),
            source_metadata_for_series=self._source_metadata_for_matching_series,
            load_deferred_source_metadata_for_series=(
                self._load_deferred_source_metadata_for_matching_series
            ),
            evaluate_match=self._evaluate_comicvine_match,
            raise_if_cancelled=self._raise_if_job_cancelled,
            reclassify_duplicates=self._reclassify_matched_series_duplicates,
            recompute_series_counters=self._recompute_series_counters,
            log_event=self._log_event,
            emit_progress=self._emit_progress,
            phase_progress=self._phase_progress,
            estimate_remaining_seconds=self._estimate_remaining_seconds,
            job_stats=self._job_stats,
            maybe_slow_item_delay=self._maybe_slow_item_delay,
            estimate_remaining_work_seconds=estimate_remaining_work_seconds,
            progress_callback=progress_callback,
        )

    @staticmethod
    def _auto_detect_mylar3_path_map(db_path: Path) -> dict[str, str] | None:
        """Auto-detect path mapping for Mylar3 by examining ComicLocation values."""
        return auto_detect_mylar3_path_map(db_path, log=logger)

    async def _run_file_matching(
        self: ImportServiceMatchingContext,
        session: AsyncSession,
        job: ImportJob,
        *,
        series_ids: list[int] | None = None,
        progress_callback: Callable[[ImportProgressEvent], Awaitable[None]] | None = None,
    ) -> None:
        """Match individual ImportedFile rows to issue targets for review."""
        await run_import_file_matching(
            session,
            job,
            metadata_provider=(
                self._metadata_provider_for_job(job.id)
                if self._metadata_service is not None
                else None
            ),
            semantic_match_engine=self._semantic_match_engine,
            is_duplicate_series=self._is_duplicate_series,
            build_duplicate_merge_profile=self._build_duplicate_merge_profile,
            duplicate_target_state=self._duplicate_target_state,
            source_metadata_for_import_file=self._source_metadata_for_import_file,
            load_deferred_source_metadata_for_import_file=(
                self._load_deferred_source_metadata_for_import_file
            ),
            build_import_metadata_conflict=self._build_import_metadata_conflict,
            raise_if_cancelled=self._raise_if_job_cancelled,
            detect_duplicate_copies=self._detect_duplicate_copies,
            detect_conflicts=self._detect_conflicts,
            recompute_file_counters=self._recompute_file_counters,
            recompute_series_counters=self._recompute_series_counters,
            log_event=self._log_event,
            emit_progress=self._emit_progress,
            phase_progress=self._phase_progress,
            estimate_remaining_seconds=self._estimate_remaining_seconds,
            job_stats=self._job_stats,
            maybe_slow_item_delay=self._maybe_slow_item_delay,
            log_warning=logger.warning,
            series_ids=series_ids,
            progress_callback=progress_callback,
        )

    def _metadata_provider_for_job(self: ImportServiceMatchingContext, job_id: int) -> Any:
        """Return the job-scoped cached provider when Step 2 is actively scanning."""
        if self._metadata_service is None:
            return None
        return self._scan_provider_cache_by_job.get(job_id, self._metadata_service._provider)

    async def _record_duplicate_copy_cluster(
        self: ImportServiceMatchingContext,
        session: AsyncSession,
        job: ImportJob,
        imp_series: ImportedSeries,
        representative: ImportedFile,
        duplicates: list[ImportedFile],
        *,
        duplicate_group_id: int,
        duplicate_reason: str,
        event_name: str,
        message: str,
        target_state: str | None = None,
    ) -> dict[str, Any]:
        """Persist duplicate-copy rows and emit a structured log for the cluster."""
        return await record_import_duplicate_copy_cluster(
            session,
            job,
            imp_series,
            representative,
            duplicates,
            duplicate_group_id=duplicate_group_id,
            duplicate_reason=duplicate_reason,
            event_name=event_name,
            message=message,
            target_state=target_state,
            log_event=self._log_event,
            mark_duplicate=self._mark_duplicate_copy,
        )

    async def _detect_duplicate_copies(
        self: ImportServiceMatchingContext,
        session: AsyncSession,
        job: ImportJob,
        imp_series: ImportedSeries,
        files: list[ImportedFile],
        duplicate_group_counter: int,
    ) -> tuple[int, int, list[dict[str, Any]]]:
        """Detect duplicate incoming copies after file matching within one logical series row."""
        return await detect_import_duplicate_copies(
            session,
            job,
            imp_series,
            files,
            duplicate_group_counter,
            log_event=self._log_event,
            is_duplicate_series=self._is_duplicate_series,
            duplicate_target_key=self._duplicate_target_key,
            normalized_release_name=self._normalized_release_name,
            preferred_file_sort_key=self._preferred_file_sort_key,
            compute_hash=self._compute_content_hash,
            record_cluster=self._record_duplicate_copy_cluster,
        )

    @staticmethod
    def _preferred_conflict_reasons(
        preferred: ImportedFile,
        others: list[ImportedFile],
    ) -> list[str]:
        """Explain why the preferred file won the conflict tiebreaker."""
        return preferred_conflict_reasons(preferred, others)

    @staticmethod
    def _rejected_conflict_reasons(
        candidate: ImportedFile,
        preferred: ImportedFile,
    ) -> list[str]:
        """Explain why a conflicting file was not selected."""
        return rejected_conflict_reasons(candidate, preferred)

    @classmethod
    def _detect_conflicts(
        cls,
        files: list[ImportedFile],
        group_counter: int,
    ) -> tuple[int, int, list[dict[str, Any]]]:
        """Group matched files by issue, mark conflicts, apply tiebreakers."""
        _ = cls
        return detect_conflicts(files, group_counter)
