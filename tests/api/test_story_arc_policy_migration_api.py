"""Authenticated API boundary for Story Arc placement-policy migration."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
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
from pullbox.services.story_arc_placement_integration import (
    StoryArcPlacementPolicyInput,
    StoryArcPlacementPolicyMode,
    StoryArcPlacementSyncService,
)
from pullbox.services.story_arc_policy_migration import (
    STORY_ARC_POLICY_MIGRATION_CONFIRMATION,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


@pytest.fixture
async def migration_api_db(
    tmp_path: Path,
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'migration-api.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def migration_api_key(
    migration_api_db: async_sessionmaker[AsyncSession],
) -> str:
    raw_key = "pb_k1_" + "6" * 64
    async with migration_api_db() as session:
        user = User(
            username="storyarcpolicymigration",
            password_hash=AuthService.hash_password("Test@1234"),
        )
        session.add(user)
        await session.flush()
        session.add(
            APIKey(
                user_id=user.id,
                key_hash=hashlib.sha256(raw_key.encode()).hexdigest(),
                name="policy-migration-api-test",
            )
        )
        await session.commit()
    return raw_key


@pytest.fixture
async def migration_client(
    migration_api_db: async_sessionmaker[AsyncSession],
    migration_api_key: str,
) -> AsyncGenerator[AsyncClient, None]:
    from pullbox.api.deps import get_db_dep
    from pullbox.api.middleware import reset_setup_cache
    from pullbox.app import create_app

    app = create_app()

    async def override_db() -> AsyncGenerator[AsyncSession, None]:
        async with migration_api_db() as session:
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
        headers={"X-Api-Key": migration_api_key},
    ) as client:
        yield client
    app.dependency_overrides.clear()
    reset_setup_cache()


async def _seed_api_scope(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> tuple[int, int, Path, Path]:
    library = tmp_path / "api-library"
    old_root = library / "OldStoryArcs"
    new_root = library / "NewStoryArcs"
    library.mkdir()
    old_root.mkdir()
    new_root.mkdir()
    canonical = library / "Issue.cbz"
    canonical.write_bytes(b"canonical")
    async with factory() as session:
        root = LibraryRoot(name="Comics", path=str(library), enabled=True)
        series = Series(title="API Series", sort_title="api series", library_root=root)
        issue = Issue(series=series, issue_number=1.0, issue_number_text="1")
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
        arc = StoryArc(name="API Arc", source_kind=StoryArcSourceKind.PULLBOX)
        membership = IssueStoryArc(
            story_arc=arc,
            issue=issue,
            sequence_number=1,
            source_ordinal=1,
            resolution_state=StoryArcResolutionState.RESOLVED,
            source_kind=StoryArcSourceKind.PULLBOX,
        )
        session.add_all([library_file, membership])
        await session.commit()
        arc_id = arc.id
        root_id = root.id
        membership_id = membership.id
    service = StoryArcPlacementSyncService()
    current = _payload(root_id, old_root)
    async with factory() as session:
        await service.update_policy(
            session,
            arc_id,
            expected_revision=1,
            proposal=StoryArcPlacementPolicyInput(**current),
        )
        await service.sync_membership(session, arc_id, membership_id)
    return arc_id, root_id, old_root, new_root


def _payload(root_id: int, destination: Path) -> dict[str, object]:
    return {
        "mode": StoryArcPlacementPolicyMode.COPY,
        "target_library_root_id": root_id,
        "destination_root": str(destination),
        "folder_template": "{StoryArc}",
        "file_template": "{ReadingOrder:03d} - {Series} {IssueNumber}",
        "symlink_style": None,
        "synchronize": True,
    }


async def test_authenticated_preview_and_confirmation_prepare_are_read_only(
    migration_client: AsyncClient,
    migration_api_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_id, root_id, old_root, new_root = await _seed_api_scope(migration_api_db, tmp_path)
    proposal = {"expected_revision": 2, **_payload(root_id, new_root)}

    preview = await migration_client.post(
        f"/api/v1/story-arcs/{arc_id}/placement-policy/migration-preview",
        params={"limit": 1, "after_placement_id": 0},
        json=proposal,
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["expected_revision"] == 2
    assert body["current_policy"]["destination_root"] == str(old_root.resolve())
    assert body["proposed_policy"]["destination_root"] == str(new_root.resolve())
    assert body["managed_migrate_count"] == 1
    assert body["referenced_preserved_count"] == 0
    assert body["blocked_count"] == 0
    assert body["required_confirmation"] == STORY_ARC_POLICY_MIGRATION_CONFIRMATION
    assert body["execution_supported"] is False
    assert body["filesystem_mutated"] is False
    assert body["items"][0]["old_path"].startswith(str(old_root))
    assert body["items"][0]["new_path"].startswith(str(new_root))
    async with migration_api_db() as session:
        last_used_at = await session.scalar(select(APIKey.last_used_at))
    assert last_used_at is not None

    wrong = await migration_client.post(
        f"/api/v1/story-arcs/{arc_id}/placement-policy/migration-confirmation",
        json={
            **proposal,
            "preview_token": body["preview_token"],
            "confirmation": "CHANGE POLICY",
        },
    )
    assert wrong.status_code == 422

    prepared = await migration_client.post(
        f"/api/v1/story-arcs/{arc_id}/placement-policy/migration-confirmation",
        json={
            **proposal,
            "preview_token": body["preview_token"],
            "confirmation": STORY_ARC_POLICY_MIGRATION_CONFIRMATION,
        },
    )
    assert prepared.status_code == 200, prepared.text
    assert prepared.json() == {
        "story_arc_id": arc_id,
        "expected_revision": 2,
        "scope_digest": body["scope_digest"],
        "confirmed": True,
        "ready_for_execution": False,
        "execution_supported": False,
        "mutation_performed": False,
        "policy_update_block_code": "managed_policy_change_requires_migration",
    }

    still_blocked = await migration_client.put(
        f"/api/v1/story-arcs/{arc_id}/placement-policy",
        json=proposal,
    )
    assert still_blocked.status_code == 409
    assert still_blocked.json()["detail"]["code"] == ("managed_policy_change_requires_migration")


async def test_migration_preview_requires_authentication(
    migration_api_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    from pullbox.api.deps import get_db_dep
    from pullbox.api.middleware import reset_setup_cache
    from pullbox.app import create_app

    arc_id, root_id, _old_root, new_root = await _seed_api_scope(migration_api_db, tmp_path)
    app = create_app()

    async def override_db() -> AsyncGenerator[AsyncSession, None]:
        async with migration_api_db() as session:
            yield session

    app.dependency_overrides[get_db_dep] = override_db
    reset_setup_cache()
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/api/v1/story-arcs/{arc_id}/placement-policy/migration-preview",
            json={"expected_revision": 2, **_payload(root_id, new_root)},
        )
    app.dependency_overrides.clear()
    reset_setup_cache()
    assert response.status_code == 401
