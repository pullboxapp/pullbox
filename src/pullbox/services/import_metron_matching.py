"""Exact local Metron folder matches; no provider calls or identity attachment."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update

from pullbox.core.issue_numbers import normalize_issue_number_text
from pullbox.core.metadata_identity import IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportSeriesStatus
from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity
from pullbox.models.series import Series
from pullbox.services.archive_metadata_reconciliation import (
    ArchiveMetadataDiagnostic,
    reconcile_archive_metadata,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.core.archive_metadata import ArchiveMetadataFiles
    from pullbox.models.import_job import ImportedSeries
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


def uses_metron_identity(evidence: dict[str, Any]) -> bool:
    return evidence.get("primary_source") == "metron" or bool(
        evidence.get("problems")
        and any(ref.get("namespace") == "metron" for ref in evidence.get("identities", []))
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
    return {
        "identities": [
            {"namespace": namespace, "id": value} for namespace, value in sorted(identities)
        ]
    }


async def match_local_metron_series(session: AsyncSession, item: ImportedSeries) -> bool:
    """Handle Metron-bearing groups before legacy name/CV matching can select them."""
    evidence = (item.diagnostics or {}).get("metron_import")
    if not isinstance(evidence, dict):
        return False
    refs = evidence.get("identities", [])
    metron_ids = {ref["id"] for ref in refs if ref["namespace"] == "metron"}
    owner = None
    if len(metron_ids) == 1:
        owner = await session.scalar(
            select(SeriesExternalIdentity).where(
                SeriesExternalIdentity.identity_namespace == IdentityNamespace.METRON,
                SeriesExternalIdentity.external_id == next(iter(metron_ids)),
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
    if not valid or series is None:
        item.status = ImportSeriesStatus.NO_MATCH
        item.diagnostics = {
            **(item.diagnostics or {}),
            "kind": "series_conflict",
            "reason": "metron_identity_needs_review",
            "rejection_reason": (
                "The embedded Metron series identity is not linked to one verified library series. "
                "Add or link the correct series through Metron, then scan this folder again, "
                "or skip it for now."
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
    item.cv_match_method = "metron_id"
    item.diagnostics = {
        **(item.diagnostics or {}),
        "kind": "duplicate_series",
        "duplicate_reason": "metron_id",
        "existing_series_id": series.id,
        "existing_series_title": series.title,
        "duplicate_match_score": 1.0,
    }
    return True


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
    error = {
        "kind": "metadata_conflict",
        "conflict_type": "metron_identity_needs_review",
        "preserve_series_match": True,
        "rejection_reason": (
            "This file needs its own verified Metron series and issue identity. "
            "Review the linked series and embedded metadata, then scan this folder again, "
            "or skip this file for now."
        ),
    }
    if evidence is None:
        return None, error
    refs = [ref for ref in evidence.get("identities", []) if ref.get("kind") == "issue"]
    series_refs = [ref for ref in evidence.get("identities", []) if ref.get("kind") == "series"]
    if not any(ref["namespace"] == "metron" for ref in series_refs) or any(
        (ref["namespace"], ref["id"]) not in index.series_provider_ids for ref in series_refs
    ):
        return None, error
    metron_refs = [ref for ref in refs if ref["namespace"] == "metron"]
    if evidence.get("problems") or len(metron_refs) != 1:
        return None, error
    target = index.provider_id_map.get(("metron", metron_refs[0]["id"]))
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
        "metron_id",
    ), None


async def revalidate_metron_import_target(
    session: AsyncSession,
    file: ImportedFile,
    issue: Issue,
) -> None:
    """A review-time provider link must still identify this issue before placement."""
    if file.match_method != "metron_id":
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
            "The verified Metron series or issue link changed after review. "
            "Recheck this file's match before retrying. The source file was left unchanged."
        )
