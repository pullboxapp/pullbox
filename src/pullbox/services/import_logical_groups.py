"""Logical-series grouping helpers for import workflows."""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.name_matcher import NameMatcher
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportSeriesStatus,
)
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile
from pullbox.models.publisher import Publisher
from pullbox.models.series import Series
from pullbox.services.import_matching import build_ambiguous_series_conflict_diagnostics
from pullbox.services.import_series_match_state import clear_auto_cv_match_fields


class ImportEventLogger(Protocol):
    """Callable contract for writing structured import-job events."""

    def __call__(
        self,
        session: AsyncSession,
        job_id: int,
        level: str,
        event: str,
        message: str | None = None,
        **kwargs: Any,
    ) -> Awaitable[None]: ...


LogicalGroupKeyFunc = Callable[
    [ImportedSeries],
    tuple[object, ...] | None,
]
CounterRecomputeFunc = Callable[[AsyncSession, ImportJob], Awaitable[None]]


async def reclassify_matched_series_duplicates(
    session: AsyncSession,
    job: ImportJob,
    *,
    log_event: ImportEventLogger,
    recompute_series_counters: CounterRecomputeFunc,
    series_ids: list[int] | None = None,
) -> int:
    """Convert exact post-match ComicVine hits into existing-library duplicates."""
    filters = [
        ImportedSeries.import_job_id == job.id,
        ImportedSeries.status == ImportSeriesStatus.MATCHED,
        ImportedSeries.cv_id.is_not(None),
        ImportedSeries.series_id.is_(None),
    ]
    if series_ids:
        filters.append(ImportedSeries.id.in_(series_ids))

    items_result = await session.execute(sa_select(ImportedSeries).where(*filters))
    items = list(items_result.scalars().all())
    if not items:
        return 0

    existing_result = await session.execute(
        sa_select(
            Series.id,
            Series.comicvine_id,
            Series.title,
            Series.year_start,
        ).where(Series.comicvine_id.in_([item.cv_id for item in items if item.cv_id is not None]))
    )
    existing_by_cv = {
        comicvine_id: (series_id, title, year_start)
        for series_id, comicvine_id, title, year_start in existing_result.all()
        if comicvine_id is not None
    }

    duplicate_count = 0
    conflict_count = 0
    for item in items:
        if item.cv_id not in existing_by_cv:
            if await _mark_library_owned_ambiguous_candidate_conflict(
                session,
                item,
                job=job,
                log_event=log_event,
            ):
                conflict_count += 1
            continue
        existing_id, existing_title, existing_year = existing_by_cv[item.cv_id]
        item.status = ImportSeriesStatus.DUPLICATE
        item.selected_for_import = False
        item.series_id = existing_id
        item.diagnostics = {
            "kind": "duplicate_series",
            "duplicate_reason": "cv_id",
            "existing_series_id": existing_id,
            "existing_series_title": existing_title,
            "existing_series_year": existing_year,
        }
        duplicate_count += 1
        await log_event(
            session,
            job.id,
            "DEBUG",
            "import_dedup_post_match_cv_id_match",
            message=(
                f"Duplicate after matching: '{item.raw_series_name}' matches "
                "existing series by CV ID"
            ),
            raw_series_name=item.raw_series_name,
            cv_id=item.cv_id,
            existing_series_id=existing_id,
            existing_series_title=existing_title,
            existing_series_year=existing_year,
        )

    if duplicate_count or conflict_count:
        await session.flush()
        await recompute_series_counters(session, job)
    return duplicate_count


_LIBRARY_AMBIGUITY_IGNORED_TITLE_TOKENS = frozenset(
    {
        "dc",
        "marvel",
        "comic",
        "comics",
        "and",
        "vs",
        "versus",
    }
)


