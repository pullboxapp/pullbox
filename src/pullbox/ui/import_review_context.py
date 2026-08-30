"""Import review table context loading."""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import func, select

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportSeriesStatus,
)
from pullbox.models.library import LibraryRoot
from pullbox.services.import_review_selection import load_import_review_selection_state
from pullbox.services.import_safety_diagnostics import normalize_import_safety_diagnostics
from pullbox.ui.import_conflict_review import _load_import_conflict_review_context
from pullbox.ui.import_review_summary import (
    load_import_review_summary,
    load_import_safety_failure_summary,
)
from pullbox.ui.import_review_tables import (
    _get_import_review_series_order_by,
    _load_import_review_file_detail_groups,
    _load_import_review_matched_file_targets,
    _needs_issue_match_filter,
    _needs_series_match_filter,
    _normalize_import_review_series_sort,
    _safety_blocked_files_filter,
    _series_conflict_kind_filter,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.import_job import ImportJob


def _object_to_int(value: object, default: int = 0) -> int:
    try:
        return int(cast("Any", value))
    except (TypeError, ValueError):
        return default


def _resolve_review_view(status: str | None) -> tuple[str, ImportSeriesStatus | None]:
    requested_series_status: ImportSeriesStatus | None = None
    current_view = "series"
    if status == "conflicts":
        current_view = "conflicts"
    elif status in {"needs_series", "needs_issue", "safety_blocked"}:
        current_view = status
    elif status:
        with contextlib.suppress(ValueError):
            requested_series_status = ImportSeriesStatus(status)
            current_view = requested_series_status.value
    return current_view, requested_series_status


def _review_filters(
    *,
    job_id: int,
    current_view: str,
    requested_series_status: ImportSeriesStatus | None,
) -> list[Any]:
    filters: list[Any] = [ImportedSeries.import_job_id == job_id]
    if current_view == "needs_issue":
        filters.append(_needs_issue_match_filter())
    elif current_view == "needs_series":
        filters.append(_needs_series_match_filter())
    elif current_view == "safety_blocked":
        filters.append(_safety_blocked_files_filter())
    elif requested_series_status is not None:
        filters.append(ImportedSeries.status == requested_series_status)
        if requested_series_status == ImportSeriesStatus.MATCHED:
            filters.append(ImportedSeries.files_conflict == 0)
        elif requested_series_status == ImportSeriesStatus.NO_MATCH:
            filters.append(_series_conflict_kind_filter())
    return filters


async def _load_selected_review_series_ids(
    session: AsyncSession,
    job_id: int,
) -> list[int]:
    """Return the authoritative matched-series selection for one review job."""
    result = await session.execute(
        select(ImportedSeries.id)
        .where(
            ImportedSeries.import_job_id == job_id,
            ImportedSeries.status == ImportSeriesStatus.MATCHED,
            ImportedSeries.files_conflict == 0,
            ImportedSeries.selected_for_import.is_(True),
        )
        .order_by(ImportedSeries.id.asc())
    )
    return [int(series_id) for series_id in result.scalars().all()]


async def _load_duplicate_selected_file_counts(
    session: AsyncSession,
    job_id: int,
) -> dict[int, int]:
    """Return selected duplicate-series matched-file counts keyed by imported series."""
    selection_state = await load_import_review_selection_state(session, job_id)
    counts = selection_state.get("duplicate_selected_file_counts", {})
    if not isinstance(counts, Mapping):
        return {}
    return {
        _object_to_int(series_id): _object_to_int(count)
        for series_id, count in counts.items()
        if _object_to_int(series_id) > 0
    }


async def _load_status_counts(
    session: AsyncSession,
    job_id: int,
) -> dict[str, int]:
    status_counts: dict[str, int] = {}
    for status in ImportSeriesStatus:
        status_filters: list[Any] = [
            ImportedSeries.import_job_id == job_id,
            ImportedSeries.status == status,
        ]
        if status == ImportSeriesStatus.MATCHED:
            status_filters.append(ImportedSeries.files_conflict == 0)
        elif status == ImportSeriesStatus.NO_MATCH:
            status_filters.append(_series_conflict_kind_filter())
        cnt = (
            await session.execute(select(func.count(ImportedSeries.id)).where(*status_filters))
        ).scalar_one()
        if cnt > 0:
            status_counts[status.value] = cnt

    needs_issue_count = (
        await session.execute(
            select(func.count(ImportedSeries.id)).where(
                ImportedSeries.import_job_id == job_id,
                _needs_issue_match_filter(),
            )
        )
    ).scalar_one()
    needs_series_count = (
        await session.execute(
            select(func.count(ImportedSeries.id)).where(
                ImportedSeries.import_job_id == job_id,
                _needs_series_match_filter(),
            )
        )
    ).scalar_one()
    if needs_issue_count > 0:
        status_counts["needs_issue"] = needs_issue_count
    if needs_series_count > 0:
        status_counts["needs_series"] = needs_series_count
    safety_blocked_count = (
        await session.execute(
            select(func.count(ImportedSeries.id)).where(
                ImportedSeries.import_job_id == job_id,
                _safety_blocked_files_filter(),
            )
        )
    ).scalar_one()
    if safety_blocked_count > 0:
        status_counts["safety_blocked"] = safety_blocked_count
    return status_counts


async def _load_safety_blocked_files_by_series_id(
    session: AsyncSession,
    visible_series_ids: list[int],
) -> dict[int, list[ImportedFile]]:
    safety_blocked_files_by_series_id: dict[int, list[ImportedFile]] = {
        series_id: [] for series_id in visible_series_ids
    }
    if not visible_series_ids:
        return safety_blocked_files_by_series_id

    safety_files_result = await session.execute(
        select(ImportedFile)
        .where(
            ImportedFile.import_series_id.in_(visible_series_ids),
            ImportedFile.status.in_(
                [ImportedFileStatus.SAFETY_BLOCKED, ImportedFileStatus.SAFETY_APPROVED]
            ),
        )
        .order_by(ImportedFile.import_series_id.asc(), ImportedFile.id.asc())
    )
    for imp_file in safety_files_result.scalars().all():
        safety_blocked_files_by_series_id.setdefault(
            imp_file.import_series_id,
            [],
        ).append(imp_file)
    return safety_blocked_files_by_series_id


def _build_safety_block_context_by_file_id(
    files_by_series_id: Mapping[int, list[ImportedFile]],
) -> dict[int, dict[str, object]]:
    """Normalize persisted/legacy blocks without mutating ORM diagnostics."""
    result: dict[int, dict[str, object]] = {}
    for files in files_by_series_id.values():
        for imp_file in files:
            diagnostics = imp_file.diagnostics or {}
            if not isinstance(diagnostics, Mapping):
                continue
            safety_block = diagnostics.get("safety_block")
            if isinstance(safety_block, Mapping):
                result[imp_file.id] = normalize_import_safety_diagnostics(safety_block)
    return result


async def load_import_review_context(
    session: AsyncSession,
    job: ImportJob,
    *,
    status: str | None,
    page: int,
    sort: str | None,
) -> dict[str, object]:
    """Load the template context for the Step 3 review table."""
    job_id = int(job.id)
    current_view, requested_series_status = _resolve_review_view(status)
    page_size = 25
    normalized_sort = _normalize_import_review_series_sort(sort)
    total = 0
    series_items: list[ImportedSeries] = []
    library_roots: list[LibraryRoot] = []
    conflict_review_ctx: dict[str, object] | None = None
    matched_file_targets_by_series_id: dict[int, list[dict[str, object]]] = {}
    review_file_groups_by_series_id: dict[int, list[dict[str, object]]] = {}
    safety_rematch_pending = False

    if current_view == "safety_blocked":
        remaining_safety_rows = (
            await session.execute(
                select(func.count(ImportedSeries.id)).where(
                    ImportedSeries.import_job_id == job_id,
                    _safety_blocked_files_filter(),
                )
            )
        ).scalar_one()
        if remaining_safety_rows == 0:
            # A completed approved-file rematch closes the now-empty transient tab.
            current_view = "series"
            requested_series_status = None

    if current_view == "conflicts":
        conflict_review_ctx = await _load_import_conflict_review_context(
            job_id,
            session,
            page=page,
            sort=sort or "series",
        )
        total = _object_to_int(conflict_review_ctx["total_groups"])
        page = _object_to_int(conflict_review_ctx["page"])
        page_size = _object_to_int(conflict_review_ctx["page_size"])
        normalized_sort = str(conflict_review_ctx["sort"])
        safety_blocked_files_by_series_id: dict[int, list[ImportedFile]] = {}
    else:
        filters = _review_filters(
            job_id=job_id,
            current_view=current_view,
            requested_series_status=requested_series_status,
        )
        count_result = await session.execute(select(func.count(ImportedSeries.id)).where(*filters))
        total = count_result.scalar_one()

        series_result = await session.execute(
            select(ImportedSeries)
            .where(*filters)
            .order_by(*_get_import_review_series_order_by(normalized_sort))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        series_items = list(series_result.scalars().all())
        visible_series_ids = [item.id for item in series_items]
        safety_blocked_files_by_series_id = await _load_safety_blocked_files_by_series_id(
            session,
            visible_series_ids,
        )
        safety_rematch_pending = any(
            imp_file.status == ImportedFileStatus.SAFETY_APPROVED
            for files in safety_blocked_files_by_series_id.values()
            for imp_file in files
        )
        if visible_series_ids:
            matched_file_targets_by_series_id = await _load_import_review_matched_file_targets(
                session,
                visible_series_ids,
            )
            review_file_groups_by_series_id = await _load_import_review_file_detail_groups(
                session,
                series_items,
            )
        library_roots_result = await session.execute(
            select(LibraryRoot).where(LibraryRoot.enabled.is_(True)).order_by(LibraryRoot.id)
        )
        library_roots = list(library_roots_result.scalars().all())

    template_ctx: dict[str, object] = {
        "job": job,
        "series_items": series_items,
        "library_roots": library_roots,
        "total": total,
        "page": page,
        "page_size": page_size,
        "status_filter": current_view if current_view != "series" else None,
        "current_view": current_view,
        "sort": normalized_sort,
        "status_counts": await _load_status_counts(session, job_id),
        "review_summary": await load_import_review_summary(session, job),
        "safety_failure_summary": await load_import_safety_failure_summary(session, job),
        "selected_series_ids": await _load_selected_review_series_ids(session, job_id),
        "duplicate_selected_file_counts": await _load_duplicate_selected_file_counts(
            session,
            job_id,
        ),
        "safety_blocked_files_by_series_id": safety_blocked_files_by_series_id,
        "safety_block_context_by_file_id": _build_safety_block_context_by_file_id(
            safety_blocked_files_by_series_id
        ),
        "safety_rematch_pending": safety_rematch_pending,
        "matched_file_targets_by_series_id": matched_file_targets_by_series_id,
        "review_file_groups_by_series_id": review_file_groups_by_series_id,
    }
    if conflict_review_ctx:
        template_ctx.update(
            {key: value for key, value in conflict_review_ctx.items() if key != "job"}
        )
    return template_ctx
