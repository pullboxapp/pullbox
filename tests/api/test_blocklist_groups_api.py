"""
API tests for release group blocklist endpoints.

Covers GET/PUT for /api/v1/blocklist/groups, persistence,
deduplication, validation, and auth.

Run:
    pytest tests/api/test_blocklist_groups_api.py -v
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.user import APIKey, User

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

pytestmark = pytest.mark.slow


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
async def _db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture()
async def _api_key_header(
    _db_factory: async_sessionmaker[AsyncSession], seeded_user_password_hash: str
) -> str:
    raw_key = "pb_k1_" + "d" * 64
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with _db_factory() as session:
        user = User(
            username="grpuser",
            password_hash=seeded_user_password_hash,
        )
        session.add(user)
        await session.flush()
        session.add(APIKey(user_id=user.id, key_hash=key_hash, name="grp-test"))
        await session.commit()
    return raw_key


@pytest.fixture()
async def client(
    _db_factory: async_sessionmaker[AsyncSession],
    _api_key_header: str,
) -> AsyncGenerator[AsyncClient, None]:
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


# ── GET /api/v1/blocklist/groups ──────────────────────────────────────


class TestGetGroups:
    """GET /api/v1/blocklist/groups."""

    async def test_empty_groups(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/blocklist/groups")
        assert resp.status_code == 200
        data = resp.json()
        assert data["groups"] == []

    async def test_after_put_returns_updated(self, client: AsyncClient) -> None:
        await client.put(
            "/api/v1/blocklist/groups",
            json={"groups": ["Empire", "DCP"]},
        )
        resp = await client.get("/api/v1/blocklist/groups")
        assert resp.status_code == 200
        data = resp.json()
        assert "Empire" in data["groups"]
        assert "DCP" in data["groups"]

    async def test_groups_returned_in_order(self, client: AsyncClient) -> None:
        await client.put(
            "/api/v1/blocklist/groups",
            json={"groups": ["Zebra", "Alpha", "Middle"]},
        )
        resp = await client.get("/api/v1/blocklist/groups")
        data = resp.json()
        assert len(data["groups"]) == 3


# ── PUT /api/v1/blocklist/groups ──────────────────────────────────────


class TestUpdateGroups:
    """PUT /api/v1/blocklist/groups."""

    async def test_set_groups(self, client: AsyncClient) -> None:
        resp = await client.put(
            "/api/v1/blocklist/groups",
            json={"groups": ["Empire", "DCP"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "Empire" in data["groups"]
        assert "DCP" in data["groups"]

    async def test_deduplicates_case_insensitive(self, client: AsyncClient) -> None:
        resp = await client.put(
            "/api/v1/blocklist/groups",
            json={"groups": ["Empire", "empire", "EMPIRE"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["groups"]) == 1

    async def test_trims_whitespace(self, client: AsyncClient) -> None:
        resp = await client.put(
            "/api/v1/blocklist/groups",
            json={"groups": [" Empire ", " DCP "]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert all(g == g.strip() for g in data["groups"])

    async def test_empty_list_clears(self, client: AsyncClient) -> None:
        await client.put(
            "/api/v1/blocklist/groups",
            json={"groups": ["Empire"]},
        )
        resp = await client.put(
            "/api/v1/blocklist/groups",
            json={"groups": []},
        )
        assert resp.status_code == 200
        assert resp.json()["groups"] == []

    async def test_filters_empty_strings(self, client: AsyncClient) -> None:
        resp = await client.put(
            "/api/v1/blocklist/groups",
            json={"groups": ["Empire", "", "DCP", ""]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "" not in data["groups"]
        assert len(data["groups"]) == 2

    async def test_single_group(self, client: AsyncClient) -> None:
        resp = await client.put(
            "/api/v1/blocklist/groups",
            json={"groups": ["Empire"]},
        )
        assert resp.status_code == 200
        assert len(resp.json()["groups"]) == 1

    async def test_many_groups(self, client: AsyncClient) -> None:
        groups = [f"Group{i}" for i in range(20)]
        resp = await client.put(
            "/api/v1/blocklist/groups",
            json={"groups": groups},
        )
        assert resp.status_code == 200
        assert len(resp.json()["groups"]) == 20

    async def test_missing_groups_field_fails(self, client: AsyncClient) -> None:
        resp = await client.put("/api/v1/blocklist/groups", json={})
        assert resp.status_code == 422
        assert "groups" in resp.text

    async def test_persistence(self, client: AsyncClient) -> None:
        """After PUT, subsequent GET returns the updated list."""
        await client.put(
            "/api/v1/blocklist/groups",
            json={"groups": ["Persistent"]},
        )
        resp = await client.get("/api/v1/blocklist/groups")
        assert "Persistent" in resp.json()["groups"]
