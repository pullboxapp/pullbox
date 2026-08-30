"""Import review query helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import Integer, String, and_, case, cast, literal, null, union_all
from sqlalchemy import func as sa_func
from sqlalchemy import select as sa_select
from sqlalchemy.orm import aliased

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportSeriesStatus,
)
from pullbox.models.issue import Issue

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.selectable import Subquery


CONFLICT_GROUP_COMPATIBILITY_PAGE_SIZE = 500
MAX_CONFLICT_GROUP_PAGE_SIZE = 100
MAX_CONFLICT_GROUP_FILES = 100
CONFLICT_SORT_FIELDS = frozenset({"series", "conflict", "files", "signal", "status"})


@dataclass(frozen=True, slots=True)
class ConflictGroupsPage:
    """One bounded, deterministic page of import conflict groups."""

    items: tuple[dict[str, Any], ...]
    total: int
    page: int
    page_size: int
    auto_resolved: int = 0
    needs_decision: int = 0
    series_candidate_conflicts: int = 0
    file_conflict_groups: int = 0


@dataclass(frozen=True, slots=True)
class _ConflictGroupCounts:
    total: int
    auto_resolved: int
    needs_decision: int
    series_candidate_conflicts: int
    file_conflict_groups: int


async def get_files_for_series(
    session: AsyncSession,
    job_id: int,
    series_id: int,
    *,
    status_filter: ImportedFileStatus | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[ImportedFile], int]:
    """Return paginated files for a series within a job."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)

    item = await session.get(ImportedSeries, series_id)
    if item is None or item.import_job_id != job_id:
        raise NotFoundError("ImportedSeries", series_id)

    filters = [
        ImportedFile.import_job_id == job_id,
        ImportedFile.import_series_id == series_id,
    ]
    if status_filter is not None:
        filters.append(ImportedFile.status == status_filter)

    count_q = sa_select(sa_func.count(ImportedFile.id)).where(*filters)
    total = (await session.execute(count_q)).scalar_one()

    offset = (page - 1) * page_size
    query = (
        sa_select(ImportedFile)
        .where(*filters)
        .order_by(ImportedFile.id.asc())
        .limit(page_size)
        .offset(offset)
    )
    result = await session.execute(query)
    return list(result.scalars().all()), total


async def get_conflict_groups(
    session: AsyncSession,
    job_id: int,
) -> list[dict[str, Any]]:
    """Return all conflict groups while preserving the legacy API contract.

    New request paths should use :func:`get_conflict_groups_page`. This wrapper
    walks bounded server-side pages so existing callers keep complete results
    without the former per-series N+1 query pattern.
    """
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)

    group_keys = _conflict_group_keys(job_id)
    counts = await _count_conflict_groups(session, group_keys)
    groups: list[dict[str, Any]] = []
    offset = 0
    while offset < counts.total:
        page_items = await _load_conflict_group_slice(
            session,
            job_id,
            group_keys,
            offset=offset,
            limit=CONFLICT_GROUP_COMPATIBILITY_PAGE_SIZE,
            max_files_per_group=None,
        )
        if not page_items:
            break
        groups.extend(page_items)
        offset += CONFLICT_GROUP_COMPATIBILITY_PAGE_SIZE
    return groups


