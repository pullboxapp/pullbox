"""File-level matching orchestration for import jobs."""

from __future__ import annotations

import json
import tempfile
import time
from contextlib import suppress
from inspect import isawaitable, iscoroutine
from typing import TYPE_CHECKING, Any, NamedTuple

from sqlalchemy import String
from sqlalchemy import and_ as sa_and
from sqlalchemy import case as sa_case
from sqlalchemy import cast as sa_cast
from sqlalchemy import exists as sa_exists
from sqlalchemy import func as sa_func
from sqlalchemy import literal as sa_literal
from sqlalchemy import or_ as sa_or
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import async_sessionmaker

from pullbox.core.exceptions import ImportProviderDegradedError, JobPausedError
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile
from pullbox.models.series import IssueCatalogState, Series
from pullbox.schemas.import_job import ImportProgressEvent
from pullbox.services.import_duplicates import DuplicateMergeProfile
from pullbox.services.import_file_conflicts import detect_cross_series_conflicts
from pullbox.services.import_file_issue_signals import candidate_issue_number
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
from pullbox.services.import_file_match_results import (
    FileMatchSeriesSummary,
    apply_file_match_series_summary,
)
from pullbox.services.import_file_match_targets import (
    FileMatchTargetIndex,
    load_file_match_target_index,
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
    ScanReviewProgressPlan,
    ScanReviewSeriesMatchProfile,
    current_item_payload,
    estimate_remaining_work_seconds,
    scan_review_analysis_weight,
    scan_review_file_match_weight,
    scan_review_file_target_weight,
    scan_review_progress_pct,
    scan_review_series_match_weight,
)
from pullbox.services.import_source_metadata import (
    import_file_has_deferred_archive_metadata,
    load_archive_entry_issue_hint_for_import_file,
)
from pullbox.services.import_workflow_state import (
    SCAN_PROGRESS_FILE_MATCH_END,
    SCAN_PROGRESS_FILE_MATCH_START,
    emit_live_progress,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.core.source_metadata import SourceMetadata
    from pullbox.providers.base import MetadataProvider
    from pullbox.services.import_file_match_candidates import FileMatchCandidate
    from pullbox.services.import_file_match_outcomes import (
        DuplicateTargetStateFunc as OutcomeDuplicateTargetStateFunc,
    )
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


_SERIES_PAGE_SIZE = 100
_FILE_PAGE_SIZE = 250
_PROFILE_PAGE_SIZE = 500
_TARGET_COHORT_PAGE_SIZE = 100
_SUMMARY_SAMPLE_LIMIT = 500
_MAX_IDENTITY_COHORT_SIZE = _FILE_PAGE_SIZE * 2
_MAX_TARGET_INDEX_ENTRIES = _FILE_PAGE_SIZE * 2


class _ImportFileMatchingCohortLimitError(RuntimeError):
    """Stop automatic matching before an unbounded identity cohort is retained."""


class _FileMatchingProfile(NamedTuple):
    id: int
    file_count: int
    issue_count: int | None
    completed_file_count: int
    target_completed: bool


class _FileMatchingPlan(NamedTuple):
    series_count: int
    total_file_phase_units: int
    completed_file_phase_units: int
    completed_file_match_weight: float
    completed_target_series_ids: frozenset[int]
    progress_plan: ScanReviewProgressPlan


class _FileTargetCohort(NamedTuple):
    kind: str
    value: str
    first_file_id: int
    file_count: int


class _CrossConflictCohort(NamedTuple):
    target_kind: str
    target_value: int
    issue_kind: str
    issue_value: str
    first_file_id: int
    file_count: int


def _eligible_import_series_filter() -> Any:
    return sa_or(
        sa_and(
            ImportedSeries.status == ImportSeriesStatus.DUPLICATE,
            ImportedSeries.series_id.is_not(None),
        ),
        sa_and(
            ImportedSeries.status == ImportSeriesStatus.MATCHED,
            sa_or(
                ImportedSeries.series_id.is_not(None),
                ImportedSeries.cv_id.is_not(None),
            ),
        ),
    )


async def _load_eligible_import_series_page(
    session: AsyncSession,
    *,
    job_id: int,
    after_id: int,
    page_size: int,
    series_ids: list[int] | None,
) -> list[ImportedSeries]:
    query = sa_select(ImportedSeries).where(
        ImportedSeries.import_job_id == job_id,
        _eligible_import_series_filter(),
        ImportedSeries.id > after_id,
    )
    if series_ids:
        query = query.where(ImportedSeries.id.in_(series_ids))
    result = await session.execute(query.order_by(ImportedSeries.id).limit(max(page_size, 1)))
    return list(result.scalars())


async def _iter_eligible_import_series(
    session: AsyncSession,
    *,
    job_id: int,
    series_ids: list[int] | None,
    raise_if_cancelled: RaiseIfCancelledFunc,
    page_size: int,
) -> AsyncIterator[tuple[int, ImportedSeries, bool]]:
    after_id = 0
    series_index = 0
    while True:
        await raise_if_cancelled(session, job_id)
        page = await _load_eligible_import_series_page(
            session,
            job_id=job_id,
            after_id=after_id,
            page_size=page_size,
            series_ids=series_ids,
        )
        if not page:
            break
        for page_index, item in enumerate(page):
            yield series_index, item, page_index == len(page) - 1
            series_index += 1
        after_id = page[-1].id


async def _load_file_matching_profile_page(
    session: AsyncSession,
    *,
    job_id: int,
    after_id: int,
    page_size: int,
    series_ids: list[int] | None,
) -> list[_FileMatchingProfile]:
    query = sa_select(
        ImportedSeries.id,
        ImportedSeries.files_total,
        ImportedSeries.file_count,
        ImportedSeries.cv_issue_count,
    ).where(
        ImportedSeries.import_job_id == job_id,
        _eligible_import_series_filter(),
        ImportedSeries.id > after_id,
    )
    if series_ids:
        query = query.where(ImportedSeries.id.in_(series_ids))
    result = await session.execute(query.order_by(ImportedSeries.id).limit(max(page_size, 1)))
    series_rows = result.all()
    if not series_rows:
        return []

    page_series_ids = [int(row.id) for row in series_rows]
    status_result = await session.execute(
        sa_select(
            ImportedFile.import_series_id,
            ImportedFile.status,
            sa_func.count(ImportedFile.id),
        )
        .where(
            ImportedFile.import_job_id == job_id,
            ImportedFile.import_series_id.in_(page_series_ids),
        )
        .group_by(ImportedFile.import_series_id, ImportedFile.status)
    )
    status_counts: dict[int, dict[ImportedFileStatus, int]] = {}
    for import_series_id, status, count in status_result:
        status_counts.setdefault(int(import_series_id), {})[status] = int(count)

    matching_resolved_statuses = {
        ImportedFileStatus.MATCHED,
        ImportedFileStatus.DUPLICATE_FILE,
        ImportedFileStatus.ALREADY_OWNED,
        ImportedFileStatus.CONFLICT,
        ImportedFileStatus.NO_MATCH,
        ImportedFileStatus.CONFIRMED,
        ImportedFileStatus.IMPORTED,
    }
    return [
        _FileMatchingProfile(
            id=int(row.id),
            file_count=(
                sum(status_counts.get(int(row.id), {}).values())
                or max(int(row.files_total or row.file_count or 0), 0)
            ),
            issue_count=row.cv_issue_count,
            completed_file_count=sum(
                count
                for status, count in status_counts.get(int(row.id), {}).items()
                if status not in {ImportedFileStatus.PENDING, ImportedFileStatus.SAFETY_APPROVED}
            ),
            target_completed=(
                not any(
                    status_counts.get(int(row.id), {}).get(status, 0)
                    for status in {
                        ImportedFileStatus.PENDING,
                        ImportedFileStatus.SAFETY_APPROVED,
                    }
                )
                or any(
                    status_counts.get(int(row.id), {}).get(status, 0)
                    for status in matching_resolved_statuses
                )
            ),
        )
        for row in series_rows
    ]


async def _build_file_matching_plan(
    session: AsyncSession,
    job: ImportJob,
    *,
    series_ids: list[int] | None,
    raise_if_cancelled: RaiseIfCancelledFunc,
    page_size: int,
) -> _FileMatchingPlan:
    series_count = 0
    total_file_phase_units = 0
    completed_file_phase_units = 0
    file_match_weight = 0.0
    completed_file_match_weight = 0.0
    completed_target_series_ids: set[int] = set()
    after_id = 0
    while True:
        await raise_if_cancelled(session, job.id)
        profiles = await _load_file_matching_profile_page(
            session,
            job_id=job.id,
            after_id=after_id,
            page_size=page_size,
            series_ids=series_ids,
        )
        if not profiles:
            break
        for profile in profiles:
            series_count += 1
            total_file_phase_units += 1 + profile.file_count
            file_profile = ScanReviewFileMatchProfile(
                file_count=profile.file_count,
                issue_count=profile.issue_count,
            )
            target_weight = scan_review_file_target_weight(file_profile)
            aggregate_weight = scan_review_file_match_weight(file_profile)
            file_match_weight += aggregate_weight
            completed_file_phase_units += profile.completed_file_count
            completed_file_match_weight += profile.completed_file_count * (
                (aggregate_weight - target_weight) / max(profile.file_count, 1)
            )
            if profile.target_completed:
                completed_file_phase_units += 1
                completed_file_match_weight += target_weight
                completed_target_series_ids.add(profile.id)
        after_id = profiles[-1].id

    series_match_profile_count = max(
        int(job.series_matched or 0) + int(job.series_no_match or 0),
        series_count,
    )
    analysis_series_count = max(
        int(job.series_found or 0),
        series_match_profile_count,
    )
    analysis_weight = scan_review_analysis_weight(analysis_series_count)
    series_match_weight = series_match_profile_count * scan_review_series_match_weight(
        ScanReviewSeriesMatchProfile(direct_match=False)
    )
    return _FileMatchingPlan(
        series_count=series_count,
        total_file_phase_units=total_file_phase_units,
        completed_file_phase_units=completed_file_phase_units,
        completed_file_match_weight=completed_file_match_weight,
        completed_target_series_ids=frozenset(completed_target_series_ids),
        progress_plan=ScanReviewProgressPlan(
            analysis_weights=(analysis_weight,) if analysis_weight else (),
            series_match_weights=(series_match_weight,) if series_match_weight else (),
            file_match_weights=(file_match_weight,) if file_match_weight else (),
        ),
    )


async def _load_eligible_import_file_page(
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
            ImportedFile.status.in_(
                [ImportedFileStatus.PENDING, ImportedFileStatus.SAFETY_APPROVED]
            ),
            ImportedFile.id > after_id,
        )
        .order_by(ImportedFile.id)
        .limit(max(page_size, 1))
    )
    return list(result.scalars())


