"""Import Step 3 review-table query and row-shaping helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, and_, case, func, or_, select

from pullbox.core.issue_numbers import format_issue_number
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportSeriesStatus,
)
from pullbox.models.issue import Issue

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_IMPORT_REVIEW_SERIES_SORT_OPTIONS = {
    "found_series",
    "year",
    "files",
    "cv_match",
    "cv_year",
    "cv_id",
    "confidence",
    "status",
}
_DEFAULT_IMPORT_REVIEW_SERIES_SORT = "confidence"
_IMPORT_REVIEW_FILE_SORT_OPTIONS = {
    "file_name",
    "series",
    "parsed_issue",
    "matched_issue",
    "confidence",
    "status",
}

_IMPORT_REVIEW_MATCHED_FILE_TARGET_STATUSES = (
    ImportedFileStatus.MATCHED,
    ImportedFileStatus.CONFIRMED,
    ImportedFileStatus.ALREADY_OWNED,
)


def _series_conflict_kind_filter() -> ColumnElement[bool]:
    """Return a filter that keeps series-candidate conflicts out of no-match views."""
    diagnostics_kind = ImportedSeries.diagnostics["kind"].as_string()
    return or_(
        ImportedSeries.diagnostics.is_(None),
        diagnostics_kind.is_(None),
        diagnostics_kind != "series_conflict",
    )


def _series_has_known_target_filter() -> ColumnElement[bool]:
    """Return a filter for import rows that already know their target series."""
    return or_(
        ImportedSeries.cv_id.is_not(None),
        ImportedSeries.user_selected_cv_id.is_not(None),
        ImportedSeries.series_id.is_not(None),
    )


def _needs_issue_match_filter() -> ColumnElement[bool]:
    """Return the Step 3 review filter for rows that need issue decisions."""
    return and_(
        _series_conflict_kind_filter(),
        or_(
            and_(
                ImportedSeries.status == ImportSeriesStatus.NO_MATCH,
                _series_has_known_target_filter(),
            ),
            and_(
                ImportedSeries.status == ImportSeriesStatus.DUPLICATE,
                ImportedSeries.series_id.is_not(None),
                ImportedSeries.files_no_match > 0,
            ),
        ),
    )


def _needs_series_match_filter() -> ColumnElement[bool]:
    """Return the Step 3 review filter for rows that still need a series match."""
    return and_(
        ImportedSeries.status == ImportSeriesStatus.NO_MATCH,
        _series_conflict_kind_filter(),
        ~_series_has_known_target_filter(),
    )


def _safety_blocked_files_filter() -> ColumnElement[bool]:
    """Return rows with one or more files requiring explicit safety review."""
    return (
        select(ImportedFile.id)
        .where(
            ImportedFile.import_job_id == ImportedSeries.import_job_id,
            ImportedFile.import_series_id == ImportedSeries.id,
            ImportedFile.status.in_(
                [ImportedFileStatus.SAFETY_BLOCKED, ImportedFileStatus.SAFETY_APPROVED]
            ),
        )
        .exists()
    )


def _normalize_import_review_series_sort(sort: str | None) -> str:
    """Return a safe series-review sort key."""
    if not sort:
        return _DEFAULT_IMPORT_REVIEW_SERIES_SORT
    field = sort.lstrip("-")
    if field not in _IMPORT_REVIEW_SERIES_SORT_OPTIONS:
        return _DEFAULT_IMPORT_REVIEW_SERIES_SORT
    return f"-{field}" if sort.startswith("-") else field


def _normalize_import_review_file_sort(sort: str | None) -> str:
    """Return a safe file-review sort key."""
    if not sort:
        return "series"
    field = sort.lstrip("-")
    if field not in _IMPORT_REVIEW_FILE_SORT_OPTIONS:
        return "series"
    return f"-{field}" if sort.startswith("-") else field


def _get_import_review_series_order_by(sort: str) -> list[ColumnElement[object]]:
    """Build stable order clauses for import review series sorting."""
    normalized_sort = _normalize_import_review_series_sort(sort)
    sort_desc = normalized_sort.startswith("-")
    sort_field = normalized_sort.lstrip("-")

    status_sort = case(
        (ImportedSeries.status == ImportSeriesStatus.PENDING, 0),
        (ImportedSeries.status == ImportSeriesStatus.MATCHED, 1),
        (ImportedSeries.status == ImportSeriesStatus.NO_MATCH, 2),
        (ImportedSeries.status == ImportSeriesStatus.DUPLICATE, 3),
        (ImportedSeries.status == ImportSeriesStatus.CONFIRMED, 4),
        (ImportedSeries.status == ImportSeriesStatus.SKIPPED, 5),
        (ImportedSeries.status == ImportSeriesStatus.IMPORTING, 6),
        (ImportedSeries.status == ImportSeriesStatus.IMPORTED, 7),
        (ImportedSeries.status == ImportSeriesStatus.FAILED, 8),
        else_=99,
    )
    files_sort = case(
        (ImportedSeries.files_total > 0, ImportedSeries.files_total),
        else_=ImportedSeries.file_count,
    )

    sort_map: dict[str, list[object]] = {
        "found_series": [ImportedSeries.raw_series_name],
        "year": [ImportedSeries.raw_year, ImportedSeries.raw_series_name],
        "files": [files_sort, ImportedSeries.raw_series_name],
        "cv_match": [ImportedSeries.cv_title, ImportedSeries.raw_series_name],
        "cv_year": [ImportedSeries.cv_year, ImportedSeries.raw_series_name],
        "cv_id": [ImportedSeries.cv_id, ImportedSeries.raw_series_name],
        "confidence": [ImportedSeries.cv_match_score, ImportedSeries.raw_series_name],
        "status": [status_sort, ImportedSeries.raw_series_name],
    }
    sort_columns = sort_map.get(sort_field, [ImportedSeries.raw_series_name])

    order_by: list[ColumnElement[object]] = []
    for col in sort_columns:
        column: ColumnElement[object] = col  # type: ignore[assignment]
        order_by.append(column.desc().nullslast() if sort_desc else column.asc().nullslast())
    order_by.append(ImportedSeries.id.desc())  # type: ignore[arg-type]
    return order_by


def _get_import_review_file_order_by(sort: str) -> list[ColumnElement[object]]:
    """Build stable order clauses for import review file sorting."""
    normalized_sort = _normalize_import_review_file_sort(sort)
    sort_desc = normalized_sort.startswith("-")
    sort_field = normalized_sort.lstrip("-")

    status_sort = case(
        (ImportedFile.status == ImportedFileStatus.PENDING, 0),
        (ImportedFile.status == ImportedFileStatus.MATCHED, 1),
        (ImportedFile.status == ImportedFileStatus.DUPLICATE_FILE, 2),
        (ImportedFile.status == ImportedFileStatus.ALREADY_OWNED, 3),
        (ImportedFile.status == ImportedFileStatus.CONFLICT, 4),
        (ImportedFile.status == ImportedFileStatus.NO_MATCH, 5),
        (ImportedFile.status == ImportedFileStatus.CONFIRMED, 6),
        (ImportedFile.status == ImportedFileStatus.SKIPPED, 7),
        (ImportedFile.status == ImportedFileStatus.IMPORTED, 8),
        (ImportedFile.status == ImportedFileStatus.FAILED, 9),
        else_=99,
    )
    confidence_sort = case(
        (ImportedFile.match_confidence == "manual", 4),
        (ImportedFile.match_confidence == "high", 3),
        (ImportedFile.match_confidence == "medium", 2),
        (ImportedFile.match_confidence == "low", 1),
        else_=0,
    )
    matched_issue_sort = func.coalesce(Issue.issue_number, ImportedFile.parsed_issue_number)

    sort_map: dict[str, list[object]] = {
        "file_name": [ImportedFile.file_name],
        "series": [ImportedSeries.raw_series_name, ImportedFile.file_name],
        "parsed_issue": [ImportedFile.parsed_issue_number, ImportedFile.file_name],
        "matched_issue": [matched_issue_sort, ImportedFile.file_name],
        "confidence": [confidence_sort, ImportedFile.file_name],
        "status": [status_sort, ImportedFile.file_name],
    }
    sort_columns = sort_map.get(
        sort_field,
        [ImportedSeries.raw_series_name, ImportedFile.file_name],
    )

    order_by: list[ColumnElement[object]] = []
    for col in sort_columns:
        column: ColumnElement[object] = col  # type: ignore[assignment]
        order_by.append(column.desc().nullslast() if sort_desc else column.asc().nullslast())
    order_by.append(ImportedFile.id.desc())  # type: ignore[arg-type]
    return order_by


def _format_import_review_issue_number(issue_number: object) -> str | None:
    """Format an issue number for compact Step 3 file-target display."""
    if issue_number is None or isinstance(issue_number, bool):
        return None
    if not isinstance(issue_number, int | float | str):
        return str(issue_number).strip() or None
    try:
        numeric_issue = float(issue_number)
    except (TypeError, ValueError):
        return str(issue_number).strip() or None
    return format_issue_number(numeric_issue)


def _comicvine_issue_url(issue_cv_id: int) -> str:
    """Build a generic ComicVine issue URL when only the issue ID is known."""
    return f"https://comicvine.gamespot.com/issue/4000-{issue_cv_id}/"


async def _load_import_review_matched_file_targets(
    session: AsyncSession,
    job_id: int,
    series_ids: list[int],
) -> dict[int, list[dict[str, object]]]:
    """Load compact matched file-target rows for visible Step 3 review rows."""
    if not series_ids:
        return {}

    files_result = await session.execute(
        select(ImportedFile)
        .where(
            ImportedFile.import_job_id == job_id,
            ImportedFile.import_series_id.in_(series_ids),
            ImportedFile.status.in_(_IMPORT_REVIEW_MATCHED_FILE_TARGET_STATUSES),
        )
        .order_by(
            ImportedFile.import_series_id.asc(),
            case(
                (ImportedFile.status == ImportedFileStatus.MATCHED, 0),
                (ImportedFile.status == ImportedFileStatus.CONFIRMED, 0),
                (ImportedFile.status == ImportedFileStatus.ALREADY_OWNED, 1),
                else_=2,
            ),
            ImportedFile.parsed_issue_number.asc(),
            ImportedFile.id.asc(),
        )
    )
    files = list(files_result.scalars().all())
    issue_ids = sorted(
        {
            int(imp_file.matched_issue_id)
            for imp_file in files
            if imp_file.matched_issue_id is not None
        }
    )
    issue_map: dict[int, Issue] = {}
    if issue_ids:
        issues_result = await session.execute(select(Issue).where(Issue.id.in_(issue_ids)))
        issue_map = {int(issue.id): issue for issue in issues_result.scalars().all()}

    rows_by_series_id: dict[int, list[dict[str, object]]] = {
        int(series_id): [] for series_id in series_ids
    }
    for imp_file in files:
        issue = (
            issue_map.get(int(imp_file.matched_issue_id))
            if imp_file.matched_issue_id is not None
            else None
        )
        diagnostics = dict(imp_file.diagnostics or {})
        issue_number: object = (
            issue.issue_number if issue is not None else diagnostics.get("target_issue_number")
        )
        if issue_number is None:
            issue_number = imp_file.parsed_issue_number
        issue_cv_id = imp_file.matched_issue_cv_id or (
            issue.comicvine_id if issue is not None else None
        )
        rows_by_series_id.setdefault(int(imp_file.import_series_id), []).append(
            {
                "file_name": imp_file.file_name,
                "file_size": imp_file.file_size,
                "matched_issue_number": _format_import_review_issue_number(issue_number),
                "matched_issue_cv_id": issue_cv_id,
                "matched_issue_url": _comicvine_issue_url(int(issue_cv_id))
                if issue_cv_id is not None
                else None,
            }
        )

    return rows_by_series_id


def _import_review_matched_issue_label(
    imp_file: ImportedFile,
    issue_map: dict[int, Issue],
) -> str | None:
    """Build a compact matched issue label for inline review file details."""
    if imp_file.matched_issue_id is None:
        return None
    issue = issue_map.get(int(imp_file.matched_issue_id))
    if issue is None:
        return None
    number = _format_import_review_issue_number(issue.issue_number)
    if issue.title and number:
        return f"#{number} {issue.title}"
    if number:
        return f"#{number}"
    return issue.title or None


def _import_review_duplicate_reason_label(reason: str | None) -> str:
    labels = {
        "exact_duplicate": "Excluded because another incoming file for this same issue was kept.",
        "hash_confirmed_duplicate": (
            "Excluded because an identical incoming file for this issue was confirmed by hash "
            "and kept."
        ),
        "already_owned_duplicate": (
            "Excluded because another incoming file already covered this owned issue."
        ),
        "informational_duplicate": (
            "Excluded because another incoming file already represented this non-importable "
            "duplicate-series case."
        ),
    }
    return labels.get(
        reason or "",
        "Excluded because another incoming file for this same issue was kept.",
    )


def _build_import_review_file_detail_row(
    imp_file: ImportedFile,
    issue_map: dict[int, Issue],
) -> dict[str, object]:
    """Build the inline Step 3 file-detail payload used by review dropdowns."""
    diagnostics = dict(imp_file.diagnostics or {})
    return {
        "id": imp_file.id,
        "file_name": imp_file.file_name,
        "file_size": imp_file.file_size,
        "has_comicinfo": imp_file.has_comicinfo,
        "status": imp_file.status.value,
        "match_confidence": imp_file.match_confidence,
        "matched_issue_label": _import_review_matched_issue_label(imp_file, issue_map),
        "duplicate_reason_label": _import_review_duplicate_reason_label(
            diagnostics.get("duplicate_reason")
            if isinstance(diagnostics.get("duplicate_reason"), str)
            else None
        ),
        "duplicate_keep_label": diagnostics.get("representative_file_name"),
    }


def _import_review_file_group_key(
    imp_file: ImportedFile,
    imported_series: ImportedSeries,
) -> str | None:
    if imp_file.status in {ImportedFileStatus.MATCHED, ImportedFileStatus.CONFIRMED}:
        return "matched"
    if imp_file.status == ImportedFileStatus.CONFLICT:
        return "conflict"
    if imp_file.status == ImportedFileStatus.ALREADY_OWNED:
        return "already_owned"
    if imp_file.status == ImportedFileStatus.DUPLICATE_FILE:
        return "duplicate_file"
    if imp_file.status == ImportedFileStatus.NO_MATCH or (
        imported_series.status == ImportSeriesStatus.NO_MATCH
        and imp_file.status == ImportedFileStatus.PENDING
    ):
        return "no_match"
    return None


async def _load_import_review_file_detail_groups(
    session: AsyncSession,
    job_id: int,
    series_items: list[ImportedSeries],
) -> dict[int, list[dict[str, object]]]:
    """Load grouped per-file details for visible Step 3 review rows."""
    if not series_items:
        return {}

    series_by_id = {int(item.id): item for item in series_items if item.id is not None}
    series_ids = list(series_by_id)
    files_result = await session.execute(
        select(ImportedFile)
        .where(
            ImportedFile.import_job_id == job_id,
            ImportedFile.import_series_id.in_(series_ids),
        )
        .order_by(
            ImportedFile.import_series_id.asc(),
            case(
                (ImportedFile.status == ImportedFileStatus.MATCHED, 0),
                (ImportedFile.status == ImportedFileStatus.CONFIRMED, 0),
                (ImportedFile.status == ImportedFileStatus.CONFLICT, 1),
                (ImportedFile.status == ImportedFileStatus.ALREADY_OWNED, 2),
                (ImportedFile.status == ImportedFileStatus.DUPLICATE_FILE, 3),
                (ImportedFile.status == ImportedFileStatus.NO_MATCH, 4),
                else_=5,
            ),
            ImportedFile.id.asc(),
        )
    )
    files = list(files_result.scalars().all())
    issue_ids = sorted(
        {
            int(imp_file.matched_issue_id)
            for imp_file in files
            if imp_file.matched_issue_id is not None
        }
    )
    issue_map: dict[int, Issue] = {}
    if issue_ids:
        issues_result = await session.execute(select(Issue).where(Issue.id.in_(issue_ids)))
        issue_map = {int(issue.id): issue for issue in issues_result.scalars().all()}

    grouped_rows_by_series: dict[int, dict[str, list[dict[str, object]]]] = {
        series_id: {
            "matched": [],
            "conflict": [],
            "already_owned": [],
            "duplicate_file": [],
            "no_match": [],
        }
        for series_id in series_ids
    }
    for imp_file in files:
        imported_series = series_by_id.get(int(imp_file.import_series_id))
        if imported_series is None:
            continue
        group_key = _import_review_file_group_key(imp_file, imported_series)
        if group_key is None:
            continue
        grouped_rows_by_series[int(imp_file.import_series_id)][group_key].append(
            _build_import_review_file_detail_row(imp_file, issue_map)
        )

    group_definitions = [
        (
            "matched",
            "Importable files",
            "Files that currently have a resolved issue target for this import.",
        ),
        (
            "conflict",
            "Conflicts",
            "Competing incoming files for the same issue. Resolve keep choices "
            "from the Conflicts view.",
        ),
        (
            "already_owned",
            "Already Owned",
            "Files that point to issues that are already owned in the library.",
        ),
        (
            "duplicate_file",
            "Extra Incoming Files",
            "Redundant copies found within this import for the same issue target.",
        ),
        (
            "no_match",
            "No Match",
            "Files that stayed unmatched or were blocked for manual review.",
        ),
    ]
    return {
        series_id: [
            {
                "key": key,
                "label": label,
                "description": description,
                "rows": grouped_rows[key],
                "count": len(grouped_rows[key]),
            }
            for key, label, description in group_definitions
            if grouped_rows[key]
        ]
        for series_id, grouped_rows in grouped_rows_by_series.items()
    }
