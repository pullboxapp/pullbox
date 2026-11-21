"""Explicit saved-library choices for folder imports, not inferred XML identities."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, update

from pullbox.core.exceptions import ValidationError
from pullbox.core.issue_numbers import normalize_issue_number_text
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
from pullbox.models.metadata_identity import SeriesExternalIdentity
from pullbox.models.series import Series
from pullbox.services.import_counters import recompute_file_counters, recompute_series_counters
from pullbox.services.import_metron_matching import file_metron_evidence
from pullbox.services.import_review_scope import verify_review_scope
from pullbox.services.import_review_series_choice import series_choice_scope

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

    from pullbox.models.issue import Issue
    from pullbox.services.import_file_match_candidates import FileMatchCandidate
    from pullbox.services.import_file_match_targets import FileMatchTargetIndex


async def library_series_identity(session: AsyncSession, series: Series) -> dict[str, Any]:
    links = (
        await session.scalars(
            select(SeriesExternalIdentity).where(
                SeriesExternalIdentity.series_id == series.id,
                SeriesExternalIdentity.verification_state == IdentityVerificationState.VERIFIED,
            )
        )
    ).all()
    keys = {(link.identity_namespace.value, link.external_id) for link in links}
    if series.comicvine_id is not None:
        keys.add(("comicvine", str(series.comicvine_id)))
    native = sorted(namespace for namespace, _ in keys if namespace in {"metron", "gcd"})
    if not native:
        raise ValidationError("This series needs a verified Metron or GCD identity first.")
    namespace = series.metadata_source if series.metadata_source in native else native[0]
    return {
        "series_id": series.id,
        "namespace": namespace,
        "links": [list(key) for key in sorted(keys)],
    }


async def library_selection_context(
    session: AsyncSession, job_id: int, series_id: int, actor_id: int
) -> tuple[ImportJob, ImportedSeries, dict[str, Any]]:
    job, parent, scope = await series_choice_scope(session, job_id, series_id, "skip", actor_id)
    if (
        job.source_type != ImportSourceType.FILESYSTEM
        or parent.status not in {ImportSeriesStatus.NO_MATCH, ImportSeriesStatus.DUPLICATE}
        or parent.diagnostics.get("metron_import")
        or (
            parent.status == ImportSeriesStatus.DUPLICATE
            and not parent.diagnostics.get("manual_library_series")
        )
    ):
        raise ValidationError("Use the existing identity review for this series.")
    statuses = (
        await session.scalars(
            select(ImportedFile.status).where(ImportedFile.import_series_id == parent.id)
        )
    ).all()
    if any(
        status
        not in {
            ImportedFileStatus.NO_MATCH,
            ImportedFileStatus.PENDING,
            ImportedFileStatus.SKIPPED,
            ImportedFileStatus.SAFETY_BLOCKED,
        }
        for status in statuses
    ):
        raise ValidationError("This series already has file decisions. Review those matches first.")
    digest = hashlib.sha256(json.dumps(scope, sort_keys=True, default=str).encode()).hexdigest()
    return job, parent, {"action": "library-series", "actor": actor_id, "scope": digest}


async def library_series_page(
    session: AsyncSession, query: str, page: int, *, series_id: int | None = None
) -> tuple[list[Series], int, int]:
    verified = select(SeriesExternalIdentity.series_id).where(
        SeriesExternalIdentity.identity_namespace.in_(
            [IdentityNamespace.METRON, IdentityNamespace.GCD]
        ),
        SeriesExternalIdentity.verification_state == IdentityVerificationState.VERIFIED,
    )
    filters: list[ColumnElement[bool]] = [Series.id.in_(verified)]
    if series_id is not None:
        filters.append(Series.id == series_id)
    if query.strip():
        filters.append(func.lower(Series.title).contains(query.strip().lower(), autoescape=True))
    total = int(await session.scalar(select(func.count(Series.id)).where(*filters)) or 0)
    pages = max(1, (total + 24) // 25)
    page = min(page, pages)
    values = list(
        (
            await session.scalars(
                select(Series)
                .where(*filters)
                .order_by(Series.sort_title, Series.id)
                .offset((page - 1) * 25)
                .limit(25)
            )
        ).all()
    )
    return values, page, pages


async def choose_library_series(
    session: AsyncSession, job_id: int, parent_id: int, series_id: int, actor_id: int, token: str
) -> None:
    job, parent, scope = await library_selection_context(session, job_id, parent_id, actor_id)
    series = await session.get(Series, series_id)
    if series is None:
        raise ValidationError("This library series no longer exists.")
    identity = await library_series_identity(session, series)
    await verify_review_scope(
        session,
        token,
        {**scope, "target": identity, "title": series.title, "year": series.year_start},
    )
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
    parent.series_id = series.id
    parent.cv_id = series.comicvine_id
    parent.cv_title = series.title
    parent.cv_year = series.year_start
    from pullbox.models.publisher import Publisher

    publisher = await session.get(Publisher, series.publisher_id) if series.publisher_id else None
    parent.cv_publisher = publisher.name if publisher else None
    parent.cv_issue_count = series.issue_count
    parent.cv_match_method = "library_review"
    parent.cv_match_score = None
    parent.status = ImportSeriesStatus.DUPLICATE
    parent.selected_for_import = False
    parent.diagnostics = {
        **parent.diagnostics,
        "kind": "duplicate_series",
        "manual_library_series": identity,
        "existing_series_id": series.id,
        "duplicate_reason": "user_review",
        "rejection_reason": None,
    }
    await session.execute(
        update(ImportedFile)
        .where(
            ImportedFile.import_series_id == parent.id,
            ImportedFile.status == ImportedFileStatus.PENDING,
        )
        .values(status=ImportedFileStatus.NO_MATCH, include_in_import=False)
    )
    await session.flush()
    await recompute_file_counters(session, job, series_ids=[parent.id])
    await recompute_series_counters(session, job)


def has_compiled_series_choice(file: ImportedFile) -> bool:
    evidence = file_metron_evidence(file) or {}
    sidecar = evidence.get("series_sidecar")
    return (
        isinstance(sidecar, dict)
        and not sidecar.get("problems")
        and any(ref.get("namespace") in {"metron", "gcd"} for ref in sidecar.get("identities", []))
    )


def reviewed_issue_proof(
    file: ImportedFile, issue: Issue, index: FileMatchTargetIndex
) -> dict[str, Any] | None:
    """A user may supply missing identity, never contradict inspected exact evidence."""
    evidence = file_metron_evidence(file)
    if (
        evidence is None
        or evidence.get("problems")
        or index.existing_series is None
        or issue.series_id != index.existing_series.id
    ):
        return None
    refs = evidence.get("identities", [])
    if any(
        (ref["namespace"], ref["id"]) not in index.series_provider_ids
        for ref in refs
        if ref.get("kind") == "series"
    ):
        return None
    if any(
        index.provider_id_map.get((ref["namespace"], ref["id"]), (None,))[0] != issue.id
        for ref in refs
        if ref.get("kind") == "issue"
    ):
        return None
    if (
        evidence.get("number") is not None
        and normalize_issue_number_text(evidence["number"]) != issue.effective_issue_number_text
    ):
        return None
    if file.comicvine_issue_id is not None and file.comicvine_issue_id != issue.comicvine_id:
        return None
    source_cv = file.diagnostics.get("comicvine_series_id")
    if source_cv is not None and source_cv != index.existing_series.comicvine_id:
        return None
    native_links = [
        key
        for key, value in index.provider_id_map.items()
        if value[0] == issue.id and key[0] in {"metron", "gcd"}
    ]
    if not native_links:
        return None
    return {
        "file_id": file.id,
        "series_id": issue.series_id,
        "issue_id": issue.id,
        "series_links": [list(key) for key in sorted(index.series_provider_ids)],
        "issue_links": [
            list(key)
            for key, value in sorted(index.provider_id_map.items())
            if value[0] == issue.id
        ],
        "number": issue.effective_issue_number_text,
        "source_signature": file.source_signature,
        "source_path": file.file_path,
        "evidence": evidence,
        "comicvine_issue_id": file.comicvine_issue_id,
        "comicvine_series_id": source_cv,
    }


async def revalidate_library_issue(session: AsyncSession, file: ImportedFile, issue: Issue) -> None:
    from pullbox.services.import_file_match_targets import FileMatchTargetIndex
    from pullbox.services.import_metron_matching import load_metron_issue_targets

    series = await session.get(Series, issue.series_id)
    parent = await session.get(ImportedSeries, file.import_series_id)
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
    proof = reviewed_issue_proof(file, issue, index)
    valid_series = False
    if series is not None and parent is not None:
        manual = parent.diagnostics.get("manual_library_series")
        if manual:
            valid_series = await library_series_identity(session, series) == manual
        elif has_compiled_series_choice(file):
            from pullbox.services.import_metron_matching import verified_native_import_series

            valid_series = await verified_native_import_series(session, parent) is series
    if (
        series is None
        or parent is None
        or parent.series_id != series.id
        or proof is None
        or proof != file.diagnostics.get("reviewed_library_issue")
        or not valid_series
    ):
        raise ValidationError(
            "The reviewed library issue or its source evidence changed. "
            "Choose its issue again before retrying. The source file was left unchanged."
        )


def reviewed_library_candidate(
    parent: ImportedSeries, file: ImportedFile, index: FileMatchTargetIndex
) -> tuple[FileMatchCandidate | None, dict[str, Any] | None]:
    from pullbox.services.import_file_match_candidates import FileMatchCandidate

    saved = file.diagnostics.get("reviewed_library_issue") or {}
    identity = parent.diagnostics.get("manual_library_series") or {}
    valid_series = (
        identity.get("links") == [list(key) for key in sorted(index.series_provider_ids)]
        if identity
        else bool(parent.diagnostics.get("metron_import")) and has_compiled_series_choice(file)
    )
    if valid_series:
        for issue, owned in index.issue_entries:
            if (
                issue.id == saved.get("issue_id")
                and reviewed_issue_proof(file, issue, index) == saved
            ):
                return FileMatchCandidate(
                    issue.id,
                    issue.comicvine_id,
                    issue.issue_number,
                    owned,
                    issue,
                    issue.title,
                    "manual",
                    "library_review",
                ), None
    return None, {
        "kind": "issue_no_match",
        "preserve_series_match": True,
        "reason": "library_issue_selection_required",
        "rejection_reason": (
            "Choose this file's issue from the selected library series, or skip it. "
            "No filename match is applied automatically."
        ),
    }
