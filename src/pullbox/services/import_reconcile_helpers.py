"""Pure helpers for Step 3 import issue reconciliation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.core.issue_numbers import format_issue_number
from pullbox.core.name_matcher import NameMatcher
from pullbox.core.release_parser import parse_release_title
from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries
from pullbox.models.issue import Issue, IssueType
from pullbox.services.import_file_match_targets import (
    PROVIDER_MISSING_ISSUE_PLACEHOLDER_KIND,
    PROVIDER_MISSING_ISSUE_PLACEHOLDER_METHOD,
)
from pullbox.services.import_file_preparation import format_comicinfo_issue_number

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.schemas.import_job import ImportReconcileDecision
    from pullbox.services.metadata_service import MetadataService


async def issue_options_for_reconcile_series(
    session: AsyncSession,
    item: ImportedSeries,
    metadata_service: MetadataService,
) -> tuple[list[dict[str, Any]], dict[int, Issue]]:
    """Load issue choices for either an existing series or a selected CV volume."""
    issue_options: list[dict[str, Any]] = []
    local_issue_by_cv_id: dict[int, Issue] = {}

    if item.series_id is not None:
        issues_result = await session.execute(
            select(Issue)
            .options(joinedload(Issue.library_file))
            .where(Issue.series_id == item.series_id)
            .order_by(Issue.issue_number)
        )
        for issue in issues_result.scalars().all():
            if issue.comicvine_id is None:
                continue
            issue_options.append(
                {
                    "issue_cv_id": issue.comicvine_id,
                    "issue_number": issue.issue_number,
                    "title": issue.title,
                    "release_date": (
                        issue.release_date.isoformat() if issue.release_date is not None else None
                    ),
                    "cover_url": issue.cover_url,
                    "issue_type": issue.issue_type.value,
                    "already_imported": issue.library_file is not None,
                }
            )
            local_issue_by_cv_id[issue.comicvine_id] = issue
        return issue_options, local_issue_by_cv_id

    cv_id = item.cv_id or item.user_selected_cv_id
    if cv_id is None:
        from pullbox.core.exceptions import ValidationError

        raise ValidationError("Choose a ComicVine match before reconciling files.")

    summaries = await metadata_service.get_issue_summaries_for_series(cv_id)
    for summary in summaries:
        if not summary.provider_id:
            continue
        issue_options.append(
            {
                "issue_cv_id": int(summary.provider_id),
                "issue_number": summary.issue_number,
                "title": summary.title,
                "release_date": summary.release_date,
                "cover_url": summary.cover_url,
                "issue_type": summary.issue_type,
                "already_imported": False,
            }
        )
    return issue_options, local_issue_by_cv_id


def archive_entry_issue_number(imp_file: ImportedFile) -> float | None:
    """Return a strong archive-entry issue hint when one was captured during scan."""
    diagnostics = dict(imp_file.diagnostics or {})
    source_metadata = diagnostics.get("source_metadata")
    hint = diagnostics.get("archive_entry_issue_hint")
    if not isinstance(hint, dict) and isinstance(source_metadata, dict):
        hint = source_metadata.get("archive_entry_issue_hint")
    if not isinstance(hint, dict) or hint.get("confidence") != "strong":
        return None
    try:
        return float(hint["issue_number"])
    except (TypeError, ValueError, KeyError):
        return None


def coerce_issue_type(raw_issue_type: object) -> IssueType | None:
    """Return a known IssueType enum when a metadata value maps cleanly."""
    if not raw_issue_type:
        return None
    try:
        return IssueType(str(raw_issue_type))
    except ValueError:
        return None


def provisional_issue_type_for_file(imp_file: ImportedFile) -> IssueType:
    """Infer the local provisional issue type to create for a reconciled file."""
    diagnostics = dict(imp_file.diagnostics or {})
    issue_type = coerce_issue_type(diagnostics.get("source_issue_type"))
    if issue_type is not None:
        return issue_type

    source_metadata = diagnostics.get("source_metadata")
    filename_parse = (
        source_metadata.get("filename_parse") if isinstance(source_metadata, dict) else None
    )
    issue_type = coerce_issue_type(
        filename_parse.get("issue_type") if isinstance(filename_parse, dict) else None
    )
    return issue_type or IssueType.ISSUE


def provisional_issue_number_for_file(
    item: ImportedSeries,
    imp_file: ImportedFile,
    issue_options: list[dict[str, Any]],
) -> float | None:
    """Return a safe local issue number when the selected provider series is missing it."""
    if imp_file.status not in {ImportedFileStatus.NO_MATCH, ImportedFileStatus.PENDING}:
        return None
    if item.cv_id is None and item.series_id is None and item.user_selected_cv_id is None:
        return None

    diagnostics = dict(imp_file.diagnostics or {})
    if diagnostics.get("kind") == "metadata_conflict" or diagnostics.get("conflict_type"):
        return None

    issue_number = archive_entry_issue_number(imp_file)
    if issue_number is None:
        issue_number = imp_file.parsed_issue_number
    if issue_number is None:
        try:
            requested_issue_number = diagnostics.get("requested_issue_number")
            if not isinstance(requested_issue_number, int | float | str):
                issue_number = None
            else:
                issue_number = float(requested_issue_number)
        except (TypeError, ValueError):
            issue_number = None
    if issue_number is None:
        issue_number = filename_issue_number_for_selected_series(item, imp_file)
    if issue_number is None or issue_number <= 0:
        return None

    for option in issue_options:
        try:
            option_number = float(option["issue_number"])
        except (TypeError, ValueError, KeyError):
            continue
        if abs(option_number - issue_number) <= 0.0001:
            return None
    return float(issue_number)


def filename_issue_number_for_selected_series(
    item: ImportedSeries,
    imp_file: ImportedFile,
) -> float | None:
    """Recover issue number from filename once the user has selected a series."""
    parsed = parse_release_title(imp_file.file_name or "")
    if parsed is None or parsed.issue_number is None or not parsed.series_name:
        return None

    target_title = item.cv_title or item.raw_series_name
    if not target_title:
        return None
    if NameMatcher.normalize(parsed.series_name) != NameMatcher.normalize(target_title):
        return None
    return float(parsed.issue_number)


def apply_reconcile_decisions(
    *,
    item: ImportedSeries,
    files: list[ImportedFile],
    decisions: list[ImportReconcileDecision],
    issue_options: list[dict[str, Any]],
    local_issue_by_cv_id: dict[int, Issue],
    provisional_issue_number_for_file: Callable[
        [ImportedSeries, ImportedFile, list[dict[str, Any]]],
        float | None,
    ],
    provisional_issue_type_for_file: Callable[[ImportedFile], IssueType],
) -> None:
    """Apply Step 3 issue reconciliation decisions to imported file rows."""
    files_by_id = {imp_file.id: imp_file for imp_file in files}
    valid_issue_cv_ids = {int(option["issue_cv_id"]) for option in issue_options}
    issue_options_by_cv_id = {int(option["issue_cv_id"]): option for option in issue_options}
    provisional_issue_number_by_file_id = {
        imp_file.id: provisional_issue_number_for_file(item, imp_file, issue_options)
        for imp_file in files
        if imp_file.id is not None
    }

    for decision in decisions:
        imp_file = files_by_id.get(decision.imported_file_id)
        if imp_file is None or imp_file.import_series_id != item.id:
            raise NotFoundError("ImportedFile", decision.imported_file_id)
        if imp_file.status not in {ImportedFileStatus.NO_MATCH, ImportedFileStatus.PENDING}:
            continue

        if decision.action == "skip":
            imp_file.status = ImportedFileStatus.SKIPPED
            imp_file.matched_issue_id = None
            imp_file.matched_issue_cv_id = None
            imp_file.match_confidence = None
            imp_file.match_method = "import_reconcile_skip"
            imp_file.include_in_import = False
            imp_file.error_message = None
            imp_file.diagnostics = {
                **dict(imp_file.diagnostics or {}),
                "kind": "import_reconcile",
                "resolution": "skipped",
            }
            continue

        if decision.action == "provisional":
            provisional_issue_number = provisional_issue_number_by_file_id.get(imp_file.id)
            if provisional_issue_number is None:
                raise ValidationError(
                    "A provisional issue can only be created when the selected series "
                    "has no matching provider issue for this file."
                )
            if (
                decision.provisional_issue_number is not None
                and abs(decision.provisional_issue_number - provisional_issue_number) > 0.0001
            ):
                raise ValidationError(
                    "The requested provisional issue number does not match this file."
                )

            issue_type = provisional_issue_type_for_file(imp_file)
            imp_file.status = ImportedFileStatus.MATCHED
            imp_file.matched_issue_id = None
            imp_file.matched_issue_cv_id = None
            imp_file.match_confidence = "manual"
            imp_file.match_method = PROVIDER_MISSING_ISSUE_PLACEHOLDER_METHOD
            imp_file.include_in_import = False
            imp_file.error_message = None
            imp_file.diagnostics = {
                **dict(imp_file.diagnostics or {}),
                "kind": PROVIDER_MISSING_ISSUE_PLACEHOLDER_KIND,
                "target_state": "provisional_issue_target",
                "target_series_cv_id": item.user_selected_cv_id or item.cv_id,
                "target_series_id": item.series_id,
                "target_series_title": item.cv_title or item.raw_series_name,
                "target_series_issue_count": item.cv_issue_count,
                "target_issue_number": provisional_issue_number,
                "target_issue_type": issue_type.value,
                "target_issue_title": None,
                "resolution": "provisional_issue_created",
                "rejection_reason": (
                    "ComicVine does not have this issue yet; Pullbox will create "
                    "a local provisional issue for this import."
                ),
            }
            continue

        if decision.issue_cv_id is None:
            raise ValidationError("Assigned reconciliation decisions require an issue.")
        if decision.issue_cv_id not in valid_issue_cv_ids:
            raise ValidationError(
                f"ComicVine issue {decision.issue_cv_id} is not available for this series."
            )

        issue = local_issue_by_cv_id.get(decision.issue_cv_id)
        issue_option = issue_options_by_cv_id[decision.issue_cv_id]
        imp_file.status = ImportedFileStatus.MATCHED
        imp_file.matched_issue_id = issue.id if issue is not None else None
        imp_file.matched_issue_cv_id = decision.issue_cv_id
        imp_file.match_confidence = "manual"
        imp_file.match_method = "import_reconcile"
        imp_file.include_in_import = False
        imp_file.error_message = None
        imp_file.diagnostics = {
            **dict(imp_file.diagnostics or {}),
            "kind": "import_reconcile",
            "resolution": "assigned",
            "target_issue_summary": {
                "provider_id": str(decision.issue_cv_id),
                "issue_number": float(issue_option["issue_number"]),
                "title": issue_option.get("title"),
                "release_date": issue_option.get("release_date"),
                "cover_url": issue_option.get("cover_url"),
                "issue_type": str(issue_option.get("issue_type") or IssueType.ISSUE.value),
            },
        }


def build_reconcile_file_rows(
    item: ImportedSeries,
    files: list[ImportedFile],
    issue_options: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    """Build modal rows and suggested issue assignments for review-time reconcile."""
    issue_label_by_cv_id: dict[int, str] = {}
    issue_number_to_cv_ids: dict[float, list[int]] = {}
    for option in issue_options:
        issue_cv_id = int(option["issue_cv_id"])
        issue_number = float(option["issue_number"])
        label = f"#{format_issue_number(issue_number)}"
        if option.get("title"):
            label = f"{label} - {option['title']}"
        issue_label_by_cv_id[issue_cv_id] = label
        issue_number_to_cv_ids.setdefault(issue_number, []).append(issue_cv_id)

    file_rows: list[dict[str, Any]] = []
    files_remaining = 0
    files_completed = 0
    unresolved_statuses = {ImportedFileStatus.NO_MATCH, ImportedFileStatus.PENDING}
    for imp_file in files:
        current_issue_cv_id = (
            imp_file.matched_issue_cv_id
            if imp_file.matched_issue_cv_id in issue_label_by_cv_id
            else None
        )
        suggested_issue_cv_id = current_issue_cv_id
        if suggested_issue_cv_id is None and imp_file.comicvine_issue_id in issue_label_by_cv_id:
            suggested_issue_cv_id = imp_file.comicvine_issue_id
        archive_issue_number = archive_entry_issue_number(imp_file)
        if (
            suggested_issue_cv_id is None
            and archive_issue_number is not None
            and len(issue_number_to_cv_ids.get(archive_issue_number, [])) == 1
        ):
            suggested_issue_cv_id = issue_number_to_cv_ids[archive_issue_number][0]
        if (
            suggested_issue_cv_id is None
            and imp_file.parsed_issue_number is not None
            and len(issue_number_to_cv_ids.get(imp_file.parsed_issue_number, [])) == 1
        ):
            suggested_issue_cv_id = issue_number_to_cv_ids[imp_file.parsed_issue_number][0]
        provisional_issue_number = provisional_issue_number_for_file(
            item,
            imp_file,
            issue_options,
        )

        decision_locked = imp_file.status not in unresolved_statuses
        if decision_locked:
            files_completed += 1
        else:
            files_remaining += 1

        file_rows.append(
            {
                "imported_file_id": imp_file.id,
                "file_name": imp_file.file_name,
                "file_path": imp_file.file_path,
                "file_format": imp_file.file_format,
                "parsed_issue_number": imp_file.parsed_issue_number,
                "archive_entry_issue_number": archive_issue_number,
                "parsed_year": imp_file.parsed_year,
                "comicvine_issue_id": imp_file.comicvine_issue_id,
                "status": imp_file.status,
                "error_message": imp_file.error_message,
                "matched_issue_cv_id": current_issue_cv_id,
                "suggested_issue_cv_id": suggested_issue_cv_id,
                "suggested_issue_label": (
                    issue_label_by_cv_id.get(suggested_issue_cv_id)
                    if suggested_issue_cv_id is not None
                    else None
                ),
                "can_create_provisional_issue": provisional_issue_number is not None,
                "provisional_issue_number": provisional_issue_number,
                "provisional_issue_label": (
                    f"#{format_comicinfo_issue_number(provisional_issue_number)}"
                    if provisional_issue_number is not None
                    else None
                ),
                "decision_locked": decision_locked,
                "diagnostics": dict(imp_file.diagnostics or {}),
            }
        )
    return file_rows, files_remaining, files_completed
