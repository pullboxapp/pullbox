"""Tests for sanitized diagnostic database snapshots."""

from __future__ import annotations

import sqlite3

from pullbox.services.diagnostic_db_snapshot import create_sanitized_db_copy


def _create_snapshot_source(path) -> None:  # type: ignore[no-untyped-def]
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);
            CREATE TABLE api_keys (id INTEGER PRIMARY KEY, token TEXT);
            CREATE TABLE issues (id INTEGER PRIMARY KEY);
            CREATE TABLE audit_logs (
                id INTEGER PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                detail TEXT
            );
            CREATE TABLE issue_reader_states (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE
            );
            CREATE TABLE system_config (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE download_client_configs (
                id INTEGER PRIMARY KEY,
                api_key TEXT,
                password TEXT
            );
            CREATE TABLE indexer_configs (
                id INTEGER PRIMARY KEY,
                api_key TEXT
            );
            """
        )
        conn.execute("INSERT INTO users (username) VALUES ('admin')")
        conn.execute("INSERT INTO api_keys (token) VALUES ('tok-secret')")
        conn.execute("INSERT INTO issues (id) VALUES (10)")
        conn.execute("INSERT INTO audit_logs (user_id, detail) VALUES (1, 'preserve me')")
        conn.execute("INSERT INTO issue_reader_states (user_id, issue_id) VALUES (1, 10)")
        conn.execute(
            "INSERT INTO system_config (key, value) VALUES (?, ?)",
            ("comicvine_api_key", "cv-secret"),
        )
        conn.execute(
            "INSERT INTO system_config (key, value) VALUES (?, ?)",
            ("log_level", "info"),
        )
        conn.execute(
            "INSERT INTO download_client_configs (api_key, password) VALUES (?, ?)",
            ("sab-key", "sab-pass"),
        )
        conn.execute(
            "INSERT INTO indexer_configs (api_key) VALUES (?)",
            ("prowlarr-key",),
        )
        conn.commit()
    finally:
        conn.close()


def test_create_sanitized_db_copy_removes_auth_rows_and_redacts_secrets(tmp_path) -> None:
    source = tmp_path / "pullbox.db"
    _create_snapshot_source(source)

    snapshot = create_sanitized_db_copy(source)

    assert snapshot is not None
    copy_path = tmp_path / "copy.db"
    copy_path.write_bytes(snapshot)
    conn = sqlite3.connect(copy_path)
    try:
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM api_keys").fetchone()[0] == 0
        assert conn.execute("SELECT user_id, detail FROM audit_logs").fetchone() == (
            None,
            "preserve me",
        )
        assert conn.execute("SELECT count(*) FROM issue_reader_states").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            conn.execute(
                "SELECT value FROM system_config WHERE key = 'comicvine_api_key'"
            ).fetchone()[0]
            == "[REDACTED]"
        )
        assert (
            conn.execute("SELECT value FROM system_config WHERE key = 'log_level'").fetchone()[0]
            == "info"
        )
        assert conn.execute("SELECT api_key, password FROM download_client_configs").fetchone() == (
            "[REDACTED]",
            "[REDACTED]",
        )
        assert conn.execute("SELECT api_key FROM indexer_configs").fetchone()[0] == "[REDACTED]"
    finally:
        conn.close()


def test_create_sanitized_db_copy_returns_none_for_missing_file(tmp_path) -> None:
    assert create_sanitized_db_copy(tmp_path / "missing.db") is None
