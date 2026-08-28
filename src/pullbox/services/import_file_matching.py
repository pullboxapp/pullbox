"""File-level matching orchestration for import jobs."""

from __future__ import annotations

import time
from contextlib import suppress
from inspect import isawaitable, iscoroutine
from typing import TYPE_CHECKING, Any

from sqlalchemy import func as sa_func
from sqlalchemy import select as sa_select

from pullbox.core.exceptions import ImportProviderDegradedError, JobPausedError
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.schemas.import_job import ImportProgressEvent
from pullbox.services.import_file_conflicts import detect_cross_series_conflicts
from pullbox.services.import_file_match_candidates import (
    build_file_match_target_context,
    reset_file_match_state,
    select_file_match_candidate,
)
from pullbox.services.import_file_match_missing_targets import (
    can_mark_missing_issue_targets,
    mark_files_missing_provider_targets,
)
from pullbox.services.import_file_match_outcomes import apply_and_log_file_match_outcome
from pullbox.services.import_file_match_provider_errors import (
    defer_file_matching_for_provider_error,
)
from pullbox.services.import_file_match_results import apply_file_match_series_summary
from pullbox.services.import_file_match_targets import (
    trusted_source_issue_identity_matches_target,
)
from pullbox.services.import_file_matching_progress import (
    build_file_matching_progress_emitter,
    load_file_match_target_index_with_progress,
)
from pullbox.services.import_file_split_series import (
    split_explicit_issue_series_mismatches as _split_explicit_issue_series_mismatches,
)
from pullbox.services.import_progress_runtime import (
    ScanReviewFileMatchProfile,
    ScanReviewSeriesMatchProfile,
    current_item_payload,
    estimate_remaining_work_seconds,
    scan_review_completed_weight,
    scan_review_progress_pct,
    scan_review_progress_plan,
)
from pullbox.services.import_source_metadata import load_archive_entry_issue_hint_for_import_file
from pullbox.services.import_workflow_state import (
    SCAN_PROGRESS_FILE_MATCH_END,
    SCAN_PROGRESS_FILE_MATCH_START,
    emit_live_progress,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.core.source_metadata import SourceMetadata
    from pullbox.models.series import Series
    from pullbox.providers.base import MetadataProvider
    from pullbox.services.import_duplicates import DuplicateMergeProfile
    from pullbox.services.import_file_match_candidates import FileMatchCandidate
    from pullbox.services.import_file_match_outcomes import (
        DuplicateTargetStateFunc as OutcomeDuplicateTargetStateFunc,
    )
    from pullbox.services.import_file_match_targets import FileMatchTargetIndex
    from pullbox.services.semantic_matching import SemanticMatchEngine

    IsDuplicateSeriesFunc = Callable[[ImportedSeries], bool]
    BuildDuplicateMergeProfileFunc = Callable[..., DuplicateMergeProfile]
    DuplicateTargetStateFunc = OutcomeDuplicateTargetStateFunc
    SourceMetadataForImportFileFunc = Callable[[ImportedSeries, ImportedFile], SourceMetadata]
    DeferredSourceMetadataForImportFileFunc = Callable[
        [ImportedSeries, ImportedFile],
        Awaitable[SourceMetadata],
    ]
    BuildImportMetadataConflictFunc = Callable[..., dict[str, Any] | None]
    RaiseIfCancelledFunc = Callable[[AsyncSession, int], Awaitable[None]]
    DetectDuplicateCopiesFunc = Callable[..., Awaitable[tuple[int, int, Any]]]
    DetectConflictsFunc = Callable[..., tuple[int, int, list[dict[str, Any]]]]
    RecomputeCountersFunc = Callable[[AsyncSession, ImportJob], Awaitable[None]]
    LogEventFunc = Callable[..., Awaitable[None]]
    EmitProgressFunc = Callable[
        [
            AsyncSession,
            ImportJob,
            ImportProgressEvent,
            Callable[[ImportProgressEvent], Awaitable[None]],
        ],
        Awaitable[None],
    ]
    PhaseProgressFunc = Callable[[int, int, int, int], int]
    EstimateRemainingFunc = Callable[[datetime | None, int], int | None]
    JobStatsFunc = Callable[[ImportJob], dict[str, int]]
    SlowItemDelayFunc = Callable[[], Awaitable[None]]
    WarningLogFunc = Callable[..., None]


async def run_import_file_matching(
    session: AsyncSession,
    job: ImportJob,
    *,
    metadata_provider: MetadataProvider | None,
    semantic_match_engine: SemanticMatchEngine,
    is_duplicate_series: IsDuplicateSeriesFunc,
    build_duplicate_merge_profile: BuildDuplicateMergeProfileFunc,
    duplicate_target_state: DuplicateTargetStateFunc,
    source_metadata_for_import_file: SourceMetadataForImportFileFunc,
    load_deferred_source_metadata_for_import_file: DeferredSourceMetadataForImportFileFunc | None,
    build_import_metadata_conflict: BuildImportMetadataConflictFunc,
    raise_if_cancelled: RaiseIfCancelledFunc,
    detect_duplicate_copies: DetectDuplicateCopiesFunc,
    detect_conflicts: DetectConflictsFunc,
    recompute_file_counters: RecomputeCountersFunc,
    recompute_series_counters: RecomputeCountersFunc,
    log_event: LogEventFunc,
    emit_progress: EmitProgressFunc,
    phase_progress: PhaseProgressFunc,
    estimate_remaining_seconds: EstimateRemainingFunc,
    job_stats: JobStatsFunc,
    maybe_slow_item_delay: SlowItemDelayFunc,
    log_warning: WarningLogFunc,
    series_ids: list[int] | None = None,
    progress_callback: Callable[[ImportProgressEvent], Awaitable[None]] | None = None,
) -> None:
    """Match individual imported files to issue targets for review."""
    started_at = time.monotonic()
    persist_batch_size = 10
    last_checkpoint_at = time.monotonic()
    series_query = sa_select(ImportedSeries).where(
        ImportedSeries.import_job_id == job.id,
        ImportedSeries.status.in_([ImportSeriesStatus.MATCHED, ImportSeriesStatus.DUPLICATE]),
    )
    if series_ids:
        series_query = series_query.where(ImportedSeries.id.in_(series_ids))
    items_result = await session.execute(series_query)
    candidate_series = list(items_result.scalars().all())
    matched_series_list = [
        series_item
        for series_item in candidate_series
        if is_duplicate_series(series_item)
        or (
            series_item.status == ImportSeriesStatus.MATCHED
            and (series_item.series_id is not None or series_item.cv_id is not None)
        )
    ]

    total_found = 0
    total_matched = 0
    total_duplicate = 0
    total_already_owned = 0
    total_no_match = 0
    total_conflict = 0
    conflict_group_counter = 0
    duplicate_group_counter = 0
    deferred_count = 0
    deferred_titles: list[str] = []
    split_series_ids: set[int] = set()
    target_index_duration_ms = 0.0
    file_evaluation_duration_ms = 0.0
    deferred_metadata_duration_ms = 0.0
    deferred_metadata_loads = 0
    series_processed = 0
    files_processed = 0
    if series_ids:
        counter_result = await session.execute(
            sa_select(
                sa_func.max(ImportedFile.conflict_group_id),
                sa_func.max(ImportedFile.duplicate_group_id),
            ).where(ImportedFile.import_job_id == job.id)
        )
        max_conflict_group_id, max_duplicate_group_id = counter_result.one()
        conflict_group_counter = int(max_conflict_group_id or 0)
        duplicate_group_counter = int(max_duplicate_group_id or 0)
    total_file_phase_units = len(matched_series_list) + sum(
        max(int(series_item.files_total or series_item.file_count or 0), 0)
        for series_item in matched_series_list
    )
    series_match_profile_count = max(
        int(job.series_matched or 0) + int(job.series_no_match or 0),
        len(matched_series_list),
    )
    progress_plan = scan_review_progress_plan(
        analysis_series_count=max(int(job.series_found or 0), series_match_profile_count),
        series_match_profiles=[
            ScanReviewSeriesMatchProfile(direct_match=False)
            for _idx in range(series_match_profile_count)
        ],
        file_match_profiles=[
            ScanReviewFileMatchProfile(
                file_count=max(int(series_item.files_total or series_item.file_count or 0), 0),
                issue_count=series_item.cv_issue_count,
            )
            for series_item in matched_series_list
        ],
    )
    completed_file_phase_units = 0
    total_series = max(len(matched_series_list), 1)
    runtime_revision_state: dict[str, int] = {"value": int(job.progress_revision or 0)}

    emit_file_matching_progress = build_file_matching_progress_emitter(
        session=session,
        job=job,
        progress_callback=progress_callback,
        emit_progress=emit_progress,
        emit_live_progress=emit_live_progress,
        phase_progress=phase_progress,
        estimate_remaining_seconds=estimate_remaining_seconds,
        estimate_remaining_work_seconds=estimate_remaining_work_seconds,
        job_stats=job_stats,
        total_file_phase_units=total_file_phase_units,
        revision_state=runtime_revision_state,
        scan_review_plan=progress_plan,
        phase_start=SCAN_PROGRESS_FILE_MATCH_START,
        phase_end=SCAN_PROGRESS_FILE_MATCH_END,
    )

    for series_idx, imp_series in enumerate(matched_series_list):
        await raise_if_cancelled(session, job.id)
        duplicate_series = is_duplicate_series(imp_series)

        files_result = await session.execute(
            sa_select(ImportedFile).where(
                ImportedFile.import_series_id == imp_series.id,
                ImportedFile.status.in_(
                    [ImportedFileStatus.PENDING, ImportedFileStatus.SAFETY_APPROVED]
                ),
            )
        )
        files = list(files_result.scalars().all())
        if not files:
            continue

        target_index_started_at = time.monotonic()
        try:
            target_index = await load_file_match_target_index_with_progress(
                session=session,
                job_id=job.id,
                item=imp_series,
                files_to_match=files,
                duplicate_series=duplicate_series,
                metadata_provider=metadata_provider,
                series_idx=series_idx,
                total_series=total_series,
                completed_units=completed_file_phase_units,
                progress_callback=progress_callback,
                emit_file_matching_progress=emit_file_matching_progress,
                raise_if_cancelled=raise_if_cancelled,
            )
            target_index_duration_ms += (time.monotonic() - target_index_started_at) * 1000
        except ImportProviderDegradedError as exc:
            target_index_duration_ms += (time.monotonic() - target_index_started_at) * 1000
            deferred_count += 1
            deferred_titles.append(imp_series.raw_series_name)
            await defer_file_matching_for_provider_error(
                session=session,
                job_id=job.id,
                imp_series=imp_series,
                exc=exc,
                log_event=log_event,
            )
            continue
        except Exception:
            if imp_series.cv_id is not None:
                log_warning(
                    "file_matching_cv_fetch_failed",
                    series=imp_series.raw_series_name,
                    cv_id=imp_series.cv_id,
                )
                continue
            raise

        completed_file_phase_units += 1
        if not target_index.has_targets:
            if not can_mark_missing_issue_targets(imp_series, metadata_provider):
                continue
            mark_files_missing_provider_targets(imp_series, files)

        series_high_confidence = (
            imp_series.cv_match_score is not None and imp_series.cv_match_score >= 0.90
        )
        duplicate_merge_profile = (
            build_duplicate_merge_profile(
                target_index.existing_series,
                target_index.issue_entries,
                incoming_file_count=len(files),
            )
            if duplicate_series
            else None
        )
        files, created_split_series_ids = await _split_explicit_issue_series_mismatches(
            session,
            job,
            imp_series,
            files,
            metadata_provider=metadata_provider,
            source_metadata_for_import_file=source_metadata_for_import_file,
            target_series=target_index.existing_series,
            log_event=log_event,
        )
        split_series_ids.update(created_split_series_ids)
        if not files:
            continue

        await emit_file_matching_progress(
            imp_series,
            completed_file_phase_units,
            message=(
                f"Matching files to issues for {imp_series.raw_series_name} "
                f"({len(files)} file{'s' if len(files) != 1 else ''})..."
            ),
        )
        series_processed += 1
        for file_idx, imp_file in enumerate(files, start=1):
            files_processed += 1
            reset_file_match_state(imp_file)
            file_metadata = source_metadata_for_import_file(imp_series, imp_file)
            file_evaluation_started_at = time.monotonic()
            match_candidate, metadata_conflict = _evaluate_file_match_candidate(
                imp_series=imp_series,
                imp_file=imp_file,
                target_index=target_index,
                target_series=target_index.existing_series,
                file_metadata=file_metadata,
                semantic_match_engine=semantic_match_engine,
                build_import_metadata_conflict=build_import_metadata_conflict,
                series_high_confidence=series_high_confidence,
            )
            if match_candidate is not None:
                enriched_metadata = await load_archive_entry_issue_hint_for_import_file(
                    imp_file,
                    file_metadata,
                )
                if enriched_metadata.diagnostics != file_metadata.diagnostics:
                    file_metadata = enriched_metadata
                    match_candidate, metadata_conflict = _evaluate_file_match_candidate(
                        imp_series=imp_series,
                        imp_file=imp_file,
                        target_index=target_index,
                        target_series=target_index.existing_series,
                        file_metadata=file_metadata,
                        semantic_match_engine=semantic_match_engine,
                        build_import_metadata_conflict=build_import_metadata_conflict,
                        series_high_confidence=series_high_confidence,
                    )
            file_evaluation_duration_ms += (time.monotonic() - file_evaluation_started_at) * 1000
            if (
                match_candidate is None
                and load_deferred_source_metadata_for_import_file is not None
                and _file_has_deferred_archive_metadata(imp_file)
            ):
                deferred_metadata_loads += 1
                deferred_metadata_started_at = time.monotonic()
                file_metadata = await load_deferred_source_metadata_for_import_file(
                    imp_series,
                    imp_file,
                )
                deferred_metadata_duration_ms += (
                    time.monotonic() - deferred_metadata_started_at
                ) * 1000
                file_evaluation_started_at = time.monotonic()
                match_candidate, metadata_conflict = _evaluate_file_match_candidate(
                    imp_series=imp_series,
                    imp_file=imp_file,
                    target_index=target_index,
                    target_series=target_index.existing_series,
                    file_metadata=file_metadata,
                    semantic_match_engine=semantic_match_engine,
                    build_import_metadata_conflict=build_import_metadata_conflict,
                    series_high_confidence=series_high_confidence,
                )
                if match_candidate is not None:
                    enriched_metadata = await load_archive_entry_issue_hint_for_import_file(
                        imp_file,
                        file_metadata,
                    )
                    if enriched_metadata.diagnostics != file_metadata.diagnostics:
                        file_metadata = enriched_metadata
                        match_candidate, metadata_conflict = _evaluate_file_match_candidate(
                            imp_series=imp_series,
                            imp_file=imp_file,
                            target_index=target_index,
                            target_series=target_index.existing_series,
                            file_metadata=file_metadata,
                            semantic_match_engine=semantic_match_engine,
                            build_import_metadata_conflict=build_import_metadata_conflict,
                            series_high_confidence=series_high_confidence,
                        )
                file_evaluation_duration_ms += (
                    time.monotonic() - file_evaluation_started_at
                ) * 1000

            await apply_and_log_file_match_outcome(
                session=session,
                job_id=job.id,
                imp_file=imp_file,
                imp_series=imp_series,
                match_candidate=match_candidate,
                duplicate_series=duplicate_series,
                duplicate_target_state=duplicate_target_state,
                duplicate_merge_profile=duplicate_merge_profile,
                metadata_conflict=metadata_conflict,
                log_event=log_event,
            )
            completed_file_phase_units += 1
            await emit_file_matching_progress(
                imp_series,
                completed_file_phase_units,
                message=(f"Matched file {file_idx}/{len(files)} for {imp_series.raw_series_name}"),
                current_item_stage="file_matching",
                current_item_progress_pct=round((file_idx / max(len(files), 1)) * 100),
                live_only=True,
            )

        (
            _duplicate_count,
            duplicate_group_counter,
            _duplicate_groups,
        ) = await detect_duplicate_copies(
            session,
            job,
            imp_series,
            files,
            duplicate_group_counter,
        )
        _conflict_count, conflict_group_counter, conflict_groups = detect_conflicts(
            files,
            conflict_group_counter,
        )

        for group_details in conflict_groups:
            await log_event(
                session,
                job.id,
                "DEBUG",
                "import_file_conflict_detail",
                message=(
                    f"Conflict group {group_details['conflict_group_id']} in "
                    f"{imp_series.raw_series_name}"
                ),
                series=imp_series.raw_series_name,
                diagnostics=group_details,
            )

        series_summary = apply_file_match_series_summary(
            imp_series,
            files,
            duplicate_series=duplicate_series,
            duplicate_merge_profile=duplicate_merge_profile,
            cv_match_threshold=job.cv_match_threshold,
        )
        if series_summary.series_invalidated:
            invalidation_reason = (series_summary.invalidation_diagnostics or {}).get("reason")
            await log_event(
                session,
                job.id,
                "DEBUG",
                "import_series_match_invalidated",
                message=(
                    "Matched series moved to review after file matching failed: "
                    f"{imp_series.raw_series_name}"
                ),
                series=imp_series.raw_series_name,
                reason=invalidation_reason,
                diagnostics=series_summary.invalidation_diagnostics,
            )

        total_found += series_summary.found
        total_matched += series_summary.matched
        total_duplicate += series_summary.duplicate
        total_already_owned += series_summary.already_owned
        total_no_match += series_summary.no_match
        total_conflict += series_summary.conflict
        job.total_files_found = total_found
        job.total_files_matched = total_matched
        job.total_files_duplicate = total_duplicate
        job.total_files_already_owned = total_already_owned
        job.total_files_no_match = total_no_match
        job.total_files_conflict = total_conflict

        should_checkpoint = (
            (series_idx == 0)
            or ((series_idx + 1) % persist_batch_size == 0)
            or series_idx == len(matched_series_list) - 1
            or (time.monotonic() - last_checkpoint_at) >= 0.5
        )
        if should_checkpoint:
            await session.flush()
            await session.commit()
            last_checkpoint_at = time.monotonic()

        if progress_callback and should_checkpoint:
            completed_weight = scan_review_completed_weight(
                progress_plan,
                phase="file_matching",
                completed_items=completed_file_phase_units,
            )
            progress = scan_review_progress_pct(
                progress_plan,
                completed_weight=completed_weight,
            )
            await emit_progress(
                session,
                job,
                ImportProgressEvent(
                    job_id=job.id,
                    status=ImportJobStatus.FILE_MATCHING,
                    phase="file_matching",
                    progress=progress,
                    message=f"Matched files in {imp_series.raw_series_name}",
                    current_series=imp_series.raw_series_name,
                    estimated_seconds_remaining=estimate_remaining_work_seconds(
                        job.scan_completed_at or job.match_completed_at or job.scan_started_at,
                        completed_units=completed_weight,
                        total_units=progress_plan.total_weight,
                    ),
                    **current_item_payload(
                        kind="series",
                        stage="file_matching",
                        name=imp_series.raw_series_name,
                        progress_pct=100,
                    ),
                    **job_stats(job),
                ),
                progress_callback,
            )
            await maybe_slow_item_delay()

    if split_series_ids:
        await session.flush()
        await session.commit()
        await run_import_file_matching(
            session,
            job,
            metadata_provider=metadata_provider,
            semantic_match_engine=semantic_match_engine,
            is_duplicate_series=is_duplicate_series,
            build_duplicate_merge_profile=build_duplicate_merge_profile,
            duplicate_target_state=duplicate_target_state,
            source_metadata_for_import_file=source_metadata_for_import_file,
            load_deferred_source_metadata_for_import_file=load_deferred_source_metadata_for_import_file,
            build_import_metadata_conflict=build_import_metadata_conflict,
            raise_if_cancelled=raise_if_cancelled,
            detect_duplicate_copies=detect_duplicate_copies,
            detect_conflicts=detect_conflicts,
            recompute_file_counters=recompute_file_counters,
            recompute_series_counters=recompute_series_counters,
            log_event=log_event,
            emit_progress=emit_progress,
            phase_progress=phase_progress,
            estimate_remaining_seconds=estimate_remaining_seconds,
            job_stats=job_stats,
            maybe_slow_item_delay=maybe_slow_item_delay,
            log_warning=log_warning,
            series_ids=sorted(split_series_ids),
            progress_callback=None,
        )

    conflict_group_counter = await _rebuild_cross_series_conflicts(
        session,
        job,
        conflict_group_counter=conflict_group_counter,
        is_duplicate_series=is_duplicate_series,
        cv_match_threshold=job.cv_match_threshold,
        log_event=log_event,
    )

    if deferred_count:
        job.error_message = (
            f"ComicVine issue targets were unavailable for {deferred_count} matched series. "
            "Import paused so file matching can be retried."
        )
        await recompute_file_counters(session, job)
        await recompute_series_counters(session, job)
        await session.flush()
        await log_event(
            session,
            job.id,
            "WARNING",
            "import_file_matching_provider_degraded",
            message=job.error_message,
            deferred_count=deferred_count,
            deferred_titles=deferred_titles[:10],
            duration_ms=round((time.monotonic() - started_at) * 1000),
            series_processed=series_processed,
            files_processed=files_processed,
            target_index_duration_ms=round(target_index_duration_ms),
            file_evaluation_duration_ms=round(file_evaluation_duration_ms),
            deferred_metadata_duration_ms=round(deferred_metadata_duration_ms),
            deferred_metadata_loads=deferred_metadata_loads,
            provider_cache_metrics=_provider_cache_metrics(metadata_provider),
        )
        await session.commit()
        raise JobPausedError(job.error_message)

    await recompute_file_counters(session, job)
    await recompute_series_counters(session, job)
    await session.flush()

    await log_event(
        session,
        job.id,
        "INFO",
        "import_file_matching_completed",
        message=(
            f"File matching complete: {job.total_files_matched} matched, "
            f"{job.total_files_already_owned} already owned, "
            f"{job.total_files_no_match} no match, {job.total_files_conflict} conflicts"
        ),
        files_matched=job.total_files_matched,
        files_already_owned=job.total_files_already_owned,
        files_no_match=job.total_files_no_match,
        files_conflict=job.total_files_conflict,
        duration_ms=round((time.monotonic() - started_at) * 1000),
        series_processed=series_processed,
        files_processed=files_processed,
        target_index_duration_ms=round(target_index_duration_ms),
        file_evaluation_duration_ms=round(file_evaluation_duration_ms),
        deferred_metadata_duration_ms=round(deferred_metadata_duration_ms),
        deferred_metadata_loads=deferred_metadata_loads,
        provider_cache_metrics=_provider_cache_metrics(metadata_provider),
    )


def _file_has_deferred_archive_metadata(imp_file: ImportedFile) -> bool:
    diagnostics = dict(imp_file.diagnostics or {})
    source_metadata = diagnostics.get("source_metadata")
    return bool(
        isinstance(source_metadata, dict)
        and source_metadata.get("archive_metadata_deferred") is True
    )


def _provider_cache_metrics(metadata_provider: MetadataProvider | None) -> dict[str, Any]:
    metrics = getattr(metadata_provider, "cache_metrics", None)
    if not callable(metrics):
        return {}
    result = metrics()
    if iscoroutine(result):
        with suppress(Exception):
            result.close()
        return {}
    if isawaitable(result):
        return {}
    return dict(result) if isinstance(result, dict) else {}


def _evaluate_file_match_candidate(
    *,
    imp_series: ImportedSeries,
    imp_file: ImportedFile,
    target_index: FileMatchTargetIndex,
    target_series: Series | None,
    file_metadata: SourceMetadata,
    semantic_match_engine: SemanticMatchEngine,
    build_import_metadata_conflict: BuildImportMetadataConflictFunc,
    series_high_confidence: bool,
) -> tuple[FileMatchCandidate | None, dict[str, Any] | None]:
    match_candidate = select_file_match_candidate(
        imp_file,
        target_index,
        series_high_confidence=series_high_confidence,
        imp_series=imp_series,
    )
    metadata_conflict: dict[str, Any] | None = None

    if match_candidate is not None:
        if trusted_source_issue_identity_matches_target(
            imp_series,
            imp_file,
            match_candidate.matched_issue_cv_id,
        ):
            return match_candidate, None
        target_context = build_file_match_target_context(
            imp_series,
            imp_file,
            match_candidate,
            existing_series=target_series,
        )
        semantic_decision = (
            semantic_match_engine.match_against_issue(
                metadata=file_metadata,
                wanted_series=target_context.series_title,
                wanted_issue=float(target_context.issue_number or 0.0),
                wanted_year=target_context.series_year,
                wanted_issue_type=target_context.issue_type,
                wanted_issue_cv_id=target_context.issue_cv_id,
                wanted_issue_title=target_context.issue_title,
            )
            if target_context.issue_number is not None
            else None
        )
        if semantic_decision is not None and not semantic_decision.is_match:
            match_candidate = None
        elif semantic_decision is not None:
            metadata_conflict = build_import_metadata_conflict(
                metadata=file_metadata,
                target_series_title=target_context.series_title,
                target_series_year=target_context.series_year,
                target_issue_number=target_context.issue_number,
                target_issue_cv_id=target_context.issue_cv_id,
                target_issue_title=target_context.issue_title,
            )
            if metadata_conflict is not None:
                match_candidate = None

    return match_candidate, metadata_conflict


def _resolved_target_series_key(imp_series: ImportedSeries) -> tuple[str, int] | None:
    if imp_series.series_id is not None:
        return ("series", int(imp_series.series_id))
    if imp_series.cv_id is not None:
        return ("cv", int(imp_series.cv_id))
    return None


def _is_cross_series_conflict(imp_file: ImportedFile) -> bool:
    diagnostics = dict(imp_file.diagnostics or {})
    return (
        imp_file.status == ImportedFileStatus.CONFLICT
        and diagnostics.get("kind") == "file_conflict"
        and diagnostics.get("scope") == "cross_series"
    )


async def _rebuild_cross_series_conflicts(
    session: AsyncSession,
    job: ImportJob,
    *,
    conflict_group_counter: int,
    is_duplicate_series: IsDuplicateSeriesFunc,
    cv_match_threshold: float,
    log_event: LogEventFunc,
) -> int:
    series_result = await session.execute(
        sa_select(ImportedSeries).where(
            ImportedSeries.import_job_id == job.id,
            ImportedSeries.status.in_([ImportSeriesStatus.MATCHED, ImportSeriesStatus.DUPLICATE]),
        )
    )
    series_list = list(series_result.scalars().all())
    target_series_key_by_series_id = {
        imp_series.id: key
        for imp_series in series_list
        if (key := _resolved_target_series_key(imp_series)) is not None
    }
    if not target_series_key_by_series_id:
        return conflict_group_counter

    file_result = await session.execute(
        sa_select(ImportedFile).where(
            ImportedFile.import_job_id == job.id,
            ImportedFile.import_series_id.in_(list(target_series_key_by_series_id)),
            ImportedFile.status.in_([ImportedFileStatus.MATCHED, ImportedFileStatus.CONFLICT]),
        )
    )
    files = list(file_result.scalars().all())
    if not files:
        return conflict_group_counter

    affected_series_ids: set[int] = set()
    target_series_key_by_file_id: dict[int, tuple[str, int]] = {}
    series_by_id = {imp_series.id: imp_series for imp_series in series_list}
    for imp_file in files:
        if imp_file.id is None:
            continue
        target_series_key = target_series_key_by_series_id.get(imp_file.import_series_id)
        if target_series_key is None:
            continue
        target_series_key_by_file_id[imp_file.id] = target_series_key
        if _is_cross_series_conflict(imp_file):
            imp_file.status = ImportedFileStatus.MATCHED
            imp_file.conflict_group_id = None
            imp_file.is_preferred = False
            imp_file.include_in_import = False
            previous_diagnostics = dict(imp_file.diagnostics or {}).get("previous_diagnostics")
            imp_file.diagnostics = (
                dict(previous_diagnostics) if isinstance(previous_diagnostics, dict) else {}
            )
            affected_series_ids.add(imp_file.import_series_id)

    (
        _cross_conflict_count,
        conflict_group_counter,
        cross_conflict_groups,
    ) = detect_cross_series_conflicts(
        files,
        conflict_group_counter,
        target_series_key_by_file_id=target_series_key_by_file_id,
    )

    for group_details in cross_conflict_groups:
        group_file_ids = {file_info["file_id"] for file_info in group_details.get("files", [])}
        group_series_labels = sorted(
            {
                series_item.raw_series_name
                for imp_file in files
                if imp_file.id in group_file_ids
                for series_item in [series_by_id.get(imp_file.import_series_id)]
                if series_item is not None
            }
        )
        for imp_file in files:
            if imp_file.id in group_file_ids:
                affected_series_ids.add(imp_file.import_series_id)
        await log_event(
            session,
            job.id,
            "DEBUG",
            "import_file_conflict_detail",
            message=(
                f"Cross-series conflict group {group_details['conflict_group_id']} across "
                f"{', '.join(group_series_labels)}"
            ),
            series=", ".join(group_series_labels),
            diagnostics=group_details,
        )

    if not affected_series_ids:
        return conflict_group_counter

    affected_files_result = await session.execute(
        sa_select(ImportedFile).where(
            ImportedFile.import_series_id.in_(sorted(affected_series_ids))
        )
    )
    files_by_series_id: dict[int, list[ImportedFile]] = {}
    for imp_file in affected_files_result.scalars().all():
        files_by_series_id.setdefault(imp_file.import_series_id, []).append(imp_file)

    for series_id in sorted(affected_series_ids):
        imp_series = series_by_id.get(series_id)
        if imp_series is None:
            continue
        apply_file_match_series_summary(
            imp_series,
            files_by_series_id.get(series_id, []),
            duplicate_series=is_duplicate_series(imp_series),
            duplicate_merge_profile=None,
            cv_match_threshold=cv_match_threshold,
        )

    return conflict_group_counter
