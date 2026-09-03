"""Scan-to-review import pipeline orchestration."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import func, select

from pullbox.core.exceptions import (
    JobCancelledError,
    JobPausedError,
    NotFoundError,
    ValidationError,
)
from pullbox.core.library_layout import SourceLayoutSpec
from pullbox.core.mylar3_reader import (
    Mylar3ArcSettingsSnapshot,
    Mylar3CollectionSnapshot,
    Mylar3ImportMetadataSnapshot,
)
from pullbox.core.sqlite_lock import is_sqlite_locked_error
from pullbox.models.import_job import (
    ImportedSeries,
    ImportFileHandlingMode,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.schemas.import_job import ImportProgressEvent
from pullbox.services.import_mylar_scan_progress import MylarScanProgress
from pullbox.services.import_progress_runtime import current_item_payload
from pullbox.services.import_referenced_sources import (
    load_mylar_reference_root_boundaries,
    validate_mylar_in_place_files,
)
from pullbox.services.import_story_arc_resolution import (
    StoryArcResolutionResult,
    resolve_staged_story_arc_entries,
)
from pullbox.services.import_story_arc_staging import (
    StoryArcStagingResult,
    stage_folder_story_arcs,
    stage_mylar_story_arcs,
)
from pullbox.services.import_workflow_state import (
    SCAN_PROGRESS_ANALYZE_START,
    SCAN_PROGRESS_FILE_MATCH_START,
    SCAN_PROGRESS_MATCH_START,
    SCAN_PROGRESS_MATERIALIZE_END,
    SCAN_PROGRESS_MATERIALIZE_START,
    emit_live_progress,
    inventory_progress_hint,
    sync_progress_snapshot_state,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.core.collection_scanner import DiscoveredSeries

    ProgressCallback = Callable[[ImportProgressEvent], Awaitable[None]]
    ResetScanArtifactsFunc = Callable[[AsyncSession, ImportJob], Awaitable[None]]
    LogEventFunc = Callable[..., Awaitable[None]]
    EmitProgressFunc = Callable[
        [AsyncSession, ImportJob, ImportProgressEvent, ProgressCallback],
        Awaitable[None],
    ]
    JobStatsFunc = Callable[[ImportJob], dict[str, int]]
    EstimateRemainingFunc = Callable[[datetime | None, int], int | None]
    SlowPhaseDelayFunc = Callable[[], Awaitable[None]]
    ResolveFileExtensionsFunc = Callable[[AsyncSession, str | None], Awaitable[frozenset[str]]]
    AutoDetectPathMapFunc = Callable[[Path], dict[str, str] | None]
    ValidateDiscoveredFilesFunc = Callable[..., Awaitable[None]]
    MaterializeScanResultsFunc = Callable[..., Awaitable[Any]]
    RaiseIfCancelledFunc = Callable[[AsyncSession, int], Awaitable[None]]
    DeduplicateSeriesFunc = Callable[..., Awaitable[None]]
    RunMatchingFunc = Callable[..., Awaitable[None]]
    ConsolidateLogicalGroupsFunc = Callable[..., Awaitable[Any]]
    RunFileMatchingFunc = Callable[..., Awaitable[None]]
    PhaseProgressFunc = Callable[[int, int, int, int], int]


def _scan_item_percentage(phase: str, progress: int, current_series: str | None) -> int:
    # Named discovery events describe a completed cohort, not overall work.
    if current_series:
        return 100
    if phase == "scanning":
        span = SCAN_PROGRESS_MATERIALIZE_END - SCAN_PROGRESS_MATERIALIZE_START
        return max(0, min(100, round((progress - SCAN_PROGRESS_MATERIALIZE_START) * 100 / span)))
    return 0


def _scan_failure_message(exc: Exception) -> str:
    """Return a user-facing import scan failure message."""
    if is_sqlite_locked_error(exc):
        return (
            "Database was busy while saving import progress. "
            "Please retry the import; WAL mode and shorter UI transactions help prevent this."
        )
    return str(exc)


async def _log_story_arc_staging_summary(
    session: AsyncSession,
    *,
    job_id: int,
    source_type: ImportSourceType,
    result: StoryArcStagingResult,
    log_event: LogEventFunc,
) -> None:
    """Log path-free counts for review-only story-arc staging."""
    await log_event(
        session,
        job_id,
        "INFO",
        "import_story_arc_staging_completed",
        message="Story-arc evidence staged for review",
        source_type=source_type.value,
        arcs_staged=result.arcs_staged,
        entries_staged=result.entries_staged,
        needs_review=result.needs_review,
        cohorts_examined=result.cohorts_examined,
        cohorts_skipped=result.cohorts_skipped,
        readlist_present=result.readlist_present,
        readlist_count=result.readlist_count,
    )


async def _log_story_arc_resolution_summary(
    session: AsyncSession,
    *,
    job_id: int,
    result: StoryArcResolutionResult,
    log_event: LogEventFunc,
) -> None:
    """Log path-free story-arc resolution counts."""
    await log_event(
        session,
        job_id,
        "INFO",
        "import_story_arc_resolution_completed",
        message="Staged story-arc entries resolved for review",
        entries_examined=result.entries_examined,
        resolved=result.resolved,
        pending=result.pending,
        missing=result.missing,
        ambiguous=result.ambiguous,
        conflicts=result.conflicts,
        skipped=result.skipped,
        linked_files=result.linked_files,
    )


async def run_import_scan_pipeline(
    session: AsyncSession,
    job_id: int,
    *,
    scanner_cls: Any,
    mylar3_reader_cls: Any,
    auto_detect_mylar3_path_map: AutoDetectPathMapFunc,
    reset_scan_artifacts: ResetScanArtifactsFunc,
    resolve_import_file_extensions: ResolveFileExtensionsFunc,
    validate_discovered_files_safety: ValidateDiscoveredFilesFunc,
    materialize_discovered_scan_results: MaterializeScanResultsFunc,
    deduplicate_series: DeduplicateSeriesFunc,
    run_matching: RunMatchingFunc,
    consolidate_logical_series_groups: ConsolidateLogicalGroupsFunc,
    run_file_matching: RunFileMatchingFunc,
    raise_if_cancelled: RaiseIfCancelledFunc,
    log_event: LogEventFunc,
    emit_progress: EmitProgressFunc,
    phase_progress: PhaseProgressFunc,
    estimate_remaining_seconds: EstimateRemainingFunc,
    job_stats: JobStatsFunc,
    maybe_slow_phase_delay: SlowPhaseDelayFunc,
    progress_callback: ProgressCallback | None = None,
) -> None:
    """Run the full import scan pipeline through review readiness."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)

    pipeline_started_at = time.monotonic()
    scan_duration_ms = 0
    analyze_duration_ms = 0
    series_matching_duration_ms = 0
    file_matching_duration_ms = 0
    runtime_revision_state: dict[str, int] = {"value": 0}

    try:
        await reset_scan_artifacts(session, job)

        job.status = ImportJobStatus.SCANNING
        job.scan_started_at = datetime.now(UTC)

        await log_event(
            session,
            job_id,
            "INFO",
            "import_scan_started",
            message="Scan phase started",
        )
        await session.commit()

        if progress_callback:
            runtime_revision_state["value"] = int(job.progress_revision or 0)
            await emit_progress(
                session,
                job,
                ImportProgressEvent(
                    job_id=job_id,
                    status=ImportJobStatus.SCANNING,
                    phase="scanning",
                    progress=0,
                    message="Inventorying collection...",
                    estimated_seconds_remaining=None,
                    **current_item_payload(
                        kind="scan",
                        stage="inventory",
                        progress_pct=0,
                    ),
                    **job_stats(job),
                ),
                progress_callback,
            )
            runtime_revision_state["value"] = max(
                runtime_revision_state["value"],
                int(job.progress_revision or 0),
            )
            await maybe_slow_phase_delay()

        async def emit_scan_progress(
            *,
            status: ImportJobStatus,
            phase: str,
            message: str,
            progress: int,
            current_series: str | None = None,
            current_series_status: ImportSeriesStatus | None = None,
        ) -> None:
            if not progress_callback:
                return
            await emit_progress(
                session,
                job,
                ImportProgressEvent(
                    job_id=job_id,
                    status=status,
                    phase=phase,
                    progress=progress,
                    message=message,
                    current_series=current_series,
                    current_series_status=current_series_status,
                    estimated_seconds_remaining=None,
                    **current_item_payload(
                        kind="series" if current_series else "scan",
                        stage=phase,
                        name=current_series,
                        progress_pct=_scan_item_percentage(phase, progress, current_series),
                    ),
                    **job_stats(job),
                ),
                progress_callback,
            )
            runtime_revision_state["value"] = max(
                runtime_revision_state["value"],
                int(job.progress_revision or 0),
            )

        async def forward_live_scan_progress(
            *,
            status: ImportJobStatus,
            phase: str,
            message: str,
            progress: int,
            current_series: str | None = None,
            current_series_status: ImportSeriesStatus | None = None,
        ) -> None:
            if not progress_callback:
                return
            await emit_live_progress(
                job,
                ImportProgressEvent(
                    job_id=job_id,
                    status=status,
                    phase=phase,
                    progress=progress,
                    message=message,
                    current_series=current_series,
                    current_series_status=current_series_status,
                    estimated_seconds_remaining=None,
                    **current_item_payload(
                        kind="series" if current_series else "scan",
                        stage=phase,
                        name=current_series,
                        progress_pct=_scan_item_percentage(phase, progress, current_series),
                    ),
                    **job_stats(job),
                ),
                progress_callback=progress_callback,
                revision_state=runtime_revision_state,
                started_at=job.scan_started_at,
            )

        scan_materialized_incrementally = False
        discovered_list: list[DiscoveredSeries] = []

        scan_phase_started_at = time.monotonic()
        if job.source_type == ImportSourceType.MYLAR3:
            discovered_count = await _load_mylar3_discovered_series(
                session,
                job,
                job_id=job_id,
                mylar3_reader_cls=mylar3_reader_cls,
                auto_detect_mylar3_path_map=auto_detect_mylar3_path_map,
                log_event=log_event,
                validate_discovered_files_safety=validate_discovered_files_safety,
                materialize_discovered_scan_results=materialize_discovered_scan_results,
                raise_if_cancelled=raise_if_cancelled,
                progress_callback=progress_callback,
            )
            await emit_scan_progress(
                status=ImportJobStatus.SCANNING,
                phase="scanning",
                message=f"Loaded {discovered_count} series from Mylar3.",
                progress=SCAN_PROGRESS_MATERIALIZE_END,
            )
            scan_materialized_incrementally = True
        else:
            discovered_count = await _scan_collection_discovered_series(
                session,
                job,
                scanner_cls=scanner_cls,
                resolve_import_file_extensions=resolve_import_file_extensions,
                emit_scan_progress=forward_live_scan_progress,
                phase_progress=phase_progress,
                validate_discovered_files_safety=validate_discovered_files_safety,
                materialize_discovered_scan_results=materialize_discovered_scan_results,
                log_event=log_event,
                raise_if_cancelled=raise_if_cancelled,
            )
            scan_materialized_incrementally = True

        if progress_callback:
            await emit_scan_progress(
                status=ImportJobStatus.SCANNING,
                phase="scanning",
                message=f"Scan complete: {discovered_count} series ready for analysis.",
                progress=SCAN_PROGRESS_MATERIALIZE_END,
            )

        if not scan_materialized_incrementally:
            await validate_discovered_files_safety(session, discovered_list)
            await materialize_discovered_scan_results(
                session,
                job,
                discovered_list,
            )

        if job.source_type == ImportSourceType.FILESYSTEM:

            async def check_folder_staging_cancellation() -> None:
                await raise_if_cancelled(session, job_id)

            story_arc_staging = await stage_folder_story_arcs(
                session,
                import_job_id=job_id,
                cancellation_check=check_folder_staging_cancellation,
            )
            await _log_story_arc_staging_summary(
                session,
                job_id=job_id,
                source_type=job.source_type,
                result=story_arc_staging,
                log_event=log_event,
            )

        await log_event(
            session,
            job_id,
            "INFO",
            "import_scan_completed",
            message=f"Scan complete: {discovered_count} series found",
            series_found=discovered_count,
            duration_ms=round((time.monotonic() - scan_phase_started_at) * 1000),
        )
        scan_duration_ms = round((time.monotonic() - scan_phase_started_at) * 1000)

        job.status = ImportJobStatus.ANALYZING
        await session.commit()

        if progress_callback:
            matching_message = (
                "Resolving local source identities..."
                if job.source_type == ImportSourceType.FILESYSTEM
                else "Matching against ComicVine..."
            )
            await emit_progress(
                session,
                job,
                ImportProgressEvent(
                    job_id=job_id,
                    status=ImportJobStatus.ANALYZING,
                    phase="analyzing",
                    progress=SCAN_PROGRESS_ANALYZE_START,
                    message="Analyzing for duplicates...",
                    estimated_seconds_remaining=estimate_remaining_seconds(
                        job.scan_started_at,
                        SCAN_PROGRESS_ANALYZE_START,
                    ),
                    **current_item_payload(
                        kind="scan",
                        stage="analyzing",
                        progress_pct=SCAN_PROGRESS_ANALYZE_START,
                    ),
                    **job_stats(job),
                ),
                progress_callback,
            )
            await maybe_slow_phase_delay()

        await raise_if_cancelled(session, job_id)

        analyze_started_at = time.monotonic()
        await deduplicate_series(session, job, progress_callback=progress_callback)
        analyze_duration_ms = round((time.monotonic() - analyze_started_at) * 1000)
        await raise_if_cancelled(session, job_id)

        job.status = ImportJobStatus.MATCHING
        job.match_started_at = datetime.now(UTC)
        await session.commit()

        if progress_callback:
            await emit_progress(
                session,
                job,
                ImportProgressEvent(
                    job_id=job_id,
                    status=ImportJobStatus.MATCHING,
                    phase="matching",
                    progress=SCAN_PROGRESS_MATCH_START,
                    message=matching_message,
                    estimated_seconds_remaining=estimate_remaining_seconds(
                        job.scan_started_at,
                        SCAN_PROGRESS_MATCH_START,
                    ),
                    **current_item_payload(
                        kind="scan",
                        stage="matching",
                        progress_pct=SCAN_PROGRESS_MATCH_START,
                    ),
                    **job_stats(job),
                ),
                progress_callback,
            )
            await maybe_slow_phase_delay()

        series_matching_started_at = time.monotonic()
        await run_matching(session, job, progress_callback=progress_callback)
        await raise_if_cancelled(session, job_id)

        await consolidate_logical_series_groups(session, job)
        series_matching_duration_ms = round((time.monotonic() - series_matching_started_at) * 1000)
        await raise_if_cancelled(session, job_id)

        job.match_completed_at = datetime.now(UTC)

        job.status = ImportJobStatus.FILE_MATCHING
        await session.commit()

        if progress_callback:
            await emit_progress(
                session,
                job,
                ImportProgressEvent(
                    job_id=job_id,
                    status=ImportJobStatus.FILE_MATCHING,
                    phase="file_matching",
                    progress=SCAN_PROGRESS_FILE_MATCH_START,
                    message="Matching files to issues...",
                    estimated_seconds_remaining=estimate_remaining_seconds(
                        job.scan_started_at,
                        SCAN_PROGRESS_FILE_MATCH_START,
                    ),
                    **current_item_payload(
                        kind="scan",
                        stage="file_matching",
                        progress_pct=SCAN_PROGRESS_FILE_MATCH_START,
                    ),
                    **job_stats(job),
                ),
                progress_callback,
            )
            await maybe_slow_phase_delay()

        file_matching_started_at = time.monotonic()
        await run_file_matching(session, job, progress_callback=progress_callback)
        file_matching_duration_ms = round((time.monotonic() - file_matching_started_at) * 1000)

        async def check_story_arc_resolution_cancellation() -> None:
            await raise_if_cancelled(session, job_id)

        story_arc_resolution = await resolve_staged_story_arc_entries(
            session,
            import_job_id=job_id,
            cancellation_check=check_story_arc_resolution_cancellation,
        )
        await _log_story_arc_resolution_summary(
            session,
            job_id=job_id,
            result=story_arc_resolution,
            log_event=log_event,
        )
        await raise_if_cancelled(session, job_id)

        job.status = ImportJobStatus.REVIEW

        await log_event(
            session,
            job_id,
            "INFO",
            "import_step2_timing",
            message="Step 2 timing metrics collected",
            scan_duration_ms=scan_duration_ms,
            analyze_duration_ms=analyze_duration_ms,
            series_matching_duration_ms=series_matching_duration_ms,
            file_matching_duration_ms=file_matching_duration_ms,
            total_duration_ms=round((time.monotonic() - pipeline_started_at) * 1000),
        )

        await log_event(
            session,
            job_id,
            "INFO",
            "import_ready_for_review",
            message="All phases complete, ready for user review",
            series_matched=job.series_matched,
            series_duplicate=job.series_duplicate,
            series_no_match=job.series_no_match,
        )
        await session.commit()

        if progress_callback:
            await emit_progress(
                session,
                job,
                ImportProgressEvent(
                    job_id=job_id,
                    status=ImportJobStatus.REVIEW,
                    phase="review",
                    progress=100,
                    message="Ready for review",
                    **current_item_payload(
                        kind="scan",
                        stage="review",
                        progress_pct=100,
                    ),
                    **job_stats(job),
                ),
                progress_callback,
            )
            await maybe_slow_phase_delay()

    except JobPausedError:
        await log_event(
            session,
            job_id,
            "INFO",
            "import_scan_paused",
            message="Import scan paused.",
        )
        raise
    except JobCancelledError:
        await log_event(
            session,
            job_id,
            "INFO",
            "import_scan_cancelled",
            message="Import scan cancelled.",
        )
        raise
    except Exception as exc:
        with contextlib.suppress(Exception):
            await session.rollback()

        try:
            failed_job = await session.get(ImportJob, job_id)
            if failed_job is not None:
                failed_job.status = ImportJobStatus.FAILED
                failure_message = _scan_failure_message(exc)
                failed_job.error_message = failure_message
                await log_event(
                    session,
                    job_id,
                    "ERROR",
                    "import_scan_failed",
                    message=f"Scan pipeline failed: {failure_message}",
                )
                await session.commit()
        except Exception:
            with contextlib.suppress(Exception):
                await session.rollback()

        raise


