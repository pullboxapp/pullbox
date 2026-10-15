"""API tests for blocklisting failed download and post-processing history rows."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.blocklist import BlocklistEntry, BlocklistReason, normalize_release_title
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import Issue
from pullbox.models.series import Series
from pullbox.models.user import APIKey, User

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

pytestmark = pytest.mark.slow

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-download-blocklist-api")


@pytest.fixture()
async def _db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture()
async def _api_key_header(
    _db_factory: async_sessionmaker[AsyncSession], seeded_user_password_hash: str
) -> str:
    raw_key = "pb_k1_" + "d" * 64
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with _db_factory() as session:
        user = User(
            username="dlblockuser",
            password_hash=seeded_user_password_hash,
        )
        session.add(user)
        await session.flush()
        session.add(APIKey(user_id=user.id, key_hash=key_hash, name="download-blocklist-test"))
        await session.commit()
    return raw_key


@pytest.fixture()
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


async def _seed_download(
    factory: async_sessionmaker[AsyncSession],
    *,
    title: str,
    state: DownloadState,
    error_message: str | None = None,
    downloaded_path: str | None = None,
) -> tuple[int, int, int]:
    async with factory() as session:
        series = Series(title="Batman", sort_title="batman")
        session.add(series)
        await session.flush()

        issue = Issue(series_id=series.id, issue_number=18.0)
        session.add(issue)
        await session.flush()

        download = DownloadHistory(
            issue_id=issue.id,
            title=title,
            download_url="https://example.com/download",
            download_client=DownloadClientType.SABNZBD,
            state=state,
            error_message=error_message,
            downloaded_path=downloaded_path,
            completed_at=datetime(2026, 5, 2, 20, 0, tzinfo=UTC),
            updated_at=datetime(2026, 5, 2, 20, 1, tzinfo=UTC),
        )
        session.add(download)
        await session.flush()

        download_id = download.id
        issue_id = issue.id
        series_id = series.id
        await session.commit()
        return download_id, issue_id, series_id


class TestDownloadHistoryBlocklistApi:
    """POST /api/v1/downloads/{id}/blocklist."""

    async def test_failed_download_can_be_blocklisted(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        download_id, issue_id, series_id = await _seed_download(
            _db_factory,
            title="Batman.018.2026.Digital.Zone-Empire",
            state=DownloadState.FAILED,
            error_message="Connection refused",
        )

        response = await client.post(f"/api/v1/downloads/{download_id}/blocklist")

        assert response.status_code == 201
        data = response.json()
        assert data["release_title"] == "Batman.018.2026.Digital.Zone-Empire"
        assert data["reason"] == BlocklistReason.FAILED.value
        assert data["issue_id"] == issue_id
        assert data["series_id"] == series_id
        assert data["download_history_id"] == download_id
        assert data["release_group"] == "Zone-Empire"
        assert data["error_message"] == "Connection refused"

    async def test_failed_post_processing_row_can_be_blocklisted(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        download_id, issue_id, _series_id = await _seed_download(
            _db_factory,
            title="Archive.Edition.001.2026.Digital-Empire",
            state=DownloadState.FAILED,
            error_message="Move failed: disk full",
            downloaded_path="/downloads/Archive Edition 001.cbz",
        )

        response = await client.post(f"/api/v1/downloads/{download_id}/blocklist")

        assert response.status_code == 201
        data = response.json()
        assert data["issue_id"] == issue_id
        assert data["download_history_id"] == download_id
        assert data["reason"] == BlocklistReason.FAILED.value
        assert data["error_message"] == "Move failed: disk full"

    async def test_cancelled_download_cannot_be_blocklisted(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        download_id, _issue_id, _series_id = await _seed_download(
            _db_factory,
            title="Batman.018.2026.Digital.Zone-Empire",
            state=DownloadState.FAILED,
            error_message="Cancelled by user",
        )

        response = await client.post(f"/api/v1/downloads/{download_id}/blocklist")

        assert response.status_code == 409
        assert response.json()["detail"] == "Cancelled downloads cannot be blocklisted."

    async def test_non_failed_row_cannot_be_blocklisted(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        download_id, _issue_id, _series_id = await _seed_download(
            _db_factory,
            title="Batman.018.2026.Digital.Zone-Empire",
            state=DownloadState.COMPLETED,
        )

        response = await client.post(f"/api/v1/downloads/{download_id}/blocklist")

        assert response.status_code == 409
        assert response.json()["detail"] == "Only failed download history items can be blocklisted."

    async def test_duplicate_blocklist_returns_conflict(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        title = "Batman.018.2026.Digital.Zone-Empire"
        download_id, issue_id, series_id = await _seed_download(
            _db_factory,
            title=title,
            state=DownloadState.FAILED,
            error_message="Connection refused",
        )

        async with _db_factory() as session:
            session.add(
                BlocklistEntry(
                    release_title=title,
                    release_title_normalized=normalize_release_title(title),
                    reason=BlocklistReason.FAILED,
                    issue_id=issue_id,
                    series_id=series_id,
                    download_history_id=download_id,
                )
            )
            await session.commit()

        response = await client.post(f"/api/v1/downloads/{download_id}/blocklist")

        assert response.status_code == 409
        assert response.json()["detail"]["error"]["message"] == "Release already in blocklist"
