"""Reconcile late-discovered exact identities before import copy grouping."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

from sqlalchemy import select

from pullbox.core.issue_numbers import format_issue_number, normalize_issue_number_text
from pullbox.models.import_job import ImportedFile, ImportedFileStatus
from pullbox.models.issue import IssueType
from pullbox.services.import_file_issue_signals import (
    candidate_issue_number,
    candidate_issue_number_text,
)
from pullbox.services.import_file_match_targets import (
    PROVIDER_MISSING_ISSUE_PLACEHOLDER_KIND,
    PROVIDER_MISSING_ISSUE_PLACEHOLDER_METHOD,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.import_job import ImportedSeries, ImportJob

_PAGE_SIZE = 250
_IssueKey = tuple[str, IssueType]


class _ExactTarget(NamedTuple):
    file_id: int
    issue_id: int | None
    issue_cv_id: int
    summary: dict[str, Any]


def _source_key(file: ImportedFile, series_cv_id: int) -> _IssueKey | None:
    diagnostics = file.diagnostics or {}
    metadata = diagnostics.get("source_metadata") or {}
    if not isinstance(metadata, dict):
        return None
    if (
        diagnostics.get("safety_block")
        or diagnostics.get("review_deferred")
        or metadata.get("identity_conflicts")
        or metadata.get("mylar3_folder_scope_conflict")
        or diagnostics.get("comicvine_series_id") not in (None, series_cv_id)
    ):
        return None
    number = candidate_issue_number(file)
    if number is None:
        return None
    text = candidate_issue_number_text(file)
    if file.issue_number_raw and text is None:
        return None
    try:
        issue_type = IssueType(diagnostics.get("source_issue_type"))
        return text or format_issue_number(number), issue_type
    except (TypeError, ValueError):
        return None


def _provisional_key(file: ImportedFile, series_cv_id: int) -> _IssueKey | None:
    diagnostics = file.diagnostics or {}
    key = _source_key(file, series_cv_id)
    if (
        key is None
        or diagnostics.get("kind") != PROVIDER_MISSING_ISSUE_PLACEHOLDER_KIND
        or diagnostics.get("target_state") != "provisional_issue_target"
        or diagnostics.get("target_series_cv_id") != series_cv_id
        or diagnostics.get("target_issue_number") != candidate_issue_number(file)
        or diagnostics.get("target_issue_type") != key[1].value
        or diagnostics.get("resolution")
    ):
        return None
    return key


def _exact_target(file: ImportedFile, series_cv_id: int) -> tuple[_IssueKey, _ExactTarget] | None:
    key = _source_key(file, series_cv_id)
    summary = (file.diagnostics or {}).get("target_issue_summary")
    if key is None or not isinstance(summary, dict) or file.matched_issue_cv_id is None:
        return None
    if (
        str(summary.get("provider_id")) != str(file.matched_issue_cv_id)
        or summary.get("issue_type") != key[1].value
        or summary.get("issue_number") != candidate_issue_number(file)
    ):
        return None
    try:
        text = normalize_issue_number_text(
            summary.get("issue_number_text") or summary["issue_number"]
        )
    except (TypeError, ValueError):
        return None
    if text != key[0]:
        return None
    return key, _ExactTarget(
        file.id, file.matched_issue_id, file.matched_issue_cv_id, dict(summary)
    )


async def reconcile_provisional_targets(
    session: AsyncSession,
    job: ImportJob,
    series: ImportedSeries,
    *,
    raise_if_cancelled: Callable[[AsyncSession, int], Awaitable[None]],
) -> int:
    """Join only unambiguous same-series, same-designation, same-type targets.

    Run after every file page has been evaluated so archive order cannot hide a
    competing copy. Reads remain bounded and never access source files/providers.
    """
    series_cv_id = series.user_selected_cv_id or series.cv_id
    if series_cv_id is None:
        return 0
    scope = (
        ImportedFile.import_job_id == job.id,
        ImportedFile.import_series_id == series.id,
        ImportedFile.status == ImportedFileStatus.MATCHED,
    )
    after_id = 0
    reconciled = 0
    while True:
        await raise_if_cancelled(session, job.id)
        result = await session.execute(
            select(ImportedFile)
            .where(
                *scope,
                ImportedFile.id > after_id,
                ImportedFile.match_method == PROVIDER_MISSING_ISSUE_PLACEHOLDER_METHOD,
                ImportedFile.matched_issue_id.is_(None),
                ImportedFile.matched_issue_cv_id.is_(None),
                ImportedFile.comicvine_issue_id.is_(None),
            )
            .order_by(ImportedFile.id)
            .limit(_PAGE_SIZE)
        )
        page = list(result.scalars())
        if not page:
            break
        after_id = page[-1].id
        keys = {file.id: _provisional_key(file, series_cv_id) for file in page}
        requested = {key for key in keys.values() if key is not None}
        numbers = {file.parsed_issue_number for file in page if keys[file.id] is not None}
        if not requested:
            continue
        targets: dict[_IssueKey, _ExactTarget | None] = {}
        target_after_id = 0
        while True:
            await raise_if_cancelled(session, job.id)
            result = await session.execute(
                select(ImportedFile)
                .where(
                    *scope,
                    ImportedFile.id > target_after_id,
                    ImportedFile.matched_issue_cv_id > 0,
                    ImportedFile.parsed_issue_number.in_(numbers),
                )
                .order_by(ImportedFile.id)
                .limit(_PAGE_SIZE)
            )
            candidates = list(result.scalars())
            if not candidates:
                break
            target_after_id = candidates[-1].id
            for candidate in candidates:
                target = _exact_target(candidate, series_cv_id)
                if target is None or target[0] not in requested:
                    continue
                key, exact = target
                if key not in targets:
                    targets[key] = exact
                else:
                    previous = targets[key]
                    if previous is None or (previous.issue_id, previous.issue_cv_id) != (
                        exact.issue_id,
                        exact.issue_cv_id,
                    ):
                        targets[key] = None
        await raise_if_cancelled(session, job.id)
        for file in page:
            key = keys[file.id]
            exact = targets.get(key) if key is not None else None
            if exact is None:
                continue
            diagnostics = dict(file.diagnostics or {})
            for field in (
                "kind",
                "target_state",
                "target_series_cv_id",
                "target_series_title",
                "target_series_issue_count",
                "target_issue_number",
                "target_issue_type",
                "target_issue_title",
                "rejection_reason",
            ):
                diagnostics.pop(field, None)
            diagnostics["target_issue_summary"] = dict(exact.summary)
            diagnostics["provisional_target_reconciliation"] = {
                "evidence_file_id": exact.file_id,
                "matched_issue_cv_id": exact.issue_cv_id,
                "previous_match_method": file.match_method,
            }
            file.matched_issue_id = exact.issue_id
            file.matched_issue_cv_id = exact.issue_cv_id
            file.match_method = "issue_number"
            file.diagnostics = diagnostics
            reconciled += 1
        await session.flush()
    return reconciled
