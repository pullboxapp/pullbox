"""Source labels and fresh exact ownership for the existing Add Story Arc page."""

from urllib.parse import urlencode

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.config import get_settings
from pullbox.core.metadata_identity import MetadataEntityKind, MetadataSource
from pullbox.providers.metadata.gcd_local import GcdLocalSource
from pullbox.schemas.metadata_sources import (
    SourceCapability,
    SourceStatus,
    StoryArcDiscoveryQuery,
    StoryArcDiscoveryRead,
    StoryArcSourceOutcome,
)
from pullbox.services.metadata_arc_search import collect_arc_candidates
from pullbox.services.metadata_discovery import (
    MetadataSourceError,
    MetadataSourceRegistry,
    describe_source_policies,
)
from pullbox.services.metadata_search_cache import MetadataSearchBusyError, discovery_cache_key
from pullbox.services.metadata_sources import load_source_runtime
from pullbox.services.provider_artwork import allowed_artwork_url
from pullbox.services.story_arc_catalog_identity import catalog_owners
from pullbox.ui.metadata_series_search import SOURCE_LABELS, source_messages
from pullbox.ui.series_routes import _request_search_cache


async def arc_search_context(
    request: Request,
    session: AsyncSession,
    *,
    q: str,
    page: int,
    source: MetadataSource | None,
    base_url: str,
) -> dict[str, object]:
    flag = get_settings().metadata_gcd_api_v2_enabled
    runtime = await load_source_runtime(session, gcd_api_enabled=flag)
    descriptors = describe_source_policies([item.policy for item in runtime], gcd_api_enabled=flag)
    capable = [row for row in descriptors if SourceCapability.STORY_ARC_SEARCH in row.capabilities]
    selected = [source] if source is not None else [row.source for row in capable if row.enabled]
    query = q.strip()
    snapshot = StoryArcDiscoveryRead(results=[], sources=[])
    error = ""
    await session.rollback()
    if len(query) >= 2 and selected:
        search = StoryArcDiscoveryQuery(query=query, sources=selected)
        registry = MetadataSourceRegistry(runtime, gcd_api_enabled=flag)
        gcd_runtime = next(
            (
                item
                for item in runtime
                if item.policy.source is MetadataSource.GCD_LOCAL
                and item.policy.enabled
                and item.policy.source in selected
            ),
            None,
        )
        generation = None
        if gcd_runtime is not None:
            try:
                generation = await GcdLocalSource(gcd_runtime.gcd_snapshot).cache_token()
            except MetadataSourceError:
                generation = "unreadable"
        try:
            snapshot = await _request_search_cache(request).get_arcs(
                discovery_cache_key(
                    search,
                    runtime,
                    catalog_generation=None,
                    gcd_api_enabled=flag,
                    gcd_generation=generation,
                ),
                lambda: collect_arc_candidates(registry, search),
                cache_result=generation != "unreadable",
            )
            if gcd_runtime is not None:
                status = SourceStatus.INVALID_CONFIG
                try:
                    changed = (
                        await GcdLocalSource(gcd_runtime.gcd_snapshot).cache_token() != generation
                    )
                except MetadataSourceError as exc:
                    changed, status = True, exc.status
                if changed:
                    snapshot.results = [
                        row
                        for row in snapshot.results
                        if row.source is not MetadataSource.GCD_LOCAL
                    ]
                    for row in snapshot.results:
                        row.also_from = [
                            item for item in row.also_from if item is not MetadataSource.GCD_LOCAL
                        ]
                    snapshot.sources = [
                        StoryArcSourceOutcome(source=row.source, status=status, truncated=True)
                        if row.source is MetadataSource.GCD_LOCAL
                        else row
                        for row in snapshot.sources
                    ]
        except MetadataSearchBusyError as exc:
            error = str(exc)
    if not snapshot.results and any(
        row.status not in {SourceStatus.OK, SourceStatus.EMPTY} for row in snapshot.sources
    ):
        error = "Story Arc search failed. Check the source status below and retry."
    total = len(snapshot.results)
    total_pages = max(1, (total + 19) // 20)
    page = min(page, total_pages)
    visible = snapshot.results[(page - 1) * 20 : page * 20]
    owners = {}
    for transport in {row.source for row in visible}:
        found = await catalog_owners(
            session,
            MetadataEntityKind.STORY_ARC,
            [row.external_id for row in visible if row.source is transport],
            transport,
        )
        owners.update({(transport.identity_namespace, key): value for key, value in found.items()})
    results = []
    for row in visible:
        results.append(
            {
                "metadata": {
                    **row.model_dump(),
                    "provider_id": row.external_id,
                    "cover_url": row.image_url
                    if row.image_url and allowed_artwork_url(row.image_url)
                    else None,
                },
                "existing_id": owners.get((row.identity_namespace, row.external_id)),
                "source_label": SOURCE_LABELS[row.source],
                "dom_id": f"arc-result-{row.source.value}-{row.external_id}",
                "preview_url": f"/story-arcs/catalog/{row.source.value}/{row.external_id}",
            }
        )
    params = {"q": query, **({"source": source.value} if source else {})}
    return {
        "query": query,
        "catalog_query": query,
        "results": results,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "shown_count": len(results),
        "in_library_count": sum(bool(row["existing_id"]) for row in results),
        "error_message": error,
        "source_notices": source_messages(snapshot) if len(query) >= 2 else [],
        "source_options": [
            ("", "All enabled sources"),
            *[(r.source.value, SOURCE_LABELS[r.source]) for r in capable],
        ],
        "source_filter": source.value if source else "",
        "search_source_label": SOURCE_LABELS[source] if source else "All enabled sources",
        "pagination_base_url": f"{base_url}?{urlencode(params)}",
        "next_url": f"{base_url}?{urlencode({**params, 'page': page + 1})}"
        if page < total_pages
        else "",
        "previous_url": f"{base_url}?{urlencode({**params, 'page': page - 1})}" if page > 1 else "",
    }
