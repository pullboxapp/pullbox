"""Authenticated REST contracts for story-arc placement policy and sync."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.issue import Issue
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.user import APIKey, User
from pullbox.services.auth_service import AuthService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest.fixture
async def db_factory(tmp_path: Path) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def api_key(db_factory: async_sessionmaker[AsyncSession]) -> str:
    raw_key = "pb_k1_" + "8" * 64
    async with db_factory() as session:
        user = User(
            username="storyarcplacementapi",
            password_hash=AuthService.hash_password("Test@1234"),
        )
        session.add(user)
        await session.flush()
        session.add(
            APIKey(
                user_id=user.id,
                key_hash=hashlib.sha256(raw_key.encode()).hexdigest(),
                name="story-arc-placement-api-test",
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

    async def override_db() -> AsyncGenerator[AsyncSession, None]:
        async with db_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db_dep] = override_db
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

    async def override_db() -> AsyncGenerator[AsyncSession, None]:
        async with db_factory() as session:
            yield session

    app.dependency_overrides[get_db_dep] = override_db
    reset_setup_cache()
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://testserver") as api_client:
        yield api_client
    app.dependency_overrides.clear()
    reset_setup_cache()


async def _seed(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> tuple[int, int, int, Path, Path]:
    canonical = tmp_path / "library" / "Batman.cbz"
    canonical.parent.mkdir(exist_ok=True)
    canonical.write_bytes(b"canonical")
    destination = tmp_path / "arcs"
    destination.mkdir(exist_ok=True)
    async with db_factory() as session:
        root = LibraryRoot(name="Comics", path=str(canonical.parent), enabled=True)
        series = Series(title="Batman", sort_title="batman", year_start=2011, library_root=root)
        issue = Issue(
            series=series,
            issue_number=1000000.0,
            issue_number_text="1000000",
            title="The Court of Owls",
        )
        library_file = LibraryFile(
            file_path=str(canonical),
            file_name=canonical.name,
            file_size=canonical.stat().st_size,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.HIGH,
            issue=issue,
            library_root=root,
        )
        arc = StoryArc(name="Court of Owls", source_kind=StoryArcSourceKind.PULLBOX)
        membership = IssueStoryArc(
            story_arc=arc,
            issue=issue,
            sequence_number=1,
            source_ordinal=1,
            resolution_state=StoryArcResolutionState.RESOLVED,
            source_kind=StoryArcSourceKind.PULLBOX,
            source_issue_number_text="1000000",
        )
        session.add_all([library_file, membership])
        await session.commit()
        return arc.id, membership.id, root.id, canonical, destination


def _policy_payload(root_id: int, destination: Path) -> dict[str, object]:
    return {
        "mode": "copy",
        "target_library_root_id": root_id,
        "destination_root": str(destination),
        "folder_template": "{StoryArc}",
        "file_template": ("{ReadingOrder:03d} - {Series} {IssueNumber}{IssueTitleOptional}"),
        "symlink_style": None,
        "synchronize": True,
    }


async def test_policy_routes_require_auth_preview_without_mutation_and_use_revision(
    client: AsyncClient,
    unauthenticated_client: AsyncClient,
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, _membership_id, root_id, _canonical, destination = await _seed(db_factory, tmp_path)
    unauthorized = await unauthenticated_client.get(f"/api/v1/story-arcs/{arc_id}/placement-policy")
    assert unauthorized.status_code == 401

    initial = await client.get(f"/api/v1/story-arcs/{arc_id}/placement-policy")
    assert initial.status_code == 200
    assert initial.json()["configured"] is False
    assert initial.json()["mode"] == "logical"

    preview = await client.post(
        f"/api/v1/story-arcs/{arc_id}/placement-policy/preview",
        params={"limit": 1, "offset": 0},
        json=_policy_payload(root_id, destination),
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["policy"]["revision"] == 1
    item = preview.json()["items"][0]
    assert item["issue_number_text"] == "1000000"
    assert "1000000" in item["target_path"]
    assert "e+" not in item["target_path"].casefold()
    assert item["classification"] == "will_materialize"
    assert item["placement_id"] is None
    assert item["current_ownership"] is None
    assert item["inspection_code"] is None

    unchanged = await client.get(f"/api/v1/story-arcs/{arc_id}/placement-policy")
    assert unchanged.json()["configured"] is False
    assert unchanged.json()["revision"] == 1

    updated = await client.put(
        f"/api/v1/story-arcs/{arc_id}/placement-policy",
        json={"expected_revision": 1, **_policy_payload(root_id, destination)},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["configured"] is True
    assert updated.json()["revision"] == 2
    assert updated.json()["snapshot"]["mode"] == "copy"

    stale = await client.put(
        f"/api/v1/story-arcs/{arc_id}/placement-policy",
        json={"expected_revision": 1, **_policy_payload(root_id, destination)},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "code": "revision_conflict",
        "category": "conflict",
        "message": "Story arc revision changed: expected 1, current 2",
    }


async def test_sync_list_retry_and_repair_have_categorized_state(
    client: AsyncClient,
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, membership_id, root_id, canonical, destination = await _seed(db_factory, tmp_path)
    update = await client.put(
        f"/api/v1/story-arcs/{arc_id}/placement-policy",
        json={"expected_revision": 1, **_policy_payload(root_id, destination)},
    )
    assert update.status_code == 200

    synced = await client.post(
        f"/api/v1/story-arcs/{arc_id}/memberships/{membership_id}/placement-sync",
        json={"adopt_identical_existing": False},
    )
    assert synced.status_code == 200, synced.text
    assert synced.json()["outcome"] == "created"
    placement = synced.json()["placement"]
    assert placement["state"] == "current"
    assert placement["ownership"] == "managed"
    target = Path(placement["placement_path"])
    assert target.read_bytes() == canonical.read_bytes()

    preview = await client.post(
        f"/api/v1/story-arcs/{arc_id}/placement-policy/preview",
        params={"limit": 1, "offset": 0},
        json=_policy_payload(root_id, destination),
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["items"][0]["classification"] == "managed_current"
    assert preview.json()["items"][0]["placement_id"] == placement["id"]
    assert preview.json()["items"][0]["current_ownership"] == "managed"
    assert preview.json()["items"][0]["inspection_code"] is None

    listed = await client.get(
        f"/api/v1/story-arcs/{arc_id}/placements",
        params={"limit": 1, "offset": 0},
    )
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["has_more"] is False
    assert listed.json()["items"][0]["target_fingerprint"]

    retried = await client.post(
        f"/api/v1/story-arcs/{arc_id}/placements/{placement['id']}/retry",
        json={"adopt_identical_existing": False},
    )
    assert retried.status_code == 200
    assert retried.json()["outcome"] == "idempotent"

    target.unlink()
    repaired = await client.post(f"/api/v1/story-arcs/{arc_id}/placements/{placement['id']}/repair")
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["outcome"] == "created"
    assert target.read_bytes() == canonical.read_bytes()


async def test_symlink_root_preview_returns_safe_category(
    client: AsyncClient,
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, _membership_id, root_id, _canonical, _destination = await _seed(db_factory, tmp_path)
    physical = tmp_path / "physical-arcs"
    physical.mkdir()
    linked = tmp_path / "linked-arcs"
    linked.symlink_to(physical, target_is_directory=True)
    payload = _policy_payload(root_id, linked)
    payload["mode"] = "symlink"
    payload["symlink_style"] = "relative"

    response = await client.post(
        f"/api/v1/story-arcs/{arc_id}/placement-policy/preview",
        json=payload,
    )
    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "symlink_root",
        "category": "safety",
        "message": "Story-arc destination root cannot be a symbolic link",
    }
