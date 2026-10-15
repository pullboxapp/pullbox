"""Owned SQLite handles close deterministically, including failed operations."""

import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from pullbox.core.https_runtime import load_https_db_values
from pullbox.services.catalog.database import open_readonly
from pullbox.services.database_optimization_service import DatabaseOptimizationService
from pullbox.services.import_mylar3_paths import auto_detect_mylar3_path_map
from scripts.import_scale_fixtures.generator import (
    FixtureRequest,
    _open_catalog,
    plan_import_scale_fixture,
)
from tests.api.test_gcd_local import gcd_dump
from tests.catalog_fixtures import build_patch, build_snapshot
from tests.unit.test_gcd_local_arcs import arc_dump
from tests.unit.test_gcd_local_credits import credited_dump
from tests.unit.test_import_scale_fixture_generator import _build_catalog


@pytest.fixture
def owned_connections(monkeypatch):
    """Keep handles alive so garbage collection cannot satisfy the close contract."""
    real_connect = sqlite3.connect
    connections = []

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    yield connections
    for connection in connections:
        connection.close()


def assert_closed(connections):
    assert connections, "The operation must acquire a real SQLite connection"
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            sqlite3.Connection.execute(connection, "SELECT 1")


@pytest.mark.parametrize("builder", [build_snapshot, gcd_dump, credited_dump, arc_dump])
@pytest.mark.parametrize("fail", [False, True], ids=["success", "schema-error"])
def test_catalog_fixture_closes_owned_connections(tmp_path, owned_connections, builder, fail):
    path = tmp_path / "catalog.db"
    builder(path)
    if fail:
        with pytest.raises(sqlite3.OperationalError, match="already exists"):
            builder(path)
    assert_closed(owned_connections)


@pytest.mark.parametrize("owner", ["fixture-catalog", "maintenance-read", "maintenance-write"])
def test_failed_connection_configuration_closes_handle(
    tmp_path, owned_connections, monkeypatch, owner
):
    path = tmp_path / "config.db"
    path.touch()
    tracked_connect = sqlite3.connect

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            raise sqlite3.OperationalError("injected configuration failure")

    def connect(*args, **kwargs):
        return tracked_connect(*args, factory=FailingConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError, match="injected configuration failure"):
        if owner == "fixture-catalog":
            _open_catalog(path)
        else:
            DatabaseOptimizationService(path)._connect(read_only=owner == "maintenance-read")
    assert_closed(owned_connections)


@pytest.mark.parametrize("failure", [None, "query", "third-open"])
def test_patch_fixture_closes_all_acquired_connections(tmp_path, owned_connections, failure):
    base = tmp_path / "base.db"
    target = tmp_path / "target.db"
    build_snapshot(base)
    build_snapshot(target, version="20260914T050000Z", extra_issue=True)
    # Inspect patch ownership separately from the builders covered above.
    for connection in owned_connections:
        connection.close()
    owned_connections.clear()
    if failure == "query":
        with closing(sqlite3.connect(base)) as db, db:
            db.execute("ALTER TABLE issues RENAME COLUMN issue_number TO missing_number")
    elif failure == "third-open":
        target = tmp_path / "missing-directory" / "target.db"

    if failure:
        with pytest.raises(sqlite3.Error):
            build_patch(tmp_path / "patch.db", base, target)
    else:
        build_patch(tmp_path / "patch.db", base, target)
    assert_closed(owned_connections)


@pytest.mark.parametrize("missing_table", [False, True], ids=["success", "query-error"])
def test_https_startup_read_closes_connection(tmp_path, owned_connections, missing_table):
    path = tmp_path / "settings.db"
    with closing(sqlite3.connect(path)) as db, db:
        if not missing_table:
            db.execute("CREATE TABLE system_config (key TEXT, value TEXT)")
            db.execute("INSERT INTO system_config VALUES ('https_enabled', 'true')")
    settings = SimpleNamespace(db_url=f"sqlite+aiosqlite:///{path}")

    assert load_https_db_values(settings) == ({} if missing_table else {"https_enabled": "true"})
    assert_closed(owned_connections)


def test_catalog_open_closes_connection_when_configuration_fails(tmp_path, owned_connections):
    path = tmp_path / "invalid.db"
    path.write_bytes(b"not a SQLite database")
    with pytest.raises(sqlite3.DatabaseError, match="not a database"):
        open_readonly(path)
    assert_closed(owned_connections)


@pytest.mark.parametrize("missing_table", [False, True], ids=["success", "query-error"])
def test_mylar_path_detection_closes_connection(tmp_path, owned_connections, missing_table):
    path = tmp_path / "mylar.db"
    with closing(sqlite3.connect(path)) as db, db:
        if not missing_table:
            db.execute("CREATE TABLE comics (ComicLocation TEXT)")
    assert auto_detect_mylar3_path_map(path) is None
    assert_closed(owned_connections)


@pytest.mark.parametrize("fail", [False, True], ids=["success", "query-error"])
def test_scale_fixture_plan_closes_catalog(tmp_path, owned_connections, fail):
    path = tmp_path / "catalog.db"
    _build_catalog(path)
    if fail:
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("DROP TABLE cv_issue")
    request = FixtureRequest(path, tmp_path / "unused", series_count=1, file_count=1)
    if fail:
        with pytest.raises(sqlite3.OperationalError, match="cv_issue"):
            plan_import_scale_fixture(request)
    else:
        assert len(plan_import_scale_fixture(request).series) == 1
    assert_closed(owned_connections)


@pytest.mark.parametrize("operation", ["preview", "optimize", "maintain"])
@pytest.mark.parametrize("fail", [False, True], ids=["success", "query-error"])
def test_maintenance_closes_connections(tmp_path, owned_connections, monkeypatch, operation, fail):
    path = tmp_path / "maintenance.db"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE records (id INTEGER)")
    tracked_connect = sqlite3.connect

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            target = "PRAGMA page_count" if operation == "preview" else "PRAGMA wal_checkpoint"
            if fail and sql.startswith(target):
                raise sqlite3.OperationalError("injected maintenance query failure")
            return super().execute(sql, *args, **kwargs)

    def connect(*args, **kwargs):
        return tracked_connect(*args, factory=FailingConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    run = getattr(DatabaseOptimizationService(path), operation)
    if fail:
        with pytest.raises(sqlite3.OperationalError, match="injected maintenance query failure"):
            run()
    else:
        run()
    assert_closed(owned_connections)
