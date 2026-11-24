"""Run the same SQLite import and recovery contracts against fresh PostgreSQL libraries."""

from uuid import uuid4

import pytest

from pullbox import database
from pullbox.config import get_settings
from tests.integration import test_mylar_native_extras as extras
from tests.integration import test_mylar_recorded_native_identity as recorded
from tests.integration.test_database_migration_target import _migrate, disposable_server

test_mixed_folder = (
    extras.test_unrecorded_native_mylar_file_imports_without_claiming_recorded_neighbors
)
test_conflict_followup = recorded.test_completed_mylar_conflict_links_only_current_verified_issue
test_recheck_at_placement = recorded.test_recorded_mylar_rechecks_native_links_before_placement
test_recorded_match = (
    recorded.test_recorded_mylar_match_preserved_without_proven_native_disagreement
)


@pytest.fixture(scope="module", params=[16, 18])
def migrated_database(request):
    with disposable_server(request.param) as (_, (_, password, port, sql)):
        url = f"postgresql+asyncpg://probe_owner:{password}@127.0.0.1:{port}"
        sql("postgres", "CREATE DATABASE mylar_template OWNER probe_owner TEMPLATE template0")
        _migrate(f"{url}/mylar_template", "upgrade", "head")
        yield url, sql


@pytest.fixture
async def db_session(migrated_database, monkeypatch):
    url, sql = migrated_database
    name = f"mylar_{uuid4().hex}"
    sql("postgres", f"CREATE DATABASE {name} OWNER probe_owner TEMPLATE mylar_template")
    # The importer opens independent sessions for progress and placement. They
    # must see this same database, not the test process's fallback SQLite file.
    monkeypatch.setenv("PULLBOX_DB_URL", f"{url}/{name}")
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_session_factory", None)
    get_settings.cache_clear()
    try:
        async with database.get_session_factory()() as session:
            yield session
    finally:
        await database.dispose_engine()
        get_settings.cache_clear()
        sql("postgres", f"DROP DATABASE {name}")
