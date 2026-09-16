"""Series-level duplicate detection for import workflows."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, NamedTuple

from sqlalchemy import ColumnElement
from sqlalchemy import func as sa_func
from sqlalchemy import or_ as sa_or
from sqlalchemy import select as sa_select

from pullbox.core.issue_numbers import (
    issue_number_text_matches_numeric,
    normalize_issue_number_text,
)
from pullbox.core.name_matcher import NameMatcher
from pullbox.core.type_semantics import series_types_compatible
from pullbox.models.import_job import (
    ImportedFile,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.models.issue import Issue
from pullbox.models.publisher import Publisher
from pullbox.models.series import Series, SeriesType
from pullbox.schemas.import_job import ImportProgressEvent
from pullbox.services.import_matching import (
    is_same_series,
    series_type_from_import_diagnostics,
)
from pullbox.services.import_progress_runtime import (
    ScanReviewFileMatchProfile,
    ScanReviewProgressPlan,
    ScanReviewSeriesMatchProfile,
    estimate_remaining_work_seconds,
    scan_review_analysis_weight,
    scan_review_file_match_weight,
    scan_review_progress_pct,
    scan_review_series_match_weight,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import date, datetime

    from sqlalchemy.engine import RowMapping
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import Select

    ProgressCallback = Callable[[ImportProgressEvent], Awaitable[None]]
    RaiseIfCancelledFunc = Callable[[AsyncSession, int], Awaitable[None]]
    LogEventFunc = Callable[..., Awaitable[None]]
    EmitProgressFunc = Callable[
        [AsyncSession, ImportJob, ImportProgressEvent, ProgressCallback],
        Awaitable[None],
    ]
    PhaseProgressFunc = Callable[[int, int, int, int], int]
    EstimateRemainingFunc = Callable[[datetime | None, int], int | None]
    JobStatsFunc = Callable[[ImportJob], dict[str, int]]


class _ExistingSeriesCandidate(NamedTuple):
    id: int
    comicvine_id: int | None
    title: str
    year_start: int | None
    series_type: SeriesType | None
    publisher_name: str | None
    issue_count: int | None
    comicvine_url: str | None


class _ScanReviewProfileRow(NamedTuple):
    id: int
    file_count: int
    direct_match: bool
    has_files: bool
    issue_count: int | None


class _ImportedIssueTargetRow(NamedTuple):
    id: int
    issue_number: float
    issue_number_text: str | None
    parsed_year: int | None


async def deduplicate_import_series(
    session: AsyncSession,
    job: ImportJob,
    *,
    raise_if_cancelled: RaiseIfCancelledFunc,
    log_event: LogEventFunc,
    emit_progress: EmitProgressFunc,
    phase_progress: PhaseProgressFunc,
    estimate_remaining_seconds: EstimateRemainingFunc,
    job_stats: JobStatsFunc,
    progress_callback: ProgressCallback | None = None,
    item_page_size: int = 100,
    candidate_page_size: int = 250,
    profile_page_size: int = 500,
    issue_target_page_size: int = 250,
) -> None:
    """Tag imported series rows as duplicate when they already exist in the library."""
    started_at = time.monotonic()
    item_page_size = max(item_page_size, 1)
    candidate_page_size = max(candidate_page_size, 1)
    profile_page_size = max(profile_page_size, 1)
    issue_target_page_size = max(issue_target_page_size, 1)
    total_items = await _count_import_series_by_status(
        session,
        job_id=job.id,
        status=ImportSeriesStatus.PENDING,
    )
    duplicate_count = await _count_import_series_by_status(
        session,
        job_id=job.id,
        status=ImportSeriesStatus.DUPLICATE,
    )
    progress_plan = (
        await _build_scan_review_progress_plan(
            session,
            job_id=job.id,
            page_size=profile_page_size,
            raise_if_cancelled=raise_if_cancelled,
        )
        if progress_callback
        else None
    )

    processed_items = 0
    after_id = 0
    while True:
        await raise_if_cancelled(session, job.id)
        items = await _load_pending_import_series_page(
            session,
            job_id=job.id,
            after_id=after_id,
            page_size=item_page_size,
        )
        if not items:
            break

        item_by_id = {item.id: item for item in items}
        cv_id_map = await _load_existing_cv_candidates(
            session,
            requested_cv_ids={item.cv_id for item in items if item.cv_id is not None},
        )
        matched_item_ids: set[int] = set()
        for item in items:
            if await _mark_cv_id_duplicate(
                session,
                item,
                job_id=job.id,
                cv_id_map=cv_id_map,
                log_event=log_event,
            ):
                duplicate_count += 1
                matched_item_ids.add(item.id)

        candidate_items = [item for item in items if item.id not in matched_item_ids]
        requested_titles = {
            item.raw_series_name.strip().lower() for item in candidate_items if item.raw_series_name
        }
        requested_years = {item.raw_year for item in candidate_items if item.raw_year is not None}
        item_ids_by_title: dict[str, list[int]] = {}
        item_ids_by_year: dict[int, list[int]] = {}
        for item in candidate_items:
            item_ids_by_title.setdefault(item.raw_series_name.strip().lower(), []).append(item.id)
            if item.raw_year is not None:
                item_ids_by_year.setdefault(item.raw_year, []).append(item.id)

        candidate_after_id = 0
        while candidate_items:
            await raise_if_cancelled(session, job.id)
            candidates = await _load_existing_name_candidate_page(
                session,
                requested_titles=requested_titles,
                requested_years=requested_years,
                after_id=candidate_after_id,
                page_size=candidate_page_size,
            )
            if not candidates:
                break

            for candidate_index, candidate in enumerate(candidates):
                if candidate_index and candidate_index % 25 == 0:
                    await raise_if_cancelled(session, job.id)
                relevant_item_ids = set(item_ids_by_title.get(candidate.title.lower(), ()))
                if candidate.year_start is not None:
                    for year in range(candidate.year_start - 1, candidate.year_start + 2):
                        relevant_item_ids.update(item_ids_by_year.get(year, ()))
                for imported_series_id in sorted(relevant_item_ids):
                    if imported_series_id in matched_item_ids:
                        continue
                    item = item_by_id[imported_series_id]
                    if await _mark_name_year_duplicate_candidate(
                        session,
                        item,
                        job_id=job.id,
                        existing=candidate,
                        log_event=log_event,
                        raise_if_cancelled=raise_if_cancelled,
                        issue_target_page_size=issue_target_page_size,
                    ):
                        duplicate_count += 1
                        matched_item_ids.add(imported_series_id)

            candidate_after_id = candidates[-1].id

        job.series_duplicate = duplicate_count
        await session.commit()
        processed_items += len(items)
        after_id = items[-1].id
        last_item = items[-1]

        if progress_callback and progress_plan is not None:
            completed_weight = progress_plan.analysis_weight * min(
                processed_items / max(total_items, 1),
                1.0,
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
                    status=ImportJobStatus.ANALYZING,
                    phase="analyzing",
                    progress=progress,
                    message=f"Analyzing {processed_items}/{total_items}...",
                    current_series=last_item.raw_series_name,
                    current_series_status=last_item.status,
                    estimated_seconds_remaining=estimate_remaining_work_seconds(
                        job.scan_completed_at or job.scan_started_at,
                        completed_units=completed_weight,
                        total_units=progress_plan.total_weight,
                    ),
                    **job_stats(job),
                ),
                progress_callback,
            )

        del item_by_id, items

    job.series_duplicate = duplicate_count
    await session.flush()

    await log_event(
        session,
        job.id,
        "INFO",
        "import_dedup_completed",
        message=f"Deduplication complete: {duplicate_count} duplicates found",
        duplicate_count=duplicate_count,
        duration_ms=round((time.monotonic() - started_at) * 1000),
    )


async def _count_import_series_by_status(
    session: AsyncSession,
    *,
    job_id: int,
    status: ImportSeriesStatus,
) -> int:
    count = await session.scalar(
        sa_select(sa_func.count(ImportedSeries.id)).where(
            ImportedSeries.import_job_id == job_id,
            ImportedSeries.status == status,
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


async def _load_scan_review_profile_page(
    session: AsyncSession,
    *,
    job_id: int,
    after_id: int,
    page_size: int,
) -> list[_ScanReviewProfileRow]:
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
        _ScanReviewProfileRow(
            id=row.id,
            file_count=int(row.files_total or row.file_count or 0),
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
    page_size: int,
    raise_if_cancelled: RaiseIfCancelledFunc,
) -> ScanReviewProgressPlan:
    analysis_weight = 0.0
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
        analysis_weight += scan_review_analysis_weight(len(profiles))
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

    return ScanReviewProgressPlan(
        analysis_weights=(analysis_weight,) if analysis_weight else (),
        series_match_weights=(series_match_weight,) if series_match_weight else (),
        file_match_weights=(file_match_weight,) if file_match_weight else (),
    )


async def _load_existing_cv_candidates(
    session: AsyncSession,
    *,
    requested_cv_ids: set[int],
) -> dict[int, _ExistingSeriesCandidate]:
    if not requested_cv_ids:
        return {}
    result = await session.execute(
        _existing_series_candidate_select()
        .where(Series.comicvine_id.in_(requested_cv_ids))
        .order_by(Series.id)
    )
    candidates: dict[int, _ExistingSeriesCandidate] = {}
    for row in result.mappings():
        candidate = _existing_series_candidate_from_row(row)
        if candidate.comicvine_id is not None:
            candidates[candidate.comicvine_id] = candidate
    return candidates


async def _load_existing_name_candidate_page(
    session: AsyncSession,
    *,
    requested_titles: set[str],
    requested_years: set[int],
    after_id: int,
    page_size: int,
) -> list[_ExistingSeriesCandidate]:
    existing_filters: list[ColumnElement[bool]] = []
    if requested_titles:
        existing_filters.append(sa_func.lower(Series.title).in_(requested_titles))
    if requested_years:
        widened_years = {year + delta for year in requested_years for delta in (-1, 0, 1)}
        existing_filters.append(Series.year_start.in_(sorted(widened_years)))
    if not existing_filters:
        return []

    result = await session.execute(
        _existing_series_candidate_select()
        .where(
            Series.id > after_id,
            sa_or(*existing_filters),
        )
        .order_by(Series.id)
        .limit(page_size)
    )
    return [_existing_series_candidate_from_row(row) for row in result.mappings()]


def _existing_series_candidate_select() -> Select[
    tuple[
        int,
        int | None,
        str,
        int | None,
        SeriesType,
        str,
        int,
        str | None,
    ]
]:
    return sa_select(
        Series.id,
        Series.comicvine_id,
        Series.title,
        Series.year_start,
        Series.series_type,
        Publisher.name.label("publisher_name"),
        Series.issue_count,
        Series.comicvine_url,
    ).outerjoin(Publisher, Publisher.id == Series.publisher_id)


def _existing_series_candidate_from_row(row: RowMapping) -> _ExistingSeriesCandidate:
    return _ExistingSeriesCandidate(
        id=row.id,
        comicvine_id=row.comicvine_id,
        title=row.title,
        year_start=row.year_start,
        series_type=row.series_type,
        publisher_name=row.publisher_name,
        issue_count=row.issue_count,
        comicvine_url=row.comicvine_url,
    )


async def _mark_cv_id_duplicate(
    session: AsyncSession,
    item: ImportedSeries,
    *,
    job_id: int,
    cv_id_map: dict[int, _ExistingSeriesCandidate],
    log_event: LogEventFunc,
) -> bool:
    if item.cv_id is None or item.cv_id not in cv_id_map:
        return False

    existing = cv_id_map[item.cv_id]
    item.status = ImportSeriesStatus.DUPLICATE
    item.series_id = existing.id
    _hydrate_duplicate_cv_fields(item, existing, match_score=1.0)
    item.diagnostics = {
        "kind": "duplicate_series",
        "duplicate_reason": "cv_id",
        "existing_series_id": existing.id,
        "existing_series_title": existing.title,
        "existing_series_year": existing.year_start,
        "duplicate_match_score": 1.0,
    }

    await log_event(
        session,
        job_id,
        "DEBUG",
        "import_dedup_cv_id_match",
        message=f"Duplicate: '{item.raw_series_name}' matches existing series by CV ID",
        raw_series_name=item.raw_series_name,
        cv_id=item.cv_id,
        existing_series_id=existing.id,
        existing_series_title=existing.title,
        existing_series_year=existing.year_start,
    )
    return True


async def _mark_name_year_duplicate_candidate(
    session: AsyncSession,
    item: ImportedSeries,
    *,
    job_id: int,
    existing: _ExistingSeriesCandidate,
    log_event: LogEventFunc,
    raise_if_cancelled: RaiseIfCancelledFunc,
    issue_target_page_size: int,
) -> bool:
    candidate_series_type = series_type_from_import_diagnostics(item.diagnostics)
    matched_by_name_year = is_same_series(
        item.raw_series_name,
        item.raw_year,
        candidate_series_type,
        existing.title,
        existing.year_start,
        existing.series_type,
    )
    matched_by_issue_target = (
        False
        if matched_by_name_year
        else await _exact_title_series_supports_imported_issue_targets(
            session,
            item,
            existing,
            candidate_series_type=candidate_series_type,
            job_id=job_id,
            page_size=issue_target_page_size,
            raise_if_cancelled=raise_if_cancelled,
        )
    )
    if not matched_by_name_year and not matched_by_issue_target:
        return False

    item.status = ImportSeriesStatus.DUPLICATE
    item.series_id = existing.id
    name_match = NameMatcher().match(item.raw_series_name, existing.title)
    _hydrate_duplicate_cv_fields(item, existing, match_score=name_match.similarity)
    item.diagnostics = {
        "kind": "duplicate_series",
        "duplicate_reason": ("name_year" if matched_by_name_year else "exact_title_issue_target"),
        "existing_series_id": existing.id,
        "existing_series_title": existing.title,
        "existing_series_year": existing.year_start,
        "duplicate_match_score": name_match.similarity,
    }

    await log_event(
        session,
        job_id,
        "DEBUG",
        "import_dedup_name_year_match",
        message=f"Duplicate: '{item.raw_series_name}' matches '{existing.title}'",
        raw_series_name=item.raw_series_name,
        raw_year=item.raw_year,
        existing_title=existing.title,
        existing_year=existing.year_start,
        existing_series_id=existing.id,
    )
    return True


async def _exact_title_series_supports_imported_issue_targets(
    session: AsyncSession,
    item: ImportedSeries,
    existing: _ExistingSeriesCandidate,
    *,
    candidate_series_type: SeriesType | None,
    job_id: int,
    page_size: int,
    raise_if_cancelled: RaiseIfCancelledFunc,
) -> bool:
    """Allow ongoing exact-title duplicates when file years are issue release years."""
    if not item.raw_series_name or not existing.title:
        return False
    if NameMatcher.normalize(item.raw_series_name) != NameMatcher.normalize(existing.title):
        return False
    if (
        candidate_series_type is not None
        and existing.series_type is not None
        and not series_types_compatible(candidate_series_type, existing.series_type)
    ):
        return False

    supported_any = False
    after_id = 0
    while True:
        await raise_if_cancelled(session, job_id)
        requested_targets = await _load_imported_issue_target_page(
            session,
            import_series_id=item.id,
            after_id=after_id,
            page_size=page_size,
        )
        if not requested_targets:
            break

        normalized_text_by_file_id = {
            target.id: _normalized_imported_issue_text(target) for target in requested_targets
        }
        exact_numbers = {
            exact_text
            for exact_text in normalized_text_by_file_id.values()
            if exact_text is not None
        }
        legacy_numbers = {
            target.issue_number
            for target in requested_targets
            if normalized_text_by_file_id[target.id] is None
        }
        exact_issue_dates, unambiguous_legacy_issue_dates = await _load_issue_target_dates(
            session,
            series_id=existing.id,
            exact_numbers=exact_numbers,
            legacy_numbers=legacy_numbers,
        )
        for target in requested_targets:
            exact_text = normalized_text_by_file_id[target.id]
            release_date = (
                exact_issue_dates.get(exact_text)
                if exact_text is not None
                else unambiguous_legacy_issue_dates.get(target.issue_number)
            )
            if release_date is None:
                continue
            if target.parsed_year is not None and abs(target.parsed_year - release_date.year) > 1:
                return False
            supported_any = True
        after_id = requested_targets[-1].id

    return supported_any


async def _load_imported_issue_target_page(
    session: AsyncSession,
    *,
    import_series_id: int,
    after_id: int,
    page_size: int,
) -> list[_ImportedIssueTargetRow]:
    result = await session.execute(
        sa_select(
            ImportedFile.id,
            ImportedFile.parsed_issue_number,
            ImportedFile.issue_number_raw,
            ImportedFile.parsed_year,
        )
        .where(
            ImportedFile.import_series_id == import_series_id,
            ImportedFile.parsed_issue_number.is_not(None),
            ImportedFile.id > after_id,
        )
        .order_by(ImportedFile.id)
        .limit(page_size)
    )
    return [
        _ImportedIssueTargetRow(
            id=int(row.id),
            issue_number=float(row.parsed_issue_number),
            issue_number_text=row.issue_number_raw,
            parsed_year=row.parsed_year,
        )
        for row in result
        if row.parsed_issue_number is not None
    ]


def _normalized_imported_issue_text(target: _ImportedIssueTargetRow) -> str | None:
    if not target.issue_number_text:
        return None
    try:
        normalized = normalize_issue_number_text(target.issue_number_text)
    except ValueError:
        return None
    return (
        normalized if issue_number_text_matches_numeric(target.issue_number, normalized) else None
    )


async def _load_issue_target_dates(
    session: AsyncSession,
    *,
    series_id: int,
    exact_numbers: set[str],
    legacy_numbers: set[float],
) -> tuple[dict[str, date | None], dict[float, date | None]]:
    exact_dates: dict[str, date | None] = {}
    if exact_numbers:
        exact_result = await session.execute(
            sa_select(Issue.issue_number_text, Issue.release_date).where(
                Issue.series_id == series_id,
                Issue.issue_number_text.in_(exact_numbers),
            )
        )
        exact_dates = {
            str(row.issue_number_text): row.release_date
            for row in exact_result
            if row.issue_number_text is not None
        }

    legacy_dates: dict[float, date | None] = {}
    if legacy_numbers:
        legacy_result = await session.execute(
            sa_select(
                Issue.issue_number,
                sa_func.count(Issue.id).label("candidate_count"),
                sa_func.max(Issue.release_date).label("release_date"),
            )
            .where(
                Issue.series_id == series_id,
                Issue.issue_number.in_(legacy_numbers),
            )
            .group_by(Issue.issue_number)
        )
        legacy_dates = {
            float(row.issue_number): row.release_date
            for row in legacy_result
            if int(row.candidate_count) == 1
        }
    return exact_dates, legacy_dates


def _hydrate_duplicate_cv_fields(
    item: ImportedSeries,
    existing: _ExistingSeriesCandidate,
    *,
    match_score: float | None = None,
) -> None:
    """Mirror existing ComicVine metadata onto duplicate import rows."""
    if existing.comicvine_id is None:
        return

    item.cv_id = existing.comicvine_id
    item.cv_title = existing.title
    item.cv_year = existing.year_start
    item.cv_publisher = existing.publisher_name
    item.cv_issue_count = existing.issue_count
    item.cv_url = existing.comicvine_url
    if item.cv_match_score is None and match_score is not None:
        item.cv_match_score = round(match_score, 4)
