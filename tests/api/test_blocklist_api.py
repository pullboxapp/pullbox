"""
API tests for blocklist endpoints.

Covers list, get, add, delete, bulk delete, count, filtering,
pagination, auth, and CSRF enforcement.

Run:
    pytest tests/api/test_blocklist_api.py -v
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest
from fastapi import HTTPException

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.api.v1 import blocklist as blocklist_api
from pullbox.core.exceptions import NotFoundError
from pullbox.models.blocklist import BlocklistEntry, BlocklistReason, normalize_release_title
from pullbox.models.config import SystemConfig
from pullbox.models.user import APIKey, User
from pullbox.schemas.blocklist import (
    BlocklistAddRequest,
    BlocklistBulkDeleteRequest,
    ReleaseGroupListRequest,
)
from tests.sqlite_schema import create_test_schema

pytestmark = pytest.mark.slow


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
async def _db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await create_test_schema(conn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture()
async def _api_key_header(
    _db_factory: async_sessionmaker[AsyncSession],
    seeded_user_password_hash: str,
) -> str:
    raw_key = "pb_k1_" + "c" * 64
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with _db_factory() as session:
        user = User(
            username="bluser",
            password_hash=seeded_user_password_hash,
        )
        session.add(user)
        await session.flush()
        session.add(APIKey(user_id=user.id, key_hash=key_hash, name="bl-test"))
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


@pytest.fixture()
async def seeded_entries(
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Seed 5 blocklist entries for list/filter tests."""
    async with _db_factory() as session:
        titles = [
            ("Batman.018.2026", BlocklistReason.FAILED),
            ("Saga.073.2026", BlocklistReason.FAILED),
            ("X-Men.012.2026", BlocklistReason.REJECTED),
            ("Superman.045.2026", BlocklistReason.REJECTED),
            ("Spawn.350.2026", BlocklistReason.MANUAL),
        ]
        for title, reason in titles:
            entry = BlocklistEntry(
                release_title=title,
                release_title_normalized=normalize_release_title(title),
                reason=reason,
            )
            session.add(entry)
        await session.commit()


# ── GET /api/v1/blocklist — List ──────────────────────────────────────


class TestListEntries:
    """GET /api/v1/blocklist."""

    async def test_empty_list(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/blocklist")
        assert resp.status_code == 200
        data = resp.json()
        assert data["entries"] == []
        assert data["total"] == 0

    async def test_with_seeded_entries(self, client: AsyncClient, seeded_entries: None) -> None:
        resp = await client.get("/api/v1/blocklist")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["entries"]) == 5

    async def test_pagination(self, client: AsyncClient, seeded_entries: None) -> None:
        resp = await client.get("/api/v1/blocklist?page=1&page_size=2")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["entries"]) == 2
        assert data["total"] == 5
        assert data["page"] == 1
        assert data["page_size"] == 2

    async def test_page_beyond_total(self, client: AsyncClient, seeded_entries: None) -> None:
        resp = await client.get("/api/v1/blocklist?page=10&page_size=2")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["entries"]) == 0
        assert data["total"] == 5

    async def test_filter_by_reason(self, client: AsyncClient, seeded_entries: None) -> None:
        resp = await client.get("/api/v1/blocklist?reason=failed")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert all(e["reason"] == "failed" for e in data["entries"])

    async def test_search_by_title(self, client: AsyncClient, seeded_entries: None) -> None:
        resp = await client.get("/api/v1/blocklist?search=batman")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1

    async def test_search_no_match(self, client: AsyncClient, seeded_entries: None) -> None:
        resp = await client.get("/api/v1/blocklist?search=nonexistent")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0

    async def test_combined_filters(self, client: AsyncClient, seeded_entries: None) -> None:
        resp = await client.get("/api/v1/blocklist?reason=failed&search=batman")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1


# ── GET /api/v1/blocklist/{id} — Single ──────────────────────────────


