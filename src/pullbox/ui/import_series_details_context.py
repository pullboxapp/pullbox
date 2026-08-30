"""Context loading for the import review series-details modal."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import case, select

from pullbox.core.exceptions import NotFoundError
from pullbox.core.issue_numbers import format_issue_number
from pullbox.core.release_parser import parse_release_title
from pullbox.core.source_metadata import SourceMetadataExtractor
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


def is_actionable_duplicate_merge(series_item: ImportedSeries | None) -> bool:
    """Return True when a duplicate series still has wanted/missing import targets."""
    if series_item is None or series_item.status != ImportSeriesStatus.DUPLICATE:
        return False
    if (series_item.files_matched or 0) > 0 or (series_item.files_conflict or 0) > 0:
        return True
    diagnostics = dict(series_item.diagnostics or {})
    if "actionable_duplicate_merge" in diagnostics:
        return bool(diagnostics["actionable_duplicate_merge"])
    return False


def _format_issue_number(issue_number: float | None) -> str | None:
    if issue_number is None:
        return None
    return format_issue_number(issue_number)


def _duplicate_reason_label(reason: str | None) -> str:
    labels = {
        "exact_duplicate": "Excluded because another incoming file for this same issue was kept.",
        "hash_confirmed_duplicate": (
            "Excluded because an identical incoming file for this issue "
            "was confirmed by hash and kept."
        ),
        "already_owned_duplicate": (
            "Excluded because another incoming file already covered this owned issue."
        ),
        "informational_duplicate": (
            "Excluded because another incoming file already represented "
            "this non-importable duplicate-series case."
        ),
    }
    return labels.get(
        reason or "",
        "Excluded because another incoming file for this same issue was kept.",
    )


def _comicinfo_for_file(
    imp_file: ImportedFile,
    metadata_extractor: SourceMetadataExtractor,
) -> dict[str, object] | None:
    diagnostics = dict(imp_file.diagnostics or {})
    comicinfo = diagnostics.get("comicinfo")
    if isinstance(comicinfo, dict):
        return comicinfo
    source_metadata = diagnostics.get("source_metadata")
    if isinstance(source_metadata, dict):
        comicinfo = source_metadata.get("comicinfo")
        if isinstance(comicinfo, dict):
            return comicinfo
    if not imp_file.has_comicinfo:
        return None
    try:
        extracted = metadata_extractor.from_archive_path(imp_file.file_path)
    except Exception:
        return None
    comicinfo = dict(extracted.diagnostics or {}).get("comicinfo")
    return comicinfo if isinstance(comicinfo, dict) else None


def _filename_parse_for_file(imp_file: ImportedFile) -> dict[str, object]:
    diagnostics = dict(imp_file.diagnostics or {})
    source_metadata = diagnostics.get("source_metadata")
    if isinstance(source_metadata, dict):
        filename_parse = source_metadata.get("filename_parse")
        if isinstance(filename_parse, dict):
            return filename_parse

    parsed = parse_release_title(imp_file.file_name)
    return {
        "series_name": parsed.series_name if parsed is not None else None,
        "issue_number": parsed.issue_number if parsed is not None else None,
        "year": parsed.year if parsed is not None else None,
        "volume": parsed.volume if parsed is not None else None,
        "issue_type": parsed.issue_type.value if parsed is not None else None,
    }


def _matched_issue_label(
    imp_file: ImportedFile,
    issue_map: dict[int, Issue],
) -> str | None:
    if imp_file.matched_issue_id is None:
        return None
    issue = issue_map.get(imp_file.matched_issue_id)
    if issue is None:
        return None
    number = _format_issue_number(issue.issue_number)
    if issue.title and number:
        return f"#{number} {issue.title}"
    if number:
        return f"#{number}"
    return issue.title or None


def _build_file_row(
    *,
    imp_file: ImportedFile,
    imported_series: ImportedSeries,
    issue_map: dict[int, Issue],
    metadata_extractor: SourceMetadataExtractor,
    duplicate_merge_actionable: bool,
) -> dict[str, object]:
    diagnostics = dict(imp_file.diagnostics or {})
    filename_parse = _filename_parse_for_file(imp_file)
    comicinfo = _comicinfo_for_file(imp_file, metadata_extractor)
    metadata_conflict = (
        imp_file.status == ImportedFileStatus.NO_MATCH
        and diagnostics.get("kind") == "metadata_conflict"
    )
    is_importable_duplicate = (
        imported_series.status == ImportSeriesStatus.DUPLICATE
        and duplicate_merge_actionable
        and imp_file.status in {ImportedFileStatus.MATCHED, ImportedFileStatus.CONFIRMED}
    )
    filename_series = filename_parse.get("series_name")
    filename_series_label = (
        filename_series.strip()
        if isinstance(filename_series, str) and filename_series.strip()
        else imported_series.raw_series_name
    )
    return {
        "id": imp_file.id,
        "file_name": imp_file.file_name,
        "file_path": imp_file.file_path,
        "file_size": imp_file.file_size,
        "has_comicinfo": imp_file.has_comicinfo,
        "status": imp_file.status.value,
        "match_confidence": imp_file.match_confidence,
        "matched_issue_label": _matched_issue_label(imp_file, issue_map),
        "include_in_import": imp_file.include_in_import,
        "is_importable_duplicate": is_importable_duplicate,
        "filename_parse": filename_parse,
        "filename_series_label": filename_series_label,
        "comicinfo": comicinfo,
        "metadata_conflict": metadata_conflict,
        "metadata_conflict_type": diagnostics.get("conflict_type"),
        "metadata_conflict_reason": diagnostics.get("rejection_reason")
        or diagnostics.get("reason")
        or imp_file.error_message,
        "archive_entry_issue_hint": diagnostics.get("archive_entry_issue_hint"),
        "duplicate_reason_label": _duplicate_reason_label(
            diagnostics.get("duplicate_reason")
            if isinstance(diagnostics.get("duplicate_reason"), str)
            else None
        ),
        "duplicate_keep_label": diagnostics.get("representative_file_name"),
        "group_conflict_id": imp_file.conflict_group_id,
        "parsed_issue_number": _format_issue_number(imp_file.parsed_issue_number),
        "parsed_year": imp_file.parsed_year,
    }


def _group_file_rows(
    files: list[ImportedFile],
    *,
    imported_series: ImportedSeries,
    issue_map: dict[int, Issue],
    duplicate_merge_actionable: bool,
) -> dict[str, list[dict[str, object]]]:
    metadata_extractor = SourceMetadataExtractor()
    grouped_rows: dict[str, list[dict[str, object]]] = {
        "matched": [],
        "conflict": [],
        "already_owned": [],
        "duplicate_file": [],
        "no_match": [],
    }
    for imp_file in files:
        row = _build_file_row(
            imp_file=imp_file,
            imported_series=imported_series,
            issue_map=issue_map,
            metadata_extractor=metadata_extractor,
            duplicate_merge_actionable=duplicate_merge_actionable,
        )
        if imp_file.status in {ImportedFileStatus.MATCHED, ImportedFileStatus.CONFIRMED}:
            grouped_rows["matched"].append(row)
        elif imp_file.status == ImportedFileStatus.CONFLICT:
            grouped_rows["conflict"].append(row)
        elif imp_file.status == ImportedFileStatus.ALREADY_OWNED:
            grouped_rows["already_owned"].append(row)
        elif imp_file.status == ImportedFileStatus.DUPLICATE_FILE:
            grouped_rows["duplicate_file"].append(row)
        elif imp_file.status == ImportedFileStatus.NO_MATCH or (
            imported_series.status == ImportSeriesStatus.NO_MATCH
            and imp_file.status == ImportedFileStatus.PENDING
        ):
            grouped_rows["no_match"].append(row)
    return grouped_rows


def _file_groups(grouped_rows: dict[str, list[dict[str, object]]]) -> list[dict[str, object]]:
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
    return [
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


async def load_import_series_details_context(
    session: AsyncSession,
    *,
    job_id: int,
    series_id: int,
) -> dict[str, object]:
    """Load context for the per-series review details modal."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)

    imported_series = await session.get(ImportedSeries, series_id)
    if imported_series is None or imported_series.import_job_id != job_id:
        raise NotFoundError("ImportedSeries", series_id)

    duplicate_merge_actionable = is_actionable_duplicate_merge(imported_series)
    issue_result = await session.execute(
        select(Issue).where(
            Issue.id.in_(
                select(ImportedFile.matched_issue_id).where(
                    ImportedFile.import_series_id == series_id,
                    ImportedFile.matched_issue_id.is_not(None),
                )
            )
        )
    )
    issue_map = {issue.id: issue for issue in issue_result.scalars().all()}
    file_result = await session.execute(
        select(ImportedFile)
        .where(ImportedFile.import_series_id == series_id)
        .order_by(
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
    grouped_rows = _group_file_rows(
        list(file_result.scalars().all()),
        imported_series=imported_series,
        issue_map=issue_map,
        duplicate_merge_actionable=duplicate_merge_actionable,
    )
    duplicate_scope_selected_count = sum(
        1
        for row in grouped_rows["matched"]
        if bool(row["is_importable_duplicate"]) and bool(row["include_in_import"])
    )
    duplicate_scope_importable_count = sum(
        1 for row in grouped_rows["matched"] if bool(row["is_importable_duplicate"])
    )
    return {
        "job": job,
        "imported_series": imported_series,
        "file_groups": _file_groups(grouped_rows),
        "duplicate_merge_actionable": duplicate_merge_actionable,
        "duplicate_scope_selected_count": duplicate_scope_selected_count,
        "duplicate_scope_importable_count": duplicate_scope_importable_count,
    }
