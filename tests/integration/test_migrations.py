"""Tests for Alembic migration chain — verify migrations apply cleanly.

Runs the full migration chain against a fresh SQLite database file
and verifies that Phase 2 columns and config keys exist.
"""

from __future__ import annotations

import json
from itertools import product
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import sqlalchemy.ext.asyncio as sqlalchemy_asyncio
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, Table, create_engine, event, func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from alembic import command

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

# Path to the alembic directory (relative to this test file)
_ALEMBIC_DIR = Path(__file__).resolve().parent.parent.parent / "alembic"
_DIRECT_ACQUISITION_PARENT_REVISION = "d9f0a1b2c345"
_AIRDCPP_FOUNDATION_PARENT_REVISION = "q2r3s4t5u678"
_AIRDCPP_SETTINGS_PARENT_REVISION = "r3s4t5u6v789"
_ISSUE_NUMBER_TEXT_PARENT_REVISION = "y0z1a2b3c456"
_STORY_ARC_DOMAIN_PARENT_REVISION = "z1a2b3c4d567"
_STORY_ARC_DOMAIN_REVISION = "a2b3c4d5e678"
_STORY_ARC_MANAGED_PLACEMENT_REVISION = "b3c4d5e6f789"
_STORY_ARC_IMPORT_SYNC_REVISION = "c4d5e6f7a890"
_STORY_ARC_IMPORT_INTENT_REVISION = "d5e6f7a8b901"
_STORY_ARC_COVER_REVISION = "f7a8b9c0d123"
_MYLAR_PATH_CONFIRMATION_REVISION = "g8b9c0d1e234"
_LIBRARY_ROOT_MANAGEMENT_REVISION = "h9c0d1e2f345"
_SERIES_PREFERRED_ROOT_REVISION = "i0d1e2f3a456"
_IMPORT_FILE_MATCHED_ISSUE_INDEX_REVISION = "j1e2f3a4b567"
_IMPORT_FILE_SERIES_INDEX_REVISION = "k2f3a4b5c678"
_IMPORT_FILE_DELETE_REFERENCE_INDEX_REVISION = "l3f4a5b6c789"
_IMPORT_JOB_ARCHIVE_REVISION = "m4g5h6i7j890"


@pytest.fixture
def alembic_cfg(tmp_path, monkeypatch):
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

    original_engine_factory = sqlalchemy_asyncio.async_engine_from_config

    def migration_engine_from_config(*args, **kwargs):
        engine = original_engine_factory(*args, **kwargs)
        if engine.dialect.name == "sqlite" and engine.url.database == str(db_path):
            # Runtime imports install a process-global FK-ON listener. The real
            # app/dev migration entrypoints use fresh Alembic subprocesses, where
            # that listener is absent and SQLite batch rebuilds run with FK OFF.
            # Match that connection policy only for this fixture's migration
            # engine; normal inspection/runtime connections retain enforcement.
            def use_cli_connection_pragmas(dbapi_connection, _connection_record):
                cursor = dbapi_connection.cursor()
                try:
                    cursor.execute("PRAGMA foreign_keys=OFF")
                finally:
                    cursor.close()

            event.listen(engine.sync_engine, "connect", use_cli_connection_pragmas)
        return engine

    monkeypatch.setattr(
        sqlalchemy_asyncio,
        "async_engine_from_config",
        migration_engine_from_config,
    )
    monkeypatch.setenv("PULLBOX_DB_URL", async_url)
    yield cfg, sync_url


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


def _seed_minimal_import_job(conn: Connection, *, source_path: str) -> int:
    """Seed an import job that is valid at the story-arc parent revision."""
    conn.execute(
        text(
            "INSERT INTO import_jobs ("
            "source_path, source_type, status, scan_total_files, scan_total_dirs, "
            "series_found, series_duplicate, series_matched, series_no_match, series_new, "
            "series_imported, series_failed, search_on_add, cv_match_threshold, "
            "auto_accept_high_confidence, skip_no_match) VALUES ("
            ":source_path, 'filesystem', 'review', 0, 0, 0, 0, 0, 0, 0, 0, 0, "
            "0, 0.7, 0, 0)"
        ),
        {"source_path": source_path},
    )
    return int(conn.execute(text("SELECT last_insert_rowid()")).scalar_one())


def _seed_story_arc_placement_parents(
    conn: Connection,
    *,
    slug: str,
) -> dict[str, int]:
    """Seed bounded parents needed for migration-level placement inserts."""
    _, issue_id = _seed_reader_state_owner(conn, slug=slug)
    conn.execute(
        text("INSERT INTO story_arcs (name) VALUES (:name)"),
        {"name": f"Placement {slug}"},
    )
    story_arc_id = int(conn.execute(text("SELECT last_insert_rowid()")).scalar_one())
    conn.execute(
        text(
            "INSERT INTO issue_story_arcs "
            "(story_arc_id, issue_id, sequence_number, source_ordinal, "
            "legacy_sequence_was_null, resolution_state, source_kind) VALUES "
            "(:story_arc_id, :issue_id, 1, 1, 0, 'resolved', 'legacy')"
        ),
        {"story_arc_id": story_arc_id, "issue_id": issue_id},
    )
    membership_id = int(conn.execute(text("SELECT last_insert_rowid()")).scalar_one())
    import_job_id = _seed_minimal_import_job(
        conn,
        source_path=f"/fixture/{slug}",
    )
    conn.execute(
        text(
            "INSERT INTO import_job_actions "
            "(import_job_id, sequence_no, phase, action_type, status) VALUES "
            "(:job_id, 1, 'story_arc', 'placement', 'COMPLETED')"
        ),
        {"job_id": import_job_id},
    )
    action_id = int(conn.execute(text("SELECT last_insert_rowid()")).scalar_one())
    conn.execute(
        text("INSERT INTO library_roots (name, path, enabled) VALUES (:name, :path, 1)"),
        {"name": f"Root {slug}", "path": f"/fixture/root-{slug}"},
    )
    library_root_id = int(conn.execute(text("SELECT last_insert_rowid()")).scalar_one())
    root_columns = {column["name"] for column in inspect(conn).get_columns("library_roots")}
    if "is_default_managed_destination" in root_columns:
        conn.execute(
            text(
                "UPDATE library_roots SET is_default_managed_destination = true "
                "WHERE id = :root_id AND NOT EXISTS ("
                "SELECT 1 FROM library_roots "
                "WHERE is_default_managed_destination = true)"
            ),
            {"root_id": library_root_id},
        )
    return {
        "membership_id": membership_id,
        "import_job_id": import_job_id,
        "action_id": action_id,
        "library_root_id": library_root_id,
    }


def _seed_story_arc_sync_work_parents(
    conn: Connection,
    *,
    slug: str,
) -> dict[str, int]:
    """Seed one canonical file plus the action/membership required by sync work."""
    parents = _seed_story_arc_placement_parents(conn, slug=slug)
    issue_id = int(
        conn.execute(
            text("SELECT issue_id FROM issue_story_arcs WHERE id = :membership_id"),
            {"membership_id": parents["membership_id"]},
        ).scalar_one()
    )
    conn.execute(
        text(
            "INSERT INTO library_files "
            "(file_path, file_name, file_size, file_format, file_modified_at, "
            "match_confidence, naming_snapshot, has_comicinfo, issue_id, library_root_id) "
            "VALUES (:path, :name, 123, 'CBZ', CURRENT_TIMESTAMP, 'HIGH', '{}', 0, "
            ":issue_id, :library_root_id)"
        ),
        {
            "path": f"/fixture/root-{slug}/Issue.cbz",
            "name": "Issue.cbz",
            "issue_id": issue_id,
            "library_root_id": parents["library_root_id"],
        },
    )
    parents["library_file_id"] = int(conn.execute(text("SELECT last_insert_rowid()")).scalar_one())
    return parents


def _insert_story_arc_sync_work(
    conn: Connection,
    *,
    parents: dict[str, int],
    generation: str,
    origin_action_id: int | None,
    cancel_requested: bool = False,
) -> int:
    """Insert one minimal durable sync-work row against the migration schema."""
    conn.execute(
        text(
            "INSERT INTO story_arc_sync_work "
            "(issue_story_arc_id, library_file_id, desired_generation, "
            "source_signature_hash, source_file_path, source_file_size, "
            "source_file_modified_at, story_arc_revision, membership_sequence, "
            "policy_schema_version, origin_import_action_id, cancel_requested_at) VALUES "
            "(:membership_id, :library_file_id, :generation, :signature_hash, "
            ":source_path, 123, CURRENT_TIMESTAMP, 1, 1, 1, :origin_action_id, "
            f"{'CURRENT_TIMESTAMP' if cancel_requested else 'NULL'})"
        ),
        {
            "membership_id": parents["membership_id"],
            "library_file_id": parents["library_file_id"],
            "generation": generation,
            "signature_hash": generation.ljust(64, "0")[:64],
            "source_path": f"/fixture/sync/{generation}.cbz",
            "origin_action_id": origin_action_id,
        },
    )
    return int(conn.execute(text("SELECT last_insert_rowid()")).scalar_one())


