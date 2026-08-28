"""Completed-download post-processing queue characterization tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.series import Series
from pullbox.tasks.download_post_processing_queue import _claim_completed_download

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def test_post_processing_queue_module_exposes_process_completed_runtime() -> None:
    """The completed-download drain should live beside the task module."""
    from pullbox.tasks import download_post_processing_queue

    assert isinstance(download_post_processing_queue._process_completed_lock, asyncio.Lock)
    assert callable(download_post_processing_queue.process_completed)


@pytest.fixture
async def db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_completed(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        series = Series(title="Claim Test", sort_title="claim test")
        session.add(series)
        await session.flush()
        issue = Issue(
            series_id=series.id,
            issue_number=1,
            status=IssueStatus.DOWNLOADING,
        )
        session.add(issue)
        await session.flush()
        download = DownloadHistory(
            issue_id=issue.id,
            title="Claim Test 001.cbz",
            download_url="test://claim",
            download_client=DownloadClientType.SABNZBD,
            state=DownloadState.COMPLETED,
            completed_at=datetime.now(UTC),
        )
        session.add(download)
        await session.commit()
        return download.id


@pytest.mark.asyncio
async def test_completed_download_atomic_claim_has_one_winner(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    download_id = await _seed_completed(db_factory)
    now = datetime.now(UTC)

    async with db_factory() as first_session:
        first = await _claim_completed_download(first_session, download_id, now=now)
    async with db_factory() as second_session:
        second = await _claim_completed_download(second_session, download_id, now=now)

    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_stale_completed_download_claim_is_restart_recoverable(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    download_id = await _seed_completed(db_factory)
    now = datetime.now(UTC)
    async with db_factory() as session:
        download = await session.get(DownloadHistory, download_id)
        assert download is not None
        download.post_processing_claim_token = "abandoned"
        download.post_processing_claimed_at = now - timedelta(minutes=16)
        await session.commit()

    async with db_factory() as session:
        recovered = await _claim_completed_download(session, download_id, now=now)

    assert recovered is not None
    assert recovered != "abandoned"
