"""Tests for bulk series operations (PATCH/DELETE /api/v1/series/bulk).

Verifies:
- Bulk update monitored status for multiple series
- Bulk delete multiple series (with and without file deletion)
- Empty series_ids returns 422
- Non-existent series skipped gracefully
- Maximum bulk size enforced (100)
- Auth required

Run:
    pytest tests/api/test_series_bulk.py -v
"""

from __future__ import annotations

import hashlib
import os
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.api.v1.series import bulk_update_series
from pullbox.models import Base
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.models.user import APIKey, User
from pullbox.schemas.series import SeriesBulkUpdate
from pullbox.services.auth_service import AuthService

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

    from pullbox.api.deps import AuthenticatedUser

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-bulk-ops")


class StatementRecorder:
    """Capture SELECT and UPDATE statements executed during a focused operation."""

    def __init__(self, async_engine: AsyncEngine) -> None:
        self._engine = async_engine.sync_engine
        self.statements: list[str] = []

    def __enter__(self) -> StatementRecorder:
        event.listen(self._engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc_info: object) -> None:
        event.remove(self._engine, "before_cursor_execute", self._record)

    def _record(
        self,
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        verb = statement.lstrip().split(maxsplit=1)[0].upper()
        if verb in {"SELECT", "UPDATE"}:
            self.statements.append(statement)


# ── Fixtures ──────────────────────────────────────────────────────────


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
) -> str:
    """Create a test user + API key, return the raw key string."""
    raw_key = "pb_k1_" + "b" * 64
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with _db_factory() as session:
        user = User(
            username="bulkuser",
            password_hash=AuthService.hash_password("Test@1234"),
        )
        session.add(user)
        await session.flush()
        session.add(APIKey(user_id=user.id, key_hash=key_hash, name="bulk-test"))
        await session.commit()
    return raw_key


