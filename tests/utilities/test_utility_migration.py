"""Tests for UT-F.1 — utility tables Alembic migration.

Verifies that the migration creates utility_jobs, utility_job_items,
utility_job_logs tables and adds integrity columns to the issues table.
Covers CHECK constraints, FK constraints, CASCADE deletes, defaults,
edge cases (unicode, large JSON, negative indices), and clean downgrade.

Run:
    pytest tests/utilities/test_utility_migration.py -v
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command

_ALEMBIC_DIR = Path(__file__).resolve().parent.parent.parent / "alembic"
_UTILITY_TABLES_REV = "a9eff3b660f5"  # The migration under test

# Explicit allowlist: the three creation sentinels and four downgrade/roundtrip
# tests still run real migrations from their original empty databases.
_HEAD_SCHEMA_TESTS = frozenset(
    {
        "test_utility_jobs_has_all_columns",
        "test_utility_job_items_has_all_columns",
        "test_utility_job_logs_has_all_columns",
        "test_issues_has_integrity_status",
        "test_issues_has_integrity_checked_at",
        "test_issues_has_integrity_details",
        "test_integrity_status_defaults_to_unchecked",
        "test_integrity_details_defaults_to_empty_json",
        "test_utility_jobs_state_index",
        "test_utility_jobs_created_at_index",
        "test_utility_job_items_job_id_index",
        "test_utility_job_items_job_id_state_index",
        "test_utility_job_items_job_id_item_index_index",
        "test_utility_job_logs_job_id_index",
        "test_utility_job_logs_job_id_level_index",
        "test_utility_job_logs_job_id_timestamp_index",
        "test_issues_integrity_status_index",
        "test_invalid_job_state_rejected",
        "test_invalid_job_type_rejected",
        "test_invalid_item_state_rejected",
        "test_invalid_log_level_rejected",
        "test_null_display_name_rejected",
        "test_null_job_type_rejected",
        "test_cascade_delete_removes_items_and_logs",
        "test_item_with_nonexistent_job_id_rejected",
        "test_parent_job_id_self_reference",
        "test_parent_job_id_nonexistent_rejected",
        "test_job_state_defaults_to_queued",
        "test_job_config_defaults_to_empty_json",
        "test_item_state_defaults_to_pending",
        "test_item_before_after_state_defaults",
        "test_log_level_defaults_to_info",
        "test_log_extra_defaults_to_empty_json",
        "test_unicode_display_name",
        "test_large_config_json",
        "test_negative_item_index_allowed",
        "test_all_valid_job_states_accepted",
        "test_all_valid_job_types_accepted",
        "test_all_valid_item_states_accepted",
        "test_all_valid_log_levels_accepted",
    }
)


@pytest.fixture
def alembic_cfg(tmp_path, request):
    """Create an Alembic config pointing at a temp SQLite database."""
    db_path = tmp_path / "test.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"

    cfg = Config(str(_ALEMBIC_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", async_url)

    os.environ["PULLBOX_DB_URL"] = async_url
    if request.node.originalname in _HEAD_SCHEMA_TESTS:
        request.getfixturevalue("migrated_head_template").copy_to(db_path, cfg)
    yield cfg, sync_url
    os.environ.pop("PULLBOX_DB_URL", None)


def _get_columns(sync_url: str, table: str) -> set[str]:
    """Get column names for a table using a sync engine."""
    engine = create_engine(sync_url)
    try:
        inspector = inspect(engine)
        return {col["name"] for col in inspector.get_columns(table)}
    finally:
        engine.dispose()


def _get_tables(sync_url: str) -> set[str]:
    """Get all table names."""
    engine = create_engine(sync_url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _get_indexes(sync_url: str, table: str) -> list[dict]:
    """Get indexes for a table."""
    engine = create_engine(sync_url)
    try:
        return list(inspect(engine).get_indexes(table))
    finally:
        engine.dispose()


# ── Table Creation ──────────────────────────────────────────


class TestUtilityTablesCreated:
    """Verify all three utility tables are created with correct columns."""

    def test_upgrade_creates_utility_jobs_table(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        assert _get_tables(sync_url) == set()
        command.upgrade(cfg, "head")
        assert "utility_jobs" in _get_tables(sync_url)

    def test_upgrade_creates_utility_job_items_table(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        assert _get_tables(sync_url) == set()
        command.upgrade(cfg, "head")
        assert "utility_job_items" in _get_tables(sync_url)

    def test_upgrade_creates_utility_job_logs_table(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        assert _get_tables(sync_url) == set()
        command.upgrade(cfg, "head")
        assert "utility_job_logs" in _get_tables(sync_url)

    def test_utility_jobs_has_all_columns(
        self, alembic_cfg, migrated_head_template, tmp_path
    ) -> None:
        cfg, sync_url = alembic_cfg
        # Schema-only checks start from a real migrated template, not an empty DB.
        assert "alembic_version" in _get_tables(sync_url)
        command.upgrade(cfg, "head")

        columns = _get_columns(sync_url, "utility_jobs")
        expected = {
            "id",
            "job_type",
            "display_name",
            "state",
            "config",
            "total_items",
            "completed_items",
            "failed_items",
            "skipped_items",
            "warning_count",
            "queue_position",
            "created_at",
            "started_at",
            "paused_at",
            "completed_at",
            "error_message",
            "created_by",
            "parent_job_id",
        }
        assert expected.issubset(columns), f"Missing columns: {expected - columns}"

        # Independent copies retain real migration state but never another
        # test's committed rows. Reuse must not rebuild or mutate the template.
        before = migrated_head_template.path.stat()
        caller_url = os.environ["PULLBOX_DB_URL"]
        first, second = tmp_path / "first.db", tmp_path / "second.db"
        migrated_head_template.copy_to(first, cfg)
        with closing(sqlite3.connect(first)) as connection:
            connection.execute(
                "INSERT INTO utility_jobs (id, job_type, display_name) "
                "VALUES ('isolated-copy', 'file_convert', 'First copy')"
            )
            connection.commit()
        migrated_head_template.copy_to(second, cfg)
        with closing(sqlite3.connect(second)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM utility_jobs").fetchone() == (0,)
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone()
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert first.stat().st_ino != second.stat().st_ino
        after = migrated_head_template.path.stat()
        assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
        assert os.environ["PULLBOX_DB_URL"] == caller_url
        with pytest.raises(FileExistsError):
            migrated_head_template.copy_to(second, cfg)

    def test_utility_job_items_has_all_columns(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        columns = _get_columns(sync_url, "utility_job_items")
        expected = {
            "id",
            "job_id",
            "item_index",
            "state",
            "file_path",
            "operation",
            "before_state",
            "after_state",
            "started_at",
            "completed_at",
            "duration_ms",
            "error_message",
            "warning_message",
            "worker_id",
        }
        assert expected.issubset(columns), f"Missing columns: {expected - columns}"

    def test_utility_job_logs_has_all_columns(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        columns = _get_columns(sync_url, "utility_job_logs")
        expected = {
            "id",
            "job_id",
            "item_id",
            "timestamp",
            "level",
            "message",
            "file_path",
            "extra",
        }
        assert expected.issubset(columns), f"Missing columns: {expected - columns}"


# ── Issues Integrity Columns ───────────────────────────────


class TestIssuesIntegrityColumns:
    """Verify integrity columns added to existing issues table."""

    def test_issues_has_integrity_status(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")
        assert "integrity_status" in _get_columns(sync_url, "issues")

    def test_issues_has_integrity_checked_at(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")
        assert "integrity_checked_at" in _get_columns(sync_url, "issues")

    def test_issues_has_integrity_details(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")
        assert "integrity_details" in _get_columns(sync_url, "issues")

    def test_integrity_status_defaults_to_unchecked(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                conn.execute(
                    text(
                        "INSERT INTO series (title, sort_title, status, issue_count, "
                        "monitored) "
                        "VALUES ('Test', 'Test', 'continuing', 0, 0)"
                    )
                )
                series_id = conn.execute(text("SELECT last_insert_rowid()")).scalar()
                conn.execute(
                    text(
                        "INSERT INTO issues (series_id, issue_number, status) "
                        "VALUES (:sid, 1.0, 'wanted')"
                    ),
                    {"sid": series_id},
                )
                result = conn.execute(
                    text("SELECT integrity_status FROM issues WHERE series_id = :sid"),
                    {"sid": series_id},
                ).scalar()
                assert result == "unchecked"
                conn.commit()
        finally:
            engine.dispose()

    def test_integrity_details_defaults_to_empty_json(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                conn.execute(
                    text(
                        "INSERT INTO series (title, sort_title, status, issue_count, "
                        "monitored) "
                        "VALUES ('Test', 'Test', 'continuing', 0, 0)"
                    )
                )
                series_id = conn.execute(text("SELECT last_insert_rowid()")).scalar()
                conn.execute(
                    text(
                        "INSERT INTO issues (series_id, issue_number, status) "
                        "VALUES (:sid, 1.0, 'wanted')"
                    ),
                    {"sid": series_id},
                )
                result = conn.execute(
                    text("SELECT integrity_details FROM issues WHERE series_id = :sid"),
                    {"sid": series_id},
                ).scalar()
                assert result == "{}"
                conn.commit()
        finally:
            engine.dispose()


# ── Indexes ─────────────────────────────────────────────────


class TestUtilityIndexes:
    """Verify indexes are created on key columns."""

    def test_utility_jobs_state_index(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        indexes = _get_indexes(sync_url, "utility_jobs")
        indexed_cols = [idx["column_names"] for idx in indexes]
        assert ["state"] in indexed_cols

    def test_utility_jobs_created_at_index(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        indexes = _get_indexes(sync_url, "utility_jobs")
        indexed_cols = [idx["column_names"] for idx in indexes]
        assert ["created_at"] in indexed_cols

    def test_utility_job_items_job_id_index(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        indexes = _get_indexes(sync_url, "utility_job_items")
        indexed_cols = [idx["column_names"] for idx in indexes]
        assert ["job_id"] in indexed_cols

    def test_utility_job_items_job_id_state_index(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        indexes = _get_indexes(sync_url, "utility_job_items")
        indexed_cols = [idx["column_names"] for idx in indexes]
        assert ["job_id", "state"] in indexed_cols

    def test_utility_job_items_job_id_item_index_index(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        indexes = _get_indexes(sync_url, "utility_job_items")
        indexed_cols = [idx["column_names"] for idx in indexes]
        assert ["job_id", "item_index"] in indexed_cols

    def test_utility_job_logs_job_id_index(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        indexes = _get_indexes(sync_url, "utility_job_logs")
        indexed_cols = [idx["column_names"] for idx in indexes]
        assert ["job_id"] in indexed_cols

    def test_utility_job_logs_job_id_level_index(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        indexes = _get_indexes(sync_url, "utility_job_logs")
        indexed_cols = [idx["column_names"] for idx in indexes]
        assert ["job_id", "level"] in indexed_cols

    def test_utility_job_logs_job_id_timestamp_index(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        indexes = _get_indexes(sync_url, "utility_job_logs")
        indexed_cols = [idx["column_names"] for idx in indexes]
        assert ["job_id", "timestamp"] in indexed_cols

    def test_issues_integrity_status_index(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        indexes = _get_indexes(sync_url, "issues")
        indexed_cols = [idx["column_names"] for idx in indexes]
        assert ["integrity_status"] in indexed_cols


# ── CHECK Constraints ───────────────────────────────────────


class TestCheckConstraints:
    """Verify CHECK constraints reject invalid enum values."""

    def test_invalid_job_state_rejected(self, alembic_cfg) -> None:
        """INSERT with invalid state value is rejected by CHECK constraint."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                # Enable FK enforcement for SQLite
                conn.execute(text("PRAGMA foreign_keys = ON"))
                with pytest.raises(IntegrityError):
                    conn.execute(
                        text(
                            "INSERT INTO utility_jobs (id, job_type, display_name, state, config) "
                            "VALUES ('test1', 'file_convert', 'Test', 'UNKNOWN', '{}')"
                        )
                    )
                    conn.commit()
        finally:
            engine.dispose()

    def test_invalid_job_type_rejected(self, alembic_cfg) -> None:
        """INSERT with invalid job_type is rejected by CHECK constraint."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                conn.execute(text("PRAGMA foreign_keys = ON"))
                with pytest.raises(IntegrityError):
                    conn.execute(
                        text(
                            "INSERT INTO utility_jobs (id, job_type, display_name, state, config) "
                            "VALUES ('test1', 'invalid_type', 'Test', 'QUEUED', '{}')"
                        )
                    )
                    conn.commit()
        finally:
            engine.dispose()

    def test_invalid_item_state_rejected(self, alembic_cfg) -> None:
        """INSERT with invalid item state is rejected by CHECK constraint."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                conn.execute(text("PRAGMA foreign_keys = ON"))
                # First insert a valid job
                conn.execute(
                    text(
                        "INSERT INTO utility_jobs (id, job_type, display_name, state, config) "
                        "VALUES ('job1', 'file_convert', 'Test', 'QUEUED', '{}')"
                    )
                )
                with pytest.raises(IntegrityError):
                    conn.execute(
                        text(
                            "INSERT INTO utility_job_items "
                            "(id, job_id, item_index, state, operation) "
                            "VALUES ('item1', 'job1', 0, 'INVALID', 'convert')"
                        )
                    )
                    conn.commit()
        finally:
            engine.dispose()

    def test_invalid_log_level_rejected(self, alembic_cfg) -> None:
        """INSERT with invalid log level is rejected by CHECK constraint."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                conn.execute(text("PRAGMA foreign_keys = ON"))
                conn.execute(
                    text(
                        "INSERT INTO utility_jobs (id, job_type, display_name, state, config) "
                        "VALUES ('job1', 'file_convert', 'Test', 'QUEUED', '{}')"
                    )
                )
                with pytest.raises(IntegrityError):
                    conn.execute(
                        text(
                            "INSERT INTO utility_job_logs (job_id, level, message) "
                            "VALUES ('job1', 'CRITICAL', 'test')"
                        )
                    )
                    conn.commit()
        finally:
            engine.dispose()

    def test_null_display_name_rejected(self, alembic_cfg) -> None:
        """NULL in NOT NULL column display_name is rejected."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn, pytest.raises(IntegrityError):
                conn.execute(
                    text(
                        "INSERT INTO utility_jobs (id, job_type, display_name, state, config) "
                        "VALUES ('test1', 'file_convert', NULL, 'QUEUED', '{}')"
                    )
                )
                conn.commit()
        finally:
            engine.dispose()

    def test_null_job_type_rejected(self, alembic_cfg) -> None:
        """NULL in NOT NULL column job_type is rejected."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn, pytest.raises(IntegrityError):
                conn.execute(
                    text(
                        "INSERT INTO utility_jobs (id, job_type, display_name, state, config) "
                        "VALUES ('test1', NULL, 'Test', 'QUEUED', '{}')"
                    )
                )
                conn.commit()
        finally:
            engine.dispose()


# ── FK Constraints & CASCADE ────────────────────────────────


class TestForeignKeysAndCascade:
    """Verify FK constraints and CASCADE delete behavior."""

    def test_cascade_delete_removes_items_and_logs(self, alembic_cfg) -> None:
        """Deleting a job cascades to its items and logs."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                conn.execute(text("PRAGMA foreign_keys = ON"))
                # Insert job
                conn.execute(
                    text(
                        "INSERT INTO utility_jobs (id, job_type, display_name, state, config) "
                        "VALUES ('job1', 'file_convert', 'Test Job', 'QUEUED', '{}')"
                    )
                )
                # Insert items
                conn.execute(
                    text(
                        "INSERT INTO utility_job_items "
                        "(id, job_id, item_index, state, operation) "
                        "VALUES ('item1', 'job1', 0, 'PENDING', 'convert')"
                    )
                )
                # Insert logs
                conn.execute(
                    text(
                        "INSERT INTO utility_job_logs (job_id, level, message) "
                        "VALUES ('job1', 'INFO', 'Started')"
                    )
                )
                conn.commit()

                # Delete the job
                conn.execute(text("DELETE FROM utility_jobs WHERE id = 'job1'"))
                conn.commit()

                # Verify cascaded
                items = conn.execute(
                    text("SELECT COUNT(*) FROM utility_job_items WHERE job_id = 'job1'")
                ).scalar()
                logs = conn.execute(
                    text("SELECT COUNT(*) FROM utility_job_logs WHERE job_id = 'job1'")
                ).scalar()
                assert items == 0
                assert logs == 0
        finally:
            engine.dispose()

    def test_item_with_nonexistent_job_id_rejected(self, alembic_cfg) -> None:
        """Inserting item with FK to non-existent job fails."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                conn.execute(text("PRAGMA foreign_keys = ON"))
                with pytest.raises(IntegrityError):
                    conn.execute(
                        text(
                            "INSERT INTO utility_job_items "
                            "(id, job_id, item_index, state, operation) "
                            "VALUES ('item1', 'nonexistent', 0, 'PENDING', 'convert')"
                        )
                    )
                    conn.commit()
        finally:
            engine.dispose()

    def test_parent_job_id_self_reference(self, alembic_cfg) -> None:
        """parent_job_id can reference another utility_job."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                conn.execute(text("PRAGMA foreign_keys = ON"))
                conn.execute(
                    text(
                        "INSERT INTO utility_jobs (id, job_type, display_name, state, config) "
                        "VALUES ('parent1', 'file_convert', 'Parent', 'COMPLETED', '{}')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO utility_jobs "
                        "(id, job_type, display_name, state, config, parent_job_id) "
                        "VALUES ('child1', 'rollback', 'Rollback', 'QUEUED', '{}', 'parent1')"
                    )
                )
                conn.commit()

                result = conn.execute(
                    text("SELECT parent_job_id FROM utility_jobs WHERE id = 'child1'")
                ).scalar()
                assert result == "parent1"
        finally:
            engine.dispose()

    def test_parent_job_id_nonexistent_rejected(self, alembic_cfg) -> None:
        """parent_job_id pointing to non-existent job is rejected."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                conn.execute(text("PRAGMA foreign_keys = ON"))
                with pytest.raises(IntegrityError):
                    conn.execute(
                        text(
                            "INSERT INTO utility_jobs "
                            "(id, job_type, display_name, state, config, parent_job_id) "
                            "VALUES ('child1', 'rollback', 'Rollback', 'QUEUED', '{}', 'ghost')"
                        )
                    )
                    conn.commit()
        finally:
            engine.dispose()


# ── Defaults ────────────────────────────────────────────────


class TestDefaults:
    """Verify default values are applied correctly."""

    def test_job_state_defaults_to_queued(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                conn.execute(
                    text(
                        "INSERT INTO utility_jobs (id, job_type, display_name, config) "
                        "VALUES ('test1', 'file_convert', 'Test', '{}')"
                    )
                )
                result = conn.execute(
                    text("SELECT state FROM utility_jobs WHERE id = 'test1'")
                ).scalar()
                assert result == "QUEUED"
                conn.commit()
        finally:
            engine.dispose()

    def test_job_config_defaults_to_empty_json(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                conn.execute(
                    text(
                        "INSERT INTO utility_jobs (id, job_type, display_name) "
                        "VALUES ('test1', 'file_convert', 'Test')"
                    )
                )
                result = conn.execute(
                    text("SELECT config FROM utility_jobs WHERE id = 'test1'")
                ).scalar()
                assert result == "{}"
                conn.commit()
        finally:
            engine.dispose()

    def test_item_state_defaults_to_pending(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                conn.execute(text("PRAGMA foreign_keys = ON"))
                conn.execute(
                    text(
                        "INSERT INTO utility_jobs (id, job_type, display_name, config) "
                        "VALUES ('job1', 'file_convert', 'Test', '{}')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO utility_job_items (id, job_id, item_index, operation) "
                        "VALUES ('item1', 'job1', 0, 'convert')"
                    )
                )
                result = conn.execute(
                    text("SELECT state FROM utility_job_items WHERE id = 'item1'")
                ).scalar()
                assert result == "PENDING"
                conn.commit()
        finally:
            engine.dispose()

    def test_item_before_after_state_defaults(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                conn.execute(text("PRAGMA foreign_keys = ON"))
                conn.execute(
                    text(
                        "INSERT INTO utility_jobs (id, job_type, display_name, config) "
                        "VALUES ('job1', 'file_convert', 'Test', '{}')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO utility_job_items (id, job_id, item_index, operation) "
                        "VALUES ('item1', 'job1', 0, 'convert')"
                    )
                )
                row = conn.execute(
                    text(
                        "SELECT before_state, after_state FROM utility_job_items WHERE id = 'item1'"
                    )
                ).fetchone()
                assert row is not None
                assert row[0] == "{}"
                assert row[1] == "{}"
                conn.commit()
        finally:
            engine.dispose()

    def test_log_level_defaults_to_info(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                conn.execute(text("PRAGMA foreign_keys = ON"))
                conn.execute(
                    text(
                        "INSERT INTO utility_jobs (id, job_type, display_name, config) "
                        "VALUES ('job1', 'file_convert', 'Test', '{}')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO utility_job_logs (job_id, message) VALUES ('job1', 'Started')"
                    )
                )
                result = conn.execute(
                    text("SELECT level FROM utility_job_logs WHERE job_id = 'job1'")
                ).scalar()
                assert result == "INFO"
                conn.commit()
        finally:
            engine.dispose()

    def test_log_extra_defaults_to_empty_json(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                conn.execute(text("PRAGMA foreign_keys = ON"))
                conn.execute(
                    text(
                        "INSERT INTO utility_jobs (id, job_type, display_name, config) "
                        "VALUES ('job1', 'file_convert', 'Test', '{}')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO utility_job_logs (job_id, message) VALUES ('job1', 'Started')"
                    )
                )
                result = conn.execute(
                    text("SELECT extra FROM utility_job_logs WHERE job_id = 'job1'")
                ).scalar()
                assert result == "{}"
                conn.commit()
        finally:
            engine.dispose()


# ── Edge Cases ──────────────────────────────────────────────


class TestEdgeCases:
    """Edge cases: unicode, large JSON, negative item_index, valid enum values."""

    def test_unicode_display_name(self, alembic_cfg) -> None:
        """Unicode in display_name (Japanese series names, em-dashes)."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                conn.execute(
                    text(
                        "INSERT INTO utility_jobs (id, job_type, display_name, config) "
                        "VALUES ('test1', 'file_convert', :name, '{}')"
                    ),
                    {"name": "バットマン — Mass Convert"},
                )
                result = conn.execute(
                    text("SELECT display_name FROM utility_jobs WHERE id = 'test1'")
                ).scalar()
                assert result == "バットマン — Mass Convert"
                conn.commit()
        finally:
            engine.dispose()

    def test_large_config_json(self, alembic_cfg) -> None:
        """Large config JSON blob (100KB+) persists and reads back."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        import json

        large_config = json.dumps({"files": [f"/comics/issue_{i}.cbr" for i in range(5000)]})
        assert len(large_config) > 100_000

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                conn.execute(
                    text(
                        "INSERT INTO utility_jobs (id, job_type, display_name, config) "
                        "VALUES ('test1', 'file_convert', 'Large Job', :config)"
                    ),
                    {"config": large_config},
                )
                result = conn.execute(
                    text("SELECT config FROM utility_jobs WHERE id = 'test1'")
                ).scalar()
                assert result == large_config
                conn.commit()
        finally:
            engine.dispose()

    def test_negative_item_index_allowed(self, alembic_cfg) -> None:
        """Negative item_index values are allowed (no CHECK constraint on it)."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                conn.execute(text("PRAGMA foreign_keys = ON"))
                conn.execute(
                    text(
                        "INSERT INTO utility_jobs (id, job_type, display_name, config) "
                        "VALUES ('job1', 'file_convert', 'Test', '{}')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO utility_job_items (id, job_id, item_index, operation) "
                        "VALUES ('item1', 'job1', -1, 'convert')"
                    )
                )
                result = conn.execute(
                    text("SELECT item_index FROM utility_job_items WHERE id = 'item1'")
                ).scalar()
                assert result == -1
                conn.commit()
        finally:
            engine.dispose()

    def test_all_valid_job_states_accepted(self, alembic_cfg) -> None:
        """All valid job state enum values are accepted."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        valid_states = [
            "QUEUED",
            "RUNNING",
            "PAUSING",
            "PAUSED",
            "CANCELLING",
            "CANCELLED",
            "COMPLETED",
            "FAILED",
            "ROLLING_BACK",
            "ROLLED_BACK",
        ]

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                for i, state in enumerate(valid_states):
                    conn.execute(
                        text(
                            "INSERT INTO utility_jobs "
                            "(id, job_type, display_name, state, config) "
                            "VALUES (:id, 'file_convert', 'Test', :state, '{}')"
                        ),
                        {"id": f"test_{i}", "state": state},
                    )
                conn.commit()

                count = conn.execute(text("SELECT COUNT(*) FROM utility_jobs")).scalar()
                assert count == len(valid_states)
        finally:
            engine.dispose()

    def test_all_valid_job_types_accepted(self, alembic_cfg) -> None:
        """All valid job type enum values are accepted."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        valid_types = [
            "file_convert",
            "mass_convert_pipeline",
            "mass_rename",
            "db_check_cleanup",
            "export_library",
            "integrity_check",
            "library_permissions",
            "rollback",
        ]

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                for i, jtype in enumerate(valid_types):
                    conn.execute(
                        text(
                            "INSERT INTO utility_jobs "
                            "(id, job_type, display_name, state, config) "
                            "VALUES (:id, :jtype, 'Test', 'QUEUED', '{}')"
                        ),
                        {"id": f"test_{i}", "jtype": jtype},
                    )
                conn.commit()

                count = conn.execute(text("SELECT COUNT(*) FROM utility_jobs")).scalar()
                assert count == len(valid_types)
        finally:
            engine.dispose()

    def test_all_valid_item_states_accepted(self, alembic_cfg) -> None:
        """All valid item state enum values are accepted."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        valid_states = [
            "PENDING",
            "IN_PROGRESS",
            "COMPLETED",
            "FAILED",
            "SKIPPED",
            "ROLLED_BACK",
            "ROLLBACK_FAILED",
        ]

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                conn.execute(text("PRAGMA foreign_keys = ON"))
                conn.execute(
                    text(
                        "INSERT INTO utility_jobs (id, job_type, display_name, config) "
                        "VALUES ('job1', 'file_convert', 'Test', '{}')"
                    )
                )
                for i, state in enumerate(valid_states):
                    conn.execute(
                        text(
                            "INSERT INTO utility_job_items "
                            "(id, job_id, item_index, state, operation) "
                            "VALUES (:id, 'job1', :idx, :state, 'convert')"
                        ),
                        {"id": f"item_{i}", "idx": i, "state": state},
                    )
                conn.commit()

                count = conn.execute(
                    text("SELECT COUNT(*) FROM utility_job_items WHERE job_id = 'job1'")
                ).scalar()
                assert count == len(valid_states)
        finally:
            engine.dispose()

    def test_all_valid_log_levels_accepted(self, alembic_cfg) -> None:
        """All valid log level enum values are accepted."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR"]

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                conn.execute(text("PRAGMA foreign_keys = ON"))
                conn.execute(
                    text(
                        "INSERT INTO utility_jobs (id, job_type, display_name, config) "
                        "VALUES ('job1', 'file_convert', 'Test', '{}')"
                    )
                )
                for level in valid_levels:
                    conn.execute(
                        text(
                            "INSERT INTO utility_job_logs (job_id, level, message) "
                            "VALUES ('job1', :level, 'test')"
                        ),
                        {"level": level},
                    )
                conn.commit()

                count = conn.execute(
                    text("SELECT COUNT(*) FROM utility_job_logs WHERE job_id = 'job1'")
                ).scalar()
                assert count == len(valid_levels)
        finally:
            engine.dispose()


