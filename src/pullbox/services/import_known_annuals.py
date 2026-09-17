"""Place corroborated annuals in an existing local review series without path repair."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from pullbox.core.issue_numbers import normalize_issue_number_text
from pullbox.core.name_matcher import NameMatcher
from pullbox.core.source_metadata import SourceMetadataExtractor
from pullbox.models.import_job import ImportedFileStatus, ImportedSeries, ImportSeriesStatus
from pullbox.models.issue import IssueType

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.core.source_metadata import SourceMetadata
    from pullbox.models.import_job import ImportedFile

_YEAR_QUALIFIER = re.compile(r"\((\d{4})(?:\s*-\s*(\d{4})?)?\)")
_KNOWN_METHODS = ("mylar3_cv_id", "comicinfo_cv_id", "folder_cv_id")


def _annual_title(series: str, year: int) -> str | None:
    for match in _YEAR_QUALIFIER.finditer(series):
        start = int(match[1])
        if "-" not in match[0]:
            if start != year:
                return None
        elif start > year or (match[2] and int(match[2]) < year):
            return None
    title = NameMatcher.normalize(_YEAR_QUALIFIER.sub("", series))
    return title if title.endswith(" annual") else None


async def reassign_to_known_annual_series(
    session: AsyncSession,
    parent: ImportedSeries,
    file: ImportedFile,
    metadata: SourceMetadata,
) -> ImportedSeries | None:
    """Require agreeing filename/ComicInfo and one same-folder, same-year known series.

    This does not identify a replacement for a missing Mylar path or borrow its
    issue ID. The moved review row still passes normal issue matching afterward.
    """
    diagnostics = dict(file.diagnostics or {})
    source = metadata.diagnostics
    comicinfo = source.get("comicinfo")
    path = Path(file.file_path)
    if (
        file.status != ImportedFileStatus.PENDING
        or file.comicvine_issue_id is not None
        or metadata.comicvine_issue_id is not None
        or file.library_file_id is not None
        or file.include_in_import
        or file.match_method
        or parent.user_selected_cv_id is not None
        or parent.selected_for_import
        or metadata.issue_type != IssueType.ANNUAL
        or not isinstance(comicinfo, dict)
        or not path.is_absolute()
        or ".." in path.parts
        or any(
            diagnostics.get(key)
            for key in ("safety_block", "resolution", "review_selection", "identity_conflicts")
        )
        or any(source.get(key) for key in ("identity_conflicts", "mylar3_issue", "file_safety"))
    ):
        return None
    if metadata.signals.get("comicvine_series_id") in ("comicinfo", "sidecar"):
        return None
    release = SourceMetadataExtractor().from_release_title(file.file_name)
    if (
        release.issue_type != IssueType.ANNUAL
        or release.year is None
        or release.issue_number is None
        or not release.series_name
        or not comicinfo.get("series")
        or not comicinfo.get("number")
        or comicinfo.get("year") not in (None, release.year)
    ):
        return None
    try:
        number = normalize_issue_number_text(comicinfo["number"])
        if number != normalize_issue_number_text(release.issue_number_text or release.issue_number):
            return None
    except (TypeError, ValueError):
        return None
    filename_title = NameMatcher.normalize(release.series_name)
    if not filename_title.endswith(" annual"):
        filename_title += " annual"
    if _annual_title(str(comicinfo["series"]), release.year) != filename_title:
        return None
    if (
        NameMatcher.normalize(parent.cv_title or "") == filename_title
        and parent.cv_year == release.year
    ):
        return None
    candidates = list(
        await session.scalars(
            select(ImportedSeries)
            .where(
                ImportedSeries.import_job_id == parent.import_job_id,
                ImportedSeries.source_folder == str(path.parent),
                ImportedSeries.cv_year == release.year,
                ImportedSeries.cv_id.is_not(None),
            )
            .order_by(ImportedSeries.id)
            .limit(26)
        )
    )
    if len(candidates) > 25:
        return None
    candidates = [
        candidate
        for candidate in candidates
        if NameMatcher.normalize(candidate.cv_title or "") == filename_title
    ]
    if len(candidates) != 1 or candidates[0].cv_id == parent.cv_id:
        return None
    target = candidates[0]
    if (
        target.cv_match_method not in _KNOWN_METHODS
        or (target.cv_match_score or 0) < 0.99
        or target.user_selected_cv_id is not None
        or target.selected_for_import
        or target.status not in {ImportSeriesStatus.MATCHED, ImportSeriesStatus.DUPLICATE}
    ):
        return None
    evidence = {
        "source_import_series_id": parent.id,
        "target_import_series_id": target.id,
        "target_series_cv_id": target.cv_id,
        "filename": file.file_name,
        "comicinfo_series": comicinfo["series"],
        "issue_number_text": number,
        "publication_year": release.year,
        "method": "same_folder_known_annual_with_corroborated_title",
    }
    source = dict(source)
    source.pop("mylar3_folder_scope_conflict", None)
    source.pop("mylar3_unrecorded_file", None)
    source["known_annual_series"] = evidence
    diagnostics.update(
        {
            "source_metadata": source,
            "source_issue_type": "annual",
            "comicvine_series_id": target.cv_id,
            "known_annual_series": evidence,
        }
    )
    file.diagnostics = diagnostics
    file.import_series_id = target.id
    file.parsed_series = target.cv_title
    file.parsed_year = release.year
    file.parsed_issue_number = release.issue_number
    file.issue_number_raw = number
    parent.file_count = max(0, int(parent.file_count or 0) - 1)
    parent.sample_paths = [
        value for value in (parent.sample_paths or []) if value != file.file_path
    ]
    target.file_count = int(target.file_count or 0) + 1
    target.has_files = True
    target.sample_paths = list(dict.fromkeys([*(target.sample_paths or [])[:4], file.file_path]))
    return target
