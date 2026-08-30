"""Import job execution orchestration helpers."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select as sa_select

from pullbox.core.exceptions import JobCancelledError, JobPausedError, NotFoundError
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.models.story_arc import ImportedStoryArcStatus
from pullbox.models.story_arc_import import ImportedStoryArc
from pullbox.schemas.import_job import ImportProgressEvent
from pullbox.services.import_active_file_progress import (
    ActiveFileProgressSettings as _ActiveFileProgressSettings,
)
from pullbox.services.import_active_file_progress import (
    active_file_progress_pct as _active_file_progress_pct,  # noqa: F401 - legacy test import
)
from pullbox.services.import_active_file_progress import (
    calculate_import_file_progress_pct as calculate_import_file_progress_pct,
)
from pullbox.services.import_catalog_hydration import (
    catalog_hydration_tasks,
)
from pullbox.services.import_job_execution_items import (
    execute_duplicate_series_merge as _execute_duplicate_series_merge,
)
from pullbox.services.import_job_execution_items import (
    execute_new_series as _execute_new_series_impl,
)
from pullbox.services.import_job_execution_items import (
    has_safety_blocked_files as _has_safety_blocked_files,  # noqa: F401 - legacy test import
)
from pullbox.services.import_job_execution_items import (
    schedule_catalog_hydration_for_series as _schedule_catalog_hydration,
)
from pullbox.services.import_job_execution_items import (
    targeted_issue_summaries_for_import_files as _targeted_issue_summaries_for_import_files,  # noqa: F401 - legacy test import
)
from pullbox.services.import_job_execution_progress import (
    await_prefetch_with_metadata_progress as _await_prefetch_with_metadata_progress,
)
from pullbox.services.import_job_execution_progress import (
    build_import_group_progress_plans as _build_import_group_progress_plans,
)
from pullbox.services.import_job_execution_progress import (
    build_report_file_progress_callback as _build_report_file_progress_callback,
)
from pullbox.services.import_job_execution_progress import (
    build_series_metadata_progress_emitter as _build_series_metadata_progress_emitter,
)
from pullbox.services.import_job_execution_progress import (
    emit_active_file_progress as _emit_active_file_progress,
)
from pullbox.services.import_job_execution_progress import (
    progress_session_factory_for_runtime as _progress_session_factory_for_runtime,
)
from pullbox.services.import_job_execution_types import (
    CatalogHydrationRequest as _CatalogHydrationRequest,
)
from pullbox.services.import_job_execution_types import (
    EmitProgressFunc,
    EstimateRemainingFunc,
    LogEventFunc,
    ProcessSeriesFilesFunc,
    RaiseIfCancelledFunc,
    RecordActionFunc,
    ReportFileProgressFunc,
    SeriesServiceFunc,
    SlowItemDelayFunc,
)
from pullbox.services.import_job_execution_types import (
    ExecutionItemPlan as _ExecutionItemPlan,
)
from pullbox.services.import_progress_runtime import (
    ImportProgressSettings,
    current_item_payload,
    import_group_progress_plan,
    weighted_import_progress_pct,
)
from pullbox.services.import_root_policy_activation import (
    RootPolicyActivationConflictError,
    activate_future_root_policy,
)
from pullbox.services.import_story_arc_materialization import (
    StoryArcMaterializationResult,
    materialize_confirmed_story_arcs,
)
from pullbox.services.import_story_arc_resolution import (
    StoryArcResolutionResult,
    resolve_staged_story_arc_entries,
)
from pullbox.services.import_workflow_state import (
    emit_live_progress,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)
_catalog_hydration_tasks = catalog_hydration_tasks


_SERIES_PREFETCH_WINDOW = 2
_ACTIVE_FILE_PROGRESS_EMIT_INTERVAL_SECONDS = 0.2
_METADATA_PROGRESS_HEARTBEAT_SECONDS = 5.0
_LIVE_ONLY_ACTIVE_FILE_STAGES = frozenset({"transferring", "rewriting"})


def _build_execution_item_plans(
    confirmed_items: list[ImportedSeries],
    duplicate_items: list[ImportedSeries],
) -> list[_ExecutionItemPlan]:
    """Build the ordered Step 4 review-group execution plan."""
    return [
        *[
            _ExecutionItemPlan(
                mode="new",
                item_id=item.id,
                raw_series_name=item.raw_series_name,
                cv_id=item.user_selected_cv_id or item.cv_id,
                existing_series_id=item.series_id,
            )
            for item in confirmed_items
        ],
        *[
            _ExecutionItemPlan(
                mode="duplicate",
                item_id=item.id,
                raw_series_name=item.raw_series_name,
                cv_id=item.user_selected_cv_id or item.cv_id,
                existing_series_id=item.series_id,
            )
            for item in duplicate_items
        ],
    ]


async def _emit_import_preparation_progress(
    session: Any,
    job: ImportJob,
    *,
    job_id: int,
    progress_callback: Callable[[ImportProgressEvent], Awaitable[None]],
    emit_progress: EmitProgressFunc,
    runtime_revision_state: dict[str, int],
) -> None:
    """Emit the initial Step 4 preparation progress event."""
    runtime_revision_state["value"] += 1
    job.progress_revision = runtime_revision_state["value"]
    await emit_progress(
        session,
        job,
        ImportProgressEvent(
            job_id=job_id,
            status=ImportJobStatus.IMPORTING,
            mode="import",
            phase="queued",
            progress=0,
            message="Preparing the selected series for import...",
            estimated_seconds_remaining=None,
            progress_revision=runtime_revision_state["value"],
            **{
                "series_found": job.series_found,
                "series_imported": job.series_imported,
                "series_failed": job.series_failed,
                "total_files_imported": job.total_files_imported,
                "total_files_failed": job.total_files_failed,
            },
        ),
        progress_callback,
    )


def _prime_series_prefetch_window(
    *,
    series_service: SeriesServiceFunc,
    execution_items: list[_ExecutionItemPlan],
    prefetch_tasks: dict[int, asyncio.Task[tuple[Any, list[Any]]]],
    start_index: int,
    window_size: int,
) -> int:
    """Prime the next window of full-series ComicVine prefetch tasks."""
    if getattr(type(series_service), "add_from_import_review_targeted", None) is not None:
        return 0
    prefetch_descriptor = getattr(type(series_service), "prefetch_comicvine_bundle", None)
    if prefetch_descriptor is None:
        return 0
    prefetch = prefetch_descriptor.__get__(series_service, type(series_service))

    primed = 0
    for candidate in execution_items[start_index:]:
        if candidate.mode != "new" or candidate.item_id in prefetch_tasks:
            continue
        cv_id = candidate.cv_id
        if cv_id is None:
            continue
        prefetch_tasks[candidate.item_id] = asyncio.create_task(prefetch(cv_id))
        primed += 1
        if primed >= window_size:
            break
    return primed


async def execute_import_job(
    session: AsyncSession,
    job_id: int,
    *,
    series_service: SeriesServiceFunc,
    process_series_files: ProcessSeriesFilesFunc,
    raise_if_cancelled: RaiseIfCancelledFunc,
    record_action: RecordActionFunc,
    log_event: LogEventFunc,
    emit_progress: EmitProgressFunc,
    estimate_remaining_seconds: EstimateRemainingFunc,
    maybe_slow_item_delay: SlowItemDelayFunc,
    progress_callback: Callable[[ImportProgressEvent], Awaitable[None]] | None = None,
) -> None:
    """Execute confirmed new-series imports plus duplicate-series file merges."""
    loaded_job = await session.get(ImportJob, job_id)
    if loaded_job is None:
        raise NotFoundError("ImportJob", job_id)
    job = loaded_job

    if job.import_started_at is None:
        job.import_started_at = datetime.now(UTC)
    job_started_at = job.import_started_at
    job.status = ImportJobStatus.IMPORTING
    job_series_found = int(job.series_found or 0)

    confirmed_items = await _load_confirmed_import_series(session, job_id)
    duplicate_items = await _load_duplicate_import_series(
        session,
        job_id,
        confirmed_ids={item.id for item in confirmed_items},
    )
    execution_items = _build_execution_item_plans(confirmed_items, duplicate_items)
    await log_event(
        session,
        job_id,
        "INFO",
        "import_execution_started",
        message=f"Import execution started for {len(execution_items)} review groups.",
        total_review_groups=len(execution_items),
        new_series_groups=len(confirmed_items),
        duplicate_series_groups=len(duplicate_items),
    )
    imported_count = int(job.series_imported or 0)
    failed_count = int(job.series_failed or 0)
    total_files_imported = int(job.total_files_imported or 0)
    total_files_failed = int(job.total_files_failed or 0)
    prefetch_tasks: dict[int, asyncio.Task[tuple[Any, list[Any]]]] = {}
    progress_session_factory = _progress_session_factory_for_runtime(session)
    active_file_progress_settings = _ActiveFileProgressSettings(
        move_to_library=bool(job.move_to_library),
        convert_to_preferred_format=bool(job.convert_to_preferred_format),
        update_embedded_comicinfo_from_match=bool(job.update_embedded_comicinfo_from_match),
    )
    shared_progress_settings = ImportProgressSettings(
        move_to_library=active_file_progress_settings.move_to_library,
        convert_to_preferred_format=active_file_progress_settings.convert_to_preferred_format,
        update_embedded_comicinfo_from_match=(
            active_file_progress_settings.update_embedded_comicinfo_from_match
        ),
    )
    group_progress_plans = await _build_import_group_progress_plans(
        session,
        execution_items,
        shared_progress_settings,
    )
    group_progress_weights = [
        group_progress_plans.get(
            item.item_id,
            import_group_progress_plan(shared_progress_settings, []),
        ).total_weight
        for item in execution_items
    ]
    runtime_revision_state: dict[str, int] = {"value": int(job.progress_revision or 0)}
    pending_catalog_hydrations: list[_CatalogHydrationRequest] = []

    def queue_catalog_hydration(series_id: int, search_on_add: bool) -> None:
        pending_catalog_hydrations.append(
            _CatalogHydrationRequest(
                series_id=series_id,
                search_on_add=search_on_add,
            )
        )

    await session.commit()

    if progress_callback:
        await _emit_import_preparation_progress(
            session,
            job,
            job_id=job_id,
            progress_callback=progress_callback,
            emit_progress=emit_progress,
            runtime_revision_state=runtime_revision_state,
        )

    _prime_series_prefetch_window(
        series_service=series_service,
        execution_items=execution_items,
        prefetch_tasks=prefetch_tasks,
        start_index=0,
        window_size=_SERIES_PREFETCH_WINDOW,
    )

    emit_series_metadata_progress = _build_series_metadata_progress_emitter(
        session=session,
        job_id=job_id,
        job=job,
        job_started_at=job_started_at,
        progress_callback=progress_callback,
        emit_progress=emit_progress,
        emit_live_progress=emit_live_progress,
        estimate_remaining_seconds=estimate_remaining_seconds,
        group_progress_plans=group_progress_plans,
        shared_progress_settings=shared_progress_settings,
        group_progress_weights=group_progress_weights,
        stats=lambda: {
            "series_imported": imported_count,
            "series_failed": failed_count,
            "total_files_imported": total_files_imported,
            "total_files_failed": total_files_failed,
        },
        series_found=job_series_found,
        revision_state=runtime_revision_state,
    )

    try:
        for idx, item_plan in enumerate(execution_items):
            await raise_if_cancelled(session, job_id)
            execution_mode = item_plan.mode
            item_id = item_plan.item_id
            item = await session.get(ImportedSeries, item_id)
            if item is None:
                continue
            item_raw_series_name = item_plan.raw_series_name
            item_cv_id = item_plan.cv_id
            item_existing_series_id = item.series_id or item_plan.existing_series_id
            file_progress_state: dict[str, object] = {
                "file_id": None,
                "stage": None,
                "pct": None,
                "emitted_at": 0.0,
            }
            current_group_index = idx
            total_groups = len(execution_items)
            current_stats = {
                "series_imported": imported_count,
                "series_failed": failed_count,
                "total_files_imported": total_files_imported,
                "total_files_failed": total_files_failed,
            }
            report_file_progress = _build_report_file_progress_callback(
                session=session,
                job_id=job_id,
                job=job,
                job_started_at=job_started_at,
                progress_callback=progress_callback,
                progress_session_factory=progress_session_factory,
                estimate_remaining_seconds=estimate_remaining_seconds,
                group_progress_plans=group_progress_plans,
                shared_progress_settings=shared_progress_settings,
                group_progress_weights=group_progress_weights,
                group_index=current_group_index,
                total_groups=total_groups,
                series_id=item_id,
                series_name=item_raw_series_name,
                series_found=job_series_found,
                stats=current_stats,
                progress_state=file_progress_state,
                revision_state=runtime_revision_state,
                active_file_progress_settings=active_file_progress_settings,
                monotonic_time=lambda: asyncio.get_running_loop().time(),
                active_file_progress_emit_interval_seconds=(
                    _ACTIVE_FILE_PROGRESS_EMIT_INTERVAL_SECONDS
                ),
                live_only_active_file_stages=_LIVE_ONLY_ACTIVE_FILE_STAGES,
                emit_active_file_progress=_emit_active_file_progress,
                emit_live_progress=emit_live_progress,
            )

            try:
                if execution_mode == "new":
                    prefetched_bundle = None
                    task = prefetch_tasks.pop(item.id, None)
                    supports_targeted_import = (
                        getattr(type(series_service), "add_from_import_review_targeted", None)
                        is not None
                    )
                    if task is not None:
                        prefetched_bundle = await _await_prefetch_with_metadata_progress(
                            task,
                            group_index=current_group_index,
                            total_groups=total_groups,
                            series_id=item_id,
                            series_name=item_raw_series_name,
                            session=session,
                            job_id=job_id,
                            raise_if_cancelled=raise_if_cancelled,
                            emit_series_metadata_progress=emit_series_metadata_progress,
                            heartbeat_seconds=_METADATA_PROGRESS_HEARTBEAT_SECONDS,
                            monotonic_time=lambda: asyncio.get_running_loop().time(),
                        )
                    elif supports_targeted_import:
                        await emit_series_metadata_progress(
                            group_index=current_group_index,
                            total_groups=total_groups,
                            series_id=item_id,
                            series_name=item_raw_series_name,
                            message=(
                                f"Preparing cached ComicVine match for {item_raw_series_name}..."
                            ),
                            current_item_stage="cached_match",
                            current_item_progress_pct=80,
                        )
                    else:
                        await emit_series_metadata_progress(
                            group_index=current_group_index,
                            total_groups=total_groups,
                            series_id=item_id,
                            series_name=item_raw_series_name,
                            message=(
                                f"Fetching ComicVine metadata for {item_raw_series_name} "
                                f"(review group {current_group_index + 1}/"
                                f"{max(total_groups, 1)})..."
                            ),
                            current_item_stage="metadata_fetch",
                            current_item_progress_pct=8,
                        )
                    (
                        files_ok,
                        files_err,
                        imported_count,
                        failed_count,
                        should_emit_progress,
                    ) = await _execute_new_series(
                        session,
                        job,
                        item,
                        imported_count=imported_count,
                        failed_count=failed_count,
                        prefetched_bundle=prefetched_bundle,
                        series_service=series_service,
                        process_series_files=process_series_files,
                        record_action=record_action,
                        log_event=log_event,
                        report_file_progress=report_file_progress,
                        queue_catalog_hydration=queue_catalog_hydration,
                    )
                    if not should_emit_progress:
                        continue
                else:
                    (
                        files_ok,
                        files_err,
                        should_emit_progress,
                    ) = await _execute_duplicate_series_merge(
                        session,
                        job,
                        item,
                        process_series_files=process_series_files,
                        log_event=log_event,
                        report_file_progress=report_file_progress,
                    )
                    if not should_emit_progress:
                        continue

                reloaded_job = await session.get(ImportJob, job_id)
                if reloaded_job is not None:
                    reloaded_job.progress_revision = max(
                        int(reloaded_job.progress_revision or 0),
                        int(runtime_revision_state["value"] or 0),
                    )
                    job = reloaded_job

                total_files_imported += files_ok
                total_files_failed += files_err
                policy_was_pending = job.future_root_policy_applied_at is None
                try:
                    policy_action = await activate_future_root_policy(
                        session,
                        job,
                        successful_registration_count=total_files_imported,
                    )
                except RootPolicyActivationConflictError as exc:
                    job.error_message = exc.message
                    await log_event(
                        session,
                        job_id,
                        "ERROR",
                        "library_root_policy_activation_conflict",
                        message=exc.message,
                        target_library_root_id=job.target_library_root_id,
                    )
                else:
                    if policy_action is not None and policy_was_pending:
                        await log_event(
                            session,
                            job_id,
                            "INFO",
                            "library_root_policy_applied",
                            message="Future library layout activated for the selected root.",
                            target_library_root_id=job.target_library_root_id,
                            policy_revision=policy_action.payload.get("applied_revision"),
                        )
                await session.commit()

            except JobPausedError:
                if execution_mode == "new":
                    item.status = ImportSeriesStatus.CONFIRMED
                await session.flush()
                raise
            except JobCancelledError:
                if execution_mode == "new":
                    item.status = ImportSeriesStatus.CONFIRMED
                await session.flush()
                raise
            except Exception as exc:
                await session.rollback()
                persisted_item = await session.get(ImportedSeries, item_id)
                if persisted_item is None:
                    raise
                item = persisted_item
                item_existing_series_id = item.series_id or item_existing_series_id
                if execution_mode == "new":
                    item.status = ImportSeriesStatus.FAILED
                    item.error_message = str(exc)
                    failed_count += 1
                    await log_event(
                        session,
                        job_id,
                        "ERROR",
                        "import_series_failed",
                        message=f"Failed to import {item_raw_series_name}: {exc}",
                        raw_series_name=item_raw_series_name,
                        cv_id=item_cv_id,
                    )
                else:
                    item.error_message = str(exc)
                    await log_event(
                        session,
                        job_id,
                        "ERROR",
                        "import_duplicate_series_merge_failed",
                        message=(
                            "Failed to merge duplicate-series files for "
                            f"{item_raw_series_name}: {exc}"
                        ),
                        raw_series_name=item_raw_series_name,
                        existing_series_id=item_existing_series_id,
                    )
                await session.commit()
                reloaded_job = await session.get(ImportJob, job_id)
                if reloaded_job is not None:
                    job = reloaded_job

            refreshed_item = await session.get(ImportedSeries, item_id)
            if refreshed_item is not None:
                item = refreshed_item

            if progress_callback:
                progress = weighted_import_progress_pct(
                    group_progress_weights,
                    current_group_index=idx,
                    current_group_progress_pct=100,
                )
                job.series_imported = imported_count
                job.series_failed = failed_count
                job.total_files_imported = total_files_imported
                job.total_files_failed = total_files_failed
                runtime_revision_state["value"] += 1
                job.progress_revision = runtime_revision_state["value"]
                await emit_progress(
                    session,
                    job,
                    ImportProgressEvent(
                        job_id=job_id,
                        status=ImportJobStatus.IMPORTING,
                        phase="importing",
                        progress=progress,
                        message=f"Processed {idx + 1}/{len(execution_items)} review groups",
                        current_series=item_raw_series_name,
                        current_series_status=(
                            item.status if item is not None else ImportSeriesStatus.FAILED
                        ),
                        estimated_seconds_remaining=estimate_remaining_seconds(
                            job_started_at,
                            progress,
                        ),
                        series_imported=imported_count,
                        series_failed=failed_count,
                        series_found=job_series_found,
                        total_files_imported=total_files_imported,
                        total_files_failed=total_files_failed,
                        current_file_id=None,
                        current_file_name=None,
                        current_file_stage=None,
                        current_file_progress_current=None,
                        current_file_progress_total=None,
                        current_file_progress_pct=None,
                        current_file_progress_unit=None,
                        progress_revision=runtime_revision_state["value"],
                        **current_item_payload(
                            kind="series",
                            stage="review_group_complete",
                            name=item_raw_series_name,
                            progress_pct=100,
                        ),
                    ),
                    progress_callback,
                )
            await maybe_slow_item_delay()
            _prime_series_prefetch_window(
                series_service=series_service,
                execution_items=execution_items,
                prefetch_tasks=prefetch_tasks,
                start_index=idx + 1,
                window_size=_SERIES_PREFETCH_WINDOW,
            )
    finally:
        for task in prefetch_tasks.values():
            task.cancel()
        if prefetch_tasks:
            await asyncio.gather(*prefetch_tasks.values(), return_exceptions=True)

    job.series_imported = imported_count
    job.series_failed = failed_count
    job.total_files_imported = total_files_imported
    job.total_files_failed = total_files_failed
    job = await _execute_story_arc_materialization(
        session,
        job,
        job_id=job_id,
        raise_if_cancelled=raise_if_cancelled,
        record_action=record_action,
        log_event=log_event,
        emit_progress=emit_progress,
        estimate_remaining_seconds=estimate_remaining_seconds,
        progress_callback=progress_callback,
        runtime_revision_state=runtime_revision_state,
        job_started_at=job_started_at,
    )
    # An unexpected logical-arc failure rolls back only the current arc
    # transaction. Reapply the durable canonical counters before completion.
    job.series_imported = imported_count
    job.series_failed = failed_count
    job.total_files_imported = total_files_imported
    job.total_files_failed = total_files_failed
    job.status = ImportJobStatus.COMPLETED
    job.import_completed_at = datetime.now(UTC)
    await session.flush()

    await log_event(
        session,
        job_id,
        "INFO",
        "import_completed",
        message=(
            f"Import complete: {imported_count} series imported, "
            f"{failed_count} series failed, "
            f"{total_files_imported} files imported, "
            f"{total_files_failed} files failed"
        ),
        imported=imported_count,
        failed=failed_count,
        files_imported=total_files_imported,
        files_failed=total_files_failed,
    )
    for request in pending_catalog_hydrations:
        _schedule_catalog_hydration(
            session,
            series_service=series_service,
            series_id=request.series_id,
            search_on_add=request.search_on_add,
        )


async def _execute_story_arc_materialization(
    session: AsyncSession,
    job: ImportJob,
    *,
    job_id: int,
    raise_if_cancelled: RaiseIfCancelledFunc,
    record_action: RecordActionFunc,
    log_event: LogEventFunc,
    emit_progress: EmitProgressFunc,
    estimate_remaining_seconds: EstimateRemainingFunc,
    progress_callback: Callable[[ImportProgressEvent], Awaitable[None]] | None,
    runtime_revision_state: dict[str, int],
    job_started_at: datetime | None,
) -> ImportJob:
    """Resolve and register confirmed arcs without reopening canonical work."""

    async def cancellation_checkpoint() -> None:
        await raise_if_cancelled(session, job_id)

    try:
        resolution = await resolve_staged_story_arc_entries(
            session,
            import_job_id=job_id,
            cancellation_check=cancellation_checkpoint,
        )
        materialization = await materialize_confirmed_story_arcs(
            session,
            import_job_id=job_id,
            cancellation_check=cancellation_checkpoint,
            record_action=record_action,
        )
    except (JobPausedError, JobCancelledError):
        raise
    except Exception as exc:
        # Canonical groups commit independently. Discard only the current
        # logical-arc transaction, then leave durable review evidence that the
        # optional registration phase failed.
        await session.rollback()
        persisted_job = await session.get(ImportJob, job_id)
        if persisted_job is None:
            raise NotFoundError("ImportJob", job_id) from exc
        await _mark_story_arc_materialization_failed(session, job_id=job_id)
        persisted_job.error_message = (
            "Story-arc registration failed; canonical files remain imported."
        )
        await log_event(
            session,
            job_id,
            "ERROR",
            "story_arc_materialization_failed",
            message="Story-arc registration failed; canonical files remain imported.",
            failure_type=type(exc).__name__,
        )
        await session.flush()
        return persisted_job

    warning_codes = sorted({warning.code for warning in materialization.warnings})
    level = "WARNING" if materialization.arcs_failed else "INFO"
    if materialization.arcs_failed:
        job.error_message = (
            "Some story arcs could not be registered; canonical files remain imported."
        )
    await log_event(
        session,
        job_id,
        level,
        "story_arc_materialization_completed",
        message=(
            f"Story-arc registration complete: {materialization.arcs_examined} examined, "
            f"{materialization.arcs_failed} failed."
        ),
        **_story_arc_log_counts(resolution, materialization),
        warning_codes=warning_codes,
    )
    if progress_callback is not None and materialization.arcs_examined:
        runtime_revision_state["value"] += 1
        job.progress_revision = runtime_revision_state["value"]
        await emit_progress(
            session,
            job,
            ImportProgressEvent(
                job_id=job_id,
                status=ImportJobStatus.IMPORTING,
                mode="import",
                phase="importing",
                progress=99,
                message=(
                    f"Registered {materialization.arcs_examined - materialization.arcs_failed}/"
                    f"{materialization.arcs_examined} story arcs."
                ),
                estimated_seconds_remaining=estimate_remaining_seconds(job_started_at, 99),
                series_found=int(job.series_found or 0),
                series_imported=int(job.series_imported or 0),
                series_failed=int(job.series_failed or 0),
                total_files_imported=int(job.total_files_imported or 0),
                total_files_failed=int(job.total_files_failed or 0),
                progress_revision=runtime_revision_state["value"],
            ),
            progress_callback,
        )
    return job


def _story_arc_log_counts(
    resolution: StoryArcResolutionResult,
    materialization: StoryArcMaterializationResult,
) -> dict[str, int]:
    """Return path- and identity-free counters safe for durable job logs."""
    return {
        "entries_examined": resolution.entries_examined,
        "entries_resolved": resolution.resolved,
        "entries_pending": resolution.pending,
        "entries_missing": resolution.missing,
        "entries_ambiguous": resolution.ambiguous,
        "entries_conflicted": resolution.conflicts,
        "entries_skipped": resolution.skipped,
        "files_linked": resolution.linked_files,
        "arcs_examined": materialization.arcs_examined,
        "arcs_created": materialization.arcs_created,
        "arcs_merged": materialization.arcs_merged,
        "arcs_reused": materialization.arcs_reused,
        "arcs_failed": materialization.arcs_failed,
        "memberships_created": materialization.memberships_created,
        "memberships_reused": materialization.memberships_reused,
        "resolved_memberships": materialization.resolved_entries,
        "unresolved_memberships": materialization.unresolved_entries,
    }


async def _mark_story_arc_materialization_failed(
    session: AsyncSession,
    *,
    job_id: int,
) -> None:
    """Persist sanitized failure state after the arc transaction is discarded."""
    staged_arcs = list(
        (
            await session.scalars(
                sa_select(ImportedStoryArc).where(
                    ImportedStoryArc.import_job_id == job_id,
                    ImportedStoryArc.status == ImportedStoryArcStatus.CONFIRMED,
                    ImportedStoryArc.selected_for_import.is_(True),
                )
            )
        ).all()
    )
    for staged_arc in staged_arcs:
        staged_arc.status = ImportedStoryArcStatus.FAILED
        staged_arc.materialized_story_arc_id = None
        diagnostics = dict(staged_arc.diagnostics or {})
        diagnostics["materialization"] = {
            "schema_version": 1,
            "status": "failed",
            "story_arc_id": None,
            "counts": {},
            "warning_codes": ["unexpected_materialization_failure"],
        }
        staged_arc.diagnostics = diagnostics
    await session.flush()


async def _load_confirmed_import_series(
    session: AsyncSession,
    job_id: int,
) -> list[ImportedSeries]:
    result = await session.execute(
        sa_select(ImportedSeries)
        .where(
            ImportedSeries.import_job_id == job_id,
            ImportedSeries.status.in_([ImportSeriesStatus.CONFIRMED, ImportSeriesStatus.IMPORTING]),
        )
        .order_by(ImportedSeries.id.asc())
    )
    return list(result.scalars().all())


async def _load_duplicate_import_series(
    session: AsyncSession,
    job_id: int,
    *,
    confirmed_ids: set[int],
) -> list[ImportedSeries]:
    duplicate_result = await session.execute(
        sa_select(ImportedSeries)
        .join(ImportedFile, ImportedFile.import_series_id == ImportedSeries.id)
        .where(
            ImportedSeries.import_job_id == job_id,
            ImportedSeries.status == ImportSeriesStatus.DUPLICATE,
            ImportedSeries.series_id.is_not(None),
            ImportedFile.include_in_import.is_(True),
            ImportedFile.status.in_([ImportedFileStatus.MATCHED, ImportedFileStatus.CONFIRMED]),
        )
        .distinct()
    )
    return [item for item in duplicate_result.scalars().all() if item.id not in confirmed_ids]


async def _execute_new_series(
    session: AsyncSession,
    job: ImportJob,
    item: ImportedSeries,
    *,
    imported_count: int,
    failed_count: int,
    prefetched_bundle: tuple[Any, list[Any]] | None = None,
    series_service: SeriesServiceFunc,
    process_series_files: ProcessSeriesFilesFunc,
    record_action: RecordActionFunc,
    log_event: LogEventFunc,
    report_file_progress: ReportFileProgressFunc | None = None,
    queue_catalog_hydration: Callable[[int, bool], None] | None = None,
) -> tuple[int, int, int, int, bool]:
    return await _execute_new_series_impl(
        session,
        job,
        item,
        imported_count=imported_count,
        failed_count=failed_count,
        prefetched_bundle=prefetched_bundle,
        series_service=series_service,
        process_series_files=process_series_files,
        record_action=record_action,
        log_event=log_event,
        report_file_progress=report_file_progress,
        queue_catalog_hydration=queue_catalog_hydration,
        schedule_catalog_hydration_func=_schedule_catalog_hydration,
    )