async def _load_import_series_file_page(
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
            ImportedFile.id > after_id,
        )
        .order_by(ImportedFile.id)
        .limit(max(page_size, 1))
    )
    return list(result.scalars())


_FILE_TARGET_COHORT_KINDS = ("issue_id", "issue_cv_id", "parsed_issue")


def _file_target_identity_spec(kind: str) -> tuple[Any, Any]:
    if kind == "issue_id":
        return ImportedFile.matched_issue_id, ImportedFile.matched_issue_id.is_not(None)
    if kind == "issue_cv_id":
        return (
            ImportedFile.matched_issue_cv_id,
            sa_and(
                ImportedFile.matched_issue_id.is_(None),
                ImportedFile.matched_issue_cv_id.is_not(None),
            ),
        )
    if kind == "parsed_issue":
        return (
            ImportedFile.parsed_issue_number,
            sa_and(
                ImportedFile.matched_issue_id.is_(None),
                ImportedFile.matched_issue_cv_id.is_(None),
                ImportedFile.parsed_issue_number.is_not(None),
            ),
        )
    raise ValueError(f"Unsupported file target cohort kind: {kind}")


def _file_target_cohort_query(
    *,
    job_id: int,
    import_series_id: int,
    kind: str,
    after_value: str | None,
    page_size: int | None,
) -> Any:
    identity_column, identity_filter = _file_target_identity_spec(kind)
    cursor_filter = None
    if after_value is not None:
        cursor_value: int | float = (
            int(after_value) if kind != "parsed_issue" else float(after_value)
        )
        cursor_filter = identity_column > cursor_value
    query = (
        sa_select(
            sa_literal(kind).label("kind"),
            sa_cast(identity_column, String).label("value"),
            sa_func.min(ImportedFile.id).label("first_file_id"),
            sa_func.count(ImportedFile.id).label("file_count"),
        )
        .where(
            ImportedFile.import_job_id == job_id,
            ImportedFile.import_series_id == import_series_id,
            identity_filter,
            cursor_filter if cursor_filter is not None else sa_literal(True),
        )
        .group_by(identity_column)
        .order_by(identity_column)
    )
    return query.limit(max(page_size, 1)) if page_size is not None else query


async def _load_file_target_cohort_page(
    session: AsyncSession,
    *,
    job_id: int,
    import_series_id: int,
    kind: str,
    after_value: str | None,
    page_size: int,
) -> list[_FileTargetCohort]:
    result = await session.execute(
        _file_target_cohort_query(
            job_id=job_id,
            import_series_id=import_series_id,
            kind=kind,
            after_value=after_value,
            page_size=page_size,
        )
    )
    return [
        _FileTargetCohort(
            kind=str(row.kind),
            value=str(row.value),
            first_file_id=int(row.first_file_id),
            file_count=int(row.file_count),
        )
        for row in result
    ]


async def _spool_grouped_cohort_rows(
    session: AsyncSession,
    *,
    job_id: int,
    query: Any,
    batch_size: int,
    scan_name: str,
    row_payload: Callable[[Any], list[str | int]],
    raise_if_cancelled: RaiseIfCancelledFunc,
) -> Any:
    """Execute one grouped scan and spool bounded row batches before writer commits."""
    size = max(batch_size, 1)
    spool = tempfile.SpooledTemporaryFile(  # noqa: SIM115 - ownership passes to iterator
        max_size=1 << 20,
        mode="w+t",
        encoding="utf-8",
        newline="\n",
    )

    def write_rows(rows: Any) -> None:
        for row in rows:
            spool.write(json.dumps(row_payload(row), separators=(",", ":")))
            spool.write("\n")

    try:
        await session.flush()
        await session.commit()
        await raise_if_cancelled(session, job_id)
        bind = session.bind
        bind_url = str(bind.sync_engine.url) if bind is not None else ""
        execution_options = {"pullbox_cohort_scan": scan_name}
        if bind is None or ":memory:" in bind_url:
            result = await session.execute(query.execution_options(**execution_options))
            for rows in result.partitions(size):
                await raise_if_cancelled(session, job_id)
                write_rows(rows)
        else:
            reader_factory = async_sessionmaker(bind=bind, expire_on_commit=False)
            async with reader_factory() as reader_session:
                stream_result = await reader_session.stream(
                    query.execution_options(
                        yield_per=size,
                        **execution_options,
                    )
                )
                try:
                    while True:
                        rows = await stream_result.fetchmany(size)
                        if not rows:
                            break
                        await raise_if_cancelled(session, job_id)
                        write_rows(rows)
                finally:
                    await stream_result.close()
        await raise_if_cancelled(session, job_id)
        spool.seek(0)
        return spool
    except BaseException:
        spool.close()
        raise


