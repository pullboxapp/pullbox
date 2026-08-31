"""REST API contracts for first-class logical story arcs."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.api.v1 import story_arcs as story_arcs_api
from pullbox.models import Base
from pullbox.models.issue import Issue
from pullbox.models.series import Series
from pullbox.models.story_arc import IssueStoryArc, StoryArc, StoryArcLifecycle
from pullbox.models.user import APIKey, User
from pullbox.services.auth_service import AuthService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest.fixture(autouse=True)
def _enable_manual_story_arc_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        story_arcs_api,
        "get_settings",
        lambda: SimpleNamespace(story_arc_manual_create_enabled=True),
        raising=False,
    )


@pytest.fixture
async def db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def api_key(db_factory: async_sessionmaker[AsyncSession]) -> str:
    raw_key = "pb_k1_" + "7" * 64
    async with db_factory() as session:
        user = User(
            username="storyarcapi",
            password_hash=AuthService.hash_password("Test@1234"),
        )
        session.add(user)
        await session.flush()
        session.add(
            APIKey(
                user_id=user.id,
                key_hash=hashlib.sha256(raw_key.encode()).hexdigest(),
                name="story-arc-api-test",
            )
        )
        await session.commit()
    return raw_key


@pytest.fixture
async def client(
    db_factory: async_sessionmaker[AsyncSession],
    api_key: str,
) -> AsyncGenerator[AsyncClient, None]:
    from pullbox.api.deps import get_db_dep
    from pullbox.api.middleware import reset_setup_cache
    from pullbox.app import create_app

    app = create_app()

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
        async with db_factory() as session:
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
        headers={"X-Api-Key": api_key},
    ) as api_client:
        yield api_client
    app.dependency_overrides.clear()
    reset_setup_cache()


@pytest.fixture
async def unauthenticated_client(
    db_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient, None]:
    from pullbox.api.deps import get_db_dep
    from pullbox.api.middleware import reset_setup_cache
    from pullbox.app import create_app

    app = create_app()

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
        async with db_factory() as session:
            yield session

    app.dependency_overrides[get_db_dep] = _override_db
    reset_setup_cache()
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://testserver") as api_client:
        yield api_client
    app.dependency_overrides.clear()
    reset_setup_cache()


async def _create_arc(client: AsyncClient, name: str = "Absolute Power") -> dict[str, object]:
    response = await client.post(
        "/api/v1/story-arcs",
        json={
            "name": name,
            "description": "A first-class logical arc",
            "monitored": True,
            "search_missing": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _seed_issue(
    db_factory: async_sessionmaker[AsyncSession],
    *,
    title: str,
    issue_number: float,
    issue_number_text: str,
) -> int:
    async with db_factory() as session:
        series = Series(title=title, sort_title=title.casefold())
        issue = Issue(
            series=series,
            issue_number=issue_number,
            issue_number_text=issue_number_text,
        )
        session.add(issue)
        await session.flush()
        issue_id = issue.id
        await session.commit()
    return issue_id


async def test_story_arc_routes_require_auth_and_keep_existing_routes(
    client: AsyncClient,
    unauthenticated_client: AsyncClient,
) -> None:
    unauthorized = await unauthenticated_client.get("/api/v1/story-arcs")
    assert unauthorized.status_code == 401

    existing = await client.get("/api/v1/series", params={"limit": 1})
    assert existing.status_code == 200
    assert existing.json()["items"] == []


async def test_manual_create_is_unreachable_when_feature_flag_is_off(
    client: AsyncClient,
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        story_arcs_api,
        "get_settings",
        lambda: SimpleNamespace(story_arc_manual_create_enabled=False),
        raising=False,
    )

    response = await client.post(
        "/api/v1/story-arcs",
        json={"name": "Hidden API arc"},
    )

    assert response.status_code == 404
    async with db_factory() as session:
        assert await session.scalar(select(func.count(StoryArc.id))) == 0


async def test_create_read_patch_and_safe_archive_use_optimistic_revision(
    client: AsyncClient,
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    created = await _create_arc(client, "  Absolute   Power ")
    arc_id = created["id"]
    assert created["cover_path"] is None
    assert created["cover_url"] is None
    assert created == {
        **created,
        "name": "Absolute Power",
        "normalized_name": "absolute power",
        "lifecycle": "active",
        "revision": 1,
        "membership_count": 0,
        "resolved_count": 0,
        "missing_count": 0,
        "conflict_count": 0,
    }

    read = await client.get(f"/api/v1/story-arcs/{arc_id}")
    assert read.status_code == 200
    assert read.json()["id"] == arc_id

    patched = await client.patch(
        f"/api/v1/story-arcs/{arc_id}",
        json={
            "expected_revision": 1,
            "description": None,
            "include_upcoming": True,
        },
    )
    assert patched.status_code == 200
    assert patched.json()["description"] is None
    assert patched.json()["include_upcoming"] is True
    assert patched.json()["revision"] == 2

    null_name = await client.patch(
        f"/api/v1/story-arcs/{arc_id}",
        json={"expected_revision": 2, "name": None},
    )
    assert null_name.status_code == 422

    stale = await client.patch(
        f"/api/v1/story-arcs/{arc_id}",
        json={"expected_revision": 1, "monitored": False},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == "Story arc revision changed: expected 1, current 2"

    missing_revision = await client.delete(f"/api/v1/story-arcs/{arc_id}")
    assert missing_revision.status_code == 422

    issue_id = await _seed_issue(
        db_factory,
        title="Absolute Power Issue",
        issue_number=1,
        issue_number_text="1",
    )
    membership = await client.post(
        f"/api/v1/story-arcs/{arc_id}/memberships",
        json={
            "issue_id": issue_id,
            "sequence_number": 1,
            "source_issue_number_text": "1",
        },
    )
    assert membership.status_code == 201
    membership_id = membership.json()["id"]

    archived = await client.delete(f"/api/v1/story-arcs/{arc_id}", params={"expected_revision": 3})
    assert archived.status_code == 200
    assert archived.json()["lifecycle"] == "archived"
    assert archived.json()["monitored"] is False
    assert archived.json()["search_missing"] is False
    assert archived.json()["include_upcoming"] is False
    assert archived.json()["revision"] == 4

    async with db_factory() as session:
        persisted = await session.get(StoryArc, arc_id)
        assert persisted is not None
        assert persisted.lifecycle == StoryArcLifecycle.ARCHIVED
        assert await session.get(IssueStoryArc, membership_id) is not None
        assert await session.get(Issue, issue_id) is not None


async def test_list_is_bounded_searchable_filterable_and_reports_state_counts(
    client: AsyncClient,
) -> None:
    first = await _create_arc(client, "Absolute Power")
    second = await _create_arc(client, "House of Brainiac")
    third = await _create_arc(client, "Absolute Universe")
    await client.delete(f"/api/v1/story-arcs/{third['id']}", params={"expected_revision": 1})

    await client.post(
        f"/api/v1/story-arcs/{first['id']}/memberships",
        json={
            "issue_id": None,
            "sequence_number": 1,
            "source_issue_number_text": "1AU",
        },
    )
    skipped = await client.post(
        f"/api/v1/story-arcs/{first['id']}/memberships",
        json={
            "issue_id": None,
            "sequence_number": 2,
            "source_issue_number_text": "2",
        },
    )
    await client.patch(
        f"/api/v1/story-arcs/{first['id']}/memberships/{skipped.json()['id']}",
        json={"intentionally_skipped": True},
    )

    page = await client.get(
        "/api/v1/story-arcs",
        params={"q": "absolute", "lifecycle": "active", "limit": 1, "offset": 0},
    )
    assert page.status_code == 200
    assert page.json()["total"] == 1
    assert page.json()["has_more"] is False
    assert page.json()["items"][0]["id"] == first["id"]
    assert page.json()["items"][0]["membership_count"] == 2
    assert page.json()["items"][0]["missing_count"] == 1
    assert page.json()["items"][0]["resolved_count"] == 0
    assert page.json()["items"][0]["conflict_count"] == 0

    monitored = await client.get("/api/v1/story-arcs", params={"monitored": True})
    assert monitored.status_code == 200
    assert {item["id"] for item in monitored.json()["items"]} == {
        first["id"],
        second["id"],
    }

    oversized = await client.get("/api/v1/story-arcs", params={"limit": 201})
    assert oversized.status_code == 422


async def test_memberships_round_trip_exact_numbers_and_paginate_in_reading_order(
    client: AsyncClient,
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    arc = await _create_arc(client, "DC Numbering")
    million_issue_id = await _seed_issue(
        db_factory,
        title="DC One Million",
        issue_number=1_000_000,
        issue_number_text="1000000",
    )
    invalid = await client.post(
        f"/api/v1/story-arcs/{arc['id']}/memberships",
        json={"issue_id": None, "sequence_number": 1},
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"] == (
        "An unresolved story-arc entry requires an exact source issue number"
    )
    payloads = [
        {
            "issue_id": million_issue_id,
            "sequence_number": 30,
            "source_ordinal": 3,
            "source_issue_number_text": "1000000",
        },
        {
            "issue_id": None,
            "sequence_number": 10,
            "source_ordinal": 2,
            "source_issue_number_text": "1AU",
        },
        {
            "issue_id": None,
            "sequence_number": 10,
            "source_ordinal": 1,
            "source_issue_number_text": "0.5",
        },
    ]
    for payload in payloads:
        response = await client.post(f"/api/v1/story-arcs/{arc['id']}/memberships", json=payload)
        assert response.status_code == 201, response.text

    first_page = await client.get(
        f"/api/v1/story-arcs/{arc['id']}/memberships",
        params={"limit": 2, "offset": 0},
    )
    assert first_page.status_code == 200
    assert first_page.json()["total"] == 3
    assert first_page.json()["has_more"] is True
    assert [item["source_issue_number_text"] for item in first_page.json()["items"]] == [
        "0.5",
        "1AU",
    ]

    second_page = await client.get(
        f"/api/v1/story-arcs/{arc['id']}/memberships",
        params={"limit": 2, "offset": 2},
    )
    assert [item["source_issue_number_text"] for item in second_page.json()["items"]] == ["1000000"]

    detail = await client.get(f"/api/v1/story-arcs/{arc['id']}")
    assert detail.json()["membership_count"] == 3
    assert detail.json()["resolved_count"] == 1
    assert detail.json()["missing_count"] == 2


async def test_patch_resolve_reorder_and_remove_membership_preserve_issue(
    client: AsyncClient,
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    arc = await _create_arc(client, "Mutable Arc")
    issue_id = await _seed_issue(
        db_factory,
        title="Batman Annual",
        issue_number=1,
        issue_number_text="1AU",
    )
    first = await client.post(
        f"/api/v1/story-arcs/{arc['id']}/memberships",
        json={
            "issue_id": None,
            "sequence_number": 1,
            "source_issue_number_text": "2",
        },
    )
    second = await client.post(
        f"/api/v1/story-arcs/{arc['id']}/memberships",
        json={
            "issue_id": None,
            "sequence_number": 2,
            "source_issue_number_text": "0.5",
        },
    )
    first_id = first.json()["id"]
    second_id = second.json()["id"]

    patched = await client.patch(
        f"/api/v1/story-arcs/{arc['id']}/memberships/{first_id}",
        json={
            "sequence_number": 7,
            "source_ordinal": 4,
            "source_issue_number_text": "1AU",
            "intentionally_skipped": True,
        },
    )
    assert patched.status_code == 200
    assert patched.json()["source_issue_number_text"] == "1AU"
    assert patched.json()["resolution_state"] == "skipped"

    resolved = await client.post(
        f"/api/v1/story-arcs/{arc['id']}/memberships/{first_id}/resolve",
        json={"issue_id": issue_id},
    )
    assert resolved.status_code == 200
    assert resolved.json()["issue_id"] == issue_id
    assert resolved.json()["resolution_state"] == "resolved"

    duplicate = await client.post(
        f"/api/v1/story-arcs/{arc['id']}/memberships/{second_id}/resolve",
        json={"issue_id": issue_id},
    )
    assert duplicate.status_code == 409

    detail = await client.get(f"/api/v1/story-arcs/{arc['id']}")
    current_revision = detail.json()["revision"]
    stale = await client.put(
        f"/api/v1/story-arcs/{arc['id']}/memberships/reorder",
        json={
            "expected_revision": current_revision - 1,
            "membership_ids": [second_id, first_id],
        },
    )
    assert stale.status_code == 409

    reordered = await client.put(
        f"/api/v1/story-arcs/{arc['id']}/memberships/reorder",
        json={
            "expected_revision": current_revision,
            "membership_ids": [second_id, first_id],
        },
    )
    assert reordered.status_code == 200
    assert [item["id"] for item in reordered.json()["items"]] == [second_id, first_id]
    assert [item["sequence_number"] for item in reordered.json()["items"]] == [1, 2]

    removed = await client.delete(f"/api/v1/story-arcs/{arc['id']}/memberships/{first_id}")
    assert removed.status_code == 204

    async with db_factory() as session:
        assert await session.get(Issue, issue_id) is not None
        assert await session.get(IssueStoryArc, first_id) is None
        assert await session.scalar(select(func.count(Issue.id))) == 1


async def test_nested_membership_routes_do_not_cross_arc_boundaries(client: AsyncClient) -> None:
    first_arc = await _create_arc(client, "First Arc")
    second_arc = await _create_arc(client, "Second Arc")
    membership = await client.post(
        f"/api/v1/story-arcs/{first_arc['id']}/memberships",
        json={
            "issue_id": None,
            "sequence_number": 1,
            "source_issue_number_text": "1",
        },
    )
    membership_id = membership.json()["id"]

    wrong_arc = await client.patch(
        f"/api/v1/story-arcs/{second_arc['id']}/memberships/{membership_id}",
        json={"sequence_number": 9},
    )
    assert wrong_arc.status_code == 404

    unchanged = await client.get(f"/api/v1/story-arcs/{first_arc['id']}/memberships")
    assert unchanged.json()["items"][0]["sequence_number"] == 1