async def get_conflict_groups_page(
    session: AsyncSession,
    job_id: int,
    *,
    page: int = 1,
    page_size: int = 25,
    sort: str = "legacy",
) -> ConflictGroupsPage:
    """Return one server-paginated conflict-group page without N+1 queries."""
    if isinstance(page, bool) or page < 1:
        raise ValidationError("Conflict review page must be at least 1")
    if isinstance(page_size, bool) or page_size < 1 or page_size > MAX_CONFLICT_GROUP_PAGE_SIZE:
        raise ValidationError(
            f"Conflict review page_size must be between 1 and {MAX_CONFLICT_GROUP_PAGE_SIZE}"
        )
    if sort != "legacy" and sort.removeprefix("-") not in CONFLICT_SORT_FIELDS:
        raise ValidationError("Unsupported conflict review sort")

    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)

    group_keys = _conflict_group_keys(job_id)
    counts = await _count_conflict_groups(session, group_keys)
    total_pages = max(1, (counts.total + page_size - 1) // page_size)
    current_page = min(page, total_pages)
    items = await _load_conflict_group_slice(
        session,
        job_id,
        group_keys,
        offset=(current_page - 1) * page_size,
        limit=page_size,
        sort=sort,
        max_files_per_group=MAX_CONFLICT_GROUP_FILES,
    )
    return ConflictGroupsPage(
        items=tuple(items),
        total=counts.total,
        page=current_page,
        page_size=page_size,
        auto_resolved=counts.auto_resolved,
        needs_decision=counts.needs_decision,
        series_candidate_conflicts=counts.series_candidate_conflicts,
        file_conflict_groups=counts.file_conflict_groups,
    )


def _conflict_group_keys(job_id: int) -> Subquery:
    """Build the portable sort-key relation shared by counts and pages."""
    diagnostics_kind = ImportedSeries.diagnostics["kind"].as_string()
    series_file_counts = (
        sa_select(
            ImportedFile.import_series_id.label("series_id"),
            sa_func.count(ImportedFile.id).label("file_count"),
        )
        .where(ImportedFile.import_job_id == job_id)
        .group_by(ImportedFile.import_series_id)
        .subquery()
    )
    series_label = _series_label_expression(
        ImportedSeries.raw_series_name,
        ImportedSeries.raw_year,
    )
    selected_candidate_title = ImportedSeries.diagnostics["selected_candidate"]["title"].as_string()
    series_keys = (
        sa_select(
            literal(0).label("kind_order"),
            literal("series_conflict").label("kind"),
            ImportedSeries.id.label("group_key"),
            cast(ImportedSeries.id, String).label("sort_key"),
            _normalized_sort_expression(series_label).label("series_sort"),
            literal(1).label("issue_null_order"),
            literal(0.0).label("issue_sort"),
            sa_func.coalesce(series_file_counts.c.file_count, 0).label("file_count"),
            literal(0).label("has_preferred"),
            _normalized_sort_expression(
                sa_func.coalesce(selected_candidate_title, "candidate needs review")
            ).label("signal_sort"),
            literal("series match conflict").label("status_sort"),
            ImportedSeries.id.label("series_id"),
            cast(null(), Integer).label("matched_issue_id"),
        )
        .where(
            ImportedSeries.import_job_id == job_id,
            ImportedSeries.status == ImportSeriesStatus.NO_MATCH,
            diagnostics_kind == "series_conflict",
        )
        .outerjoin(
            series_file_counts,
            series_file_counts.c.series_id == ImportedSeries.id,
        )
    )

    file_aggregates = (
        sa_select(
            ImportedFile.conflict_group_id.label("group_key"),
            sa_func.min(ImportedFile.id).label("first_file_id"),
            sa_func.count(ImportedFile.id).label("file_count"),
            sa_func.max(case((ImportedFile.is_preferred.is_(True), 1), else_=0)).label(
                "has_preferred"
            ),
            sa_func.min(ImportedFile.parsed_issue_number).label("parsed_issue_number"),
        )
        .where(
            ImportedFile.import_job_id == job_id,
            ImportedFile.status == ImportedFileStatus.CONFLICT,
            ImportedFile.conflict_group_id.is_not(None),
        )
        .group_by(ImportedFile.conflict_group_id)
        .subquery()
    )
    first_file = aliased(ImportedFile)
    file_series = aliased(ImportedSeries)

    issue_sort = sa_func.coalesce(Issue.issue_number, file_aggregates.c.parsed_issue_number)
    file_series_label = _series_label_expression(
        file_series.raw_series_name,
        file_series.raw_year,
    )
    file_keys = (
        sa_select(
            literal(1).label("kind_order"),
            literal("file_conflict").label("kind"),
            file_aggregates.c.group_key,
            cast(file_aggregates.c.group_key, String).label("sort_key"),
            _normalized_sort_expression(file_series_label).label("series_sort"),
            case((issue_sort.is_(None), 1), else_=0).label("issue_null_order"),
            sa_func.coalesce(issue_sort, 0.0).label("issue_sort"),
            file_aggregates.c.file_count,
            file_aggregates.c.has_preferred,
            case(
                (file_aggregates.c.has_preferred == 1, "auto-selected"),
                else_="needs choice",
            ).label("signal_sort"),
            case(
                (file_aggregates.c.has_preferred == 1, "auto-selected"),
                else_="needs choice",
            ).label("status_sort"),
            first_file.import_series_id.label("series_id"),
            first_file.matched_issue_id.label("matched_issue_id"),
        )
        .select_from(file_aggregates)
        .join(first_file, first_file.id == file_aggregates.c.first_file_id)
        .join(file_series, file_series.id == first_file.import_series_id)
        .outerjoin(Issue, Issue.id == first_file.matched_issue_id)
    )
    return union_all(series_keys, file_keys).subquery()


def _series_label_expression(name: Any, year: Any) -> Any:
    return name + case(
        (year.is_not(None), literal(" (") + cast(year, String) + literal(")")),
        else_="",
    )


def _normalized_sort_expression(value: Any) -> Any:
    """Approximate NameMatcher normalization with portable SQL functions."""
    normalized: Any = sa_func.lower(sa_func.trim(sa_func.coalesce(value, "")))
    normalized = case(
        (normalized.like("the %"), sa_func.substr(normalized, 5)),
        (normalized.like("an %"), sa_func.substr(normalized, 4)),
        (normalized.like("a %"), sa_func.substr(normalized, 3)),
        else_=normalized,
    )
    normalized = sa_func.replace(normalized, "&", " and ")
    normalized = sa_func.replace(normalized, "'s", "s")
    for punctuation in ("-", "_", ".", ",", ":", ";", "!", "?", "'", '"', "(", ")"):
        normalized = sa_func.replace(normalized, punctuation, " ")
    for _ in range(4):
        normalized = sa_func.replace(normalized, "  ", " ")
    return sa_func.trim(normalized)


async def _count_conflict_groups(
    session: AsyncSession,
    group_keys: Subquery,
) -> _ConflictGroupCounts:
    row = (
        await session.execute(
            sa_select(
                sa_func.count().label("total"),
                sa_func.coalesce(
                    sa_func.sum(case((group_keys.c.kind_order == 0, 1), else_=0)), 0
                ).label("series_candidate_conflicts"),
                sa_func.coalesce(
                    sa_func.sum(case((group_keys.c.kind_order == 1, 1), else_=0)), 0
                ).label("file_conflict_groups"),
                sa_func.coalesce(
                    sa_func.sum(
                        case(
                            (
                                and_(
                                    group_keys.c.kind_order == 1,
                                    group_keys.c.has_preferred == 1,
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("auto_resolved"),
                sa_func.coalesce(
                    sa_func.sum(
                        case(
                            (
                                and_(
                                    group_keys.c.kind_order == 1,
                                    group_keys.c.has_preferred == 0,
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("needs_decision"),
            ).select_from(group_keys)
        )
    ).one()
    return _ConflictGroupCounts(
        total=int(row.total or 0),
        auto_resolved=int(row.auto_resolved or 0),
        needs_decision=int(row.needs_decision or 0),
        series_candidate_conflicts=int(row.series_candidate_conflicts or 0),
        file_conflict_groups=int(row.file_conflict_groups or 0),
    )


async def _load_conflict_group_slice(
    session: AsyncSession,
    job_id: int,
    group_keys: Subquery,
    *,
    offset: int,
    limit: int,
    sort: str = "legacy",
    max_files_per_group: int | None = MAX_CONFLICT_GROUP_FILES,
) -> list[dict[str, Any]]:
    """Load one bounded group-key slice and all files for only those groups."""
    key_rows = (
        await session.execute(
            sa_select(
                group_keys.c.kind,
                group_keys.c.group_key,
                group_keys.c.file_count,
                group_keys.c.series_id,
                group_keys.c.matched_issue_id,
            )
            .order_by(*_conflict_group_order(group_keys, sort))
            .offset(offset)
            .limit(limit)
        )
    ).all()
    if not key_rows:
        return []

    ordered_keys = [(str(row.kind), int(row.group_key)) for row in key_rows]
    file_count_by_key = {
        (str(row.kind), int(row.group_key)): int(row.file_count or 0) for row in key_rows
    }
    series_ids = [int(row.series_id) for row in key_rows if row.series_id is not None]
    conflict_series_ids = [
        group_key for kind, group_key in ordered_keys if kind == "series_conflict"
    ]
    file_group_ids = [group_key for kind, group_key in ordered_keys if kind == "file_conflict"]

    series_by_id: dict[int, ImportedSeries] = {}
    if series_ids:
        series_result = await session.execute(
            sa_select(ImportedSeries).where(ImportedSeries.id.in_(series_ids))
        )
        series_by_id = {int(item.id): item for item in series_result.scalars().all()}

    files_by_series_id: dict[int, list[ImportedFile]] = {}
    files_by_group_id: dict[int, list[ImportedFile]] = {}
    if conflict_series_ids or file_group_ids:
        file_rows = await _load_bounded_group_files(
            session,
            job_id,
            conflict_series_ids=conflict_series_ids,
            file_group_ids=file_group_ids,
            max_files_per_group=max_files_per_group,
        )
        for imp_file, kind, group_key in file_rows:
            if kind == "series_conflict":
                files_by_series_id.setdefault(group_key, []).append(imp_file)
            group_id = imp_file.conflict_group_id
            if (
                kind == "file_conflict"
                and group_id is not None
                and group_id in file_group_ids
                and imp_file.status == ImportedFileStatus.CONFLICT
            ):
                files_by_group_id.setdefault(group_id, []).append(imp_file)

    groups: list[dict[str, Any]] = []
    for kind, group_key in ordered_keys:
        if kind == "series_conflict":
            imp_series = series_by_id.get(group_key)
            if imp_series is None:
                continue
            groups.append(
                {
                    "kind": "series_conflict",
                    "conflict_group_id": f"series-{imp_series.id}",
                    "series_id": imp_series.id,
                    "matched_issue_id": None,
                    "files": files_by_series_id.get(group_key, []),
                    "file_count": file_count_by_key[(kind, group_key)],
                    "files_truncated": (
                        file_count_by_key[(kind, group_key)]
                        > len(files_by_series_id.get(group_key, []))
                    ),
                    "series": imp_series,
                    "diagnostics": dict(imp_series.diagnostics or {}),
                }
            )
            continue

        files = files_by_group_id.get(group_key, [])
        if not files:
            continue
        key_row = next(
            row for row in key_rows if str(row.kind) == kind and int(row.group_key) == group_key
        )
        series_id = int(key_row.series_id)
        groups.append(
            {
                "kind": "file_conflict",
                "conflict_group_id": group_key,
                "matched_issue_id": files[0].matched_issue_id,
                "files": files,
                "file_count": file_count_by_key[(kind, group_key)],
                "files_truncated": file_count_by_key[(kind, group_key)] > len(files),
                "series_id": series_id,
                "series": series_by_id.get(series_id),
            }
        )
    return groups


def _conflict_group_order(group_keys: Subquery, sort: str) -> tuple[Any, ...]:
    if sort == "legacy":
        return (group_keys.c.kind_order.asc(), group_keys.c.sort_key.asc())

    descending = sort.startswith("-")
    field = sort.removeprefix("-")
    default_columns = (
        group_keys.c.series_sort,
        group_keys.c.kind_order,
        group_keys.c.issue_null_order,
        group_keys.c.issue_sort,
        group_keys.c.sort_key,
    )
    columns: tuple[Any, ...]
    match field:
        case "conflict":
            columns = (
                group_keys.c.kind_order,
                group_keys.c.issue_null_order,
                group_keys.c.issue_sort,
                *default_columns,
            )
        case "files":
            columns = (group_keys.c.file_count, *default_columns)
        case "signal":
            columns = (group_keys.c.signal_sort, *default_columns)
        case "status":
            columns = (group_keys.c.status_sort, *default_columns)
        case _:
            columns = default_columns
    if descending:
        return tuple(column.desc() for column in columns)
    return tuple(column.asc() for column in columns)


async def _load_bounded_group_files(
    session: AsyncSession,
    job_id: int,
    *,
    conflict_series_ids: list[int],
    file_group_ids: list[int],
    max_files_per_group: int | None,
) -> list[tuple[ImportedFile, str, int]]:
    candidates: list[Any] = []
    if conflict_series_ids:
        candidates.append(
            sa_select(
                ImportedFile.id.label("file_id"),
                literal("series_conflict").label("kind"),
                ImportedFile.import_series_id.label("group_key"),
            ).where(
                ImportedFile.import_job_id == job_id,
                ImportedFile.import_series_id.in_(conflict_series_ids),
            )
        )
    if file_group_ids:
        candidates.append(
            sa_select(
                ImportedFile.id.label("file_id"),
                literal("file_conflict").label("kind"),
                ImportedFile.conflict_group_id.label("group_key"),
            ).where(
                ImportedFile.import_job_id == job_id,
                ImportedFile.status == ImportedFileStatus.CONFLICT,
                ImportedFile.conflict_group_id.in_(file_group_ids),
            )
        )
    if not candidates:
        return []

    candidate_rows = union_all(*candidates).subquery()
    if max_files_per_group is None:
        selected_rows = candidate_rows
    else:
        ranked_rows = sa_select(
            candidate_rows.c.file_id,
            candidate_rows.c.kind,
            candidate_rows.c.group_key,
            sa_func.row_number()
            .over(
                partition_by=(candidate_rows.c.kind, candidate_rows.c.group_key),
                order_by=candidate_rows.c.file_id.asc(),
            )
            .label("group_row_number"),
        ).subquery()
        selected_rows = (
            sa_select(
                ranked_rows.c.file_id,
                ranked_rows.c.kind,
                ranked_rows.c.group_key,
            )
            .where(ranked_rows.c.group_row_number <= max_files_per_group)
            .subquery()
        )

    rows = (
        await session.execute(
            sa_select(ImportedFile, selected_rows.c.kind, selected_rows.c.group_key)
            .join(selected_rows, selected_rows.c.file_id == ImportedFile.id)
            .order_by(
                selected_rows.c.kind.asc(),
                selected_rows.c.group_key.asc(),
                ImportedFile.id.asc(),
            )
        )
    ).all()
    return [(row[0], str(row[1]), int(row[2])) for row in rows]
