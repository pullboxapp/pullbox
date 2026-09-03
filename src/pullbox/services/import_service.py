"""Import service for collection import workflow.

Orchestrates the full scan → analyze → match → import lifecycle,
including series deduplication and ComicVine matching.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog

from pullbox.config import get_settings
from pullbox.core.collection_scanner import CollectionScanner
from pullbox.core.exceptions import NotFoundError
from pullbox.core.file_ops import (
    LibraryFileRegistrationOutcome,
    register_library_file_with_metadata,
)
from pullbox.core.mylar3_reader import Mylar3Reader
from pullbox.models.import_job import (
    ImportedSeries,
    ImportJob,
    ImportJobAction,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.services.import_catalog_hydration import run_pending_catalog_hydration
from pullbox.services.import_comicinfo_enrichment import (
    run_pending_import_comicinfo_enrichment,
    schedule_import_comicinfo_enrichment,
)
from pullbox.services.import_confirm_policy import apply_confirm_import_policy
from pullbox.services.import_confirmation import confirm_import_job
from pullbox.services.import_counters import job_stats as import_job_stats
from pullbox.services.import_duplicates import (
    DuplicateMergeProfile as DuplicateMergeProfile,
)
from pullbox.services.import_event_logging import (
    IMPORT_ROOT_SUMMARY_EVENTS as _IMPORT_ROOT_SUMMARY_EVENTS,
)
from pullbox.services.import_event_logging import (
    log_import_event,
)
from pullbox.services.import_file_registration_adapters import (
    build_import_library_file_adapters,
)
from pullbox.services.import_job_actions import (
    next_action_sequence as next_import_action_sequence,
)
from pullbox.services.import_job_actions import record_action as record_import_action
from pullbox.services.import_job_actions import record_actions as record_import_actions
from pullbox.services.import_job_actions import rollback_action as rollback_import_action
from pullbox.services.import_job_controls import (
    raise_if_job_cancelled_immediately as raise_if_import_job_cancelled_immediately,
)
from pullbox.services.import_job_creation import create_job as create_import_job
from pullbox.services.import_job_execution import execute_import_job
from pullbox.services.import_job_execution_progress import progress_session_factory_for_runtime
from pullbox.services.import_matching import (
    ComicVineMatchEvaluation as ComicVineMatchEvaluation,
)
from pullbox.services.import_matching import (
    DeduplicationResult as DeduplicationResult,
)
from pullbox.services.import_matching import (
    evaluate_comicvine_match as evaluate_comicvine_match,
)
from pullbox.services.import_matching import (
    is_same_series as _is_same_series,  # noqa: F401 - compatibility import
)
from pullbox.services.import_matching import (
    match_to_comicvine as match_to_comicvine,
)
from pullbox.services.import_matching import (
    score_cv_result as _score_cv_result,  # noqa: F401 - compatibility import
)
from pullbox.services.import_placement_recovery import (
    load_completed_import_placement_recovery,
)
from pullbox.services.import_provider_cache import (
    CachedImportMetadataProvider,
    build_import_scan_metadata_provider,
)
from pullbox.services.import_rollback_execution import RollbackActionPlan, rollback_import_job
from pullbox.services.import_rollback_state import restore_review_state_after_rollback
from pullbox.services.import_runtime_settings import (
    ImportRuntimeCache,
    load_cached_import_ingest_policy,
    load_cached_import_media_settings,
    load_cached_import_permission_policy,
)
from pullbox.services.import_runtime_settings import (
    resolve_import_file_extensions as resolve_runtime_import_file_extensions,
)
from pullbox.services.import_scan_helpers import reset_scan_artifacts as reset_import_scan_artifacts
from pullbox.services.import_scan_helpers import (
    validate_discovered_files_safety as validate_import_discovered_files_safety,
)
from pullbox.services.import_scan_materialization import materialize_discovered_scan_results
from pullbox.services.import_scan_pipeline import run_import_scan_pipeline
from pullbox.services.import_scan_resume import (
    RESUMABLE_SCAN_STATUSES,
    resume_import_scan_phase,
)
from pullbox.services.import_series_file_processor import process_series_files_for_import
from pullbox.services.import_series_overrides import override_cv_id as override_import_cv_id
from pullbox.services.import_service_file_operations import ImportServiceFileOperationsMixin
from pullbox.services.import_service_job_lifecycle import ImportServiceJobLifecycleMixin
from pullbox.services.import_service_matching import ImportServiceMatchingMixin
from pullbox.services.import_service_recovery import ImportServiceRecoveryMixin
from pullbox.services.import_service_review import ImportServiceReviewMixin
from pullbox.services.import_story_arc_placement_completion import (
    ImportStoryArcPlacementCompletionState,
    finalize_import_story_arc_placements,
)
from pullbox.services.import_workflow_state import (
    emit_progress,
    estimate_remaining_seconds,
    persist_progress_snapshot,
    phase_progress,
    raise_if_job_cancelled,
)
from pullbox.services.import_workflow_state import (
    import_control_state_for_job as import_control_state_for_job,
)
from pullbox.services.semantic_matching import ImportPolicy, SemanticMatchEngine
from pullbox.utilities.sse import publish

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from pullbox.core.collection_scanner import DiscoveredSeries
    from pullbox.core.events import EventBus
    from pullbox.core.library_permissions import LibraryPermissionPolicy
    from pullbox.core.library_policy import LibraryIngestPolicy
    from pullbox.models.issue import Issue
    from pullbox.models.library import LibraryFile, MatchConfidence
    from pullbox.providers.base import SeriesMetadata
    from pullbox.schemas.import_job import (
        ConfirmImportRequest,
        ImportJobCreate,
        ImportProgressEvent,
    )
    from pullbox.services.import_file_execution_protocols import ReportFileProgressFunc
    from pullbox.services.import_job_actions import ImportJobActionSpec
    from pullbox.services.metadata_service import MetadataService
    from pullbox.services.series_service import SeriesService

logger = structlog.get_logger(__name__)

register_library_file = register_library_file_with_metadata
import_detail_logger = structlog.get_logger("pullbox.imports")


@dataclass(frozen=True)
class RunImportResult:
    """Post-transaction follow-up work requested by import execution."""

    schedule_comicinfo_enrichment: bool = False
    schedule_story_arc_sync: bool = False


# ── ImportService class ──────────────────────────────────────────────────


class ImportService(
    ImportServiceFileOperationsMixin,
    ImportServiceJobLifecycleMixin,
    ImportServiceMatchingMixin,
    ImportServiceRecoveryMixin,
    ImportServiceReviewMixin,
):
    """Orchestrates the collection import workflow.

    This service owns the full lifecycle:
      create_job → start_scan → (background) scan → analyze → match →
      (wait for user) → confirm_import → (background) import → complete
    """

    def __init__(
        self,
        series_service: SeriesService,
        metadata_service: MetadataService,
        event_bus: EventBus,
    ) -> None:
        self._series_service = series_service
        self._metadata_service = metadata_service
        self._event_bus = event_bus
        self._settings = get_settings()
        self._semantic_match_engine = SemanticMatchEngine(policy=ImportPolicy())
        self._scan_provider_cache_by_job: dict[int, CachedImportMetadataProvider] = {}
        self._import_runtime_cache_by_job: dict[int, ImportRuntimeCache] = {}

    @staticmethod
    def _job_stats(job: ImportJob) -> dict[str, int]:
        """Extract stat counters from an ImportJob for SSE progress events."""
        return import_job_stats(job)

    async def _evaluate_comicvine_match(self, **kwargs: Any) -> ComicVineMatchEvaluation:
        """Compatibility adapter for tests/callers that patch the legacy import path."""
        return await evaluate_comicvine_match(**kwargs)

    async def _maybe_slow_phase_delay(self) -> None:
        """Slow phase-level progress transitions in dev when enabled."""
        await self._maybe_debug_sleep(self._settings.import_debug_phase_delay_seconds)

    async def _maybe_slow_item_delay(self) -> None:
        """Slow per-item progress transitions in dev when enabled."""
        await self._maybe_debug_sleep(self._settings.import_debug_item_delay_seconds)

    async def _maybe_debug_sleep(self, delay_seconds: float) -> None:
        """Apply an env-gated debug delay without affecting normal runtime."""
        if not self._settings.import_debug_slow_mode:
            return

        delay = max(float(delay_seconds), 0.0)
        if delay <= 0:
            return

        await asyncio.sleep(delay)

    @staticmethod
    def _phase_progress(start: int, end: int, completed: int, total: int) -> int:
        """Scale measured phase work into a bounded overall progress range."""
        return phase_progress(start, end, completed, total)

    @staticmethod
    def _estimate_remaining_seconds(
        started_at: datetime | None,
        progress: int,
    ) -> int | None:
        """Estimate remaining runtime from elapsed wall-clock time and progress."""
        return estimate_remaining_seconds(started_at, progress)

    @staticmethod
    def _control_state_for_job(job: ImportJob) -> dict[str, object]:
        """Return durable UI controls for the current job state."""
        return import_control_state_for_job(job)

    async def _persist_progress_snapshot(
        self,
        session: AsyncSession,
        job: ImportJob,
        event: ImportProgressEvent,
    ) -> None:
        """Persist the latest progress payload on the job for recovery/UI hydration."""
        await persist_progress_snapshot(session, job, event)

    async def _emit_progress(
        self,
        session: AsyncSession,
        job: ImportJob,
        event: ImportProgressEvent,
        progress_callback: Callable[[ImportProgressEvent], Awaitable[None]] | None = None,
    ) -> None:
        """Persist and forward a progress event."""
        await emit_progress(session, job, event, progress_callback=progress_callback)

    def _import_runtime_cache(self, job_id: int) -> ImportRuntimeCache:
        return self._import_runtime_cache_by_job.setdefault(job_id, ImportRuntimeCache())

    async def _load_import_media_settings(
        self,
        session: AsyncSession,
        job: ImportJob,
    ) -> dict[str, str]:
        """Load media-management settings honored by import execution."""
        cache = self._import_runtime_cache(job.id)
        return await load_cached_import_media_settings(session, job, cache)

    async def _load_import_ingest_policy(
        self,
        session: AsyncSession,
        job: ImportJob,
    ) -> LibraryIngestPolicy:
        """Load and memoize the effective ingest policy for an active import run."""
        cache = self._import_runtime_cache(job.id)
        return await load_cached_import_ingest_policy(session, job, cache)

    async def _load_import_permission_policy(
        self,
        session: AsyncSession,
        job: ImportJob,
    ) -> LibraryPermissionPolicy:
        """Load and memoize the effective permission policy for an active import run."""
        cache = self._import_runtime_cache(job.id)
        return await load_cached_import_permission_policy(session, cache)

    async def _resolve_import_file_extensions(
        self,
        session: AsyncSession,
        configured_formats: str | None,
    ) -> frozenset[str]:
        """Resolve import file extensions from the job override or global safety config."""
        return await resolve_runtime_import_file_extensions(session, configured_formats)

    async def _raise_if_job_cancelled(self, session: AsyncSession, job_id: int) -> None:
        """Abort cooperative work if the import job has been paused or cancelled."""
        await raise_if_job_cancelled(session, job_id)

    async def _raise_if_job_cancelled_immediately(
        self,
        session: AsyncSession,
        job_id: int,
    ) -> None:
        """Read control state through a fresh session for killable file operations."""
        await raise_if_import_job_cancelled_immediately(
            session,
            job_id,
            raise_if_cancelled=raise_if_job_cancelled,
        )

    # ── Job lifecycle ────────────────────────────────────────────────

    async def create_job(
        self,
        session: AsyncSession,
        request: ImportJobCreate,
    ) -> ImportJob:
        """Create an import job record. Does not start scanning."""
        return await create_import_job(
            session,
            request,
            log_event=self._log_event,
        )

    async def _reset_scan_artifacts(self, session: AsyncSession, job: ImportJob) -> None:
        """Clear scan-produced rows and counters before a fresh/recovered scan run."""
        await reset_import_scan_artifacts(session, job)

    async def _validate_discovered_files_safety(
        self,
        session: AsyncSession,
        discovered_list: list[DiscoveredSeries],
        *,
        progress_callback: Callable[[int, int, str], Awaitable[None]] | None = None,
    ) -> None:
        """Run safety checks on discovered source files before review/import."""
        await validate_import_discovered_files_safety(
            session,
            discovered_list,
            progress_callback=progress_callback,
            worker_count=self._settings.import_scan_worker_count,
        )

    def _build_scan_metadata_provider(self, session: AsyncSession) -> CachedImportMetadataProvider:
        """Return the Step 2 provider stack: persistent cache, then per-job cache."""
        return build_import_scan_metadata_provider(session, self._metadata_service._provider)

    async def start_scan(
        self,
        session: AsyncSession,
        job_id: int,
        progress_callback: Callable[[ImportProgressEvent], Awaitable[None]] | None = None,
    ) -> None:
        """Full pipeline: scan → analyze → match. Sets job to REVIEW when done.

        This is called from the background task. Runs to completion or FAILED.
        """
        if self._metadata_service is not None:
            self._scan_provider_cache_by_job[job_id] = self._build_scan_metadata_provider(session)
        try:
            await run_import_scan_pipeline(
                session,
                job_id,
                scanner_cls=CollectionScanner,
                mylar3_reader_cls=Mylar3Reader,
                auto_detect_mylar3_path_map=self._auto_detect_mylar3_path_map,
                reset_scan_artifacts=self._reset_scan_artifacts,
                resolve_import_file_extensions=self._resolve_import_file_extensions,
                validate_discovered_files_safety=self._validate_discovered_files_safety,
                materialize_discovered_scan_results=materialize_discovered_scan_results,
                deduplicate_series=self._deduplicate_series,
                run_matching=self._run_matching,
                consolidate_logical_series_groups=self._consolidate_logical_series_groups,
                run_file_matching=self._run_file_matching,
                raise_if_cancelled=self._raise_if_job_cancelled,
                log_event=self._log_event,
                emit_progress=self._emit_progress,
                phase_progress=self._phase_progress,
                estimate_remaining_seconds=self._estimate_remaining_seconds,
                job_stats=self._job_stats,
                maybe_slow_phase_delay=self._maybe_slow_phase_delay,
                progress_callback=progress_callback,
            )
        finally:
            self._scan_provider_cache_by_job.pop(job_id, None)

    async def resume_scan_phase(
        self,
        session: AsyncSession,
        job_id: int,
        progress_callback: Callable[[ImportProgressEvent], Awaitable[None]] | None = None,
    ) -> None:
        """Resume a scan job from the latest durable phase when possible."""
        job = await session.get(ImportJob, job_id)
        if job is None:
            raise NotFoundError("ImportJob", job_id)

        if job.status not in RESUMABLE_SCAN_STATUSES:
            await self.start_scan(session, job_id, progress_callback=progress_callback)
            return

        if self._metadata_service is not None:
            self._scan_provider_cache_by_job[job_id] = self._build_scan_metadata_provider(session)

        try:
            await resume_import_scan_phase(
                session,
                job,
                deduplicate_series=self._deduplicate_series,
                run_matching=self._run_matching,
                consolidate_logical_series_groups=self._consolidate_logical_series_groups,
                run_file_matching=self._run_file_matching,
                raise_if_cancelled=self._raise_if_job_cancelled,
                recompute_series_counters=self._recompute_series_counters,
                log_event=self._log_event,
                emit_progress=self._emit_progress,
                estimate_remaining_seconds=self._estimate_remaining_seconds,
                job_stats=self._job_stats,
                maybe_slow_phase_delay=self._maybe_slow_phase_delay,
                progress_callback=progress_callback,
            )
        finally:
            self._scan_provider_cache_by_job.pop(job_id, None)

    async def confirm_import(
        self,
        session: AsyncSession,
        job_id: int,
        request: ConfirmImportRequest,
    ) -> ImportJob:
        """Mark selected series as CONFIRMED; transition job to IMPORTING."""
        return await confirm_import_job(
            session,
            job_id,
            request,
            apply_manual_file_match=self._apply_manual_file_match,
            recompute_file_counters=self._recompute_file_counters,
            apply_confirm_policy=apply_confirm_import_policy,
            log_event=self._log_event,
        )

    async def _next_action_sequence(self, session: AsyncSession, job_id: int) -> int:
        """Return the next durable action sequence number for a job."""
        return await next_import_action_sequence(session, job_id)

    async def _record_action(
        self,
        session: AsyncSession,
        job: ImportJob,
        *,
        phase: str,
        action_type: str,
        payload: dict[str, Any],
    ) -> ImportJobAction:
        """Persist a durable rollback journal action."""
        return await record_import_action(
            session,
            job,
            phase=phase,
            action_type=action_type,
            payload=payload,
        )

    async def _record_actions(
        self,
        session: AsyncSession,
        job: ImportJob,
        specs: Sequence[ImportJobActionSpec],
    ) -> list[ImportJobAction]:
        """Persist one bounded rollback-journal action batch."""
        return await record_import_actions(session, job, specs)

    async def _register_import_library_file(
        self,
        session: AsyncSession,
        job: ImportJob,
        source_path: Path,
        issue: Issue,
        confidence: MatchConfidence,
        **kwargs: Any,
    ) -> LibraryFile | LibraryFileRegistrationOutcome:
        """Register a library file with interruptible archive operations."""
        adapters = build_import_library_file_adapters(
            session=session,
            job=job,
            convert_file_interruptible=self._convert_import_file_interruptible,
            embed_comicinfo_interruptible=self._embed_import_comicinfo_interruptible,
            transfer_artifact_interruptible=self._transfer_import_artifact_interruptible,
            materialize_cbz_with_comicinfo_interruptible=(
                self._materialize_import_cbz_with_comicinfo_interruptible
            ),
        )
        recovery_imported_file_id = kwargs.pop("recovery_imported_file_id", None)
        recovery_source_value = kwargs.pop("recovery_original_source_path", None)
        recovery_original_source_path = (
            Path(recovery_source_value) if recovery_source_value is not None else source_path
        )
        placement_action_id: int | None = None

        async def placement_started_callback(
            *,
            artifact_source_path: Path,
            target_path: Path,
            transfer_method: str,
            series_folder_created: bool,
            series_folder_path: Path,
            created_directory_paths: tuple[Path, ...] = (),
            directory_ownership_boundary_path: Path | None = None,
            temp_paths: tuple[Path, ...] = (),
        ) -> None:
            nonlocal placement_action_id
            action = await self._record_action(
                session,
                job,
                phase="import",
                action_type="library_file_placement_started",
                payload={
                    "imported_file_id": recovery_imported_file_id,
                    "issue_id": issue.id,
                    "destination_path": str(target_path),
                    "original_source_path": str(recovery_original_source_path),
                    "artifact_source_path": str(artifact_source_path),
                    "transfer_method": transfer_method,
                    "created_series_folder": series_folder_created,
                    "created_series_folder_path": str(series_folder_path),
                    "created_directory_paths": [str(path) for path in created_directory_paths],
                    "directory_ownership_boundary_path": (
                        str(directory_ownership_boundary_path)
                        if directory_ownership_boundary_path is not None
                        else None
                    ),
                    "temp_paths": [str(path) for path in temp_paths],
                    "placement_completed": False,
                },
            )
            placement_action_id = action.id
            # This journal row must survive if archive materialization raises and
            # the caller rolls back the active session.
            await session.commit()

        async def placement_completed_callback(
            *,
            target_path: Path,
            destination_signature: dict[str, int | str],
        ) -> None:
            if placement_action_id is None:
                raise RuntimeError("Import placement completed without a durable start record")
            action = await session.get(ImportJobAction, placement_action_id)
            if action is None:
                raise RuntimeError("Import placement start record disappeared before completion")
            payload = dict(action.payload or {})
            if str(payload.get("destination_path") or "") != str(target_path):
                raise RuntimeError("Import placement completion target changed after planning")
            payload["placement_completed"] = True
            payload["destination_signature"] = dict(destination_signature)
            action.payload = payload
            await session.commit()

        source_scan_root = kwargs.pop(
            "source_scan_root",
            Path(job.source_path) if job.source_type == ImportSourceType.FILESYSTEM else None,
        )
        kwargs.pop("strict_import_target", None)

        transfer_method = str(kwargs.get("transfer_method") or "")
        recovery = None
        if (
            isinstance(recovery_imported_file_id, int)
            and not isinstance(recovery_imported_file_id, bool)
            and issue.id is not None
            and transfer_method
        ):
            recovery = await load_completed_import_placement_recovery(
                session,
                job_id=int(job.id),
                imported_file_id=recovery_imported_file_id,
                issue_id=int(issue.id),
                source_path=recovery_original_source_path,
                transfer_method=transfer_method,
            )
        if recovery is not None:
            recovery_kwargs = dict(kwargs)
            recovery_kwargs.update(
                {
                    "move_to_library": False,
                    "expected_source_signature": None,
                    "transfer_method": "recovered",
                    "normalize_to_cbz": False,
                    "update_embedded_comicinfo_from_match": False,
                    "comicinfo_payload": None,
                    "rename": False,
                    "recover_existing_managed_artifact": True,
                }
            )
            result = await register_library_file(
                session,
                recovery.destination_path,
                issue,
                confidence,
                source_scan_root=None,
                strict_import_target=True,
                **recovery_kwargs,
            )
            library_file = (
                result.library_file
                if isinstance(result, LibraryFileRegistrationOutcome)
                else result
            )
            library_file.source_signature = dict(recovery.destination_signature)
            await session.flush()
            return result

        result = await register_library_file(
            session,
            source_path,
            issue,
            confidence,
            converter=adapters.converter,
            comicinfo_embedder=adapters.comicinfo_embedder,
            artifact_transfer=adapters.artifact_transfer,
            comicinfo_materializer=adapters.comicinfo_materializer,
            placement_started_callback=placement_started_callback,
            placement_completed_callback=placement_completed_callback,
            placement_temp_paths=adapters.placement_temp_paths,
            source_scan_root=source_scan_root,
            strict_import_target=True,
            **kwargs,
        )
        await self._log_import_file_timing_events(
            session,
            job,
            issue,
            source_path,
            adapters.operation_timings,
        )
        return result

    async def _rollback_action(
        self,
        session: AsyncSession,
        action: RollbackActionPlan,
    ) -> None:
        """Reverse a recorded import action in reverse execution order."""
        await rollback_import_action(
            session,
            action_id=action.action_id,
            action_type=action.action_type,
            payload=action.payload,
            delete_series=self._delete_series_for_rollback_action,
        )

    async def _delete_series_for_rollback_action(
        self,
        session: AsyncSession,
        series_id: int,
        *,
        delete_files: bool,
        delete_folder: bool,
    ) -> None:
        """Compatibility adapter for extracted rollback action helpers."""
        await self._series_service.delete(
            session,
            series_id,
            delete_files=delete_files,
            delete_folder=delete_folder,
        )

    async def rollback_import(
        self,
        session: AsyncSession,
        job_id: int,
        *,
        progress_callback: Callable[[ImportProgressEvent], Awaitable[None]] | None = None,
    ) -> bool:
        """Rollback durable import actions in reverse order."""
        return await rollback_import_job(
            session,
            job_id,
            rollback_action=self._rollback_action,
            restore_review_state=restore_review_state_after_rollback,
            recompute_series_counters=self._recompute_series_counters,
            recompute_file_counters=self._recompute_file_counters,
            log_event=self._log_event,
            emit_progress=self._emit_progress,
            estimate_remaining_seconds=self._estimate_remaining_seconds,
            job_stats=self._job_stats,
            progress_callback=progress_callback,
        )

    async def run_import(
        self,
        session: AsyncSession,
        job_id: int,
        progress_callback: Callable[[ImportProgressEvent], Awaitable[None]] | None = None,
    ) -> RunImportResult:
        """Execute confirmed new-series imports plus duplicate-series file merges."""
        try:
            current_job = await session.get(ImportJob, job_id)
            if (
                current_job is not None
                and dict(current_job.progress_snapshot or {}).get("phase") == "story_arc_placements"
            ):
                outcome = await finalize_import_story_arc_placements(session, job_id)
                return RunImportResult(
                    schedule_comicinfo_enrichment=(
                        outcome.state is ImportStoryArcPlacementCompletionState.COMPLETED
                    ),
                    schedule_story_arc_sync=(
                        outcome.state is ImportStoryArcPlacementCompletionState.PENDING
                    ),
                )
            await execute_import_job(
                session,
                job_id,
                series_service=self._series_service,
                process_series_files=self._process_series_files,
                raise_if_cancelled=self._raise_if_job_cancelled,
                record_action=self._record_action,
                record_actions=self._record_actions,
                log_event=self._log_event,
                emit_progress=self._emit_progress,
                estimate_remaining_seconds=self._estimate_remaining_seconds,
                maybe_slow_item_delay=self._maybe_slow_item_delay,
                progress_callback=progress_callback,
            )
            completed_job = await session.get(ImportJob, job_id)
            story_arc_placements_pending = (
                completed_job is not None
                and completed_job.status == ImportJobStatus.IMPORTING
                and dict(completed_job.progress_snapshot or {}).get("phase")
                == "story_arc_placements"
            )
            return RunImportResult(
                schedule_comicinfo_enrichment=completed_job is not None
                and completed_job.status == ImportJobStatus.COMPLETED,
                schedule_story_arc_sync=story_arc_placements_pending,
            )
        finally:
            self._import_runtime_cache_by_job.pop(job_id, None)

    def schedule_comicinfo_enrichment(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None,
        *,
        job_id: int,
    ) -> None:
        """Queue deferred ComicInfo rewrites after the import transaction commits."""
        schedule_import_comicinfo_enrichment(
            session_factory,
            job_id=job_id,
            build_comicinfo_payload=self._build_comicinfo_payload_for_issue,
            apply_comicinfo=self._apply_comicinfo_to_imported_artifact,
            log_event=self._log_event,
        )

    def schedule_story_arc_sync(self) -> None:
        """Nudge durable story-arc work only after its import transaction commits."""
        from pullbox.services.story_arc_sync_queue import request_story_arc_sync_now

        request_story_arc_sync_now()

    async def recover_pending_comicinfo_enrichment(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> int:
        """Resume deferred ComicInfo rewrites left pending after a restart."""
        return await run_pending_import_comicinfo_enrichment(
            session_factory,
            build_comicinfo_payload=self._build_comicinfo_payload_for_issue,
            apply_comicinfo=self._apply_comicinfo_to_imported_artifact,
            log_event=self._log_event,
        )

    async def recover_pending_catalog_hydration(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> int:
        """Resume full catalog hydration left pending after a restart."""
        return await run_pending_catalog_hydration(
            session_factory,
            series_service=self._series_service,
        )

    async def _process_series_files(
        self,
        session: AsyncSession,
        job: ImportJob,
        item: ImportedSeries,
        *,
        duplicate_mode: bool = False,
        series_id_override: int | None = None,
        report_file_progress: ReportFileProgressFunc | None = None,
    ) -> tuple[int, int]:
        """Register matched files for a successfully imported series.

        After the series is imported into Pullbox, real Issue records exist.
        This method resolves pre-import file matches (by parsed_issue_number
        or comicvine_issue_id) to actual Issue IDs, then moves the files
        into the library via register_library_file().

        Returns (files_imported, files_failed).
        """
        return await process_series_files_for_import(
            session,
            job,
            item,
            duplicate_mode=duplicate_mode,
            series_id_override=series_id_override,
            load_media_settings=self._load_import_media_settings,
            load_trash_dir=self._load_utility_trash_dir,
            load_ingest_policy=self._load_import_ingest_policy,
            load_permission_policy=self._load_import_permission_policy,
            raise_if_cancelled=self._raise_if_job_cancelled,
            prepare_file=self._prepare_import_file,
            build_cached_comicinfo_payload=self._build_cached_comicinfo_payload_for_issue,
            apply_comicinfo=self._apply_comicinfo_to_imported_artifact,
            cleanup_prepared_file=self._cleanup_prepared_file,
            record_action=self._record_action,
            log_event=self._log_event,
            register_import_library_file=self._register_import_library_file,
            report_file_progress=report_file_progress,
            file_worker_count=self._settings.import_file_worker_count,
            session_factory=progress_session_factory_for_runtime(session),
        )

    async def override_cv_id(
        self,
        session: AsyncSession,
        imported_series_id: int,
        cv_id: int,
        *,
        rematch_files: bool = True,
    ) -> ImportedSeries:
        """User manually sets a CV ID for a NO_MATCH or MATCHED candidate."""
        return await override_import_cv_id(
            session,
            imported_series_id,
            cv_id,
            fetch_series_metadata=self._fetch_series_metadata_for_override,
            reclassify_matched_series_duplicates=self._reclassify_matched_series_duplicates,
            logical_series_group_key=self._logical_series_group_key,
            logical_group_series_ids=self._logical_group_series_ids,
            reset_series_group_files=self._reset_series_group_files,
            consolidate_logical_series_groups=self._consolidate_logical_series_groups,
            run_file_matching=self._run_file_matching,
            rematch_files=rematch_files,
        )

    async def _fetch_series_metadata_for_override(self, cv_id: int) -> SeriesMetadata:
        """Fetch ComicVine metadata for a manual imported-series override."""
        return await self._metadata_service._provider.get_series(str(cv_id))

    async def rematch_imported_series_files(
        self,
        session: AsyncSession,
        job_id: int,
        imported_series_id: int,
    ) -> ImportedSeries:
        """Rerun file matching for one import-review series after an override."""
        job = await session.get(ImportJob, job_id)
        if job is None:
            raise NotFoundError("ImportJob", job_id)
        item = await session.get(ImportedSeries, imported_series_id)
        if item is None:
            raise NotFoundError("ImportedSeries", imported_series_id)

        await self._log_event(
            session,
            job.id,
            "INFO",
            "import_series_file_rematch_started",
            message=f"Rematching files for {item.raw_series_name}",
            series=item.raw_series_name,
            imported_series_id=item.id,
            cv_id=item.cv_id,
        )
        await self._run_file_matching(session, job, series_ids=[item.id])

        refreshed = await session.get(ImportedSeries, imported_series_id)
        if refreshed is None:
            raise NotFoundError("ImportedSeries", imported_series_id)
        diagnostics = dict(refreshed.diagnostics or {})
        if diagnostics.pop("rematch_pending", None) is not None:
            refreshed.diagnostics = diagnostics
        await session.flush()
        await self._log_event(
            session,
            job.id,
            "INFO",
            "import_series_file_rematch_completed",
            message=f"Finished rematching files for {refreshed.raw_series_name}",
            series=refreshed.raw_series_name,
            imported_series_id=refreshed.id,
            status=refreshed.status.value,
            files_matched=refreshed.files_matched,
            files_no_match=refreshed.files_no_match,
            files_conflict=refreshed.files_conflict,
        )
        return refreshed

    async def _log_event(
        self,
        session: AsyncSession,
        job_id: int,
        level: str,
        event: str,
        message: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Emit one structured import event to detail logs, summaries, and the DB."""
        entry = log_import_event(
            session,
            job_id,
            level,
            event,
            message=message,
            detail_logger=import_detail_logger,
            root_logger=logger,
            root_summary_events=_IMPORT_ROOT_SUMMARY_EVENTS,
            data=kwargs,
        )
        await publish(
            f"import:{job_id}",
            "log",
            {
                "stream_token": uuid4().hex,
                "job_id": job_id,
                "logged_at": entry.logged_at.isoformat(),
                "level": entry.level,
                "event": entry.event,
                "message": entry.message,
                "data": dict(entry.data or {}),
            },
        )
