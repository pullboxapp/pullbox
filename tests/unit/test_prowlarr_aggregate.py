"""Tests for Prowlarr aggregate search — single ProwlarrIndexer per Prowlarr instance.

Covers:
- ProwlarrIndexer passes indexer_ids and categories as repeated query params
- register_indexers aggregates Prowlarr-synced configs into one ProwlarrIndexer
- register_indexers still registers manual indexers individually
- _search_indexers does NOT inject categories for Prowlarr (post-filter handles it)
- build_registry succeeds when only Prowlarr-synced indexers exist

Run:
    pytest tests/unit/test_prowlarr_aggregate.py -v
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pullbox.providers.base import ReleaseResult, SearchQuery
from pullbox.providers.indexer.prowlarr import ProwlarrIndexer
from pullbox.services.search_service import SearchService


class TestProwlarrIndexerParams:
    """Verify ProwlarrIndexer passes indexer_ids and categories correctly."""

    def test_init_stores_indexer_ids(self) -> None:
        """indexer_ids are stored on the instance."""
        p = ProwlarrIndexer(url="http://prowlarr:9696", api_key="test", indexer_ids=[1, 5, 9])
        assert p._indexer_ids == [1, 5, 9]

    def test_init_default_no_indexer_ids(self) -> None:
        """Without indexer_ids, defaults to None."""
        p = ProwlarrIndexer(url="http://prowlarr:9696", api_key="test")
        assert p._indexer_ids is None

    def test_aggregate_result_keeps_local_id_and_priority(self) -> None:
        results = ProwlarrIndexer._parse_api_results(
            [
                {
                    "title": "Batman 001 (2025)",
                    "indexer": "1337x",
                    "indexerId": 101,
                    "protocol": "torrent",
                    "downloadUrl": "https://prowlarr.test/download/1",
                    "categories": [{"name": "Books/Comics"}],
                }
            ],
            {101: (7, 3)},
        )

        assert results[0].indexer_id == 7
        assert results[0].ranking_priority == 3

    @pytest.mark.asyncio
    async def test_search_sends_categories_as_list(self) -> None:
        """categories param should be a list of ints for repeated query params."""
        p = ProwlarrIndexer(url="http://prowlarr:9696", api_key="test", indexer_ids=[1, 2])

        captured_params: dict[str, Any] = {}

        async def mock_api_request(
            endpoint: str,
            params: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> Any:
            if params:
                captured_params.update(params)
            return []

        p._api_request = mock_api_request  # type: ignore[assignment]

        query = SearchQuery(
            series_title="Batman",
            issue_number=5.0,
            categories=["7000", "7020", "7030"],
        )
        await p.search(query)

        assert captured_params["categories"] == [7000, 7020, 7030]
        assert captured_params["indexerIds"] == [1, 2]

    @pytest.mark.asyncio
    async def test_search_omits_indexer_ids_when_none(self) -> None:
        """indexerIds param should be absent when indexer_ids is None."""
        p = ProwlarrIndexer(url="http://prowlarr:9696", api_key="test")

        captured_params: dict[str, Any] = {}

        async def mock_api_request(
            endpoint: str,
            params: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> Any:
            if params:
                captured_params.update(params)
            return []

        p._api_request = mock_api_request  # type: ignore[assignment]

        query = SearchQuery(series_title="Batman", issue_number=1.0)
        await p.search(query)

        assert "indexerIds" not in captured_params


class TestRegisterIndexersAggregation:
    """Verify register_indexers splits Prowlarr Torznab from Newznab/manual."""

    @pytest.mark.asyncio
    async def test_prowlarr_torznab_creates_single_aggregate(self) -> None:
        """Prowlarr-synced Torznab indexers produce one ProwlarrIndexer."""
        from pullbox.composition.providers import _PROWLARR_AGGREGATE_CONFIG_ID, register_indexers
        from pullbox.providers.base import ProviderRegistry

        configs = []
        for i in range(1, 4):
            cfg = MagicMock()
            cfg.id = i
            cfg.name = f"Torznab-{i}"
            cfg.source = "prowlarr"
            cfg.prowlarr_indexer_id = 100 + i
            cfg.indexer_type = "torznab"
            cfg.url = f"http://prowlarr:9696/{100 + i}"
            cfg.api_key = "encrypted_key"
            cfg.enabled = True
            cfg.priority = i * 10
            configs.append(cfg)

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = configs

        url_cfg = MagicMock()
        url_cfg.value = "http://prowlarr:9696"
        key_cfg = MagicMock()
        key_cfg.value = "encrypted_api_key"
        url_result = MagicMock()
        url_result.scalar_one_or_none.return_value = url_cfg
        key_result = MagicMock()
        key_result.scalar_one_or_none.return_value = key_cfg

        mock_session.execute = AsyncMock(side_effect=[mock_result, url_result, key_result])

        registry = ProviderRegistry()

        with patch("pullbox.composition.providers.decrypt_secret", return_value="decrypted_key"):
            configs_map = await register_indexers(mock_session, registry)

        # Torznab-only → empty configs_map (no health tracking)
        assert configs_map == {}

        items = registry.get_indexer_items()
        assert len(items) == 1
        config_id, indexer = items[0]
        assert config_id == _PROWLARR_AGGREGATE_CONFIG_ID
        assert isinstance(indexer, ProwlarrIndexer)
        assert indexer._indexer_ids == [101, 102, 103]
        assert getattr(indexer, "_indexer_rankings", None) == {
            101: (1, 10),
            102: (2, 20),
            103: (3, 30),
        }
        # Download lookup uses the persisted ID, without adding search fan-out.
        assert registry.get_indexer(1) is indexer
        assert registry.get_indexer(2) is indexer
        assert registry.get_indexer(3) is indexer
        assert registry.get_indexer(999) is None

    @pytest.mark.asyncio
    async def test_prowlarr_newznab_registered_individually(self) -> None:
        """Prowlarr-synced Newznab indexers are registered individually, not aggregated."""
        from pullbox.composition.providers import register_indexers
        from pullbox.providers.base import ProviderRegistry

        cfg = MagicMock()
        cfg.id = 5
        cfg.name = "NZBgeek (Prowlarr)"
        cfg.source = "prowlarr"
        cfg.prowlarr_indexer_id = 1
        cfg.indexer_type = "newznab"
        cfg.url = "http://prowlarr:9696/1"
        cfg.api_key = "encrypted_key"
        cfg.enabled = True

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [cfg]
        mock_session.execute = AsyncMock(return_value=mock_result)

        registry = ProviderRegistry()

        with patch("pullbox.composition.providers.decrypt_secret", return_value="decrypted_key"):
            configs_map = await register_indexers(mock_session, registry)

        # Prowlarr Newznab appears in configs_map for health tracking
        assert 5 in configs_map
        items = registry.get_indexer_items()
        assert len(items) == 1
        assert items[0][0] == 5
        assert not isinstance(items[0][1], ProwlarrIndexer)

    @pytest.mark.asyncio
    async def test_manual_indexers_registered_individually(self) -> None:
        """Manual (non-Prowlarr) indexers are still registered individually."""
        from pullbox.composition.providers import register_indexers
        from pullbox.providers.base import ProviderRegistry

        cfg = MagicMock()
        cfg.id = 42
        cfg.name = "NZBgeek"
        cfg.source = "manual"
        cfg.prowlarr_indexer_id = None
        cfg.indexer_type = "newznab"
        cfg.url = "https://api.nzbgeek.info/"
        cfg.api_key = "encrypted_key"
        cfg.enabled = True

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [cfg]
        mock_session.execute = AsyncMock(return_value=mock_result)

        registry = ProviderRegistry()

        with patch("pullbox.composition.providers.decrypt_secret", return_value="decrypted_key"):
            configs_map = await register_indexers(mock_session, registry)

        assert 42 in configs_map
        items = registry.get_indexer_items()
        assert len(items) == 1
        assert items[0][0] == 42
        assert not isinstance(items[0][1], ProwlarrIndexer)

    @pytest.mark.asyncio
    async def test_manual_torznab_receives_only_its_opted_in_resolver_chain(self) -> None:
        """Composition wires ranked resolvers only into a manual Torznab instance."""
        from pullbox.composition.providers import register_indexers
        from pullbox.providers.base import ProviderRegistry

        cfg = MagicMock()
        cfg.id = 43
        cfg.name = "Manual 1337x proxy"
        cfg.source = "manual"
        cfg.prowlarr_indexer_id = None
        cfg.indexer_type = "torznab"
        cfg.url = "https://torznab.example"
        cfg.api_key = "encrypted_key"
        cfg.enabled = True
        cfg.resolver_enabled = True
        options = (MagicMock(name="resolver-option"),)

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [cfg]
        mock_session.execute = AsyncMock(return_value=mock_result)
        registry = ProviderRegistry()

        with (
            patch("pullbox.composition.providers.decrypt_secret", return_value="decrypted_key"),
            patch(
                "pullbox.composition.providers.build_manual_torznab_resolver_options",
                AsyncMock(return_value=options),
            ) as build_options,
            patch("pullbox.composition.providers.TorznabIndexer") as torznab_cls,
        ):
            await register_indexers(mock_session, registry)

        build_options.assert_awaited_once_with(mock_session, cfg)
        torznab_cls.assert_called_once_with(
            name="Manual 1337x proxy",
            url="https://torznab.example",
            api_key="decrypted_key",
            resolver_enabled=True,
            resolver_options=options,
            cache_namespace="manual-torznab:43",
        )

    @pytest.mark.asyncio
    async def test_jackett_tracker_is_registered_individually_without_pullbox_resolver(
        self,
    ) -> None:
        """Jackett owns challenge solving and each tracker keeps its own identity."""
        from pullbox.composition.providers import register_indexers
        from pullbox.providers.base import ProviderRegistry

        cfg = MagicMock()
        cfg.id = 44
        cfg.name = "1337x (Jackett)"
        cfg.source = "jackett"
        cfg.manager_indexer_id = "1337x"
        cfg.manager_available = True
        cfg.prowlarr_indexer_id = None
        cfg.indexer_type = "torznab"
        cfg.url = "http://jackett:9117/api/v2.0/indexers/1337x/results/torznab"
        cfg.api_key = "encrypted_key"
        cfg.enabled = True
        cfg.resolver_enabled = True

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [cfg]
        mock_session.execute = AsyncMock(return_value=mock_result)
        registry = ProviderRegistry()

        with (
            patch("pullbox.composition.providers.decrypt_secret", return_value="decrypted_key"),
            patch(
                "pullbox.composition.providers.build_manual_torznab_resolver_options",
                AsyncMock(),
            ) as build_options,
            patch("pullbox.composition.providers.TorznabIndexer") as torznab_cls,
        ):
            configs_map = await register_indexers(mock_session, registry)

        assert configs_map == {44: cfg}
        build_options.assert_not_awaited()
        torznab_cls.assert_called_once_with(
            name="1337x (Jackett)",
            url="http://jackett:9117/api/v2.0/indexers/1337x/results/torznab",
            api_key="decrypted_key",
            resolver_enabled=False,
            resolver_options=(),
            cache_namespace="jackett-torznab:44",
            rate_limit_per_minute=60,
            request_timeout=60.0,
        )

    @pytest.mark.asyncio
    async def test_retired_manager_tracker_is_not_registered(self) -> None:
        """A tracker missing from its manager remains historical but cannot search."""
        from pullbox.composition.providers import register_indexers
        from pullbox.providers.base import ProviderRegistry

        cfg = MagicMock()
        cfg.id = 45
        cfg.source = "jackett"
        cfg.manager_available = False
        cfg.indexer_type = "torznab"
        cfg.enabled = True

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [cfg]
        mock_session.execute = AsyncMock(return_value=mock_result)
        registry = ProviderRegistry()

        configs_map = await register_indexers(mock_session, registry)

        assert configs_map == {}
        assert registry.get_indexer_items() == []

    @pytest.mark.asyncio
    async def test_mixed_newznab_and_torznab_prowlarr(self) -> None:
        """Prowlarr Newznab stays individual, Prowlarr Torznab aggregates."""
        from pullbox.composition.providers import _PROWLARR_AGGREGATE_CONFIG_ID, register_indexers
        from pullbox.providers.base import ProviderRegistry

        newznab_cfg = MagicMock()
        newznab_cfg.id = 1
        newznab_cfg.name = "NZBgeek (Prowlarr)"
        newznab_cfg.source = "prowlarr"
        newznab_cfg.prowlarr_indexer_id = 1
        newznab_cfg.indexer_type = "newznab"
        newznab_cfg.url = "http://prowlarr:9696/1"
        newznab_cfg.api_key = "encrypted_key"
        newznab_cfg.enabled = True

        torznab_cfg = MagicMock()
        torznab_cfg.id = 2
        torznab_cfg.name = "1337x (Prowlarr)"
        torznab_cfg.source = "prowlarr"
        torznab_cfg.prowlarr_indexer_id = 55
        torznab_cfg.indexer_type = "torznab"
        torznab_cfg.url = "http://prowlarr:9696/55"
        torznab_cfg.api_key = "encrypted_key"
        torznab_cfg.enabled = True

        mock_session = AsyncMock()
        configs_result = MagicMock()
        configs_result.scalars.return_value.all.return_value = [newznab_cfg, torznab_cfg]

        url_cfg = MagicMock()
        url_cfg.value = "http://prowlarr:9696"
        key_cfg = MagicMock()
        key_cfg.value = "encrypted_api_key"
        url_result = MagicMock()
        url_result.scalar_one_or_none.return_value = url_cfg
        key_result = MagicMock()
        key_result.scalar_one_or_none.return_value = key_cfg

        mock_session.execute = AsyncMock(side_effect=[configs_result, url_result, key_result])

        registry = ProviderRegistry()

        with patch("pullbox.composition.providers.decrypt_secret", return_value="decrypted_key"):
            configs_map = await register_indexers(mock_session, registry)

        # Newznab in configs_map, Torznab is not
        assert 1 in configs_map
        assert 2 not in configs_map

        # Registry has 2 entries: individual Newznab + aggregate Torznab
        items = registry.get_indexer_items()
        assert len(items) == 2
        ids = {cid for cid, _ in items}
        assert 1 in ids
        assert _PROWLARR_AGGREGATE_CONFIG_ID in ids


class TestSearchServiceProwlarrCategories:
    """Verify _search_indexers does NOT inject categories for Prowlarr.

    Prowlarr's REST API interprets categories differently than direct
    Newznab feeds and can exclude valid comic results.  The post-filter
    _is_comic_category() handles non-comic rejection instead.
    """

    @pytest.mark.asyncio
    async def test_prowlarr_indexer_no_category_injection(self) -> None:
        """ProwlarrIndexer should NOT get categories injected — post-filter handles it."""
        mock_registry = MagicMock()

        prowlarr = ProwlarrIndexer(url="http://prowlarr:9696", api_key="test", indexer_ids=[1, 2])

        mock_registry.get_indexer_items.return_value = [(-1, prowlarr)]

        svc = SearchService(mock_registry)

        # Capture the query passed to search
        captured_queries: list[SearchQuery] = []

        async def tracking_search(query: SearchQuery) -> list[ReleaseResult]:
            captured_queries.append(query)
            return []

        prowlarr.search = tracking_search  # type: ignore[assignment]

        query = SearchQuery(series_title="Batman", issue_number=5.0)
        await svc._search_indexers(query)

        assert len(captured_queries) == 1
        assert captured_queries[0].categories is None

    @pytest.mark.asyncio
    async def test_prowlarr_preserves_explicit_categories(self) -> None:
        """When query already has categories, they are passed through."""
        mock_registry = MagicMock()

        prowlarr = ProwlarrIndexer(url="http://prowlarr:9696", api_key="test", indexer_ids=[1])

        mock_registry.get_indexer_items.return_value = [(-1, prowlarr)]

        svc = SearchService(mock_registry)

        captured_queries: list[SearchQuery] = []

        async def tracking_search(query: SearchQuery) -> list[ReleaseResult]:
            captured_queries.append(query)
            return []

        prowlarr.search = tracking_search  # type: ignore[assignment]

        query = SearchQuery(series_title="Batman", issue_number=5.0, categories=["7030"])
        await svc._search_indexers(query)

        assert len(captured_queries) == 1
        assert captured_queries[0].categories == ["7030"]


class TestBuildRegistryProwlarrOnly:
    """Verify build_registry works when only Prowlarr Torznab indexers exist."""

    @pytest.mark.asyncio
    async def test_torznab_only_returns_registry(self) -> None:
        """build_registry should succeed with empty configs_map but registered aggregate."""
        from pullbox.composition.providers import build_registry

        prowlarr_cfg = MagicMock()
        prowlarr_cfg.id = 1
        prowlarr_cfg.name = "1337x (Prowlarr)"
        prowlarr_cfg.source = "prowlarr"
        prowlarr_cfg.prowlarr_indexer_id = 55
        prowlarr_cfg.indexer_type = "torznab"
        prowlarr_cfg.url = "http://prowlarr:9696/55"
        prowlarr_cfg.api_key = "encrypted_key"
        prowlarr_cfg.enabled = True

        mock_session = AsyncMock()

        configs_result = MagicMock()
        configs_result.scalars.return_value.all.return_value = [prowlarr_cfg]
        url_cfg = MagicMock()
        url_cfg.value = "http://prowlarr:9696"
        key_cfg = MagicMock()
        key_cfg.value = "encrypted_api_key"
        url_result = MagicMock()
        url_result.scalar_one_or_none.return_value = url_cfg
        key_result = MagicMock()
        key_result.scalar_one_or_none.return_value = key_cfg

        dl_result = MagicMock()
        dl_result.scalars.return_value.all.return_value = []

        mock_session.execute = AsyncMock(
            side_effect=[configs_result, url_result, key_result, dl_result]
        )

        with patch("pullbox.composition.providers.decrypt_secret", return_value="decrypted_key"):
            result = await build_registry(mock_session)

        assert result is not None
        registry, configs_map = result
        assert configs_map == {}
        assert len(registry.get_indexer_items()) == 1