async def _iter_file_target_cohort_batches(
    session: AsyncSession,
    *,
    job_id: int,
    import_series_id: int,
    kind: str,
    batch_size: int,
    raise_if_cancelled: RaiseIfCancelledFunc,
) -> AsyncIterator[list[_FileTargetCohort]]:
    """Yield bounded cohort descriptors from one grouped database execution."""
    spool = await _spool_grouped_cohort_rows(
        session,
        job_id=job_id,
        query=_file_target_cohort_query(
            job_id=job_id,
            import_series_id=import_series_id,
            kind=kind,
            after_value=None,
            page_size=None,
        ),
        batch_size=batch_size,
        scan_name="file_target",
        row_payload=lambda row: [
            str(row.kind),
            str(row.value),
            int(row.first_file_id),
            int(row.file_count),
        ],
        raise_if_cancelled=raise_if_cancelled,
    )
    try:
        while True:
            await raise_if_cancelled(session, job_id)
            batch: list[_FileTargetCohort] = []
            while len(batch) < max(batch_size, 1):
                line = spool.readline()
                if not line:
                    break
                kind_value, value, first_file_id, file_count = json.loads(line)
                batch.append(
                    _FileTargetCohort(
                        kind=str(kind_value),
                        value=str(value),
                        first_file_id=int(first_file_id),
                        file_count=int(file_count),
                    )
                )
            if not batch:
                break
            yield batch
    finally:
        spool.close()


async def _load_file_target_cohort(
    session: AsyncSession,
    *,
    job_id: int,
    import_series_id: int,
    cohort: _FileTargetCohort,
) -> list[ImportedFile]:
    if cohort.file_count > _MAX_IDENTITY_COHORT_SIZE:
        raise _ImportFileMatchingCohortLimitError(
            "Import file identity cohort exceeds the bounded automatic-matching limit "
            f"({_MAX_IDENTITY_COHORT_SIZE} files; kind={cohort.kind})."
        )
    if cohort.kind == "issue_id":
        identity_filter = ImportedFile.matched_issue_id == int(cohort.value)
    elif cohort.kind == "issue_cv_id":
        identity_filter = sa_and(
            ImportedFile.matched_issue_id.is_(None),
            ImportedFile.matched_issue_cv_id == int(cohort.value),
        )
    else:
        identity_filter = sa_and(
            ImportedFile.matched_issue_id.is_(None),
            ImportedFile.matched_issue_cv_id.is_(None),
            ImportedFile.parsed_issue_number == float(cohort.value),
        )
    result = await session.execute(
        sa_select(ImportedFile)
        .where(
            ImportedFile.import_job_id == job_id,
            ImportedFile.import_series_id == import_series_id,
            identity_filter,
        )
        .order_by(ImportedFile.id)
        .limit(_MAX_IDENTITY_COHORT_SIZE)
    )
    return list(result.scalars())


async def _finalize_import_series_file_groups(
    session: AsyncSession,
    job: ImportJob,
    imp_series: ImportedSeries,
    *,
    duplicate_group_counter: int,
    conflict_group_counter: int,
    detect_duplicate_copies: DetectDuplicateCopiesFunc,
    detect_conflicts: DetectConflictsFunc,
    log_event: LogEventFunc,
    raise_if_cancelled: RaiseIfCancelledFunc,
) -> tuple[int, int]:
    for kind in _FILE_TARGET_COHORT_KINDS:
        async for cohorts in _iter_file_target_cohort_batches(
            session,
            job_id=job.id,
            import_series_id=imp_series.id,
            kind=kind,
            batch_size=_TARGET_COHORT_PAGE_SIZE,
            raise_if_cancelled=raise_if_cancelled,
        ):
            for cohort in cohorts:
                await raise_if_cancelled(session, job.id)
                files = await _load_file_target_cohort(
                    session,
                    job_id=job.id,
                    import_series_id=imp_series.id,
                    cohort=cohort,
                )
                if not files:
                    continue
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
                await session.flush()
                await session.commit()
    return duplicate_group_counter, conflict_group_counter


async def _summarize_import_series_files(
    session: AsyncSession,
    imp_series: ImportedSeries,
    *,
    duplicate_series: bool,
    duplicate_merge_profile: DuplicateMergeProfile | None,
    cv_match_threshold: float,
    raise_if_cancelled: RaiseIfCancelledFunc,
    job_id: int,
) -> FileMatchSeriesSummary:
    status_counts: dict[ImportedFileStatus, int] = {}
    sample: list[ImportedFile] = []
    representative_by_status: dict[ImportedFileStatus, ImportedFile] = {}
    special_representatives: dict[tuple[str, bool, bool], ImportedFile] = {}
    source_layout_review_count = 0
    source_layout_review_names: list[str] = []
    metadata_conflict_count = 0
    trusted_identity_conflict_count = 0
    trusted_identity_conflicts: list[dict[str, object]] = []
    trusted_identity_conflict_files: list[dict[str, object]] = []
    after_id = 0
    total_files = 0
    while True:
        await raise_if_cancelled(session, job_id)
        page = await _load_import_series_file_page(
            session,
            import_series_id=imp_series.id,
            after_id=after_id,
            page_size=_FILE_PAGE_SIZE,
        )
        if not page:
            break
        for imp_file in page:
            total_files += 1
            status_counts[imp_file.status] = status_counts.get(imp_file.status, 0) + 1
            representative_by_status.setdefault(imp_file.status, imp_file)
            diagnostics = dict(imp_file.diagnostics or {})
            kind = str(diagnostics.get("kind") or "")
            conflict_type = str(diagnostics.get("conflict_type") or "")
            if kind in {"metadata_conflict", "source_layout_review"}:
                preserve_series_match = bool(diagnostics.get("preserve_series_match"))
                special_representatives.setdefault(
                    (
                        kind,
                        conflict_type == "trusted_source_identity_conflict",
                        preserve_series_match,
                    ),
                    imp_file,
                )
            if kind == "source_layout_review":
                source_layout_review_count += 1
                if len(source_layout_review_names) < _SUMMARY_SAMPLE_LIMIT:
                    source_layout_review_names.append(imp_file.file_name)
            if kind == "metadata_conflict":
                metadata_conflict_count += 1
            if kind == "metadata_conflict" and conflict_type == (
                "trusted_source_identity_conflict"
            ):
                trusted_identity_conflict_count += 1
                if len(trusted_identity_conflict_files) < _SUMMARY_SAMPLE_LIMIT:
                    trusted_identity_conflict_files.append(
                        {
                            "file_name": imp_file.file_name,
                            "rejection_reason": diagnostics.get("rejection_reason"),
                        }
                    )
                raw_conflicts = diagnostics.get("identity_conflicts")
                if isinstance(raw_conflicts, list):
                    for conflict_item in raw_conflicts:
                        if (
                            isinstance(conflict_item, dict)
                            and conflict_item not in trusted_identity_conflicts
                            and len(trusted_identity_conflicts) < _SUMMARY_SAMPLE_LIMIT
                        ):
                            trusted_identity_conflicts.append(dict(conflict_item))
            if len(sample) < _SUMMARY_SAMPLE_LIMIT:
                sample.append(imp_file)
        after_id = page[-1].id

    if total_files > len(sample):
        required = [*representative_by_status.values(), *special_representatives.values()]
        required_by_id = {item.id: item for item in required}
        sample = [
            *required_by_id.values(),
            *(item for item in sample if item.id not in required_by_id),
        ][:_SUMMARY_SAMPLE_LIMIT]

    if sample:
        summary = apply_file_match_series_summary(
            imp_series,
            sample,
            duplicate_series=duplicate_series,
            duplicate_merge_profile=duplicate_merge_profile,
            cv_match_threshold=cv_match_threshold,
        )
    else:
        summary = FileMatchSeriesSummary(0, 0, 0, 0, 0, 0)

    matched = status_counts.get(ImportedFileStatus.MATCHED, 0) + status_counts.get(
        ImportedFileStatus.CONFIRMED,
        0,
    )
    duplicate = status_counts.get(ImportedFileStatus.DUPLICATE_FILE, 0)
    already_owned = status_counts.get(ImportedFileStatus.ALREADY_OWNED, 0)
    no_match = status_counts.get(ImportedFileStatus.NO_MATCH, 0)
    conflict = status_counts.get(ImportedFileStatus.CONFLICT, 0)
    imp_series.files_total = total_files
    imp_series.files_matched = matched
    imp_series.files_duplicate = duplicate
    imp_series.files_already_owned = already_owned
    imp_series.files_no_match = no_match
    imp_series.files_conflict = conflict
    if duplicate_series:
        diagnostics = dict(imp_series.diagnostics or {})
        actionable = bool(
            (duplicate_merge_profile.actionable if duplicate_merge_profile else False)
            or matched
            or conflict
        )
        diagnostics.update(
            {
                "actionable_duplicate_merge": actionable,
                "has_importable_files": matched > 0,
                "importable_files": matched,
                "duplicate_files": duplicate,
                "already_owned_files": already_owned,
                "no_match_files": no_match,
                "conflict_files": conflict,
            }
        )
        imp_series.diagnostics = diagnostics
    invalidation_diagnostics = (
        dict(summary.invalidation_diagnostics)
        if summary.invalidation_diagnostics is not None
        else None
    )
    if invalidation_diagnostics is not None:
        reason = invalidation_diagnostics.get("reason")
        if reason == "selected_layout_no_match":
            invalidation_diagnostics.update(
                {
                    "source_layout_review_files": source_layout_review_count,
                    "unmatched_files": source_layout_review_names,
                    "unmatched_files_truncated": (
                        source_layout_review_count > len(source_layout_review_names)
                    ),
                }
            )
        elif reason == "trusted_source_identity_conflict":
            invalidation_diagnostics.update(
                {
                    "identity_conflict_files": trusted_identity_conflict_count,
                    "identity_conflicts": trusted_identity_conflicts,
                    "conflicting_files": trusted_identity_conflict_files,
                    "conflicting_files_truncated": (
                        trusted_identity_conflict_count > len(trusted_identity_conflict_files)
                    ),
                }
            )
        elif reason == "file_metadata_conflict":
            invalidation_diagnostics["metadata_conflict_files"] = metadata_conflict_count
        imp_series.diagnostics = invalidation_diagnostics
    return FileMatchSeriesSummary(
        found=total_files,
        matched=matched,
        duplicate=duplicate,
        already_owned=already_owned,
        no_match=no_match,
        conflict=conflict,
        series_invalidated=summary.series_invalidated,
        invalidation_diagnostics=invalidation_diagnostics,
    )


