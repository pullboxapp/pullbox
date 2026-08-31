"""API contracts for the read-only Story Arc Step 1 preflight."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

from pullbox.models.import_job import ImportJob

pytest_plugins = ["conftest_security"]

if TYPE_CHECKING:
    from pathlib import Path

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.asyncio
async def test_story_arc_preview_returns_sanitized_folder_evidence_without_creating_job(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    arc_folder = tmp_path / "Knightfall"
    arc_folder.mkdir()
    (arc_folder / "001 - Batman 497.cbz").write_bytes(b"fixture")
    (arc_folder / "002 - Catwoman 014.cbz").write_bytes(b"fixture")

    from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService

    session_token = authenticated_client.cookies.get(SESSION_COOKIE_NAME) or ""
    csrf = AuthService.get_csrf_token_from_session(session_token) or ""
    response = await authenticated_client.post(
        "/api/v1/import/story-arc-preview",
        json={"source_path": str(tmp_path), "source_type": "filesystem"},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["evidence_detected"] is True
    assert payload["arcs_detected"] == 1
    assert payload["entries_detected"] == 2
    assert payload["resolution"]["pending"] == 2
    assert payload["provider_calls_required"] is True
    assert payload["proposed_policy"]["mode"] == "reference_only"
    assert payload["examples"][0]["relative_path"] == "Knightfall/001 - Batman 497.cbz"
    assert str(tmp_path) not in response.text

    async with sec_db() as session:
        assert await session.scalar(select(func.count(ImportJob.id))) == 0


@pytest.mark.asyncio
async def test_story_arc_preview_requires_authentication(
    unauthenticated_client: AsyncClient,
    tmp_path: Path,
) -> None:
    response = await unauthenticated_client.post(
        "/api/v1/import/story-arc-preview",
        json={"source_path": str(tmp_path), "source_type": "filesystem"},
    )

    assert response.status_code == 401


@pytest.mark.skipif(os.name == "nt", reason="POSIX sensitive-directory policy")
@pytest.mark.parametrize("endpoint", ["layout-preview", "story-arc-preview"])
@pytest.mark.parametrize("source", ["/etc", "/var/log", "/var/../etc"])
async def test_import_preview_rejects_sensitive_paths_at_api_boundary(
    authenticated_client: AsyncClient, endpoint: str, source: str
) -> None:
    from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService

    token = authenticated_client.cookies.get(SESSION_COOKIE_NAME) or ""
    csrf = AuthService.get_csrf_token_from_session(token) or ""
    response = await authenticated_client.post(
        f"/api/v1/import/{endpoint}",
        json={"source_path": source, "source_type": "filesystem"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 422