class TestMigrationChain:
    """Verify the full Alembic migration chain applies cleanly."""

    def test_migration_engine_isolated_from_runtime_connection_pragmas(
        self,
        alembic_cfg,
    ) -> None:
        """App imports cannot change the subprocess-equivalent migration connection."""
        from pullbox import database

        assert event.contains(Engine, "connect", database._set_sqlite_pragma)
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, _STORY_ARC_IMPORT_INTENT_REVISION)

        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
                _insert_download_history_issue(conn)
                conn.execute(
                    text(
                        "INSERT INTO download_history "
                        "(issue_id, title, download_url, download_client, protocol, state) "
                        "VALUES (1, 'Import-order preservation', "
                        "'https://example.test/import-order', 'SABNZBD', 'usenet', 'QUEUED')"
                    )
                )
        finally:
            engine.dispose()

        command.upgrade(cfg, "head")
        command.downgrade(cfg, _STORY_ARC_IMPORT_INTENT_REVISION)
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
                assert conn.execute(text("SELECT COUNT(*) FROM download_history")).scalar_one() == 1
                assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
        finally:
            engine.dispose()

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

    def test_issue_number_text_migration_backfills_and_round_trips(
        self,
        alembic_cfg,
    ) -> None:
        """Exact issue text is additive, indexed, bounded, and reversible."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, _ISSUE_NUMBER_TEXT_PARENT_REVISION)

        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                issue_ids: list[int] = []
                for slug, number in (
                    ("million", 1_000_000.0),
                    ("fraction", 0.5),
                    ("exponent", 1e86),
                ):
                    _, issue_id = _seed_reader_state_owner(conn, slug=slug)
                    conn.execute(
                        text("UPDATE issues SET issue_number = :number WHERE id = :issue_id"),
                        {"number": number, "issue_id": issue_id},
                    )
                    issue_ids.append(issue_id)
                conn.execute(
                    text(
                        "INSERT INTO download_history "
                        "(issue_id, title, download_url, download_client, protocol, state) "
                        "VALUES (:issue_id, 'Preserved history', "
                        "'https://example.test/preserved-history', 'SABNZBD', "
                        "'usenet', 'QUEUED')"
                    ),
                    {"issue_id": issue_ids[0]},
                )
                preserved_history_id = conn.execute(text("SELECT last_insert_rowid()")).scalar_one()
        finally:
            engine.dispose()

        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            inspector = inspect(engine)
            columns = {column["name"]: column for column in inspector.get_columns("issues")}
            unique_constraints = {
                constraint["name"]: constraint["column_names"]
                for constraint in inspector.get_unique_constraints("issues")
            }
            indexes = {index["name"]: index for index in inspector.get_indexes("issues")}
            with engine.begin() as conn:
                rows = conn.execute(
                    text(
                        "SELECT id, issue_number_text FROM issues "
                        "WHERE id IN (:one, :two, :three) ORDER BY id"
                    ),
                    {"one": issue_ids[0], "two": issue_ids[1], "three": issue_ids[2]},
                ).fetchall()
                _, legacy_issue_id = _seed_reader_state_owner(conn, slug="legacy-write")
                legacy_text = conn.execute(
                    text("SELECT issue_number_text FROM issues WHERE id = :issue_id"),
                    {"issue_id": legacy_issue_id},
                ).scalar_one()
                preserved_history_issue_id = conn.execute(
                    text("SELECT issue_id FROM download_history WHERE id = :history_id"),
                    {"history_id": preserved_history_id},
                ).scalar_one()
        finally:
            engine.dispose()

        assert columns["issue_number_text"]["nullable"] is True
        assert columns["issue_number_text"]["type"].length == 320
        assert "uq_series_issue" not in unique_constraints
        assert indexes["uq_series_issue_number_text"]["column_names"] == [
            "series_id",
            "issue_number_text",
        ]
        assert bool(indexes["uq_series_issue_number_text"]["unique"]) is True
        assert indexes["ix_issues_series_number_order"]["column_names"] == [
            "series_id",
            "issue_number",
            "issue_number_text",
            "id",
        ]
        assert [tuple(row) for row in rows] == [
            (issue_ids[0], "1000000"),
            (issue_ids[1], "0.5"),
            (issue_ids[2], "1" + ("0" * 86)),
        ]
        assert legacy_text is None
        assert preserved_history_issue_id == issue_ids[0]

        command.downgrade(cfg, _ISSUE_NUMBER_TEXT_PARENT_REVISION)
        assert "issue_number_text" not in _get_columns(sync_url, "issues")
        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                assert (
                    conn.execute(
                        text("SELECT issue_id FROM download_history WHERE id = :history_id"),
                        {"history_id": preserved_history_id},
                    ).scalar_one()
                    == issue_ids[0]
                )
        finally:
            engine.dispose()
        command.upgrade(cfg, "head")

    def test_issue_number_text_downgrade_blocks_divergent_exact_text(
        self,
        alembic_cfg,
    ) -> None:
        """Downgrade refuses to erase exact provider suffix semantics."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                _, issue_id = _seed_reader_state_owner(conn, slug="suffix")
                conn.execute(
                    text("UPDATE issues SET issue_number_text = '1AU' WHERE id = :issue_id"),
                    {"issue_id": issue_id},
                )
        finally:
            engine.dispose()

        with pytest.raises(RuntimeError, match="divergent exact issue-number text"):
            command.downgrade(cfg, _ISSUE_NUMBER_TEXT_PARENT_REVISION)

        assert "issue_number_text" in _get_columns(sync_url, "issues")

    def test_exact_issue_identity_migration_allows_suffix_siblings_and_round_trips(
        self,
        alembic_cfg,
    ) -> None:
        """M2 replaces float identity, preserves exact uniqueness, and fails closed."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, _STORY_ARC_IMPORT_INTENT_REVISION)

        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                _, first_issue_id = _seed_reader_state_owner(conn, slug="suffix-sibling")
                series_id = int(
                    conn.execute(
                        text("SELECT series_id FROM issues WHERE id = :issue_id"),
                        {"issue_id": first_issue_id},
                    ).scalar_one()
                )
                conn.execute(
                    text("UPDATE issues SET issue_number_text = '1AU' WHERE id = :issue_id"),
                    {"issue_id": first_issue_id},
                )
                conn.execute(
                    text(
                        "INSERT INTO download_history "
                        "(issue_id, title, download_url, download_client, protocol, state) "
                        "VALUES (:issue_id, 'Preserved sibling history', "
                        "'https://example.test/sibling-history', 'SABNZBD', "
                        "'usenet', 'QUEUED')"
                    ),
                    {"issue_id": first_issue_id},
                )
                history_id = int(conn.execute(text("SELECT last_insert_rowid()")).scalar_one())
        finally:
            engine.dispose()

        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            inspector = inspect(engine)
            unique_constraints = {
                constraint["name"]: constraint["column_names"]
                for constraint in inspector.get_unique_constraints("issues")
            }
            indexes = {index["name"]: index for index in inspector.get_indexes("issues")}
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO issues "
                        "(series_id, issue_number, issue_number_text, status, issue_type, "
                        "manual_skip, created_at, updated_at) VALUES "
                        "(:series_id, 1.0, '1B', 'OWNED', 'ISSUE', 0, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {"series_id": series_id},
                )
                second_issue_id = int(conn.execute(text("SELECT last_insert_rowid()")).scalar_one())

            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO issues "
                        "(series_id, issue_number, issue_number_text, status, issue_type, "
                        "manual_skip, created_at, updated_at) VALUES "
                        "(:series_id, 1.0, '1AU', 'OWNED', 'ISSUE', 0, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {"series_id": series_id},
                )

            with engine.connect() as conn:
                exact_rows = conn.execute(
                    text(
                        "SELECT issue_number, issue_number_text FROM issues "
                        "WHERE series_id = :series_id "
                        "ORDER BY issue_number, issue_number_text, id"
                    ),
                    {"series_id": series_id},
                ).fetchall()
                preserved_history_issue_id = int(
                    conn.execute(
                        text("SELECT issue_id FROM download_history WHERE id = :history_id"),
                        {"history_id": history_id},
                    ).scalar_one()
                )
        finally:
            engine.dispose()

        assert "uq_series_issue" not in unique_constraints
        assert indexes["uq_series_issue_number_text"]["column_names"] == [
            "series_id",
            "issue_number_text",
        ]
        assert bool(indexes["uq_series_issue_number_text"]["unique"]) is True
        assert indexes["ix_issues_series_number_order"]["column_names"] == [
            "series_id",
            "issue_number",
            "issue_number_text",
            "id",
        ]
        assert [tuple(row) for row in exact_rows] == [(1.0, "1AU"), (1.0, "1B")]
        assert preserved_history_issue_id == first_issue_id

        with pytest.raises(RuntimeError, match="exact issue-number siblings"):
            command.downgrade(cfg, _STORY_ARC_IMPORT_INTENT_REVISION)

        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM issues WHERE id = :issue_id"),
                    {"issue_id": second_issue_id},
                )
        finally:
            engine.dispose()

        command.downgrade(cfg, _STORY_ARC_IMPORT_INTENT_REVISION)
        engine = create_engine(sync_url)
        try:
            restored_constraints = {
                constraint["name"]: constraint["column_names"]
                for constraint in inspect(engine).get_unique_constraints("issues")
            }
            with engine.connect() as conn:
                assert (
                    conn.execute(
                        text("SELECT issue_id FROM download_history WHERE id = :history_id"),
                        {"history_id": history_id},
                    ).scalar_one()
                    == first_issue_id
                )
        finally:
            engine.dispose()
        assert restored_constraints["uq_series_issue"] == ["series_id", "issue_number"]

        command.upgrade(cfg, "head")

    def test_story_arc_domain_migration_preserves_legacy_graph_and_round_trips(
        self,
        alembic_cfg,
    ) -> None:
        """Legacy arcs gain reviewable identity without rebuilding parent tables."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, _STORY_ARC_DOMAIN_PARENT_REVISION)

        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                issue_ids = [
                    _seed_reader_state_owner(conn, slug=slug)[1]
                    for slug in ("million", "duplicate-order", "missing-order")
                ]
                for issue_id, number, exact_text in (
                    (issue_ids[0], 1_000_000.0, "1000000"),
                    (issue_ids[1], 2.0, "2"),
                    (issue_ids[2], 3.0, "3"),
                ):
                    conn.execute(
                        text(
                            "UPDATE issues SET issue_number = :number, "
                            "issue_number_text = :exact_text WHERE id = :issue_id"
                        ),
                        {"number": number, "exact_text": exact_text, "issue_id": issue_id},
                    )

                conn.execute(
                    text(
                        "INSERT INTO story_arcs (comicvine_id, name, description) "
                        "VALUES (7001, '  Legacy   Event  ', 'Preserved')"
                    )
                )
                first_arc_id = int(conn.execute(text("SELECT last_insert_rowid()")).scalar_one())
                conn.execute(text("INSERT INTO story_arcs (name) VALUES ('Second Arc')"))
                second_arc_id = int(conn.execute(text("SELECT last_insert_rowid()")).scalar_one())
                conn.execute(
                    text(
                        "INSERT INTO issue_story_arcs "
                        "(issue_id, story_arc_id, sequence_number) VALUES "
                        "(:issue_one, :first_arc, 10), "
                        "(:issue_two, :first_arc, 10), "
                        "(:issue_three, :first_arc, NULL), "
                        "(:issue_one, :second_arc, 4)"
                    ),
                    {
                        "issue_one": issue_ids[0],
                        "issue_two": issue_ids[1],
                        "issue_three": issue_ids[2],
                        "first_arc": first_arc_id,
                        "second_arc": second_arc_id,
                    },
                )
                conn.execute(
                    text(
                        "INSERT INTO download_history "
                        "(issue_id, title, download_url, download_client, protocol, state) "
                        "VALUES (:issue_id, 'Story arc child row', "
                        "'https://example.test/story-arc-child', 'SABNZBD', "
                        "'usenet', 'QUEUED')"
                    ),
                    {"issue_id": issue_ids[0]},
                )
                history_id = int(conn.execute(text("SELECT last_insert_rowid()")).scalar_one())

                import_job_id = _seed_minimal_import_job(
                    conn,
                    source_path="/fixture/library",
                )
                conn.execute(
                    text(
                        "INSERT INTO import_series "
                        "(import_job_id, status, raw_series_name, file_count, has_files) "
                        "VALUES (:job_id, 'pending', 'Legacy Series', 1, 1)"
                    ),
                    {"job_id": import_job_id},
                )
                imported_series_id = int(
                    conn.execute(text("SELECT last_insert_rowid()")).scalar_one()
                )
                conn.execute(
                    text(
                        "INSERT INTO import_files "
                        "(import_job_id, import_series_id, file_path, file_name, file_size, "
                        "file_format, has_comicinfo, status, is_preferred) VALUES "
                        "(:job_id, :series_id, '/fixture/library/issue.cbz', 'issue.cbz', "
                        "100, 'cbz', 0, 'pending', 0)"
                    ),
                    {"job_id": import_job_id, "series_id": imported_series_id},
                )
                imported_file_id = int(
                    conn.execute(text("SELECT last_insert_rowid()")).scalar_one()
                )
        finally:
            engine.dispose()

        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            inspector = inspect(engine)
            assert {
                "story_arc_external_identities",
                "import_story_arcs",
                "import_story_arc_entries",
                "story_arc_placements",
            }.issubset(inspector.get_table_names())
            assert {
                "normalized_name",
                "source_kind",
                "lifecycle",
                "policy_schema_version",
                "policy_snapshot",
                "source_import_job_id",
                "revision",
                "diagnostics",
            }.issubset(_get_columns(sync_url, "story_arcs"))
            assert {
                "id",
                "source_ordinal",
                "legacy_sequence_was_null",
                "resolution_state",
                "source_issue_number_text",
            }.issubset(_get_columns(sync_url, "issue_story_arcs"))
            assert {
                "source_folder_cohort_key",
                "source_ordinal",
            }.issubset(_get_columns(sync_url, "import_files"))

            indexes = _get_indexes(sync_url, "issue_story_arcs")
            assert indexes["ix_issue_story_arcs_order"] == [
                "story_arc_id",
                "sequence_number",
                "source_ordinal",
                "id",
            ]
            assert _get_indexes(sync_url, "import_files")["ix_import_files_job_cohort_order"] == [
                "import_job_id",
                "source_folder_cohort_key",
                "source_ordinal",
                "id",
            ]

            with engine.connect() as conn:
                arc_row = conn.execute(
                    text(
                        "SELECT normalized_name, source_kind, lifecycle, monitored, "
                        "search_missing, include_upcoming, sync_enabled, revision "
                        "FROM story_arcs WHERE id = :arc_id"
                    ),
                    {"arc_id": first_arc_id},
                ).one()
                membership_rows = conn.execute(
                    text(
                        "SELECT issue_id, story_arc_id, sequence_number, source_ordinal, "
                        "legacy_sequence_was_null, resolution_state, "
                        "source_issue_number_text FROM issue_story_arcs "
                        "ORDER BY story_arc_id, issue_id"
                    )
                ).fetchall()
                external_identity = conn.execute(
                    text(
                        "SELECT source, namespace, external_id "
                        "FROM story_arc_external_identities WHERE story_arc_id = :arc_id"
                    ),
                    {"arc_id": first_arc_id},
                ).one()
                preserved_history_issue_id = conn.execute(
                    text("SELECT issue_id FROM download_history WHERE id = :history_id"),
                    {"history_id": history_id},
                ).scalar_one()
                cohort_values = conn.execute(
                    text(
                        "SELECT source_folder_cohort_key, source_ordinal "
                        "FROM import_files WHERE id = :file_id"
                    ),
                    {"file_id": imported_file_id},
                ).one()

            assert tuple(arc_row) == ("legacy event", "legacy", "active", 0, 0, 0, 0, 1)
            assert [tuple(row) for row in membership_rows] == [
                (issue_ids[0], first_arc_id, 10, 1, 0, "resolved", "1000000"),
                (issue_ids[1], first_arc_id, 10, 2, 0, "resolved", "2"),
                (issue_ids[2], first_arc_id, 11, 3, 1, "resolved", "3"),
                (issue_ids[0], second_arc_id, 4, 1, 0, "resolved", "1000000"),
            ]
            assert tuple(external_identity) == ("comicvine", "story_arc", "7001")
            assert preserved_history_issue_id == issue_ids[0]
            assert tuple(cohort_values) == (None, None)

            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO import_story_arcs "
                        "(import_job_id, source_kind, source_key, source_arc_id, "
                        "source_ordinal, name, normalized_name, status) VALUES "
                        "(:job_id, 'mylar3', 'mylar3:arc:42', '42', 0, "
                        "'Imported Event', 'imported event', 'detected')"
                    ),
                    {"job_id": import_job_id},
                )
                staged_arc_id = int(conn.execute(text("SELECT last_insert_rowid()")).scalar_one())
                conn.execute(
                    text(
                        "INSERT INTO import_story_arc_entries "
                        "(imported_story_arc_id, import_file_id, matched_issue_id, "
                        "source_ordinal, reading_order, resolution_state, source_kind, "
                        "source_issue_number_text) VALUES "
                        "(:arc_id, :file_id, :issue_id, 0, 1, 'resolved', "
                        "'mylar3', '1000000')"
                    ),
                    {
                        "arc_id": staged_arc_id,
                        "file_id": imported_file_id,
                        "issue_id": issue_ids[0],
                    },
                )
                assert conn.execute(text("SELECT COUNT(*) FROM story_arcs")).scalar_one() == 2
                assert conn.execute(text("SELECT COUNT(*) FROM issue_story_arcs")).scalar_one() == 4
                membership_id = int(
                    conn.execute(
                        text(
                            "SELECT id FROM issue_story_arcs "
                            "WHERE story_arc_id = :arc_id AND issue_id = :issue_id"
                        ),
                        {"arc_id": first_arc_id, "issue_id": issue_ids[0]},
                    ).scalar_one()
                )
                conn.execute(
                    text(
                        "INSERT INTO story_arc_placements "
                        "(issue_story_arc_id, placement_path, mode, "
                        "ownership, source_kind, state) VALUES "
                        "(:membership_id, '/fixture/arcs/legacy-event/001.cbz', "
                        "'reference_only', 'referenced', 'mylar3', 'current')"
                    ),
                    {"membership_id": membership_id},
                )

            with (
                pytest.raises(IntegrityError),
                engine.begin() as conn,
            ):
                conn.execute(
                    text(
                        "INSERT INTO story_arc_external_identities "
                        "(story_arc_id, source, namespace, external_id) VALUES "
                        "(:arc_id, 'comicvine', 'story_arc', '7001')"
                    ),
                    {"arc_id": second_arc_id},
                )

            with (
                pytest.raises(IntegrityError),
                engine.begin() as conn,
            ):
                conn.execute(
                    text(
                        "INSERT INTO story_arc_placements "
                        "(issue_story_arc_id, placement_path, mode, "
                        "ownership, source_kind, state) VALUES "
                        "(:membership_id, '/fixture/arcs/legacy-event/managed.cbz', "
                        "'copy', 'referenced', 'pullbox', 'current')"
                    ),
                    {"membership_id": membership_id},
                )

            with (
                pytest.raises(IntegrityError),
                engine.begin() as conn,
            ):
                conn.execute(
                    text("UPDATE story_arcs SET lifecycle = 'ACTIVE' WHERE id = :arc_id"),
                    {"arc_id": first_arc_id},
                )

            with engine.begin() as conn:
                conn.execute(text("DELETE FROM story_arc_placements"))
                conn.execute(text("DELETE FROM import_story_arc_entries"))
                conn.execute(text("DELETE FROM import_story_arcs"))
        finally:
            engine.dispose()

        command.downgrade(cfg, _STORY_ARC_DOMAIN_PARENT_REVISION)

        engine = create_engine(sync_url)
        try:
            inspector = inspect(engine)
            assert "story_arc_external_identities" not in inspector.get_table_names()
            assert set(_get_columns(sync_url, "issue_story_arcs")) == {
                "issue_id",
                "story_arc_id",
                "sequence_number",
            }
            with engine.connect() as conn:
                restored_memberships = conn.execute(
                    text(
                        "SELECT issue_id, story_arc_id, sequence_number "
                        "FROM issue_story_arcs ORDER BY story_arc_id, issue_id"
                    )
                ).fetchall()
                assert (
                    conn.execute(
                        text("SELECT issue_id FROM download_history WHERE id = :history_id"),
                        {"history_id": history_id},
                    ).scalar_one()
                    == issue_ids[0]
                )
            assert [tuple(row) for row in restored_memberships] == [
                (issue_ids[0], first_arc_id, 10),
                (issue_ids[1], first_arc_id, 10),
                (issue_ids[2], first_arc_id, None),
                (issue_ids[0], second_arc_id, 4),
            ]
        finally:
            engine.dispose()

        command.upgrade(cfg, "head")
        assert "story_arc_placements" in inspect(create_engine(sync_url)).get_table_names()

    def test_story_arc_domain_downgrade_blocks_unresolved_memberships(
        self,
        alembic_cfg,
    ) -> None:
        """Downgrade refuses to erase missing-entry and exact review evidence."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO story_arcs (name) VALUES ('Unresolved Arc')"))
                arc_id = int(conn.execute(text("SELECT last_insert_rowid()")).scalar_one())
                conn.execute(
                    text(
                        "INSERT INTO issue_story_arcs "
                        "(story_arc_id, issue_id, sequence_number, source_ordinal, "
                        "legacy_sequence_was_null, resolution_state, source_kind, "
                        "source_issue_number_text) VALUES "
                        "(:arc_id, NULL, 1, 1, 0, 'missing', 'legacy', '1AU')"
                    ),
                    {"arc_id": arc_id},
                )
        finally:
            engine.dispose()

        with pytest.raises(RuntimeError, match="legacy story-arc schema"):
            command.downgrade(cfg, _STORY_ARC_DOMAIN_PARENT_REVISION)

        assert "resolution_state" in _get_columns(sync_url, "issue_story_arcs")

    def test_managed_story_arc_placement_migration_preserves_referenced_rows_losslessly(
        self,
        alembic_cfg,
    ) -> None:
        """IU6-A referenced placement evidence survives upgrade and downgrade exactly."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, _STORY_ARC_DOMAIN_REVISION)

        row_sql = text(
            "SELECT id, issue_story_arc_id, library_file_id, library_root_id, "
            "placement_path, mode, ownership, symlink_style, source_kind, "
            "source_import_job_id, creating_action_id, rendered_reading_order, "
            "policy_schema_version, source_fingerprint, state, last_result, "
            "last_checked_at, created_at, updated_at "
            "FROM story_arc_placements WHERE placement_path = :path"
        )
        placement_path = "/fixture/arcs/reference/017.cbz"
        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                parents = _seed_story_arc_placement_parents(conn, slug="preserve")
                conn.execute(
                    text(
                        "INSERT INTO story_arc_placements "
                        "(issue_story_arc_id, library_root_id, placement_path, mode, "
                        "ownership, symlink_style, source_kind, source_import_job_id, "
                        "creating_action_id, rendered_reading_order, policy_schema_version, "
                        "source_fingerprint, state, last_result, last_checked_at) VALUES "
                        "(:membership_id, :library_root_id, :path, 'reference_only', "
                        "'referenced', NULL, 'mylar3', :job_id, :action_id, 17, 1, "
                        ":source_fingerprint, 'drifted', :last_result, "
                        "'2026-08-30 12:34:56')"
                    ),
                    {
                        "membership_id": parents["membership_id"],
                        "library_root_id": parents["library_root_id"],
                        "path": placement_path,
                        "job_id": parents["import_job_id"],
                        "action_id": parents["action_id"],
                        "source_fingerprint": '{"size":123,"mtime_ns":456}',
                        "last_result": '{"result":"preserved"}',
                    },
                )
                before = dict(conn.execute(row_sql, {"path": placement_path}).mappings().one())
        finally:
            engine.dispose()

        command.upgrade(cfg, _STORY_ARC_MANAGED_PLACEMENT_REVISION)
        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                upgraded = dict(conn.execute(row_sql, {"path": placement_path}).mappings().one())
        finally:
            engine.dispose()
        assert upgraded == before

        command.downgrade(cfg, _STORY_ARC_DOMAIN_REVISION)
        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                downgraded = dict(conn.execute(row_sql, {"path": placement_path}).mappings().one())
        finally:
            engine.dispose()
        assert downgraded == before

    def test_managed_story_arc_placement_migration_enforces_complete_mode_matrix(
        self,
        alembic_cfg,
    ) -> None:
        """Every valid combination persists and every invalid combination is rejected."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        valid = {
            ("reference_only", "referenced", None),
            ("copy", "managed", None),
            ("hardlink", "managed", None),
            ("symlink", "managed", "absolute"),
            ("symlink", "managed", "relative"),
        }
        all_combinations = set(
            product(
                ("reference_only", "copy", "hardlink", "symlink"),
                ("referenced", "managed"),
                (None, "absolute", "relative"),
            )
        )
        invalid = sorted(
            all_combinations - valid,
            key=lambda item: (item[0], item[1], str(item[2])),
        )
        insert_sql = text(
            "INSERT INTO story_arc_placements "
            "(issue_story_arc_id, placement_path, mode, ownership, symlink_style) "
            "VALUES (:membership_id, :path, :mode, :ownership, :symlink_style)"
        )

        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                parents = _seed_story_arc_placement_parents(conn, slug="matrix")
                for ordinal, (mode, ownership, symlink_style) in enumerate(
                    sorted(valid, key=lambda item: (item[0], item[1], str(item[2]))),
                    start=1,
                ):
                    conn.execute(
                        insert_sql,
                        {
                            "membership_id": parents["membership_id"],
                            "path": f"/fixture/arcs/matrix/valid-{ordinal}.cbz",
                            "mode": mode,
                            "ownership": ownership,
                            "symlink_style": symlink_style,
                        },
                    )

            for ordinal, (mode, ownership, symlink_style) in enumerate(invalid, start=1):
                with pytest.raises(IntegrityError), engine.begin() as conn:
                    conn.execute(
                        insert_sql,
                        {
                            "membership_id": parents["membership_id"],
                            "path": f"/fixture/arcs/matrix/invalid-{ordinal}.cbz",
                            "mode": mode,
                            "ownership": ownership,
                            "symlink_style": symlink_style,
                        },
                    )

            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(
                    insert_sql,
                    {
                        "membership_id": parents["membership_id"],
                        "path": "/fixture/arcs/matrix/move.cbz",
                        "mode": "move",
                        "ownership": "managed",
                        "symlink_style": None,
                    },
                )

            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(
                    insert_sql,
                    {
                        "membership_id": parents["membership_id"],
                        "path": "/fixture/arcs/matrix/valid-1.cbz",
                        "mode": "copy",
                        "ownership": "managed",
                        "symlink_style": None,
                    },
                )

            with engine.connect() as conn:
                assert conn.execute(
                    text("SELECT COUNT(*) FROM story_arc_placements")
                ).scalar_one() == len(valid)
        finally:
            engine.dispose()

    def test_managed_story_arc_placement_downgrade_refuses_managed_rows(
        self,
        alembic_cfg,
    ) -> None:
        """Downgrade fails before DDL rather than silently discarding managed evidence."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                parents = _seed_story_arc_placement_parents(conn, slug="guard")
                conn.execute(
                    text(
                        "INSERT INTO story_arc_placements "
                        "(issue_story_arc_id, placement_path, mode, ownership) VALUES "
                        "(:membership_id, '/fixture/arcs/guard/001.cbz', "
                        "'copy', 'managed')"
                    ),
                    {"membership_id": parents["membership_id"]},
                )
        finally:
            engine.dispose()

        with pytest.raises(RuntimeError, match="managed story-arc placements"):
            command.downgrade(cfg, _STORY_ARC_DOMAIN_REVISION)

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                assert conn.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one() == (_STORY_ARC_MANAGED_PLACEMENT_REVISION)
                assert (
                    conn.execute(text("SELECT mode FROM story_arc_placements")).scalar_one()
                    == "copy"
                )
        finally:
            engine.dispose()

    def test_import_owned_story_arc_sync_schema_enforces_provenance(self, alembic_cfg) -> None:
        """Import sync provenance is optional, unique when present, and SET NULL safe."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            inspector = inspect(engine)
            columns = {
                column["name"]: column for column in inspector.get_columns("story_arc_sync_work")
            }
            foreign_keys = inspector.get_foreign_keys("story_arc_sync_work")
            import_job_columns = {
                column["name"]: column for column in inspector.get_columns("import_jobs")
            }
            import_job_foreign_keys = inspector.get_foreign_keys("import_jobs")
            unique_constraints = {
                constraint["name"]: constraint["column_names"]
                for constraint in inspector.get_unique_constraints("story_arc_sync_work")
            }
            assert columns["origin_import_action_id"]["nullable"] is True
            assert columns["origin_import_job_id"]["nullable"] is True
            assert columns["origin_imported_story_arc_id"]["nullable"] is True
            assert columns["origin_imported_story_arc_entry_id"]["nullable"] is True
            assert columns["cancel_requested_at"]["nullable"] is True
            assert any(
                foreign_key["constrained_columns"] == ["origin_import_action_id"]
                and foreign_key["referred_table"] == "import_job_actions"
                and foreign_key["referred_columns"] == ["id"]
                and foreign_key["options"].get("ondelete") == "SET NULL"
                for foreign_key in foreign_keys
            )
            expected_typed_origins = {
                ("origin_import_job_id", "import_jobs"),
                ("origin_imported_story_arc_id", "import_story_arcs"),
                ("origin_imported_story_arc_entry_id", "import_story_arc_entries"),
            }
            assert expected_typed_origins <= {
                (foreign_key["constrained_columns"][0], foreign_key["referred_table"])
                for foreign_key in foreign_keys
                if foreign_key["options"].get("ondelete") == "SET NULL"
            }
            assert import_job_columns["story_arc_placement_followup_pending"]["nullable"] is False
            assert import_job_columns["story_arc_rollback_waiting_work_id"]["nullable"] is True
            assert any(
                foreign_key["constrained_columns"] == ["story_arc_rollback_waiting_work_id"]
                and foreign_key["referred_table"] == "story_arc_sync_work"
                and foreign_key["referred_columns"] == ["id"]
                for foreign_key in import_job_foreign_keys
            )
            with engine.connect() as conn:
                rollback_fk = next(
                    row
                    for row in conn.execute(text("PRAGMA foreign_key_list(import_jobs)"))
                    if row[3] == "story_arc_rollback_waiting_work_id"
                )
                assert rollback_fk[2] == "story_arc_sync_work"
                assert rollback_fk[4] == "id"
                assert rollback_fk[6] == "SET NULL"
            assert unique_constraints["uq_story_arc_sync_work_origin_import_action"] == [
                "origin_import_action_id"
            ]
            assert _get_indexes(sync_url, "story_arc_placements")[
                "ix_story_arc_placements_creating_action"
            ] == ["creating_action_id", "id"]
            sync_indexes = _get_indexes(sync_url, "story_arc_sync_work")
            assert sync_indexes["ix_story_arc_sync_work_queued"] == [
                "claimable",
                "state",
                "created_at",
                "id",
            ]
            assert sync_indexes["ix_story_arc_sync_work_stale_claim"] == [
                "claimable",
                "state",
                "claimed_at",
                "id",
            ]
            assert sync_indexes["ix_story_arc_sync_work_origin_job_state"] == [
                "origin_import_job_id",
                "state",
                "id",
            ]
            import_job_indexes = _get_indexes(sync_url, "import_jobs")
            assert import_job_indexes["ix_import_jobs_story_arc_followup"] == [
                "status",
                "story_arc_placement_followup_pending",
                "id",
            ]
            assert import_job_indexes["ix_import_jobs_story_arc_rollback_waiting"] == [
                "status",
                "story_arc_rollback_waiting_work_id",
                "id",
            ]
            assert _get_indexes(sync_url, "import_job_actions")[
                "ix_import_job_actions_job_id_keyset"
            ] == ["import_job_id", "id"]

            with engine.begin() as conn:
                parents = _seed_story_arc_sync_work_parents(conn, slug="origin-schema")
                first_unlinked_id = _insert_story_arc_sync_work(
                    conn,
                    parents=parents,
                    generation="unlinked-1",
                    origin_action_id=None,
                )
                second_unlinked_id = _insert_story_arc_sync_work(
                    conn,
                    parents=parents,
                    generation="unlinked-2",
                    origin_action_id=None,
                )
                linked_id = _insert_story_arc_sync_work(
                    conn,
                    parents=parents,
                    generation="linked",
                    origin_action_id=parents["action_id"],
                    cancel_requested=True,
                )
                lifecycle_defaults = conn.execute(
                    text(
                        "SELECT story_arc_placement_followup_pending, "
                        "story_arc_rollback_waiting_work_id FROM import_jobs "
                        "WHERE id = :job_id"
                    ),
                    {"job_id": parents["import_job_id"]},
                ).one()
                assert tuple(lifecycle_defaults) == (0, None)

            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(text("PRAGMA foreign_keys=ON"))
                conn.execute(
                    text(
                        "UPDATE story_arc_sync_work SET origin_import_job_id = 999999 "
                        "WHERE id = :work_id"
                    ),
                    {"work_id": linked_id},
                )

            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(text("PRAGMA foreign_keys=ON"))
                conn.execute(
                    text(
                        "UPDATE import_jobs SET story_arc_rollback_waiting_work_id = 999999 "
                        "WHERE id = :job_id"
                    ),
                    {"job_id": parents["import_job_id"]},
                )

            with pytest.raises(IntegrityError), engine.begin() as conn:
                _insert_story_arc_sync_work(
                    conn,
                    parents=parents,
                    generation="linked-duplicate",
                    origin_action_id=parents["action_id"],
                )

            with engine.begin() as conn:
                conn.execute(text("PRAGMA foreign_keys=ON"))
                conn.execute(
                    text("DELETE FROM import_job_actions WHERE id = :action_id"),
                    {"action_id": parents["action_id"]},
                )
                rows = conn.execute(
                    text(
                        "SELECT id, origin_import_action_id, cancel_requested_at "
                        "FROM story_arc_sync_work ORDER BY id"
                    )
                ).fetchall()
            assert [row.id for row in rows] == [first_unlinked_id, second_unlinked_id, linked_id]
            assert all(row.origin_import_action_id is None for row in rows)
            assert rows[-1].cancel_requested_at is not None
        finally:
            engine.dispose()

    def test_import_owned_story_arc_sync_migration_round_trips_unlinked_work(
        self,
        alembic_cfg,
    ) -> None:
        """SQLite batch DDL preserves ordinary queue work across downgrade and re-upgrade."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")
        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                parents = _seed_story_arc_sync_work_parents(conn, slug="origin-cycle")
                work_id = _insert_story_arc_sync_work(
                    conn,
                    parents=parents,
                    generation="cycle",
                    origin_action_id=None,
                )
        finally:
            engine.dispose()

        command.downgrade(cfg, _STORY_ARC_MANAGED_PLACEMENT_REVISION)
        assert {
            "origin_import_action_id",
            "origin_import_job_id",
            "origin_imported_story_arc_id",
            "origin_imported_story_arc_entry_id",
            "cancel_requested_at",
        }.isdisjoint(_get_columns(sync_url, "story_arc_sync_work"))
        assert {
            "story_arc_placement_followup_pending",
            "story_arc_rollback_waiting_work_id",
        }.isdisjoint(_get_columns(sync_url, "import_jobs"))
        assert "ix_story_arc_placements_creating_action" not in _get_indexes(
            sync_url, "story_arc_placements"
        )
        sync_indexes = _get_indexes(sync_url, "story_arc_sync_work")
        assert "ix_story_arc_sync_work_queued" not in sync_indexes
        assert "ix_story_arc_sync_work_stale_claim" not in sync_indexes
        assert "ix_story_arc_sync_work_origin_job_state" not in sync_indexes
        assert "ix_import_job_actions_job_id_keyset" not in _get_indexes(
            sync_url, "import_job_actions"
        )
        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                assert (
                    conn.execute(
                        text("SELECT desired_generation FROM story_arc_sync_work WHERE id = :id"),
                        {"id": work_id},
                    ).scalar_one()
                    == "cycle"
                )
                assert (
                    conn.execute(
                        text("SELECT COUNT(*) FROM import_jobs WHERE id = :job_id"),
                        {"job_id": parents["import_job_id"]},
                    ).scalar_one()
                    == 1
                )
        finally:
            engine.dispose()

        command.upgrade(cfg, "head")
        assert {
            "origin_import_action_id",
            "origin_import_job_id",
            "origin_imported_story_arc_id",
            "origin_imported_story_arc_entry_id",
            "cancel_requested_at",
        } <= _get_columns(sync_url, "story_arc_sync_work")
        assert {
            "story_arc_placement_followup_pending",
            "story_arc_rollback_waiting_work_id",
        } <= _get_columns(sync_url, "import_jobs")
        sync_indexes = _get_indexes(sync_url, "story_arc_sync_work")
        assert "ix_story_arc_sync_work_queued" in sync_indexes
        assert "ix_story_arc_sync_work_stale_claim" in sync_indexes
        assert "ix_story_arc_sync_work_origin_job_state" in sync_indexes
        assert "ix_import_job_actions_job_id_keyset" in _get_indexes(sync_url, "import_job_actions")
        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                restored = conn.execute(
                    text(
                        "SELECT desired_generation, origin_import_action_id, "
                        "cancel_requested_at FROM story_arc_sync_work WHERE id = :id"
                    ),
                    {"id": work_id},
                ).one()
            assert tuple(restored) == ("cycle", None, None)
        finally:
            engine.dispose()

    def test_import_owned_story_arc_sync_downgrade_refuses_linked_work(
        self,
        alembic_cfg,
    ) -> None:
        """Downgrade refuses to erase action provenance while linked work remains."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")
        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                parents = _seed_story_arc_sync_work_parents(conn, slug="origin-guard")
                work_id = _insert_story_arc_sync_work(
                    conn,
                    parents=parents,
                    generation="guard",
                    origin_action_id=parents["action_id"],
                )
        finally:
            engine.dispose()

        with pytest.raises(RuntimeError, match="import-owned story-arc sync work"):
            command.downgrade(cfg, _STORY_ARC_MANAGED_PLACEMENT_REVISION)

        assert {
            "origin_import_action_id",
            "cancel_requested_at",
        } <= _get_columns(sync_url, "story_arc_sync_work")
        assert "ix_story_arc_placements_creating_action" in _get_indexes(
            sync_url, "story_arc_placements"
        )
        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                assert (
                    conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                    == _STORY_ARC_IMPORT_SYNC_REVISION
                )
                assert (
                    conn.execute(
                        text(
                            "SELECT origin_import_action_id FROM story_arc_sync_work WHERE id = :id"
                        ),
                        {"id": work_id},
                    ).scalar_one()
                    == parents["action_id"]
                )
        finally:
            engine.dispose()

    def test_import_owned_story_arc_sync_downgrade_refuses_detached_held_work(
        self,
        alembic_cfg,
    ) -> None:
        """Downgrade cannot revive held work after its provenance is detached."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")
        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                conn.execute(text("PRAGMA foreign_keys=ON"))
                parents = _seed_story_arc_sync_work_parents(conn, slug="held-guard")
                work_id = _insert_story_arc_sync_work(
                    conn,
                    parents=parents,
                    generation="held-guard",
                    origin_action_id=parents["action_id"],
                )
                conn.execute(
                    text(
                        "UPDATE story_arc_sync_work SET origin_import_job_id = :job_id, "
                        "claimable = 0 WHERE id = :work_id"
                    ),
                    {"job_id": parents["import_job_id"], "work_id": work_id},
                )
                conn.execute(
                    text("DELETE FROM import_jobs WHERE id = :job_id"),
                    {"job_id": parents["import_job_id"]},
                )
                detached = conn.execute(
                    text(
                        "SELECT origin_import_action_id, origin_import_job_id, claimable "
                        "FROM story_arc_sync_work WHERE id = :work_id"
                    ),
                    {"work_id": work_id},
                ).one()
            assert tuple(detached) == (None, None, 0)
        finally:
            engine.dispose()

        with pytest.raises(RuntimeError, match="held story-arc sync work"):
            command.downgrade(cfg, _STORY_ARC_MANAGED_PLACEMENT_REVISION)

        assert "claimable" in _get_columns(sync_url, "story_arc_sync_work")
        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                assert (
                    conn.execute(
                        text("SELECT claimable FROM story_arc_sync_work WHERE id = :id"),
                        {"id": work_id},
                    ).scalar_one()
                    == 0
                )
                assert (
                    conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                    == _STORY_ARC_IMPORT_SYNC_REVISION
                )
        finally:
            engine.dispose()

    def test_import_owned_story_arc_sync_downgrade_refuses_pending_cancellation(
        self,
        alembic_cfg,
    ) -> None:
        """Downgrade cannot revive work whose cancellation has not yet been consumed."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")
        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                parents = _seed_story_arc_sync_work_parents(conn, slug="cancel-guard")
                work_id = _insert_story_arc_sync_work(
                    conn,
                    parents=parents,
                    generation="cancel-guard",
                    origin_action_id=None,
                    cancel_requested=True,
                )
        finally:
            engine.dispose()

        with pytest.raises(RuntimeError, match="pending story-arc sync cancellation"):
            command.downgrade(cfg, _STORY_ARC_MANAGED_PLACEMENT_REVISION)

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                assert (
                    conn.execute(
                        text("SELECT cancel_requested_at FROM story_arc_sync_work WHERE id = :id"),
                        {"id": work_id},
                    ).scalar_one()
                    is not None
                )
                assert (
                    conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                    == _STORY_ARC_IMPORT_SYNC_REVISION
                )
        finally:
            engine.dispose()

    def test_library_root_management_backfills_and_constrains_one_default(
        self,
        alembic_cfg,
    ) -> None:
        """Legacy roots gain both roles and the configured managed default."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, _MYLAR_PATH_CONFIRMATION_REVISION)
        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO library_roots "
                        "(name, path, enabled, created_at, updated_at) VALUES "
                        "('First', '/library/first', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
                        "('Configured', '/library/configured', 1, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
                        "('Disabled', '/library/disabled', 0, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )
                updated = conn.execute(
                    text(
                        "UPDATE system_config SET value = '/library/configured', "
                        "value_type = 'string' WHERE key = 'comics_directory'"
                    )
                )
                if updated.rowcount == 0:
                    conn.execute(
                        text(
                            "INSERT INTO system_config (key, value, value_type) VALUES "
                            "('comics_directory', '/library/configured', 'string')"
                        )
                    )
        finally:
            engine.dispose()

        command.upgrade(cfg, "head")
        engine = create_engine(sync_url)
        try:
            inspector = inspect(engine)
            columns = {column["name"]: column for column in inspector.get_columns("library_roots")}
            indexes = {index["name"]: index for index in inspector.get_indexes("library_roots")}
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT name, allow_referenced_registrations, "
                        "allow_managed_writes, is_default_managed_destination "
                        "FROM library_roots ORDER BY id"
                    )
                ).all()
            assert [tuple(row) for row in rows] == [
                ("First", 1, 1, 0),
                ("Configured", 1, 1, 1),
                ("Disabled", 1, 1, 0),
            ]
            assert columns["allow_referenced_registrations"]["nullable"] is False
            assert columns["allow_managed_writes"]["nullable"] is False
            assert columns["is_default_managed_destination"]["nullable"] is False
            assert columns["enabled"]["default"] is None
            assert indexes["uq_library_roots_default_managed_destination"]["unique"] == 1
            with engine.begin() as conn, pytest.raises(IntegrityError):
                conn.execute(
                    text(
                        "UPDATE library_roots SET is_default_managed_destination = 1 "
                        "WHERE name = 'First'"
                    )
                )
        finally:
            engine.dispose()

        command.downgrade(cfg, _MYLAR_PATH_CONFIRMATION_REVISION)
        root_columns = _get_columns(sync_url, "library_roots")
        assert "allow_referenced_registrations" not in root_columns
        assert "allow_managed_writes" not in root_columns
        assert "is_default_managed_destination" not in root_columns

        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE system_config SET value = '/library/missing' "
                        "WHERE key = 'comics_directory'"
                    )
                )
        finally:
            engine.dispose()
        command.upgrade(cfg, "head")
        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                assert (
                    conn.execute(
                        text(
                            "SELECT name FROM library_roots "
                            "WHERE is_default_managed_destination = 1"
                        )
                    ).scalar_one()
                    == "First"
                )
        finally:
            engine.dispose()

    @pytest.mark.parametrize(
        ("allow_referenced_registrations", "allow_managed_writes"),
        [(False, True), (True, False)],
    )
    def test_library_root_management_downgrade_refuses_nonrepresentable_roles(
        self,
        alembic_cfg,
        allow_referenced_registrations: bool,
        allow_managed_writes: bool,
    ) -> None:
        """A downgrade cannot erase either explicit library-root restriction."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, _LIBRARY_ROOT_MANAGEMENT_REVISION)
        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO library_roots "
                        "(name, path, enabled, allow_referenced_registrations, "
                        "allow_managed_writes, is_default_managed_destination, "
                        "created_at, updated_at) VALUES "
                        "('Managed', '/library/managed', 1, 1, 1, 1, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
                        "('Restricted', '/library/restricted', 1, "
                        ":allow_referenced_registrations, :allow_managed_writes, 0, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {
                        "allow_referenced_registrations": allow_referenced_registrations,
                        "allow_managed_writes": allow_managed_writes,
                    },
                )
                updated = conn.execute(
                    text(
                        "UPDATE system_config SET value = '/library/managed', "
                        "value_type = 'string' WHERE key = 'comics_directory'"
                    )
                )
                if updated.rowcount == 0:
                    conn.execute(
                        text(
                            "INSERT INTO system_config (key, value, value_type) VALUES "
                            "('comics_directory', '/library/managed', 'string')"
                        )
                    )
        finally:
            engine.dispose()

        with pytest.raises(RuntimeError, match="library-root capabilities"):
            command.downgrade(cfg, _MYLAR_PATH_CONFIRMATION_REVISION)

        assert "allow_managed_writes" in _get_columns(sync_url, "library_roots")
        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                assert (
                    conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                    == _LIBRARY_ROOT_MANAGEMENT_REVISION
                )
                conn.execute(
                    text(
                        "UPDATE library_roots SET allow_referenced_registrations = 1, "
                        "allow_managed_writes = 1 WHERE name = 'Restricted'"
                    )
                )
        finally:
            engine.dispose()

        command.downgrade(cfg, _MYLAR_PATH_CONFIRMATION_REVISION)
        command.upgrade(cfg, "head")
        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT name, allow_referenced_registrations, allow_managed_writes, "
                        "is_default_managed_destination FROM library_roots ORDER BY id"
                    )
                ).all()
            assert [tuple(row) for row in rows] == [
                ("Managed", 1, 1, 1),
                ("Restricted", 1, 1, 0),
            ]
        finally:
            engine.dispose()

    def test_library_root_management_downgrade_refuses_unrepresentable_default(
        self,
        alembic_cfg,
    ) -> None:
        """A downgrade cannot silently change the selected managed destination."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, _LIBRARY_ROOT_MANAGEMENT_REVISION)
        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO library_roots "
                        "(name, path, enabled, allow_referenced_registrations, "
                        "allow_managed_writes, is_default_managed_destination, "
                        "created_at, updated_at) VALUES "
                        "('Configured', '/library/configured', 1, 1, 1, 0, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
                        "('Selected', '/library/selected', 1, 1, 1, 1, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )
                updated = conn.execute(
                    text(
                        "UPDATE system_config SET value = '/library/configured', "
                        "value_type = 'string' WHERE key = 'comics_directory'"
                    )
                )
                if updated.rowcount == 0:
                    conn.execute(
                        text(
                            "INSERT INTO system_config (key, value, value_type) VALUES "
                            "('comics_directory', '/library/configured', 'string')"
                        )
                    )
        finally:
            engine.dispose()

        with pytest.raises(RuntimeError, match="default managed destination"):
            command.downgrade(cfg, _MYLAR_PATH_CONFIRMATION_REVISION)

        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                assert (
                    conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                    == _LIBRARY_ROOT_MANAGEMENT_REVISION
                )
                conn.execute(
                    text(
                        "UPDATE library_roots SET is_default_managed_destination = 0 "
                        "WHERE name = 'Selected'"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE library_roots SET is_default_managed_destination = 1 "
                        "WHERE name = 'Configured'"
                    )
                )
        finally:
            engine.dispose()

        command.downgrade(cfg, _MYLAR_PATH_CONFIRMATION_REVISION)
        command.upgrade(cfg, "head")
        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                assert (
                    conn.execute(
                        text(
                            "SELECT name FROM library_roots "
                            "WHERE is_default_managed_destination = 1"
                        )
                    ).scalar_one()
                    == "Configured"
                )
        finally:
            engine.dispose()

    def test_root_removal_protects_dependencies_without_losing_other_fk_actions(self, alembic_cfg):
        cfg, sync_url = alembic_cfg
        script = ScriptDirectory.from_config(cfg)
        assert script.get_heads() == ["n5h6i7j8k901"]
        assert script.get_revision("n5h6i7j8k901").down_revision == _IMPORT_JOB_ARCHIVE_REVISION
        command.upgrade(cfg, _IMPORT_JOB_ARCHIVE_REVISION)
        engine = create_engine(sync_url)

        def actions():
            with engine.connect() as conn:
                return {
                    (table, row[3]): row[6]
                    for table in (
                        "library_files",
                        "series",
                        "story_arcs",
                        "story_arc_placements",
                        "import_jobs",
                    )
                    for row in conn.exec_driver_sql(f'PRAGMA foreign_key_list("{table}")')
                }

        before = actions()
        command.upgrade(cfg, "head")
        after = actions()
        for (table, column), action in before.items():
            assert after[table, column] == (
                "RESTRICT"
                if column
                in {
                    "library_root_id",
                    "target_library_root_id",
                    "preferred_library_root_id",
                }
                else action
            )
        assert "removed_library_root_snapshot" in _get_columns(sync_url, "import_jobs")
        command.downgrade(cfg, _IMPORT_JOB_ARCHIVE_REVISION)
        assert actions() == before
        engine.dispose()

    def test_import_job_archival_extends_the_single_migration_head(
        self,
        alembic_cfg,
    ) -> None:
        """Import history archival extends the existing migration graph."""
        cfg, sync_url = alembic_cfg
        script = ScriptDirectory.from_config(cfg)

        assert (
            script.get_revision(_IMPORT_JOB_ARCHIVE_REVISION).down_revision
            == _IMPORT_FILE_DELETE_REFERENCE_INDEX_REVISION
        )

        command.upgrade(cfg, "head")
        engine = create_engine(sync_url)
        try:
            story_arc_columns = {
                column["name"]: column for column in inspect(engine).get_columns("story_arcs")
            }
            assert story_arc_columns["cover_path"]["nullable"] is True
            assert story_arc_columns["cover_url"]["nullable"] is True
            columns = {
                column["name"]: column for column in inspect(engine).get_columns("import_jobs")
            }
            import_job_indexes = {
                index["name"]: index for index in inspect(engine).get_indexes("import_jobs")
            }
            assert columns["archived_at"]["nullable"] is True
            assert import_job_indexes["ix_import_jobs_archived_created"]["column_names"] == [
                "archived_at",
                "created_at",
            ]
            assert columns["story_arc_import_requested"]["nullable"] is False
            assert columns["story_arc_materialization_requested"]["nullable"] is False
            assert columns["mylar3_path_map_confirmed"]["nullable"] is False
            with engine.begin() as conn:
                job_id = _seed_minimal_import_job(conn, source_path="/fixture/intent-defaults")
                defaults = conn.execute(
                    text(
                        "SELECT story_arc_import_requested, "
                        "story_arc_materialization_requested, mylar3_path_map_confirmed "
                        "FROM import_jobs WHERE id = :id"
                    ),
                    {"id": job_id},
                ).one()
                assert tuple(defaults) == (0, 0, 0)
        finally:
            engine.dispose()

        command.downgrade(cfg, _STORY_ARC_COVER_REVISION)
        downgraded_columns = _get_columns(sync_url, "import_jobs")
        assert "archived_at" not in downgraded_columns
        assert "mylar3_path_map_confirmed" not in downgraded_columns
        command.upgrade(cfg, "head")

        command.downgrade(cfg, _STORY_ARC_IMPORT_SYNC_REVISION)
        c4_columns = _get_columns(sync_url, "import_jobs")
        assert "story_arc_import_requested" not in c4_columns
        assert "story_arc_materialization_requested" not in c4_columns
        assert "story_arc_placement_followup_pending" in c4_columns
        assert "story_arc_rollback_waiting_work_id" in c4_columns

        command.downgrade(cfg, _STORY_ARC_MANAGED_PLACEMENT_REVISION)
        b3_columns = _get_columns(sync_url, "import_jobs")
        assert "story_arc_placement_followup_pending" not in b3_columns
        assert "story_arc_rollback_waiting_work_id" not in b3_columns

        command.upgrade(cfg, "head")

    def test_import_file_matched_issue_index_is_reversible(self, alembic_cfg) -> None:
        """Issue rollback uses an indexed import-file foreign-key lookup."""
        cfg, sync_url = alembic_cfg

        command.upgrade(cfg, _SERIES_PREFERRED_ROOT_REVISION)
        assert "ix_import_files_matched_issue_id" not in _get_indexes(sync_url, "import_files")

        command.upgrade(cfg, _IMPORT_FILE_MATCHED_ISSUE_INDEX_REVISION)
        assert _get_indexes(sync_url, "import_files")["ix_import_files_matched_issue_id"] == [
            "matched_issue_id"
        ]

        command.downgrade(cfg, _SERIES_PREFERRED_ROOT_REVISION)
        assert "ix_import_files_matched_issue_id" not in _get_indexes(sync_url, "import_files")

    def test_import_file_series_index_is_reversible(self, alembic_cfg) -> None:
        """Per-series matching and rollback use an indexed file lookup."""
        cfg, sync_url = alembic_cfg

        command.upgrade(cfg, _IMPORT_FILE_MATCHED_ISSUE_INDEX_REVISION)
        assert "ix_import_files_import_series_id" not in _get_indexes(sync_url, "import_files")

        command.upgrade(cfg, _IMPORT_FILE_SERIES_INDEX_REVISION)
        assert _get_indexes(sync_url, "import_files")["ix_import_files_import_series_id"] == [
            "import_series_id"
        ]

        command.downgrade(cfg, _IMPORT_FILE_MATCHED_ISSUE_INDEX_REVISION)
        assert "ix_import_files_import_series_id" not in _get_indexes(sync_url, "import_files")

    def test_import_file_delete_reference_indexes_are_reversible(self, alembic_cfg) -> None:
        """Large staged-job deletes use indexed foreign-key reference checks."""
        cfg, sync_url = alembic_cfg

        command.upgrade(cfg, _IMPORT_FILE_SERIES_INDEX_REVISION)
        assert "ix_import_files_duplicate_of_file_id" not in _get_indexes(sync_url, "import_files")
        assert "ix_import_story_arc_entries_import_file_id" not in _get_indexes(
            sync_url, "import_story_arc_entries"
        )

        command.upgrade(cfg, _IMPORT_FILE_DELETE_REFERENCE_INDEX_REVISION)
        assert _get_indexes(sync_url, "import_files")["ix_import_files_duplicate_of_file_id"] == [
            "duplicate_of_file_id"
        ]
        assert _get_indexes(sync_url, "import_story_arc_entries")[
            "ix_import_story_arc_entries_import_file_id"
        ] == ["import_file_id"]

        command.downgrade(cfg, _IMPORT_FILE_SERIES_INDEX_REVISION)
        assert "ix_import_files_duplicate_of_file_id" not in _get_indexes(sync_url, "import_files")
        assert "ix_import_story_arc_entries_import_file_id" not in _get_indexes(
            sync_url, "import_story_arc_entries"
        )

    def test_series_preferred_root_backfills_only_managed_destinations(
        self,
        alembic_cfg,
    ) -> None:
        """Existing managed series retain their root as the future destination."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, _LIBRARY_ROOT_MANAGEMENT_REVISION)
        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO library_roots "
                        "(name, path, enabled, allow_referenced_registrations, "
                        "allow_managed_writes, is_default_managed_destination, "
                        "created_at, updated_at) VALUES "
                        "('Managed', '/library/managed', 1, 1, 1, 1, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
                        "('Referenced', '/library/referenced', 1, 1, 0, 0, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )
                roots = dict(conn.execute(text("SELECT name, id FROM library_roots")).all())
                conn.execute(
                    text(
                        "INSERT INTO series "
                        "(title, sort_title, status, issue_count, monitored, series_type, "
                        "alternate_names, library_root_id, created_at, updated_at) VALUES "
                        "('Managed Series', 'managed series', 'CONTINUING', 0, 0, "
                        "'STANDARD', '[]', :managed_id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
                        "('Referenced Series', 'referenced series', 'CONTINUING', 0, 0, "
                        "'STANDARD', '[]', :referenced_id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
                        "('Unplaced Series', 'unplaced series', 'CONTINUING', 0, 0, "
                        "'STANDARD', '[]', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {
                        "managed_id": roots["Managed"],
                        "referenced_id": roots["Referenced"],
                    },
                )
        finally:
            engine.dispose()

        command.upgrade(cfg, "head")
        engine = create_engine(sync_url)
        try:
            inspector = inspect(engine)
            columns = {column["name"]: column for column in inspector.get_columns("series")}
            preferred_fks = [
                fk
                for fk in inspector.get_foreign_keys("series")
                if fk["constrained_columns"] == ["preferred_library_root_id"]
            ]
            with engine.connect() as conn:
                rows = conn.execute(
                    text("SELECT title, preferred_library_root_id FROM series ORDER BY title")
                ).all()
            assert columns["preferred_library_root_id"]["nullable"] is True
            assert len(preferred_fks) == 1
            assert preferred_fks[0]["referred_table"] == "library_roots"
            assert preferred_fks[0]["options"].get("ondelete") == "RESTRICT"
            assert [tuple(row) for row in rows] == [
                ("Managed Series", roots["Managed"]),
                ("Referenced Series", None),
                ("Unplaced Series", None),
            ]
        finally:
            engine.dispose()

        command.downgrade(cfg, _LIBRARY_ROOT_MANAGEMENT_REVISION)
        assert "preferred_library_root_id" not in _get_columns(sync_url, "series")
        command.upgrade(cfg, "head")

    @pytest.mark.parametrize("current_root_state", ["different", "missing"])
    def test_series_preferred_root_downgrade_refuses_unrepresentable_choice(
        self,
        alembic_cfg,
        current_root_state: str,
    ) -> None:
        """A downgrade cannot discard a preferred root not encoded by the old schema."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")
        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO library_roots "
                        "(name, path, enabled, allow_referenced_registrations, "
                        "allow_managed_writes, is_default_managed_destination, "
                        "created_at, updated_at) VALUES "
                        "('Preferred', '/library/preferred', 1, 1, 1, 1, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
                        "('Current', '/library/current', 1, 1, 1, 0, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )
                roots = dict(conn.execute(text("SELECT name, id FROM library_roots")).all())
                current_root_id = roots["Current"] if current_root_state == "different" else None
                conn.execute(
                    text(
                        "INSERT INTO series "
                        "(title, sort_title, status, issue_count, monitored, series_type, "
                        "alternate_names, library_root_id, preferred_library_root_id, "
                        "created_at, updated_at) VALUES "
                        "('Split Series', 'split series', 'CONTINUING', 0, 0, "
                        "'STANDARD', '[]', :current_root_id, :preferred_root_id, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {
                        "current_root_id": current_root_id,
                        "preferred_root_id": roots["Preferred"],
                    },
                )
        finally:
            engine.dispose()

        with pytest.raises(RuntimeError, match="preferred managed destination"):
            command.downgrade(cfg, _LIBRARY_ROOT_MANAGEMENT_REVISION)

        assert "preferred_library_root_id" in _get_columns(sync_url, "series")
        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                assert (
                    conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                    == _SERIES_PREFERRED_ROOT_REVISION
                )
                conn.execute(
                    text(
                        "UPDATE series SET preferred_library_root_id = library_root_id "
                        "WHERE title = 'Split Series'"
                    )
                )
        finally:
            engine.dispose()

        command.downgrade(cfg, _LIBRARY_ROOT_MANAGEMENT_REVISION)
        command.upgrade(cfg, "head")
        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT library_root_id, preferred_library_root_id "
                        "FROM series WHERE title = 'Split Series'"
                    )
                ).one()
            assert tuple(row) == (current_root_id, current_root_id)
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

    def test_import_jobs_has_durable_layout_snapshot_fields(self, alembic_cfg) -> None:
        """Import job choices survive background execution, restarts, and retries."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            inspector = inspect(engine)
            columns = {column["name"]: column for column in inspector.get_columns("import_jobs")}
        finally:
            engine.dispose()

        assert {
            "file_handling_mode",
            "source_layout_snapshot",
            "future_layout_requested",
            "future_root_policy_snapshot",
            "future_root_policy_applied_at",
        }.issubset(columns)
        assert columns["file_handling_mode"]["nullable"] is False
        assert columns["source_layout_snapshot"]["nullable"] is False
        assert columns["future_layout_requested"]["nullable"] is False
        assert columns["future_root_policy_snapshot"]["nullable"] is True
        assert columns["future_root_policy_applied_at"]["nullable"] is True

    def test_import_job_layout_snapshot_migration_backfills_existing_rows(
        self,
        alembic_cfg,
    ) -> None:
        """Pre-v1.3 import history receives the compatible managed/auto defaults."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "v7w8x9y0z123")

        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO import_jobs ("
                        "source_path, source_type, status, scan_total_files, scan_total_dirs, "
                        "series_found, series_duplicate, series_matched, series_no_match, "
                        "series_new, series_imported, series_failed, search_on_add, "
                        "cv_match_threshold, auto_accept_high_confidence, skip_no_match"
                        ") VALUES ("
                        "'/imports/history', 'FILESYSTEM', 'COMPLETED', 4, 2, "
                        "1, 0, 1, 0, 1, 1, 0, 0, 0.7, 1, 0"
                        ")"
                    )
                )
        finally:
            engine.dispose()

        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT file_handling_mode, source_layout_snapshot, "
                        "future_layout_requested, future_root_policy_snapshot, "
                        "future_root_policy_applied_at FROM import_jobs "
                        "WHERE source_path = '/imports/history'"
                    )
                ).one()
        finally:
            engine.dispose()

        assert row.file_handling_mode == "managed_copy"
        assert json.loads(row.source_layout_snapshot) == {
            "schema_version": 1,
            "mode": "auto",
            "preset": None,
            "series_path_template": None,
            "issue_filename_template": None,
            "selected_cluster_id": None,
            "fallback_to_auto": True,
        }
        assert row.future_layout_requested == 0
        assert row.future_root_policy_snapshot is None
        assert row.future_root_policy_applied_at is None

    def test_library_files_has_naming_snapshot(self, alembic_cfg) -> None:
        """Library files keep the naming inputs used at placement time."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        columns = _get_columns(sync_url, "library_files")

        assert "naming_snapshot" in columns

    def test_library_file_ownership_migration_backfills_managed_and_signatures(
        self,
        alembic_cfg,
    ) -> None:
        """Existing library/import file rows receive conservative ownership defaults."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "w8x9y0z1a234")

        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
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
                        "INSERT INTO library_files "
                        "(file_path, file_name, file_size, file_format, file_modified_at, "
                        "match_confidence, library_root_id, has_comicinfo, naming_snapshot, "
                        "created_at, updated_at) "
                        "VALUES ('/library/Batman/Batman 001.cbz', 'Batman 001.cbz', 100, "
                        "'CBZ', CURRENT_TIMESTAMP, 'HIGH', :root_id, 0, '{}', "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {"root_id": root_id},
                )
        finally:
            engine.dispose()

        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            inspector = inspect(engine)
            library_columns = {
                column["name"]: column for column in inspector.get_columns("library_files")
            }
            import_columns = {
                column["name"]: column for column in inspector.get_columns("import_files")
            }
            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT storage_mode, source_signature FROM library_files "
                        "WHERE file_path = '/library/Batman/Batman 001.cbz'"
                    )
                ).one()
        finally:
            engine.dispose()

        assert library_columns["storage_mode"]["nullable"] is False
        assert library_columns["source_signature"]["nullable"] is False
        assert import_columns["source_signature"]["nullable"] is False
        assert row.storage_mode == "managed"
        assert json.loads(row.source_signature) == {}

        command.downgrade(cfg, "w8x9y0z1a234")
        assert "storage_mode" not in _get_columns(sync_url, "library_files")
        assert "source_signature" not in _get_columns(sync_url, "library_files")
        assert "source_signature" not in _get_columns(sync_url, "import_files")
        command.upgrade(cfg, "head")

    def test_library_file_ownership_downgrade_blocks_referenced_rows(
        self,
        alembic_cfg,
    ) -> None:
        """A downgrade cannot erase ownership while user-owned references remain."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO library_roots "
                        "(name, path, enabled, allow_referenced_registrations, "
                        "allow_managed_writes, is_default_managed_destination, "
                        "created_at, updated_at) "
                        "VALUES ('Comics', '/library', 1, 1, 1, 1, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )
                root_id = conn.execute(text("SELECT last_insert_rowid()")).scalar()
                conn.execute(
                    text(
                        "INSERT INTO library_files "
                        "(file_path, file_name, file_size, file_format, file_modified_at, "
                        "match_confidence, library_root_id, has_comicinfo, naming_snapshot, "
                        "storage_mode, source_signature, created_at, updated_at) "
                        "VALUES ('/library/Batman/Batman 001.cbz', 'Batman 001.cbz', 100, "
                        "'CBZ', CURRENT_TIMESTAMP, 'HIGH', :root_id, 0, '{}', "
                        "'referenced', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {"root_id": root_id},
                )
        finally:
            engine.dispose()

        with pytest.raises(RuntimeError, match="referenced files remain"):
            command.downgrade(cfg, "w8x9y0z1a234")

        assert "storage_mode" in _get_columns(sync_url, "library_files")

    def test_library_root_policy_migration_is_unique_and_does_not_backfill(
        self,
        alembic_cfg,
    ) -> None:
        """Existing roots retain global fallback until an explicit policy is saved."""
        cfg, sync_url = alembic_cfg
        command.upgrade(cfg, "x9y0z1a2b345")

        engine = create_engine(sync_url)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO library_roots "
                        "(name, path, enabled, created_at, updated_at) "
                        "VALUES ('Existing', '/library/existing', 1, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )
        finally:
            engine.dispose()

        command.upgrade(cfg, "head")

        engine = create_engine(sync_url)
        try:
            inspector = inspect(engine)
            columns = {
                column["name"]: column for column in inspector.get_columns("library_root_policies")
            }
            unique_constraints = inspector.get_unique_constraints("library_root_policies")
            with engine.begin() as conn:
                root_id = conn.execute(
                    text("SELECT id FROM library_roots WHERE name = 'Existing'")
                ).scalar_one()
                assert (
                    conn.execute(text("SELECT COUNT(*) FROM library_root_policies")).scalar_one()
                    == 0
                )
                values = {
                    "root_id": root_id,
                    "series_path": "{Publisher}/{Series} ({Year})",
                    "comic": "{Series} {IssueTitle} Issue {Issue:03d}",
                    "annual": "{Series} Annual Issue {Issue:03d}",
                    "non_standard": "{Series} {Type} {Volume:02d} - {IssueTitle}",
                    "single": "{Series} {Type} - {IssueTitle}",
                }
                insert = text(
                    "INSERT INTO library_root_policies ("
                    "library_root_id, schema_version, series_path_template, "
                    "comic_file_template, annual_file_template, "
                    "non_standard_file_template, single_non_standard_file_template, "
                    "replace_illegal_characters, colon_replacement, source, revision, "
                    "created_at, updated_at) VALUES ("
                    ":root_id, 1, :series_path, :comic, :annual, :non_standard, :single, "
                    "1, 'dash', 'manual', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
                conn.execute(insert, values)
                with pytest.raises(IntegrityError):
                    conn.execute(insert, values)
        finally:
            engine.dispose()

        assert set(columns) == {
            "id",
            "library_root_id",
            "schema_version",
            "series_path_template",
            "comic_file_template",
            "annual_file_template",
            "non_standard_file_template",
            "single_non_standard_file_template",
            "replace_illegal_characters",
            "colon_replacement",
            "source",
            "source_import_job_id",
            "revision",
            "created_at",
            "updated_at",
        }
        assert columns["source_import_job_id"]["nullable"] is True
        assert any(
            constraint["column_names"] == ["library_root_id"] for constraint in unique_constraints
        )

        command.downgrade(cfg, "x9y0z1a2b345")
        engine = create_engine(sync_url)
        try:
            assert "library_root_policies" not in inspect(engine).get_table_names()
        finally:
            engine.dispose()
        command.upgrade(cfg, "head")

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
