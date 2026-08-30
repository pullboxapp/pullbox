"""Scan-to-review import pipeline orchestration."""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pullbox.core.exceptions import JobCancelledError, JobPausedError, NotFoundError
from pullbox.core.library_layout import SourceLayoutSpec
from pullbox.core.sqlite_lock import is_sqlite_locked_error
from pullbox.models.import_job import (
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.schemas.import_job import ImportProgressEvent
from pullbox.services.import_progress_runtime import current_item_payload
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
    ValidateDiscoveredFilesFunc = Callable[
        [AsyncSession, list[DiscoveredSeries]],
        Awaitable[None],
    ]
    MaterializeScanResultsFunc = Callable[..., Awaitable[Any]]
    RaiseIfCancelledFunc = Callable[[AsyncSession, int], Awaitable[None]]
    DeduplicateSeriesFunc = Callable[..., Awaitable[None]]
    RunMatchingFunc = Callable[..., Awaitable[None]]
    ConsolidateLogicalGroupsFunc = Callable[..., Awaitable[Any]]
    RunFileMatchingFunc = Callable[..., Awaitable[None]]
    PhaseProgressFunc = Callable[[int, int, int, int], int]


def _scan_failure_message(exc: Exception) -> str:
    """Return a user-facing import scan failure message."""
    if is_sqlite_locked_error(exc):
        return (
            "Database was busy while saving import progress. "
            "Please retry the import; WAL mode and shorter UI transactions help prevent this."
        )
    return str(exc)


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
                    estimated_seconds_remaining=estimate_remaining_seconds(
                        (
                            job.import_started_at
                            if status == ImportJobStatus.IMPORTING
                            else job.scan_started_at
                        ),
                        progress,
                    ),
                    **current_item_payload(
                        kind="series" if current_series else "scan",
                        stage=phase,
                        name=current_series,
                        progress_pct=progress,
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
                    estimated_seconds_remaining=estimate_remaining_seconds(
                        (
                            job.import_started_at
                            if status == ImportJobStatus.IMPORTING
                            else job.scan_started_at
                        ),
                        progress,
                    ),
                    **current_item_payload(
                        kind="series" if current_series else "scan",
                        stage=phase,
                        name=current_series,
                        progress_pct=progress,
                    ),
                    **job_stats(job),
                ),
                progress_callback=progress_callback,
                revision_state=runtime_revision_state,
                started_at=job.scan_started_at,
            )

        scan_materialized_incrementally = False

        scan_phase_started_at = time.monotonic()
        if job.source_type == ImportSourceType.MYLAR3:
            discovered_list = await _load_mylar3_discovered_series(
                session,
                job,
                job_id=job_id,
                mylar3_reader_cls=mylar3_reader_cls,
                auto_detect_mylar3_path_map=auto_detect_mylar3_path_map,
                log_event=log_event,
            )
            await emit_scan_progress(
                status=ImportJobStatus.SCANNING,
                phase="scanning",
                message=f"Loaded {len(discovered_list)} series from Mylar3.",
                progress=SCAN_PROGRESS_MATERIALIZE_END,
            )
        else:
            discovered_list = await _scan_collection_discovered_series(
                session,
                job,
                scanner_cls=scanner_cls,
                resolve_import_file_extensions=resolve_import_file_extensions,
                emit_scan_progress=forward_live_scan_progress,
                phase_progress=phase_progress,
                validate_discovered_files_safety=validate_discovered_files_safety,
                materialize_discovered_scan_results=materialize_discovered_scan_results,
                log_event=log_event,
            )
            scan_materialized_incrementally = True

        if progress_callback:
            await emit_scan_progress(
                status=ImportJobStatus.SCANNING,
                phase="scanning",
                message=f"Scan complete: {len(discovered_list)} series ready for analysis.",
                progress=SCAN_PROGRESS_MATERIALIZE_END,
            )

        if not scan_materialized_incrementally:
            await validate_discovered_files_safety(session, discovered_list)
            await materialize_discovered_scan_results(
                session,
                job,
                discovered_list,
            )

        await log_event(
            session,
            job_id,
            "INFO",
            "import_scan_completed",
            message=f"Scan complete: {len(discovered_list)} series found",
            series_found=len(discovered_list),
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
) -> list[DiscoveredSeries]:
    db_path = Path(job.source_path)
    if db_path.is_dir():
        db_path = db_path / "mylar.db"

    path_map: dict[str, str] | None = job.mylar3_path_map
    if not path_map:
        detected = await asyncio.to_thread(lambda: auto_detect_mylar3_path_map(db_path))
        path_map = detected
        if detected:
            await log_event(
                session,
                job_id,
                "INFO",
                "mylar3_path_map_detected",
                message="Auto-detected Mylar3 path mapping",
                path_map=detected,
            )
        else:
            await log_event(
                session,
                job_id,
                "DEBUG",
                "mylar3_path_map_not_detected",
                message="No Mylar3 path mapping auto-detected",
                db_path=str(db_path),
            )
    reader = mylar3_reader_cls(
        db_path=db_path,
        path_map=path_map or None,
        source_layout=SourceLayoutSpec.from_dict(dict(job.source_layout_snapshot or {})),
    )
    discovered_list = await reader.read_series()

    path_status_counts: dict[str, int] = {}
    mapping_applied_series = 0
    incompatible_series = 0
    for series in discovered_list:
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
    job.scan_total_files = sum(series.file_count for series in discovered_list)
    job.scan_total_dirs = len(
        {series.source_folder for series in discovered_list if series.source_folder}
    )
    job.series_found = len(discovered_list)
    await session.commit()
    return list(discovered_list)


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
) -> list[DiscoveredSeries]:
    custom_exts = await resolve_import_file_extensions(session, job.file_formats)
    checked_paths: set[str] = set()
    batch: list[DiscoveredSeries] = []
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

        if validate_discovered_files_safety is not None:
            batch_for_validation: list[DiscoveredSeries] = []
            for discovered in batch_to_flush:
                pending_files = [
                    discovered_file
                    for discovered_file in discovered.files
                    if discovered_file.file_path not in checked_paths
                ]
                if not pending_files:
                    continue
                checked_paths.update(discovered_file.file_path for discovered_file in pending_files)
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

        if materialize_discovered_scan_results is not None:
            await materialize_discovered_scan_results(session, job, batch_to_flush)

        if log_event is not None:
            await log_event(
                session,
                job.id,
                "DEBUG",
                "import_scan_batch_discovered",
                message=(
                    f"Discovered {len(batch_to_flush)} series ({len(discovered_list)} total so far)"
                ),
                series_found_in_batch=len(batch_to_flush),
                total_series_found=len(discovered_list),
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
                f"Discovered {len(discovered_list)} series · "
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
        extensions=custom_exts,
        source_layout=SourceLayoutSpec.from_dict(dict(job.source_layout_snapshot or {})),
    )
    discovered_list: list[DiscoveredSeries] = []

    file_paths_mode = list(job.selected_file_paths or [])

    if file_paths_mode:
        discovered_list = await scanner.scan_files(
            file_paths_mode,
            root_path=job.source_path,
        )
        job.scan_total_files = sum(series.file_count for series in discovered_list)
        job.scan_total_dirs = len({series.source_folder for series in discovered_list})
        job.series_found = len(discovered_list)
        batch.extend(discovered_list)
        await flush_scan_batch(force=True)
        await forward_live_scan_progress(
            phase="scanning",
            message=f"Prepared {len(discovered_list)} series from the selected files.",
            progress=SCAN_PROGRESS_MATERIALIZE_END,
        )
        return discovered_list

    async for series in scanner.scan(job.source_path):
        discovered_list.append(series)
        batch.append(series)
        job.series_found = len(discovered_list)
        await flush_scan_batch()
        await forward_live_scan_progress(
            phase="scanning",
            message=(
                f"Discovered {len(discovered_list)} series · "
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

    return discovered_list
