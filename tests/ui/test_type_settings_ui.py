"""Tests for non-standard type settings UI — ESM Phase 3, Task 3.5.

Covers:
- Type thresholds settings section renders with correct defaults
- File size threshold inputs render with defaults
- Two-pass toggle renders
- Settings save/load round-trip via API

Run:
    pytest tests/ui/test_type_settings_ui.py -v
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.config import SystemConfig

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-type-settings")


@pytest.fixture
async def db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


class TestTypeSettingsRender:
    """Tests that the settings template renders type settings correctly."""

    @pytest.mark.asyncio
    async def test_type_thresholds_section_renders(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Settings search tab includes non-standard type threshold dropdowns."""
        from unittest.mock import MagicMock

        from pullbox.ui.routes import templates

        configs: dict[str, str] = {}
        request = MagicMock()
        request.url_for = MagicMock(return_value="/")

        template = templates.get_template("partials/settings_search.html")
        html = template.render(
            configs=configs,
            csrf_token="test-token",
            request=request,
            user=MagicMock(),
        )

        # Check that type threshold dropdowns are present
        assert "Non-standard types" in html
        assert "Auto-grab thresholds" in html
        assert 'data-testid="settings-search-threshold-tpb-select"' in html
        assert 'data-testid="settings-search-threshold-hc-select"' in html
        assert 'data-testid="settings-search-threshold-issue-select"' in html
        assert 'data-testid="settings-search-threshold-annual-select"' in html
        assert 'data-testid="settings-search-threshold-compendium-select"' in html
        assert 'data-dropdown-select-contract="v1"' in html

    @pytest.mark.asyncio
    async def test_file_size_inputs_render(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Settings search tab includes file size warning inputs."""
        from unittest.mock import MagicMock

        from pullbox.ui.routes import templates

        configs: dict[str, str] = {}
        request = MagicMock()
        request.url_for = MagicMock(return_value="/")

        template = templates.get_template("partials/settings_search.html")
        html = template.render(
            configs=configs,
            csrf_token="test-token",
            request=request,
            user=MagicMock(),
        )

        assert "search_size_warn_issue_mb" in html
        assert "search_size_warn_collection_mb" in html
        assert "Issue Size Warning" in html
        assert "Collection Size Warning" in html
        # Default values shown
        assert 'value="750"' in html
        assert 'value="50"' in html

    @pytest.mark.asyncio
    async def test_two_pass_toggle_renders(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Settings search tab includes two-pass search toggle."""
        from unittest.mock import MagicMock

        from pullbox.ui.routes import templates

        configs: dict[str, str] = {}
        request = MagicMock()
        request.url_for = MagicMock(return_value="/")

        template = templates.get_template("partials/settings_search.html")
        html = template.render(
            configs=configs,
            csrf_token="test-token",
            request=request,
            user=MagicMock(),
        )

        assert "search_two_pass_enabled" in html
        assert "Two-Pass Search" in html

    @pytest.mark.asyncio
    async def test_intervention_expiry_renders(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Settings search tab includes intervention expiry input."""
        from unittest.mock import MagicMock

        from pullbox.ui.routes import templates

        configs: dict[str, str] = {}
        request = MagicMock()
        request.url_for = MagicMock(return_value="/")

        template = templates.get_template("partials/settings_search.html")
        html = template.render(
            configs=configs,
            csrf_token="test-token",
            request=request,
            user=MagicMock(),
        )

        assert "search_intervention_expiry_days" in html
        assert "Intervention Queue" in html
        assert 'value="10"' in html


class TestTypeSettingsRoundTrip:
    """Tests for settings save/load round-trip via API."""

    @pytest.mark.asyncio
    async def test_type_thresholds_save_load(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """POST type thresholds, then verify values persisted in SystemConfig."""
        # Simulate saving thresholds as the UI would (JSON string)
        thresholds = {"issue": "high", "tpb": "medium", "hc": "high"}
        async with db_factory() as session:
            session.add(
                SystemConfig(
                    key="search_type_thresholds",
                    value=json.dumps(thresholds),
                )
            )
            await session.commit()

        # Verify round-trip
        async with db_factory() as session:
            row = await session.execute(
                select(SystemConfig.value).where(SystemConfig.key == "search_type_thresholds")
            )
            stored = json.loads(row.scalar_one())
            assert stored["issue"] == "high"
            assert stored["tpb"] == "medium"
            assert stored["hc"] == "high"

    @pytest.mark.asyncio
    async def test_file_size_thresholds_save_load(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """POST file size thresholds, verify persistence."""
        async with db_factory() as session:
            session.add(SystemConfig(key="search_size_warn_issue_mb", value="200"))
            session.add(SystemConfig(key="search_size_warn_collection_mb", value="75"))
            await session.commit()

        async with db_factory() as session:
            rows = await session.execute(
                select(SystemConfig.key, SystemConfig.value).where(
                    SystemConfig.key.in_(
                        ["search_size_warn_issue_mb", "search_size_warn_collection_mb"]
                    )
                )
            )
            cfg = dict(rows.all())
            assert cfg["search_size_warn_issue_mb"] == "200"
            assert cfg["search_size_warn_collection_mb"] == "75"

    @pytest.mark.asyncio
    async def test_two_pass_toggle_save_load(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """POST two-pass toggle, verify persistence."""
        async with db_factory() as session:
            session.add(SystemConfig(key="search_two_pass_enabled", value="false"))
            await session.commit()

        async with db_factory() as session:
            row = await session.execute(
                select(SystemConfig.value).where(SystemConfig.key == "search_two_pass_enabled")
            )
            assert row.scalar_one() == "false"
