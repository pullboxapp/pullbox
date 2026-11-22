"""Unit tests for the about endpoint — path sanitization in production mode.

Tests verify that filesystem paths are hidden in production mode and
shown in debug mode.

Run:
    pytest tests/unit/test_about_endpoint.py -v
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-about-endpoint")

from pullbox.api.v1.system import get_about
from pullbox.config import PullboxSettings


def _mock_user() -> MagicMock:
    user = MagicMock()
    user.id = 1
    user.username = "admin"
    user.is_active = True
    return user


def _make_settings(*, debug: bool) -> PullboxSettings:
    return PullboxSettings(
        secret_key="test",
        debug=debug,
        db_url="sqlite+aiosqlite:///home/adam/data/pullbox.db",
        data_dir="/home/adam/data",
    )  # type: ignore[call-arg]


class TestAboutHidesPathsInProduction:
    """In production mode, filesystem paths must not be exposed."""

    @pytest.mark.parametrize("debug", [False, True])
    async def test_postgres_shows_connected_version_and_no_sqlite_path(self, debug):
        settings = _make_settings(debug=debug).model_copy(
            update={
                "db_url": "postgresql+asyncpg://dsn-fixture:dsn-fixture@db/dsn-fixture?passfile=dsn-fixture"
            }
        )
        session = MagicMock()
        session.get_bind.return_value.dialect.server_version_info = (18, 1)
        with patch("pullbox.config.get_settings", return_value=settings):
            data = await get_about(_mock_user(), session)
        assert data["database"] == "PostgreSQL 18.1"
        assert data["database_path"] is None and data["database_size_bytes"] is None
        assert "dsn-fixture" not in str(data)

    @pytest.mark.asyncio
    async def test_about_hides_paths_in_production(self) -> None:
        settings = _make_settings(debug=False)
        with patch("pullbox.config.get_settings", return_value=settings):
            data = await get_about(_user=_mock_user(), session=MagicMock())
        assert data["database_path"] == "pullbox.db"
        assert "/" not in data["database_path"]

    @pytest.mark.asyncio
    async def test_about_includes_data_directory(self) -> None:
        settings = _make_settings(debug=False)
        with patch("pullbox.config.get_settings", return_value=settings):
            data = await get_about(_user=_mock_user(), session=MagicMock())
        assert "data_directory" in data
        assert isinstance(data["data_directory"], str)

    @pytest.mark.asyncio
    async def test_about_includes_config_directory(self) -> None:
        settings = _make_settings(debug=False)
        with patch("pullbox.config.get_settings", return_value=settings):
            data = await get_about(_user=_mock_user(), session=MagicMock())
        assert "config_directory" in data
        assert isinstance(data["config_directory"], str)

    @pytest.mark.asyncio
    async def test_about_includes_startup_directory(self) -> None:
        settings = _make_settings(debug=False)
        with patch("pullbox.config.get_settings", return_value=settings):
            data = await get_about(_user=_mock_user(), session=MagicMock())
        assert "startup_directory" in data
        assert isinstance(data["startup_directory"], str)


class TestAboutShowsPathsInDebug:
    """In debug mode, full paths are shown for developer convenience."""

    @pytest.mark.asyncio
    async def test_about_shows_paths_in_debug(self) -> None:
        settings = _make_settings(debug=True)
        with patch("pullbox.config.get_settings", return_value=settings):
            data = await get_about(_user=_mock_user(), session=MagicMock())
        assert "home/adam/data/pullbox.db" in data["database_path"]
        assert "data_directory" in data
        assert "startup_directory" in data


class TestAboutAlwaysPresent:
    """Fields that should always be present regardless of mode."""

    @pytest.mark.asyncio
    async def test_about_always_shows_version(self) -> None:
        settings = _make_settings(debug=False)
        with patch("pullbox.config.get_settings", return_value=settings):
            data = await get_about(_user=_mock_user(), session=MagicMock())
        assert "version" in data

    @pytest.mark.asyncio
    async def test_about_always_shows_uptime(self) -> None:
        settings = _make_settings(debug=False)
        with patch("pullbox.config.get_settings", return_value=settings):
            data = await get_about(_user=_mock_user(), session=MagicMock())
        assert "uptime_seconds" in data
