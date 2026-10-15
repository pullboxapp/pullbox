"""Tests for the global search-on-add policy in the series add flow.

Verifies that:
- the global SystemConfig default is passed to add_from_comicvine
- omitted values resolve to the SystemConfig default
- conflicting deprecated request overrides are rejected

Run:
    pytest tests/api/test_series_search_on_add.py -v
"""

from __future__ import annotations

import hashlib
import os
import sys
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.config import SystemConfig
from pullbox.models.series import SeriesStatus, SeriesType
from pullbox.models.user import APIKey, User
from pullbox.schemas.series import SeriesResponse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-search-on-add")

SERIES_URL = "/api/v1/series"

_RESPONSE_TEMPLATE = {
    "id": 1,
    "comicvine_id": 12345,
    "title": "Test Series",
    "sort_title": "Test Series",
    "year_start": 2024,
    "status": "continuing",
    "monitored": True,
    "created_at": "2024-01-01T00:00:00",
    "updated_at": "2024-01-01T00:00:00",
}


def _make_mock_series(search_on_add: bool = False) -> MagicMock:
    """Build a mock Series object returned by add_from_comicvine."""
    series = MagicMock()
    series.id = 1
    series.comicvine_id = 12345
    series.title = "Test Series"
    series.sort_title = "Test Series"
    series.year_start = 2024
    series.year_end = None
    series.status = SeriesStatus.CONTINUING
    series.series_type = SeriesType.STANDARD
    series.parent_series_id = None
    series.description = "A test series"
    series.cover_path = None
    series.issue_count = 10
    series.comicvine_url = "https://comicvine.gamespot.com/test/1234/"
    series.monitored = search_on_add  # search_on_add sets monitored
    series.metadata_last_refreshed = None
    series.metadata_source = "comicvine"
    series.publisher_id = None
    series.path = "/comics/Test Series (2024)"
    series.library_root_id = None
    series.alternate_names = []
    series.publisher = None
    series.issues = []
    return series


@pytest.fixture
async def _db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def _api_key_header(
    _db_factory: async_sessionmaker[AsyncSession],
    seeded_user_password_hash: str,
) -> str:
    """Create a test user + API key, return the raw key string."""
    raw_key = "pb_k1_" + "b" * 64
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with _db_factory() as session:
        user = User(
            username="testuser",
            password_hash=seeded_user_password_hash,
        )
        session.add(user)
        await session.flush()
        session.add(APIKey(user_id=user.id, key_hash=key_hash, name="test"))
        await session.commit()
    return raw_key


@pytest.fixture
async def client(
    _db_factory: async_sessionmaker[AsyncSession],
    _api_key_header: str,
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client authenticated via API key (bypasses CSRF)."""
    from pullbox.api.deps import get_db_dep
    from pullbox.api.middleware import reset_setup_cache
    from pullbox.app import create_app

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
        headers={"X-Api-Key": _api_key_header},
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
    reset_setup_cache()


def _patch_add_series(mock_series: MagicMock) -> tuple[object, object, object]:
    """Return (meta_patch, add_patch, load_patch) context managers."""
    meta_patch = patch(
        "pullbox.api.v1.series._build_metadata_service",
        new_callable=AsyncMock,
    )
    add_patch = patch.object(
        __import__("pullbox.services.series_service", fromlist=["SeriesService"]).SeriesService,
        "add_from_comicvine",
        new_callable=AsyncMock,
        return_value=mock_series,
    )
    load_patch = patch(
        "pullbox.api.v1.series._load_series_response",
        new_callable=AsyncMock,
        return_value=SeriesResponse.model_validate(
            {**_RESPONSE_TEMPLATE, "monitored": mock_series.monitored}
        ),
    )
    return meta_patch, add_patch, load_patch


# ── Tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_series_uses_global_search_on_add_default_when_enabled(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Series creation uses the global search-on-add default when it is enabled."""
    async with _db_factory() as session:
        session.add(SystemConfig(key="search_on_add_default", value="true"))
        await session.commit()

    mock_series = _make_mock_series(search_on_add=True)
    meta_p, add_p, load_p = _patch_add_series(mock_series)

    with meta_p, add_p as mock_add, load_p:
        resp = await client.post(
            SERIES_URL,
            json={"comicvine_id": 12345, "monitored": True},
        )

    assert resp.status_code == 201
    assert mock_add.called
    assert mock_add.call_args.kwargs["search_on_add"] is True


@pytest.mark.asyncio
async def test_create_series_uses_global_search_on_add_default_when_disabled(
    client: AsyncClient,
) -> None:
    """Series creation defaults search-on-add to False when the global config is unset."""
    mock_series = _make_mock_series(search_on_add=False)
    meta_p, add_p, load_p = _patch_add_series(mock_series)

    with meta_p, add_p as mock_add, load_p:
        resp = await client.post(
            SERIES_URL,
            json={"comicvine_id": 12345, "monitored": True},
        )

    assert resp.status_code == 201
    assert mock_add.called
    assert mock_add.call_args.kwargs["search_on_add"] is False


@pytest.mark.asyncio
async def test_create_series_rejects_conflicting_deprecated_override(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Deprecated request overrides that conflict with global policy are rejected."""
    async with _db_factory() as session:
        session.add(SystemConfig(key="search_on_add_default", value="true"))
        await session.commit()

    mock_series = _make_mock_series(search_on_add=False)
    meta_p, add_p, load_p = _patch_add_series(mock_series)

    with meta_p, add_p as mock_add, load_p:
        resp = await client.post(
            SERIES_URL,
            json={"comicvine_id": 12345, "monitored": True, "search_on_add": False},
        )

    assert resp.status_code == 422
    assert not mock_add.called
    assert "Search on add is now controlled by the global import policy" in resp.text


@pytest.mark.asyncio
async def test_create_series_accepts_matching_deprecated_override(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Deprecated request overrides are tolerated when they already match global policy."""
    async with _db_factory() as session:
        session.add(SystemConfig(key="search_on_add_default", value="false"))
        await session.commit()

    mock_series = _make_mock_series(search_on_add=False)
    meta_p, add_p, load_p = _patch_add_series(mock_series)

    with meta_p, add_p as mock_add, load_p:
        resp = await client.post(
            SERIES_URL,
            json={"comicvine_id": 12345, "monitored": True, "search_on_add": False},
        )

    assert resp.status_code == 201
    assert mock_add.called
    assert mock_add.call_args.kwargs["search_on_add"] is False
