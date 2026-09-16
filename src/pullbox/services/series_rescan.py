"""Read-only filesystem reconciliation for one existing catalog series."""

import os
import time
from collections import Counter
from pathlib import Path
from typing import Any

from pullbox.core.exceptions import ConfigurationError
from pullbox.core.file_safety import FileSafetyError, run_safety_checks
from pullbox.core.issue_numbers import normalize_issue_number_text
from pullbox.core.library_file_ownership import build_file_identity_signature
from pullbox.core.source_metadata import SourceMetadataExtractor
from pullbox.models.issue import IssueType
from pullbox.models.library import MatchConfidence
from pullbox.services.import_content_inspection import inspect_import_content
from pullbox.services.import_source_metadata import (
    build_import_metadata_conflict,
    corroborated_import_title_conflict,
)
from pullbox.services.semantic_matching import ImportPolicy, SemanticMatchEngine

COMIC_EXTENSIONS = frozenset({".cbz", ".cbr", ".cb7", ".cbt", ".pdf", ".epub"})


def _root_for(path: Path, context: dict[str, Any]) -> int:
    for root in sorted(context["roots"], key=lambda root: len(root["path"]), reverse=True):
        if path.is_relative_to(Path(root["path"]).resolve()):
            return int(root["id"])
    raise ValueError("This path is outside enabled roots that allow referencing existing files.")


def _inspect(path: Path, context: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {"file_path": str(path), "operation": "rescan", "outcome": "review"}
    try:
        if path.is_symlink():
            raise ValueError("Symbolic links need manual review; the rescan does not follow them.")
        path = path.resolve(strict=True)
        item["file_path"] = str(path)
        item["root_id"] = _root_for(path, context)
        if path.suffix.lower() not in context.get("extensions", COMIC_EXTENSIONS):
            raise ValueError("This file type is not enabled in the import settings.")
        signature = build_file_identity_signature(path)
        if time.time() - path.stat().st_mtime < 30:
            raise ValueError("File was modified recently. Wait for copying to finish, then rescan.")
        inspection = run_safety_checks(
            path,
            block_dangerous=context["block_dangerous"],
            max_archive_size=context["max_archive_size"],
        )
        content = inspect_import_content(path, inspection)
        if content.get("file_safety"):
            raise ValueError(
                "Archive has fewer than two pages. Review it through Import before adding it."
            )
        metadata = SourceMetadataExtractor().from_archive_path(path)
        series = context["series"]
        if metadata.comicvine_series_id not in {None, series["comicvine_id"]}:
            raise ValueError("Embedded or sidecar metadata identifies a different series.")
        candidates = [
            issue
            for issue in context["issues"]
            if (
                issue["cv_id"] == metadata.comicvine_issue_id
                if metadata.comicvine_issue_id
                else normalize_issue_number_text(issue["text"])
                == normalize_issue_number_text(metadata.issue_number_text or "")
            )
        ]
        if len(candidates) != 1:
            raise ValueError("No unique issue in this series matches the file's local identity.")
        issue = candidates[0]
        item["issue_id"] = issue["id"]
        item["target_number"] = issue["text"]
        item["target_cv_id"] = issue["cv_id"]
        conflict = build_import_metadata_conflict(
            metadata=metadata,
            target_series_title=series["title"],
            target_series_year=series["year_start"],
            target_issue_number=issue["number"],
            target_issue_cv_id=issue["cv_id"],
            target_issue_title=issue.get("title"),
        ) or corroborated_import_title_conflict(metadata, series["title"])
        if conflict:
            raise ValueError(str(conflict["rejection_reason"]))
        decision = SemanticMatchEngine(policy=ImportPolicy()).match_against_issue(
            metadata=metadata,
            wanted_series=series["title"],
            wanted_issue=issue["number"],
            wanted_issue_number_text=issue["text"],
            wanted_year=issue.get("year") or series["year_start"],
            wanted_issue_type=IssueType(issue["type"]),
            wanted_issue_cv_id=issue["cv_id"],
            wanted_issue_title=issue.get("title"),
        )
        if float(decision.match_diagnostics.get("series_similarity", 0)) >= 0.7:
            item["candidate_issue_id"] = issue["id"]
        if not decision.is_match or decision.confidence != MatchConfidence.HIGH:
            raise ValueError(
                decision.rejection_reason or "The local match needs review before registration."
            )
        if signature != build_file_identity_signature(path):
            raise ValueError(
                "File changed during inspection. Wait for copying to finish, then rescan."
            )
        item.update(
            outcome="register",
            issue_id=issue["id"],
            signature=signature,
            reason="Proven local match.",
        )
    except FileNotFoundError:
        item.update(
            missing=True,
            reason="File is missing. Check its mount or location; ownership was not changed.",
        )
    except (OSError, ValueError, ConfigurationError, FileSafetyError) as exc:
        item["reason"] = str(exc)
    return item


def plan_series_rescan(context: dict[str, Any]) -> list[dict[str, Any]]:
    """Inspect local candidates before deciding which registrations are unique."""
    folder = Path(context["folder"])
    try:
        folder = folder.resolve(strict=True)
        if not folder.is_dir():
            raise ValueError("Series folder is unavailable. Check the mount before rescanning.")
        _root_for(folder, context)
        if any(folder == Path(root["path"]).resolve() for root in context["roots"]):
            raise ValueError("Choose a series folder, not an entire library root.")
        paths: set[Path] = set()

        def fail_scan(error: OSError) -> None:
            raise error

        for parent, directories, names in os.walk(folder, followlinks=False, onerror=fail_scan):
            directories[:] = [name for name in directories if not name.startswith(".")]
            for name in names:
                path = Path(parent) / name
                if not name.startswith(".") and path.suffix.lower() in COMIC_EXTENSIONS:
                    paths.add(path)
    except OSError as exc:
        raise ValueError(
            "Series folder is unavailable or unreadable. Check the mount before rescanning."
        ) from exc
    # Check already-linked files outside this folder, but never enumerate their parents.
    paths.update(Path(record["path"]) for record in context["files"])
    items = [_inspect(path, context) for path in sorted(paths)]
    counts = Counter(item["candidate_issue_id"] for item in items if "candidate_issue_id" in item)
    registered_paths = {record["path"]: record["issue_id"] for record in context["files"]}
    for item in items:
        if "issue_id" in item and counts[item["issue_id"]] > 1:
            if registered_paths.get(item["file_path"]) == item["issue_id"]:
                continue
            item.update(
                outcome="review",
                reason="Multiple files match this issue. Choose a copy through Import review.",
            )
    replacements = {item["issue_id"] for item in items if item["outcome"] == "register"}
    # A proven replacement and its obsolete missing path are one repair, not an
    # additional warning that the user must resolve after that repair succeeds.
    return [
        item
        for item in items
        if not (item.get("missing") and registered_paths.get(item["file_path"]) in replacements)
    ]
