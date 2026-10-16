"""Refresh one verified issue without changing its designation, ownership or files."""

import asyncio
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.exceptions import NotFoundError
from pullbox.core.issue_numbers import normalize_issue_number_text
from pullbox.core.metadata_identity import MetadataEntityKind
from pullbox.models import Issue, Series
from pullbox.models.metadata_source import MetadataSourceConfig
from pullbox.schemas.issue_metadata_links import IssueRefreshRead
from pullbox.schemas.metadata_sources import ProviderIssueRead, SourceStatus
from pullbox.services.metadata_baselines import MetadataBaselineWrite, save_metadata_baselines
from pullbox.services.metadata_credits import write_issue_credits
from pullbox.services.metadata_discovery import MetadataSourceRegistry
from pullbox.services.metadata_entity_values import apply_issue_metadata_values
from pullbox.services.metadata_locg_enrichment import (
    enrich_issue_snapshot,
    read_issue_release_facts,
    read_series_release_facts,
)
from pullbox.services.metadata_read_cache import source_read_cache
from pullbox.services.metadata_refresh_snapshot import fetch_metadata_snapshot
from pullbox.services.metadata_series_refresh_state import ISSUE_FIELDS, read_series_refresh_state
from pullbox.services.metadata_sources import load_source_runtime
from pullbox.services.metadata_writer_identity import metadata_write_scope

logger = structlog.get_logger(__name__)


async def refresh_issue_from_sources(
    session: AsyncSession,
    issue_id: int,
    *,
    gcd_api_enabled: bool,
    registry: MetadataSourceRegistry | None = None,
) -> IssueRefreshRead:
    """Read/fetch/revalidate/apply in a caller-owned transaction, never an archive writer."""
    if session.new or session.dirty or session.deleted:
        raise ValueError("Finish pending library changes before refreshing metadata.")
    series_id = await session.scalar(select(Issue.series_id).where(Issue.id == issue_id))
    if series_id is None:
        raise NotFoundError("Issue", issue_id)
    before = await read_series_refresh_state(session, series_id, issue_ids=(issue_id,))
    member = before.issues[0]
    if not member.identities:
        raise ValueError("Link a verified metadata provider to this issue before refreshing.")
    release_facts = await read_series_release_facts(
        session, before.series.identities, now=datetime.now(UTC)
    )
    issue_release_facts = await read_issue_release_facts(session, before, release_facts)
    if registry is None:
        registry = MetadataSourceRegistry(
            await load_source_runtime(session, gcd_api_enabled=gcd_api_enabled),
            gcd_api_enabled=gcd_api_enabled,
            read_cache=source_read_cache(session),
            total_timeout=60,
        )
    await session.rollback()
    try:
        async with asyncio.timeout(65):
            fetched = await fetch_metadata_snapshot(
                registry,
                MetadataEntityKind.ISSUE,
                member.identities,
                now=datetime.now(UTC),
                requested_fields=ISSUE_FIELDS,
                current=member.values,
                previous=member.baseline,
                overrides=member.overrides,
                replace_managed=True,
                parent_identities=before.series.identities,
            )
    except TimeoutError as exc:
        raise ValueError(
            "Metadata refresh timed out. No issue metadata was changed; retry later."
        ) from exc
    failures = [
        item
        for item in fetched.outcomes
        if item.status
        not in {
            SourceStatus.OK,
            SourceStatus.NOT_QUERIED,
            SourceStatus.DISABLED,
            SourceStatus.FEATURE_DISABLED,
        }
    ]
    if not fetched.candidates and (
        failures
        or not any(
            item.status in {SourceStatus.OK, SourceStatus.NOT_QUERIED} for item in fetched.outcomes
        )
    ):
        raise ValueError(
            "No linked provider could refresh this issue. Check Metadata settings "
            "or try again later; no metadata was changed."
        )
    if any(
        not isinstance(item, ProviderIssueRead)
        or normalize_issue_number_text(item.issue_number_text) != member.values.issue_number_text
        for item in fetched.candidates
    ):
        raise ValueError(
            "The provider changed this issue's exact number. Review the match before retrying; "
            "no metadata was changed."
        )
    async with metadata_write_scope(session):
        await session.execute(
            select(MetadataSourceConfig)
            .order_by(MetadataSourceConfig.source)
            .with_for_update(read=True)
        )
        await session.execute(select(Series.id).where(Series.id == series_id).with_for_update())
        await session.execute(select(Issue.id).where(Issue.id == issue_id).with_for_update())
        current = await read_series_refresh_state(session, series_id, issue_ids=(issue_id,))
        if before != current:
            raise ValueError(
                "Issue metadata, identities or source settings changed. Retry the refresh; "
                "no metadata was changed."
            )
        rechecked_facts = await read_series_release_facts(
            session, current.series.identities, now=datetime.now(UTC), lock=True
        )
        if rechecked_facts != release_facts:
            raise ValueError(
                "Cached release facts changed or expired during refresh. Retry the refresh; "
                "no metadata was changed."
            )
        rechecked_issues = await read_issue_release_facts(session, current, rechecked_facts)
        if rechecked_issues != issue_release_facts:
            raise ValueError(
                "Cached release issue matches changed during refresh. Retry the refresh; "
                "no metadata was changed."
            )
        snapshot = enrich_issue_snapshot(
            fetched.snapshot,
            next((item for item in rechecked_issues if item.local_id == issue_id), None),
            now=datetime.now(UTC),
        )
        issue = await session.get(Issue, issue_id)
        assert issue is not None
        apply_issue_metadata_values(issue, snapshot.values)
        await write_issue_credits(session, {issue_id: snapshot.values.credits})
        await save_metadata_baselines(
            session, [MetadataBaselineWrite(issue_id, snapshot, member.baseline_revision)]
        )
        await session.flush()
    logger.info(
        "metadata_issue_source_refreshed",
        issue_id=issue_id,
        series_id=series_id,
        partial=bool(failures),
    )
    return IssueRefreshRead(issue_id=issue_id, outcomes=failures)
