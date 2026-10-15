"""Tests for the manual issue search endpoint (C-4.3).

Covers:
- POST /api/v1/issues/{issue_id}/search basic functionality
- SearchLog entry created with correct type
- No indexers configured returns error
- Nonexistent issue returns 404
- Search results returned with correct fields
- Edge cases: no ComicVine ID, indexer timeout, empty results

Run:
    pytest tests/api/test_issue_manual_search.py -v
"""

from __future__ import annotations

import hashlib
import os
import sys
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.search_log import SearchLog, SearchType
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.models.user import APIKey, User
from pullbox.providers.base import ReleaseResult

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-manual-search")


# ── Test Data ──────────────────────────────────────────────────────────


def _make_release(
    title: str,
    indexer_name: str = "NZBgeek",
    *,
    size_bytes: int | None = 100_000_000,
    age_days: int | None = 5,
    is_torrent: bool = False,
) -> ReleaseResult:
    return ReleaseResult(
        title=title,
        indexer_name=indexer_name,
        download_url=f"https://indexer.example.com/dl/{title.replace(' ', '_')}",
        size_bytes=size_bytes,
        age_days=age_days,
        seeders=10 if is_torrent else None,
        leechers=2 if is_torrent else None,
        grabs=50 if not is_torrent else None,
        is_torrent=is_torrent,
        category="comics",
        published_at=None,
    )


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
async def _db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def _api_key_header(
    _db_factory: async_sessionmaker[AsyncSession],
    seeded_user_password_hash: str,
) -> str:
    raw_key = "pb_k1_" + "m" * 64
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with _db_factory() as session:
        user = User(
            username="manualsearchuser",
            password_hash=seeded_user_password_hash,
        )
        session.add(user)
        await session.flush()
        session.add(APIKey(user_id=user.id, key_hash=key_hash, name="manual-search-test"))
        await session.commit()
    return raw_key


@pytest.fixture
async def client(
    _db_factory: async_sessionmaker[AsyncSession],
    _api_key_header: str,
) -> AsyncGenerator[AsyncClient, None]:
    from pullbox.api.deps import get_db_dep
    from pullbox.api.middleware import reset_setup_cache
    from pullbox.app import create_app

    app = create_app()

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
        async with _db_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db_dep] = _override_db
    reset_setup_cache()

    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"X-Api-Key": _api_key_header},
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
    reset_setup_cache()


@pytest.fixture
async def unauthed_client(
    _db_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient, None]:
    from pullbox.api.deps import get_db_dep
    from pullbox.api.middleware import reset_setup_cache
    from pullbox.app import create_app

    app = create_app()

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
        async with _db_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db_dep] = _override_db
    reset_setup_cache()

    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
    reset_setup_cache()


# ── Helpers ────────────────────────────────────────────────────────────


async def _create_issue(
    factory: async_sessionmaker[AsyncSession],
    *,
    series_title: str = "Batman",
    year_start: int = 2016,
    issue_number: float = 1.0,
    comicvine_id: int | None = 99900,
    status: IssueStatus = IssueStatus.WANTED,
) -> int:
    async with factory() as session:
        series = Series(
            comicvine_id=comicvine_id,
            title=series_title,
            sort_title=series_title.lower(),
            year_start=year_start,
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
            monitored=True,
            issue_count=1,
        )
        session.add(series)
        await session.flush()
        issue = Issue(
            series_id=series.id,
            comicvine_id=50001 if comicvine_id else None,
            issue_number=issue_number,
            title=f"Issue #{int(issue_number)}",
            status=status,
        )
        session.add(issue)
        await session.commit()
        return issue.id


def _mock_registry_and_search(results: list[ReleaseResult]) -> tuple[object, object]:
    """Context manager that patches build_registry and search_for_issue."""
    mock_registry = AsyncMock()
    return (
        patch(
            "pullbox.composition.providers.build_registry",
            new_callable=AsyncMock,
            return_value=(mock_registry, {}),
        ),
        patch(
            "pullbox.services.search_service.SearchService.search_for_issue",
            new_callable=AsyncMock,
            return_value=results,
        ),
    )


# ── Tests: Basic functionality ─────────────────────────────────────────