async def _mark_library_owned_ambiguous_candidate_conflict(
    session: AsyncSession,
    item: ImportedSeries,
    *,
    job: ImportJob,
    log_event: ImportEventLogger,
) -> bool:
    """Move metadata-poor one-shot matches to review when a library-owned twin exists."""
    if item.user_selected_cv_id is not None or item.cv_match_method == "user_override":
        return False
    if item.cv_id is None or item.cv_issue_count != 1:
        return False
    if not item.raw_series_name or not item.cv_title:
        return False

    files_result = await session.execute(
        sa_select(ImportedFile).where(ImportedFile.import_series_id == item.id)
    )
    files = list(files_result.scalars().all())
    if len(files) != 1:
        return False
    imp_file = files[0]
    if imp_file.has_comicinfo or imp_file.comicvine_issue_id is not None:
        return False
    if (
        imp_file.parsed_issue_number is None
        or abs(float(imp_file.parsed_issue_number) - 1.0) > 0.0001
    ):
        return False

    raw_tokens = _library_ambiguity_core_tokens(item.raw_series_name)
    selected_tokens = _library_ambiguity_core_tokens(item.cv_title)
    if not raw_tokens or selected_tokens != raw_tokens:
        return False

    competing = await _find_owned_ambiguous_library_candidate(
        session,
        item,
        issue_number=float(imp_file.parsed_issue_number),
        raw_tokens=raw_tokens,
    )
    if competing is None:
        return False

    selected_candidate = _selected_candidate_diagnostics(item)
    competing_candidate = _owned_competing_candidate_diagnostics(competing)
    item.status = ImportSeriesStatus.NO_MATCH
    item.selected_for_import = False
    item.diagnostics = {
        **build_ambiguous_series_conflict_diagnostics(
            raw_name=item.raw_series_name,
            raw_year=item.raw_year,
            match_threshold=job.cv_match_threshold,
            selected_candidate=selected_candidate,
            competing_candidate=competing_candidate,
            top_candidates=[selected_candidate, competing_candidate],
        ),
        "reason": "library_owned_ambiguous_candidate",
    }
    clear_auto_cv_match_fields(item)

    imp_file.status = ImportedFileStatus.NO_MATCH
    imp_file.include_in_import = False
    imp_file.diagnostics = {
        **dict(imp_file.diagnostics or {}),
        "kind": "series_conflict_file",
        "reason": "library_owned_ambiguous_candidate",
        "target_state": "series_match_conflict",
        "rejection_reason": (
            "A same-year single-issue library item has the same crossover title tokens."
        ),
        "selected_candidate": selected_candidate,
        "competing_candidate": competing_candidate,
    }
    await log_event(
        session,
        job.id,
        "DEBUG",
        "import_series_library_owned_ambiguity",
        message=f"Library-owned ambiguous candidate requires review: {item.raw_series_name}",
        raw_series_name=item.raw_series_name,
        selected_cv_id=selected_candidate.get("cv_id"),
        competing_cv_id=competing_candidate.get("cv_id"),
        existing_series_id=competing_candidate.get("existing_series_id"),
    )
    return True


async def _find_owned_ambiguous_library_candidate(
    session: AsyncSession,
    item: ImportedSeries,
    *,
    issue_number: float,
    raw_tokens: Counter[str],
) -> dict[str, Any] | None:
    year = item.cv_year or item.raw_year
    if year is None:
        return None

    result = await session.execute(
        sa_select(
            Series.id,
            Series.comicvine_id,
            Series.title,
            Series.year_start,
            Series.issue_count,
            Publisher.name.label("publisher_name"),
            Issue.id.label("issue_id"),
            Issue.comicvine_id.label("issue_cv_id"),
            Issue.issue_number,
            LibraryFile.id.label("library_file_id"),
        )
        .join(Issue, Issue.series_id == Series.id)
        .join(LibraryFile, LibraryFile.issue_id == Issue.id)
        .outerjoin(Publisher, Publisher.id == Series.publisher_id)
        .where(
            Series.comicvine_id.is_not(None),
            Series.comicvine_id != item.cv_id,
            Series.year_start >= year - 1,
            Series.year_start <= year + 1,
            Series.issue_count == 1,
            Issue.issue_number == issue_number,
        )
    )
    for row in result.mappings().all():
        title = str(row["title"] or "")
        if _library_ambiguity_core_tokens(title) != raw_tokens:
            continue
        return dict(row)
    return None


def _library_ambiguity_core_tokens(title: str) -> Counter[str]:
    return Counter(
        token
        for token in NameMatcher.normalize(title).split()
        if token and token not in _LIBRARY_AMBIGUITY_IGNORED_TITLE_TOKENS
    )


def _selected_candidate_diagnostics(item: ImportedSeries) -> dict[str, Any]:
    score = float(item.cv_match_score or 0.0)
    return {
        "cv_id": item.cv_id,
        "title": item.cv_title,
        "year": item.cv_year,
        "publisher": item.cv_publisher,
        "issue_count": item.cv_issue_count,
        "score": round(score, 4),
        "score_pct": round(score * 100),
        "match_method": item.cv_match_method,
        "rejection_reasons": ["Auto-match is ambiguous with an already-owned library candidate."],
    }


def _owned_competing_candidate_diagnostics(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "cv_id": row.get("comicvine_id"),
        "title": row.get("title"),
        "year": row.get("year_start"),
        "publisher": row.get("publisher_name"),
        "issue_count": row.get("issue_count"),
        "score": 1.0,
        "score_pct": 100,
        "match_method": "library_owned_ambiguous_candidate",
        "existing_series_id": row.get("id"),
        "owned_issue_id": row.get("issue_id"),
        "owned_issue_cv_id": row.get("issue_cv_id"),
        "owned_library_file_id": row.get("library_file_id"),
        "rejection_reasons": ["Already owned in the library with the same crossover title tokens."],
    }


