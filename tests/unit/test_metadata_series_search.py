"""Search snapshots preserve healthy sources when a local catalog changes."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from pullbox.core.metadata_identity import MetadataSource as Source
from pullbox.schemas.metadata_sources import (
    ProviderSeriesRead,
    SeriesDiscoveryQuery,
    SeriesDiscoveryRead,
    SourceOutcome,
    SourceStatus,
)
from pullbox.services.metadata_search_cache import MetadataSearchBusyError, MetadataSearchCache
from pullbox.services.metadata_sources import SourceRuntime, default_policy
from pullbox.ui import metadata_series_search as search


async def test_generation_change_drops_local_rows_but_keeps_healthy_remote_results(monkeypatch):
    local, remote = Source.COMICVINE_LOCAL, Source.METRON_API
    snapshot = SeriesDiscoveryRead(
        results=[
            ProviderSeriesRead(
                source=source,
                identity_namespace=source.identity_namespace,
                external_id="42",
                title=f"{source} match",
            )
            for source in (local, remote)
        ],
        sources=[
            SourceOutcome(source=source, status=SourceStatus.OK) for source in (local, remote)
        ],
    )
    read = AsyncMock(return_value=snapshot)
    monkeypatch.setattr(search.MetadataSourceRegistry, "discover_all", read)
    monkeypatch.setattr(
        search, "catalog_search_cache_token", AsyncMock(side_effect=["one", "two", "two", "two"])
    )
    runtime = [SourceRuntime(default_policy(local)), SourceRuntime(default_policy(remote))]
    cache = MetadataSearchCache()
    result = await search.search_snapshot(
        SeriesDiscoveryQuery(query="Example", search_mode="full"),
        runtime,
        cache,
        gcd_api_enabled=False,
    )
    assert [row.source for row in result.results] == [remote]
    assert result.sources[0].status is SourceStatus.UNAVAILABLE
    assert result.sources[1].status is SourceStatus.OK
    await search.search_snapshot(
        SeriesDiscoveryQuery(query="Example", search_mode="full"),
        runtime,
        cache,
        gcd_api_enabled=False,
    )
    assert read.await_count == 2


def test_no_enabled_sources_has_an_actionable_message():
    assert search.source_messages(SeriesDiscoveryRead(results=[], sources=[])) == [
        "No search sources are enabled. Enable a source in Metadata settings."
    ]


def test_metron_backoff_message_includes_when_to_retry():
    snapshot = SeriesDiscoveryRead(
        results=[],
        sources=[
            SourceOutcome(
                source=Source.METRON_API,
                status=SourceStatus.RATE_LIMITED,
                retry_after_seconds=4,
            )
        ],
    )
    assert search.source_messages(snapshot) == ["Metron: requests paused; try again in 4 seconds."]


async def test_unreadable_catalog_does_not_bypass_search_admission(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    async def load(query):
        started.set()
        await release.wait()
        return SeriesDiscoveryRead(results=[], sources=[])

    monkeypatch.setattr(search.MetadataSourceRegistry, "discover_all", AsyncMock(side_effect=load))
    monkeypatch.setattr(
        search, "catalog_search_cache_token", AsyncMock(side_effect=OSError("unreadable"))
    )
    runtime = [SourceRuntime(default_policy(Source.COMICVINE_LOCAL))]
    cache = MetadataSearchCache(max_pending=1)
    first = asyncio.create_task(
        search.search_snapshot(
            SeriesDiscoveryQuery(query="First", search_mode="full"),
            runtime,
            cache,
            gcd_api_enabled=False,
        )
    )
    await started.wait()
    try:
        with pytest.raises(MetadataSearchBusyError):
            await asyncio.wait_for(
                search.search_snapshot(
                    SeriesDiscoveryQuery(query="Second", search_mode="full"),
                    runtime,
                    cache,
                    gcd_api_enabled=False,
                ),
                timeout=0.2,
            )
    finally:
        release.set()
        await first


async def test_gcd_search_cache_reuses_reads_but_never_hides_replaced_dump(tmp_path, monkeypatch):
    from pullbox.providers.metadata.gcd_local_database import validate_candidate
    from pullbox.schemas.metadata_sources import SourceSettings
    from tests.api.test_gcd_local import gcd_dump

    path = gcd_dump(tmp_path / "gcd.db")
    snapshot = await validate_candidate(str(path))
    runtime = [
        SourceRuntime(
            default_policy(Source.GCD_LOCAL).model_copy(
                update={
                    "enabled": True,
                    "settings": SourceSettings(database_path=str(path)),
                    "revision": 1,
                }
            ),
            gcd_snapshot=snapshot,
        )
    ]
    original = search.MetadataSourceRegistry.discover
    calls = []

    async def counted(self, query):
        calls.append(query)
        return await original(self, query)

    monkeypatch.setattr(search.MetadataSourceRegistry, "discover", counted)
    cache = MetadataSearchCache()
    query = SeriesDiscoveryQuery(query="Swamp Thing", sources=[Source.GCD_LOCAL])
    for _ in range(2):
        result = await search.search_snapshot(query, runtime, cache, gcd_api_enabled=False)
        assert result.results[0].external_id == "2999"
    assert len(calls) == 1
    path.touch()
    result = await search.search_snapshot(query, runtime, cache, gcd_api_enabled=False)
    assert result.results == []
    assert result.sources[0].status is SourceStatus.INVALID_CONFIG
