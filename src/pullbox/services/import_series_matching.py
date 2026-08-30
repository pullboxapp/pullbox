"""Series-level ComicVine matching orchestration for import jobs."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass
from inspect import isawaitable, iscoroutine
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import func as sa_func
from sqlalchemy import select as sa_select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker

from pullbox.core.exceptions import ImportProviderDegradedError, JobCancelledError, JobPausedError
from pullbox.core.name_matcher import NameMatcher
from pullbox.core.source_metadata import (
    MetadataSignal,
    SourceMetadata,
    VolumeSubtitleHint,
    volume_subtitle_hint_from_filename,
)
from pullbox.core.sqlite_lock import is_sqlite_locked_error
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.models.issue import IssueType
from pullbox.schemas.import_job import ImportProgressEvent
from pullbox.services.import_known_cv_match import (
    ComicVineMatchEvaluation,
    known_cv_id_evaluation_from_source,
)
from pullbox.services.import_progress_runtime import (
    ScanReviewFileMatchProfile,
    ScanReviewProgressPlan,
    ScanReviewSeriesMatchProfile,
    current_item_payload,
    scan_review_analysis_weight,
    scan_review_file_match_weight,
    scan_review_progress_pct,
    scan_review_series_match_weight,
)
from pullbox.services.import_series_match_state import clear_auto_cv_match_fields
from pullbox.services.import_workflow_state import emit_live_progress

logger = structlog.get_logger(__name__)

_MATCH_PROGRESS_HEARTBEAT_SECONDS = 5.0
_VOLUME_SUBTITLE_SPLIT_THRESHOLD = 0.95
_TITLE_ARTICLES = frozenset({"a", "an", "the"})
_DIRECT_CV_ID_MATCH_METHODS = frozenset({"mylar3_cv_id", "comicinfo_cv_id", "folder_cv_id"})


def _matching_item_heartbeat_progress(elapsed_seconds: int) -> int:
    """Return item-local progress for an in-flight ComicVine series match."""
    return min(90, 15 + (max(elapsed_seconds, 1) // 5) * 15)


def _filesystem_source_identity_evaluation(
    item: ImportedSeries,
    source_metadata: SourceMetadata,
    *,
    match_threshold: float,
) -> ComicVineMatchEvaluation:
    """Classify a filesystem series from trusted local identity only."""
    normalized_query = NameMatcher.normalize(source_metadata.series_name or item.raw_series_name)
    identity_conflicts = source_metadata.diagnostics.get("identity_conflicts")
    if isinstance(identity_conflicts, list) and identity_conflicts:
        return ComicVineMatchEvaluation(
            match=None,
            diagnostics={
                "kind": "series_conflict",
                "reason": "trusted_source_identity_conflict",
                "raw_name": item.raw_series_name,
                "raw_year": item.raw_year,
                "normalized_query": normalized_query,
                "threshold": round(match_threshold, 4),
                "identity_conflicts": [
                    dict(conflict) for conflict in identity_conflicts if isinstance(conflict, dict)
                ],
                "provider_lookup_deferred": True,
                "top_candidates": [],
            },
        )

    cv_id = source_metadata.comicvine_series_id or item.cv_id
    match_method = item.cv_match_method
    if match_method not in _DIRECT_CV_ID_MATCH_METHODS:
        signal = source_metadata.signals.get("comicvine_series_id")
        match_method = (
            {
                MetadataSignal.MYLAR3: "mylar3_cv_id",
                MetadataSignal.COMICINFO: "comicinfo_cv_id",
                MetadataSignal.SIDECAR: "comicinfo_cv_id",
                MetadataSignal.PULLBOX_FOLDER: "folder_cv_id",
            }.get(signal)
            if signal is not None
            else None
        )
    if cv_id is not None and match_method in _DIRECT_CV_ID_MATCH_METHODS:
        return known_cv_id_evaluation_from_source(
            int(cv_id),
            match_method=match_method,
            raw_name=item.raw_series_name,
            raw_year=item.raw_year,
            normalized_query=normalized_query,
            source_metadata=source_metadata,
            match_threshold=match_threshold,
        )

    return ComicVineMatchEvaluation(
        match=None,
        diagnostics={
            "kind": "series_no_match",
            "reason": "trusted_source_identity_missing",
            "raw_name": item.raw_series_name,
            "raw_year": item.raw_year,
            "normalized_query": normalized_query,
            "threshold": round(match_threshold, 4),
            "provider_lookup_deferred": True,
            "top_candidates": [],
        },
    )


@dataclass(frozen=True)
class VolumeSubtitleRebucketResult:
    """Result of moving collection-volume files into matched subtitle rows."""

    created_series_count: int = 0
    removed_source_series: bool = False
    evaluated_file_count: int = 0


@dataclass(frozen=True)
class _VolumeSubtitleRebucketMatch:
    """A safe rebucket decision collected before mutating import rows."""

    imp_file: ImportedFile
    hint: VolumeSubtitleHint
    raw_year: int | None
    cv_result: dict[str, Any]
    diagnostics: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _ScanReviewProfile:
    """Minimal persisted facts needed to aggregate matching progress."""

    id: int
    file_count: int
    direct_match: bool
    has_files: bool
    issue_count: int | None


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
    from datetime import datetime
    from typing import Protocol

    from sqlalchemy.ext.asyncio import AsyncSession

    SourceMetadataForSeriesFunc = Callable[
        [AsyncSession, ImportedSeries],
        Awaitable[SourceMetadata],
    ]
    DeferredSourceMetadataForSeriesFunc = Callable[
        [AsyncSession, ImportedSeries],
        Awaitable[SourceMetadata],
    ]
    EvaluateMatchFunc = Callable[..., Coroutine[Any, Any, ComicVineMatchEvaluation]]
    RaiseIfCancelledFunc = Callable[[AsyncSession, int], Awaitable[None]]
    ReclassifyDuplicatesFunc = Callable[[AsyncSession, ImportJob], Awaitable[int | None]]
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

    class EstimateRemainingWorkFunc(Protocol):
        def __call__(
            self,
            started_at: datetime | None,
            *,
            completed_units: int | float,
            total_units: int | float,
            current_unit_progress_pct: int | float | None = None,
        ) -> int | None: ...

    JobStatsFunc = Callable[[ImportJob], dict[str, int]]
    SlowItemDelayFunc = Callable[[], Awaitable[None]]


async def _log_detail_event_best_effort(
    session: AsyncSession,
    *,
    job_id: int,
    level: str,
    event: str,
    message: str,
    log_event: LogEventFunc,
    **details: Any,
) -> None:
    """Persist optional per-series detail logs without risking match state."""
    bind = session.bind
    session_factory = (
        async_sessionmaker(bind, expire_on_commit=False, class_=type(session))
        if bind is not None
        else None
    )

    log_session = session
    try:
        if session_factory is not None:
            async with session_factory() as isolated_session:
                log_session = isolated_session
                await log_event(
                    isolated_session,
                    job_id,
                    level,
                    event,
                    message=message,
                    **details,
                )
                await isolated_session.commit()
        else:
            await log_event(
                session,
                job_id,
                level,
                event,
                message=message,
                **details,
            )
            await session.commit()
    except OperationalError as exc:
        with suppress(Exception):
            await log_session.rollback()
        log_kwargs = {
            "job_id": job_id,
            "detail_event": event,
            "error": str(exc),
        }
        if is_sqlite_locked_error(exc):
            logger.warning("import_series_detail_log_skipped_sqlite_locked", **log_kwargs)
        else:
            logger.warning("import_series_detail_log_failed", **log_kwargs)
    except Exception as exc:
        with suppress(Exception):
            await log_session.rollback()
        logger.warning(
            "import_series_detail_log_failed",
            job_id=job_id,
            detail_event=event,
            error=str(exc),
        )


async def _count_pending_import_series(session: AsyncSession, *, job_id: int) -> int:
    count = await session.scalar(
        sa_select(sa_func.count(ImportedSeries.id)).where(
            ImportedSeries.import_job_id == job_id,
            ImportedSeries.status == ImportSeriesStatus.PENDING,
        )
    )
    return int(count or 0)


async def _load_pending_import_series_page(
    session: AsyncSession,
    *,
    job_id: int,
    after_id: int,
    page_size: int,
) -> list[ImportedSeries]:
    result = await session.execute(
        sa_select(ImportedSeries)
        .where(
            ImportedSeries.import_job_id == job_id,
            ImportedSeries.status == ImportSeriesStatus.PENDING,
            ImportedSeries.id > after_id,
        )
        .order_by(ImportedSeries.id)
        .limit(page_size)
    )
    return list(result.scalars())


async def _iter_pending_import_series(
    session: AsyncSession,
    *,
    job_id: int,
    page_size: int,
    raise_if_cancelled: RaiseIfCancelledFunc,
) -> AsyncIterator[tuple[int, ImportedSeries, bool]]:
    after_id = 0
    item_index = 0
    while True:
        await raise_if_cancelled(session, job_id)
        items = await _load_pending_import_series_page(
            session,
            job_id=job_id,
            after_id=after_id,
            page_size=page_size,
        )
        if not items:
            break
        for page_index, item in enumerate(items):
            yield item_index, item, page_index == len(items) - 1
            item_index += 1
        after_id = items[-1].id


async def _load_pending_import_file_page(
    session: AsyncSession,
    *,
    import_series_id: int,
    after_id: int,
    page_size: int,
) -> list[ImportedFile]:
    result = await session.execute(
        sa_select(ImportedFile)
        .where(
            ImportedFile.import_series_id == import_series_id,
            ImportedFile.status == ImportedFileStatus.PENDING,
            ImportedFile.id > after_id,
        )
        .order_by(ImportedFile.id)
        .limit(page_size)
    )
    return list(result.scalars())


async def _load_scan_review_profile_page(
    session: AsyncSession,
    *,
    job_id: int,
    after_id: int,
    page_size: int,
) -> list[_ScanReviewProfile]:
    result = await session.execute(
        sa_select(
            ImportedSeries.id,
            ImportedSeries.files_total,
            ImportedSeries.file_count,
            ImportedSeries.cv_id,
            ImportedSeries.has_files,
            ImportedSeries.cv_issue_count,
        )
        .where(
            ImportedSeries.import_job_id == job_id,
            ImportedSeries.status == ImportSeriesStatus.PENDING,
            ImportedSeries.id > after_id,
        )
        .order_by(ImportedSeries.id)
        .limit(page_size)
    )
    return [
        _ScanReviewProfile(
            id=int(row.id),
            file_count=max(int(row.files_total or row.file_count or 0), 0),
            direct_match=bool(row.cv_id),
            has_files=bool(row.has_files),
            issue_count=row.cv_issue_count,
        )
        for row in result
    ]


async def _build_scan_review_progress_plan(
    session: AsyncSession,
    *,
    job_id: int,
    analysis_series_count: int,
    page_size: int,
    raise_if_cancelled: RaiseIfCancelledFunc,
) -> ScanReviewProgressPlan:
    series_match_weight = 0.0
    file_match_weight = 0.0
    after_id = 0
    while True:
        await raise_if_cancelled(session, job_id)
        profiles = await _load_scan_review_profile_page(
            session,
            job_id=job_id,
            after_id=after_id,
            page_size=page_size,
        )
        if not profiles:
            break
        for profile in profiles:
            series_match_weight += scan_review_series_match_weight(
                ScanReviewSeriesMatchProfile(
                    file_count=profile.file_count,
                    direct_match=profile.direct_match,
                )
            )
            if profile.has_files:
                file_match_weight += scan_review_file_match_weight(
                    ScanReviewFileMatchProfile(
                        file_count=profile.file_count,
                        issue_count=profile.issue_count,
                    )
                )
        after_id = profiles[-1].id

    analysis_weight = scan_review_analysis_weight(analysis_series_count)
    return ScanReviewProgressPlan(
        analysis_weights=(analysis_weight,) if analysis_weight else (),
        series_match_weights=(series_match_weight,) if series_match_weight else (),
        file_match_weights=(file_match_weight,) if file_match_weight else (),
    )


def _aggregate_matching_completed_weight(
    plan: ScanReviewProgressPlan,
    *,
    completed_items: int,
    total_items: int,
    current_item_progress_pct: int | None = None,
) -> float:
    current_fraction = max(min(int(current_item_progress_pct or 0), 100), 0) / 100
    completed_fraction = min(
        max((completed_items + current_fraction) / max(total_items, 1), 0.0),
        1.0,
    )
    return plan.analysis_weight + plan.series_match_weight * completed_fraction


async def _mark_pending_files_no_match(
    session: AsyncSession,
    item: ImportedSeries,
    *,
    job_id: int,
    page_size: int,
    raise_if_cancelled: RaiseIfCancelledFunc,
) -> None:
    after_id = 0
    while True:
        await raise_if_cancelled(session, job_id)
        pending_files = await _load_pending_import_file_page(
            session,
            import_series_id=item.id,
            after_id=after_id,
            page_size=page_size,
        )
        if not pending_files:
            break
        for imp_file in pending_files:
            diagnostics = dict(imp_file.diagnostics or {})
            diagnostics.setdefault("kind", "series_no_match_file")
            local_identity_missing = (
                dict(item.diagnostics or {}).get("reason") == "trusted_source_identity_missing"
            )
            diagnostics.setdefault(
                "rejection_reason",
                (
                    "No trusted local series identity was found. Match this series in Step 3."
                    if local_identity_missing
                    else "Series could not be matched to ComicVine during import review."
                ),
            )
            imp_file.status = ImportedFileStatus.NO_MATCH
            imp_file.include_in_import = False
            imp_file.matched_issue_id = None
            imp_file.matched_issue_cv_id = None
            imp_file.match_confidence = None
            imp_file.match_method = None
            imp_file.conflict_group_id = None
            imp_file.duplicate_group_id = None
            imp_file.duplicate_of_file_id = None
            imp_file.is_preferred = False
            imp_file.error_message = None
            imp_file.diagnostics = diagnostics
        after_id = pending_files[-1].id
        await session.flush()


async def run_import_series_matching(
    session: AsyncSession,
    job: ImportJob,
    *,
    metadata_provider: Any,
    source_metadata_for_series: SourceMetadataForSeriesFunc,
    load_deferred_source_metadata_for_series: DeferredSourceMetadataForSeriesFunc | None = None,
    evaluate_match: EvaluateMatchFunc,
    raise_if_cancelled: RaiseIfCancelledFunc,
    reclassify_duplicates: ReclassifyDuplicatesFunc,
    recompute_series_counters: RecomputeCountersFunc,
    log_event: LogEventFunc,
    emit_progress: EmitProgressFunc,
    phase_progress: PhaseProgressFunc,
    estimate_remaining_seconds: EstimateRemainingFunc,
    job_stats: JobStatsFunc,
    maybe_slow_item_delay: SlowItemDelayFunc,
    provider_free_filesystem: bool = False,
    estimate_remaining_work_seconds: EstimateRemainingWorkFunc | None = None,
    progress_callback: Callable[[ImportProgressEvent], Awaitable[None]] | None = None,
    item_page_size: int = 100,
    file_page_size: int = 250,
    profile_page_size: int = 500,
) -> None:
    """Run ComicVine matching for all pending imported series in a job."""
    started_at = time.monotonic()
    persist_batch_size = 25
    last_checkpoint_at = time.monotonic()
    item_page_size = max(item_page_size, 1)
    file_page_size = max(file_page_size, 1)
    profile_page_size = max(profile_page_size, 1)

    matched_count = 0
    no_match_count = 0
    deferred_count = 0
    deferred_titles: list[str] = []
    pending_detail_logs: list[dict[str, Any]] = []
    source_metadata_duration_ms = 0.0
    deferred_metadata_duration_ms = 0.0
    provider_evaluation_duration_ms = 0.0
    rebucket_duration_ms = 0.0
    rebucket_evaluated_files = 0
    deferred_metadata_loads = 0
    total_items = await _count_pending_import_series(session, job_id=job.id)
    processed_items = 0
    runtime_revision_state: dict[str, int] = {"value": int(job.progress_revision or 0)}
    progress_plan = (
        await _build_scan_review_progress_plan(
            session,
            job_id=job.id,
            analysis_series_count=max(job.series_found or total_items, total_items),
            page_size=profile_page_size,
            raise_if_cancelled=raise_if_cancelled,
        )
        if progress_callback is not None
        else None
    )

    async def emit_matching_progress(
        item: ImportedSeries,
        idx: int,
        *,
        message: str,
        current_item_stage: str = "matching",
        current_item_progress_pct: int | None = None,
        live_only: bool = False,
    ) -> None:
        if progress_callback is None:
            return
        assert progress_plan is not None

        completed_weight = _aggregate_matching_completed_weight(
            progress_plan,
            completed_items=idx,
            total_items=total_items,
            current_item_progress_pct=current_item_progress_pct,
        )
        progress = scan_review_progress_pct(
            progress_plan,
            completed_weight=completed_weight,
        )
        estimated_seconds_remaining = (
            estimate_remaining_work_seconds(
                job.scan_completed_at or job.match_started_at or job.scan_started_at,
                completed_units=completed_weight,
                total_units=progress_plan.total_weight,
            )
            if estimate_remaining_work_seconds is not None
            else estimate_remaining_seconds(
                job.scan_started_at,
                progress,
            )
        )
        event = ImportProgressEvent(
            job_id=job.id,
            status=ImportJobStatus.MATCHING,
            phase="matching",
            progress=progress,
            message=message,
            current_series=item.raw_series_name,
            current_series_status=item.status,
            estimated_seconds_remaining=estimated_seconds_remaining,
            **current_item_payload(
                kind="series",
                stage=current_item_stage,
                name=item.raw_series_name,
                progress_pct=(
                    current_item_progress_pct if current_item_progress_pct is not None else 0
                ),
            ),
            **job_stats(job),
        )
        if live_only:
            await emit_live_progress(
                job,
                event,
                progress_callback=progress_callback,
                revision_state=runtime_revision_state,
                started_at=job.scan_started_at,
            )
            return
        await emit_progress(session, job, event, progress_callback)
        runtime_revision_state["value"] = max(
            runtime_revision_state["value"],
            int(event.progress_revision or 0),
        )

    async def evaluate_match_with_progress(
        item: ImportedSeries,
        idx: int,
        **match_kwargs: Any,
    ) -> ComicVineMatchEvaluation:
        if progress_callback is None:
            return await evaluate_match(**match_kwargs)

        await emit_matching_progress(
            item,
            idx,
            message=(
                f"Matching {item.raw_series_name} against ComicVine "
                f"(series {idx + 1}/{max(total_items, 1)})..."
            ),
            current_item_stage="matching",
        )
        task: asyncio.Task[ComicVineMatchEvaluation] = asyncio.create_task(
            evaluate_match(**match_kwargs)
        )
        started_at = asyncio.get_running_loop().time()
        try:
            while not task.done():
                done, _pending = await asyncio.wait(
                    {task},
                    timeout=_MATCH_PROGRESS_HEARTBEAT_SECONDS,
                )
                if done:
                    break
                await raise_if_cancelled(session, job.id)
                elapsed_seconds = max(
                    round(asyncio.get_running_loop().time() - started_at),
                    1,
                )
                await emit_matching_progress(
                    item,
                    idx,
                    message=(
                        f"Still matching {item.raw_series_name} against ComicVine "
                        f"({elapsed_seconds}s elapsed)... "
                        "Large searches can take a few minutes."
                    ),
                    current_item_stage="matching",
                    current_item_progress_pct=_matching_item_heartbeat_progress(
                        elapsed_seconds,
                    ),
                    live_only=True,
                )
            return await task
        except (JobPausedError, JobCancelledError):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _rebucket_collection_volume_subtitle_series_with_progress(
        item: ImportedSeries,
        idx: int,
    ) -> VolumeSubtitleRebucketResult:
        if progress_callback is None:
            return await _rebucket_collection_volume_subtitle_series(
                session,
                job,
                item,
                metadata_provider=metadata_provider,
                evaluate_match=evaluate_match,
                match_threshold=job.cv_match_threshold,
                log_event=log_event,
            )

        await emit_matching_progress(
            item,
            idx,
            message=f"Checking volume subtitle matches for {item.raw_series_name}...",
            current_item_stage="rebucket",
            current_item_progress_pct=10,
            live_only=True,
        )
        task = asyncio.create_task(
            _rebucket_collection_volume_subtitle_series(
                session,
                job,
                item,
                metadata_provider=metadata_provider,
                evaluate_match=evaluate_match,
                match_threshold=job.cv_match_threshold,
                log_event=log_event,
            )
        )
        started_at = asyncio.get_running_loop().time()
        try:
            while not task.done():
                done, _pending = await asyncio.wait(
                    {task},
                    timeout=_MATCH_PROGRESS_HEARTBEAT_SECONDS,
                )
                if done:
                    break
                elapsed_seconds = max(
                    round(asyncio.get_running_loop().time() - started_at),
                    1,
                )
                await emit_matching_progress(
                    item,
                    idx,
                    message=(
                        f"Still checking volume subtitle matches for {item.raw_series_name} "
                        f"({elapsed_seconds}s elapsed)..."
                    ),
                    current_item_stage="rebucket",
                    current_item_progress_pct=_matching_item_heartbeat_progress(
                        elapsed_seconds,
                    ),
                    live_only=True,
                )
            return await task
        except (JobPausedError, JobCancelledError):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    async for idx, item, is_page_last in _iter_pending_import_series(
        session,
        job_id=job.id,
        page_size=item_page_size,
        raise_if_cancelled=raise_if_cancelled,
    ):
        await raise_if_cancelled(session, job.id)
        try:
            source_metadata_started_at = time.monotonic()
            series_source_metadata = await source_metadata_for_series(session, item)
            source_metadata_duration_ms += (time.monotonic() - source_metadata_started_at) * 1000
            evaluation_started_at = time.monotonic()
            if provider_free_filesystem:
                evaluation = _filesystem_source_identity_evaluation(
                    item,
                    series_source_metadata,
                    match_threshold=job.cv_match_threshold,
                )
            else:
                evaluation = await evaluate_match_with_progress(
                    item,
                    idx,
                    provider=metadata_provider,
                    raw_name=item.raw_series_name,
                    raw_year=item.raw_year,
                    source_metadata=series_source_metadata,
                    mylar3_cv_id=item.cv_id if item.cv_match_method == "mylar3_cv_id" else None,
                    comicinfo_cv_id=(
                        item.cv_id if item.cv_match_method == "comicinfo_cv_id" else None
                    ),
                    match_threshold=job.cv_match_threshold,
                )
            provider_evaluation_duration_ms += (time.monotonic() - evaluation_started_at) * 1000
            cv_result = evaluation.match
            if (
                cv_result is None
                and not provider_free_filesystem
                and load_deferred_source_metadata_for_series is not None
                and await _series_has_deferred_archive_metadata(session, item)
            ):
                deferred_metadata_loads += 1
                deferred_metadata_started_at = time.monotonic()
                series_source_metadata = await load_deferred_source_metadata_for_series(
                    session,
                    item,
                )
                deferred_metadata_duration_ms += (
                    time.monotonic() - deferred_metadata_started_at
                ) * 1000
                evaluation_started_at = time.monotonic()
                evaluation = await evaluate_match_with_progress(
                    item,
                    idx,
                    provider=metadata_provider,
                    raw_name=item.raw_series_name,
                    raw_year=item.raw_year,
                    source_metadata=series_source_metadata,
                    mylar3_cv_id=item.cv_id if item.cv_match_method == "mylar3_cv_id" else None,
                    comicinfo_cv_id=(
                        item.cv_id if item.cv_match_method == "comicinfo_cv_id" else None
                    ),
                    match_threshold=job.cv_match_threshold,
                )
                provider_evaluation_duration_ms += (time.monotonic() - evaluation_started_at) * 1000
                cv_result = evaluation.match
        except ImportProviderDegradedError as exc:
            deferred_count += 1
            if len(deferred_titles) < 10:
                deferred_titles.append(item.raw_series_name)
            clear_auto_cv_match_fields(item)
            item.status = ImportSeriesStatus.PENDING
            item.diagnostics = {
                "kind": "series_provider_error",
                "reason": "provider_degraded",
                "provider": exc.provider,
                "query": exc.query,
                "raw_year": exc.year,
                "attempts": exc.attempts,
                "last_error": exc.last_error,
            }
            pending_detail_logs.append(
                {
                    "level": "WARNING",
                    "event": "import_series_match_deferred_provider_error",
                    "message": (f"Deferred '{item.raw_series_name}' because ComicVine is degraded"),
                    "details": {
                        "raw_series_name": item.raw_series_name,
                        "raw_year": item.raw_year,
                        "provider": exc.provider,
                        "query": exc.query,
                        "attempts": exc.attempts,
                        "last_error": exc.last_error,
                    },
                }
            )
            evaluation = None
            cv_result = None

        if cv_result:
            item.cv_id = cv_result["cv_id"]
            item.cv_title = cv_result["cv_title"]
            item.cv_year = cv_result["cv_year"]
            item.cv_publisher = cv_result["cv_publisher"]
            item.cv_issue_count = cv_result["cv_issue_count"]
            item.cv_url = cv_result["cv_url"]
            item.cv_match_score = cv_result["cv_match_score"]
            item.cv_match_method = cv_result["cv_match_method"]
            item.status = ImportSeriesStatus.MATCHED
            item.diagnostics = evaluation.diagnostics if evaluation is not None else {}
            matched_count += 1
            rebucket_result = VolumeSubtitleRebucketResult()
            if not provider_free_filesystem:
                rebucket_started_at = time.monotonic()
                rebucket_result = await _rebucket_collection_volume_subtitle_series_with_progress(
                    item,
                    idx,
                )
                rebucket_duration_ms += (time.monotonic() - rebucket_started_at) * 1000
                rebucket_evaluated_files += rebucket_result.evaluated_file_count
                if rebucket_result.created_series_count:
                    matched_count += rebucket_result.created_series_count
                    if rebucket_result.removed_source_series:
                        matched_count -= 1

            if not rebucket_result.removed_source_series:
                pending_detail_logs.append(
                    {
                        "level": "DEBUG",
                        "event": "import_series_match_detail",
                        "message": f"Matched '{item.raw_series_name}' -> CV {cv_result['cv_id']}",
                        "details": {
                            "raw_series_name": item.raw_series_name,
                            "cv_id": cv_result["cv_id"],
                            "cv_title": cv_result["cv_title"],
                            "score": cv_result["cv_match_score"],
                            "method": cv_result["cv_match_method"],
                        },
                    }
                )
        elif evaluation is not None:
            item.status = ImportSeriesStatus.NO_MATCH
            item.diagnostics = evaluation.diagnostics
            clear_auto_cv_match_fields(item)
            await _mark_pending_files_no_match(
                session,
                item,
                job_id=job.id,
                page_size=file_page_size,
                raise_if_cancelled=raise_if_cancelled,
            )
            no_match_count += 1

            diagnostics = dict(item.diagnostics or {})
            candidate_conflict = diagnostics.get("kind") == "series_conflict"
            pending_detail_logs.append(
                {
                    "level": "DEBUG",
                    "event": (
                        "import_series_candidate_conflict_detail"
                        if candidate_conflict
                        else "import_series_no_match_detail"
                    ),
                    "message": (
                        f"Candidate conflict for '{item.raw_series_name}'"
                        if candidate_conflict
                        else f"No match for '{item.raw_series_name}'"
                    ),
                    "details": {
                        "raw_series_name": item.raw_series_name,
                        "raw_year": item.raw_year,
                        "threshold": job.cv_match_threshold,
                        "diagnostics": item.diagnostics,
                    },
                }
            )

        should_checkpoint = (
            idx == 0
            or (idx + 1) % persist_batch_size == 0
            or idx == total_items - 1
            or is_page_last
            or (time.monotonic() - last_checkpoint_at) >= 0.5
        )
        if should_checkpoint:
            await session.flush()
            await session.commit()
            last_checkpoint_at = time.monotonic()
            while pending_detail_logs:
                detail = pending_detail_logs.pop(0)
                await _log_detail_event_best_effort(
                    session,
                    job_id=job.id,
                    level=str(detail["level"]),
                    event=str(detail["event"]),
                    message=str(detail["message"]),
                    log_event=log_event,
                    **dict(detail["details"]),
                )

        if progress_callback and should_checkpoint:
            assert progress_plan is not None
            completed_weight = _aggregate_matching_completed_weight(
                progress_plan,
                completed_items=idx + 1,
                total_items=total_items,
            )
            progress = scan_review_progress_pct(
                progress_plan,
                completed_weight=completed_weight,
            )
            estimated_seconds_remaining = (
                estimate_remaining_work_seconds(
                    job.scan_completed_at or job.match_started_at or job.scan_started_at,
                    completed_units=completed_weight,
                    total_units=progress_plan.total_weight,
                )
                if estimate_remaining_work_seconds is not None
                else estimate_remaining_seconds(
                    job.scan_started_at,
                    progress,
                )
            )
            job.series_matched = matched_count
            job.series_no_match = no_match_count
            await emit_progress(
                session,
                job,
                ImportProgressEvent(
                    job_id=job.id,
                    status=ImportJobStatus.MATCHING,
                    phase="matching",
                    progress=progress,
                    message=f"Matching {idx + 1}/{total_items}...",
                    current_series=item.raw_series_name,
                    current_series_status=item.status,
                    estimated_seconds_remaining=estimated_seconds_remaining,
                    **current_item_payload(
                        kind="series",
                        stage="matching",
                        name=item.raw_series_name,
                        progress_pct=100,
                    ),
                    **job_stats(job),
                ),
                progress_callback,
            )
            await maybe_slow_item_delay()
        processed_items = idx + 1

    if deferred_count:
        job.series_matched = matched_count
        job.series_no_match = no_match_count
        job.error_message = (
            f"ComicVine timed out while matching {deferred_count} series. "
            "Import paused so pending matches can be retried."
        )
        await log_event(
            session,
            job.id,
            "WARNING",
            "import_matching_provider_degraded",
            message=job.error_message,
            deferred_count=deferred_count,
            deferred_titles=deferred_titles,
            duration_ms=round((time.monotonic() - started_at) * 1000),
            items_processed=processed_items,
            source_metadata_duration_ms=round(source_metadata_duration_ms),
            deferred_metadata_duration_ms=round(deferred_metadata_duration_ms),
            provider_evaluation_duration_ms=round(provider_evaluation_duration_ms),
            rebucket_duration_ms=round(rebucket_duration_ms),
            rebucket_evaluated_files=rebucket_evaluated_files,
            deferred_metadata_loads=deferred_metadata_loads,
            provider_cache_metrics=_provider_cache_metrics(metadata_provider),
        )
        await session.flush()
        await session.commit()
        raise JobPausedError(job.error_message)

    await reclassify_duplicates(session, job)

    job.error_message = None
    job.series_matched = matched_count
    job.series_no_match = no_match_count
    await recompute_series_counters(session, job)
    await session.flush()

    await log_event(
        session,
        job.id,
        "INFO",
        "import_matching_completed",
        message=f"Matching complete: {matched_count} matched, {no_match_count} no match",
        matched=matched_count,
        no_match=no_match_count,
        duration_ms=round((time.monotonic() - started_at) * 1000),
        items_processed=processed_items,
        source_metadata_duration_ms=round(source_metadata_duration_ms),
        deferred_metadata_duration_ms=round(deferred_metadata_duration_ms),
        provider_evaluation_duration_ms=round(provider_evaluation_duration_ms),
        rebucket_duration_ms=round(rebucket_duration_ms),
        rebucket_evaluated_files=rebucket_evaluated_files,
        deferred_metadata_loads=deferred_metadata_loads,
        provider_cache_metrics=_provider_cache_metrics(metadata_provider),
    )


async def _rebucket_collection_volume_subtitle_series(
    session: AsyncSession,
    job: ImportJob,
    item: ImportedSeries,
    *,
    metadata_provider: Any,
    evaluate_match: EvaluateMatchFunc,
    match_threshold: float,
    log_event: LogEventFunc,
) -> VolumeSubtitleRebucketResult:
    """Split collection files when ComicVine models each volume subtitle as its own series."""
    if item.cv_id is None:
        return VolumeSubtitleRebucketResult()
    if item.cv_match_method in _DIRECT_CV_ID_MATCH_METHODS:
        return VolumeSubtitleRebucketResult()

    with session.no_autoflush:
        files_result = await session.execute(
            sa_select(ImportedFile)
            .where(
                ImportedFile.import_series_id == item.id,
                ImportedFile.status == ImportedFileStatus.PENDING,
            )
            .order_by(ImportedFile.id.asc())
        )
    files = list(files_result.scalars().all())
    if not files:
        return VolumeSubtitleRebucketResult()

    rebucket_matches: list[_VolumeSubtitleRebucketMatch] = []
    evaluated_file_count = 0
    for imp_file in files:
        hint = volume_subtitle_hint_from_filename(imp_file.file_name)
        if hint is None:
            continue
        subtitle_candidate_name = _volume_subtitle_candidate_name(hint)
        if not subtitle_candidate_name:
            continue

        raw_year = imp_file.parsed_year or item.raw_year
        subtitle_metadata = SourceMetadata(
            original_title=imp_file.file_name,
            source_path=imp_file.file_path,
            series_name=subtitle_candidate_name,
            year=raw_year,
            issue_type=IssueType.VOLUME,
            signals={
                "series_name": MetadataSignal.RELEASE_TITLE,
                "issue_type": MetadataSignal.RELEASE_TITLE,
            },
            diagnostics={
                "volume_subtitle_hint": dict(hint),
                "rebucket_source_import_series_id": item.id,
                "rebucket_source_file_id": imp_file.id,
            },
        )
        evaluated_file_count += 1
        evaluation = await evaluate_match(
            provider=metadata_provider,
            raw_name=subtitle_candidate_name,
            raw_year=raw_year,
            source_metadata=subtitle_metadata,
            match_threshold=match_threshold,
        )
        cv_result = evaluation.match
        if cv_result is None:
            continue
        if not _volume_subtitle_match_is_safe(
            hint=hint,
            cv_result=cv_result,
            diagnostics=evaluation.diagnostics,
            source_cv_id=item.cv_id,
            match_threshold=match_threshold,
        ):
            continue
        rebucket_matches.append(
            _VolumeSubtitleRebucketMatch(
                imp_file=imp_file,
                hint=hint,
                raw_year=raw_year,
                cv_result=cv_result,
                diagnostics=dict(evaluation.diagnostics or {}),
            )
        )

    if not rebucket_matches:
        return VolumeSubtitleRebucketResult(evaluated_file_count=evaluated_file_count)

    split_series_by_cv_id: dict[int, ImportedSeries] = {}
    created_series_count = 0
    moved_file_paths: list[str] = []
    moved_file_ids: list[int] = []

    for rebucket_match in rebucket_matches:
        imp_file = rebucket_match.imp_file
        hint = rebucket_match.hint
        cv_result = rebucket_match.cv_result
        split_cv_id = int(cv_result["cv_id"])
        split_series = split_series_by_cv_id.get(split_cv_id)
        if split_series is None:
            split_series = ImportedSeries(
                import_job_id=job.id,
                status=ImportSeriesStatus.MATCHED,
                raw_series_name=cv_result["cv_title"],
                raw_year=cv_result["cv_year"] or rebucket_match.raw_year,
                raw_publisher=cv_result["cv_publisher"] or item.raw_publisher,
                file_count=0,
                files_total=0,
                sample_paths=[],
                source_folder=item.source_folder,
                has_files=True,
                cv_id=split_cv_id,
                cv_title=cv_result["cv_title"],
                cv_year=cv_result["cv_year"],
                cv_publisher=cv_result["cv_publisher"],
                cv_issue_count=cv_result["cv_issue_count"],
                cv_url=cv_result["cv_url"],
                cv_match_score=cv_result["cv_match_score"],
                cv_match_method="volume_subtitle_series_match",
                selected_for_import=False,
                diagnostics={
                    **rebucket_match.diagnostics,
                    "split_reason": "volume_subtitle_series_match",
                    "source_import_series_id": item.id,
                    "source_import_series_name": item.raw_series_name,
                    "source_target_cv_id": item.cv_id,
                    "source_target_cv_title": item.cv_title,
                    "volume_subtitle_hint": dict(hint),
                },
            )
            session.add(split_series)
            await session.flush()
            split_series_by_cv_id[split_cv_id] = split_series
            created_series_count += 1

        imp_file.import_series_id = split_series.id
        imp_file.include_in_import = False
        imp_file.conflict_group_id = None
        imp_file.duplicate_group_id = None
        imp_file.duplicate_of_file_id = None
        moved_file_paths.append(imp_file.file_path)
        moved_file_ids.append(imp_file.id)
        split_series.file_count = int(split_series.file_count or 0) + 1
        split_series.files_total = int(split_series.files_total or 0) + 1
        split_samples = list(split_series.sample_paths or [])
        if imp_file.file_path not in split_samples:
            split_samples.append(imp_file.file_path)
        split_series.sample_paths = split_samples
        file_diagnostics = dict(imp_file.diagnostics or {})
        file_diagnostics["rebucketed_to_import_series_id"] = split_series.id
        file_diagnostics["rebucketed_to_cv_id"] = split_cv_id
        file_diagnostics["rebucket_reason"] = "volume_subtitle_series_match"
        imp_file.diagnostics = file_diagnostics

        await log_event(
            session,
            job.id,
            "INFO",
            "import_file_rebucketed_to_volume_subtitle_series",
            message=(
                f"Rebucketed {imp_file.file_name} into its own import row for "
                f"{split_series.cv_title or split_series.raw_series_name}"
            ),
            file_name=imp_file.file_name,
            source_import_series_id=item.id,
            source_import_series_name=item.raw_series_name,
            target_import_series_id=split_series.id,
            target_series_cv_id=split_cv_id,
            volume_subtitle_hint=dict(hint),
        )

    if not moved_file_ids:
        return VolumeSubtitleRebucketResult()

    item.sample_paths = [
        path for path in list(item.sample_paths or []) if path not in set(moved_file_paths)
    ]
    remaining_count = max(int(item.files_total or item.file_count or 0) - len(moved_file_ids), 0)
    item.file_count = remaining_count
    item.files_total = remaining_count
    removed_source_series = remaining_count == 0
    if removed_source_series:
        await session.delete(item)
        await session.flush()

    return VolumeSubtitleRebucketResult(
        created_series_count=created_series_count,
        removed_source_series=removed_source_series,
        evaluated_file_count=evaluated_file_count,
    )


def _volume_subtitle_candidate_name(hint: VolumeSubtitleHint) -> str | None:
    base_series = str(hint.get("base_series") or "").strip()
    subtitle = str(hint.get("subtitle") or "").strip()
    if not base_series or not subtitle:
        return None
    return f"{base_series} {subtitle}".strip()


def _volume_subtitle_match_is_safe(
    *,
    hint: VolumeSubtitleHint,
    cv_result: dict[str, Any],
    diagnostics: dict[str, Any],
    source_cv_id: int,
    match_threshold: float,
) -> bool:
    try:
        cv_id = int(cv_result.get("cv_id") or 0)
        issue_count = int(cv_result.get("cv_issue_count") or 0)
        score = float(cv_result.get("cv_match_score") or 0.0)
    except (TypeError, ValueError):
        return False
    if cv_id == source_cv_id or issue_count != 1:
        return False
    if score < max(match_threshold, _VOLUME_SUBTITLE_SPLIT_THRESHOLD):
        return False
    title = str(cv_result.get("cv_title") or "")
    if not _title_contains_volume_hint(title, hint):
        return False
    selected_candidate = diagnostics.get("selected_candidate")
    if isinstance(selected_candidate, dict):
        match_type = str(selected_candidate.get("match_type") or "")
        if match_type and match_type not in {"exact", "alternate", "token_set", "starts_with"}:
            return False
    return True


def _title_contains_volume_hint(title: str, hint: VolumeSubtitleHint) -> bool:
    expected = _volume_subtitle_candidate_name(hint)
    if not expected:
        return False
    expected_tokens = {
        token
        for token in NameMatcher.normalize(expected).split()
        if token and token not in _TITLE_ARTICLES
    }
    title_tokens = {
        token
        for token in NameMatcher.normalize(title).split()
        if token and token not in _TITLE_ARTICLES
    }
    return bool(expected_tokens) and expected_tokens.issubset(title_tokens)


async def _series_has_deferred_archive_metadata(
    session: AsyncSession,
    item: ImportedSeries,
) -> bool:
    result = await session.execute(
        sa_select(ImportedFile.diagnostics).where(ImportedFile.import_series_id == item.id)
    )
    for diagnostics in result.scalars().all():
        source_metadata = (
            diagnostics.get("source_metadata") if isinstance(diagnostics, dict) else None
        )
        if isinstance(source_metadata, dict) and source_metadata.get("archive_metadata_deferred"):
            return True
    return False


def _provider_cache_metrics(metadata_provider: Any) -> dict[str, Any]:
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