async def consolidate_logical_series_groups(
    session: AsyncSession,
    job: ImportJob,
    *,
    log_event: ImportEventLogger,
    logical_series_group_key: Callable[..., tuple[object, ...] | None],
    recompute_series_counters: CounterRecomputeFunc,
    recompute_file_counters: CounterRecomputeFunc,
    series_ids: list[int] | None = None,
    prefer_resolved_cv_only: bool = False,
) -> dict[int, int]:
    """Physically merge repeated import buckets that resolve to the same logical series."""
    query = (
        sa_select(ImportedSeries)
        .where(
            ImportedSeries.import_job_id == job.id,
            ImportedSeries.status.in_([ImportSeriesStatus.MATCHED, ImportSeriesStatus.DUPLICATE]),
        )
        .order_by(ImportedSeries.id.asc())
    )
    if series_ids:
        query = query.where(ImportedSeries.id.in_(series_ids))

    result = await session.execute(query)
    canonical_by_series_id = {series_id: series_id for series_id in (series_ids or [])}
    groups: dict[tuple[object, ...], list[ImportedSeries]] = {}
    for item in result.scalars().all():
        key = logical_series_group_key(
            item,
            prefer_resolved_cv_only=prefer_resolved_cv_only,
        )
        if key is not None:
            groups.setdefault(key, []).append(item)

    for group_key, grouped_items in groups.items():
        if len(grouped_items) < 2:
            if grouped_items:
                canonical_by_series_id[grouped_items[0].id] = grouped_items[0].id
            continue

        canonical = grouped_items[0]
        redundant_items = grouped_items[1:]
        redundant_ids = [item.id for item in redundant_items]
        for series_item in grouped_items:
            canonical_by_series_id[series_item.id] = canonical.id
        file_rows = list(
            (
                await session.execute(
                    sa_select(ImportedFile).where(ImportedFile.import_series_id.in_(redundant_ids))
                )
            )
            .scalars()
            .all()
        )
        for imp_file in file_rows:
            imp_file.import_series_id = canonical.id

        all_sample_paths: list[str] = []
        source_folders: list[str] = []
        merged_from_ids: list[int] = []
        total_file_count = 0
        total_files_total = 0
        for series_item in grouped_items:
            total_file_count += int(series_item.file_count or 0)
            total_files_total += int(series_item.files_total or 0)
            merged_from_ids.append(series_item.id)
            if series_item.source_folder and series_item.source_folder not in source_folders:
                source_folders.append(series_item.source_folder)
            for sample_path in list(series_item.sample_paths or []):
                if sample_path not in all_sample_paths:
                    all_sample_paths.append(sample_path)

        canonical.file_count = total_file_count
        canonical.files_total = total_files_total
        canonical.sample_paths = all_sample_paths
        canonical.has_files = any(item.has_files for item in grouped_items)
        canonical.selected_for_import = canonical.status == ImportSeriesStatus.MATCHED and any(
            item.selected_for_import for item in grouped_items
        )
        if len(source_folders) == 1:
            canonical.source_folder = source_folders[0]

        diagnostics = dict(canonical.diagnostics or {})
        diagnostics.update(
            {
                "logical_group_key": f"{group_key[0]}:{group_key[1]}",
                "consolidated_series_count": len(grouped_items),
                "source_folders": source_folders,
                "merged_from_series_ids": merged_from_ids,
            }
        )
        canonical.diagnostics = diagnostics

        for redundant in redundant_items:
            await session.delete(redundant)

        await log_event(
            session,
            job.id,
            "INFO",
            "import_series_consolidated",
            message=(
                f"Consolidated {len(grouped_items)} import buckets into {canonical.raw_series_name}"
            ),
            imported_series_id=canonical.id,
            merged_from_series_ids=merged_from_ids,
            logical_group_key=f"{group_key[0]}:{group_key[1]}",
            source_folders=source_folders,
        )

    await session.flush()
    await recompute_series_counters(session, job)
    await recompute_file_counters(session, job)
    return canonical_by_series_id


async def logical_group_series_ids(
    session: AsyncSession,
    job_id: int,
    group_key: tuple[object, ...],
    *,
    logical_series_group_key: Callable[..., tuple[object, ...] | None],
    prefer_resolved_cv_only: bool = False,
) -> list[int]:
    """Return import-series ids that currently resolve to the same logical group."""
    result = await session.execute(
        sa_select(ImportedSeries).where(
            ImportedSeries.import_job_id == job_id,
            ImportedSeries.status.in_([ImportSeriesStatus.MATCHED, ImportSeriesStatus.DUPLICATE]),
        )
    )
    return [
        item.id
        for item in result.scalars().all()
        if logical_series_group_key(
            item,
            prefer_resolved_cv_only=prefer_resolved_cv_only,
        )
        == group_key
    ]


async def reset_series_group_files(
    session: AsyncSession,
    *,
    job_id: int,
    series_ids: list[int],
) -> None:
    """Clear file-level review state so a logical group can be rematched cleanly."""
    if not series_ids:
        return
    result = await session.execute(
        sa_select(ImportedFile).where(
            ImportedFile.import_job_id == job_id,
            ImportedFile.import_series_id.in_(series_ids),
        )
    )
    for imp_file in result.scalars().all():
        diagnostics = dict(imp_file.diagnostics or {})
        imp_file.status = ImportedFileStatus.PENDING
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
        imp_file.diagnostics = {
            key: diagnostics[key]
            for key in (
                "source_issue_type",
                "comicvine_series_id",
                "series_status",
                "issue_count_hint",
                "metadata_signals",
                "source_metadata",
            )
            if key in diagnostics
        }
