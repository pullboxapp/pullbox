"""File-level catalog evidence shared by deferred and referenced-file recovery."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from pullbox.core.issue_numbers import normalize_issue_number_text
from pullbox.core.name_matcher import NameMatcher
from pullbox.core.release_parser import parse_release_title
from pullbox.core.story_arc_ordering import extract_story_arc_order_prefix

if TYPE_CHECKING:
    from pullbox.models.import_job import ImportedFile


def catalog_file_identity(file: ImportedFile) -> dict[str, Any] | None:
    """Extract a candidate without inheriting the parent folder's series or year."""
    diagnostics = dict(file.diagnostics or {})
    source = diagnostics.get("source_metadata") or {}
    signals = diagnostics.get("metadata_signals") or {}
    if not isinstance(source, dict) or not isinstance(signals, dict):
        return None
    if source.get("identity_conflicts") or diagnostics.get("identity_conflicts"):
        return None
    comicinfo = source.get("comicinfo") or {}
    if not isinstance(comicinfo, dict):
        return None
    embedded_title = str(comicinfo.get("series") or "").strip()
    embedded_number = comicinfo.get("number")
    use_embedded = bool(
        embedded_title
        and embedded_number is not None
        and signals.get("series_name") == "comicinfo"
        and signals.get("issue_number") == "comicinfo"
    )
    use_sidecar = signals.get("series_name") == signals.get("issue_number") == "sidecar" and bool(
        file.parsed_series
    )
    name = file.file_name
    prefix = extract_story_arc_order_prefix(name)
    if prefix is not None:
        name = prefix.residual_file_name
    name = re.sub(r"\s*\(converted\)\s*", " ", name, flags=re.IGNORECASE)
    parsed = parse_release_title(name, expected_series=(embedded_title,) if use_embedded else ())
    if parsed is not None and parsed.is_pack:
        return None
    if use_embedded:
        title = embedded_title
        raw_number = embedded_number
        evidence = "comicinfo"
    elif use_sidecar:
        title = file.parsed_series or ""
        raw_number = file.issue_number_raw or file.parsed_issue_number
        evidence = "sidecar"
    else:
        if parsed is None or parsed.volume is not None or parsed.issue_number is None:
            return None
        title = parsed.series_name or ""
        raw_number = parsed.issue_number_text or parsed.issue_number
        evidence = "filename_parse"
    if not isinstance(raw_number, str | float | int):
        return None
    try:
        number = normalize_issue_number_text(raw_number)
    except (TypeError, ValueError):
        return None
    # Embedded metadata can be generated or stale; don't erase a contradictory
    # issue designation in an otherwise recognizable filename.
    if (
        use_embedded
        and parsed is not None
        and parsed.issue_number is not None
        and normalize_issue_number_text(parsed.issue_number_text or parsed.issue_number) != number
    ):
        return None
    if (
        use_embedded
        and parsed is not None
        and parsed.series_name
        and not NameMatcher().match(parsed.series_name, title).is_match
    ):
        return None
    issue_type = str(diagnostics.get("source_issue_type") or "issue")
    if parsed is not None and issue_type == "issue":
        issue_type = parsed.issue_type.value
    if issue_type in {"annual", "special"} and not NameMatcher.normalize(title).endswith(
        f" {issue_type}"
    ):
        title = f"{title} {issue_type.title()}"
    year = parsed.year if parsed is not None else None
    if not year and use_embedded:
        raw_year = comicinfo.get("year")
        if isinstance(raw_year, int) or (isinstance(raw_year, str) and raw_year.isdigit()):
            year = int(raw_year)
    if not title or (not year and evidence == "filename_parse"):
        return None
    return {
        "key": NameMatcher.normalize(title),
        "query": title,
        "issue_number": number,
        "year": year or 0,
        "issue_type": issue_type,
        "series_cv_id": diagnostics.get("comicvine_series_id")
        if signals.get("comicvine_series_id") in {"comicinfo", "sidecar"}
        else None,
        "issue_cv_id": file.comicvine_issue_id
        if signals.get("comicvine_issue_id") in {"comicinfo", "sidecar"}
        else None,
        "evidence": evidence,
    }


def catalog_target_agrees(identity: dict[str, Any], target: dict[str, Any]) -> bool:
    """Validate each file, even when lookup results were grouped across dates."""
    summary = target["summary"]
    target_type = str(summary.get("issue_type") or "issue")
    source_type = identity["issue_type"]
    # Dedicated Annual/Special volumes commonly catalog their numbered entries
    # as ordinary issues. Their exact qualified title still distinguishes them.
    type_agrees = target_type == source_type or (
        source_type in {"annual", "special"}
        and target_type == "issue"
        and identity["key"].endswith(f" {source_type}")
    )
    if (
        NameMatcher.normalize(str(target["title"])) != identity["key"]
        or not type_agrees
        or (identity.get("series_cv_id") and str(identity["series_cv_id"]) != str(target["cv_id"]))
        or (
            identity.get("issue_cv_id")
            and str(identity["issue_cv_id"]) != str(summary["provider_id"])
        )
    ):
        return False
    try:
        if (
            normalize_issue_number_text(summary.get("issue_number_text") or summary["issue_number"])
            != identity["issue_number"]
        ):
            return False
        release_date = summary.get("release_date")
        if identity["year"] and release_date:
            return abs(int(str(release_date)[:4]) - int(identity["year"])) <= 1
    except (TypeError, ValueError):
        return False
    return bool(identity["evidence"] != "filename_parse")


def record_catalog_review(file: ImportedFile, identity: dict[str, Any], reason: str) -> None:
    """Keep unresolved candidate evidence without changing any import decision."""
    file.diagnostics = {
        **file.diagnostics,
        "mixed_folder_recovery": {
            "candidate_series": identity["query"],
            "candidate_issue": identity["issue_number"],
            "evidence": identity["evidence"],
            "reason": reason,
        },
    }
