"""Global arc file defaults are adopted once, never retroactively or by imports."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pullbox.models.config import SystemConfig
from pullbox.models.library import LibraryRoot
from pullbox.models.story_arc import StoryArc
from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService
from pullbox.services.story_arc_catalog import StoryArcCatalogService
from pullbox.services.story_arc_placement_integration import (
    StoryArcPlacementPolicyInput,
    StoryArcPlacementSyncService,
)
from tests.story_arc_catalog_fixtures import CatalogProvider

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytest_plugins = ["conftest_security"]


def _csrf(client: AsyncClient) -> dict[str, str]:
    token = client.cookies.get(SESSION_COOKIE_NAME) or ""
    return {"X-CSRF-Token": AuthService.get_csrf_token_from_session(token) or ""}


async def _save(client: AsyncClient, values: dict[str, str]):
    return await client.put("/api/v1/config", json={"values": values}, headers=_csrf(client))


async def _root(factory: async_sessionmaker[AsyncSession], path: Path) -> int:
    async with factory() as session:
        root = LibraryRoot(name="Arc defaults library", path=str(path), enabled=True)
        session.add(root)
        await session.commit()
        return root.id


async def test_media_exposes_safe_off_by_default_story_arc_files(authenticated_client: AsyncClient):
    response = await authenticated_client.get("/settings?tab=media")
    assert response.status_code == 200
    assert 'data-testid="settings-story-arc-files"' in response.text
    assert "Story Arc Files" in response.text
    assert "newly added arcs only" in response.text
    assert 'name="story_arc_files_enabled"' in response.text
    response = await authenticated_client.get("/api/v1/config")
    values = {row["key"]: row["value"] for row in response.json()}
    assert values["story_arc_files_enabled"] == "false"


@pytest.mark.parametrize(
    "invalid",
    [
        {"story_arc_files_method": "reference_only"},
        {"story_arc_files_method": "move"},
        {"story_arc_files_folder_template": "../{StoryArc}"},
        {"story_arc_files_reading_order_width": "1000"},
        {"story_arc_files_enabled": "true"},
    ],
)
async def test_invalid_defaults_are_atomic(authenticated_client: AsyncClient, sec_db, invalid):
    response = await _save(authenticated_client, {"instance_name": "Must not persist", **invalid})
    assert response.status_code == 422
    async with sec_db() as session:
        row = await session.get(SystemConfig, "instance_name")
        assert row is None or row.value != "Must not persist"


async def test_defaults_snapshot_is_stable_and_explicit_import_policy_wins(
    authenticated_client: AsyncClient, sec_db, tmp_path: Path
):
    root_id = await _root(sec_db, tmp_path)
    saved = await _save(
        authenticated_client,
        {
            "story_arc_files_enabled": "true",
            "story_arc_files_method": "copy",
            "story_arc_files_library_root_id": str(root_id),
            "story_arc_files_destination": str(tmp_path),
            "story_arc_files_prefix_reading_order": "true",
        },
    )
    assert saved.status_code == 200, saved.text
    provider = CatalogProvider()
    catalog = StoryArcCatalogService(provider)
    preview = await catalog.preview("42")
    async with sec_db() as session:
        arc = await catalog.add(
            session, preview, ordered_issue_provider_ids=["101", "102"], library_root_id=root_id
        )
        old_id, snapshot = arc.id, dict(arc.policy_snapshot)
        await session.commit()
    assert snapshot["mode"] == "copy"
    assert snapshot["file_template"] == "{ReadingOrder:02d} - {OriginalFilename}"
    assert snapshot["synchronize"] is True
    assert (
        await _save(authenticated_client, {"story_arc_files_enabled": "false"})
    ).status_code == 200
    async with sec_db() as session:
        arc = await session.get(StoryArc, old_id)
        assert arc.policy_snapshot == snapshot
        effective = await StoryArcPlacementSyncService().get_policy(session, old_id)
        assert effective.snapshot == snapshot
        assert effective.synchronize is True  # future downloads still use the saved policy
        new_arc = await catalog.add(
            session,
            await catalog.preview("43"),
            ordered_issue_provider_ids=["101", "102"],
            library_root_id=root_id,
        )
        assert new_arc.policy_snapshot["mode"] == "logical"
        imported = await catalog.add(
            session,
            await catalog.preview("44"),
            ordered_issue_provider_ids=["101", "102"],
            library_root_id=root_id,
            placement_policy=StoryArcPlacementPolicyInput("reference_only", root_id, str(tmp_path)),
        )
        assert imported.policy_snapshot["mode"] == "reference_only"
        await session.commit()
    detail = await authenticated_client.get(f"/story-arcs/{old_id}")
    assert 'data-testid="story-arc-placement-policy-form"' not in detail.text
    assert 'data-testid="story-arc-placement-state"' in detail.text
    assert 'href="/settings?tab=media#story-arc-files"' in detail.text
    assert list(tmp_path.iterdir()) == []


async def test_default_destination_outside_root_rejected(
    authenticated_client: AsyncClient, sec_db, tmp_path: Path
):
    root_path = tmp_path / "library"
    root_path.mkdir()
    root_id = await _root(sec_db, root_path)
    response = await _save(
        authenticated_client,
        {
            "story_arc_files_enabled": "true",
            "story_arc_files_library_root_id": str(root_id),
            "story_arc_files_destination": str(tmp_path),
        },
    )
    assert response.status_code == 422
    async with sec_db() as session:
        assert list(await session.scalars(select(StoryArc))) == []


async def test_arc_naming_preview_uses_real_renderer_without_writes(
    authenticated_client: AsyncClient,
):
    response = await authenticated_client.post(
        "/api/v1/config/story-arc-files/preview",
        json={
            "values": {
                "story_arc_files_prefix_reading_order": "true",
                "story_arc_files_reading_order_width": "3",
            }
        },
        headers=_csrf(authenticated_client),
    )
    assert response.status_code == 200, response.text
    assert response.json()["path"] == "The Court of Owls/001 - Batman 001.cbz"