@pytest.fixture
async def client(
    _db_factory: async_sessionmaker[AsyncSession],
    _api_key_header: str,
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client authenticated via API key (bypasses CSRF)."""
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
async def unauthenticated_client(
    _db_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client WITHOUT authentication."""
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


async def _seed_series(
    factory: async_sessionmaker[AsyncSession],
    *,
    count: int = 1,
    monitored: bool = True,
) -> list[int]:
    """Seed N series with 2 issues each, return list of series IDs."""
    ids: list[int] = []
    async with factory() as session:
        for i in range(count):
            series = Series(
                comicvine_id=50000 + i,
                title=f"Bulk Series {i}",
                sort_title=f"bulk series {i}",
                year_start=2024,
                status=SeriesStatus.CONTINUING,
                series_type=SeriesType.STANDARD,
                monitored=monitored,
                issue_count=2,
            )
            session.add(series)
            await session.flush()
            ids.append(series.id)

            # Add a couple of issues
            session.add(
                Issue(
                    series_id=series.id,
                    comicvine_id=60000 + i * 10 + 1,
                    issue_number=1.0,
                    title="Issue #1",
                    status=IssueStatus.WANTED if monitored else IssueStatus.SKIPPED,
                )
            )
            session.add(
                Issue(
                    series_id=series.id,
                    comicvine_id=60000 + i * 10 + 2,
                    issue_number=2.0,
                    title="Issue #2",
                    status=IssueStatus.OWNED,
                )
            )

        await session.commit()
    return ids


# ── Bulk Monitor Update Tests ─────────────────────────────────────────


class TestBulkMonitorUpdate:
    """PATCH /api/v1/series/bulk — update monitored status."""

    @pytest.mark.asyncio
    async def test_bulk_update_uses_set_based_update(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Bulk monitoring updates should not load each series one by one."""
        ids = await _seed_series(_db_factory, count=3, monitored=False)

        async with _db_factory() as session:
            engine = cast("AsyncEngine", session.bind)
            with StatementRecorder(engine) as recorder:
                result = await bulk_update_series(
                    SeriesBulkUpdate(series_ids=ids, monitored=True),
                    cast("AuthenticatedUser", object()),
                    session,
                )

        assert result == {"updated": 3, "skipped": 0}
        assert [stmt.lstrip().split(maxsplit=1)[0].upper() for stmt in recorder.statements] == [
            "UPDATE",
            "UPDATE",
        ]

    @pytest.mark.asyncio
    async def test_bulk_update_chunks_large_all_results_selection(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Large all-results selections stay below SQLite bind-variable limits."""
        series_ids = list(range(1, 502))

        async with _db_factory() as session:
            engine = cast("AsyncEngine", session.bind)
            with StatementRecorder(engine) as recorder:
                result = await bulk_update_series(
                    SeriesBulkUpdate(series_ids=series_ids, monitored=True),
                    cast("AuthenticatedUser", object()),
                    session,
                )

        assert result == {"updated": 0, "skipped": 501}
        assert [stmt.lstrip().split(maxsplit=1)[0].upper() for stmt in recorder.statements] == [
            "UPDATE",
            "UPDATE",
            "UPDATE",
            "UPDATE",
        ]

    @pytest.mark.asyncio
    async def test_set_monitored_true(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Set monitored=True for multiple unmonitored series."""
        ids = await _seed_series(_db_factory, count=3, monitored=False)
        resp = await client.patch(
            "/api/v1/series/bulk",
            json={"series_ids": ids, "monitored": True},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["updated"] == 3

        # Verify in DB
        async with _db_factory() as session:
            for sid in ids:
                s = await session.get(Series, sid)
                assert s is not None
                assert s.monitored is True
                issues = list(
                    (
                        await session.execute(
                            select(Issue).where(Issue.series_id == sid).order_by(Issue.issue_number)
                        )
                    )
                    .scalars()
                    .all()
                )
                assert [issue.status for issue in issues] == [
                    IssueStatus.WANTED,
                    IssueStatus.OWNED,
                ]

    @pytest.mark.asyncio
    async def test_set_monitored_false(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Set monitored=False for multiple monitored series."""
        ids = await _seed_series(_db_factory, count=2, monitored=True)
        resp = await client.patch(
            "/api/v1/series/bulk",
            json={"series_ids": ids, "monitored": False},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["updated"] == 2

        async with _db_factory() as session:
            for sid in ids:
                s = await session.get(Series, sid)
                assert s is not None
                assert s.monitored is False
                issues = list(
                    (
                        await session.execute(
                            select(Issue).where(Issue.series_id == sid).order_by(Issue.issue_number)
                        )
                    )
                    .scalars()
                    .all()
                )
                assert [issue.status for issue in issues] == [
                    IssueStatus.SKIPPED,
                    IssueStatus.OWNED,
                ]

    @pytest.mark.asyncio
    async def test_bulk_monitor_preserves_manual_skips(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ids = await _seed_series(_db_factory, count=1, monitored=False)
        async with _db_factory() as session:
            issue = (
                await session.execute(
                    select(Issue).where(Issue.series_id == ids[0], Issue.issue_number == 1.0)
                )
            ).scalar_one()
            issue.manual_skip = True
            await session.commit()

        resp = await client.patch(
            "/api/v1/series/bulk",
            json={"series_ids": ids, "monitored": True},
        )

        assert resp.status_code == 200
        async with _db_factory() as session:
            issue = (
                await session.execute(
                    select(Issue).where(Issue.series_id == ids[0], Issue.issue_number == 1.0)
                )
            ).scalar_one()
            assert issue.status == IssueStatus.SKIPPED
            assert issue.manual_skip is True

    @pytest.mark.asyncio
    async def test_bulk_monitor_repairs_skipped_issue_when_series_is_already_monitored(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ids = await _seed_series(_db_factory, count=1, monitored=True)
        async with _db_factory() as session:
            issue = (
                await session.execute(
                    select(Issue).where(Issue.series_id == ids[0], Issue.issue_number == 1.0)
                )
            ).scalar_one()
            issue.status = IssueStatus.SKIPPED
            await session.commit()

        resp = await client.patch(
            "/api/v1/series/bulk",
            json={"series_ids": ids, "monitored": True},
        )

        assert resp.status_code == 200
        async with _db_factory() as session:
            issue = (
                await session.execute(
                    select(Issue).where(Issue.series_id == ids[0], Issue.issue_number == 1.0)
                )
            ).scalar_one()
            assert issue.status == IssueStatus.WANTED

    @pytest.mark.asyncio
    async def test_bulk_unmonitor_repairs_wanted_issue_when_series_is_already_unmonitored(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ids = await _seed_series(_db_factory, count=1, monitored=False)
        async with _db_factory() as session:
            issue = (
                await session.execute(
                    select(Issue).where(Issue.series_id == ids[0], Issue.issue_number == 1.0)
                )
            ).scalar_one()
            issue.status = IssueStatus.WANTED
            await session.commit()

        resp = await client.patch(
            "/api/v1/series/bulk",
            json={"series_ids": ids, "monitored": False},
        )

        assert resp.status_code == 200
        async with _db_factory() as session:
            issue = (
                await session.execute(
                    select(Issue).where(Issue.series_id == ids[0], Issue.issue_number == 1.0)
                )
            ).scalar_one()
            assert issue.status == IssueStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_bulk_unmonitor_skips_downloading_issues(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ids = await _seed_series(_db_factory, count=1, monitored=True)
        async with _db_factory() as session:
            issue = (
                await session.execute(
                    select(Issue).where(Issue.series_id == ids[0], Issue.issue_number == 1.0)
                )
            ).scalar_one()
            issue.status = IssueStatus.DOWNLOADING
            await session.commit()

        resp = await client.patch(
            "/api/v1/series/bulk",
            json={"series_ids": ids, "monitored": False},
        )

        assert resp.status_code == 200
        async with _db_factory() as session:
            issue = (
                await session.execute(
                    select(Issue).where(Issue.series_id == ids[0], Issue.issue_number == 1.0)
                )
            ).scalar_one()
            assert issue.status == IssueStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_nonexistent_series_skipped(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Non-existent IDs are skipped; valid ones still updated."""
        ids = await _seed_series(_db_factory, count=1, monitored=False)
        resp = await client.patch(
            "/api/v1/series/bulk",
            json={"series_ids": [*ids, 99999], "monitored": True},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["updated"] == 1
        assert data["skipped"] == 1

    @pytest.mark.asyncio
    async def test_empty_ids_returns_422(self, client: AsyncClient) -> None:
        """Empty series_ids list returns 422."""
        resp = await client.patch(
            "/api/v1/series/bulk",
            json={"series_ids": [], "monitored": True},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_all_results_sized_update_is_accepted(
        self,
        client: AsyncClient,
    ) -> None:
        """Selecting more than one page of series remains a valid bulk update."""
        resp = await client.patch(
            "/api/v1/series/bulk",
            json={"series_ids": list(range(1, 102)), "monitored": True},
        )

        assert resp.status_code == 200
        assert resp.json() == {"updated": 0, "skipped": 101}

    @pytest.mark.asyncio
    async def test_over_safety_max_returns_422(
        self,
        client: AsyncClient,
    ) -> None:
        """Unreasonably large bulk payloads retain a defensive upper bound."""
        resp = await client.patch(
            "/api/v1/series/bulk",
            json={"series_ids": list(range(1, 10_002)), "monitored": True},
        )

        assert resp.status_code == 422


# ── Bulk Delete Tests ─────────────────────────────────────────────────


class TestBulkDelete:
    """DELETE /api/v1/series/bulk — delete multiple series."""

    @pytest.mark.asyncio
    async def test_delete_context_counts_only_existing_linked_files(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Delete preview only surfaces tracked files that still exist on disk."""
        ids = await _seed_series(_db_factory, count=1)

        folder = tmp_path / "Bulk Series 0 (2024)"
        folder.mkdir()
        comic_path = folder / "Bulk Series 0 001.cbz"
        comic_path.write_bytes(b"data")

        async with _db_factory() as session:
            root = LibraryRoot(
                name="Comics",
                path=str(tmp_path),
                enabled=True,
                last_scan_at=datetime.now(tz=UTC),
            )
            session.add(root)
            await session.flush()

            series = await session.get(Series, ids[0])
            assert series is not None
            series.library_root_id = root.id
            series.path = None

            issue = (
                await session.execute(
                    select(Issue).where(Issue.series_id == series.id, Issue.issue_number == 1.0)
                )
            ).scalar_one()
            session.add(
                LibraryFile(
                    file_path=str(comic_path),
                    file_name=comic_path.name,
                    file_size=comic_path.stat().st_size,
                    file_format=FileFormat.CBZ,
                    match_confidence=MatchConfidence.HIGH,
                    issue_id=issue.id,
                    library_root_id=root.id,
                    file_modified_at=datetime.now(tz=UTC),
                )
            )
            await session.commit()

        response = await client.post(
            "/api/v1/series/delete-context",
            json={"series_ids": ids},
        )

        assert response.status_code == 200
        assert response.json() == {
            "series_count": 1,
            "linked_file_count": 1,
            "managed_file_count": 1,
            "referenced_file_count": 0,
        }

    @pytest.mark.asyncio
    async def test_delete_context_ignores_missing_disk_files(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Delete preview hides the delete-files option when tracked files are already gone."""
        ids = await _seed_series(_db_factory, count=1)

        missing_path = tmp_path / "Bulk Series 0 001.cbz"

        async with _db_factory() as session:
            root = LibraryRoot(
                name="Comics",
                path=str(tmp_path),
                enabled=True,
                last_scan_at=datetime.now(tz=UTC),
            )
            session.add(root)
            await session.flush()

            series = await session.get(Series, ids[0])
            assert series is not None
            series.library_root_id = root.id
            series.path = None

            issue = (
                await session.execute(
                    select(Issue).where(Issue.series_id == series.id, Issue.issue_number == 1.0)
                )
            ).scalar_one()
            session.add(
                LibraryFile(
                    file_path=str(missing_path),
                    file_name=missing_path.name,
                    file_size=1,
                    file_format=FileFormat.CBZ,
                    match_confidence=MatchConfidence.HIGH,
                    issue_id=issue.id,
                    library_root_id=root.id,
                    file_modified_at=datetime.now(tz=UTC),
                )
            )
            await session.commit()

        response = await client.post(
            "/api/v1/series/delete-context",
            json={"series_ids": ids},
        )

        assert response.status_code == 200
        assert response.json() == {
            "series_count": 1,
            "linked_file_count": 0,
            "managed_file_count": 0,
            "referenced_file_count": 0,
        }

    @pytest.mark.asyncio
    async def test_delete_multiple_series(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Delete multiple series; verify removed from DB."""
        ids = await _seed_series(_db_factory, count=3)

        with patch(
            "pullbox.services.series_service._cancel_download_on_client",
        ):
            resp = await client.request(
                "DELETE",
                "/api/v1/series/bulk",
                json={"series_ids": ids},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] == 3

        async with _db_factory() as session:
            for sid in ids:
                s = await session.get(Series, sid)
                assert s is None

    @pytest.mark.asyncio
    async def test_delete_with_files_flag(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """delete_files flag is passed through to SeriesService.delete()."""
        ids = await _seed_series(_db_factory, count=1)

        with patch(
            "pullbox.api.v1.series.SeriesService.delete",
        ) as mock_delete:
            resp = await client.request(
                "DELETE",
                "/api/v1/series/bulk",
                json={"series_ids": ids, "delete_files": True},
            )

        assert resp.status_code == 200
        mock_delete.assert_called_once_with(
            mock_delete.call_args[0][0],  # session
            ids[0],
            delete_files=True,
            delete_folder=False,
        )

    @pytest.mark.asyncio
    async def test_delete_nonexistent_skipped(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Non-existent series IDs are skipped; valid ones still deleted."""
        ids = await _seed_series(_db_factory, count=1)

        with patch(
            "pullbox.services.series_service._cancel_download_on_client",
        ):
            resp = await client.request(
                "DELETE",
                "/api/v1/series/bulk",
                json={"series_ids": [*ids, 99999]},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] == 1
        assert data["skipped"] == 1

    @pytest.mark.asyncio
    async def test_delete_empty_ids_returns_422(self, client: AsyncClient) -> None:
        """Empty series_ids list returns 422."""
        resp = await client.request(
            "DELETE",
            "/api/v1/series/bulk",
            json={"series_ids": []},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_delete_over_max_returns_422(self, client: AsyncClient) -> None:
        """More than 100 IDs returns 422."""
        resp = await client.request(
            "DELETE",
            "/api/v1/series/bulk",
            json={"series_ids": list(range(1, 102))},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_delete_cascades_to_issues(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Deleting series also removes associated issues."""
        ids = await _seed_series(_db_factory, count=1)

        with patch(
            "pullbox.services.series_service._cancel_download_on_client",
        ):
            resp = await client.request(
                "DELETE",
                "/api/v1/series/bulk",
                json={"series_ids": ids},
            )

        assert resp.status_code == 200

        async with _db_factory() as session:
            result = await session.execute(select(Issue).where(Issue.series_id == ids[0]))
            assert list(result.scalars().all()) == []


# ── Auth Tests ────────────────────────────────────────────────────────


class TestBulkAuth:
    """Bulk endpoints require authentication."""

    @pytest.mark.asyncio
    async def test_patch_requires_auth(
        self,
        unauthenticated_client: AsyncClient,
    ) -> None:
        resp = await unauthenticated_client.patch(
            "/api/v1/series/bulk",
            json={"series_ids": [1], "monitored": True},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_delete_requires_auth(
        self,
        unauthenticated_client: AsyncClient,
    ) -> None:
        resp = await unauthenticated_client.request(
            "DELETE",
            "/api/v1/series/bulk",
            json={"series_ids": [1]},
        )
        assert resp.status_code == 401
