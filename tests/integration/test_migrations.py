"""Tests for Alembic migration chain — verify migrations apply cleanly.

Runs the full migration chain against a fresh SQLite database file
and verifies that Phase 2 columns and config keys exist.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from alembic import command

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

# Path to the alembic directory (relative to this test file)
_ALEMBIC_DIR = Path(__file__).resolve().parent.parent.parent / "alembic"
_DIRECT_ACQUISITION_PARENT_REVISION = "d9f0a1b2c345"
_AIRDCPP_FOUNDATION_PARENT_REVISION = "q2r3s4t5u678"
_AIRDCPP_SETTINGS_PARENT_REVISION = "r3s4t5u6v789"


@pytest.fixture
def alembic_cfg(tmp_path):
    """Create an Alembic config pointing at a temp SQLite database.

    Uses the async driver for Alembic (which uses async_engine_from_config)
    and the sync driver for inspection queries.
    """
    db_path = tmp_path / "test.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"

    cfg = Config(str(_ALEMBIC_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", async_url)

    # Set the env var that env.py reads
    os.environ["PULLBOX_DB_URL"] = async_url
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


def _seed_reader_state_owner(conn: Connection, *, slug: str) -> tuple[int, int]:
    conn.execute(
        text(
            "INSERT INTO users (username, password_hash, is_active) "
            "VALUES (:username, 'not-used', 1)"
        ),
        {"username": f"reader-{slug}"},
    )
    user_id = conn.execute(text("SELECT last_insert_rowid()")).scalar_one()
    conn.execute(
        text(
            "INSERT INTO series "
            "(title, sort_title, status, issue_count, monitored, series_type, "
            "alternate_names, created_at, updated_at) "
            "VALUES (:title, :sort_title, 'CONTINUING', 1, 1, "
            "'STANDARD', '[]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {
            "title": f"Reader {slug.title()}",
            "sort_title": f"reader {slug}",
        },
    )
    series_id = conn.execute(text("SELECT last_insert_rowid()")).scalar_one()
    conn.execute(
        text(
            "INSERT INTO issues "
            "(series_id, issue_number, status, issue_type, manual_skip, "
            "created_at, updated_at) "
            "VALUES (:series_id, 1.0, 'OWNED', 'ISSUE', 0, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {"series_id": series_id},
    )
    issue_id = conn.execute(text("SELECT last_insert_rowid()")).scalar_one()
    return user_id, issue_id


def _get_indexes(sync_url: str, table: str) -> dict[str, list[str]]:
    """Get index names and ordered columns for a table."""
    engine = create_engine(sync_url)
    try:
        inspector = inspect(engine)
        return {
            index["name"]: list(index["column_names"]) for index in inspector.get_indexes(table)
        }
    finally:
        engine.dispose()


def _insert_download_history_issue(conn) -> None:
    """Seed the minimum valid parent rows for download-history fixtures."""
    conn.execute(
        text(
            "INSERT INTO series "
            "(id, title, sort_title, status, issue_count, monitored) "
            "VALUES (1, 'Migration Fixture', 'migration fixture', 'UNKNOWN', 0, 0)"
        )
    )
    conn.execute(
        text("INSERT INTO issues (id, series_id, issue_number, status) VALUES (1, 1, 1, 'UNKNOWN')")
    )


class TestMigrationChain:
    """Verify the full Alembic migration chain applies cleanly."""

    def test_upgrade_to_head(self, alembic_cfg) -> None:
        """All migrations apply without error."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            tables = inspect(engine).get_table_names()
            assert "series" in tables
            assert "issues" in tables
            assert "library_roots" in tables
            assert "system_config" in tables
        finally:
            engine.dispose()

    def test_airdcpp_foundation_backfills_populated_history_and_source_order(
        self,
        alembic_cfg,
    ) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, _AIRDCPP_FOUNDATION_PARENT_REVISION)

        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                _insert_download_history_issue(conn)
                conn.execute(
                    text(
                        "INSERT INTO download_history "
                        "(issue_id, title, download_url, download_client, state) VALUES "
                        "(1, 'Usenet', 'https://example.test/nzb', 'SABNZBD', 'QUEUED'), "
                        "(1, 'Torrent', 'https://example.test/torrent', "
                        "'QBITTORRENT', 'QUEUED'), "
                        "(1, 'Direct', 'pullbox-direct://attempt/1', 'DIRECT', 'QUEUED')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO system_config (key, value, value_type) VALUES "
                        '(\'source_priority\', \'["direct", "torrent", "usenet"]\', '
                        "'string')"
                    )
                )
        finally:
            engine.dispose()

        command.upgrade(cfg, "head")

        columns = _get_columns(sync_url, "download_history")
        assert {"protocol", "download_client_config_id"}.issubset(columns)
        indexes = _get_indexes(sync_url, "download_history")
        assert indexes["ix_download_history_protocol_state"] == ["protocol", "state"]
        assert indexes["ix_download_history_client_config_state"] == [
            "download_client_config_id",
            "state",
        ]
        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT download_client, protocol, download_client_config_id "
                        "FROM download_history ORDER BY id"
                    )
                ).fetchall()
                source_priority = conn.execute(
                    text("SELECT value FROM system_config WHERE key = 'source_priority'")
                ).scalar_one()
        finally:
            engine.dispose()

        assert [tuple(row) for row in rows] == [
            ("SABNZBD", "usenet", None),
            ("QBITTORRENT", "torrent", None),
            ("DIRECT", "direct", None),
        ]
        assert json.loads(source_priority) == ["direct", "torrent", "usenet", "dc"]

        command.downgrade(cfg, _AIRDCPP_FOUNDATION_PARENT_REVISION)

        columns = _get_columns(sync_url, "download_history")
        assert "protocol" not in columns
        assert "download_client_config_id" not in columns
        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                clients = conn.execute(
                    text("SELECT download_client FROM download_history ORDER BY id")
                ).scalars()
                source_priority = conn.execute(
                    text("SELECT value FROM system_config WHERE key = 'source_priority'")
                ).scalar_one()
                assert list(clients) == ["SABNZBD", "QBITTORRENT", "DIRECT"]
        finally:
            engine.dispose()

        assert json.loads(source_priority) == ["direct", "torrent", "usenet"]

    def test_airdcpp_foundation_downgrade_refuses_dc_history(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                _insert_download_history_issue(conn)
                conn.execute(
                    text(
                        "INSERT INTO download_history "
                        "(issue_id, title, download_url, download_client, protocol, state) "
                        "VALUES (1, 'DC', 'airdcpp://result/opaque', "
                        "'AIRDCPP', 'dc', 'QUEUED')"
                    )
                )
        finally:
            engine.dispose()

        with pytest.raises(RuntimeError, match="AirDC\\+\\+ download history"):
            command.downgrade(cfg, _AIRDCPP_FOUNDATION_PARENT_REVISION)

    def test_airdcpp_settings_migration_is_bounded_and_does_not_seed_rows(
        self,
        alembic_cfg,
    ) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, _AIRDCPP_SETTINGS_PARENT_REVISION)
        engine = create_engine(sync_url)
        try:
            assert "airdcpp_client_settings" not in inspect(engine).get_table_names()
        finally:
            engine.dispose()

        command.upgrade(cfg, "head")
        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                assert (
                    conn.execute(text("SELECT COUNT(*) FROM airdcpp_client_settings")).scalar_one()
                    == 0
                )
                conn.execute(
                    text(
                        "INSERT INTO download_client_configs "
                        "(id, name, client_type, url, enabled, priority) "
                        "VALUES (1, 'AirDC++', 'AIRDCPP', "
                        "'http://airdcpp.test:5600', 1, 50)"
                    )
                )
                conn.execute(
                    text("INSERT INTO airdcpp_client_settings (client_config_id) VALUES (1)")
                )

            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT search_enabled, automatic_search_enabled, "
                        "minimum_search_interval_seconds, manual_collection_seconds, "
                        "automatic_collection_seconds, max_results, max_retained_routes, "
                        "max_concurrent_searches, request_timeout_seconds, "
                        "search_dispatch_deadline_seconds, reconciliation_interval_seconds, "
                        "hub_allowlist, queue_priority, next_search_allowed_at "
                        "FROM airdcpp_client_settings"
                    )
                ).one()
            assert tuple(row) == (
                1,
                0,
                45,
                8,
                15,
                200,
                400,
                1,
                15,
                45,
                30,
                "[]",
                None,
                None,
            )

            with (
                pytest.raises(IntegrityError, match="minimum_search_interval"),
                engine.begin() as conn,
            ):
                conn.execute(
                    text(
                        "INSERT INTO download_client_configs "
                        "(id, name, client_type, url, enabled, priority) "
                        "VALUES (2, 'Unsafe AirDC++', 'AIRDCPP', "
                        "'http://unsafe.test:5600', 1, 50)"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO airdcpp_client_settings "
                        "(client_config_id, minimum_search_interval_seconds) "
                        "VALUES (2, 44)"
                    )
                )
        finally:
            engine.dispose()

        command.downgrade(cfg, _AIRDCPP_SETTINGS_PARENT_REVISION)
        engine = create_engine(sync_url)
        try:
            assert "airdcpp_client_settings" not in inspect(engine).get_table_names()
        finally:
            engine.dispose()

    def test_indexer_manager_migration_backfills_prowlarr_identity(
        self,
        alembic_cfg,
    ) -> None:
        """Generic manager identity preserves existing Prowlarr rows."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "i4e5f6g70829")

        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO indexer_configs "
                        "(name, indexer_type, url, api_key, enabled, priority, "
                        "failure_count, source, prowlarr_indexer_id) "
                        "VALUES ('1337x (Prowlarr)', 'TORZNAB', 'http://prowlarr/7', "
                        "'encrypted', 1, 50, 0, 'prowlarr', 7)"
                    )
                )
        finally:
            engine.dispose()

        command.upgrade(cfg, "head")

        columns = _get_columns(sync_url, "indexer_configs")
        assert {"manager_indexer_id", "manager_available"}.issubset(columns)
        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT manager_indexer_id, manager_available "
                        "FROM indexer_configs WHERE source = 'prowlarr'"
                    )
                ).one()
            assert tuple(row) == ("7", 1)
        finally:
            engine.dispose()

    def test_series_has_path_column(self, alembic_cfg) -> None:
        """Phase 2 migration adds path column to series."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        columns = _get_columns(sync_url, "series")
        assert "path" in columns
        assert "library_root_id" in columns

    def test_series_has_issue_catalog_checked_timestamp(self, alembic_cfg) -> None:
        """Issue catalog scheduling tracks checks separately from full syncs."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        columns = _get_columns(sync_url, "series")
        assert "issue_catalog_last_checked_at" in columns

    def test_issues_has_issue_type_column(self, alembic_cfg) -> None:
        """Phase 2 migration adds issue_type column to issues."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        columns = _get_columns(sync_url, "issues")
        assert "issue_type" in columns

    def test_issue_type_default_value(self, alembic_cfg) -> None:
        """issue_type column defaults to 'issue'."""
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
                    text("SELECT issue_type FROM issues WHERE series_id = :sid"), {"sid": series_id}
                ).scalar()
                assert result == "issue"
                conn.commit()
        finally:
            engine.dispose()

    def test_naming_config_keys_seeded(self, alembic_cfg) -> None:
        """Phase 2 migration seeds the naming configuration keys."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        expected_keys = {
            "series_folder_template",
            "comic_file_template",
            "annual_file_template",
            "non_standard_file_template",
            "rename_on_import",
            "replace_illegal_characters",
            "colon_replacement",
            "create_empty_series_folders",
            "delete_empty_folders",
        }

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                result = conn.execute(text("SELECT key FROM system_config"))
                keys = {row[0] for row in result.fetchall()}
        finally:
            engine.dispose()

        assert expected_keys.issubset(keys), f"Missing keys: {expected_keys - keys}"

    def test_series_library_root_fk(self, alembic_cfg) -> None:
        """library_root_id FK on series references library_roots."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            fks = inspect(engine).get_foreign_keys("series")
            root_fk = [fk for fk in fks if "library_root_id" in fk["constrained_columns"]]
            assert len(root_fk) == 1
            assert root_fk[0]["referred_table"] == "library_roots"
        finally:
            engine.dispose()

    def test_downgrade_then_upgrade(self, alembic_cfg) -> None:
        """Migration can be downgraded and re-applied."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "-1")
        command.upgrade(cfg, "head")

        columns = _get_columns(sync_url, "series")
        assert "path" in columns
        assert "library_root_id" in columns

    def test_reader_state_migration_backfills_dimension_clocks_and_defaults(
        self,
        alembic_cfg,
    ) -> None:
        """Existing reader progress gains independent semantic clocks."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "o0p1q2r3s456")

        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                user_id, issue_id = _seed_reader_state_owner(conn, slug="migration")
                conn.execute(
                    text(
                        "INSERT INTO issue_reader_states "
                        "(user_id, issue_id, last_page_index, content_revision, page_count, "
                        "completed_at, created_at, updated_at) "
                        "VALUES (:user_id, :issue_id, 4, 'revision-a', 5, "
                        "'2026-08-20 12:00:00', '2026-08-19 11:00:00', "
                        "'2026-08-21 13:00:00')"
                    ),
                    {"user_id": user_id, "issue_id": issue_id},
                )
        finally:
            engine.dispose()

        command.upgrade(cfg, "head")

        columns = _get_columns(sync_url, "issue_reader_states")
        assert {
            "progress_updated_at",
            "last_opened_at",
            "completion_updated_at",
            "want_to_read",
            "want_to_read_updated_at",
            "state_version",
        }.issubset(columns)
        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT progress_updated_at, last_opened_at, completed_at, "
                        "completion_updated_at, want_to_read, want_to_read_updated_at, "
                        "state_version FROM issue_reader_states"
                    )
                ).one()
            assert row.progress_updated_at == row.last_opened_at
            assert row.progress_updated_at == "2026-08-21 13:00:00"
            assert row.completion_updated_at == row.completed_at
            assert row.want_to_read == 0
            assert row.want_to_read_updated_at is None
            assert row.state_version == 1
        finally:
            engine.dispose()

    def test_reader_state_migration_downgrade_discards_intent_only_rows(
        self,
        alembic_cfg,
    ) -> None:
        """Downgrade explicitly drops rows the previous schema cannot represent."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                user_id, issue_id = _seed_reader_state_owner(conn, slug="intent")
                conn.execute(
                    text(
                        "INSERT INTO issue_reader_states "
                        "(user_id, issue_id, want_to_read, want_to_read_updated_at) "
                        "VALUES (:user_id, :issue_id, 1, CURRENT_TIMESTAMP)"
                    ),
                    {"user_id": user_id, "issue_id": issue_id},
                )
        finally:
            engine.dispose()

        command.downgrade(cfg, "o0p1q2r3s456")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                remaining = conn.execute(
                    text("SELECT COUNT(*) FROM issue_reader_states")
                ).scalar_one()
            assert remaining == 0
        finally:
            engine.dispose()
        engine = create_engine(sync_url)
        try:
            columns = inspect(engine).get_columns("issue_reader_states")
        finally:
            engine.dispose()
        progress_columns = {
            column["name"]: column["nullable"]
            for column in columns
            if column["name"] in {"last_page_index", "content_revision", "page_count"}
        }
        assert progress_columns == {
            "last_page_index": False,
            "content_revision": False,
            "page_count": False,
        }

    def test_reader_query_join_index_round_trip(self, alembic_cfg) -> None:
        """The measured library-file issue join index upgrades and downgrades cleanly."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            indexes = {index["name"] for index in inspect(engine).get_indexes("library_files")}
        finally:
            engine.dispose()
        assert "ix_library_files_issue" in indexes

        command.downgrade(cfg, "p1q2r3s4t567")
        engine = create_engine(sync_url)
        try:
            indexes = {index["name"] for index in inspect(engine).get_indexes("library_files")}
        finally:
            engine.dispose()
        assert "ix_library_files_issue" not in indexes

        command.upgrade(cfg, "head")

    def test_import_jobs_has_materialization_audit_fields(self, alembic_cfg) -> None:
        """Import jobs record the effective materialization policy for auditability."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        columns = _get_columns(sync_url, "import_jobs")

        assert "torrent_import_strategy" in columns
        assert "effective_import_strategy" in columns
        assert "effective_transfer_method" in columns
        assert "source_preserved" in columns
        assert "ingest_policy_snapshot" in columns

    def test_library_files_has_naming_snapshot(self, alembic_cfg) -> None:
        """Library files keep the naming inputs used at placement time."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        columns = _get_columns(sync_url, "library_files")

        assert "naming_snapshot" in columns

    def test_naming_snapshot_migration_backfills_series_path(self, alembic_cfg) -> None:
        """Existing library files materialize missing series folder paths."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "g9h0i1j2k345")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                conn.execute(
                    text(
                        "INSERT INTO library_roots "
                        "(name, path, enabled, created_at, updated_at) "
                        "VALUES ('Comics', '/library', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )
                root_id = conn.execute(text("SELECT last_insert_rowid()")).scalar()
                conn.execute(
                    text(
                        "INSERT INTO series "
                        "(comicvine_id, title, sort_title, year_start, status, issue_count, "
                        "monitored, series_type, alternate_names, created_at, updated_at) "
                        "VALUES (123, 'Batman', 'Batman', 2024, 'CONTINUING', 1, 0, "
                        "'STANDARD', '[]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )
                series_id = conn.execute(text("SELECT last_insert_rowid()")).scalar()
                conn.execute(
                    text(
                        "INSERT INTO issues "
                        "(series_id, issue_number, status, issue_type, manual_skip, "
                        "created_at, updated_at) "
                        "VALUES (:series_id, 1.0, 'OWNED', 'ISSUE', 0, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {"series_id": series_id},
                )
                issue_id = conn.execute(text("SELECT last_insert_rowid()")).scalar()
                conn.execute(
                    text(
                        "INSERT INTO library_files "
                        "(file_path, file_name, file_size, file_format, file_modified_at, "
                        "match_confidence, issue_id, library_root_id, has_comicinfo, "
                        "created_at, updated_at) "
                        "VALUES ('/library/Batman (2024)/Batman (2024) #001.cbz', "
                        "'Batman (2024) #001.cbz', 100, 'CBZ', CURRENT_TIMESTAMP, "
                        "'HIGH', :issue_id, :root_id, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {"issue_id": issue_id, "root_id": root_id},
                )
                conn.commit()
        finally:
            engine.dispose()

        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT path, library_root_id FROM series WHERE title = 'Batman'")
                ).one()
        finally:
            engine.dispose()

        assert row[0] == "/library/Batman (2024)"
        assert row[1] == root_id

    def test_whats_new_release_cache_table(self, alembic_cfg) -> None:
        """What's New release payloads are cached in a dedicated table."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        columns = _get_columns(sync_url, "whats_new_release_cache")

        assert {
            "id",
            "cache_key",
            "cache_kind",
            "store_date",
            "publisher",
            "payload",
            "fetched_at",
            "last_successful_refresh_at",
            "created_at",
            "updated_at",
        }.issubset(columns)

        engine = create_engine(sync_url)
        try:
            inspector = inspect(engine)
            indexes = {index["name"] for index in inspector.get_indexes("whats_new_release_cache")}
            unique_constraints = {
                constraint["name"]
                for constraint in inspector.get_unique_constraints("whats_new_release_cache")
            }
        finally:
            engine.dispose()

        assert "uq_whats_new_release_cache_key" in unique_constraints
        assert "ix_whats_new_release_cache_kind" in indexes
        assert "ix_whats_new_release_cache_fetched_at" in indexes
        assert "ix_whats_new_release_cache_last_success" in indexes

    def test_usage_stats_instance_id_config_seeded(self, alembic_cfg) -> None:
        """Telemetry instance ID config exists but starts empty."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT value, value_type FROM system_config "
                        "WHERE key = 'usage_stats_instance_id'"
                    )
                ).one()
        finally:
            engine.dispose()

        assert row[0] == ""
        assert row[1] == "string"

    def test_upgrade_backfills_year_end_for_ended_series(self, alembic_cfg) -> None:
        """Ended series with a missing year_end are repaired on upgrade."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "u6p7q8r9s012")

        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO series "
                        "(id, title, sort_title, year_start, year_end, status, issue_count, "
                        "monitored, series_type) "
                        "VALUES "
                        "(1, 'Thanos: The Infinity Revelation', 'Thanos: The Infinity Revelation', "
                        "2014, NULL, 'ENDED', 1, 0, 'HARDCOVER'), "
                        "(2, 'Fallback Hardcover', 'Fallback Hardcover', "
                        "2020, NULL, 'ENDED', 0, 0, 'HARDCOVER')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO issues "
                        "(series_id, issue_number, release_date, status, issue_type) "
                        "VALUES (1, 1.0, '2014-08-27', 'OWNED', 'GN')"
                    )
                )

            command.upgrade(cfg, "head")

            with engine.connect() as conn:
                rows = conn.execute(
                    text("SELECT id, year_end FROM series WHERE id IN (1, 2) ORDER BY id")
                ).fetchall()
        finally:
            engine.dispose()

        assert rows[0][1] == 2014
        assert rows[1][1] == 2020

    def test_direct_acquisition_tables_are_created_empty_with_recovery_indexes(
        self, alembic_cfg
    ) -> None:
        """Direct acquisition persistence is dormant, indexed, and empty after upgrade."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        expected_indexes = {
            "direct_resolver_configs": set(),
            "direct_provider_configs": {
                "ix_direct_provider_configs_state",
                "ix_direct_provider_configs_enabled_priority",
            },
            "direct_host_configs": {
                "ix_direct_host_configs_account_state",
                "ix_direct_host_configs_enabled_preference",
            },
            "direct_acquisition_attempts": {
                "ix_direct_acquisition_attempts_state_retry",
                "ix_direct_acquisition_attempts_issue_created",
                "ix_direct_acquisition_attempts_provider_state",
            },
            "direct_artifact_attempts": {
                "ix_direct_artifact_attempts_acquisition_sequence",
                "ix_direct_artifact_attempts_state_retry",
                "ix_direct_artifact_attempts_host_state",
            },
        }

        engine = create_engine(sync_url)
        try:
            inspector = inspect(engine)
            tables = set(inspector.get_table_names())
            assert set(expected_indexes).issubset(tables)

            with engine.connect() as conn:
                metadata = MetaData()
                for table, required_indexes in expected_indexes.items():
                    indexes = {index["name"] for index in inspector.get_indexes(table)}
                    assert required_indexes.issubset(indexes)
                    reflected = Table(table, metadata, autoload_with=conn)
                    count = conn.execute(select(func.count()).select_from(reflected)).scalar_one()
                    assert count == 0
        finally:
            engine.dispose()

    def test_direct_failure_constraints_include_route_safety(self, alembic_cfg) -> None:
        """Unsafe routes persist separately from hard-stop content safety failures."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            inspector = inspect(engine)
            for table in ("direct_acquisition_attempts", "direct_artifact_attempts"):
                constraints = {
                    item["name"]: item["sqltext"] for item in inspector.get_check_constraints(table)
                }
                assert "unsafe_route" in constraints["directartifactfailureclass"]
        finally:
            engine.dispose()

    def test_direct_route_safety_migration_reclassifies_known_url_failures(
        self,
        alembic_cfg,
    ) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "l7m8n9o0p123")
        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO series "
                        "(id, title, sort_title, status, issue_count, monitored) "
                        "VALUES (9901, 'Route Safety', 'Route Safety', 'continuing', 1, 1)"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO issues (id, series_id, issue_number, status) "
                        "VALUES (9901, 9901, '1', 'wanted')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO direct_acquisition_attempts "
                        "(id, request_key, issue_id, provider_identity, provider_candidate_id, "
                        "failure_class, failure_code) VALUES "
                        "(9901, 'route-safety-migration', 9901, 'test', 'candidate', "
                        "'safety', 'unsafe_artifact_url')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO direct_artifact_attempts "
                        "(id, acquisition_attempt_id, sequence_no, artifact_identity, "
                        "route_kind, host_kind, failure_class, failure_code) VALUES "
                        "(9901, 9901, 0, 'route:test', 'direct', 'generic_https', "
                        "'safety', 'unsafe_artifact_url')"
                    )
                )
        finally:
            engine.dispose()

        command.upgrade(cfg, "head")
        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                acquisition = conn.execute(
                    text("SELECT failure_class FROM direct_acquisition_attempts WHERE id = 9901")
                ).scalar_one()
                artifact = conn.execute(
                    text("SELECT failure_class FROM direct_artifact_attempts WHERE id = 9901")
                ).scalar_one()
            assert acquisition == "unsafe_route"
            assert artifact == "unsafe_route"
        finally:
            engine.dispose()

        command.downgrade(cfg, "l7m8n9o0p123")
        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                acquisition = conn.execute(
                    text("SELECT failure_class FROM direct_acquisition_attempts WHERE id = 9901")
                ).scalar_one()
                artifact = conn.execute(
                    text("SELECT failure_class FROM direct_artifact_attempts WHERE id = 9901")
                ).scalar_one()
            assert acquisition == "safety"
            assert artifact == "safety"
        finally:
            engine.dispose()

    def test_direct_host_reachability_migration_backfills_existing_status(
        self,
        alembic_cfg,
    ) -> None:
        """Existing account checks become conservative reachability history."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "j5f6g7h81930")

        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO direct_host_configs "
                        "(host_kind, account_state, last_tested_at) VALUES "
                        "('pixeldrain', 'healthy', '2026-07-30 12:00:00'), "
                        "('terabox', 'authentication_required', '2026-07-30 13:00:00')"
                    )
                )
        finally:
            engine.dispose()

        command.upgrade(cfg, "head")

        columns = _get_columns(sync_url, "direct_host_configs")
        assert {
            "reachability_state",
            "last_reachable_at",
            "last_operational_result",
            "last_operational_at",
        }.issubset(columns)

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT host_kind, reachability_state, last_reachable_at, "
                        "last_operational_result, last_operational_at "
                        "FROM direct_host_configs ORDER BY host_kind"
                    )
                ).fetchall()
        finally:
            engine.dispose()

        assert tuple(rows[0]) == (
            "pixeldrain",
            "reachable",
            "2026-07-30 12:00:00",
            None,
            None,
        )
        assert tuple(rows[1]) == (
            "terabox",
            "authentication_required",
            None,
            None,
            None,
        )

    def test_direct_acquisition_migration_downgrades_and_reapplies(self, alembic_cfg) -> None:
        """The dormant direct-download schema can be removed and recreated safely."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")
        command.downgrade(cfg, _DIRECT_ACQUISITION_PARENT_REVISION)

        engine = create_engine(sync_url)
        try:
            tables = set(inspect(engine).get_table_names())
        finally:
            engine.dispose()

        assert "direct_provider_configs" not in tables
        assert "direct_host_configs" not in tables
        assert "direct_acquisition_attempts" not in tables
        assert "direct_artifact_attempts" not in tables

        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            tables = set(inspect(engine).get_table_names())
        finally:
            engine.dispose()

        assert {
            "direct_resolver_configs",
            "direct_provider_configs",
            "direct_host_configs",
            "direct_acquisition_attempts",
            "direct_artifact_attempts",
        }.issubset(tables)

    def test_direct_resolver_migration_has_bounded_secret_configuration(self, alembic_cfg) -> None:
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            inspector = inspect(engine)
            columns = {
                item["name"]: item for item in inspector.get_columns("direct_resolver_configs")
            }
            unique_constraints = {
                item["name"] for item in inspector.get_unique_constraints("direct_resolver_configs")
            }
            check_constraints = {
                item["name"] for item in inspector.get_check_constraints("direct_resolver_configs")
            }
        finally:
            engine.dispose()

        assert columns["encrypted_auth_headers"]["nullable"] is False
        assert columns["resolver_kind"]["nullable"] is False
        assert columns["priority"]["nullable"] is False
        assert columns["timeout_seconds"]["nullable"] is False
        assert columns["max_concurrency"]["nullable"] is False
        assert "uq_direct_resolver_name" in unique_constraints
        assert "uq_direct_resolver_kind" in unique_constraints
        assert {
            "ck_direct_resolver_priority",
            "ck_direct_resolver_timeout",
            "ck_direct_resolver_concurrency",
        }.issubset(check_constraints)
