"""Bounded saved-catalog choices for native import rows; no provider or file I/O."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select, update

from pullbox.core.exceptions import ValidationError
from pullbox.core.metadata_identity import IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue
from pullbox.models.metadata_identity import IssueExternalIdentity
from pullbox.providers.base import IssueSummary
from pullbox.services.import_file_match_targets import FileMatchTargetIndex
from pullbox.services.import_metron_matching import (
    import_identity_namespace,
    load_metron_issue_targets,
    metron_file_candidate,
    verified_native_import_series,
)
from pullbox.services.import_review_file_assignment import (
    apply_review_file_assignment,
    load_review_file,
)
from pullbox.services.import_review_scope import review_scope, verify_review_scope

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.series import Series


@dataclass
class NativeIssueChoice:
    issue: Issue
    eligible: bool
    scope: dict[str, Any]
    token: str = ""


async def native_review_context(
    session: AsyncSession, job_id: int, file_id: int, *, follow_up: bool = False
) -> tuple[ImportJob, ImportedSeries, ImportedFile, Series, IdentityNamespace]:
    if follow_up:
        from pullbox.core.exceptions import NotFoundError
        from pullbox.services.import_orphan_recovery_context import load_orphan_recovery_item

        file = await session.get(ImportedFile, file_id)
        if file is None or file.import_job_id != job_id:
            raise NotFoundError("ImportedFile", file_id)
        job, parent = await load_orphan_recovery_item(session, file.import_series_id)
        if job.control_request != ImportControlRequest.NONE:
            raise ValidationError("This import is busy. Try again after it finishes.")
    else:
        job, parent, file = await load_review_file(session, job_id, file_id)
    manual = parent.diagnostics.get("manual_library_series")
    namespace: IdentityNamespace | None
    if manual:
        from pullbox.models.series import Series
        from pullbox.services.import_library_selection import library_series_identity

        series = await session.get(Series, parent.series_id)
        if series is None or await library_series_identity(session, series) != manual:
            raise ValidationError(
                "The selected library series links changed. Review its metadata first."
            )
        from pullbox.core.metadata_identity import IdentityNamespace

        namespace = IdentityNamespace(manual["namespace"])
    else:
        series = await verified_native_import_series(session, parent)
        namespace = import_identity_namespace(parent.diagnostics.get("metron_import", {}))
    if (
        job.source_type != ImportSourceType.FILESYSTEM
        or (not follow_up and parent.status != ImportSeriesStatus.DUPLICATE)
        or parent.user_selected_cv_id is not None
        or series is None
        or series.id != parent.series_id
        or namespace is None
    ):
        raise ValidationError("Review the linked series metadata, then recheck its identity first.")
    if file.status not in {
        ImportedFileStatus.NO_MATCH,
        ImportedFileStatus.MATCHED,
        ImportedFileStatus.CONFIRMED,
        ImportedFileStatus.CONFLICT,
    }:
        raise ValidationError("Resolve source inspection before assigning this file.")
    return job, parent, file, series, namespace


async def native_issue_choice(
    session: AsyncSession,
    job: ImportJob,
    parent: ImportedSeries,
    file: ImportedFile,
    series: Series,
    issue: Issue,
    actor_id: int,
) -> NativeIssueChoice:
    index = FileMatchTargetIndex(existing_series=series, issue_entries=[(issue, False)])
    if issue.comicvine_id is not None:
        index.cv_id_map[issue.comicvine_id] = (
            issue.id,
            issue.comicvine_id,
            False,
            issue,
            issue.title,
        )
    await load_metron_issue_targets(session, index, [file])
    candidate, _ = metron_file_candidate(file, index)
    selection = None
    manual = parent.diagnostics.get("manual_library_series")
    from pullbox.services.import_library_selection import (
        has_compiled_series_choice,
        reviewed_issue_proof,
    )

    explicit_choice = bool(manual) or (candidate is None and has_compiled_series_choice(file))
    if explicit_choice:
        selection = reviewed_issue_proof(file, issue, index)
    return NativeIssueChoice(
        issue,
        (selection is not None if explicit_choice else candidate is not None)
        and issue.series_id == series.id,
        {
            "actor": actor_id,
            "action": "assign-native",
            "scope": review_scope(job, parent, file),
            "series_id": series.id,
            "issue_id": issue.id,
            "series_links": [list(key) for key in sorted(index.series_provider_ids)],
            "issue_links": [list(key) for key in sorted(index.provider_id_map)],
            "number": issue.effective_issue_number_text,
            "title": issue.title,
            "manual_series": manual,
            "selection": selection,
        },
    )


async def native_issue_page(
    session: AsyncSession,
    series_id: int,
    namespace: IdentityNamespace,
    page: int,
    query: str,
    *,
    include_other_native: bool = False,
) -> tuple[list[Issue], int, int]:
    verified = select(IssueExternalIdentity.issue_id).where(
        IssueExternalIdentity.identity_namespace.in_(
            [IdentityNamespace.METRON, IdentityNamespace.GCD]
        )
        if include_other_native
        else IssueExternalIdentity.identity_namespace == namespace,
        IssueExternalIdentity.verification_state == IdentityVerificationState.VERIFIED,
    )
    filters = [Issue.series_id == series_id, Issue.id.in_(verified)]
    if query.strip():
        text = query.strip().lower()
        filters.append(
            or_(
                func.lower(Issue.issue_number_text).contains(text, autoescape=True),
                func.lower(Issue.title).contains(text, autoescape=True),
            )
        )
    total = int(await session.scalar(select(func.count(Issue.id)).where(*filters)) or 0)
    pages = max(1, (total + 24) // 25)
    page = min(page, pages)
    issues = list(
        (
            await session.scalars(
                select(Issue)
                .where(*filters)
                .order_by(Issue.issue_number, Issue.issue_number_text, Issue.id)
                .offset((page - 1) * 25)
                .limit(25)
            )
        ).all()
    )
    return issues, page, pages


async def assign_native_issue(
    session: AsyncSession,
    job_id: int,
    file_id: int,
    issue_id: int,
    token: str,
    actor_id: int,
) -> ImportedFile:
    job, parent, file, series, namespace = await native_review_context(session, job_id, file_id)
    issue = await session.get(Issue, issue_id)
    if issue is None or issue.series_id != series.id:
        raise ValidationError("The chosen issue does not belong to this verified library series.")
    choice = await native_issue_choice(session, job, parent, file, series, issue, actor_id)
    if not choice.eligible:
        raise ValidationError(
            "The embedded identities or issue number do not agree with this verified issue. "
            "Correct the linked metadata or source evidence and recheck it, or skip this file."
        )
    await verify_review_scope(session, token, choice.scope)
    claimed = await session.scalar(
        update(ImportJob)
        .where(
            ImportJob.id == job_id,
            ImportJob.status == ImportJobStatus.REVIEW,
            ImportJob.control_request == ImportControlRequest.NONE,
        )
        .values(progress_revision=ImportJob.progress_revision + 1)
        .returning(ImportJob.id)
    )
    if claimed is None:
        raise ValidationError("This import is no longer available for review.")
    external_id = await session.scalar(
        select(IssueExternalIdentity.external_id)
        .where(
            IssueExternalIdentity.issue_id == issue.id,
            IssueExternalIdentity.identity_namespace.in_(
                [IdentityNamespace.METRON, IdentityNamespace.GCD]
            )
            if choice.scope["selection"]
            else IssueExternalIdentity.identity_namespace == namespace,
            IssueExternalIdentity.verification_state == IdentityVerificationState.VERIFIED,
        )
        .order_by(IssueExternalIdentity.identity_namespace, IssueExternalIdentity.external_id)
        .limit(1)
    )
    if external_id is None:
        raise ValidationError("This issue no longer has a verified provider identity. Recheck it.")
    summary = IssueSummary(
        external_id,
        issue.issue_number,
        issue.title,
        issue.release_date.isoformat() if issue.release_date else None,
        issue.cover_url,
        issue.issue_type.value,
        issue.effective_issue_number_text,
    )
    result = await apply_review_file_assignment(
        session,
        job,
        parent,
        file,
        parent,
        issue,
        summary,
        issue_cv_id=issue.comicvine_id,
        method="library_review" if choice.scope["selection"] else f"{namespace.value}_id",
    )
    if choice.scope["selection"]:
        result.diagnostics = {
            **result.diagnostics,
            "reviewed_library_issue": choice.scope["selection"],
        }
    return result
