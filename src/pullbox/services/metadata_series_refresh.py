"""Source-aware series refresh: short read, bounded fetch, revision-checked write."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.config import get_settings
from pullbox.core.exceptions import ValidationError
from pullbox.core.metadata_identity import MetadataEntityKind, MetadataSource
from pullbox.models import Issue, Series
from pullbox.models.metadata_source import MetadataSourceConfig
from pullbox.models.series import IssueCatalogState
from pullbox.schemas.metadata_snapshot import FieldOrigin, MetadataSnapshot, field_domain
from pullbox.schemas.metadata_sources import (
    MetadataDomain,
    ProviderSeriesRead,
    SourceCapability,
    SourceOutcome,
    SourceStatus,
)
from pullbox.services.cover_resolver import resolve_covers_dir
from pullbox.services.metadata_assembly import assemble_metadata
from pullbox.services.metadata_baselines import MetadataBaselineWrite, save_metadata_baselines
from pullbox.services.metadata_catalog_checkpoints import save_full_catalog_checkpoint
from pullbox.services.metadata_catalog_review import apply_catalog_exclusions
from pullbox.services.metadata_discovery import MetadataSourceRegistry
from pullbox.services.metadata_entity_values import apply_series_metadata_values
from pullbox.services.metadata_issue_catalog import (
    IssueCatalogConflictError,
    SourceIssueBatch,
    apply_issue_batch,
)
from pullbox.services.metadata_locg_enrichment import (
    LocgEnrichmentError,
    enrich_series_snapshot,
    read_series_release_facts,
)
from pullbox.services.metadata_read_cache import source_read_cache
from pullbox.services.metadata_refresh_snapshot import fetch_metadata_snapshot
from pullbox.services.metadata_series_adoption import (
    SeriesAdoptionError,
    SeriesCatalogCountError,
    SourceSeriesBundle,
    _validate_bundle,
    fetch_source_series_bundle,
)
from pullbox.services.metadata_series_artwork import with_representative_cover
from pullbox.services.metadata_series_refresh_state import (
    SERIES_FIELDS,
    SeriesRefreshState,
    read_series_refresh_state,
)
from pullbox.services.metadata_service import MetadataService
from pullbox.services.metadata_sources import load_source_runtime
from pullbox.services.metadata_writer_identity import metadata_write_scope
from pullbox.services.provider_artwork import ProviderArtworkClient, pending_provider_cover

logger = structlog.get_logger(__name__)


class SeriesRefreshError(ValueError):
    """The refresh requires a new read or an explicit identity decision."""

    def __init__(
        self,
        message: str,
        *,
        outcomes: tuple[SourceOutcome, ...] = (),
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.outcomes = outcomes
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class SeriesCatalogRefresh:
    series: Series
    created_issue_ids: tuple[int, ...]
    outcomes: tuple[SourceOutcome, ...] = ()


@asynccontextmanager
async def source_series_refresh_transaction(
    session: AsyncSession, series_id: int
) -> AsyncIterator[Series]:
    """Serialize the response before commit, then refresh artwork outside the write."""
    if session.new or session.dirty or session.deleted:
        raise SeriesRefreshError("Finish pending library changes before refreshing metadata.")
    covers = await resolve_covers_dir(session)
    try:
        series = await refresh_series_from_sources(session, series_id)
        cover_url = series.cover_url
        yield series
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    if cover_url:
        await refresh_series_artwork(session, series_id, cover_url, covers)


async def refresh_series_artwork(
    session: AsyncSession, series_id: int, url: str, covers: Path
) -> None:
    """Best-effort optional cache work after the caller commits metadata."""
    try:
        await _refresh_artwork(session, series_id, url, covers)
    except (OSError, SQLAlchemyError) as exc:
        logger.warning(
            "metadata_series_artwork_refresh_failed",
            series_id=series_id,
            error_type=type(exc).__name__,
        )


async def _refresh_artwork(session: AsyncSession, series_id: int, url: str, covers: Path) -> None:
    destination = covers / str(series_id) / "series.jpg"
    async with (
        ProviderArtworkClient() as client,
        pending_provider_cover(client, url, destination) as pending,
    ):
        if pending is None:
            return
        async with session.begin():
            # A different refresh/delete may have won while the image downloaded.
            current = await session.scalar(
                select(Series)
                .where(Series.id == series_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if current is not None and current.cover_url == url:
                pending.replace(destination)
                current.cover_path = f"/api/v1/series/{series_id}/cover"


async def refresh_series_from_sources(
    session: AsyncSession,
    series_id: int,
    *,
    registry: MetadataSourceRegistry | None = None,
    replace_managed: bool = True,
) -> Series:
    """Refresh metadata through the canonical writer, preserving the public result."""
    result = await refresh_series_catalog_from_sources(
        session, series_id, registry=registry, replace_managed=replace_managed
    )
    return result.series


async def refresh_series_catalog_from_sources(
    session: AsyncSession,
    series_id: int,
    *,
    registry: MetadataSourceRegistry | None = None,
    replace_managed: bool = True,
) -> SeriesCatalogRefresh:
    """Release only a clean read transaction; leave the atomic write to the caller.

    No library/archive files are modified. The caller owns response construction,
    commit, and post-commit cover work. Never call from a pending edit transaction.
    """
    if session.new or session.dirty or session.deleted:
        raise SeriesRefreshError("Finish pending library changes before refreshing metadata.")
    try:
        before = await read_series_refresh_state(session, series_id)
        release_facts = await read_series_release_facts(
            session, before.series.identities, now=datetime.now(UTC)
        )
        if not before.series.identities:
            raise SeriesRefreshError(
                "This series needs a verified metadata identity before refresh. Review its match."
            )
        if registry is None:
            enabled = get_settings().metadata_gcd_api_v2_enabled
            runtime = await load_source_runtime(session, gcd_api_enabled=enabled)
            registry = MetadataSourceRegistry(
                runtime,
                gcd_api_enabled=enabled,
                read_cache=source_read_cache(session),
                revalidate_reads=True,
                total_timeout=60,
            )
        else:
            registry = MetadataSourceRegistry(
                list(registry.runtime.values()),
                factories=registry.factories,
                gcd_api_enabled=registry.gcd_api_enabled,
                read_cache=registry.read_cache,
                revalidate_reads=True,
                per_source_timeout=registry.per_source_timeout,
                total_timeout=registry.total_timeout,
                concurrency=registry.concurrency,
            )
        await session.rollback()
        now = datetime.now(UTC)
        async with asyncio.timeout(120):
            fetched = await fetch_metadata_snapshot(
                registry,
                MetadataEntityKind.SERIES,
                before.series.identities,
                now=now,
                requested_fields=SERIES_FIELDS,
                current=before.series.values,
                previous=before.series.baseline,
                overrides=before.series.overrides,
                replace_managed=replace_managed,
            )
            profiles = {
                item.source: item
                for item in fetched.candidates
                if isinstance(item, ProviderSeriesRead)
            }
            failed = {
                item.source: item
                for item in fetched.outcomes
                if item.status not in {SourceStatus.OK, SourceStatus.NOT_QUERIED}
            }
            bundle = await _catalog(registry, before, profiles, failed)
            profile = profiles.get(MetadataSource.METRON_API)
            if (
                profile is not None
                and not profile.image_url
                and bundle.series.source is not profile.source
            ):
                page = await registry.issues(profile.source, profile.external_id)
                if page.status is SourceStatus.OK and page.data is not None:
                    profiles[profile.source] = with_representative_cover(profile, page.data.results)
        profiles[bundle.series.source] = bundle.series
        snapshot = assemble_metadata(
            MetadataEntityKind.SERIES,
            before.series.identities,
            list(profiles.values()),
            before.policies,
            now=now,
            current=before.series.values,
            previous=before.series.baseline,
            overrides=before.series.overrides,
            replace_managed=replace_managed,
            fields=SERIES_FIELDS,
        )
        async with metadata_write_scope(session):
            # Source policy writers never take entity locks. Lock policies first,
            # followed by the same parent-first graph order as identity writers.
            await session.execute(
                select(MetadataSourceConfig)
                .order_by(MetadataSourceConfig.source)
                .with_for_update(read=True)
            )
            await session.execute(select(Series.id).where(Series.id == series_id).with_for_update())
            await session.execute(
                select(Issue.id)
                .where(Issue.series_id == series_id)
                .order_by(Issue.id)
                .with_for_update()
            )
            current = await read_series_refresh_state(session, series_id)
            if current != before:
                raise SeriesRefreshError(
                    "Library metadata, identities or source settings changed. Retry the refresh."
                )
            rechecked_facts = await read_series_release_facts(
                session, current.series.identities, now=datetime.now(UTC), lock=True
            )
            if rechecked_facts != release_facts:
                raise SeriesRefreshError(
                    "Cached release facts changed or expired during refresh. Retry the refresh."
                )
            snapshot = enrich_series_snapshot(snapshot, rechecked_facts, now=now)
            result = await _apply(
                session, current, bundle, snapshot, now, replace_managed=replace_managed
            )
            if bundle.catalog_started_at is not None:
                checkpoint = next(
                    (item for item in current.checkpoints if item.source is bundle.series.source),
                    None,
                )
                identity_revision = next(
                    revision
                    for ref, _, revision in current.series.claims
                    if ref.namespace is bundle.series.identity_namespace
                    and ref.external_id == bundle.series.external_id
                )
                await save_full_catalog_checkpoint(
                    session,
                    result.series.id,
                    source=bundle.series.source,
                    source_revision=bundle.source_revision,
                    identity_revision=identity_revision,
                    external_id=bundle.series.external_id,
                    started_at=bundle.catalog_started_at,
                    source_updated_at=bundle.series.source_updated_at,
                    expected_revision=checkpoint.revision if checkpoint else 0,
                )
            await session.flush()
        logger.info(
            "metadata_series_source_refreshed",
            series_id=series_id,
            source=bundle.series.source.value,
            catalog_count=bundle.catalog_total,
        )
        return replace(result, outcomes=tuple(failed.values()))
    except SeriesRefreshError:
        raise
    except IssueCatalogConflictError as exc:
        raise SeriesRefreshError(str(exc)) from exc
    except LocgEnrichmentError as exc:
        raise SeriesRefreshError(str(exc)) from exc
    except (ValueError, IntegrityError, ValidationError) as exc:
        raise SeriesRefreshError(
            "Metadata could not be refreshed safely. Review the series match "
            "or retry after source settings are corrected."
        ) from exc
    except TimeoutError as exc:
        raise SeriesRefreshError(
            "Metadata refresh timed out. No library metadata was changed; retry later.",
            retry_after_seconds=300,
        ) from exc


async def _catalog(
    registry: MetadataSourceRegistry,
    state: SeriesRefreshState,
    profiles: dict[MetadataSource, ProviderSeriesRead],
    failed: dict[MetadataSource, SourceOutcome],
) -> SourceSeriesBundle:
    known = {item.namespace: item.external_id for item in state.series.identities}
    inconsistent_counts = False
    catalog_namespaces = {
        identity.namespace for issue in state.issues for identity in issue.identities
    }
    sources = sorted(
        registry.runtime,
        key=lambda source: (
            registry.runtime[source].policy.domain_priorities.get(
                MetadataDomain.ISSUES, registry.runtime[source].policy.priority
            ),
            source.value,
        ),
    )
    for source in sources:
        if (
            source in failed
            or source.identity_namespace not in known
            # A series link authorizes descriptive enrichment, not issue rematching.
            or (catalog_namespaces and source.identity_namespace not in catalog_namespaces)
            or registry._unavailable(source, capability=SourceCapability.ISSUE_LIST)
        ):
            continue
        try:
            bundle = await fetch_source_series_bundle(
                registry,
                source,
                known[source.identity_namespace],
                source_revision=registry.runtime[source].policy.revision,
                profile=profiles.get(source),
            )
            bundle = apply_catalog_exclusions(bundle, state.catalog_exclusions)
            _validate_bundle(bundle)
            return bundle
        except SeriesCatalogCountError:
            # Do not accept an incomplete catalog or mistake a stale summary for
            # an identity dispute. Another eligible source must pass every check.
            inconsistent_counts = True
            failed[source] = SourceOutcome(source=source, status=SourceStatus.INCOMPATIBLE_RESPONSE)
            logger.info(
                "metadata_series_catalog_count_mismatch",
                series_id=state.series.local_id,
                source=source.value,
            )
        except SeriesAdoptionError as exc:
            if exc.status is None or exc.status is SourceStatus.INCOMPATIBLE_RESPONSE:
                raise SeriesRefreshError(
                    "The provider issue catalog changed or disagrees with this series. "
                    "Review the issue match before retrying."
                ) from exc
            logger.info(
                "metadata_series_catalog_unavailable", source=source.value, status=exc.status.value
            )
            failed[source] = SourceOutcome(
                source=source, status=exc.status, retry_after_seconds=exc.retry_after_seconds
            )
    if inconsistent_counts:
        raise SeriesRefreshError(
            "A metadata source reported inconsistent issue counts, and no configured source "
            "could supply a complete catalog. Check source availability in Metadata settings "
            "or retry later. Existing issue matches and files were kept.",
            outcomes=tuple(failed.values()),
        )
    raise SeriesRefreshError(
        "No configured source could supply a complete issue catalog. "
        "Check source status and retry.",
        outcomes=tuple(failed.values()),
    )


def _with_derived(
    snapshot: MetadataSnapshot, field: str, value: object, now: datetime
) -> MetadataSnapshot:
    if getattr(snapshot.values, field) == value:
        return snapshot
    origins = {item.field: item for item in snapshot.origins}
    origins[field] = FieldOrigin(
        field=field,
        domain=field_domain(snapshot.entity_kind, field),
        observed_at=now,
        derivation="catalog" if field == "issue_count" else "lifecycle",
    )
    return MetadataSnapshot.model_validate(
        {
            **snapshot.model_dump(),
            "values": {**snapshot.values.model_dump(), field: value},
            "origins": tuple(origins.values()),
        }
    )


async def _apply(
    session: AsyncSession,
    state: SeriesRefreshState,
    bundle: SourceSeriesBundle,
    snapshot: MetadataSnapshot,
    now: datetime,
    *,
    replace_managed: bool,
) -> SeriesCatalogRefresh:
    series = await session.get(Series, state.series.local_id)
    assert series is not None
    await apply_series_metadata_values(session, series, snapshot.values)
    created = await apply_issue_batch(
        session,
        state,
        SourceIssueBatch(
            bundle.series.source, bundle.series.external_id, bundle.issues, bundle.source_revision
        ),
        now,
        complete=True,
        replace_managed=replace_managed,
    )
    series.issue_count = len(state.issues) + len(created)
    snapshot = _with_derived(snapshot, "issue_count", series.issue_count, now)
    origins = {item.field: item for item in snapshot.origins}
    status_origin = origins.get("status")
    if (
        status_origin is not None
        and status_origin.derivation == "lifecycle"
        and not series.status_override
    ):
        await MetadataService.infer_series_status(session, series)
        snapshot = _with_derived(snapshot, "status", series.status.value, now)
        snapshot = _with_derived(snapshot, "year_end", series.year_end, now)
    series.metadata_last_refreshed = now
    series.issue_catalog_state = IssueCatalogState.COMPLETE
    series.issue_catalog_last_synced_at = now
    series.issue_catalog_last_checked_at = now
    series.issue_catalog_error = None
    await save_metadata_baselines(
        session, [MetadataBaselineWrite(series.id, snapshot, state.series.baseline_revision)]
    )
    return SeriesCatalogRefresh(series, created)
