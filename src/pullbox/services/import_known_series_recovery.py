"""Recovery of legacy series rejections from agreeing saved local identities."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from hashlib import sha256
from itertools import batched
from typing import TYPE_CHECKING, Any

from sqlalchemy import exists, or_, select

from pullbox.core.issue_numbers import parse_issue_number_text
from pullbox.core.name_matcher import NameMatcher
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile
from pullbox.models.series import Series
from pullbox.providers.base import IssueSummary
from pullbox.services.import_file_match_targets import trusted_source_issue_identity_matches_target
from pullbox.services.import_file_selection import not_excluded_from_review
from pullbox.services.import_source_metadata import (
    build_import_metadata_conflict,
    source_metadata_for_import_file,
)
from pullbox.services.import_terminal_recovery import allows_terminal_import_recovery

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class KnownSeriesRecovery:
    file_id: int
    series_id: int
    cv_id: int
    match_method: str
    summary: dict[str, Any]
    evidence_digest: str


def _positive_id(value: object) -> int | None:
    try:
        number = int(str(value))
    except (ValueError, TypeError):
        return None
    return number if number > 0 else None


def _known_identity(
    item: ImportedSeries,
    files: list[ImportedFile],
    source_type: ImportSourceType,
) -> tuple[int, str] | None:
    if (
        item.user_selected_cv_id is not None
        or item.series_id is not None
        or any(
            file.status in {ImportedFileStatus.CONFLICT, ImportedFileStatus.IMPORTED}
            for file in files
        )
    ):
        return None
    diagnostics = dict(item.diagnostics or {})
    candidate = diagnostics.get("selected_candidate")
    candidate = candidate if isinstance(candidate, dict) else {}
    cv_id = _positive_id(candidate.get("cv_id"))
    method = str(candidate.get("match_method") or "")
    if candidate.get("title") and NameMatcher.normalize(
        str(candidate["title"])
    ) != NameMatcher.normalize(item.raw_series_name):
        return None
    if source_type is ImportSourceType.MYLAR3:
        mylar_ids = {
            _positive_id(file.diagnostics.get("comicvine_series_id"))
            for file in files
            if isinstance(file.diagnostics, dict)
            and isinstance(file.diagnostics.get("metadata_signals"), dict)
            and file.diagnostics["metadata_signals"].get("comicvine_series_id") == "mylar3"
        } - {None}
        if len(mylar_ids) != 1:
            return None
        mylar_id = next(iter(mylar_ids))
        if cv_id is not None and (cv_id != mylar_id or method != "mylar3_cv_id"):
            return None
        if item.cv_id not in (None, mylar_id):
            return None
        assert mylar_id is not None
        return mylar_id, "mylar3_cv_id"
    if cv_id is None or method not in {"comicinfo_cv_id", "folder_cv_id"}:
        return None
    if item.cv_id not in (None, cv_id):
        return None
    # Folder imports have no authoritative Mylar row to resolve series disagreements.
    conflicts = diagnostics.get("identity_conflicts", [])
    if not isinstance(conflicts, list) or any(
        not isinstance(conflict, dict) or conflict.get("field") != "comicvine_issue_id"
        for conflict in conflicts
    ):
        return None
    return cv_id, method


def _file_plan(
    item: ImportedSeries, file: ImportedFile, cv_id: int, method: str
) -> KnownSeriesRecovery | None:
    if file.status not in {
        ImportedFileStatus.MATCHED,
        ImportedFileStatus.CONFIRMED,
        ImportedFileStatus.NO_MATCH,
    }:
        return None
    diagnostics = dict(file.diagnostics or {})
    if diagnostics.get("review_selection") is False:
        return None
    if diagnostics.get("kind") in {
        "metadata_conflict",
        "source_scope_review",
        "source_layout_review",
    }:
        return None
    if file.library_file_id is not None or file.conflict_group_id is not None:
        return None
    if (file.match_method or "").startswith(("manual", "orphan_recovery")):
        return None
    if any(
        diagnostics.get(key)
        for key in ("safety_block", "source_revalidation", "safety_exception", "safety_review")
    ):
        return None
    if (
        file.status is ImportedFileStatus.NO_MATCH
        and diagnostics.get("kind") != "series_no_match_file"
    ):
        return None
    target = ImportedSeries(
        raw_series_name=item.raw_series_name,
        raw_year=item.raw_year,
        cv_id=cv_id,
        cv_match_method=method,
    )
    if not trusted_source_issue_identity_matches_target(target, file, file.comicvine_issue_id):
        return None
    if file.matched_issue_cv_id not in (None, file.comicvine_issue_id):
        return None
    metadata = source_metadata_for_import_file(target, file)
    if metadata.issue_number is None:
        return None
    if (
        build_import_metadata_conflict(
            metadata=metadata,
            target_series_title=item.raw_series_name,
            target_series_year=item.raw_year,
            target_issue_number=metadata.issue_number,
            target_issue_cv_id=file.comicvine_issue_id,
            target_issue_title=None,
        )
        is not None
    ):
        return None
    raw_summary = diagnostics.get("target_issue_summary")
    if raw_summary is not None:
        if (
            not isinstance(raw_summary, dict)
            or _positive_id(raw_summary.get("provider_id")) != file.comicvine_issue_id
        ):
            return None
        try:
            number, number_text = parse_issue_number_text(
                str(raw_summary.get("issue_number_text") or raw_summary.get("issue_number"))
            )
        except ValueError:
            return None
        if number != metadata.issue_number:
            return None
        summary = dict(raw_summary)
    else:
        if file.status is not ImportedFileStatus.NO_MATCH:
            return None
        number, number_text = parse_issue_number_text(str(metadata.issue_number))
        summary = asdict(
            IssueSummary(
                provider_id=str(file.comicvine_issue_id),
                issue_number=number,
                issue_number_text=number_text,
                title=None,
                release_date=None,
                cover_url=None,
                issue_type=metadata.issue_type.value,
            )
        )
    evidence = {
        "series": [item.id, item.updated_at.isoformat(), item.diagnostics],
        "file": [file.id, file.updated_at.isoformat(), file.diagnostics, file.source_signature],
        "identity": [cv_id, method, file.matched_issue_id, file.matched_issue_cv_id],
    }
    return KnownSeriesRecovery(
        file.id,
        item.id,
        cv_id,
        method,
        summary,
        sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest(),
    )


async def load_known_series_recovery(
    session: AsyncSession,
    job_id: int,
) -> tuple[KnownSeriesRecovery, ...]:
    """Return only exact recoverable file identities without modifying the session."""
    job = await session.get(ImportJob, job_id)
    if job is None or not allows_terminal_import_recovery(job):
        return ()
    plans: list[KnownSeriesRecovery] = []
    matched_local_ids: dict[int, int | None] = {}
    cursor = 0
    while True:
        items = list(
            (
                await session.scalars(
                    select(ImportedSeries)
                    .where(
                        ImportedSeries.import_job_id == job_id,
                        ImportedSeries.id > cursor,
                        ImportedSeries.status.in_(
                            (ImportSeriesStatus.NO_MATCH, ImportSeriesStatus.FAILED)
                        ),
                        ImportedSeries.diagnostics["reason"].as_string()
                        == "trusted_source_identity_conflict",
                    )
                    .order_by(ImportedSeries.id)
                    .limit(100)
                )
            ).all()
        )
        if not items:
            break
        cursor = items[-1].id
        for item in items:
            files = list(
                (
                    await session.scalars(
                        select(ImportedFile)
                        .where(
                            ImportedFile.import_series_id == item.id,
                        )
                        .order_by(ImportedFile.id)
                    )
                ).all()
            )
            identity = _known_identity(item, files, job.source_type)
            if identity is None:
                continue
            for file in files:
                plan = _file_plan(item, file, *identity)
                if plan is not None:
                    plans.append(plan)
                    matched_local_ids[file.id] = file.matched_issue_id
    issue_ids = sorted({int(plan.summary["provider_id"]) for plan in plans})
    local_targets = {}
    ready_claims: dict[int, set[int]] = defaultdict(set)
    for ids in batched(issue_ids, 300):
        for file_id, source_cv_id, target_cv_id, local_cv_id in (
            await session.execute(
                select(
                    ImportedFile.id,
                    ImportedFile.comicvine_issue_id,
                    ImportedFile.matched_issue_cv_id,
                    Issue.comicvine_id,
                )
                .outerjoin(Issue, Issue.id == ImportedFile.matched_issue_id)
                .where(
                    ImportedFile.import_job_id == job_id,
                    ImportedFile.status.in_(
                        (ImportedFileStatus.MATCHED, ImportedFileStatus.CONFIRMED)
                    ),
                    not_excluded_from_review(),
                    or_(
                        ImportedFile.comicvine_issue_id.in_(ids),
                        ImportedFile.matched_issue_cv_id.in_(ids),
                        Issue.comicvine_id.in_(ids),
                    ),
                )
            )
        ).all():
            for claimed_id in (source_cv_id, target_cv_id, local_cv_id):
                if claimed_id is not None:
                    ready_claims[claimed_id].add(file_id)
        for issue, series_cv_id, owned in (
            await session.execute(
                select(Issue, Series.comicvine_id, exists().where(LibraryFile.issue_id == Issue.id))
                .join(Series, Series.id == Issue.series_id)
                .where(Issue.comicvine_id.in_(ids))
            )
        ).all():
            local_targets[issue.comicvine_id] = (issue, series_cv_id, owned)
    # Comic Vine issue IDs are globally unique, including across legacy series rows.
    counts = Counter(int(plan.summary["provider_id"]) for plan in plans)
    result = []
    for plan in plans:
        issue_cv_id = int(plan.summary["provider_id"])
        if counts[issue_cv_id] != 1 or ready_claims[issue_cv_id] - {plan.file_id}:
            continue
        local = local_targets.get(int(plan.summary["provider_id"]))
        if local is not None:
            issue, series_cv_id, owned = local
            if (
                series_cv_id != plan.cv_id
                or owned
                or issue.issue_number != plan.summary["issue_number"]
                or matched_local_ids[plan.file_id] not in (None, issue.id)
            ):
                continue
        elif matched_local_ids[plan.file_id] is not None:
            continue
        result.append(plan)
    # Apply isolates these files into new scoped groups. Unpreviewed siblings
    # stay in their original parent and are never authorized by this recovery.
    return tuple(sorted(result, key=lambda plan: plan.file_id))