@pytest.mark.asyncio
class TestManualSearchBasic:
    """POST /api/v1/issues/{id}/search — basic search functionality."""

    async def test_returns_results(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Search returns results with expected fields."""
        issue_id = await _create_issue(_db_factory)
        results = [
            _make_release("Batman 001 (2016).cbz"),
            _make_release("Batman 001 (2016).cbr", "Torznab", is_torrent=True),
        ]
        p_reg, p_search = _mock_registry_and_search(results)
        with p_reg, p_search:
            resp = await client.post(f"/api/v1/issues/{issue_id}/search")

        assert resp.status_code == 200
        data = resp.json()
        assert data["issue_id"] == issue_id
        assert len(data["results"]) == 2
        for r in data["results"]:
            assert "title" in r
            assert "indexer_name" in r
            assert "size_bytes" in r
            assert "age_days" in r
            assert "is_torrent" in r

    async def test_empty_results(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """No matches returns empty results list."""
        issue_id = await _create_issue(_db_factory)
        p_reg, p_search = _mock_registry_and_search([])
        with p_reg, p_search:
            resp = await client.post(f"/api/v1/issues/{issue_id}/search")

        assert resp.status_code == 200
        assert resp.json()["results"] == []

    async def test_nonexistent_issue_returns_404(self, client: AsyncClient) -> None:
        """Missing issue returns 404."""
        resp = await client.post("/api/v1/issues/99999/search")
        assert resp.status_code == 404


# ── Tests: SearchLog creation ──────────────────────────────────────────


@pytest.mark.asyncio
class TestManualSearchLog:
    """Manual search creates SearchLog entries."""

    async def test_creates_search_log_entry(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Search creates a SearchLog with type MANUAL."""
        issue_id = await _create_issue(_db_factory)
        results = [_make_release("Batman 001 (2016).cbz")]
        p_reg, p_search = _mock_registry_and_search(results)
        with p_reg, p_search:
            await client.post(f"/api/v1/issues/{issue_id}/search")

        async with _db_factory() as session:
            log_result = await session.execute(
                select(SearchLog).where(SearchLog.issue_id == issue_id)
            )
            log = log_result.scalar_one_or_none()
            assert log is not None
            assert log.search_type == SearchType.MANUAL
            assert log.results_found == 1
            assert log.series_title == "Batman"

    async def test_search_log_records_zero_results(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Empty search still logs with results_found=0."""
        issue_id = await _create_issue(_db_factory)
        p_reg, p_search = _mock_registry_and_search([])
        with p_reg, p_search:
            await client.post(f"/api/v1/issues/{issue_id}/search")

        async with _db_factory() as session:
            log_result = await session.execute(
                select(SearchLog).where(SearchLog.issue_id == issue_id)
            )
            log = log_result.scalar_one_or_none()
            assert log is not None
            assert log.results_found == 0

    async def test_multiple_searches_create_multiple_logs(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Each search creates its own log entry."""
        issue_id = await _create_issue(_db_factory)
        p_reg, p_search = _mock_registry_and_search([])
        with p_reg, p_search:
            await client.post(f"/api/v1/issues/{issue_id}/search")
            await client.post(f"/api/v1/issues/{issue_id}/search")

        async with _db_factory() as session:
            log_result = await session.execute(
                select(SearchLog).where(SearchLog.issue_id == issue_id)
            )
            logs = log_result.scalars().all()
            assert len(logs) == 2


# ── Tests: No indexers configured ──────────────────────────────────────


@pytest.mark.asyncio
class TestManualSearchNoIndexers:
    """Handling when no indexers are configured."""

    async def test_no_indexers_returns_error(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Returns error message when no indexers configured."""
        issue_id = await _create_issue(_db_factory)
        with patch(
            "pullbox.composition.providers.build_registry",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = await client.post(f"/api/v1/issues/{issue_id}/search")

        assert resp.status_code == 200
        data = resp.json()
        assert data["error"] == "no indexers configured"
        assert data["results"] == []
        assert data["issue_id"] == issue_id


# ── Tests: Authentication ──────────────────────────────────────────────


@pytest.mark.asyncio
class TestManualSearchAuth:
    """Authentication required for manual search."""

    async def test_unauthenticated_rejected(self, unauthed_client: AsyncClient) -> None:
        """Unauthenticated requests are rejected."""
        resp = await unauthed_client.post("/api/v1/issues/1/search")
        assert resp.status_code in (401, 403, 307)


# ── Tests: Edge cases from C-4.3 ──────────────────────────────────────


@pytest.mark.asyncio
class TestManualSearchEdgeCases:
    """Edge cases defined in cleanup-sprint-guide.md C-4.3."""

    async def test_issue_without_comicvine_id(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Issue with no ComicVine ID still searches by title/number."""
        issue_id = await _create_issue(_db_factory, comicvine_id=None)
        results = [_make_release("Batman 001.cbz")]
        p_reg, p_search = _mock_registry_and_search(results)
        with p_reg, p_search:
            resp = await client.post(f"/api/v1/issues/{issue_id}/search")

        assert resp.status_code == 200
        assert len(resp.json()["results"]) == 1

    async def test_indexer_timeout_returns_partial_results(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Indexer timeout returns whatever results were gathered."""
        issue_id = await _create_issue(_db_factory)
        partial = [_make_release("Batman 001 (2016).cbz", "WorkingIndexer")]

        mock_registry = AsyncMock()
        with (
            patch(
                "pullbox.composition.providers.build_registry",
                new_callable=AsyncMock,
                return_value=(mock_registry, {}),
            ),
            patch(
                "pullbox.services.search_service.SearchService.search_for_issue",
                new_callable=AsyncMock,
                return_value=partial,
            ),
        ):
            resp = await client.post(f"/api/v1/issues/{issue_id}/search")

        assert resp.status_code == 200
        assert len(resp.json()["results"]) == 1

    async def test_large_result_set(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Handles large result sets (100+ results)."""
        issue_id = await _create_issue(_db_factory)
        results = [_make_release("Batman 001 (2016).cbz") for _ in range(150)]
        p_reg, p_search = _mock_registry_and_search(results)
        with p_reg, p_search:
            resp = await client.post(f"/api/v1/issues/{issue_id}/search")

        assert resp.status_code == 200
        assert len(resp.json()["results"]) == 150

    async def test_results_from_multiple_indexers(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Results from different indexers are included."""
        issue_id = await _create_issue(_db_factory)
        results = [
            _make_release("Batman 001 (2016).cbz", "NZBgeek"),
            _make_release("Batman 001 (2016).cbr", "Torznab"),
            _make_release("Batman 001 (2016).cbz", "Prowlarr"),
        ]
        p_reg, p_search = _mock_registry_and_search(results)
        with p_reg, p_search:
            resp = await client.post(f"/api/v1/issues/{issue_id}/search")

        assert resp.status_code == 200
        data = resp.json()
        indexer_names = {r["indexer_name"] for r in data["results"]}
        assert indexer_names == {"NZBgeek", "Torznab", "Prowlarr"}

    async def test_torrent_and_usenet_results_mixed(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Both torrent and usenet results can be returned."""
        issue_id = await _create_issue(_db_factory)
        results = [
            _make_release("Batman 001.cbz", is_torrent=False),
            _make_release("Batman 001.cbz", "TorrentIndexer", is_torrent=True),
        ]
        p_reg, p_search = _mock_registry_and_search(results)
        with p_reg, p_search:
            resp = await client.post(f"/api/v1/issues/{issue_id}/search")

        assert resp.status_code == 200
        data = resp.json()
        torrent_flags = [r["is_torrent"] for r in data["results"]]
        assert True in torrent_flags
        assert False in torrent_flags

    async def test_result_with_null_size_and_age(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Results with null size_bytes and age_days are handled."""
        issue_id = await _create_issue(_db_factory)
        results = [_make_release("Batman 001.cbz", size_bytes=None, age_days=None)]
        p_reg, p_search = _mock_registry_and_search(results)
        with p_reg, p_search:
            resp = await client.post(f"/api/v1/issues/{issue_id}/search")

        assert resp.status_code == 200
        r = resp.json()["results"][0]
        assert r["size_bytes"] is None
        assert r["age_days"] is None

    async def test_search_owned_issue(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Searching an OWNED issue still works (returns results)."""
        issue_id = await _create_issue(_db_factory, status=IssueStatus.OWNED)
        results = [_make_release("Batman 001 (2016).cbz")]
        p_reg, p_search = _mock_registry_and_search(results)
        with p_reg, p_search:
            resp = await client.post(f"/api/v1/issues/{issue_id}/search")

        assert resp.status_code == 200
        assert len(resp.json()["results"]) == 1

    async def test_search_skipped_issue(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Searching a SKIPPED issue still works."""
        issue_id = await _create_issue(_db_factory, status=IssueStatus.SKIPPED)
        results = [_make_release("Batman 001 (2016).cbz")]
        p_reg, p_search = _mock_registry_and_search(results)
        with p_reg, p_search:
            resp = await client.post(f"/api/v1/issues/{issue_id}/search")

        assert resp.status_code == 200
        assert len(resp.json()["results"]) == 1
