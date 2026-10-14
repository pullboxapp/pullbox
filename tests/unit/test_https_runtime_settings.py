"""Runtime and validation contracts for native HTTPS settings."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from pullbox.models.config import DEFAULT_SYSTEM_CONFIG


class TestHttpsDefaults:
    """HTTPS has DB-editable defaults plus runtime env override fields."""

    def test_https_fields_exist_in_runtime_settings(self) -> None:
        from pullbox.config import PullboxSettings

        assert PullboxSettings.model_fields["https_enabled"].default is False
        assert PullboxSettings.model_fields["https_cert_path"].default == ""
        assert PullboxSettings.model_fields["https_key_path"].default == ""
        assert PullboxSettings.model_fields["https_cert_root"].default == Path("/config/certs")

    def test_https_keys_exist_in_db_defaults(self) -> None:
        assert DEFAULT_SYSTEM_CONFIG["https_enabled"] == ("false", "bool")
        assert DEFAULT_SYSTEM_CONFIG["https_cert_path"] == ("", "string")
        assert DEFAULT_SYSTEM_CONFIG["https_key_path"] == ("", "string")


class TestHttpsRuntimeResolver:
    """Startup HTTPS resolution keeps env overrides above persisted DB values."""

    def test_disabled_by_default_returns_no_uvicorn_ssl_kwargs(self) -> None:
        from pullbox.core.https_runtime import (
            resolve_https_runtime_settings,
            uvicorn_ssl_kwargs,
        )

        settings = SimpleNamespace(
            https_enabled=False,
            https_cert_path="",
            https_key_path="",
            https_cert_root=Path("/config/certs"),
            db_url="sqlite+aiosqlite:///:memory:",
        )

        resolved = resolve_https_runtime_settings(settings=settings, db_values={})

        assert resolved.enabled is False
        assert resolved.cert_path == ""
        assert resolved.key_path == ""
        assert resolved.cert_root == Path("/config/certs")
        assert uvicorn_ssl_kwargs(resolved) == {}

    def test_db_values_are_used_when_env_values_are_absent(self) -> None:
        from pullbox.core.https_runtime import resolve_https_runtime_settings

        settings = SimpleNamespace(
            https_enabled=False,
            https_cert_path="",
            https_key_path="",
            https_cert_root=Path("/config/certs"),
            db_url="sqlite+aiosqlite:///:memory:",
        )

        resolved = resolve_https_runtime_settings(
            settings=settings,
            db_values={
                "https_enabled": "true",
                "https_cert_path": "/config/certs/db.crt",
                "https_key_path": "/config/certs/db.key",
            },
            environ={},
        )

        assert resolved.enabled is True
        assert resolved.cert_path == "/config/certs/db.crt"
        assert resolved.key_path == "/config/certs/db.key"

    def test_env_values_override_db_values(self) -> None:
        from pullbox.core.https_runtime import resolve_https_runtime_settings

        settings = SimpleNamespace(
            https_enabled=False,
            https_cert_path="",
            https_key_path="",
            https_cert_root=Path("/config/certs"),
            db_url="sqlite+aiosqlite:///:memory:",
        )

        resolved = resolve_https_runtime_settings(
            settings=settings,
            db_values={
                "https_enabled": "false",
                "https_cert_path": "/config/certs/db.crt",
                "https_key_path": "/config/certs/db.key",
            },
            environ={
                "PULLBOX_HTTPS_ENABLED": "true",
                "PULLBOX_HTTPS_CERT_PATH": "/config/certs/env.crt",
                "PULLBOX_HTTPS_KEY_PATH": "/config/certs/env.key",
                "PULLBOX_HTTPS_CERT_ROOT": "/config/certs",
            },
        )

        assert resolved.enabled is True
        assert resolved.cert_path == "/config/certs/env.crt"
        assert resolved.key_path == "/config/certs/env.key"

    def test_load_https_db_values_reads_only_known_https_keys(self, tmp_path: Path) -> None:
        from pullbox.core.https_runtime import load_https_db_values

        db_path = tmp_path / "pullbox.db"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("CREATE TABLE system_config (key TEXT PRIMARY KEY, value TEXT)")
            conn.executemany(
                "INSERT INTO system_config (key, value) VALUES (?, ?)",
                [
                    ("https_enabled", "true"),
                    ("https_cert_path", "/config/certs/server.crt"),
                    ("https_key_path", "/config/certs/server.key"),
                    ("unrelated", "ignored"),
                ],
            )

        settings = SimpleNamespace(db_url=f"sqlite+aiosqlite:///{db_path}")

        assert load_https_db_values(settings) == {
            "https_enabled": "true",
            "https_cert_path": "/config/certs/server.crt",
            "https_key_path": "/config/certs/server.key",
        }


class TestHttpsValidation:
    """Enabled HTTPS must point to a readable cert/key pair under the cert root."""

    def test_enabled_https_requires_absolute_paths(self, tmp_path: Path) -> None:
        from pullbox.core.https_runtime import (
            HttpsRuntimeSettings,
            validate_https_runtime_settings,
        )

        resolved = HttpsRuntimeSettings(
            enabled=True,
            cert_path="relative.crt",
            key_path=str(tmp_path / "server.key"),
            cert_root=tmp_path,
        )

        with pytest.raises(ValueError, match="absolute"):
            validate_https_runtime_settings(resolved)

    def test_enabled_https_rejects_paths_outside_cert_root(self, tmp_path: Path) -> None:
        from pullbox.core.https_runtime import (
            HttpsRuntimeSettings,
            validate_https_runtime_settings,
        )

        cert_root = tmp_path / "certs"
        cert_root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        cert = outside / "server.crt"
        key = cert_root / "server.key"
        cert.write_text("cert")
        key.write_text("key")

        resolved = HttpsRuntimeSettings(
            enabled=True,
            cert_path=str(cert),
            key_path=str(key),
            cert_root=cert_root,
        )

        with pytest.raises(ValueError, match="inside"):
            validate_https_runtime_settings(resolved)

    def test_enabled_https_loads_cert_chain(self, tmp_path: Path, monkeypatch) -> None:
        from pullbox.core import https_runtime
        from pullbox.core.https_runtime import (
            HttpsRuntimeSettings,
            uvicorn_ssl_kwargs,
            validate_https_runtime_settings,
        )

        cert_root = tmp_path / "certs"
        cert_root.mkdir()
        cert = cert_root / "server.crt"
        key = cert_root / "server.key"
        cert.write_text("cert")
        key.write_text("key")
        calls: list[tuple[str, str]] = []

        def fake_load_cert_chain(certfile: str, keyfile: str) -> None:
            calls.append((certfile, keyfile))

        monkeypatch.setattr(https_runtime, "_load_cert_chain", fake_load_cert_chain)

        resolved = HttpsRuntimeSettings(
            enabled=True,
            cert_path=str(cert),
            key_path=str(key),
            cert_root=cert_root,
        )

        validate_https_runtime_settings(resolved)

        assert calls == [(str(cert), str(key))]
        assert uvicorn_ssl_kwargs(resolved) == {
            "ssl_certfile": str(cert),
            "ssl_keyfile": str(key),
        }
