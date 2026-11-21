"""Exact Metron/GCD folder matches using saved XML evidence and verified links.

The durable ``metron_import`` key remains compatible with existing review jobs.
Compiled series-sidecar claims are marked separately from per-file XML evidence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, or_, select, update

from pullbox.core.exceptions import NotFoundError, ValidationError
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
from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity
from pullbox.models.series import Series
from pullbox.services.archive_metadata_reconciliation import (
    ArchiveMetadataDiagnostic,
    reconcile_archive_metadata,
)
from pullbox.services.import_counters import recompute_file_counters, recompute_series_counters

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.core.archive_metadata import ArchiveMetadataFiles
    from pullbox.models.issue import Issue
    from pullbox.services.import_file_match_candidates import FileMatchCandidate
    from pullbox.services.import_file_match_targets import FileMatchTargetIndex


def _identity_diagnostic(item: ArchiveMetadataDiagnostic) -> bool:
    if item.code == "unmapped_content":
        return False
    if item.code in {"invalid_field", "ambiguous_field"}:
        return item.locator in {
            "Number",
            "Notes",
            "Web",
            "IDS",
            "Series",
        } or item.locator.startswith(("IDS[", "Series/@id"))
    return True


def metron_import_evidence(files: ArchiveMetadataFiles) -> dict[str, Any]:
    """Keep bounded normalized evidence, not raw XML or archive-controlled paths."""
    result = reconcile_archive_metadata(files)
    return {
        "primary_source": (
            result.metroninfo.metron.primary_source.value
            if result.metroninfo.metron is not None and result.metroninfo.metron.primary_source
            else None
        ),
        "identities": [
            {"namespace": ref.namespace.value, "kind": ref.entity_kind.value, "id": ref.external_id}
            for ref in dict.fromkeys(item.identity for item in result.evidence)
        ],
        "number": result.issue.issue_number_text,
        "diagnostics": [
            {"document": item.document, "code": item.code, "locator": item.locator}
            for item in result.diagnostics
        ],
        "problems": sorted(
            {
                *(item.code for item in result.diagnostics if _identity_diagnostic(item)),
                *("exact_identity_conflict" for _ in result.identity_conflicts),
                *(
                    "issue_number_conflict"
                    for item in result.differences
                    if item.field == "issue_number_text"
                ),
            }
        ),
    }


def import_identity_namespace(evidence: dict[str, Any]) -> IdentityNamespace | None:
    primary = evidence.get("primary_source")
    if primary in {"metron", "gcd"}:
        return IdentityNamespace(primary)
    namespaces = {
        ref["namespace"]
        for ref in evidence.get("identities", [])
        if ref.get("namespace") in {"metron", "gcd"}
    }
    return IdentityNamespace(next(iter(namespaces))) if len(namespaces) == 1 else None


def uses_metron_identity(evidence: dict[str, Any]) -> bool:
    return (
        bool(evidence.get("series_sidecar"))
        or evidence.get("primary_source") in {"metron", "gcd"}
        or bool(
            evidence.get("problems")
            and any(
                ref.get("namespace") in {"metron", "gcd"} for ref in evidence.get("identities", [])
            )
        )
    )


def file_metron_evidence(file: ImportedFile) -> dict[str, Any] | None:
    source = (file.diagnostics or {}).get("source_metadata")
    value = source.get("metron_import") if isinstance(source, dict) else None
    return value if isinstance(value, dict) else None


def series_evidence(files: list[dict[str, Any]]) -> dict[str, Any]:
    identities = {
        (ref["namespace"], ref["id"])
        for evidence in files
        for ref in evidence.get("identities", [])
        if ref.get("kind") == "series"
    }
    primaries = {
        namespace for evidence in files if (namespace := import_identity_namespace(evidence))
    }
    result: dict[str, Any] = {
        "primary_source": next(iter(primaries)).value if len(primaries) == 1 else None,
        "identities": [
            {"namespace": namespace, "id": value} for namespace, value in sorted(identities)
        ],
    }
    if any(evidence.get("series_sidecar") for evidence in files):
        result["series_sidecar"] = True
        result["sidecar_problems"] = sorted(
            {
                problem
                for evidence in files
                for problem in (evidence.get("series_sidecar") or {}).get("problems", [])
            }
        )
    return result


async def _verified_metron_series(session: AsyncSession, item: ImportedSeries) -> Series | None:
    evidence = (item.diagnostics or {}).get("metron_import")
    if not isinstance(evidence, dict) or evidence.get("sidecar_problems"):
        return None
    refs = evidence.get("identities", [])
    namespace = import_identity_namespace(evidence)
    native_ids = {ref["id"] for ref in refs if ref["namespace"] == namespace}
    owner = None
    if namespace is not None and len(native_ids) == 1:
        owner = await session.scalar(
            select(SeriesExternalIdentity).where(
                SeriesExternalIdentity.identity_namespace == namespace,
                SeriesExternalIdentity.external_id == next(iter(native_ids)),
                SeriesExternalIdentity.verification_state == IdentityVerificationState.VERIFIED,
            )
        )
    series = await session.get(Series, owner.series_id) if owner is not None else None
    valid = series is not None
    if series is not None:
        linked = (
            await session.scalars(
                select(SeriesExternalIdentity).where(
                    SeriesExternalIdentity.series_id == series.id,
                    SeriesExternalIdentity.verification_state == IdentityVerificationState.VERIFIED,
                )
            )
        ).all()
        keys = {(link.identity_namespace.value, link.external_id) for link in linked}
        if series.comicvine_id is not None:
            keys.add(("comicvine", str(series.comicvine_id)))
        valid = all((ref["namespace"], ref["id"]) in keys for ref in refs)
        valid = valid and (item.cv_id is None or item.cv_id == series.comicvine_id)
    return series if valid else None


async def verified_native_import_series(
    session: AsyncSession, item: ImportedSeries
) -> Series | None:
    """Resolve the same exact series evidence used during scan and native review."""
    return await _verified_metron_series(session, item)


def native_review_source(job: ImportJob, item: ImportedSeries) -> bool:
    """Mylar native review is limited to separately staged unrecorded archives."""
    return job.source_type == ImportSourceType.FILESYSTEM or (
        job.source_type == ImportSourceType.MYLAR3
        and isinstance((item.diagnostics or {}).get("mylar3_native_extra"), dict)
        and isinstance((item.diagnostics or {}).get("metron_import"), dict)
    )


async def match_local_metron_series(session: AsyncSession, item: ImportedSeries) -> bool:
    """Handle native XML identities before legacy name/CV matching can select them."""
    if not isinstance((item.diagnostics or {}).get("metron_import"), dict):
        return False
    series = await _verified_metron_series(session, item)
    namespace = import_identity_namespace(item.diagnostics["metron_import"])
    identity_label = "GCD" if namespace == IdentityNamespace.GCD else "Metron"
    identity_key = namespace.value if namespace else "provider"
    if series is None:
        item.status = ImportSeriesStatus.NO_MATCH
        item.diagnostics = {
            **(item.diagnostics or {}),
            "kind": "series_conflict",
            "reason": f"{identity_key}_identity_needs_review",
            "rejection_reason": (
                "Correct the source sidecar and rescan this folder, or skip this series. "
                "series.json is invalid, unsupported, or exceeds the 1 MiB safety limit."
                if item.diagnostics["metron_import"].get("sidecar_problems")
                else f"The {identity_label} identity in the source metadata needs a verified "
                f"library series. Add or link the correct series through {identity_label}, "
                "then recheck its linked metadata, or skip it for now."
            ),
        }
        await session.execute(
            update(ImportedFile)
            .where(
                ImportedFile.import_series_id == item.id,
                ImportedFile.status == ImportedFileStatus.PENDING,
            )
            .values(status=ImportedFileStatus.NO_MATCH, include_in_import=False)
        )
        return True
    item.series_id = series.id
    item.status = ImportSeriesStatus.DUPLICATE
    item.cv_id = series.comicvine_id
    item.cv_title = series.title
    item.cv_year = series.year_start
    item.cv_issue_count = series.issue_count
    item.cv_match_score = 1.0
    item.cv_match_method = f"{identity_key}_id"
    diagnostics = dict(item.diagnostics or {})
    if diagnostics.get("reason") in {
        "metron_identity_needs_review",
        "gcd_identity_needs_review",
        "provider_identity_needs_review",
    }:
        diagnostics.pop("reason", None)
        diagnostics.pop("rejection_reason", None)
    item.diagnostics = {
        **diagnostics,
        "kind": "duplicate_series",
        "duplicate_reason": item.cv_match_method,
        "existing_series_id": series.id,
        "existing_series_title": series.title,
        "duplicate_match_score": 1.0,
    }
    return True


async def _load_metron_review_scope(
    session: AsyncSession, job_id: int, series_id: int
) -> tuple[ImportJob, ImportedSeries]:
    job = await session.get(ImportJob, job_id)
    item = await session.get(ImportedSeries, series_id)
    if job is None or item is None or item.import_job_id != job_id:
        raise NotFoundError("ImportedSeries", series_id)
    if (
        job.status != ImportJobStatus.REVIEW
        or job.control_request != ImportControlRequest.NONE
        or not native_review_source(job, item)
        or item.status not in {ImportSeriesStatus.NO_MATCH, ImportSeriesStatus.DUPLICATE}
        or item.user_selected_cv_id is not None
        or (item.diagnostics or {}).get("rematch_pending")
    ):
        raise ValidationError("This series is busy or no longer available for an identity recheck.")
    return job, item


async def native_series_preview_identity(
    session: AsyncSession, job_id: int, series_id: int
) -> tuple[ImportJob, ImportedSeries, IdentityNamespace, str]:
    """Use only the saved exact series identity for an explicit provider preview."""
    job, item = await _load_metron_review_scope(session, job_id, series_id)
    evidence = (item.diagnostics or {}).get("metron_import", {})
    namespace = import_identity_namespace(evidence)
    refs = evidence.get("identities", [])
    native_ids = {str(ref["id"]) for ref in refs if ref.get("namespace") == namespace}
    if namespace is None or len(native_ids) != 1:
        raise ValidationError(
            "These files need one exact provider series identity before previewing."
        )
    external_id = next(iter(native_ids))
    if not external_id.isascii() or not external_id.isdecimal() or int(external_id) <= 0:
        raise ValidationError("The saved provider series identity is invalid. Review the source.")
    return job, item, namespace, external_id


async def prepare_metron_series_recheck(
    session: AsyncSession, job_id: int, series_id: int
) -> ImportedSeries:
    """Prepare an explicit local-identity recheck without rescanning source files."""
    job, item = await _load_metron_review_scope(session, job_id, series_id)
    namespace = import_identity_namespace((item.diagnostics or {}).get("metron_import") or {})
    identity_label = "GCD" if namespace == IdentityNamespace.GCD else "Metron"
    target = await _verified_metron_series(session, item)
    if target is None:
        raise ValidationError(
            "The embedded identities still need one verified library series. "
            f"Add or link the correct {identity_label} series, then recheck linked metadata. "
            "Conflicting identities must be resolved first."
        )
    if item.series_id is not None and item.series_id != target.id:
        raise ValidationError("The linked series changed. Review its identity before retrying.")
    eligible = (
        ImportedFile.import_job_id == job_id,
        ImportedFile.import_series_id == series_id,
        ImportedFile.include_in_import.is_(False),
        ImportedFile.matched_issue_id.is_(None),
        ImportedFile.matched_issue_cv_id.is_(None),
        or_(
            ImportedFile.status == ImportedFileStatus.NO_MATCH,
            and_(
                ImportedFile.status == ImportedFileStatus.CONFLICT,
                ImportedFile.diagnostics["conflict_type"]
                .as_string()
                .in_(["metron_identity_needs_review", "gcd_identity_needs_review"]),
            ),
        ),
    )
    if await session.scalar(select(ImportedFile.id).where(*eligible).limit(1)) is None:
        raise ValidationError("No unresolved files are eligible for this identity recheck.")
    # Serialize review admission with execution; the worker revalidates each exact identity.
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
    await match_local_metron_series(session, item)
    item.diagnostics = {**item.diagnostics, "rematch_pending": True}
    await session.execute(
        update(ImportedFile).where(*eligible).values(status=ImportedFileStatus.PENDING)
    )
    await session.flush()
    await recompute_series_counters(session, job)
    await recompute_file_counters(session, job, series_ids=[series_id])
    await session.flush()
    return item


async def load_metron_issue_targets(
    session: AsyncSession,
    index: FileMatchTargetIndex,
    files: list[ImportedFile],
) -> None:
    if not any(file_metron_evidence(file) is not None for file in files):
        return
    if index.existing_series is not None:
        series = index.existing_series
        links = (
            await session.scalars(
                select(SeriesExternalIdentity).where(
                    SeriesExternalIdentity.series_id == series.id,
                    SeriesExternalIdentity.verification_state == IdentityVerificationState.VERIFIED,
                )
            )
        ).all()
        index.series_provider_ids = {
            (link.identity_namespace.value, link.external_id) for link in links
        }
        if series.comicvine_id is not None:
            index.series_provider_ids.add(("comicvine", str(series.comicvine_id)))
    entries = {
        issue.id: (issue.id, issue.comicvine_id, owned, issue, issue.title)
        for issue, owned in index.issue_entries
    }
    issue_ids = set(entries)
    if not issue_ids:
        return
    issue_links = (
        await session.scalars(
            select(IssueExternalIdentity).where(
                IssueExternalIdentity.issue_id.in_(issue_ids),
                IssueExternalIdentity.verification_state == IdentityVerificationState.VERIFIED,
            )
        )
    ).all()
    for link in issue_links:
        index.provider_id_map[(link.identity_namespace.value, link.external_id)] = entries[
            link.issue_id
        ]
    for cv_id, entry in index.cv_id_map.items():
        index.provider_id_map[("comicvine", str(cv_id))] = entry


def metron_file_candidate(
    file: ImportedFile,
    index: FileMatchTargetIndex,
) -> tuple[FileMatchCandidate | None, dict[str, Any] | None]:
    from pullbox.services.import_file_match_candidates import FileMatchCandidate

    evidence = file_metron_evidence(file)
    namespace = import_identity_namespace(evidence or {})
    identity_label = "GCD" if namespace == IdentityNamespace.GCD else "Metron"
    identity_key = namespace.value if namespace else "provider"
    error = {
        "kind": "metadata_conflict",
        "conflict_type": f"{identity_key}_identity_needs_review",
        "preserve_series_match": True,
        "rejection_reason": (
            f"This file needs its own verified {identity_label} series and issue identity. "
            "Review the linked metadata and recheck it. If the source file changed, rescan it, "
            "or skip this file for now."
        ),
    }
    if evidence is None or namespace is None:
        return None, error
    refs = [ref for ref in evidence.get("identities", []) if ref.get("kind") == "issue"]
    series_refs = [ref for ref in evidence.get("identities", []) if ref.get("kind") == "series"]
    if not any(ref["namespace"] == namespace for ref in series_refs) or any(
        (ref["namespace"], ref["id"]) not in index.series_provider_ids for ref in series_refs
    ):
        return None, error
    sidecar = evidence.get("series_sidecar")
    native_refs = refs if sidecar else [ref for ref in refs if ref["namespace"] == namespace]
    if evidence.get("problems") or not native_refs or (not sidecar and len(native_refs) != 1):
        return None, error
    first = min(
        native_refs,
        key=lambda ref: (
            ref["namespace"] != evidence.get("embedded_primary_source", namespace.value),
            ref["namespace"],
            ref["id"],
        ),
    )
    target = index.provider_id_map.get((first["namespace"], first["id"]))
    if target is None or target[3] is None:
        return None, error
    issue = target[3]
    if any(
        index.provider_id_map.get((ref["namespace"], ref["id"]), (None,))[0] != issue.id
        for ref in refs
    ):
        return None, error
    if (
        evidence.get("number") is not None
        and normalize_issue_number_text(evidence["number"]) != issue.effective_issue_number_text
    ):
        return None, error
    if file.comicvine_issue_id is not None and file.comicvine_issue_id != issue.comicvine_id:
        return None, error
    return FileMatchCandidate(
        issue.id,
        issue.comicvine_id,
        issue.issue_number,
        target[2],
        issue,
        issue.title,
        "high",
        f"{identity_key}_id",
    ), None


async def revalidate_metron_import_target(
    session: AsyncSession,
    file: ImportedFile,
    issue: Issue,
) -> None:
    """A review-time provider link must still identify this issue before placement."""
    if file.match_method == "library_review":
        from pullbox.services.import_library_selection import revalidate_library_issue

        await revalidate_library_issue(session, file, issue)
        return
    if file.match_method not in {"metron_id", "gcd_id"}:
        return
    from pullbox.core.exceptions import ValidationError
    from pullbox.services.import_file_match_targets import FileMatchTargetIndex

    index = FileMatchTargetIndex(
        existing_series=await session.get(Series, issue.series_id),
        issue_entries=[(issue, False)],
    )
    if issue.comicvine_id is not None:
        index.cv_id_map[issue.comicvine_id] = (
            issue.id,
            issue.comicvine_id,
            False,
            issue,
            issue.title,
        )
    await load_metron_issue_targets(session, index, [file])
    if file_metron_evidence(file) is None:
        candidate = None
    else:
        candidate, _ = metron_file_candidate(file, index)
    if candidate is None or candidate.matched_issue_id != file.matched_issue_id:
        raise ValidationError(
            "The verified provider series or issue link changed after review. "
            "Recheck this file's match before retrying. The source file was left unchanged."
        )
