"""Tests for __main__.py config.xml-aware startup resolution.

Verifies:
- _resolve_db_path extracts SQLite path from various URL formats
- _resolve_db_path returns None for PostgreSQL URLs
- resolve_host_config reads from config.xml (env > config.xml > default)
- resolve_host_config auto-generates config.xml on first run
- invalid port falls back to default

Run:
    pytest tests/unit/test_main_entry.py -v
"""

from __future__ import annotations

import runpy
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from pathlib import Path


# ── _resolve_db_path ─────────────────────────────────────────────────


class TestResolveDbPath:
    """Verify _resolve_db_path extracts the SQLite file path from DB URLs."""

    def test_sqlite_triple_slash(self) -> None:
        from pathlib import Path as StdPath

        from pullbox.__main__ import _resolve_db_path

        result = _resolve_db_path("sqlite+aiosqlite:///data/pullbox.db")
        assert result == StdPath("data/pullbox.db")

    def test_sqlite_quad_slash_absolute(self) -> None:
        from pathlib import Path as StdPath

        from pullbox.__main__ import _resolve_db_path

        result = _resolve_db_path("sqlite+aiosqlite:////data/pullbox.db")
        assert result == StdPath("/data/pullbox.db")

    def test_sqlite_with_query_params(self) -> None:
        from pathlib import Path as StdPath

        from pullbox.__main__ import _resolve_db_path

        result = _resolve_db_path("sqlite+aiosqlite:///data/pullbox.db?timeout=30")
        assert result == StdPath("data/pullbox.db")

    def test_postgresql_returns_none(self) -> None:
        from pullbox.__main__ import _resolve_db_path

        result = _resolve_db_path("postgresql+asyncpg://user:pass@host:5432/db")
        assert result is None

    def test_empty_string_returns_none(self) -> None:
        from pullbox.__main__ import _resolve_db_path

        result = _resolve_db_path("")
        assert result is None

    def test_sqlite_double_slash_path(self) -> None:
        from pathlib import Path as StdPath

        from pullbox.__main__ import _resolve_db_path

        result = _resolve_db_path("sqlite+aiosqlite://relative.db")
        assert result == StdPath("relative.db")

    def test_sqlite_without_url_separator_returns_none(self) -> None:
        from pullbox.__main__ import _resolve_db_path

        result = _resolve_db_path("sqlite")
        assert result is None

    def test_sqlite_empty_path_returns_none(self) -> None:
        from pullbox.__main__ import _resolve_db_path

        result = _resolve_db_path("sqlite+aiosqlite:///")
        assert result is None


# ── secret bootstrap + runtime launch ────────────────────────────────


class TestEnsureHostSecret:
    """The entrypoint only bootstraps the persisted secret file."""

    def test_config_xml_generated_on_first_startup(self, tmp_path: Path) -> None:
        from pullbox.__main__ import ensure_host_secret

        config_path = tmp_path / "config.xml"
        assert not config_path.exists()

        ensure_host_secret(tmp_path)
        assert config_path.exists()


class TestMainRuntimeSettings:
    """main() should launch uvicorn from runtime-managed settings."""

    def test_main_bounds_graceful_shutdown_for_long_lived_connections(self, tmp_path: Path) -> None:
        """An open SSE connection must not hold container shutdown forever."""
        from unittest.mock import MagicMock, patch

        from pullbox.__main__ import GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS, main

        settings = MagicMock()
        settings.db_url = "sqlite+aiosqlite:////data/pullbox.db"
        settings.data_dir = tmp_path
        settings.bind_address = "127.0.0.1"
        settings.port = 8585

        with (
            patch("pullbox.config.get_settings", return_value=settings),
            patch("pullbox.__main__.ensure_host_secret"),
            patch("uvicorn.run") as uvicorn_run,
        ):
            main()

        assert GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS <= 5
        assert (
            uvicorn_run.call_args.kwargs["timeout_graceful_shutdown"]
            == GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS
        )

    def test_main_uses_runtime_bind_and_port(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        from pullbox.__main__ import main

        settings = MagicMock()
        settings.db_url = "sqlite+aiosqlite:////data/pullbox.db"
        settings.data_dir = tmp_path
        settings.bind_address = "192.168.1.1"
        settings.port = 9999

        with (
            patch("pullbox.config.get_settings", return_value=settings),
            patch("pullbox.__main__.ensure_host_secret") as ensure_mock,
            patch("uvicorn.run") as uvicorn_run,
        ):
            main()

        from pathlib import Path as StdPath

        ensure_mock.assert_called_once_with(tmp_path, db_path=StdPath("/data/pullbox.db"))
        uvicorn_run.assert_called_once_with(
            "pullbox.app:create_app",
            host="192.168.1.1",
            port=9999,
            factory=True,
            timeout_graceful_shutdown=5,
        )

    def test_main_passes_https_ssl_kwargs_when_enabled(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        from pullbox.__main__ import main
        from pullbox.core.https_runtime import HttpsRuntimeSettings

        cert = tmp_path / "server.crt"
        key = tmp_path / "server.key"
        cert.write_text("cert")
        key.write_text("key")

        settings = MagicMock()
        settings.db_url = "sqlite+aiosqlite:////data/pullbox.db"
        settings.data_dir = tmp_path
        settings.bind_address = "0.0.0.0"
        settings.port = 8585
        https_settings = HttpsRuntimeSettings(
            enabled=True,
            cert_path=str(cert),
            key_path=str(key),
            cert_root=tmp_path,
        )

        with (
            patch("pullbox.config.get_settings", return_value=settings),
            patch("pullbox.__main__.ensure_host_secret"),
            patch("pullbox.__main__.resolve_https_runtime_settings", return_value=https_settings),
            patch("pullbox.__main__.validate_https_runtime_settings") as validate_mock,
            patch("uvicorn.run") as uvicorn_run,
        ):
            main()

        validate_mock.assert_called_once_with(https_settings)
        uvicorn_run.assert_called_once_with(
            "pullbox.app:create_app",
            host="0.0.0.0",
            port=8585,
            factory=True,
            timeout_graceful_shutdown=5,
            ssl_certfile=str(cert),
            ssl_keyfile=str(key),
        )

    def test_module_entrypoint_runs_main_path(self, tmp_path: Path, monkeypatch) -> None:
        import uvicorn

        import pullbox.config as pullbox_config
        import pullbox.core.https_runtime as https_runtime

        settings = SimpleNamespace(
            db_url="sqlite+aiosqlite:////data/pullbox.db",
            data_dir=tmp_path,
            bind_address="127.0.0.1",
            port=8585,
        )
        https_settings = SimpleNamespace(enabled=False)
        monkeypatch.delitem(sys.modules, "pullbox.__main__", raising=False)
        monkeypatch.setattr(pullbox_config, "get_settings", lambda: settings)
        monkeypatch.setattr(
            https_runtime,
            "resolve_https_runtime_settings",
            lambda *, settings: https_settings,
        )
        monkeypatch.setattr(https_runtime, "validate_https_runtime_settings", lambda _: None)
        monkeypatch.setattr(https_runtime, "uvicorn_ssl_kwargs", lambda _: {})

        with patch.object(uvicorn, "run") as uvicorn_run:
            runpy.run_module("pullbox.__main__", run_name="__main__")

        assert (tmp_path / "config.xml").exists()
        uvicorn_run.assert_called_once_with(
            "pullbox.app:create_app",
            host="127.0.0.1",
            port=8585,
            factory=True,
            timeout_graceful_shutdown=5,
        )