# ── Downgrade ───────────────────────────────────────────────


class TestDowngrade:
    """Verify migration downgrades cleanly."""

    def test_downgrade_removes_utility_tables(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, _UTILITY_TABLES_REV)
        command.downgrade(cfg, "-1")

        tables = _get_tables(sync_url)
        assert "utility_jobs" not in tables
        assert "utility_job_items" not in tables
        assert "utility_job_logs" not in tables

    def test_downgrade_removes_integrity_columns(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, _UTILITY_TABLES_REV)
        command.downgrade(cfg, "-1")

        columns = _get_columns(sync_url, "issues")
        assert "integrity_status" not in columns
        assert "integrity_checked_at" not in columns
        assert "integrity_details" not in columns

    def test_downgrade_after_data_inserted(self, alembic_cfg) -> None:
        """Downgrade works even when utility tables have data."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, _UTILITY_TABLES_REV)

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                conn.execute(
                    text(
                        "INSERT INTO utility_jobs (id, job_type, display_name, config) "
                        "VALUES ('job1', 'file_convert', 'Test', '{}')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO utility_job_items "
                        "(id, job_id, item_index, operation) "
                        "VALUES ('item1', 'job1', 0, 'convert')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO utility_job_logs (job_id, level, message) "
                        "VALUES ('job1', 'INFO', 'test')"
                    )
                )
                conn.commit()
        finally:
            engine.dispose()

        # Downgrade should succeed even with data
        command.downgrade(cfg, "-1")

        tables = _get_tables(sync_url)
        assert "utility_jobs" not in tables

    def test_downgrade_then_upgrade_roundtrip(self, alembic_cfg) -> None:
        """Migration can be downgraded and re-applied cleanly."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, _UTILITY_TABLES_REV)
        command.downgrade(cfg, "-1")
        command.upgrade(cfg, _UTILITY_TABLES_REV)

        tables = _get_tables(sync_url)
        assert "utility_jobs" in tables
        assert "utility_job_items" in tables
        assert "utility_job_logs" in tables

        columns = _get_columns(sync_url, "issues")
        assert "integrity_status" in columns
