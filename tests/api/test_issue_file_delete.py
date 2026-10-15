"""Tests for deleting an individual issue file from the issue detail workflow."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.config import SystemConfig
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.models.user import APIKey, User

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-issue-file-delete")


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
            username="deletefileuser",
            password_hash=seeded_user_password_hash,
        )
        session.add(user)
        await session.flush()
        session.add(APIKey(user_id=user.id, key_hash=key_hash, name="delete-file-test"))
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


async def _seed_owned_issue(
    factory: async_sessionmaker[AsyncSession],
    file_path: Path,
    *,
    monitored: bool = True,
    trash_dir: Path | None = None,
) -> int:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b"old comic")

    async with factory() as session:
        root = LibraryRoot(name="Comics", path=str(file_path.parents[1]), enabled=True)
        session.add(root)
        await session.flush()

        series = Series(
            comicvine_id=100,
            title="Batman",
            sort_title="batman",
            year_start=2016,
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
            monitored=monitored,
            issue_count=1,
            library_root_id=root.id,
        )
        session.add(series)
        await session.flush()

        issue = Issue(
            series_id=series.id,
            comicvine_id=50001,
            issue_number=1.0,
            title="I Am Gotham",
            status=IssueStatus.OWNED,
        )
        session.add(issue)
        await session.flush()

        session.add(
            LibraryFile(
                file_path=str(file_path),
                file_name=file_path.name,
                file_size=file_path.stat().st_size,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(UTC),
                match_confidence=MatchConfidence.HIGH,
                issue_id=issue.id,
                library_root_id=root.id,
            )
        )
        if trash_dir is not None:
            session.add(
                SystemConfig(
                    key="utility_trash_folder",
                    value=str(trash_dir),
                    value_type="string",
                )
            )
        await session.commit()
        return issue.id


@pytest.mark.asyncio
async def test_delete_issue_file_moves_file_to_trash_and_marks_monitored_issue_wanted(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    library_file = tmp_path / "comics" / "Batman" / "Batman 001.cbz"
    trash_dir = tmp_path / "trash"
    issue_id = await _seed_owned_issue(_db_factory, library_file, trash_dir=trash_dir)

    response = await client.delete(f"/api/v1/issues/{issue_id}/file")

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["issue_id"] == issue_id
    assert data["status"] == "wanted"
    assert data["trashed"] is True
    assert library_file.exists() is False
    assert list(trash_dir.rglob("Batman 001.cbz"))

    async with _db_factory() as session:
        issue = await session.get(Issue, issue_id)
        rows = (await session.execute(select(LibraryFile))).scalars().all()

    assert issue is not None
    assert issue.status == IssueStatus.WANTED
    assert rows == []


@pytest.mark.asyncio
async def test_delete_issue_file_unlinks_without_trash_and_marks_unmonitored_issue_skipped(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    library_file = tmp_path / "comics" / "Batman" / "Batman 001.cbz"
    issue_id = await _seed_owned_issue(_db_factory, library_file, monitored=False)

    response = await client.delete(f"/api/v1/issues/{issue_id}/file")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "skipped"
    assert response.json()["trashed"] is False
    assert library_file.exists() is False

    async with _db_factory() as session:
        issue = await session.get(Issue, issue_id)
        rows = (await session.execute(select(LibraryFile))).scalars().all()

    assert issue is not None
    assert issue.status == IssueStatus.SKIPPED
    assert rows == []
