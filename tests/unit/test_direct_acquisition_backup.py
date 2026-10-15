"""Backup/restore contracts for encrypted direct-download configuration."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from pullbox.core.encryption import _get_fernet, decrypt_secret, encrypt_secret
from pullbox.services.backup_service import BackupService

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _deterministic_application_secret() -> None:
    provider = MagicMock()
    provider.secret_key.return_value = "direct-download-backup-secret"
    _get_fernet.cache_clear()
    with patch("pullbox.core.config_file.get_config_provider", return_value=provider):
        yield
    _get_fernet.cache_clear()


def test_backup_restore_preserves_decryptable_direct_provider_ciphertext(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "pullbox.db"
    backup_dir = tmp_path / "backups"
    original_ciphertext = encrypt_secret("provider-bearer-token")

    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            "CREATE TABLE direct_provider_configs "
            "(id INTEGER PRIMARY KEY, encrypted_bearer_token TEXT)"
        )
        connection.execute(
            "INSERT INTO direct_provider_configs VALUES (?, ?)",
            (1, original_ciphertext),
        )

    service = BackupService(backup_dir=backup_dir, db_path=db_path)
    backup = service.create_backup()

    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            "UPDATE direct_provider_configs SET encrypted_bearer_token = ? WHERE id = 1",
            (encrypt_secret("replacement-token"),),
        )
    assert service.restore_backup(backup.filename) is True

    with closing(sqlite3.connect(db_path)) as connection, connection:
        restored_ciphertext = connection.execute(
            "SELECT encrypted_bearer_token FROM direct_provider_configs WHERE id = 1"
        ).fetchone()[0]

    assert restored_ciphertext == original_ciphertext
    assert decrypt_secret(restored_ciphertext) == "provider-bearer-token"
    assert "provider-bearer-token" not in restored_ciphertext


def test_backup_restore_preserves_decryptable_resolver_auth_headers(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "pullbox.db"
    backup_dir = tmp_path / "backups"
    original_ciphertext = encrypt_secret("resolver-auth-token")

    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            "CREATE TABLE direct_resolver_configs "
            "(id INTEGER PRIMARY KEY, encrypted_auth_headers JSON)"
        )
        connection.execute(
            "INSERT INTO direct_resolver_configs VALUES (?, ?)",
            (1, json.dumps({"Authorization": original_ciphertext})),
        )

    service = BackupService(backup_dir=backup_dir, db_path=db_path)
    backup = service.create_backup()

    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            "UPDATE direct_resolver_configs SET encrypted_auth_headers = ? WHERE id = 1",
            (json.dumps({"Authorization": encrypt_secret("replacement-token")}),),
        )
    assert service.restore_backup(backup.filename) is True

    with closing(sqlite3.connect(db_path)) as connection, connection:
        restored_json = connection.execute(
            "SELECT encrypted_auth_headers FROM direct_resolver_configs WHERE id = 1"
        ).fetchone()[0]

    restored_ciphertext = json.loads(restored_json)["Authorization"]
    assert restored_ciphertext == original_ciphertext
    assert decrypt_secret(restored_ciphertext) == "resolver-auth-token"
    assert "resolver-auth-token" not in restored_ciphertext
