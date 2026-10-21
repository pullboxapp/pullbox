"""Bounded identity evidence for Metron's lightweight issue-list rows."""

from dataclasses import replace

from pullbox.core.issue_numbers import parse_issue_number_text
from pullbox.core.metadata_identity import ExternalIdentityRef, MetadataEntityKind, MetadataSource
from pullbox.schemas.metadata_sources import SourceStatus
from pullbox.services.metadata_discovery import MetadataSourceRegistry
from pullbox.services.metadata_issue_catalog import IssueCatalogConflictError
from pullbox.services.metadata_series_adoption import SourceSeriesBundle, _validate_bundle
from pullbox.services.metadata_series_refresh_state import SeriesRefreshState

_MAX_DETAIL_READS = 25


async def hydrate_metron_catalog_crosswalks(
    registry: MetadataSourceRegistry,
    state: SeriesRefreshState,
    bundles: tuple[SourceSeriesBundle, ...],
) -> tuple[SourceSeriesBundle, ...]:
    """Fetch proof, never infer a match from an overlapping issue designation.

    Already verified native issues need no detail read. Only the crosswalks
    augment the list row; the normal atomic writer still decides ownership.
    """
    known = {identity for issue in state.issues for identity in issue.identities}
    numbers = {issue.values.issue_number_text for issue in state.issues}
    for bundle in bundles:
        if bundle.series.source is not MetadataSource.METRON_API:
            numbers.update(text for _, text in _validate_bundle(bundle))
    result = []
    for bundle in bundles:
        source = bundle.series.source
        if source is not MetadataSource.METRON_API:
            result.append(bundle)
            continue
        pending = [
            row
            for row, (_, text) in zip(bundle.issues, _validate_bundle(bundle), strict=True)
            if text in numbers
            and not row.cross_identities
            and ExternalIdentityRef(
                source.identity_namespace, MetadataEntityKind.ISSUE, row.external_id
            )
            not in known
        ]
        if len(pending) > _MAX_DETAIL_READS:
            raise IssueCatalogConflictError(
                "The linked Metron catalog needs more issue identity checks than one refresh "
                "can safely perform. Review the issue matches; no library metadata was changed."
            )
        checked = {}
        for row in pending:
            fetched = await registry.issue(source, row.external_id)
            detail = fetched.data
            if fetched.status is not SourceStatus.OK or detail is None:
                raise IssueCatalogConflictError(
                    "Metron issue identities could not be verified. Check source availability "
                    "and retry the refresh; no library metadata was changed."
                )
            if detail.series_external_id != row.series_external_id or parse_issue_number_text(
                detail.issue_number_text
            ) != parse_issue_number_text(row.issue_number_text):
                raise IssueCatalogConflictError(
                    "Metron issue details disagree with its catalog. Review the issue match; "
                    "no library metadata was changed."
                )
            checked[row.external_id] = row.model_copy(
                update={"cross_identities": detail.cross_identities}
            )
        enriched = replace(
            bundle, issues=tuple(checked.get(row.external_id, row) for row in bundle.issues)
        )
        _validate_bundle(enriched)
        result.append(enriched)
    return tuple(result)
