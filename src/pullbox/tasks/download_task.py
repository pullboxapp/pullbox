"""Download monitoring background tasks — polls clients and processes completions.

Two scheduled tasks:
- ``monitor_downloads`` (interval, 3s) — single source of truth for both
  the in-memory progress cache (UI) and DB state transitions.  Also handles
  retry scheduling on failure and orphan recovery (throttled to ~30s).
- ``process_completed`` (interval, 5m backstop) — post-processes downloads that have
  completed but not yet been imported (file move, library registration). The
  normal handoff is triggered immediately when ``monitor_downloads`` sees a
  completion; the scheduled sweep exists as recovery.
"""

from __future__ import annotations

import time as _time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from pullbox.composition.events import build_domain_event_bus
from pullbox.composition.providers import register_download_clients
from pullbox.core.sqlite_lock import run_sqlite_transaction_with_retry
from pullbox.database import get_session_factory
from pullbox.models.download import DownloadClientType
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.series import Series
from pullbox.providers.base import ProviderRegistry
from pullbox.services.download_operation_progress import project_download_operation_progress
from pullbox.services.download_service import DownloadService
from pullbox.services.post_processing_operation_progress import (
    queue_post_processing_phase,
    queue_post_processing_snapshot,
)
from pullbox.tasks import download_monitor_apply as _download_monitor_apply
from pullbox.tasks import download_monitor_poll as _download_monitor_poll
from pullbox.tasks import download_monitor_read as _download_monitor_read
from pullbox.tasks import download_monitor_updates as _download_monitor_updates
from pullbox.tasks import download_post_processing_queue as _download_queue
from pullbox.tasks import download_post_processing_sources as _download_sources
from pullbox.tasks import download_progress as _download_progress
from pullbox.tasks import download_recovery as _download_recovery
from pullbox.tasks import download_stall_recovery as _download_stall_recovery
from pullbox.tasks.download_failure import (
    RETRY_BACKOFF_SECONDS as RETRY_BACKOFF_SECONDS,
)
from pullbox.tasks.download_failure import (
    auto_blocklist_on_download_failure,
    get_backoff_delay,
    handle_download_failure,
)
from pullbox.tasks.download_lifecycle import (
    classify_post_processing_error,
    compute_download_lifecycle_duration,
    download_lifecycle_summary_payload,
)
from pullbox.tasks.download_post_processing_cleanup import (
    cleanup_source_dir,
    should_cleanup_source_dir,
)
from pullbox.tasks.download_post_processing_destination import (
    build_destination_plan,
    find_existing_destination_file,
    register_existing_destination_file,
)
from pullbox.tasks.download_post_processing_runtime import PostProcessingRuntime
from pullbox.tasks.download_post_processing_source_validation import (
    ResolveLocalPath,
    resolve_and_validate_source,
)
from pullbox.tasks.download_post_processing_transfer import transfer_and_register_library_file
from pullbox.tasks.post_processing_progress import (
    _POST_PROCESSING_COMPLETION_GRACE_SECONDS as _POST_PROCESSING_COMPLETION_GRACE_SECONDS,
)
from pullbox.tasks.post_processing_progress import (
    _POST_PROCESSING_PHASE_TIMING_KEYS as _POST_PROCESSING_PHASE_TIMING_KEYS,
)
from pullbox.tasks.post_processing_progress import (
    PostProcessingPhase,
    PostProcessingRunTrace,
    _infer_effective_post_processing_transfer_method,
    _set_post_processing_phase,
    _set_post_processing_transfer_progress,
)
from pullbox.tasks.post_processing_progress import (
    PostProcessingSnapshot as PostProcessingSnapshot,
)
from pullbox.tasks.post_processing_progress import (
    _clear_post_processing as _clear_post_processing,
)
from pullbox.tasks.post_processing_progress import (
    _mark_post_processing_complete as _mark_post_processing_complete,
)
from pullbox.tasks.post_processing_progress import (
    _post_processing_cache as _post_processing_cache,
)
from pullbox.tasks.post_processing_progress import (
    get_all_post_processing_progress as get_all_post_processing_progress,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.download import DownloadHistory

logger = structlog.get_logger(__name__)

__all__ = [
    "PostProcessingPhase",
    "get_all_post_processing_progress",
]

# Compatibility aliases while progress state lives in the adjacent module.
ProgressSnapshot = _download_progress.ProgressSnapshot
_MILESTONES = _download_progress._MILESTONES
_STALL_CLIENT_STATES = _download_progress._STALL_CLIENT_STATES
_clear_progress = _download_progress._clear_progress
_first_active_observed_at = _download_progress._first_active_observed_at
_milestone_logged = _download_progress._milestone_logged
_progress_cache = _download_progress._progress_cache
_stall_first_seen = _download_progress._stall_first_seen
get_all_progress = _download_progress.get_all_progress
_build_status_check_error_update = _download_monitor_updates.build_status_check_error_update
_build_status_update = _download_monitor_updates.build_status_update
_apply_monitor_updates = _download_monitor_apply.apply_monitor_updates
_load_monitor_poll_items = _download_monitor_read.load_monitor_poll_items
_poll_download_clients = _download_monitor_poll.poll_download_clients
_record_download_progress = _download_progress.record_download_progress
_STALE_DOWNLOAD_TIMEOUT = _download_recovery._STALE_DOWNLOAD_TIMEOUT
_process_direct_retry_pending = _download_recovery._process_direct_retry_pending
_process_retry_pending = _download_recovery._process_retry_pending
_recover_orphaned_downloads = _download_recovery._recover_orphaned_downloads
_POST_PROCESSING_SOURCE_RETRY_DELAYS = _download_sources._POST_PROCESSING_SOURCE_RETRY_DELAYS
PostProcessingSourceProbe = _download_sources.PostProcessingSourceProbe
_build_post_processing_integrity_exception = (
    _download_sources._build_post_processing_integrity_exception
)
_find_comic_file = _download_sources._find_comic_file
_resolve_local_download_root = _download_sources._resolve_local_download_root
_resolve_local_path = _download_sources._resolve_local_path
_process_completed_lock = _download_queue._process_completed_lock
_STALLED_DOWNLOAD_TIMEOUT = _download_stall_recovery._STALLED_DOWNLOAD_TIMEOUT
_get_stall_timeout = _download_stall_recovery._get_stall_timeout
_recover_stalled_downloads = _download_stall_recovery._recover_stalled_downloads


def _build_download_service(registry: ProviderRegistry) -> DownloadService:
    """Construct a task-local DownloadService while preserving patch seams."""
    return DownloadService(registry, build_domain_event_bus())


# Throttle expensive recovery checks (orphan recovery, retry processing)
# so they don't run on every 3s tick of monitor_downloads.
_last_recovery_check: float = 0.0
_RECOVERY_CHECK_INTERVAL = 30.0  # seconds


async def _probe_post_processing_source(
    source_path: Path,
    allowed_extensions: set[str],
) -> PostProcessingSourceProbe:
    """Retry source discovery while preserving the task module's patch seam."""
    return await _download_sources._probe_post_processing_source(
        source_path,
        allowed_extensions,
        find_comic_file=_find_comic_file,
    )


def _trigger_process_completed_now() -> None:
    """Queue an immediate post-processing run after a completion handoff."""
    from pullbox.core.scheduler import get_scheduler

    try:
        status = get_scheduler().run_task_now("process_completed")
        if status == "queued":
            logger.debug("process_completed_triggered_after_completion")
    except Exception:
        logger.warning("process_completed_trigger_failed", exc_info=True)


async def _build_download_registry(
    session: AsyncSession,
) -> ProviderRegistry | None:
    """Build a ProviderRegistry with download clients from enabled DB configs.

    Returns ``None`` if no download clients are configured.
    """
    registry = ProviderRegistry()
    await register_download_clients(session, registry)

    if not registry.get_download_clients():
        return None

    return registry


def _get_backoff_delay(retry_count: int) -> timedelta:
    """Return the backoff delay for the given retry attempt (0-indexed)."""
    return get_backoff_delay(retry_count)


def _compute_download_lifecycle_duration(
    download: DownloadHistory,
    *,
    observed_at: datetime | None,
) -> tuple[float | None, str | None]:
    """Return the best available lifecycle duration and its basis."""
    return compute_download_lifecycle_duration(download, observed_at=observed_at)


def _emit_download_lifecycle_summary(
    download: DownloadHistory,
    *,
    outcome: str,
    client_state: str | None,
    downloaded_path: str | None,
    observed_at: datetime | None,
) -> None:
    """Emit one info-level summary when a download leaves active monitoring."""
    payload = download_lifecycle_summary_payload(
        download,
        outcome=outcome,
        client_state=client_state,
        downloaded_path=downloaded_path,
        observed_at=observed_at,
    )
    logger.info(
        "download_lifecycle_summary",
        **payload,
    )


def _classify_post_processing_error(exc: Exception) -> str:
    """Classify post-processing failures for troubleshooting summaries."""
    return classify_post_processing_error(exc)


async def _handle_download_failure(
    session: AsyncSession,
    download: DownloadHistory,
    error_message: str | None,
) -> None:
    """Compatibility wrapper for download failure retry/blocklist handling."""
    await handle_download_failure(
        session,
        download,
        error_message,
        auto_blocklist_on_failure=_auto_blocklist_on_failure,
    )


async def _auto_blocklist_on_failure(
    session: AsyncSession,
    download: DownloadHistory,
    error_message: str | None,
) -> None:
    """Add a permanently failed download to the blocklist if config allows."""
    await auto_blocklist_on_download_failure(session, download, error_message)


async def monitor_downloads() -> None:
    """Poll download clients for status updates on active downloads.

    Single source of truth for both progress cache (UI) and DB state
    transitions.  Runs every 3 seconds to match UI polling cadence.

    Structured in three phases to avoid holding a DB session open
    during slow HTTP calls to download clients:

    1. Read phase — short session to load active downloads and build registry
    2. Poll phase — no session; HTTP calls to download clients
    3. Write phase — short session to apply state updates and commit

    Expensive recovery checks (orphan recovery, retry processing) are
    throttled to run only every ~30 seconds.
    """
    global _last_recovery_check
    import time

    start = time.monotonic()
    factory = get_session_factory()
    recovery_checked_at = time.monotonic()
    recovery_due = recovery_checked_at - _last_recovery_check >= _RECOVERY_CHECK_INTERVAL
    direct_retried = 0
    if recovery_due:
        direct_retried = await _process_direct_retry_pending()

    # ── Phase 1: Read — load active downloads and build registry ──
    async with factory() as session:
        try:
            read_result = await _load_monitor_poll_items(
                session,
                build_download_registry=_build_download_registry,
            )
            if read_result is None:
                if recovery_due:
                    _last_recovery_check = recovery_checked_at
                if direct_retried:
                    logger.info(
                        "monitor_downloads_complete",
                        checked=0,
                        completed=0,
                        failed=0,
                        retried=direct_retried,
                        recovered=0,
                        duration_ms=round((time.monotonic() - start) * 1000, 1),
                    )
                return
            registry = read_result.registry
            poll_items = read_result.poll_items
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    download_svc = _build_download_service(registry)

    # ── Phase 2: Poll — HTTP calls to download clients (no DB session) ──
    checked = len(poll_items)
    completed = 0
    failed = 0
    retried = direct_retried
    recovered = 0
    updates = []

    if poll_items:
        updates = await _poll_download_clients(
            poll_items,
            download_svc,
            record_download_progress=_record_download_progress,
            build_status_update=_build_status_update,
            build_status_check_error_update=_build_status_check_error_update,
            event_logger=logger,
        )

    # ── Phase 3: Write — short, independently retryable DB transactions ──
    if updates:
        async with factory() as session:

            async def apply_updates() -> _download_monitor_apply.MonitorApplyResult:
                return await _apply_monitor_updates(
                    session,
                    updates,
                    first_active_observed_at=_first_active_observed_at,
                    clear_progress=_clear_progress,
                    handle_download_failure=_handle_download_failure,
                    emit_download_lifecycle_summary=_emit_download_lifecycle_summary,
                    event_logger=logger,
                    publish_progress=project_download_operation_progress,
                )

            apply_result = await run_sqlite_transaction_with_retry(
                session,
                apply_updates,
                event_name="download_monitor_write",
                logger=logger,
            )
            completed = apply_result.completed
            failed = apply_result.failed

    # Throttle expensive recovery checks to ~every 30s. Network retries run
    # outside this transaction; only the local orphan-recovery write is retried.
    if recovery_due:
        retried += await _process_retry_pending(factory, download_svc)
        async with factory() as session:

            async def recover_orphans() -> int:
                return await _recover_orphaned_downloads(session)

            recovered = await run_sqlite_transaction_with_retry(
                session,
                recover_orphans,
                event_name="download_orphan_recovery",
                logger=logger,
            )
        _last_recovery_check = recovery_checked_at

    duration_ms = (time.monotonic() - start) * 1000
    log_kwargs = {
        "checked": checked,
        "completed": completed,
        "failed": failed,
        "retried": retried,
        "recovered": recovered,
        "duration_ms": round(duration_ms, 1),
    }
    if completed or failed or retried or recovered:
        logger.info("monitor_downloads_complete", **log_kwargs)
    elif checked > 0:
        logger.debug("monitor_downloads_complete", **log_kwargs)

    if completed:
        _trigger_process_completed_now()


async def process_completed() -> None:
    """Post-process downloads that have completed but not yet been imported.

    Handles the full lifecycle: move files to library, update issue status,
    and create library file records.

    IMPORTANT: Each download is processed in its own short-lived DB session
    so that the slow file-I/O doesn't hold a SQLite write-lock that blocks
    other tasks (especially monitor_downloads).
    """
    if _process_completed_lock.locked():
        await _download_queue.process_completed(_run_post_processing, event_logger=logger)
        return

    await _download_queue.process_completed(
        _run_post_processing,
        session_factory=get_session_factory(),
        event_logger=logger,
    )


async def _run_post_processing(
    session: AsyncSession,
    download: DownloadHistory,
    *,
    resolve_local_path: ResolveLocalPath | None = None,
    cleanup_source: bool = True,
    allow_resource_safety_exception: bool = False,
) -> None:
    """Transfer the downloaded file to the library and update all records.

    Steps:
    1. Resolve the download path (apply remote path mapping if configured)
    2. Locate the comic file in the resolved path
    3. Load issue + series for naming
    4. Build destination path using naming format
    5. Transfer the file using the configured method
    6. Update download.final_path, issue.status, and create a LibraryFile
    """
    from asyncio import get_running_loop

    operation_loop = get_running_loop()
    log = logger.bind(download_id=download.id, issue_id=download.issue_id)
    trace = PostProcessingRunTrace(download_id=download.id)
    trace.transfer_method = None

    def _publish_post_processing_phase(
        current_download: DownloadHistory,
        phase: PostProcessingPhase,
    ) -> None:
        operation_loop.call_soon_threadsafe(
            queue_post_processing_phase,
            current_download,
            phase,
        )

    runtime = PostProcessingRuntime(
        download=download,
        trace=trace,
        log=log,
        summary_logger=logger,
        set_phase=_set_post_processing_phase,
        publish_phase=_publish_post_processing_phase,
    )

    log.debug(
        "post_processing_start",
        downloaded_path=(
            None
            if download.download_client is DownloadClientType.AIRDCPP
            else download.downloaded_path
        ),
        client_type=str(download.download_client.value),
    )
    runtime.enter_phase(PostProcessingPhase.RESOLVING_SOURCE)

    try:
        from pullbox.core.library_policy import load_library_ingest_policy

        source_validation = await resolve_and_validate_source(
            session=session,
            download=download,
            trace=trace,
            runtime=runtime,
            log=log,
            resolve_local_path=resolve_local_path or _resolve_local_path,
            probe_source=_probe_post_processing_source,
            build_integrity_exception=_build_post_processing_integrity_exception,
            allow_resource_safety_exception=allow_resource_safety_exception,
        )
        probe_root = source_validation.probe_root
        comic_file = source_validation.comic_file

        # 3. Load issue + series for naming
        runtime.enter_phase(PostProcessingPhase.PREPARING_DESTINATION)
        from sqlalchemy.orm import selectinload

        issue_result = await session.execute(
            select(Issue)
            .options(selectinload(Issue.library_file))
            .where(Issue.id == download.issue_id)
        )
        issue = issue_result.scalar_one_or_none()
        if issue is None:
            log.error("post_processing_issue_not_found")
            return

        # Check skip_existing_files — if enabled, skip issues that already have a file
        from pullbox.core.file_ops import register_library_file, resolve_library_destination

        ingest_policy = await load_library_ingest_policy(session)
        trace.configured_transfer_method = ingest_policy.post_processing_method
        trace.effective_transfer_method = ingest_policy.post_processing_method
        trace.transfer_method = ingest_policy.post_processing_method
        trace.torrent_import_strategy = getattr(
            ingest_policy, "torrent_import_strategy", "standard"
        )
        trace.seed_safe_torrent_import = (
            download.download_client.is_torrent and trace.torrent_import_strategy == "seed_safe"
        )
        replacing_existing_file = bool(getattr(download, "replace_existing_file", False))
        if (
            ingest_policy.skip_existing_files
            and issue.library_file is not None
            and not replacing_existing_file
        ):
            log.info(
                "post_processing_skipped_existing",
                issue_id=issue.id,
                library_file_id=issue.library_file.id,
                reason="skip_existing_files enabled and issue already has a library file",
            )
            issue.status = IssueStatus.OWNED
            trace.final_path = issue.library_file.file_path
            trace.file_size_bytes = issue.library_file.file_size
            trace.transferred_bytes = issue.library_file.file_size
            download.imported_at = datetime.now(UTC)
            runtime.emit_summary(outcome="skipped_existing")
            return

        series_result = await session.execute(
            select(Series)
            .options(selectinload(Series.publisher))
            .where(
                Series.id == issue.series_id,
            )
        )
        series = series_result.scalar_one_or_none()
        if series is None:
            log.error("post_processing_series_not_found", series_id=issue.series_id)
            return

        # 4. Build destination path from the shared ingest engine.
        destination_plan = await build_destination_plan(
            session=session,
            comic_file=comic_file,
            series=series,
            issue=issue,
            log=log,
            resolve_library_destination=resolve_library_destination,
        )
        dest_path = destination_plan.dest_path
        dest_dir = destination_plan.dest_dir

        existing_destination = None
        if not replacing_existing_file:
            existing_destination = await find_existing_destination_file(
                comic_file=comic_file,
                dest_path=dest_path,
                dest_dir=dest_dir,
                log=log,
            )
        if existing_destination is not None:
            await register_existing_destination_file(
                session=session,
                existing_destination=existing_destination,
                issue=issue,
                series=series,
                download=download,
                trace=trace,
                runtime=runtime,
                log=log,
                register_library_file=register_library_file,
            )
            return

        # If source is gone and destination doesn't exist either, we can't proceed.
        if comic_file is None:
            log.error(
                "post_processing_path_not_found",
                source=str(probe_root),
                dest=str(dest_path),
                hint="Source path did not become visible and file is not already at the "
                "destination. Check path mapping and shared-storage visibility.",
            )
            raise FileNotFoundError(
                f"Post-processing source did not become visible after {source_validation.attempts} "
                f"probe(s) and no file exists at the destination: {probe_root}"
            )

        # 5. Transfer the file using the shared ingest engine
        method = ingest_policy.post_processing_method
        from pullbox.services.issue_file_service import resolve_configured_utility_trash_dir

        def _track_transfer_progress(
            download_id: int,
            *,
            total_bytes: int,
            done_bytes: int,
        ) -> None:
            _set_post_processing_transfer_progress(
                download_id,
                total_bytes=total_bytes,
                done_bytes=done_bytes,
            )
            snapshot = get_all_post_processing_progress().get(download_id)
            if snapshot is not None:
                operation_loop.call_soon_threadsafe(
                    queue_post_processing_snapshot,
                    download,
                    snapshot,
                )

        dest_path = await transfer_and_register_library_file(
            session=session,
            comic_file=comic_file,
            issue=issue,
            series=series,
            download=download,
            ingest_policy=ingest_policy,
            trace=trace,
            runtime=runtime,
            log=log,
            register_library_file=register_library_file,
            set_transfer_progress=_track_transfer_progress,
            infer_effective_transfer_method=_infer_effective_post_processing_transfer_method,
            replacement_trash_dir=await resolve_configured_utility_trash_dir(session)
            if replacing_existing_file
            else None,
        )

        # 5b. Clean up empty source directory for usenet downloads (SABnzbd/NZBGet).
        # Torrent clients manage their own files (seeding), so we never touch those.
        # Only clean up when using "move" — copy/hardlink/symlink should preserve.
        if cleanup_source and should_cleanup_source_dir(method, download.download_client):
            cleanup_start = _time.monotonic()
            cleanup_root = await _resolve_local_download_root(session, download)
            cleanup_dir = probe_root if probe_root != comic_file else comic_file.parent
            cleanup_result = await get_running_loop().run_in_executor(
                None,
                cleanup_source_dir,
                cleanup_dir,
                cleanup_root,
            )
            trace.cleanup_ms = round((_time.monotonic() - cleanup_start) * 1000, 1)
            cleanup_context = {
                "source_dir": str(cleanup_dir),
                "download_root": str(cleanup_root) if cleanup_root else None,
                "client": str(download.download_client.value),
                "reason": cleanup_result.reason,
            }
            if cleanup_result.removed:
                log.debug("post_processing_source_cleaned", **cleanup_context)
            elif cleanup_result.reason == "error":
                log.warning(
                    "post_processing_source_cleanup_failed",
                    **cleanup_context,
                    error=cleanup_result.error,
                )
            elif cleanup_result.reason in {"root_missing", "unsafe_path"}:
                log.warning(
                    "post_processing_source_cleanup_skipped",
                    **cleanup_context,
                    hint=(
                        "Configure Download Directory to the local completed-download root "
                        "and ensure the source job is beneath it."
                    ),
                )
            else:
                log.debug("post_processing_source_cleanup_skipped", **cleanup_context)

        # 6. Update records
        download.final_path = str(dest_path)
        log.info(
            "post_processing_complete",
            final_path=str(dest_path),
        )
        runtime.emit_summary(outcome="success")
    except Exception as exc:
        trace.finalize_current_phase()
        trace.error_classification = _classify_post_processing_error(exc)
        runtime.emit_summary(outcome="failed", error_message=str(exc))
        raise
