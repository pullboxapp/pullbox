"""Import Step 3 conflict-review context helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select

from pullbox.composition.services import build_import_control_service
from pullbox.core.exceptions import NotFoundError
from pullbox.core.name_matcher import NameMatcher
from pullbox.core.release_parser import parse_release_title
from pullbox.core.source_metadata import SourceMetadataExtractor
from pullbox.models.import_job import ImportedFile, ImportedSeries, ImportJob
from pullbox.models.issue import Issue

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _object_to_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float | str):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return default


async def _load_import_conflict_review_context(
    job_id: int,
    session: AsyncSession,
    page: int = 1,
    sort: str = "series",
) -> dict[str, object]:
    """Load the conflict review context shared by the Step 3 shell and legacy partial."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)

    allowed_sort_fields = {"series", "conflict", "files", "signal", "status"}
    sort_field = sort.removeprefix("-")
    if sort_field not in allowed_sort_fields:
        sort = "series"

    page_size = 25
    svc = build_import_control_service()
    conflict_page = await svc.get_conflict_groups_page(
        session,
        job_id,
        page=page,
        page_size=page_size,
        sort=sort,
    )
    conflict_groups = list(conflict_page.items)
    metadata_extractor = SourceMetadataExtractor()

    def _series_label(imp_series: ImportedSeries) -> str:
        label = imp_series.raw_series_name
        if imp_series.raw_year:
            label += f" ({imp_series.raw_year})"
        return label

    def _comicinfo_for_file(imp_file: ImportedFile) -> dict[str, object] | None:
        diagnostics = dict(imp_file.diagnostics or {})
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

    def _build_source_file_summary(
        imp_file: ImportedFile,
        *,
        current_series_label: str | None = None,
    ) -> dict[str, object]:
        diagnostics = dict(imp_file.diagnostics or {})
        source_metadata = diagnostics.get("source_metadata")
        source_metadata = source_metadata if isinstance(source_metadata, dict) else {}
        metadata_signals = diagnostics.get("metadata_signals")
        metadata_signals = metadata_signals if isinstance(metadata_signals, dict) else {}
        filename_parse = _filename_parse_for_file(imp_file)
        return {
            "file_id": imp_file.id,
            "file_name": imp_file.file_name,
            "file_path": imp_file.file_path,
            "status": imp_file.status.value,
            "match_method": imp_file.match_method,
            "parsed_series": imp_file.parsed_series,
            "parsed_issue_number": imp_file.parsed_issue_number,
            "parsed_year": imp_file.parsed_year,
            "filename_series": filename_parse.get("series_name"),
            "filename_issue_number": filename_parse.get("issue_number"),
            "filename_year": filename_parse.get("year"),
            "has_comicinfo": imp_file.has_comicinfo,
            "comicinfo": _comicinfo_for_file(imp_file),
            "metadata_signals": metadata_signals,
            "source_metadata": source_metadata,
            "current_series_label": current_series_label,
        }

    def _distinct_filename_series_names(files: list[ImportedFile]) -> list[str]:
        seen: dict[str, str] = {}
        for imp_file in files:
            filename_parse = _filename_parse_for_file(imp_file)
            raw_name = filename_parse.get("series_name")
            if not raw_name or not isinstance(raw_name, str):
                continue
            label = raw_name.strip()
            if not label:
                continue
            normalized = NameMatcher.normalize(label)
            if normalized and normalized not in seen:
                seen[normalized] = label
        return sorted(seen.values(), key=lambda value: NameMatcher.normalize(value))

    series_by_id: dict[int, ImportedSeries] = {}
    for group in conflict_groups:
        group_series = group.get("series")
        if isinstance(group_series, ImportedSeries):
            series_by_id[group_series.id] = group_series

    requested_series_ids = {
        int(series_id)
        for group in conflict_groups
        if (series_id := group.get("series_id")) is not None
    }
    missing_series_ids = requested_series_ids.difference(series_by_id)
    if missing_series_ids:
        series_result = await session.execute(
            select(ImportedSeries).where(ImportedSeries.id.in_(missing_series_ids))
        )
        series_by_id.update({item.id: item for item in series_result.scalars().all()})

    issue_ids = {
        int(issue_id)
        for group in conflict_groups
        if (issue_id := group.get("matched_issue_id")) is not None
    }
    issues_by_id: dict[int, Issue] = {}
    if issue_ids:
        issue_result = await session.execute(select(Issue).where(Issue.id.in_(issue_ids)))
        issues_by_id = {item.id: item for item in issue_result.scalars().all()}

    series_conflicts = [
        group for group in conflict_groups if group.get("kind") == "series_conflict"
    ]
    sibling_series_by_name: dict[str, list[ImportedSeries]] = {}
    related_files_by_series_id: dict[int, list[ImportedFile]] = {}
    if series_conflicts:
        conflict_series = [
            series_by_id[int(group["series_id"])]
            for group in series_conflicts
            if int(group["series_id"]) in series_by_id
        ]
        raw_names = {item.raw_series_name.casefold() for item in conflict_series}
        sibling_result = await session.execute(
            select(ImportedSeries).where(
                ImportedSeries.import_job_id == job_id,
                func.lower(ImportedSeries.raw_series_name).in_(raw_names),
            )
        )
        sibling_series = list(sibling_result.scalars().all())
        for sibling in sibling_series:
            sibling_series_by_name.setdefault(
                NameMatcher.normalize(sibling.raw_series_name), []
            ).append(sibling)

        sibling_ids = [sibling.id for sibling in sibling_series]
        if sibling_ids:
            related_file_limit = max(24, len(series_conflicts) * 24)
            sibling_files_result = await session.execute(
                select(ImportedFile)
                .where(ImportedFile.import_series_id.in_(sibling_ids))
                .order_by(ImportedFile.has_comicinfo.desc(), ImportedFile.id.asc())
                .limit(related_file_limit)
            )
            for sibling_file in sibling_files_result.scalars().all():
                related_files_by_series_id.setdefault(sibling_file.import_series_id, []).append(
                    sibling_file
                )

    enriched_groups: list[dict[str, object]] = []
    for group in conflict_groups:
        if group.get("kind") == "series_conflict":
            imp_series = series_by_id.get(int(group["series_id"]))
            if imp_series is None:
                continue
            files = [item for item in group.get("files", []) if isinstance(item, ImportedFile)]
            parsed_series_names = _distinct_filename_series_names(files)
            normalized_title = NameMatcher.normalize(imp_series.raw_series_name or "")
            sibling_series = [
                sibling
                for sibling in sibling_series_by_name.get(normalized_title, [])
                if sibling.id != imp_series.id
            ]
            sibling_by_id = {sibling.id: sibling for sibling in sibling_series}
            related_source_files: list[dict[str, object]] = []
            current_series_year = imp_series.raw_year
            if sibling_by_id:
                for sibling_id, sibling in sibling_by_id.items():
                    for sibling_file in related_files_by_series_id.get(sibling_id, []):
                        related_summary = _build_source_file_summary(
                            sibling_file,
                            current_series_label=_series_label(sibling),
                        )
                        if related_summary["comicinfo"]:
                            related_source_files.append(related_summary)
            related_source_files.sort(
                key=lambda item: (
                    0
                    if isinstance(item.get("parsed_year"), int)
                    and item.get("parsed_year") == current_series_year
                    else 1,
                    str(item.get("current_series_label") or ""),
                    str(item.get("file_name") or ""),
                )
            )
            enriched_groups.append(
                {
                    "kind": "series_conflict",
                    "conflict_group_id": group["conflict_group_id"],
                    "series_id": imp_series.id,
                    "matched_issue_id": None,
                    "files": files,
                    "issue": None,
                    "series_name": _series_label(imp_series),
                    "raw_series_name": imp_series.raw_series_name,
                    "source_folder": imp_series.source_folder or "",
                    "has_preferred": False,
                    "file_count": _object_to_int(group.get("file_count"), len(files)),
                    "files_truncated": bool(group.get("files_truncated")),
                    "display_issue_number": None,
                    "parsed_series_names": parsed_series_names,
                    "mixed_series_bucket": False,
                    "diagnostics": dict(group.get("diagnostics") or imp_series.diagnostics or {}),
                    "source_files": [
                        _build_source_file_summary(
                            imp_file,
                            current_series_label=_series_label(imp_series),
                        )
                        for imp_file in files
                    ],
                    "related_source_files": related_source_files[:6],
                }
            )
            continue

        issue: Issue | None = None
        series_name = ""
        source_folder = ""
        if group["matched_issue_id"]:
            issue = issues_by_id.get(int(group["matched_issue_id"]))
        group_files = [item for item in group.get("files", []) if isinstance(item, ImportedFile)]
        group_series_id = group.get("series_id")
        imp_series = series_by_id.get(int(group_series_id)) if group_series_id is not None else None
        if imp_series:
            series_name = imp_series.raw_series_name
            if imp_series.raw_year:
                series_name += f" ({imp_series.raw_year})"
            source_folder = imp_series.source_folder or ""

        parsed_issue_numbers = sorted(
            {f.parsed_issue_number for f in group_files if f.parsed_issue_number is not None}
        )
        parsed_series_names = _distinct_filename_series_names(group_files)
        display_issue_number = (
            issue.issue_number
            if issue is not None
            else (parsed_issue_numbers[0] if parsed_issue_numbers else None)
        )

        has_preferred = any(f.is_preferred for f in group_files)
        preferred_file = next((f for f in group_files if f.is_preferred), None)
        diagnostics = (
            (preferred_file.diagnostics if preferred_file is not None else None)
            or (group_files[0].diagnostics if group_files else {})
            or {}
        )
        enriched_groups.append(
            {
                "kind": group.get("kind", "file_conflict"),
                "conflict_group_id": group["conflict_group_id"],
                "matched_issue_id": group["matched_issue_id"],
                "files": group_files,
                "issue": issue,
                "series_name": series_name,
                "source_folder": source_folder,
                "has_preferred": has_preferred,
                "file_count": _object_to_int(group.get("file_count"), len(group_files)),
                "files_truncated": bool(group.get("files_truncated")),
                "display_issue_number": display_issue_number,
                "parsed_series_names": parsed_series_names,
                "mixed_series_bucket": len(parsed_series_names) > 1,
                "diagnostics": diagnostics,
            }
        )

    total_groups = conflict_page.total
    total_pages = max(1, (total_groups + page_size - 1) // page_size)
    current_page = conflict_page.page
    visible_file_conflict_groups = [
        group for group in enriched_groups if group.get("kind") == "file_conflict"
    ]

    return {
        "job": job,
        "conflict_groups": enriched_groups,
        "auto_resolved": conflict_page.auto_resolved,
        "needs_decision": conflict_page.needs_decision,
        "series_candidate_conflicts": conflict_page.series_candidate_conflicts,
        "file_conflict_group_count": conflict_page.file_conflict_groups,
        "visible_file_conflict_group_count": len(visible_file_conflict_groups),
        "total_groups": total_groups,
        "page": current_page,
        "page_size": page_size,
        "total_pages": total_pages,
        "sort": sort,
    }
