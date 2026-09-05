"""Root FK changes preserve data through upgrade, downgrade, and upgrade again."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_root_removal_migration_preserves_dependent_rows(monkeypatch):  # type: ignore[no-untyped-def]
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic/versions/n5h6i7j8k901_protect_library_root_removal.py"
    )
    spec = importlib.util.spec_from_file_location("root_removal_migration", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        # Model/service tests may install the runtime Engine connect listener.
        # Match the isolated Alembic process, not a running app's connection.
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.exec_driver_sql("CREATE TABLE library_roots (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql("INSERT INTO library_roots VALUES (1)")
        for table, column, old_action in [
            ("library_files", "library_root_id", "CASCADE"),
            ("series", "library_root_id", "SET NULL"),
            ("story_arcs", "target_library_root_id", "SET NULL"),
            ("story_arc_placements", "library_root_id", "SET NULL"),
            ("import_jobs", "target_library_root_id", "SET NULL"),
        ]:
            conn.exec_driver_sql(
                f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, "
                f"{column} INTEGER REFERENCES library_roots(id) ON DELETE {old_action})"
            )
            conn.exec_driver_sql(f"INSERT INTO {table} VALUES (1, 1)")
        conn.exec_driver_sql(
            "ALTER TABLE series ADD COLUMN preferred_library_root_id "
            "INTEGER REFERENCES library_roots(id) ON DELETE SET NULL"
        )
        conn.exec_driver_sql("UPDATE series SET preferred_library_root_id=1")
        conn.exec_driver_sql(
            "CREATE TABLE child (id INTEGER PRIMARY KEY, "
            "file_id INTEGER REFERENCES library_files(id) ON DELETE CASCADE)"
        )
        conn.exec_driver_sql("INSERT INTO child VALUES (1, 1)")
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(conn)))
        for upgrade in (True, False, True):
            migration.upgrade() if upgrade else migration.downgrade()
            for table in migration._TABLES:
                # SQLite's FK list includes inline actions that SQLAlchemy's
                # SQL-text reflection does not always recover correctly.
                keys = conn.exec_driver_sql(f'PRAGMA foreign_key_list("{table}")').all()
                assert keys
                assert all(
                    k[6]
                    == (
                        "RESTRICT"
                        if upgrade
                        else "CASCADE"
                        if table == "library_files"
                        else "SET NULL"
                    )
                    for k in keys
                )
                assert conn.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar() == 1
            assert conn.exec_driver_sql("SELECT file_id FROM child").scalar() == 1
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        conn.exec_driver_sql("UPDATE import_jobs SET removed_library_root_snapshot='{\"id\": 1}'")
        with pytest.raises(RuntimeError, match="import history"):
            migration.downgrade()
    engine.dispose()
