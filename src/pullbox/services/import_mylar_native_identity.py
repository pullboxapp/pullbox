"""Block proven native identity disagreements without replacing Mylar ownership."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

from sqlalchemy import select, tuple_

from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models.issue import Issue
from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity
from pullbox.models.series import Series
from pullbox.services.import_metron_matching import file_metron_evidence

if TYPE_CHECKING:
    from sqlalchemy import Select
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.import_job import ImportedFile


class NativeIdentityOwner(NamedTuple):
    series_id: int
    series_cv_id: int | None
    issue_id: int | None = None
    issue_cv_id: int | None = None


def recorded_native_refs(file: ImportedFile) -> set[tuple[str, str, str]]:
    source = (file.diagnostics or {}).get("source_metadata") or {}
    signals = (file.diagnostics or {}).get("metadata_signals") or {}
    if not source.get("mylar3_issue") and signals.get("comicvine_issue_id") != "mylar3":
        return set()
    return {
        (ref["namespace"], ref["kind"], ref["id"])
        for ref in (file_metron_evidence(file) or {}).get("identities", [])
        if ref.get("namespace") in {"metron", "gcd"}
        and ref.get("kind") in {"series", "issue"}
        and isinstance(ref.get("id"), str)
    }


async def load_mylar_native_owners(
    session: AsyncSession, files: list[ImportedFile]
) -> dict[tuple[str, str, str], NativeIdentityOwner]:
    """Read verified local owners in bounded batches; unknown IDs remain evidence."""
    refs = {ref for file in files for ref in recorded_native_refs(file)}
    owners: dict[tuple[str, str, str], NativeIdentityOwner] = {}
    for kind in ("series", "issue"):
        keys = sorted(
            (namespace, external_id) for namespace, entity, external_id in refs if entity == kind
        )
        model = SeriesExternalIdentity if kind == "series" else IssueExternalIdentity
        query: Select[*tuple[Any, ...]]
        if kind == "series":
            query = select(
                model.identity_namespace, model.external_id, Series.id, Series.comicvine_id
            ).join(Series, Series.id == SeriesExternalIdentity.series_id)
        else:
            query = (
                select(
                    model.identity_namespace,
                    model.external_id,
                    Series.id,
                    Series.comicvine_id,
                    Issue.id,
                    Issue.comicvine_id,
                )
                .join(Issue, Issue.id == IssueExternalIdentity.issue_id)
                .join(Series, Series.id == Issue.series_id)
            )
        for offset in range(0, len(keys), 250):
            rows = await session.execute(
                query.where(
                    model.verification_state == IdentityVerificationState.VERIFIED,
                    tuple_(model.identity_namespace, model.external_id).in_(
                        keys[offset : offset + 250]
                    ),
                )
            )
            for row in rows:
                owners[(row[0].value, kind, row[1])] = NativeIdentityOwner(*row[2:])
    return owners


def mylar_native_conflict(
    file: ImportedFile,
    owners: dict[tuple[str, str, str], NativeIdentityOwner],
    *,
    series_id: int | None,
    series_cv_id: int | None,
    issue_id: int | None,
    issue_cv_id: int | None,
) -> dict[str, Any] | None:
    conflicts = []
    for ref in sorted(recorded_native_refs(file)):
        owner = owners.get(ref)
        if owner is None:
            continue
        if (
            (series_id is not None and owner.series_id != series_id)
            or (
                series_cv_id is not None
                and owner.series_cv_id is not None
                and owner.series_cv_id != series_cv_id
            )
            or (ref[1] == "issue" and issue_id is not None and owner.issue_id != issue_id)
            or (
                ref[1] == "issue"
                and issue_cv_id is not None
                and owner.issue_cv_id is not None
                and owner.issue_cv_id != issue_cv_id
            )
        ):
            conflicts.append(
                {
                    "namespace": ref[0],
                    "kind": ref[1],
                    "id": ref[2],
                    "saved_series_id": owner.series_id,
                    "saved_issue_id": owner.issue_id,
                }
            )
    if not conflicts:
        return None
    return {
        "kind": "metadata_conflict",
        "conflict_type": "mylar_native_identity_conflict",
        "preserve_series_match": True,
        "conflicting_provider_identities": conflicts,
        "rejection_reason": (
            "Mylar and the file's embedded provider identities point to different "
            "saved library records. Review the linked metadata or correct the source file, "
            "then recheck it, or skip this file. "
            "The source file was left unchanged."
        ),
    }


async def mylar_placement_conflict(
    session: AsyncSession, file: ImportedFile, issue: Issue
) -> dict[str, Any] | None:
    if not recorded_native_refs(file):
        return None
    series = await session.get(Series, issue.series_id)
    return mylar_native_conflict(
        file,
        await load_mylar_native_owners(session, [file]),
        series_id=issue.series_id,
        series_cv_id=series.comicvine_id if series else None,
        issue_id=issue.id,
        issue_cv_id=issue.comicvine_id,
    )
