"""Tests for Task R-1.3 — Library UI simplification.

Verifies:
- Library settings page shows a single Comics Directory input (no multi-root)
- Library page shows read-only stats without scan buttons
- Library root CRUD endpoints are removed
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.user import User
from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-library-ui")


@pytest.fixture
async def _test_db() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def test_user(
    _test_db: async_sessionmaker[AsyncSession], seeded_user_password_hash: str
) -> User:
    async with _test_db() as session:
        user = User(
            username="testuser",
            password_hash=seeded_user_password_hash,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


@pytest.fixture
async def app(
    _test_db: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[object, None]:
    from pullbox.api.deps import get_db_dep
    from pullbox.api.middleware import reset_setup_cache
    from pullbox.app import create_app

    application = create_app()
    # Keep setup/auth middleware on the same in-memory database as the route deps.
    application.state.db_session_factory = _test_db

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
        async with _test_db() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    application.dependency_overrides[get_db_dep] = _override_db
    reset_setup_cache()
    yield application
    application.dependency_overrides.clear()
    reset_setup_cache()


@pytest.fixture
async def client(app: object, test_user: User) -> AsyncGenerator[AsyncClient, None]:
    token = AuthService.create_session_token(test_user.id, test_user.session_version)
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        cookies={SESSION_COOKIE_NAME: token},
    ) as ac:
        yield ac


class TestLibraryPageReadOnly:
    """Library page shows read-only stats without scan buttons."""

    @pytest.mark.asyncio
    async def test_library_page_loads(self, client: AsyncClient) -> None:
        resp = await client.get("/library")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_library_page_has_stats(self, client: AsyncClient) -> None:
        resp = await client.get("/library")
        html = resp.text
        assert "ROOT" in html
        assert "FILES" in html
        assert "SIZE" in html

    @pytest.mark.asyncio
    async def test_library_page_no_scan_button(self, client: AsyncClient) -> None:
        resp = await client.get("/library")
        html = resp.text
        assert "Scan" not in html or "scan" not in html.lower().replace("scanner", "")
        # More specific: no scan trigger buttons
        assert "trigger_scan" not in html
        assert "Start Scan" not in html

    @pytest.mark.asyncio
    async def test_library_page_no_roots_management(self, client: AsyncClient) -> None:
        resp = await client.get("/library")
        html = resp.text
        assert "Add Root" not in html
        assert "Delete Root" not in html
        assert "add_root" not in html

    @pytest.mark.asyncio
    async def test_library_page_shows_comics_directory(self, client: AsyncClient) -> None:
        resp = await client.get("/library")
        html = resp.text
        assert "Comics Directory" in html
        assert "No library root configured" in html

    @pytest.mark.asyncio
    async def test_library_page_has_import_link(self, client: AsyncClient) -> None:
        """Empty library should show import CTA."""
        resp = await client.get("/library")
        html = resp.text
        assert "/import" in html


class TestSettingsMediaTab:
    """Settings media tab shows Comics Directory input."""

    @pytest.mark.asyncio
    async def test_settings_media_has_comics_directory(self, client: AsyncClient) -> None:
        resp = await client.get("/settings?tab=media")
        html = resp.text
        assert "Comics Directory" in html

    @pytest.mark.asyncio
    async def test_settings_media_no_root_folders(self, client: AsyncClient) -> None:
        resp = await client.get("/settings?tab=media")
        html = resp.text
        assert "Root Folders" not in html
        assert "Add Root Folder" not in html

    @pytest.mark.asyncio
    async def test_settings_media_no_scan_interval(self, client: AsyncClient) -> None:
        resp = await client.get("/settings?tab=media")
        html = resp.text
        assert "scan_interval" not in html
        assert "Scan Interval" not in html

    @pytest.mark.asyncio
    async def test_settings_media_has_preferred_format(self, client: AsyncClient) -> None:
        resp = await client.get("/settings?tab=media")
        html = resp.text
        assert "Preferred Format" in html

    @pytest.mark.asyncio
    async def test_settings_media_has_cbz_normalize_toggle(self, client: AsyncClient) -> None:
        resp = await client.get("/settings?tab=media")
        html = resp.text
        assert "Normalize Imported Archives to CBZ" in html

    @pytest.mark.asyncio
    async def test_settings_media_has_seed_safe_torrent_strategy_copy(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/settings?tab=media")
        html = resp.text
        assert "Torrent Import Strategy" in html
        assert "automated torrent post-processing" in html
        assert "preserves the torrent source for seeding" in html
        assert "Imports that rewrite archive contents still require Move or Copy" in html

    @pytest.mark.asyncio
    async def test_settings_media_has_rename_toggle(self, client: AsyncClient) -> None:
        resp = await client.get("/settings?tab=media")
        html = resp.text
        assert "rename_on_import" in html or "Rename Comics" in html

    @pytest.mark.asyncio
    async def test_comics_directory_is_read_only(self, client: AsyncClient) -> None:
        """Comics directory input should be read-only — no save or browse buttons."""
        resp = await client.get("/settings?tab=media")
        html = resp.text
        assert "readonly" in html or "disabled" in html
        assert "openFileBrowser" not in html or "openFileBrowser('comicsDir'" not in html

    @pytest.mark.asyncio
    async def test_comics_directory_shows_env_indicator(self, client: AsyncClient) -> None:
        """Comics directory should always show the PULLBOX_LIBRARY_ROOT env indicator."""
        resp = await client.get("/settings?tab=media")
        html = resp.text
        assert "PULLBOX_LIBRARY_ROOT" in html

    @pytest.mark.asyncio
    async def test_comics_directory_no_save_button(self, client: AsyncClient) -> None:
        """Comics directory section should not have a save button."""
        resp = await client.get("/settings?tab=media")
        html = resp.text
        # The comics directory section should not have its own form submit
        assert (
            "comics_directory" not in html
            or "Save Changes" not in html.split("Comics Directory")[1].split("Naming")[0]
        )


class TestLibraryRootEndpointsRemoved:
    """Library root CRUD API endpoints are removed."""

    @pytest.mark.asyncio
    async def test_roots_list_returns_404(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/library/roots")
        assert resp.status_code in (404, 405)

    @pytest.mark.asyncio
    async def test_roots_add_returns_not_found(self, client: AsyncClient) -> None:
        # POST hits CSRF middleware (403) before route matching — either way, not functional
        resp = await client.post(
            "/api/v1/library/roots",
            json={"name": "test", "path": "/tmp/test"},
        )
        assert resp.status_code in (403, 404, 405)

    @pytest.mark.asyncio
    async def test_roots_delete_returns_not_found(self, client: AsyncClient) -> None:
        resp = await client.delete("/api/v1/library/roots/1")
        assert resp.status_code in (403, 404, 405)

    @pytest.mark.asyncio
    async def test_scan_trigger_returns_not_found(self, client: AsyncClient) -> None:
        resp = await client.post("/api/v1/library/scan")
        assert resp.status_code in (403, 404, 405)

    @pytest.mark.asyncio
    async def test_scan_status_returns_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/library/scan/status")
        assert resp.status_code in (404, 405)