async def _load_mylar3_discovered_series(
    session: AsyncSession,
    job: ImportJob,
    *,
    job_id: int,
    mylar3_reader_cls: Any,
    auto_detect_mylar3_path_map: AutoDetectPathMapFunc,
    log_event: LogEventFunc,
    validate_discovered_files_safety: ValidateDiscoveredFilesFunc | None = None,
    materialize_discovered_scan_results: MaterializeScanResultsFunc | None = None,
    raise_if_cancelled: RaiseIfCancelledFunc | None = None,
    progress_callback: ProgressCallback | None = None,
) -> int:
    db_path = Path(job.source_path)
    if db_path.is_dir():
        db_path = db_path / "mylar.db"

    if not job.mylar3_path_map_confirmed:
        raise ValidationError(
            "Return to Import Step 1 and confirm the Mylar path mapping before scanning."
        )
    path_map: dict[str, str] | None = job.mylar3_path_map
    in_place = job.file_handling_mode == ImportFileHandlingMode.IN_PLACE
    in_place_root_boundaries = (
        await load_mylar_reference_root_boundaries(session) if in_place else ()
    )
    reader_options: dict[str, object] = {}
    if in_place:
        reader_options["include_missing_files"] = True
        reader_options["reference_root_boundaries"] = tuple(
            (boundary.lexical, boundary.resolved) for boundary in in_place_root_boundaries
        )
    reader = mylar3_reader_cls(
        db_path=db_path,
        path_map=path_map or None,
        source_layout=SourceLayoutSpec.from_dict(dict(job.source_layout_snapshot or {})),
        **reader_options,
    )
    if raise_if_cancelled is not None:
        await raise_if_cancelled(session, job_id)

    async def check_mylar_staging_cancellation() -> None:
        if raise_if_cancelled is not None:
            await raise_if_cancelled(session, job_id)

    read_import_metadata = getattr(reader, "read_import_metadata", None)
    iter_story_arc_pages = getattr(reader, "iter_import_story_arc_pages", None)
    iter_series_pages = getattr(reader, "iter_import_series_pages", None)
    paged_reader = (
        inspect.iscoroutinefunction(read_import_metadata)
        and inspect.isasyncgenfunction(iter_story_arc_pages)
        and inspect.isasyncgenfunction(iter_series_pages)
    )
    legacy_snapshot: Mylar3CollectionSnapshot | None = None
    if paged_reader:
        metadata = cast("Mylar3ImportMetadataSnapshot", await reader.read_import_metadata())
    else:
        legacy_snapshot = await _read_mylar3_collection_snapshot(reader)
        metadata = Mylar3ImportMetadataSnapshot(
            storyarcs_present=legacy_snapshot.storyarcs_present,
            readlist_present=legacy_snapshot.readlist_present,
            readlist_count=legacy_snapshot.readlist_count,
            arc_settings=legacy_snapshot.arc_settings,
            series_count=len(legacy_snapshot.series),
        )

    story_arc_staging = StoryArcStagingResult(
        readlist_present=metadata.readlist_present,
        readlist_count=metadata.readlist_count,
    )

    async def stage_arc_page(
        arc_page: tuple[Any, ...],
        *,
        source_ordinal_offset: int,
    ) -> StoryArcStagingResult:
        page_snapshot = Mylar3CollectionSnapshot(
            series=(),
            story_arcs=cast("Any", arc_page),
            storyarcs_present=metadata.storyarcs_present,
            readlist_present=metadata.readlist_present,
            readlist_count=metadata.readlist_count,
            arc_settings=metadata.arc_settings,
        )
        return await stage_mylar_story_arcs(
            session,
            import_job_id=job_id,
            snapshot=page_snapshot,
            source_ordinal_offset=source_ordinal_offset,
            cancellation_check=check_mylar_staging_cancellation,
        )

    def combine_staging(
        current: StoryArcStagingResult,
        page_result: StoryArcStagingResult,
    ) -> StoryArcStagingResult:
        return StoryArcStagingResult(
            arcs_staged=current.arcs_staged + page_result.arcs_staged,
            entries_staged=current.entries_staged + page_result.entries_staged,
            needs_review=current.needs_review + page_result.needs_review,
            cohorts_examined=current.cohorts_examined + page_result.cohorts_examined,
            cohorts_skipped=current.cohorts_skipped + page_result.cohorts_skipped,
            readlist_present=metadata.readlist_present,
            readlist_count=metadata.readlist_count,
        )

    source_ordinal_offset = 0
    if paged_reader:
        async for raw_arc_page in reader.iter_import_story_arc_pages():
            await check_mylar_staging_cancellation()
            arc_page = tuple(raw_arc_page)
            if not arc_page:
                continue
            page_result = await stage_arc_page(
                arc_page,
                source_ordinal_offset=source_ordinal_offset,
            )
            story_arc_staging = combine_staging(story_arc_staging, page_result)
            source_ordinal_offset += len(arc_page)
            await session.commit()
    elif legacy_snapshot is not None:
        page_result = await stage_arc_page(
            tuple(legacy_snapshot.story_arcs),
            source_ordinal_offset=0,
        )
        story_arc_staging = combine_staging(story_arc_staging, page_result)

    await _log_story_arc_staging_summary(
        session,
        job_id=job_id,
        source_type=job.source_type,
        result=story_arc_staging,
        log_event=log_event,
    )
    await session.commit()

    scan_progress = MylarScanProgress(
        session,
        job,
        metadata.series_count,
        check_mylar_staging_cancellation,
        callback=progress_callback,
    )

    path_status_counts: dict[str, int] = {}
    mapping_applied_series = 0
    incompatible_series = 0
    series_count = 0
    file_count = 0
    fallback_source_folders: set[str] = set()

    async def persist_series_page(raw_page: tuple[Any, ...]) -> None:
        nonlocal incompatible_series, mapping_applied_series, series_count, file_count
        await check_mylar_staging_cancellation()
        page = cast("list[DiscoveredSeries]", list(raw_page))
        if not page:
            return
        page_started_at = time.monotonic()
        source_rows_read = getattr(reader, "import_series_rows_read", None)
        scan_progress.source_page_end = (
            source_rows_read
            if isinstance(source_rows_read, int)
            else scan_progress.source_completed + len(page)
        )
        await scan_progress.report_safety(0, sum(len(item.files) for item in page), "")
        inspection_started_at = time.monotonic()
        if in_place:
            await asyncio.to_thread(
                validate_mylar_in_place_files,
                page,
                in_place_root_boundaries,
            )
        if validate_discovered_files_safety is not None:
            await validate_discovered_files_safety(
                session, page, progress_callback=scan_progress.report_safety
            )
        if materialize_discovered_scan_results is not None:
            persistence_started_at = time.monotonic()
            await materialize_discovered_scan_results(session, job, page)
        else:
            persistence_started_at = time.monotonic()
        persistence_duration_ms = round((time.monotonic() - persistence_started_at) * 1000)
        inspection_duration_ms = round((persistence_started_at - inspection_started_at) * 1000)
        job.scan_completed_at = None

        for series in page:
            series_count += 1
            file_count += series.file_count
            if materialize_discovered_scan_results is None and series.source_folder:
                fallback_source_folders.add(series.source_folder)
            path_details = series.diagnostics.get("mylar3_path")
            if not isinstance(path_details, dict):
                continue
            status = path_details.get("status")
            if isinstance(status, str):
                path_status_counts[status] = path_status_counts.get(status, 0) + 1
                if status not in {"local", "mapped"}:
                    incompatible_series += 1
            if path_details.get("mapping_applied") is True:
                mapping_applied_series += 1

        job.scan_total_files = file_count
        job.series_found = series_count
        await log_event(
            session,
            job_id,
            "INFO",
            "import_mylar_batch_scanned",
            message=f"Prepared {series_count} series and {file_count} file records from Mylar.",
            series_found=series_count,
            files_found=file_count,
            inspection_duration_ms=inspection_duration_ms,
            persistence_duration_ms=persistence_duration_ms,
            duration_ms=round((time.monotonic() - page_started_at) * 1000),
        )
        await scan_progress.checkpoint_page()
        await check_mylar_staging_cancellation()

    if paged_reader:
        async for raw_series_page in reader.iter_import_series_pages():
            await persist_series_page(tuple(raw_series_page))
    elif legacy_snapshot is not None:
        await persist_series_page(tuple(legacy_snapshot.series))

    if materialize_discovered_scan_results is not None:
        distinct_source_folders = await session.scalar(
            select(func.count(func.distinct(ImportedSeries.source_folder))).where(
                ImportedSeries.import_job_id == job_id,
                ImportedSeries.source_folder.is_not(None),
                ImportedSeries.source_folder != "",
            )
        )
        job.scan_total_dirs = int(distinct_source_folders or 0)
    else:
        job.scan_total_dirs = len(fallback_source_folders)

    await log_event(
        session,
        job_id,
        "INFO",
        "mylar3_path_resolution",
        message="Resolved Mylar comic folders",
        path_status_counts=dict(sorted(path_status_counts.items())),
        mapping_applied_series=mapping_applied_series,
        incompatible_series=incompatible_series,
    )
    job.scan_total_files = file_count
    job.series_found = series_count
    job.scan_completed_at = datetime.now(UTC)
    await session.commit()
    return series_count


