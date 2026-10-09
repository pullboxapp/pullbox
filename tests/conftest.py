"""Shared test fixtures — async SQLAlchemy session with in-memory SQLite."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.providers.base import ReleaseResult
from tests.sqlite_schema import create_test_schema

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

# Exclude macOS Finder copy artifacts ("file 2.py", "file 3.py") from collection
collect_ignore_glob = ["**/* [0-9].*", "**/* [0-9][0-9].*"]


def _configure_worker_runtime_environment() -> None:
    """Point runtime defaults at a safe per-worker temp sandbox.

    A subset of middleware and service paths still consult bootstrap settings
    before test-scoped dependency overrides are applied. On CI runners, the
    production defaults like ``/data`` and ``/comics`` may not exist, which can
    make otherwise isolated tests fail only in the full xdist matrix. Give each
    worker a stable writable sandbox so those fallback code paths stay harmless.
    """

    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "main")
    invocation_id = os.environ.setdefault("_PULLBOX_PYTEST_RUN_UID", uuid4().hex)
    run_id = os.environ.get("PYTEST_XDIST_TESTRUNUID", invocation_id)
    # Worker names repeat across jobs, and workers inherit the controller's env.
    # Hash the namespace to keep paths bounded and independent of env spelling.
    namespace = hashlib.sha256(f"{run_id}/{worker_id}".encode()).hexdigest()[:24]
    temp_root = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir())
    runtime_root = temp_root / "pullbox-pytest-runtime" / namespace
    data_dir = runtime_root / "data"
    library_root = runtime_root / "library"
    logs_dir = data_dir / "logs"
    temp_dir = data_dir / "tmp"
    backup_dir = data_dir / "backups"
    covers_dir = library_root / ".covers"

    for path in (data_dir, library_root, logs_dir, temp_dir, backup_dir, covers_dir):
        path.mkdir(parents=True, exist_ok=True)

    defaults = {
        "PULLBOX_DATA_DIR": str(data_dir),
        "PULLBOX_LIBRARY_ROOT": str(library_root),
        "PULLBOX_LOGS_DIR": str(logs_dir),
        "PULLBOX_TEMP_DIR": str(temp_dir),
        "PULLBOX_BACKUP_DIR": str(backup_dir),
        "PULLBOX_COVERS_DIR": str(covers_dir),
        "PULLBOX_DB_URL": f"sqlite+aiosqlite:///{data_dir / 'pullbox.db'}",
        "PULLBOX_ALLOW_WEAK_SECRET_FOR_TESTS": "true",
    }
    inherited = json.loads(os.environ.get("_PULLBOX_PYTEST_DEFAULTS", "{}"))
    generated = {}
    for key, value in defaults.items():
        # Replace only defaults we generated, never explicit caller overrides.
        if key not in os.environ or os.environ[key] == inherited.get(key):
            os.environ[key] = value
            generated[key] = value
    os.environ["_PULLBOX_PYTEST_DEFAULTS"] = json.dumps(generated)


_configure_worker_runtime_environment()


def pytest_make_parametrize_id(val: object, argname: str) -> str | None:
    """Never serialize large adversarial payloads into test names or CI logs."""
    if isinstance(val, (str, bytes)) and len(val) > 128:
        payload = val.encode("utf-8", errors="backslashreplace") if isinstance(val, str) else val
        digest = hashlib.sha256(payload).hexdigest()[:12]
        return f"{argname}-{type(val).__name__}-{len(val)}-{digest}"
    return None


@pytest.fixture
def paired_import_writer_setting(monkeypatch, request):
    from pullbox.config import get_settings

    enabled = getattr(request, "param", True)
    monkeypatch.setenv("PULLBOX_METADATA_PAIRED_IMPORT_WRITER_ENABLED", str(enabled))
    get_settings.cache_clear()
    try:
        yield enabled
    finally:
        get_settings.cache_clear()


# ── Event loop isolation ─────────────────────────────────────────────
# The e2e tests run a live uvicorn server in a daemon thread. That
# thread's event loop can leak into the main thread's C-level
# ``_running_loop`` state, causing "Cannot run the event loop while
# another loop is running" errors in subsequent async tests.
# These hooks clear the stale reference at every phase boundary.


_saw_e2e = False


def _clear_running_loop() -> None:
    """Clear any stale C-level running-loop reference."""
    try:
        if asyncio._get_running_loop() is not None:  # type: ignore[attr-defined]
            asyncio._set_running_loop(None)  # type: ignore[attr-defined]
    except AttributeError:
        pass


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Clear stale running-loop when transitioning away from e2e tests.

    E2e tests (Playwright + live uvicorn) can leave the C-level
    ``_running_loop`` set, which prevents pytest-asyncio from creating
    new event loops for subsequent async tests. We only clear it after
    e2e tests have run and we're entering a non-e2e test.
    """
    global _saw_e2e
    if "/e2e/" in str(item.fspath):
        _saw_e2e = True
    elif _saw_e2e:
        _clear_running_loop()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-mark non-unit tests as slow for fast/slow test splitting.

    Tests outside tests/unit/ get the ``slow`` marker so developers can run
    ``pytest tests/unit/ -v`` for fast iteration (~15-20s) or
    ``pytest tests/ -v -m "not slow"`` to skip integration/API/UI tests.
    """
    slow_marker = pytest.mark.slow
    for item in items:
        if "/unit/" not in str(item.fspath):
            item.add_marker(slow_marker)


@pytest.fixture
async def async_engine():
    """Create a fresh in-memory async SQLite engine with all tables."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    try:
        async with engine.begin() as conn:
            await create_test_schema(conn)
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_session(async_engine) -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional async session that rolls back after each test."""
    session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
        await session.rollback()


@pytest.fixture
def nzbgeek_issues():
    """Real-world NZB issue titles from NZBGeek."""
    path = Path(__file__).parent / "fixtures" / "nzbgeek_test_fixtures.json"
    data = json.loads(path.read_text())
    return data["issue_titles"]


@pytest.fixture
def nzbgeek_non_issues():
    """Real-world NZB non-issue titles from NZBGeek."""
    path = Path(__file__).parent / "fixtures" / "nzbgeek_test_fixtures.json"
    data = json.loads(path.read_text())
    return data["non_issue_titles"]


@pytest.fixture
def nzbgeek_issue_results():
    """Real-world NZB issue results with full metadata from NZBGeek."""
    path = Path(__file__).parent / "fixtures" / "nzbgeek_test_fixtures.json"
    data = json.loads(path.read_text())
    return data["issue_results"]


@pytest.fixture
def nzbgeek_non_issue_results():
    """Real-world NZB non-issue results with full metadata from NZBGeek."""
    path = Path(__file__).parent / "fixtures" / "nzbgeek_test_fixtures.json"
    data = json.loads(path.read_text())
    return data["non_issue_results"]


@pytest.fixture
def prowlarr_issues():
    """Real-world issue titles from Prowlarr (multi-indexer, Usenet + torrent)."""
    path = Path(__file__).parent / "fixtures" / "prowlarr_test_fixtures.json"
    data = json.loads(path.read_text())
    return data["issue_titles"]


@pytest.fixture
def prowlarr_non_issues():
    """Real-world non-issue titles from Prowlarr."""
    path = Path(__file__).parent / "fixtures" / "prowlarr_test_fixtures.json"
    data = json.loads(path.read_text())
    return data["non_issue_titles"]


@pytest.fixture
def prowlarr_issue_results():
    """Real-world issue results with full metadata from Prowlarr."""
    path = Path(__file__).parent / "fixtures" / "prowlarr_test_fixtures.json"
    data = json.loads(path.read_text())
    return data["issue_results"]


@pytest.fixture
def prowlarr_torrent_results():
    """Torrent-only results from Prowlarr with seeders/leechers."""
    path = Path(__file__).parent / "fixtures" / "prowlarr_test_fixtures.json"
    data = json.loads(path.read_text())
    all_results = data["issue_results"] + data["non_issue_results"]
    return [r for r in all_results if r.get("protocol") == "torrent"]


def make_release(
    title: str,
    size_mb: float = 100.0,
    age_days: int = 5,
    seeders: int | None = None,
    is_torrent: bool = False,
) -> ReleaseResult:
    """Create a mock ReleaseResult for testing."""
    return ReleaseResult(
        title=title,
        indexer_name="TestIndexer",
        download_url=f"https://test.com/nzb/{title.replace(' ', '_')}",
        size_bytes=int(size_mb * 1024 * 1024),
        age_days=age_days,
        seeders=seeders,
        leechers=None,
        grabs=10,
        is_torrent=is_torrent,
        category="7030",
        published_at=datetime.now(UTC) - timedelta(days=age_days),
    )
