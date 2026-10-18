"""Provider-aware Add Series presentation; never attach identities from search."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from sqlalchemy import and_, or_, select

from pullbox.core.metadata_identity import IdentityNamespace, MetadataSource
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.core.naming import format_series_folder
from pullbox.models.metadata_identity import SeriesExternalIdentity
from pullbox.models.series import Series
from pullbox.providers.metadata.gcd_local import GcdLocalSource
from pullbox.providers.metadata.sources import catalog_search_cache_token
from pullbox.schemas.metadata_sources import (
    ProviderSeriesRead,
    SeriesDiscoveryQuery,
    SeriesDiscoveryRead,
    SourceOutcome,
    SourceStatus,
    StoryArcDiscoveryRead,
)
from pullbox.services.catalog.contract import CatalogError
from pullbox.services.metadata_discovery import MetadataSourceError, MetadataSourceRegistry
from pullbox.services.metadata_search_cache import MetadataSearchCache, discovery_cache_key
from pullbox.services.provider_artwork import allowed_artwork_url

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.services.metadata_sources import SourceRuntime

SOURCE_LABELS = {
    MetadataSource.COMICVINE_LOCAL: "ComicVine local",
    MetadataSource.COMICVINE_API: "ComicVine API",
    MetadataSource.METRON_API: "Metron",
    MetadataSource.GCD_LOCAL: "GCD local",
    MetadataSource.GCD_API_V2: "GCD API",
}

_OUTCOME_TEXT = {
    SourceStatus.EMPTY: "no matches",
    SourceStatus.DISABLED: "disabled in Metadata settings",
    SourceStatus.FEATURE_DISABLED: "not enabled for this release",
    SourceStatus.NOT_IMPLEMENTED: "not available yet",
    SourceStatus.UNCONFIGURED: "needs credentials in Metadata settings",
    SourceStatus.INVALID_CONFIG: "check its configuration in Metadata settings",
    SourceStatus.AUTHENTICATION_FAILED: "credentials were rejected; check Metadata settings",
    SourceStatus.RATE_LIMITED: "rate limited; try again later",
    SourceStatus.TIMEOUT: "timed out; try again later",
    SourceStatus.UNAVAILABLE: "unavailable; check its status in Metadata settings",
    SourceStatus.INCOMPATIBLE_RESPONSE: "returned an unreadable response",
    SourceStatus.UNSUPPORTED: "does not support series search",
}


def _resource_url(row: ProviderSeriesRead) -> str | None:
    if not row.resource_url:
        return None
    try:
        url = urlsplit(row.resource_url)
        hosts, path = {
            IdentityNamespace.COMICVINE: (
                {"comicvine.gamespot.com", "comicvine.com"},
                f"/volume/4050-{row.external_id}/",
            ),
            IdentityNamespace.METRON: ({"metron.cloud"}, f"/series/{row.external_id}/"),
            IdentityNamespace.GCD: (
                {"www.comics.org", "comics.org"},
                f"/series/{row.external_id}/",
            ),
        }.get(row.identity_namespace, (set(), ""))
        if (
            url.scheme == "https"
            and url.hostname in hosts
            and url.port in {None, 443}
            and url.path == path
            and not url.username
            and not url.password
            and not url.query
            and not url.fragment
            and not any(char.isspace() or ord(char) < 32 for char in row.resource_url)
        ):
            return row.resource_url
    except ValueError:
        pass
    return None


def source_messages(snapshot: SeriesDiscoveryRead | StoryArcDiscoveryRead) -> list[str]:
    if not snapshot.sources:
        return ["No search sources are enabled. Enable a source in Metadata settings."]
    messages = []
    for outcome in snapshot.sources:
        detail = _OUTCOME_TEXT.get(outcome.status)
        if outcome.source is MetadataSource.METRON_API and outcome.retry_after_seconds:
            waiting = {
                SourceStatus.RATE_LIMITED: "requests paused",
                SourceStatus.TIMEOUT: "request timed out",
                SourceStatus.UNAVAILABLE: "temporarily unavailable",
            }.get(outcome.status)
            if waiting:
                seconds = outcome.retry_after_seconds
                unit = "second" if seconds == 1 else "seconds"
                detail = f"{waiting}; try again in {seconds} {unit}"
        if outcome.status is SourceStatus.UNSUPPORTED and isinstance(
            snapshot, StoryArcDiscoveryRead
        ):
            detail = "does not support Story Arc search"
        if detail is None and outcome.truncated:
            detail = "showing a limited result set; narrow the search for more"
        if detail is None and outcome.rejected_results:
            detail = "some results could not be read"
        if detail is not None:
            messages.append(f"{SOURCE_LABELS[outcome.source]}: {detail}.")
    return messages


async def search_snapshot(
    query: SeriesDiscoveryQuery,
    runtime: Sequence[SourceRuntime],
    cache: MetadataSearchCache,
    *,
    gcd_api_enabled: bool,
) -> SeriesDiscoveryRead:
    registry = MetadataSourceRegistry(runtime, gcd_api_enabled=gcd_api_enabled)
    local_selected = any(
        item.policy.source is MetadataSource.COMICVINE_LOCAL
        and item.policy.enabled
        and (query.sources is None or item.policy.source in query.sources)
        for item in runtime
    )
    generation = None
    cacheable = True
    gcd_runtime = next(
        (
            item
            for item in runtime
            if item.policy.source is MetadataSource.GCD_LOCAL
            and item.policy.enabled
            and (query.sources is None or item.policy.source in query.sources)
        ),
        None,
    )
    gcd_generation = None
    if gcd_runtime is not None:
        try:
            gcd_generation = await GcdLocalSource(gcd_runtime.gcd_snapshot).cache_token()
        except MetadataSourceError:
            gcd_generation, cacheable = "unreadable", False
    if local_selected:
        try:
            async with asyncio.timeout(8):
                generation = await catalog_search_cache_token()
        except (CatalogError, OSError, ValueError, TimeoutError, MetadataSourceError):
            # A broken local catalog must not suppress healthy remote results.
            generation = "unreadable"
            cacheable = False

    async def load() -> SeriesDiscoveryRead:
        result = (
            await registry.discover(query)
            if query.search_mode == "preview"
            else await registry.discover_all(query)
        )
        if generation != "unreadable" and local_selected:
            try:
                async with asyncio.timeout(8):
                    changed = await catalog_search_cache_token() != generation
            except (CatalogError, OSError, ValueError, TimeoutError, MetadataSourceError):
                changed = True
            if changed:
                # Never present mixed generations as one complete local snapshot,
                # or hide healthy remote candidates because the catalog changed.
                result = result.model_copy(deep=True)
                result.results = [
                    row
                    for row in result.results
                    if row.source is not MetadataSource.COMICVINE_LOCAL
                ]
                for row in result.results:
                    row.also_from = [
                        source
                        for source in row.also_from
                        if source is not MetadataSource.COMICVINE_LOCAL
                    ]
                result.sources = [
                    SourceOutcome(
                        source=outcome.source, status=SourceStatus.UNAVAILABLE, truncated=True
                    )
                    if outcome.source is MetadataSource.COMICVINE_LOCAL
                    else outcome
                    for outcome in result.sources
                ]
        if gcd_runtime is not None and gcd_generation != "unreadable":
            try:
                changed = (
                    await GcdLocalSource(gcd_runtime.gcd_snapshot).cache_token() != gcd_generation
                )
            except MetadataSourceError:
                changed = True
            if changed:
                result = result.model_copy(deep=True)
                result.results = [
                    row for row in result.results if row.source is not MetadataSource.GCD_LOCAL
                ]
                for row in result.results:
                    row.also_from = [
                        source for source in row.also_from if source is not MetadataSource.GCD_LOCAL
                    ]
                result.sources = [
                    SourceOutcome(
                        source=outcome.source, status=SourceStatus.INVALID_CONFIG, truncated=True
                    )
                    if outcome.source is MetadataSource.GCD_LOCAL
                    else outcome
                    for outcome in result.sources
                ]
        return result

    return await cache.get(
        discovery_cache_key(
            query,
            runtime,
            catalog_generation=generation,
            gcd_api_enabled=gcd_api_enabled,
            gcd_generation=gcd_generation,
        ),
        load,
        cache_result=cacheable,
    )


async def format_source_series_results(
    session: AsyncSession,
    results: list[ProviderSeriesRead],
    *,
    folder_template: str,
    replace_illegal: bool,
    colon_replacement: str,
) -> list[dict[str, object]]:
    """Decorate only the visible page with fresh, exact local ownership."""
    if not results:
        return []
    identities = {(item.identity_namespace, item.external_id) for item in results}
    claims = (
        await session.scalars(
            select(SeriesExternalIdentity).where(
                or_(
                    *[
                        and_(
                            SeriesExternalIdentity.identity_namespace == namespace,
                            SeriesExternalIdentity.external_id == external_id,
                        )
                        for namespace, external_id in identities
                    ]
                )
            )
        )
    ).all()
    owners = {
        (claim.identity_namespace, claim.external_id): (
            claim.series_id,
            claim.verification_state is IdentityVerificationState.VERIFIED,
        )
        for claim in claims
    }
    cv_ids = [
        int(external_id)
        for namespace, external_id in identities
        if namespace is IdentityNamespace.COMICVINE and int(external_id) < 2**63
    ]
    if cv_ids:
        # An explicit generic claim takes precedence over the legacy bridge.
        rows = (
            await session.execute(
                select(Series.id, Series.comicvine_id, SeriesExternalIdentity.id)
                .outerjoin(
                    SeriesExternalIdentity,
                    and_(
                        SeriesExternalIdentity.series_id == Series.id,
                        SeriesExternalIdentity.identity_namespace == IdentityNamespace.COMICVINE,
                    ),
                )
                .where(Series.comicvine_id.in_(cv_ids))
            )
        ).all()
        for series_id, cv_id, claim_id in rows:
            key = (IdentityNamespace.COMICVINE, str(cv_id))
            if key not in owners:
                owners[key] = (series_id, claim_id is None)
    formatted = []
    for row in results:
        owner = owners.get((row.identity_namespace, row.external_id))
        formatted.append(
            {
                **row.model_dump(mode="json"),
                "source_label": SOURCE_LABELS[row.source],
                "dom_id": f"metadata-result-{row.identity_namespace.value}-{row.external_id}",
                "publisher_name": row.publisher,
                "resource_url": _resource_url(row),
                "cover_url": row.image_url
                if row.image_url and allowed_artwork_url(row.image_url)
                else None,
                "already_added": bool(owner and owner[1]),
                "identity_needs_review": bool(owner and not owner[1]),
                "library_series_id": owner[0] if owner else None,
                "folder_preview": format_series_folder(
                    title=row.title,
                    year=row.year_start,
                    publisher=row.publisher,
                    template=folder_template,
                    replace_illegal=replace_illegal,
                    colon_replacement=colon_replacement,
                ),
            }
        )
    return formatted
