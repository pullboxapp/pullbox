"""Apply signed native issue decisions within the existing Follow-up worker."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select, update

from pullbox.core.exceptions import ValidationError
from pullbox.models.import_job import ImportControlRequest, ImportJob, ImportJobStatus
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile
from pullbox.providers.base import IssueSummary
from pullbox.services.import_native_issue_choices import native_issue_choice, native_review_context
from pullbox.services.import_review_file_assignment import apply_review_file_assignment
from pullbox.services.import_review_scope import verify_review_scope

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.import_job import ImportedFile, ImportedSeries
    from pullbox.schemas.import_job import RecoverOrphanRequest
    from pullbox.services.import_native_issue_choices import NativeIssueChoice


async def apply_native_orphan_decisions(
    session: AsyncSession,
    job: ImportJob,
    parent: ImportedSeries,
    files: list[ImportedFile],
    request: RecoverOrphanRequest,
    actor_id: int | None,
) -> None:
    from pullbox.services.import_orphans import (
        apply_orphan_recovery_decisions,
        requires_orphan_issue_decision,
    )

    if actor_id is None:
        raise ValidationError("Open Follow-up and review these issue choices before recovering.")
    active = {file.id: file for file in files if requires_orphan_issue_decision(file)}
    ids = [decision.imported_file_id for decision in request.decisions]
    if len(set(ids)) != len(ids) or set(ids) != set(active):
        raise ValidationError(
            "Review each remaining file once. Completed files cannot be reassigned."
        )
    revision = job.progress_revision
    choices: dict[int, NativeIssueChoice] = {}
    namespaces = {}
    # Validate the entire request before changing any decision or placing files.
    for decision in request.decisions:
        if decision.action == "skip":
            continue
        if not decision.library_issue_id or not decision.review_token or decision.issue_cv_id:
            raise ValidationError(
                "Choose a verified library issue from Follow-up before recovering."
            )
        _, _, file, series, namespace = await native_review_context(
            session, job.id, decision.imported_file_id, follow_up=True
        )
        issue = await session.get(Issue, decision.library_issue_id)
        if issue is None or issue.series_id != series.id:
            raise ValidationError(
                "The chosen issue does not belong to this verified library series."
            )
        choice = await native_issue_choice(session, job, parent, file, series, issue, actor_id)
        if not choice.eligible:
            raise ValidationError(
                "The source evidence or linked issue changed. "
                "Review the issue choice again, or skip this file."
            )
        await verify_review_scope(session, decision.review_token, choice.scope)
        if await session.scalar(
            select(LibraryFile.id).where(LibraryFile.issue_id == issue.id).limit(1)
        ):
            raise ValidationError(
                "This issue is already owned. Its library file was left unchanged; "
                "skip this alternate file or review the owned copy."
            )
        choices[file.id] = choice
        namespaces[file.id] = namespace
    claimed = await session.scalar(
        update(ImportJob)
        .where(
            ImportJob.id == job.id,
            ImportJob.status == ImportJobStatus.COMPLETED,
            ImportJob.control_request == ImportControlRequest.NONE,
            ImportJob.progress_revision == revision,
        )
        .values(progress_revision=ImportJob.progress_revision + 1)
        .returning(ImportJob.id)
    )
    if claimed is None:
        raise ValidationError("This import changed during review. Open Follow-up again.")
    for decision in request.decisions:
        file = active[decision.imported_file_id]
        if decision.action == "skip":
            apply_orphan_recovery_decisions(
                item=parent, files=files, decisions=[decision], cv_id_to_issue={}
            )
            file.include_in_import = False
            continue
        choice = choices[file.id]
        issue = choice.issue
        selection = choice.scope["selection"]
        external_id = next(
            str(identifier)
            for namespace, identifier in choice.scope["issue_links"]
            if namespace in ({"metron", "gcd"} if selection else {namespaces[file.id].value})
        )
        summary = IssueSummary(
            external_id,
            issue.issue_number,
            issue.title,
            issue.release_date.isoformat() if issue.release_date else None,
            issue.cover_url,
            issue.issue_type.value,
            issue.effective_issue_number_text,
        )
        await apply_review_file_assignment(
            session,
            job,
            parent,
            file,
            parent,
            issue,
            summary,
            issue_cv_id=issue.comicvine_id,
            method="library_review" if selection else f"{namespaces[file.id].value}_id",
        )
        if selection:
            file.diagnostics = {**file.diagnostics, "reviewed_library_issue": selection}
