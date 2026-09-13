"""Comic Vine UI provider lifecycle contracts."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pullbox.ui import comicvine_provider


async def test_open_provider_releases_database_transaction_wraps_cache_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(rollback=AsyncMock())
    session_factory = object()
    provider = SimpleNamespace(close=AsyncMock())
    cached_provider = object()
    provider_factory = Mock(return_value=provider)
    cache_factory = Mock(return_value=cached_provider)
    monkeypatch.setattr(
        "pullbox.core.comicvine_key.get_comicvine_api_key",
        AsyncMock(return_value="configured-key"),
    )
    monkeypatch.setattr(
        "pullbox.providers.metadata.comicvine.ComicVineProvider",
        provider_factory,
    )
    monkeypatch.setattr(
        comicvine_provider,
        "PersistentComicVineCacheProvider",
        cache_factory,
    )

    async with comicvine_provider.open_comicvine_ui_provider(
        session,  # type: ignore[arg-type]
        session_factory=session_factory,  # type: ignore[arg-type]
    ) as opened:
        assert opened is cached_provider

    session.rollback.assert_awaited_once_with()
    provider_factory.assert_called_once_with(api_key="configured-key")
    cache_factory.assert_called_once_with(provider, session_factory)
    provider.close.assert_awaited_once_with()


async def test_open_provider_rejects_missing_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(rollback=AsyncMock())
    provider_factory = Mock()
    monkeypatch.setattr(
        "pullbox.core.comicvine_key.get_comicvine_api_key",
        AsyncMock(return_value=""),
    )
    monkeypatch.setattr(
        "pullbox.providers.metadata.comicvine.ComicVineProvider",
        provider_factory,
    )

    with pytest.raises(comicvine_provider.ComicVineNotConfiguredError):
        async with comicvine_provider.open_comicvine_ui_provider(  # type: ignore[arg-type]
            session
        ):
            pass

    session.rollback.assert_awaited_once_with()
    provider_factory.assert_not_called()