async def _count_import_series_files(
    session: AsyncSession,
    *,
    import_series_id: int,
    eligible_only: bool,
) -> int:
    query = sa_select(sa_func.count(ImportedFile.id)).where(
        ImportedFile.import_series_id == import_series_id
    )
    if eligible_only:
        query = query.where(
            ImportedFile.status.in_(
                [ImportedFileStatus.PENDING, ImportedFileStatus.SAFETY_APPROVED]
            )
        )
    count = await session.scalar(query)
    return int(count or 0)


def _stable_series_file_count(
    imp_series: ImportedSeries,
    *,
    persisted_file_count: int,
) -> int:
    """Retain the pre-split series size across cancellation and resume."""
    raw_pre_split_count = (imp_series.diagnostics or {}).get("pre_split_file_count")
    try:
        pre_split_count = int(raw_pre_split_count or 0)
    except (TypeError, ValueError):
        pre_split_count = 0
    return max(
        persisted_file_count,
        int(imp_series.files_total or 0),
        pre_split_count,
    )


async def _load_existing_series_page_target_index(
    session: AsyncSession,
    imp_series: ImportedSeries,
    files: list[ImportedFile],
) -> FileMatchTargetIndex:
    """Load only canonical issues addressable by one bounded imported-file page."""
    target_index = FileMatchTargetIndex()
    if imp_series.series_id is None:
        return target_index

    target_index.existing_series = await session.get(Series, imp_series.series_id)
    requested_cv_ids = {
        int(imp_file.comicvine_issue_id)
        for imp_file in files
        if imp_file.comicvine_issue_id is not None
    }
    requested_issue_numbers = {
        float(issue_number)
        for imp_file in files
        if imp_file.comicvine_issue_id is None
        and (issue_number := candidate_issue_number(imp_file)) is not None
    }
    identity_filters: list[Any] = []
    if requested_cv_ids:
        identity_filters.append(Issue.comicvine_id.in_(sorted(requested_cv_ids)))
    if requested_issue_numbers:
        identity_filters.append(Issue.issue_number.in_(sorted(requested_issue_numbers)))
    if not identity_filters:
        return target_index

    has_library_file = sa_exists(sa_select(LibraryFile.id).where(LibraryFile.issue_id == Issue.id))
    result = await session.execute(
        sa_select(Issue, has_library_file.label("has_library_file"))
        .where(
            Issue.series_id == imp_series.series_id,
            sa_or(*identity_filters),
        )
        .order_by(Issue.id)
        .limit(_MAX_TARGET_INDEX_ENTRIES + 1)
    )
    target_rows = result.all()
    if len(target_rows) > _MAX_TARGET_INDEX_ENTRIES:
        raise _ImportFileMatchingCohortLimitError(
            "Issue target index exceeds the bounded automatic-matching limit "
            f"({_MAX_TARGET_INDEX_ENTRIES} entries)."
        )

    ambiguous_issue_numbers: set[float] = set()
    for issue, owned in target_rows:
        entry = (issue.id, issue.comicvine_id, bool(owned), issue, issue.title)
        if issue.comicvine_id is not None:
            target_index.cv_id_map[issue.comicvine_id] = entry
        target_index.exact_number_map[issue.effective_issue_number_text] = entry
        if issue.issue_number in ambiguous_issue_numbers:
            continue
        if issue.issue_number in target_index.number_map:
            target_index.number_map.pop(issue.issue_number)
            ambiguous_issue_numbers.add(issue.issue_number)
            continue
        target_index.number_map[issue.issue_number] = entry
    return target_index


def _ensure_bounded_target_index(target_index: FileMatchTargetIndex) -> None:
    unique_target_keys: set[tuple[int | None, int | None, float | None]] = {
        (
            entry[0],
            entry[1],
            issue_number if entry[0] is None and entry[1] is None else None,
        )
        for issue_number, entry in target_index.number_map.items()
    }
    unique_target_keys.update(
        (entry[0], entry[1], None) for entry in target_index.cv_id_map.values()
    )
    unique_target_keys.update(
        (entry[0], entry[1], None) for entry in target_index.exact_number_map.values()
    )
    if len(unique_target_keys) > _MAX_TARGET_INDEX_ENTRIES:
        raise _ImportFileMatchingCohortLimitError(
            "Issue target index exceeds the bounded automatic-matching limit "
            f"({_MAX_TARGET_INDEX_ENTRIES} entries)."
        )


async def _load_file_page_target_index(
    session: AsyncSession,
    job: ImportJob,
    imp_series: ImportedSeries,
    files: list[ImportedFile],
    *,
    series_file_count: int,
    duplicate_series: bool,
    metadata_provider: MetadataProvider | None,
    series_idx: int,
    total_series: int,
    completed_units: int,
    progress_callback: Callable[[ImportProgressEvent], Awaitable[None]] | None,
    emit_file_matching_progress: Callable[..., Awaitable[None]],
    raise_if_cancelled: RaiseIfCancelledFunc,
) -> FileMatchTargetIndex:
    if imp_series.series_id is not None:
        if progress_callback is not None:
            await emit_file_matching_progress(
                imp_series,
                completed_units,
                message=(
                    f"Loading issue targets for {imp_series.raw_series_name} "
                    f"(series {series_idx + 1}/{total_series})..."
                ),
                current_item_stage="file_matching",
                current_item_progress_pct=5,
                current_work_unit_progress_pct=0,
            )
        target_index = await _load_existing_series_page_target_index(
            session,
            imp_series,
            files,
        )
    else:
        target_index = await load_file_match_target_index_with_progress(
            session=session,
            job_id=job.id,
            item=imp_series,
            files_to_match=files,
            series_file_count=series_file_count,
            duplicate_series=duplicate_series,
            metadata_provider=metadata_provider,
            series_idx=series_idx,
            total_series=total_series,
            completed_units=completed_units,
            progress_callback=progress_callback,
            emit_file_matching_progress=emit_file_matching_progress,
            raise_if_cancelled=raise_if_cancelled,
        )
    _ensure_bounded_target_index(target_index)
    return target_index