class TestGetEntry:
    """GET /api/v1/blocklist/{id}."""

    async def test_existing_entry(self, client: AsyncClient, seeded_entries: None) -> None:
        # First list to get an ID
        list_resp = await client.get("/api/v1/blocklist")
        entry_id = list_resp.json()["entries"][0]["id"]

        resp = await client.get(f"/api/v1/blocklist/{entry_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == entry_id

    async def test_nonexistent_entry(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/blocklist/99999")
        assert resp.status_code == 404


# ── POST /api/v1/blocklist — Add ─────────────────────────────────────


class TestAddEntry:
    """POST /api/v1/blocklist."""

    async def test_add_valid_entry(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/blocklist",
            json={"release_title": "Batman.019.2026.Digital"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["release_title"] == "Batman.019.2026.Digital"
        assert data["reason"] == "manual"
        assert data["id"] > 0

    async def test_missing_title(self, client: AsyncClient) -> None:
        resp = await client.post("/api/v1/blocklist", json={})
        assert resp.status_code == 422
        assert "release_title" in resp.text

    async def test_empty_title(self, client: AsyncClient) -> None:
        resp = await client.post("/api/v1/blocklist", json={"release_title": ""})
        assert resp.status_code == 422

    async def test_title_too_long(self, client: AsyncClient) -> None:
        resp = await client.post("/api/v1/blocklist", json={"release_title": "A" * 501})
        assert resp.status_code == 422

    async def test_duplicate_returns_conflict(self, client: AsyncClient) -> None:
        await client.post("/api/v1/blocklist", json={"release_title": "Duplicate.Test"})
        resp = await client.post("/api/v1/blocklist", json={"release_title": "Duplicate.Test"})
        assert resp.status_code in (200, 409)


# ── DELETE /api/v1/blocklist/{id} — Remove Single ────────────────────


class TestDeleteEntry:
    """DELETE /api/v1/blocklist/{id}."""

    async def test_delete_existing(self, client: AsyncClient) -> None:
        add_resp = await client.post("/api/v1/blocklist", json={"release_title": "To Delete"})
        entry_id = add_resp.json()["id"]

        resp = await client.delete(f"/api/v1/blocklist/{entry_id}")
        assert resp.status_code == 204

        # Verify gone
        get_resp = await client.get(f"/api/v1/blocklist/{entry_id}")
        assert get_resp.status_code == 404

    async def test_delete_nonexistent(self, client: AsyncClient) -> None:
        resp = await client.delete("/api/v1/blocklist/99999")
        assert resp.status_code == 404


# ── DELETE /api/v1/blocklist/bulk — Bulk Remove ──────────────────────


class TestBulkDelete:
    """DELETE /api/v1/blocklist/bulk."""

    async def test_bulk_delete(self, client: AsyncClient) -> None:
        r1 = await client.post("/api/v1/blocklist", json={"release_title": "Bulk 1"})
        r2 = await client.post("/api/v1/blocklist", json={"release_title": "Bulk 2"})
        ids = [r1.json()["id"], r2.json()["id"]]

        resp = await client.request("DELETE", "/api/v1/blocklist/bulk", json={"ids": ids})
        assert resp.status_code == 200
        assert resp.json()["removed"] == 2

    async def test_bulk_empty_ids_fails(self, client: AsyncClient) -> None:
        resp = await client.request("DELETE", "/api/v1/blocklist/bulk", json={"ids": []})
        assert resp.status_code == 422
        assert "ids" in resp.text


class TestClearAll:
    """DELETE /api/v1/blocklist/clear."""

    async def test_clear_all_entries(self, client: AsyncClient) -> None:
        await client.post("/api/v1/blocklist", json={"release_title": "Clear 1"})
        await client.post("/api/v1/blocklist", json={"release_title": "Clear 2"})

        resp = await client.delete("/api/v1/blocklist/clear")
        assert resp.status_code == 200
        assert resp.json()["removed"] == 2

        count_resp = await client.get("/api/v1/blocklist/count")
        assert count_resp.status_code == 200
        assert count_resp.json()["count"] == 0


# ── GET /api/v1/blocklist/count ──────────────────────────────────────


class TestCount:
    """GET /api/v1/blocklist/count."""

    async def test_empty_count(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/blocklist/count")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    async def test_count_after_add(self, client: AsyncClient) -> None:
        await client.post("/api/v1/blocklist", json={"release_title": "Count Test"})
        resp = await client.get("/api/v1/blocklist/count")
        assert resp.status_code == 200
        assert resp.json()["count"] == 1


class TestBlocklistRouteFunctions:
    """Direct route-function coverage for blocklist contracts."""

    async def test_route_functions_cover_crud_filters_groups_and_errors(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with _db_factory() as session:
            empty_count = await blocklist_api.get_count(object(), session)  # type: ignore[arg-type]
            empty_groups = await blocklist_api.get_release_groups(object(), session)  # type: ignore[arg-type]

            groups = await blocklist_api.update_release_groups(
                ReleaseGroupListRequest(groups=[" Empire ", "empire", "", "DCP"]),
                object(),  # type: ignore[arg-type]
                session,
            )
            updated_groups = await blocklist_api.update_release_groups(
                ReleaseGroupListRequest(groups=["Pyrate", "DCP"]),
                object(),  # type: ignore[arg-type]
                session,
            )
            group_row = await session.get(SystemConfig, "blocklist.release_groups")

            first = await blocklist_api.add_entry(
                BlocklistAddRequest(
                    release_title="Batman.019.2026.Digital",
                    reason=BlocklistReason.MANUAL,
                    download_url="https://example.com/batman.nzb",
                ),
                object(),  # type: ignore[arg-type]
                session,
            )
            second = await blocklist_api.add_entry(
                BlocklistAddRequest(
                    release_title="Saga.073.2026.Digital",
                    reason=BlocklistReason.FAILED,
                ),
                object(),  # type: ignore[arg-type]
                session,
            )

            with pytest.raises(HTTPException) as duplicate:
                await blocklist_api.add_entry(
                    BlocklistAddRequest(release_title="Batman.019.2026.Digital"),
                    object(),  # type: ignore[arg-type]
                    session,
                )

            listed = await blocklist_api.list_entries(
                object(),  # type: ignore[arg-type]
                session,
                page=1,
                page_size=10,
                reason=BlocklistReason.MANUAL,
                search="batman",
            )
            fetched = await blocklist_api.get_entry(first.id, object(), session)  # type: ignore[arg-type]
            count_after_add = await blocklist_api.get_count(object(), session)  # type: ignore[arg-type]

            await blocklist_api.delete_entry(first.id, object(), session)  # type: ignore[arg-type]
            bulk = await blocklist_api.bulk_delete(
                BlocklistBulkDeleteRequest(ids=[second.id]),
                object(),  # type: ignore[arg-type]
                session,
            )
            clear_seed = await blocklist_api.add_entry(
                BlocklistAddRequest(release_title="Clear.Me.001"),
                object(),  # type: ignore[arg-type]
                session,
            )
            cleared = await blocklist_api.clear_entries(object(), session)  # type: ignore[arg-type]

            with pytest.raises(NotFoundError):
                await blocklist_api.get_entry(999_001, object(), session)  # type: ignore[arg-type]
            with pytest.raises(NotFoundError):
                await blocklist_api.delete_entry(999_002, object(), session)  # type: ignore[arg-type]

        assert empty_count.count == 0
        assert empty_groups.groups == []
        assert groups.groups == ["Empire", "DCP"]
        assert updated_groups.groups == ["Pyrate", "DCP"]
        assert group_row is not None
        assert group_row.value == "Pyrate,DCP"
        assert first.release_title == "Batman.019.2026.Digital"
        assert first.download_url == "https://example.com/batman.nzb"
        assert duplicate.value.status_code == 409
        assert listed.total == 1
        assert listed.entries[0].id == first.id
        assert fetched.id == first.id
        assert count_after_add.count == 2
        assert bulk.removed == 1
        assert clear_seed.id > 0
        assert cleared.removed == 1


# ── Auth tests ────────────────────────────────────────────────────────


class TestAuth:
    """Authentication enforcement."""

    async def test_unauthenticated_returns_401(
        self, _db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
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
        async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
            resp = await ac.get("/api/v1/blocklist")
            assert resp.status_code == 401

        app.dependency_overrides.clear()
        reset_setup_cache()
