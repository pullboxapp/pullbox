"""Shared issue-only writes for complete catalogs and bounded source windows."""

from dataclasses import replace
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.metadata_identity import ExternalIdentityRef, MetadataEntityKind
from pullbox.models import Issue, Series
from pullbox.models.issue import IssueStatus
from pullbox.services.metadata_assembly import assemble_metadata
from pullbox.services.metadata_baselines import MetadataBaselineWrite, save_metadata_baselines
from pullbox.services.metadata_credits import write_issue_credits
from pullbox.services.metadata_entity_values import apply_issue_metadata_values
from pullbox.services.metadata_identity_attachment import attach_verified_identities
from pullbox.services.metadata_identity_review import record_identity_observation
from pullbox.services.metadata_locg_enrichment import IssueReleaseFacts, enrich_issue_snapshot
from pullbox.services.metadata_series_adoption import (
    SourceIssueBatch as SourceIssueBatch,
)
from pullbox.services.metadata_series_adoption import (
    _identities,
    _metadata_label,
    _request,
    _require_unowned_issues,
    _validate_issue_batch,
)
from pullbox.services.metadata_series_refresh_state import ISSUE_FIELDS, SeriesRefreshState
from pullbox.services.metadata_service import classify_issue_metadata


class IssueCatalogConflictError(ValueError):
    """A source issue cannot safely update its canonical library target."""


async def apply_issue_batch(
    session: AsyncSession,
    state: SeriesRefreshState,
    batch: SourceIssueBatch,
    now: datetime,
    *,
    complete: bool = False,
    replace_managed: bool = False,
    release_facts: tuple[IssueReleaseFacts, ...] = (),
) -> tuple[int, ...]:
    """Apply to a locked, revalidated read set; return exactly the created IDs.

    The caller owns transaction rollback, series metadata, completeness and
    checkpoint updates. This writer never starts provider or filesystem work.
    """
    source = batch.source
    parent = ExternalIdentityRef(
        source.identity_namespace, MetadataEntityKind.SERIES, batch.series_external_id
    )
    policy = next((item for item in state.policies if item.source is source), None)
    if (
        parent.external_id != batch.series_external_id
        or parent not in state.series.identities
        or policy is None
        or not policy.enabled
        or policy.revision != batch.source_revision
    ):
        raise IssueCatalogConflictError("Catalog source or verified series identity changed.")
    try:
        numbers = _validate_issue_batch(batch)
    except ValueError as exc:
        raise IssueCatalogConflictError(str(exc)) from exc
    by_identity = {identity: item for item in state.issues for identity in item.identities}
    offered = {
        ExternalIdentityRef(source.identity_namespace, MetadataEntityKind.ISSUE, item.external_id)
        for item in batch.issues
    }
    missing = {
        identity for identity in by_identity if identity.namespace is source.identity_namespace
    } - offered
    if complete and missing:
        raise IssueCatalogConflictError(
            "The provider removed existing issue identities. "
            "Review the catalog; existing issues and files were kept."
        )
    by_number = {item.values.issue_number_text: item for item in state.issues}
    prepared = []
    release_by_issue = {item.local_id: item for item in release_facts}
    for metadata, (number, text) in zip(batch.issues, numbers, strict=True):
        identity = ExternalIdentityRef(
            source.identity_namespace, MetadataEntityKind.ISSUE, metadata.external_id
        )
        existing = by_identity.get(identity)
        if existing is not None and existing.values.issue_number_text != text:
            raise IssueCatalogConflictError(
                "A provider issue was renumbered. Review the issue match; existing files were kept."
            )
        if existing is None and text in by_number:
            raise IssueCatalogConflictError(
                "An issue designation already belongs to a different identity. "
                "Review its match; no issues were reassigned."
            )
        snapshot = assemble_metadata(
            MetadataEntityKind.ISSUE,
            existing.identities if existing else (identity,),
            [metadata.model_copy(update={"issue_number_text": text})],
            state.policies,
            now=now,
            current=existing.values if existing else None,
            previous=existing.baseline if existing else None,
            parent_identities=state.series.identities,
            replace_managed=replace_managed,
            fields=ISSUE_FIELDS,
        )
        if existing is not None:
            snapshot = enrich_issue_snapshot(
                snapshot, release_by_issue.get(existing.local_id), now=now
            )
        prepared.append((existing, metadata, number, text, snapshot))
    new_metadata = tuple(metadata for old, metadata, *_rest in prepared if old is None)
    if new_metadata:
        await _require_unowned_issues(session, replace(batch, issues=new_metadata))
    series = await session.get(Series, state.series.local_id)
    if series is None:
        raise IssueCatalogConflictError("The series no longer exists.")
    existing_issues = {
        item.id: item
        for item in await session.scalars(select(Issue).where(Issue.series_id == series.id))
    }
    created = []
    for offset in range(0, len(prepared), 200):
        pending = []
        for existing, metadata, number, text, snapshot in prepared[offset : offset + 200]:
            issue = existing_issues.get(existing.local_id) if existing else None
            if issue is None:
                issue = Issue(
                    series_id=series.id,
                    issue_number=number,
                    issue_number_text=text,
                    status=IssueStatus.WANTED if series.monitored else IssueStatus.SKIPPED,
                    issue_type=classify_issue_metadata(series.series_type, metadata.title)[1],
                    metadata_source=_metadata_label(source),
                )
                session.add(issue)
            apply_issue_metadata_values(issue, snapshot.values)
            pending.append((issue, existing, metadata, snapshot))
        await session.flush()
        await attach_verified_identities(
            session,
            [
                _request(
                    batch, metadata, _identities(metadata, MetadataEntityKind.ISSUE)[0], issue.id
                )
                for issue, old, metadata, _ in pending
                if old is None
            ],
        )
        for issue, old, metadata, _ in pending:
            if old is None:
                created.append(issue.id)
                for crosswalk in _identities(metadata, MetadataEntityKind.ISSUE)[1:]:
                    await record_identity_observation(
                        session, _request(batch, metadata, crosswalk, issue.id, observation=True)
                    )
        await write_issue_credits(
            session, {issue.id: item.values.credits for issue, _, _, item in pending}
        )
        await save_metadata_baselines(
            session,
            [
                MetadataBaselineWrite(issue.id, item, old.baseline_revision if old else 0)
                for issue, old, _, item in pending
            ],
        )
    return tuple(created)
