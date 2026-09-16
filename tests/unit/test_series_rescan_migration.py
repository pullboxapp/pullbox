"""The job-type extension preserves existing jobs and their dependent reports."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine


def test_rescan_job_type_migration_preserves_history(monkeypatch):
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic/versions/o6i7j8k9l012_add_series_rescan_job_type.py"
    )
    spec = importlib.util.spec_from_file_location("series_rescan_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql(
            "CREATE TABLE utility_jobs (id TEXT PRIMARY KEY, job_type TEXT, "
            "CONSTRAINT ck_utility_jobs_job_type CHECK (job_type IN ('db_check_cleanup')))"
        )
        connection.exec_driver_sql(
            "CREATE TABLE children (id INTEGER PRIMARY KEY, "
            "job_id TEXT REFERENCES utility_jobs(id) ON DELETE CASCADE)"
        )
        connection.exec_driver_sql(
            "INSERT INTO utility_jobs VALUES ('existing', 'db_check_cleanup')"
        )
        connection.exec_driver_sql("INSERT INTO children VALUES (1, 'existing')")
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        connection.exec_driver_sql("INSERT INTO utility_jobs VALUES ('scan', 'series_rescan')")
        assert connection.exec_driver_sql("SELECT job_id FROM children").scalar_one() == "existing"
        with pytest.raises(RuntimeError, match="Remove saved series rescan jobs"):
            migration.downgrade()
        connection.exec_driver_sql("DELETE FROM utility_jobs WHERE id='scan'")
        migration.downgrade()
        migration.upgrade()
        assert connection.exec_driver_sql("SELECT job_id FROM children").scalar_one() == "existing"
        assert not connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    engine.dispose()