async def _read_mylar3_collection_snapshot(reader: Any) -> Mylar3CollectionSnapshot:
    """Read one complete Mylar snapshot, with a series-only test-double fallback."""
    read_collection = getattr(reader, "read_collection", None)
    if callable(read_collection) and inspect.iscoroutinefunction(read_collection):
        return cast("Mylar3CollectionSnapshot", await read_collection())

    read_snapshot = getattr(reader, "read_snapshot", None)
    if callable(read_snapshot) and inspect.iscoroutinefunction(read_snapshot):
        return cast("Mylar3CollectionSnapshot", await read_snapshot())

    discovered_list = await reader.read_series()
    return Mylar3CollectionSnapshot(
        series=tuple(discovered_list),
        story_arcs=(),
        storyarcs_present=False,
        readlist_present=False,
        readlist_count=0,
        arc_settings=Mylar3ArcSettingsSnapshot(
            present=False,
            parse_warnings=(),
            values=(),
        ),
    )


async def _scan_collection_discovered_series(
    session: AsyncSession,
    job: ImportJob,
    *,
    scanner_cls: Any,
    resolve_import_file_extensions: ResolveFileExtensionsFunc,
    emit_scan_progress: Callable[..., Awaitable[None]],
    phase_progress: PhaseProgressFunc,
    validate_discovered_files_safety: ValidateDiscoveredFilesFunc | None = None,
    materialize_discovered_scan_results: MaterializeScanResultsFunc | None = None,
    log_event: LogEventFunc | None = None,
    raise_if_cancelled: RaiseIfCancelledFunc | None = None,
) -> int:
    custom_exts = await resolve_import_file_extensions(session, job.file_formats)
    batch: list[DiscoveredSeries] = []
    series_found = 0
    last_batch_flush_at = time.monotonic()

    async def forward_live_scan_progress(
        *,
        phase: str,
        message: str,
        progress: int,
        current_series: str | None = None,
        current_series_status: ImportSeriesStatus | None = None,
    ) -> None:
        await emit_scan_progress(
            status=ImportJobStatus.SCANNING,
            phase=phase,
            message=message,
            progress=progress,
            current_series=current_series,
            current_series_status=current_series_status,
        )

    async def capture_inventory_progress(directories_visited: int, files_found: int) -> None:
        job.scan_total_dirs = directories_visited
        job.scan_total_files = files_found
        await forward_live_scan_progress(
            phase="inventory",
            message=(
                f"Inventorying collection · {files_found} comic files across "
                f"{directories_visited} directories visited."
            ),
            progress=inventory_progress_hint(directories_visited),
        )

    async def capture_scan_totals(files_found: int, directories_visited: int) -> None:
        job.scan_total_dirs = directories_visited
        job.scan_total_files = files_found

    async def check_scan_cancellation() -> None:
        if raise_if_cancelled is not None:
            await raise_if_cancelled(session, job.id)

    materialized_files = 0
    last_materialized_emit = 0.0

    async def capture_materialized_file(_delta: int) -> None:
        nonlocal materialized_files, last_materialized_emit
        materialized_files += 1
        if job.scan_total_files <= 0:
            return

        loop = asyncio.get_running_loop()
        now = loop.time()
        if materialized_files < job.scan_total_files and (now - last_materialized_emit) < 1.0:
            return

        last_materialized_emit = now
        await forward_live_scan_progress(
            phase="scanning",
            message=(
                f"Scanning files · {materialized_files}/{job.scan_total_files} files processed."
            ),
            progress=phase_progress(
                SCAN_PROGRESS_MATERIALIZE_START,
                SCAN_PROGRESS_MATERIALIZE_END,
                materialized_files,
                max(job.scan_total_files, materialized_files),
            ),
        )

    async def flush_scan_batch(*, force: bool = False) -> None:
        nonlocal last_batch_flush_at
        if not batch:
            return

        now = time.monotonic()
        if not force and len(batch) < 25 and (now - last_batch_flush_at) < 0.5:
            return

        batch_to_flush = list(batch)
        batch.clear()
        last_batch_flush_at = now
        inspection_started_at = time.monotonic()

        if validate_discovered_files_safety is not None:
            batch_for_validation: list[DiscoveredSeries] = []
            batch_checked_paths: set[str] = set()
            for discovered in batch_to_flush:
                pending_files = [
                    discovered_file
                    for discovered_file in discovered.files
                    if discovered_file.file_path not in batch_checked_paths
                ]
                if not pending_files:
                    continue
                batch_checked_paths.update(
                    discovered_file.file_path for discovered_file in pending_files
                )
                batch_for_validation.append(
                    discovered.__class__(
                        raw_series_name=discovered.raw_series_name,
                        raw_year=discovered.raw_year,
                        raw_publisher=discovered.raw_publisher,
                        file_count=len(pending_files),
                        sample_paths=discovered.sample_paths,
                        source_folder=discovered.source_folder,
                        source_folder_relative=discovered.source_folder_relative,
                        files=pending_files,
                        has_files=discovered.has_files,
                        mylar3_cv_id=discovered.mylar3_cv_id,
                        folder_cv_id=discovered.folder_cv_id,
                        comicinfo_cv_id=discovered.comicinfo_cv_id,
                        comicinfo_source=discovered.comicinfo_source,
                        diagnostics=dict(discovered.diagnostics),
                    )
                )
            if batch_for_validation:
                await validate_discovered_files_safety(session, batch_for_validation)

        persistence_started_at = time.monotonic()
        if materialize_discovered_scan_results is not None:
            await materialize_discovered_scan_results(session, job, batch_to_flush)
            # The shared materializer also supports one-shot callers and sets
            # this counter to the size of the list it receives. Restore the
            # cumulative scanner count after each incremental batch so large
            # scans do not appear to contain only their final (partial) batch.
            job.series_found = series_found

        if log_event is not None:
            await log_event(
                session,
                job.id,
                "DEBUG",
                "import_scan_batch_discovered",
                message=f"Discovered {len(batch_to_flush)} series ({series_found} total so far)",
                series_found_in_batch=len(batch_to_flush),
                total_series_found=series_found,
                inspection_duration_ms=round(
                    (persistence_started_at - inspection_started_at) * 1000
                ),
                persistence_duration_ms=round((time.monotonic() - persistence_started_at) * 1000),
                sample_series=[item.raw_series_name for item in batch_to_flush[:5]],
            )

        sync_progress_snapshot_state(
            job,
            status=ImportJobStatus.SCANNING,
            mode="scan",
            phase="scanning",
            progress=phase_progress(
                SCAN_PROGRESS_MATERIALIZE_START,
                SCAN_PROGRESS_MATERIALIZE_END,
                materialized_files,
                max(job.scan_total_files, materialized_files, 1),
            ),
            message=(
                f"Discovered {series_found} series · "
                f"{materialized_files}/{max(job.scan_total_files, 1)} files processed."
            ),
            current_series_name=batch_to_flush[-1].raw_series_name if batch_to_flush else None,
        )
        await session.commit()

    scanner = scanner_cls(
        min_file_count=job.min_files_per_series,
        progress_callback=capture_scan_totals,
        file_progress_callback=capture_materialized_file,
        inventory_progress_callback=capture_inventory_progress,
        cancellation_check=check_scan_cancellation if raise_if_cancelled is not None else None,
        extensions=custom_exts,
        source_layout=SourceLayoutSpec.from_dict(dict(job.source_layout_snapshot or {})),
    )
    file_paths_mode = list(job.selected_file_paths or [])

    if file_paths_mode:
        selected_discovered = await scanner.scan_files(
            file_paths_mode,
            root_path=job.source_path,
        )
        series_found = len(selected_discovered)
        job.scan_total_files = sum(series.file_count for series in selected_discovered)
        job.scan_total_dirs = len({series.source_folder for series in selected_discovered})
        job.series_found = series_found
        batch.extend(selected_discovered)
        await flush_scan_batch(force=True)
        await forward_live_scan_progress(
            phase="scanning",
            message=f"Prepared {series_found} series from the selected files.",
            progress=SCAN_PROGRESS_MATERIALIZE_END,
        )
        return series_found

    async for series in scanner.scan(job.source_path):
        series_found += 1
        batch.append(series)
        job.series_found = series_found
        await flush_scan_batch()
        await forward_live_scan_progress(
            phase="scanning",
            message=(
                f"Discovered {series_found} series · "
                f"{materialized_files}/"
                f"{max(job.scan_total_files, 1)} files processed."
            ),
            progress=phase_progress(
                SCAN_PROGRESS_MATERIALIZE_START,
                SCAN_PROGRESS_MATERIALIZE_END,
                materialized_files,
                max(job.scan_total_files, materialized_files),
            ),
            current_series=series.raw_series_name,
        )

    await flush_scan_batch(force=True)

    return series_found
