"""Completed-only AirDC++ handoff through the existing library pipeline."""

from __future__ import annotations

import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.models import Base
from pullbox.models.airdcpp import AirDcppAcquisition
from pullbox.models.client import DownloadClientConfig
from pullbox.models.config import SystemConfig
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import LibraryFile, LibraryRoot
from pullbox.models.series import Series
from pullbox.tasks.download_post_processing_queue import process_completed
from pullbox.tasks.download_task import _run_post_processing

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

_TTH = "CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y"


@pytest.fixture
async def db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _write_valid_cbz(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("page001.jpg", b"\xff\xd8\xff\xd9")


@pytest.mark.asyncio
async def test_completed_airdcpp_cbz_imports_once_without_torrent_seed_branch(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    download_root = tmp_path / "airdcpp-downloads"
    library_path = tmp_path / "library"
    download_root.mkdir()
    library_path.mkdir()
    source = download_root / "Example Comic 001 (2026).cbz"
    _write_valid_cbz(source)

    async with db_factory() as session:
        root = LibraryRoot(name="Isolated Library", path=str(library_path), enabled=True)
        client = DownloadClientConfig(
            name="Dedicated Air",
            client_type=DownloadClientType.AIRDCPP,
            url="http://air.example.test:5600",
            username="pullbox",
            password="encrypted-test-value",
            enabled=True,
            priority=20,
            remote_path="/Downloads",
            download_dir=str(download_root),
        )
        session.add_all(
            [
                root,
                client,
                SystemConfig(
                    key="comics_directory",
                    value=str(library_path),
                    value_type="string",
                ),
                SystemConfig(
                    key="post_processing_method",
                    value="move",
                    value_type="string",
                ),
                SystemConfig(
                    key="torrent_import_strategy",
                    value="seed_safe",
                    value_type="string",
                ),
            ]
        )
        await session.flush()
        series = Series(
            title="Example Comic",
            sort_title="example comic",
            year_start=2026,
            library_root_id=root.id,
        )
        session.add(series)
        await session.flush()
        issue = Issue(
            series_id=series.id,
            issue_number=1,
            status=IssueStatus.DOWNLOADING,
        )
        session.add(issue)
        await session.flush()
        history = DownloadHistory(
            issue_id=issue.id,
            download_client_config_id=client.id,
            title=source.name,
            download_url="airdcpp://intent/import-once",
            download_client=DownloadClientType.AIRDCPP,
            protocol=AcquisitionProtocol.DC,
            external_id=f"airdcpp:{client.id}:bundle:91",
            state=DownloadState.COMPLETED,
            downloaded_path=f"/Downloads/{source.name}",
            completed_at=datetime.now(UTC),
            file_size=source.stat().st_size,
        )
        session.add(history)
        await session.flush()
        acquisition = AirDcppAcquisition(
            download_history_id=history.id,
            request_key="import-once",
            client_config_id=client.id,
            client_identity=f"airdcpp:{client.id}",
            tth=_TTH,
            size_bytes=source.stat().st_size,
            original_name=source.name,
            bundle_id=91,
            client_state="completed",
            remote_target=f"/Downloads/{source.name}",
        )
        session.add(acquisition)
        await session.commit()
        history_id = history.id
        issue_id = issue.id

    await process_completed(_run_post_processing, session_factory=db_factory)
    await process_completed(_run_post_processing, session_factory=db_factory)

    async with db_factory() as session:
        history = await session.get(DownloadHistory, history_id)
        issue = await session.get(Issue, issue_id)
        library_count = await session.scalar(select(func.count(LibraryFile.id)))
        assert history is not None and issue is not None
        assert history.state is DownloadState.COMPLETED
        assert history.imported_at is not None
        assert history.post_processing_claim_token is None
        assert history.final_path is not None
        assert Path(history.final_path).is_file()
        assert issue.status is IssueStatus.OWNED
        assert library_count == 1
        assert source.exists() is False
