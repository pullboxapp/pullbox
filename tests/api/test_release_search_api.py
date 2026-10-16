"""Tests for the raw release-search endpoint."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.user import APIKey, User
from pullbox.providers.base import ProviderRegistry, ReleaseResult

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def _make_release(title: str) -> ReleaseResult:
    return ReleaseResult(
        title=title,
        indexer_name="NZBgeek",
        download_url=f"https://example.com/{title.replace(' ', '_')}",
        size_bytes=100_000_000,
        age_days=4,
        seeders=None,
        leechers=None,
        grabs=50,
        is_torrent=False,
        category="7030",
        published_at=None,
    )


@pytest.fixture
async def _db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def client(
    _db_factory: async_sessionmaker[AsyncSession],
    seeded_user_password_hash: str,
) -> AsyncGenerator[AsyncClient, None]:
    from pullbox.api.deps import get_db_dep
    from pullbox.api.middleware import reset_setup_cache
    from pullbox.app import create_app

    raw_key = "pb_k1_" + "r" * 64
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with _db_factory() as session:
        user = User(
            username="releasesearchuser",
            password_hash=seeded_user_password_hash,
        )
        session.add(user)
        await session.flush()
        session.add(APIKey(user_id=user.id, key_hash=key_hash, name="release-search-test"))
        await session.commit()

    app = create_app()

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
        async with _db_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db_dep] = _override_db
    reset_setup_cache()

    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"X-Api-Key": raw_key},
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
    reset_setup_cache()


@pytest.mark.asyncio
async def test_release_search_returns_empty_when_no_runtime(client: AsyncClient) -> None:
    with patch(
        "pullbox.services.search_service.build_search_runtime",
        new_callable=AsyncMock,
        return_value=None,
    ):
        resp = await client.get("/api/v1/search/releases", params={"series": "Absolute Superman"})

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_release_search_uses_shared_runtime_and_search_service(
    client: AsyncClient,
) -> None:
    runtime = SimpleNamespace(
        registry=ProviderRegistry(),
        indexer_configs={9: MagicMock()},
        failure_threshold=7,
    )
    results = [_make_release("Absolute Superman 009"), _make_release("Absolute Superman 010")]

    with (
        patch(
            "pullbox.services.search_service.build_search_runtime",
            new_callable=AsyncMock,
            return_value=runtime,
        ) as build_runtime,
        patch(
            "pullbox.services.search_service.SearchService.search",
            new_callable=AsyncMock,
            return_value=results,
        ) as search_mock,
    ):
        resp = await client.get(
            "/api/v1/search/releases",
            params={"series": "Absolute Superman", "issue": 9, "year": 2025},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert [item["title"] for item in data] == ["Absolute Superman 009", "Absolute Superman 010"]
    assert build_runtime.await_args.kwargs["include_download_clients"] is False
    assert search_mock.await_args.kwargs["indexer_configs"] == runtime.indexer_configs
