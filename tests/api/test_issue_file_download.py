"""Tests for issue file download endpoint (GET /api/v1/issues/{id}/download-file).

Verifies:
- Download OWNED issue returns file with correct headers
- Download non-OWNED issue returns 404
- Download with missing physical file returns 404
- Requires authentication
- Content-Disposition filename is correct

Run:
    pytest tests/api/test_issue_file_download.py -v
"""

from __future__ import annotations

import hashlib
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.models.user import APIKey, User

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-file-download")


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
    seeded_user_password_hash: str,
) -> str:
    raw_key = "pb_k1_" + "d" * 64
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with _db_factory() as session:
        user = User(
            username="dluser",
            password_hash=seeded_user_password_hash,
        )
        session.add(user)
        await session.flush()
        session.add(APIKey(user_id=user.id, key_hash=key_hash, name="dl-test"))
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
async def unauthenticated_client(
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


async def _seed_owned_issue(
    factory: async_sessionmaker[AsyncSession],
    file_path: str,
) -> int:
    """Seed a series with one OWNED issue linked to a library file."""
    async with factory() as session:
        series = Series(
            comicvine_id=77777,
            title="Download Test Series",
            sort_title="download test series",
            year_start=2024,
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
            monitored=True,
            issue_count=1,
        )
        session.add(series)
        await session.flush()

        issue = Issue(
            series_id=series.id,
            comicvine_id=88888,
            issue_number=1.0,
            title="Issue #1",
            status=IssueStatus.OWNED,
        )
        session.add(issue)
        await session.flush()

        root = LibraryRoot(name="test", path="/tmp/comics")
        session.add(root)
        await session.flush()

        lf = LibraryFile(
            file_path=file_path,
            file_name=Path(file_path).name,
            file_size=1234,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(tz=UTC),
            issue_id=issue.id,
            match_confidence=MatchConfidence.HIGH,
            library_root_id=root.id,
        )
        session.add(lf)
        await session.commit()
        return issue.id


async def _seed_wanted_issue(
    factory: async_sessionmaker[AsyncSession],
) -> int:
    """Seed a series with one WANTED issue (no file)."""
    async with factory() as session:
        series = Series(
            comicvine_id=77778,
            title="Wanted Test Series",
            sort_title="wanted test series",
            year_start=2024,
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
            monitored=True,
            issue_count=1,
        )
        session.add(series)
        await session.flush()

        issue = Issue(
            series_id=series.id,
            comicvine_id=88889,
            issue_number=1.0,
            title="Issue #1",
            status=IssueStatus.WANTED,
        )
        session.add(issue)
        await session.commit()
        return issue.id


# ── Tests ─────────────────────────────────────────────────────────────


class TestDownloadFile:
    """GET /api/v1/issues/{id}/download-file."""

    @pytest.mark.asyncio
    async def test_download_owned_issue_returns_file(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """OWNED issue with existing file returns 200 with file contents."""
        comic_file = tmp_path / "Batman 001 (2024).cbz"
        comic_file.write_bytes(b"PK\x03\x04fake-zip-content")

        issue_id = await _seed_owned_issue(_db_factory, str(comic_file))
        resp = await client.get(f"/api/v1/issues/{issue_id}/download-file")

        assert resp.status_code == 200
        assert resp.content == b"PK\x03\x04fake-zip-content"

    @pytest.mark.asyncio
    async def test_content_disposition_header(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Response has Content-Disposition with the original filename."""
        comic_file = tmp_path / "Batman 001 (2024).cbz"
        comic_file.write_bytes(b"PK\x03\x04test")

        issue_id = await _seed_owned_issue(_db_factory, str(comic_file))
        resp = await client.get(f"/api/v1/issues/{issue_id}/download-file")

        assert resp.status_code == 200
        cd = resp.headers.get("content-disposition", "")
        assert "attachment" in cd
        # Filename may be URL-encoded in the header
        assert "Batman" in cd
        assert ".cbz" in cd

    @pytest.mark.asyncio
    async def test_download_wanted_issue_returns_404(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Non-OWNED issue returns 404."""
        issue_id = await _seed_wanted_issue(_db_factory)
        resp = await client.get(f"/api/v1/issues/{issue_id}/download-file")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_download_missing_file_returns_404(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """OWNED issue whose file no longer exists on disk returns 404."""
        issue_id = await _seed_owned_issue(_db_factory, "/nonexistent/path/comic.cbz")
        resp = await client.get(f"/api/v1/issues/{issue_id}/download-file")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_download_nonexistent_issue_returns_404(
        self,
        client: AsyncClient,
    ) -> None:
        resp = await client.get("/api/v1/issues/99999/download-file")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_download_requires_auth(
        self,
        unauthenticated_client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        comic_file = tmp_path / "test.cbz"
        comic_file.write_bytes(b"PK\x03\x04test")
        issue_id = await _seed_owned_issue(_db_factory, str(comic_file))
        resp = await unauthenticated_client.get(f"/api/v1/issues/{issue_id}/download-file")
        assert resp.status_code == 401