async def _reload_file_page_target_index_after_deferred_identity(
    session: AsyncSession,
    imp_series: ImportedSeries,
    files: list[ImportedFile],
    *,
    series_file_count: int,
    duplicate_series: bool,
    metadata_provider: MetadataProvider | None,
) -> FileMatchTargetIndex:
    """Reload a bounded page index after ComicInfo reveals a new exact issue ID."""
    if imp_series.series_id is not None:
        target_index = await _load_existing_series_page_target_index(
            session,
            imp_series,
            files,
        )
    else:
        target_index = await load_file_match_target_index(
            session,
            imp_series,
            duplicate_series=duplicate_series,
            metadata_provider=metadata_provider,
            files=files,
            series_file_count=series_file_count,
        )
    _ensure_bounded_target_index(target_index)
    return target_index


async def _build_bounded_duplicate_merge_profile(
    session: AsyncSession,
    imp_series: ImportedSeries,
    *,
    incoming_file_count: int,
    build_duplicate_merge_profile: BuildDuplicateMergeProfileFunc,
) -> DuplicateMergeProfile | None:
    """Build the exact duplicate summary without retaining every canonical issue."""
    if imp_series.series_id is None:
        return None
    existing_series = await session.get(Series, imp_series.series_id)
    owned_issue = sa_case((LibraryFile.id.is_not(None), Issue.id), else_=None)
    result = await session.execute(
        sa_select(
            sa_func.count(sa_func.distinct(Issue.id)),
            sa_func.count(sa_func.distinct(owned_issue)),
        )
        .select_from(Issue)
        .outerjoin(LibraryFile, LibraryFile.issue_id == Issue.id)
        .where(Issue.series_id == imp_series.series_id)
    )
    existing_issue_count, owned_issue_count = result.one()
    existing_count = int(existing_issue_count or 0)
    owned_count = int(owned_issue_count or 0)

    if existing_count <= 1:
        issue_entries: list[tuple[Issue, bool]] = []
        if existing_count:
            single_result = await session.execute(
                sa_select(
                    Issue,
                    sa_exists(
                        sa_select(LibraryFile.id).where(LibraryFile.issue_id == Issue.id)
                    ).label("has_library_file"),
                )
                .where(Issue.series_id == imp_series.series_id)
                .limit(1)
            )
            issue, owned = single_result.one()
            issue_entries.append((issue, bool(owned)))
        return build_duplicate_merge_profile(
            existing_series,
            issue_entries,
            incoming_file_count=incoming_file_count,
        )

    catalog_state = (
        existing_series.issue_catalog_state
        if existing_series is not None
        else IssueCatalogState.COMPLETE
    )
    if catalog_state is None:
        catalog_state = IssueCatalogState.COMPLETE
    elif not isinstance(catalog_state, IssueCatalogState):
        catalog_state = IssueCatalogState(str(catalog_state).lower())
    expected_issue_count = int(existing_series.issue_count or 0) if existing_series else 0
    catalog_proves_full_ownership = not (
        catalog_state != IssueCatalogState.COMPLETE and expected_issue_count > 0
    ) and (expected_issue_count <= 0 or existing_count >= expected_issue_count)
    fully_owned = bool(
        existing_count > 0 and owned_count == existing_count and catalog_proves_full_ownership
    )
    return DuplicateMergeProfile(
        actionable=owned_count < existing_count,
        fully_owned=fully_owned,
        existing_issue_count=existing_count,
        owned_issue_count=owned_count,
    )


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
    rebuild_cross_conflicts: bool = True,
) -> None:
    """Match individual imported files to issue targets for review."""
    started_at = time.monotonic()
    last_checkpoint_at = time.monotonic()
    matching_plan = await _build_file_matching_plan(
        session,
        job,
        series_ids=series_ids,
        raise_if_cancelled=raise_if_cancelled,
        page_size=_PROFILE_PAGE_SIZE,
    )

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
    counter_result = await session.execute(
        sa_select(
            sa_func.max(ImportedFile.conflict_group_id),
            sa_func.max(ImportedFile.duplicate_group_id),
        ).where(ImportedFile.import_job_id == job.id)
    )
    max_conflict_group_id, max_duplicate_group_id = counter_result.one()
    conflict_group_counter = int(max_conflict_group_id or 0)
    duplicate_group_counter = int(max_duplicate_group_id or 0)
    total_file_phase_units = matching_plan.total_file_phase_units
    progress_plan = matching_plan.progress_plan
    completed_file_phase_units = matching_plan.completed_file_phase_units
    completed_file_match_weight = matching_plan.completed_file_match_weight
    active_target_weight = 0.0
    file_match_unit_weight = scan_review_file_match_weight(
        ScanReviewFileMatchProfile(file_count=1, issue_count=0)
    ) - scan_review_file_target_weight(ScanReviewFileMatchProfile(file_count=1, issue_count=0))
    total_series = max(matching_plan.series_count, 1)
    runtime_revision_state: dict[str, int] = {"value": int(job.progress_revision or 0)}

    emit_weighted_file_matching_progress = build_file_matching_progress_emitter(
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

    async def emit_file_matching_progress(
        item: ImportedSeries,
        _completed_units: int,
        **kwargs: Any,
    ) -> None:
        current_progress = int(kwargs.get("current_work_unit_progress_pct") or 0)
        effective_file_weight = completed_file_match_weight + (
            active_target_weight * max(min(current_progress, 100), 0) / 100
        )
        overall_file_progress = round(
            (effective_file_weight / max(progress_plan.file_match_weight, 1.0)) * 100
        )
        kwargs["current_work_unit_progress_pct"] = overall_file_progress
        await emit_weighted_file_matching_progress(item, 0, **kwargs)

    async def process_file_page(
        imp_series: ImportedSeries,
        files: list[ImportedFile],
        *,
        target_index: FileMatchTargetIndex,
        duplicate_series: bool,
        duplicate_merge_profile: Any,
        series_high_confidence: bool,
        file_index_offset: int,
        total_file_count: int,
    ) -> tuple[list[ImportedFile], list[int]]:
        nonlocal completed_file_phase_units
        nonlocal completed_file_match_weight
        nonlocal deferred_metadata_duration_ms
        nonlocal deferred_metadata_loads
        nonlocal file_evaluation_duration_ms
        nonlocal files_processed

        if not target_index.has_targets and can_mark_missing_issue_targets(
            imp_series,
            metadata_provider,
        ):
            mark_files_missing_provider_targets(imp_series, files)

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
        if not files:
            return files, created_split_series_ids

        await emit_file_matching_progress(
            imp_series,
            completed_file_phase_units,
            message=(
                f"Matching files to issues for {imp_series.raw_series_name} "
                f"({total_file_count} file{'s' if total_file_count != 1 else ''})..."
            ),
            current_item_progress_pct=50,
            current_work_unit_progress_pct=0,
        )
        for page_file_index, imp_file in enumerate(files, start=1):
            if page_file_index > 1 and page_file_index % 25 == 0:
                await raise_if_cancelled(session, job.id)
            files_processed += 1
            reset_file_match_state(imp_file)
            file_metadata = source_metadata_for_import_file(imp_series, imp_file)
            if (
                imp_series.cv_match_method == "mylar3_cv_id"
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
                _persist_deferred_source_evidence(imp_file, file_metadata)
                if (
                    imp_file.comicvine_issue_id is not None
                    and imp_file.comicvine_issue_id not in target_index.cv_id_map
                ):
                    target_index = await _reload_file_page_target_index_after_deferred_identity(
                        session,
                        imp_series,
                        files,
                        series_file_count=stable_series_file_count,
                        duplicate_series=duplicate_series,
                        metadata_provider=metadata_provider,
                    )
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
                _persist_deferred_source_evidence(imp_file, file_metadata)
                if (
                    imp_file.comicvine_issue_id is not None
                    and imp_file.comicvine_issue_id not in target_index.cv_id_map
                ):
                    target_index = await _reload_file_page_target_index_after_deferred_identity(
                        session,
                        imp_series,
                        files,
                        series_file_count=stable_series_file_count,
                        duplicate_series=duplicate_series,
                        metadata_provider=metadata_provider,
                    )
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
            completed_file_match_weight += file_match_unit_weight
            file_index = file_index_offset + page_file_index
            await emit_file_matching_progress(
                imp_series,
                completed_file_phase_units,
                message=f"Matched file {file_index}/{total_file_count} for "
                f"{imp_series.raw_series_name}",
                current_item_stage="file_matching",
                current_item_progress_pct=(
                    50 + round((file_index / max(total_file_count, 1)) * 50)
                ),
                current_work_unit_progress_pct=0,
                live_only=True,
            )

        return files, created_split_series_ids

    async def process_split_series_batch(batch_ids: list[int]) -> None:
        nonlocal conflict_group_counter
        nonlocal duplicate_group_counter

        if not batch_ids:
            return
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
            load_deferred_source_metadata_for_import_file=(
                load_deferred_source_metadata_for_import_file
            ),
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
            series_ids=batch_ids,
            progress_callback=None,
            rebuild_cross_conflicts=False,
        )
        counters = await session.execute(
            sa_select(
                sa_func.max(ImportedFile.conflict_group_id),
                sa_func.max(ImportedFile.duplicate_group_id),
            ).where(ImportedFile.import_job_id == job.id)
        )
        max_conflict_group_id, max_duplicate_group_id = counters.one()
        conflict_group_counter = max(
            conflict_group_counter,
            int(max_conflict_group_id or 0),
        )
        duplicate_group_counter = max(
            duplicate_group_counter,
            int(max_duplicate_group_id or 0),
        )

    async for series_idx, imp_series, series_page_end in _iter_eligible_import_series(
        session,
        job_id=job.id,
        series_ids=series_ids,
        raise_if_cancelled=raise_if_cancelled,
        page_size=_SERIES_PAGE_SIZE,
    ):
        await raise_if_cancelled(session, job.id)
        duplicate_series = is_duplicate_series(imp_series)

        eligible_file_count = await _count_import_series_files(
            session,
            import_series_id=imp_series.id,
            eligible_only=True,
        )
        total_series_file_count = await _count_import_series_files(
            session,
            import_series_id=imp_series.id,
            eligible_only=False,
        )
        stable_series_file_count = _stable_series_file_count(
            imp_series,
            persisted_file_count=total_series_file_count,
        )
        target_progress_completed = imp_series.id in matching_plan.completed_target_series_ids
        active_target_weight = (
            0.0
            if target_progress_completed
            else scan_review_file_target_weight(
                ScanReviewFileMatchProfile(
                    file_count=eligible_file_count,
                    issue_count=imp_series.cv_issue_count,
                )
            )
        )
        target_load_failed = False
        series_high_confidence = (
            imp_series.cv_match_score is not None and imp_series.cv_match_score >= 0.90
        )
        duplicate_merge_profile = (
            await _build_bounded_duplicate_merge_profile(
                session,
                imp_series,
                incoming_file_count=total_series_file_count,
                build_duplicate_merge_profile=build_duplicate_merge_profile,
            )
            if duplicate_series
            else None
        )
        original_series_status = imp_series.status
        original_series_diagnostics = dict(imp_series.diagnostics or {})
        retained_file_count = 0
        processed_file_count = 0
        series_had_processed_files = False
        file_after_id = 0
        while eligible_file_count:
            await raise_if_cancelled(session, job.id)
            file_page = await _load_eligible_import_file_page(
                session,
                import_series_id=imp_series.id,
                after_id=file_after_id,
                page_size=_FILE_PAGE_SIZE,
            )
            if not file_page:
                break
            page_last_id = file_page[-1].id
            # End the page-read transaction before any provider/archive-backed target work.
            await session.commit()
            target_index_started_at = time.monotonic()
            try:
                target_index = await _load_file_page_target_index(
                    session,
                    job,
                    imp_series,
                    file_page,
                    series_file_count=stable_series_file_count,
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
                if (
                    imp_series.series_id is None
                    and not target_index.has_targets
                    and not can_mark_missing_issue_targets(imp_series, metadata_provider)
                ):
                    target_load_failed = True
                    break
            except ImportProviderDegradedError as exc:
                active_target_weight = 0.0
                target_index_duration_ms += (time.monotonic() - target_index_started_at) * 1000
                deferred_count += 1
                if len(deferred_titles) < 10:
                    deferred_titles.append(imp_series.raw_series_name)
                await defer_file_matching_for_provider_error(
                    session=session,
                    job_id=job.id,
                    imp_series=imp_series,
                    exc=exc,
                    log_event=log_event,
                )
                target_load_failed = True
                break
            except _ImportFileMatchingCohortLimitError:
                raise
            except Exception:
                active_target_weight = 0.0
                if imp_series.cv_id is not None:
                    log_warning(
                        "file_matching_cv_fetch_failed",
                        series=imp_series.raw_series_name,
                        cv_id=imp_series.cv_id,
                    )
                    target_load_failed = True
                    break
                raise

            if not target_progress_completed:
                completed_file_phase_units += 1
                completed_file_match_weight += active_target_weight
                active_target_weight = 0.0
                target_progress_completed = True
            # File metadata/archive evaluation starts outside the target-read transaction.
            await session.commit()
            matched_page, created_split_series_ids = await process_file_page(
                imp_series,
                file_page,
                target_index=target_index,
                duplicate_series=duplicate_series,
                duplicate_merge_profile=duplicate_merge_profile,
                series_high_confidence=series_high_confidence,
                file_index_offset=processed_file_count,
                total_file_count=eligible_file_count,
            )
            retained_file_count += len(matched_page)
            processed_file_count += len(file_page)
            series_had_processed_files = series_had_processed_files or bool(matched_page)
            split_series_ids.update(created_split_series_ids)
            while len(split_series_ids) >= _SERIES_PAGE_SIZE:
                split_batch = sorted(split_series_ids)[:_SERIES_PAGE_SIZE]
                split_series_ids.difference_update(split_batch)
                await process_split_series_batch(split_batch)
            file_after_id = page_last_id
            await session.flush()
            await session.commit()

        if series_had_processed_files:
            series_processed += 1

        if target_load_failed:
            await session.flush()
            await session.commit()
            continue

        if retained_file_count and imp_series.status == ImportSeriesStatus.SKIPPED:
            imp_series.status = original_series_status
            imp_series.diagnostics = original_series_diagnostics

        duplicate_group_counter, conflict_group_counter = await _finalize_import_series_file_groups(
            session,
            job,
            imp_series,
            duplicate_group_counter=duplicate_group_counter,
            conflict_group_counter=conflict_group_counter,
            detect_duplicate_copies=detect_duplicate_copies,
            detect_conflicts=detect_conflicts,
            log_event=log_event,
            raise_if_cancelled=raise_if_cancelled,
        )
        series_summary = await _summarize_import_series_files(
            session,
            imp_series,
            duplicate_series=duplicate_series,
            duplicate_merge_profile=duplicate_merge_profile,
            cv_match_threshold=job.cv_match_threshold,
            raise_if_cancelled=raise_if_cancelled,
            job_id=job.id,
        )
        if not series_summary.found:
            continue
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
            or series_page_end
            or series_idx == matching_plan.series_count - 1
            or (time.monotonic() - last_checkpoint_at) >= 0.5
        )
        if should_checkpoint:
            await session.flush()
            await session.commit()
            last_checkpoint_at = time.monotonic()

        if progress_callback and should_checkpoint:
            completed_weight = (
                progress_plan.analysis_weight
                + progress_plan.series_match_weight
                + completed_file_match_weight
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
        await process_split_series_batch(sorted(split_series_ids))

    if rebuild_cross_conflicts:
        conflict_group_counter = await _rebuild_cross_series_conflicts(
            session,
            job,
            conflict_group_counter=conflict_group_counter,
            is_duplicate_series=is_duplicate_series,
            log_event=log_event,
            raise_if_cancelled=raise_if_cancelled,
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
    return import_file_has_deferred_archive_metadata(imp_file)


def _persist_deferred_source_evidence(
    imp_file: ImportedFile,
    metadata: SourceMetadata,
) -> None:
    """Persist local archive evidence without replacing Mylar row authority."""
    diagnostics = dict(imp_file.diagnostics or {})
    diagnostics["source_metadata"] = dict(metadata.diagnostics)
    diagnostics["metadata_signals"] = {
        key: signal.value for key, signal in metadata.signals.items()
    }
    diagnostics["source_issue_type"] = metadata.issue_type.value
    if metadata.comicvine_series_id is not None:
        diagnostics["comicvine_series_id"] = metadata.comicvine_series_id
    imp_file.diagnostics = diagnostics
    imp_file.has_comicinfo = bool(metadata.diagnostics.get("has_comicinfo"))
    if imp_file.comicvine_issue_id is None and metadata.comicvine_issue_id is not None:
        imp_file.comicvine_issue_id = metadata.comicvine_issue_id


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


def _is_cross_series_conflict(imp_file: ImportedFile) -> bool:
    diagnostics = dict(imp_file.diagnostics or {})
    return (
        imp_file.status == ImportedFileStatus.CONFLICT
        and diagnostics.get("kind") == "file_conflict"
        and diagnostics.get("scope") == "cross_series"
    )


async def _apply_cross_conflict_counter_deltas(
    session: AsyncSession,
    counter_deltas: dict[int, tuple[int, int]],
    *,
    is_duplicate_series: IsDuplicateSeriesFunc,
) -> dict[int, str]:
    if not counter_deltas:
        return {}
    if len(counter_deltas) > _MAX_IDENTITY_COHORT_SIZE:
        raise _ImportFileMatchingCohortLimitError(
            "Cross-series summary cohort exceeds the bounded automatic-matching limit."
        )
    result = await session.execute(
        sa_select(ImportedSeries)
        .where(ImportedSeries.id.in_(sorted(counter_deltas)))
        .order_by(ImportedSeries.id)
    )
    series_labels: dict[int, str] = {}
    for imp_series in result.scalars():
        series_labels[imp_series.id] = imp_series.raw_series_name
        matched_delta, conflict_delta = counter_deltas.get(imp_series.id, (0, 0))
        imp_series.files_matched = max(int(imp_series.files_matched or 0) + matched_delta, 0)
        imp_series.files_conflict = max(
            int(imp_series.files_conflict or 0) + conflict_delta,
            0,
        )
        if is_duplicate_series(imp_series):
            diagnostics = dict(imp_series.diagnostics or {})
            actionable = bool(
                diagnostics.get("actionable_duplicate_merge")
                or imp_series.files_matched
                or imp_series.files_conflict
            )
            diagnostics.update(
                {
                    "actionable_duplicate_merge": actionable,
                    "has_importable_files": imp_series.files_matched > 0,
                    "importable_files": imp_series.files_matched,
                    "conflict_files": imp_series.files_conflict,
                }
            )
            imp_series.diagnostics = diagnostics
    return series_labels


async def _rebuild_cross_series_conflicts(
    session: AsyncSession,
    job: ImportJob,
    *,
    conflict_group_counter: int,
    is_duplicate_series: IsDuplicateSeriesFunc,
    log_event: LogEventFunc,
    raise_if_cancelled: RaiseIfCancelledFunc,
) -> int:
    reset_after_id = 0
    while True:
        await raise_if_cancelled(session, job.id)
        reset_result = await session.execute(
            sa_select(ImportedFile)
            .join(ImportedSeries, ImportedSeries.id == ImportedFile.import_series_id)
            .where(
                ImportedFile.import_job_id == job.id,
                ImportedFile.status == ImportedFileStatus.CONFLICT,
                ImportedSeries.status.in_(
                    [ImportSeriesStatus.MATCHED, ImportSeriesStatus.DUPLICATE]
                ),
                ImportedFile.id > reset_after_id,
            )
            .order_by(ImportedFile.id)
            .limit(_FILE_PAGE_SIZE)
        )
        reset_page = list(reset_result.scalars())
        if not reset_page:
            break
        reset_counter_deltas: dict[int, tuple[int, int]] = {}
        for imp_file in reset_page:
            if not _is_cross_series_conflict(imp_file):
                continue
            imp_file.status = ImportedFileStatus.MATCHED
            imp_file.conflict_group_id = None
            imp_file.is_preferred = False
            imp_file.include_in_import = False
            previous_diagnostics = dict(imp_file.diagnostics or {}).get("previous_diagnostics")
            imp_file.diagnostics = (
                dict(previous_diagnostics) if isinstance(previous_diagnostics, dict) else {}
            )
            matched_delta, conflict_delta = reset_counter_deltas.get(
                imp_file.import_series_id,
                (0, 0),
            )
            reset_counter_deltas[imp_file.import_series_id] = (
                matched_delta + 1,
                conflict_delta - 1,
            )
        reset_after_id = reset_page[-1].id
        await _apply_cross_conflict_counter_deltas(
            session,
            reset_counter_deltas,
            is_duplicate_series=is_duplicate_series,
        )
        await session.flush()
        await session.commit()

    for target_kind in _CROSS_TARGET_COHORT_KINDS:
        for issue_kind in _FILE_TARGET_COHORT_KINDS:
            async for cohorts in _iter_cross_conflict_cohort_batches(
                session,
                job_id=job.id,
                target_kind=target_kind,
                issue_kind=issue_kind,
                batch_size=_TARGET_COHORT_PAGE_SIZE,
                raise_if_cancelled=raise_if_cancelled,
            ):
                for cohort in cohorts:
                    await raise_if_cancelled(session, job.id)
                    files = await _load_cross_conflict_cohort(
                        session,
                        job_id=job.id,
                        cohort=cohort,
                    )
                    if not files:
                        continue
                    target_key = (cohort.target_kind, cohort.target_value)
                    target_series_key_by_file_id = {
                        imp_file.id: target_key for imp_file in files if imp_file.id is not None
                    }
                    (
                        _cross_conflict_count,
                        conflict_group_counter,
                        cross_conflict_groups,
                    ) = detect_cross_series_conflicts(
                        files,
                        conflict_group_counter,
                        target_series_key_by_file_id=target_series_key_by_file_id,
                    )
                    cohort_counter_deltas: dict[int, tuple[int, int]] = {}
                    group_series_ids_by_group: list[tuple[dict[str, Any], set[int]]] = []
                    for group_details in cross_conflict_groups:
                        group_file_ids = {
                            file_info["file_id"] for file_info in group_details.get("files", [])
                        }
                        group_series_ids = {
                            imp_file.import_series_id
                            for imp_file in files
                            if imp_file.id in group_file_ids
                        }
                        group_series_ids_by_group.append((group_details, group_series_ids))
                        for imp_file in files:
                            if imp_file.id not in group_file_ids:
                                continue
                            matched_delta, conflict_delta = cohort_counter_deltas.get(
                                imp_file.import_series_id,
                                (0, 0),
                            )
                            cohort_counter_deltas[imp_file.import_series_id] = (
                                matched_delta - 1,
                                conflict_delta + 1,
                            )
                    series_labels = await _apply_cross_conflict_counter_deltas(
                        session,
                        cohort_counter_deltas,
                        is_duplicate_series=is_duplicate_series,
                    )
                    for group_details, group_series_ids in group_series_ids_by_group:
                        group_series_labels = sorted(
                            series_labels[series_id]
                            for series_id in group_series_ids
                            if series_id in series_labels
                        )
                        await log_event(
                            session,
                            job.id,
                            "DEBUG",
                            "import_file_conflict_detail",
                            message=(
                                f"Cross-series conflict group "
                                f"{group_details['conflict_group_id']} across "
                                f"{', '.join(group_series_labels)}"
                            ),
                            series=", ".join(group_series_labels),
                            diagnostics=group_details,
                        )
                    await session.flush()
                    await session.commit()

    return conflict_group_counter


_CROSS_TARGET_COHORT_KINDS = ("series", "cv")


def _cross_target_identity_spec(kind: str) -> tuple[Any, Any]:
    if kind == "series":
        return ImportedSeries.series_id, ImportedSeries.series_id.is_not(None)
    if kind == "cv":
        return (
            ImportedSeries.cv_id,
            sa_and(
                ImportedSeries.series_id.is_(None),
                ImportedSeries.cv_id.is_not(None),
            ),
        )
    raise ValueError(f"Unsupported cross-series target cohort kind: {kind}")


def _cross_conflict_cohort_query(
    *,
    job_id: int,
    target_kind: str,
    issue_kind: str,
    after_target_value: int | None,
    after_issue_value: str | None,
    page_size: int | None,
) -> Any:
    target_column, target_filter = _cross_target_identity_spec(target_kind)
    issue_column, issue_filter = _file_target_identity_spec(issue_kind)
    cursor_filter = None
    if after_target_value is not None:
        if after_issue_value is None:
            raise ValueError("Cross-series cohort cursor requires an issue identity")
        issue_cursor: int | float = (
            int(after_issue_value) if issue_kind != "parsed_issue" else float(after_issue_value)
        )
        cursor_filter = sa_or(
            target_column > after_target_value,
            sa_and(
                target_column == after_target_value,
                issue_column > issue_cursor,
            ),
        )
    query = (
        sa_select(
            sa_literal(target_kind).label("target_kind"),
            target_column.label("target_value"),
            sa_literal(issue_kind).label("issue_kind"),
            sa_cast(issue_column, String).label("issue_value"),
            sa_func.min(ImportedFile.id).label("first_file_id"),
            sa_func.count(ImportedFile.id).label("file_count"),
        )
        .select_from(ImportedFile)
        .join(ImportedSeries, ImportedSeries.id == ImportedFile.import_series_id)
        .where(
            ImportedFile.import_job_id == job_id,
            ImportedFile.status.in_([ImportedFileStatus.MATCHED, ImportedFileStatus.CONFLICT]),
            ImportedSeries.status.in_([ImportSeriesStatus.MATCHED, ImportSeriesStatus.DUPLICATE]),
            target_filter,
            issue_filter,
            cursor_filter if cursor_filter is not None else sa_literal(True),
        )
        .group_by(target_column, issue_column)
        .having(sa_func.count(sa_func.distinct(ImportedFile.import_series_id)) > 1)
        .order_by(target_column, issue_column)
    )
    return query.limit(max(page_size, 1)) if page_size is not None else query


async def _load_cross_conflict_cohort_page(
    session: AsyncSession,
    *,
    job_id: int,
    target_kind: str,
    issue_kind: str,
    after_target_value: int | None,
    after_issue_value: str | None,
    page_size: int,
) -> list[_CrossConflictCohort]:
    result = await session.execute(
        _cross_conflict_cohort_query(
            job_id=job_id,
            target_kind=target_kind,
            issue_kind=issue_kind,
            after_target_value=after_target_value,
            after_issue_value=after_issue_value,
            page_size=page_size,
        )
    )
    return [
        _CrossConflictCohort(
            target_kind=str(row.target_kind),
            target_value=int(row.target_value),
            issue_kind=str(row.issue_kind),
            issue_value=str(row.issue_value),
            first_file_id=int(row.first_file_id),
            file_count=int(row.file_count),
        )
        for row in result
    ]


async def _iter_cross_conflict_cohort_batches(
    session: AsyncSession,
    *,
    job_id: int,
    target_kind: str,
    issue_kind: str,
    batch_size: int,
    raise_if_cancelled: RaiseIfCancelledFunc,
) -> AsyncIterator[list[_CrossConflictCohort]]:
    """Yield bounded cross-conflict descriptors from one grouped execution."""
    spool = await _spool_grouped_cohort_rows(
        session,
        job_id=job_id,
        query=_cross_conflict_cohort_query(
            job_id=job_id,
            target_kind=target_kind,
            issue_kind=issue_kind,
            after_target_value=None,
            after_issue_value=None,
            page_size=None,
        ),
        batch_size=batch_size,
        scan_name="cross_conflict",
        row_payload=lambda row: [
            str(row.target_kind),
            int(row.target_value),
            str(row.issue_kind),
            str(row.issue_value),
            int(row.first_file_id),
            int(row.file_count),
        ],
        raise_if_cancelled=raise_if_cancelled,
    )
    try:
        while True:
            await raise_if_cancelled(session, job_id)
            batch: list[_CrossConflictCohort] = []
            while len(batch) < max(batch_size, 1):
                line = spool.readline()
                if not line:
                    break
                (
                    target_kind_value,
                    target_value,
                    issue_kind_value,
                    issue_value,
                    first_file_id,
                    file_count,
                ) = json.loads(line)
                batch.append(
                    _CrossConflictCohort(
                        target_kind=str(target_kind_value),
                        target_value=int(target_value),
                        issue_kind=str(issue_kind_value),
                        issue_value=str(issue_value),
                        first_file_id=int(first_file_id),
                        file_count=int(file_count),
                    )
                )
            if not batch:
                break
            yield batch
    finally:
        spool.close()


async def _load_cross_conflict_cohort(
    session: AsyncSession,
    *,
    job_id: int,
    cohort: _CrossConflictCohort,
) -> list[ImportedFile]:
    if cohort.file_count > _MAX_IDENTITY_COHORT_SIZE:
        raise _ImportFileMatchingCohortLimitError(
            "Cross-series issue cohort exceeds the bounded automatic-matching limit "
            f"({_MAX_IDENTITY_COHORT_SIZE} files; issue kind={cohort.issue_kind})."
        )
    if cohort.target_kind == "series":
        target_filter = ImportedSeries.series_id == cohort.target_value
    else:
        target_filter = sa_and(
            ImportedSeries.series_id.is_(None),
            ImportedSeries.cv_id == cohort.target_value,
        )
    if cohort.issue_kind == "issue_id":
        issue_filter = ImportedFile.matched_issue_id == int(cohort.issue_value)
    elif cohort.issue_kind == "issue_cv_id":
        issue_filter = sa_and(
            ImportedFile.matched_issue_id.is_(None),
            ImportedFile.matched_issue_cv_id == int(cohort.issue_value),
        )
    else:
        issue_filter = sa_and(
            ImportedFile.matched_issue_id.is_(None),
            ImportedFile.matched_issue_cv_id.is_(None),
            ImportedFile.parsed_issue_number == float(cohort.issue_value),
        )
    result = await session.execute(
        sa_select(ImportedFile)
        .join(ImportedSeries, ImportedSeries.id == ImportedFile.import_series_id)
        .where(
            ImportedFile.import_job_id == job_id,
            ImportedFile.status.in_([ImportedFileStatus.MATCHED, ImportedFileStatus.CONFLICT]),
            ImportedSeries.status.in_([ImportSeriesStatus.MATCHED, ImportSeriesStatus.DUPLICATE]),
            target_filter,
            issue_filter,
        )
        .order_by(ImportedFile.id)
        .limit(_MAX_IDENTITY_COHORT_SIZE)
    )
    return list(result.scalars())
