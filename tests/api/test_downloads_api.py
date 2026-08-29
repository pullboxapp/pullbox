"""API contract tests for download queue, history, and retry routes."""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.api.v1 import downloads as downloads_api
from pullbox.core.exceptions import NotFoundError
from pullbox.models import Base
from pullbox.models.airdcpp import AirDcppAcquisition
from pullbox.models.client import DownloadClientConfig
from pullbox.models.direct_acquisition import (
    DirectAcquisitionAttempt,
    DirectAcquisitionState,
    DirectArtifactAttempt,
    DirectArtifactHostKind,
    DirectArtifactRouteKind,
    DirectArtifactState,
)
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.indexer import IndexerConfig, IndexerSource, IndexerType
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.series import Series
from pullbox.models.user import APIKey, User
from pullbox.providers.airdcpp.errors import AirDcppUnavailableError
from pullbox.providers.airdcpp.supervisor import AirDcppSupervisorState
from pullbox.providers.download.qbittorrent import QBittorrentError
from pullbox.providers.indexer.newznab import NewznabError
from pullbox.services.auth_service import AuthService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-downloads-api")


@pytest.fixture
async def db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def api_key(db_factory: async_sessionmaker[AsyncSession]) -> str:
    raw_key = "pb_k1_" + "a" * 64
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with db_factory() as session:
        user = User(
            username="downloadsapi",
            password_hash=AuthService.hash_password("Test@1234"),
        )
        session.add(user)
        await session.flush()
        session.add(APIKey(user_id=user.id, key_hash=key_hash, name="downloads-api-test"))
        await session.commit()
    return raw_key


@pytest.fixture
async def client(
    db_factory: async_sessionmaker[AsyncSession],
    api_key: str,
) -> AsyncGenerator[AsyncClient, None]:
    from pullbox.api.deps import get_db_dep
    from pullbox.api.middleware import reset_setup_cache
    from pullbox.app import create_app

    app = create_app()

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
        async with db_factory() as session:
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
        headers={"X-Api-Key": api_key},
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
    reset_setup_cache()


async def _seed_issue(
    factory: async_sessionmaker[AsyncSession],
    *,
    status: IssueStatus = IssueStatus.WANTED,
) -> int:
    async with factory() as session:
        series = Series(title="Batman", sort_title="batman", year_start=2025)
        session.add(series)
        await session.flush()
        issue = Issue(series_id=series.id, issue_number=4.0, status=status)
        session.add(issue)
        await session.flush()
        issue_id = issue.id
        await session.commit()
        return issue_id


async def _seed_download(
    factory: async_sessionmaker[AsyncSession],
    issue_id: int,
    *,
    title: str = "Batman 004 (2025)",
    state: DownloadState = DownloadState.FAILED,
    client_type: DownloadClientType = DownloadClientType.SABNZBD,
    download_url: str = "https://example.com/batman-004.nzb",
    external_id: str | None = "download-ext",
    downloaded_path: str | None = None,
    error_message: str | None = "Download failed",
    completed_at: datetime | None = None,
    indexer_id: int | None = None,
    download_client_config_id: int | None = None,
) -> int:
    async with factory() as session:
        download = DownloadHistory(
            title=title,
            state=state,
            download_client=client_type,
            download_url=download_url,
            external_id=external_id,
            issue_id=issue_id,
            downloaded_path=downloaded_path,
            error_message=error_message,
            completed_at=completed_at,
            indexer_id=indexer_id,
            download_client_config_id=download_client_config_id,
        )
        session.add(download)
        await session.flush()
        download_id = download.id
        await session.commit()
        return download_id


async def _seed_client_config(
    factory: async_sessionmaker[AsyncSession],
    *,
    config_id: int,
    client_type: DownloadClientType,
) -> None:
    async with factory() as session:
        session.add(
            DownloadClientConfig(
                id=config_id,
                name=f"Client {config_id}",
                client_type=client_type,
                url=f"http://client-{config_id}.test",
            )
        )
        await session.commit()


async def _seed_direct_download_with_routes(
    factory: async_sessionmaker[AsyncSession],
    issue_id: int,
) -> tuple[int, int]:
    async with factory() as session:
        attempt = DirectAcquisitionAttempt(
            request_key=f"download-source-switch:{issue_id}",
            issue_id=issue_id,
            provider_identity="pullbox.getcomics",
            provider_candidate_id="candidate-switch",
            state=DirectAcquisitionState.DOWNLOADING,
            plan_revision=1,
            plan_snapshot={
                "schema_version": 1,
                "selected_artifact_identity": "route:current",
                "artifacts": [
                    {
                        "artifact_identity": "route:current",
                        "content_identity": "artifact:primary",
                        "route_kind": "direct",
                        "host_kind": "generic_https",
                        "eligible": True,
                        "eligibility_code": "eligible",
                        "expected_size": 1_000,
                    },
                    {
                        "artifact_identity": "route:pixel",
                        "content_identity": "artifact:primary",
                        "route_kind": "direct",
                        "host_kind": "pixeldrain",
                        "eligible": True,
                        "eligibility_code": "eligible",
                        "expected_size": 1_000,
                    },
                    {
                        "artifact_identity": "route:other",
                        "content_identity": "artifact:other",
                        "route_kind": "direct",
                        "host_kind": "mediafire",
                        "eligible": True,
                        "eligibility_code": "eligible",
                    },
                ],
            },
            progress_revision=1,
            progress_snapshot={
                "schema_version": 1,
                "stage": "downloading",
                "host_kind": "generic_https",
                "bytes_transferred": 512,
            },
            candidate_snapshot={"display_title": "Batman 004 (2025)"},
        )
        attempt.artifact_attempts = [
            DirectArtifactAttempt(
                sequence_no=0,
                artifact_identity="route:current",
                route_kind=DirectArtifactRouteKind.DIRECT,
                host_kind=DirectArtifactHostKind.GENERIC_HTTPS,
                state=DirectArtifactState.TRANSFERRING,
                is_selected=True,
                expected_size=1_000,
                bytes_transferred=512,
            )
        ]
        session.add(attempt)
        await session.flush()
        download = DownloadHistory(
            issue_id=issue_id,
            title="Batman 004 (2025)",
            state=DownloadState.DOWNLOADING,
            download_client=DownloadClientType.DIRECT,
            download_url=f"pullbox-direct://attempt/{attempt.id}",
            external_id=f"direct:{attempt.id}",
        )
        session.add(download)
        await session.flush()
        ids = (download.id, attempt.id)
        await session.commit()
        return ids


async def _get_download(
    factory: async_sessionmaker[AsyncSession],
    download_id: int,
) -> DownloadHistory:
    async with factory() as session:
        download = await session.get(DownloadHistory, download_id)
        assert download is not None
        return download


async def _get_issue(factory: async_sessionmaker[AsyncSession], issue_id: int) -> Issue:
    async with factory() as session:
        issue = await session.get(Issue, issue_id)
        assert issue is not None
        return issue


class TestDownloadQueueAndHistory:
    @pytest.mark.asyncio
    async def test_queue_returns_only_active_downloads_with_series_context(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory)
        await _seed_download(
            db_factory,
            issue_id,
            state=DownloadState.QUEUED,
            error_message=None,
        )
        await _seed_download(
            db_factory,
            issue_id,
            state=DownloadState.FINALIZING,
            error_message=None,
        )
        await _seed_download(db_factory, issue_id, state=DownloadState.COMPLETED)
        await _seed_download(db_factory, issue_id, state=DownloadState.FAILED)

        response = await client.get("/api/v1/downloads/queue")

        assert response.status_code == 200
        data = response.json()
        assert {item["state"] for item in data} == {"queued", "finalizing"}
        assert {item["protocol"] for item in data} == {"usenet"}
        assert data[0]["series_title"] == "Batman"
        assert data[0]["issue_number"] == 4.0

    @pytest.mark.asyncio
    async def test_history_paginates_download_page_records_only(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory)
        for index in range(3):
            await _seed_download(
                db_factory,
                issue_id,
                title=f"Batman history {index}",
                state=DownloadState.COMPLETED,
                error_message=None,
            )
        await _seed_download(
            db_factory,
            issue_id,
            title="Imported row",
            state=DownloadState.IMPORTED,
            downloaded_path="/downloads/imported.cbz",
            error_message=None,
        )

        response = await client.get("/api/v1/downloads/history?limit=2&offset=1")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 3
        assert data["limit"] == 2
        assert data["offset"] == 1
        assert data["has_more"] is False
        assert len(data["items"]) == 2
        assert all(item["series_title"] == "Batman" for item in data["items"])
        assert {item["protocol"] for item in data["items"]} == {"usenet"}


class TestDownloadPostProcessingRetry:
    @pytest.mark.asyncio
    async def test_retry_processing_requeues_failed_downloaded_file(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory)
        download_id = await _seed_download(
            db_factory,
            issue_id,
            downloaded_path="/downloads/batman-004.cbz",
            error_message="File placement failed",
        )
        scheduler = MagicMock()

        with patch("pullbox.core.scheduler.get_scheduler", return_value=scheduler):
            response = await client.post(f"/api/v1/downloads/{download_id}/retry-processing")

        assert response.status_code == 200
        assert response.json() == {"status": "queued"}
        scheduler.run_task_now.assert_called_once_with("process_completed")
        download = await _get_download(db_factory, download_id)
        assert download.state == DownloadState.COMPLETED
        assert download.error_message == "File placement failed"

    @pytest.mark.asyncio
    async def test_retry_processing_rejects_non_post_processing_failure(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory)
        download_id = await _seed_download(db_factory, issue_id, downloaded_path=None)

        response = await client.post(f"/api/v1/downloads/{download_id}/retry-processing")

        assert response.status_code == 409
        assert response.json()["detail"] == "Only failed post-processing items can be retried."


class TestDownloadRetry:
    @pytest.mark.asyncio
    async def test_retry_failed_direct_download_uses_native_runner(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory, status=IssueStatus.WANTED)
        async with db_factory() as session:
            attempt = DirectAcquisitionAttempt(
                request_key="downloads-api-direct-retry",
                issue_id=issue_id,
                provider_identity="community.test",
                provider_candidate_id="candidate-1",
                state=DirectAcquisitionState.FAILED,
                requested_coverage={"issue_numbers": ["4"]},
                candidate_snapshot={"display_title": "Batman 004 (2025)"},
                plan_snapshot={"schema_version": 1},
                plan_revision=1,
                progress_snapshot={"stage": "failed"},
            )
            attempt.artifact_attempts = [
                DirectArtifactAttempt(
                    sequence_no=0,
                    artifact_identity="route:one",
                    route_kind=DirectArtifactRouteKind.DIRECT,
                    host_kind=DirectArtifactHostKind.GENERIC_HTTPS,
                    state=DirectArtifactState.FAILED,
                    is_selected=True,
                )
            ]
            session.add(attempt)
            await session.flush()
            attempt_id = attempt.id
            await session.commit()
        download_id = await _seed_download(
            db_factory,
            issue_id,
            client_type=DownloadClientType.DIRECT,
            download_url=f"pullbox-direct://attempt/{attempt_id}",
            external_id=f"direct:{attempt_id}",
            downloaded_path=None,
            error_message="Direct transfer failed",
        )
        runner = AsyncMock()
        runner.retry.return_value = True

        with patch(
            "pullbox.tasks.direct_acquisition_task.get_direct_acquisition_runner",
            return_value=runner,
        ):
            response = await client.post(f"/api/v1/downloads/{download_id}/retry")

        assert response.status_code == 200
        assert response.json() == {"status": "sent"}
        runner.retry.assert_awaited_once_with(attempt_id)
        issue = await _get_issue(db_factory, issue_id)
        assert issue.status is IssueStatus.DOWNLOADING

    @pytest.mark.asyncio
    async def test_retry_failed_usenet_download_resends_to_configured_client(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory, status=IssueStatus.WANTED)
        download_id = await _seed_download(
            db_factory,
            issue_id,
            downloaded_path=None,
            error_message="Connection failed",
            completed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        mock_client = AsyncMock()
        mock_client.client_type = "sabnzbd"
        mock_client.add_nzb = AsyncMock(return_value="resent-nzb-id")

        with patch(
            "pullbox.composition.providers.register_download_clients",
            new_callable=AsyncMock,
        ) as mock_register:

            async def _register(_session: object, registry: object) -> None:
                registry.register_download_client(1, mock_client)  # type: ignore[union-attr]

            mock_register.side_effect = _register

            response = await client.post(f"/api/v1/downloads/{download_id}/retry")

        assert response.status_code == 200
        assert response.json() == {"status": "sent"}
        mock_client.add_nzb.assert_awaited_once_with(
            "https://example.com/batman-004.nzb",
            "Batman 004 (2025)",
        )
        download = await _get_download(db_factory, download_id)
        assert download.state == DownloadState.SENT
        assert download.external_id == "resent-nzb-id"
        assert download.error_message is None
        assert download.downloaded_path is None
        assert download.completed_at is None
        assert download.sent_at is not None
        issue = await _get_issue(db_factory, issue_id)
        assert issue.status == IssueStatus.DOWNLOADING

    @pytest.mark.asyncio
    async def test_retry_failed_torrent_download_uses_torrent_client_method(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory, status=IssueStatus.OWNED)
        download_id = await _seed_download(
            db_factory,
            issue_id,
            client_type=DownloadClientType.QBITTORRENT,
            download_url="https://example.com/batman-004.torrent",
            downloaded_path=None,
            error_message="Cancelled by user",
        )
        mock_client = AsyncMock()
        mock_client.client_type = "qbittorrent"
        mock_client.add_torrent = AsyncMock(return_value="torrent-hash")

        with patch(
            "pullbox.composition.providers.register_download_clients",
            new_callable=AsyncMock,
        ) as mock_register:

            async def _register(_session: object, registry: object) -> None:
                registry.register_download_client(2, mock_client)  # type: ignore[union-attr]

            mock_register.side_effect = _register

            response = await client.post(f"/api/v1/downloads/{download_id}/retry")

        assert response.status_code == 200
        mock_client.add_torrent.assert_awaited_once_with(
            "https://example.com/batman-004.torrent",
            "Batman 004 (2025)",
        )
        download = await _get_download(db_factory, download_id)
        assert download.external_id == "torrent-hash"
        issue = await _get_issue(db_factory, issue_id)
        assert issue.status == IssueStatus.DOWNLOADING

    @pytest.mark.asyncio
    async def test_retry_failed_torrent_returns_provider_error_without_advancing_state(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory, status=IssueStatus.OWNED)
        download_id = await _seed_download(
            db_factory,
            issue_id,
            client_type=DownloadClientType.QBITTORRENT,
            download_url="https://example.com/absolute-wonder-woman-012.torrent",
            downloaded_path=None,
            error_message="Cancelled by user",
        )
        provider_message = "Torrent was not added to qBittorrent."
        mock_client = AsyncMock()
        mock_client.client_type = "qbittorrent"
        mock_client.add_torrent = AsyncMock(side_effect=QBittorrentError(provider_message))

        with patch(
            "pullbox.composition.providers.register_download_clients",
            new_callable=AsyncMock,
        ) as mock_register:

            async def _register(_session: object, registry: object) -> None:
                registry.register_download_client(2, mock_client)  # type: ignore[union-attr]

            mock_register.side_effect = _register

            response = await client.post(f"/api/v1/downloads/{download_id}/retry")

        assert response.status_code == 502
        assert response.json()["detail"] == provider_message
        download = await _get_download(db_factory, download_id)
        assert download.state == DownloadState.FAILED
        assert download.external_id == "download-ext"
        assert download.error_message == "Cancelled by user"
        issue = await _get_issue(db_factory, issue_id)
        assert issue.status == IssueStatus.OWNED

    @pytest.mark.asyncio
    async def test_retry_download_rejects_post_processing_failures(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory)
        download_id = await _seed_download(
            db_factory,
            issue_id,
            downloaded_path="/downloads/batman-004.cbz",
            error_message="ComicInfo write failed",
        )

        response = await client.post(f"/api/v1/downloads/{download_id}/retry")

        assert response.status_code == 409
        assert response.json()["detail"] == "Use retry-processing for post-processing failures."

    @pytest.mark.asyncio
    async def test_retry_download_requires_configured_client(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory)
        download_id = await _seed_download(db_factory, issue_id, downloaded_path=None)

        with patch(
            "pullbox.composition.providers.register_download_clients",
            new_callable=AsyncMock,
        ):
            response = await client.post(f"/api/v1/downloads/{download_id}/retry")

        assert response.status_code == 503
        assert response.json()["detail"] == "No download client configured for this download type."

    @pytest.mark.asyncio
    async def test_retry_download_rejects_non_failed_rows(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory)
        download_id = await _seed_download(
            db_factory,
            issue_id,
            state=DownloadState.COMPLETED,
            error_message=None,
        )

        response = await client.post(f"/api/v1/downloads/{download_id}/retry")

        assert response.status_code == 409
        assert response.json()["detail"] == "Only failed downloads can be retried."


class TestDownloadRouteFunctions:
    @pytest.mark.asyncio
    async def test_queue_history_and_clear_routes(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory)
        await _seed_download(db_factory, issue_id, state=DownloadState.QUEUED)
        await _seed_download(
            db_factory,
            issue_id,
            state=DownloadState.COMPLETED,
            error_message=None,
        )
        await _seed_download(
            db_factory,
            issue_id,
            state=DownloadState.IMPORTED,
            downloaded_path="/downloads/imported.cbz",
            error_message=None,
        )

        async with db_factory() as session:
            queue = await downloads_api.download_queue(object(), session)  # type: ignore[arg-type]
            history = await downloads_api.download_history(object(), session, limit=10, offset=0)  # type: ignore[arg-type]
            deleted_downloads = await downloads_api.clear_download_history(  # type: ignore[arg-type]
                object(),
                session,
            )
            deleted_post_processing = await downloads_api.clear_post_processing_history(  # type: ignore[arg-type]
                object(),
                session,
            )
            await session.commit()

        assert [item.state for item in queue] == [DownloadState.QUEUED]
        assert queue[0].series_title == "Batman"
        assert history.total == 1
        assert history.items[0].series_title == "Batman"
        assert deleted_downloads == {"deleted": 1}
        assert deleted_post_processing == {"deleted": 1}

    @pytest.mark.asyncio
    async def test_retry_processing_route_requeues_failed_downloaded_file(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory)
        download_id = await _seed_download(
            db_factory,
            issue_id,
            downloaded_path="/downloads/batman-004.cbz",
            error_message="File placement failed",
        )
        scheduler = MagicMock()

        async with db_factory() as session:
            with patch("pullbox.core.scheduler.get_scheduler", return_value=scheduler):
                result = await downloads_api.retry_post_processing(  # type: ignore[arg-type]
                    download_id,
                    object(),
                    session,
                )
            await session.commit()

        assert result == {"status": "queued"}
        scheduler.run_task_now.assert_called_once_with("process_completed")
        download = await _get_download(db_factory, download_id)
        assert download.state == DownloadState.COMPLETED

    @pytest.mark.asyncio
    async def test_blocklist_failed_download_route_creates_entry(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory)
        download_id = await _seed_download(
            db_factory,
            issue_id,
            title="Batman 004 (2025) (Digital) (Empire)",
            error_message="CRC failed",
        )

        async with db_factory() as session:
            response = await downloads_api.blocklist_failed_download(  # type: ignore[arg-type]
                download_id,
                object(),
                session,
            )
            await session.commit()

        assert response.release_title == "Batman 004 (2025) (Digital) (Empire)"
        assert response.reason.value == "failed"
        assert response.release_group == "Empire"
        assert response.download_history_id == download_id
        assert response.series_title == "Batman"

    @pytest.mark.asyncio
    async def test_blocklist_failed_download_route_rejects_duplicate(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory)
        download_id = await _seed_download(db_factory, issue_id)

        async with db_factory() as session:
            await downloads_api.blocklist_failed_download(download_id, object(), session)  # type: ignore[arg-type]
            with pytest.raises(Exception) as exc_info:
                await downloads_api.blocklist_failed_download(download_id, object(), session)  # type: ignore[arg-type]

        assert "Release already in blocklist" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_retry_download_route_sends_usenet_and_updates_issue(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory, status=IssueStatus.OWNED)
        download_id = await _seed_download(
            db_factory,
            issue_id,
            downloaded_path=None,
            error_message="Download failed",
            completed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        mock_client = AsyncMock()
        mock_client.client_type = "sabnzbd"
        mock_client.add_nzb = AsyncMock(return_value="resent-id")

        async with db_factory() as session:
            with patch(
                "pullbox.composition.providers.register_download_clients",
                new_callable=AsyncMock,
            ) as mock_register:

                async def _register(_session: object, registry: object) -> None:
                    registry.register_download_client(1, mock_client)  # type: ignore[union-attr]

                mock_register.side_effect = _register
                result = await downloads_api.retry_download(download_id, object(), session)  # type: ignore[arg-type]
            await session.commit()

        assert result == {"status": "sent"}
        mock_client.add_nzb.assert_awaited_once_with(
            "https://example.com/batman-004.nzb",
            "Batman 004 (2025)",
        )
        download = await _get_download(db_factory, download_id)
        assert download.state == DownloadState.SENT
        assert download.external_id == "resent-id"
        issue = await _get_issue(db_factory, issue_id)
        assert issue.status == IssueStatus.DOWNLOADING

    @pytest.mark.asyncio
    async def test_retry_download_route_uses_exact_client_config(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_client_config(
            db_factory,
            config_id=22,
            client_type=DownloadClientType.SABNZBD,
        )
        issue_id = await _seed_issue(db_factory)
        download_id = await _seed_download(
            db_factory,
            issue_id,
            downloaded_path=None,
            download_client_config_id=22,
        )
        first = AsyncMock(client_type="sabnzbd")
        first.add_nzb = AsyncMock(return_value="wrong-client")
        exact = AsyncMock(client_type="sabnzbd")
        exact.add_nzb = AsyncMock(return_value="exact-client")

        async with db_factory() as session:
            with patch(
                "pullbox.composition.providers.register_download_clients",
                new_callable=AsyncMock,
            ) as mock_register:

                async def _register(_session: object, registry: object) -> None:
                    registry.register_download_client(11, first)  # type: ignore[union-attr]
                    registry.register_download_client(22, exact)  # type: ignore[union-attr]

                mock_register.side_effect = _register
                await downloads_api.retry_download(download_id, object(), session)  # type: ignore[arg-type]

        exact.add_nzb.assert_awaited_once()
        first.add_nzb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retry_download_route_sends_torrent(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory)
        download_id = await _seed_download(
            db_factory,
            issue_id,
            client_type=DownloadClientType.QBITTORRENT,
            download_url="https://example.com/batman-004.torrent",
            downloaded_path=None,
            error_message="Cancelled by user",
        )
        mock_client = AsyncMock()
        mock_client.client_type = "qbittorrent"
        mock_client.add_torrent = AsyncMock(return_value="torrent-hash")

        async with db_factory() as session:
            with patch(
                "pullbox.composition.providers.register_download_clients",
                new_callable=AsyncMock,
            ) as mock_register:

                async def _register(_session: object, registry: object) -> None:
                    registry.register_download_client(1, mock_client)  # type: ignore[union-attr]

                mock_register.side_effect = _register
                result = await downloads_api.retry_download(download_id, object(), session)  # type: ignore[arg-type]
            await session.commit()

        assert result == {"status": "sent"}
        mock_client.add_torrent.assert_awaited_once_with(
            "https://example.com/batman-004.torrent",
            "Batman 004 (2025)",
        )

    @pytest.mark.asyncio
    async def test_retry_download_reports_torznab_descriptor_failure(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory)
        async with db_factory() as session:
            config = IndexerConfig(
                name="Challenged Torznab",
                indexer_type=IndexerType.TORZNAB,
                source=IndexerSource.MANUAL,
                url="https://indexer.example",
                api_key="encrypted",
                enabled=True,
                resolver_enabled=True,
            )
            session.add(config)
            await session.flush()
            indexer_id = config.id
            await session.commit()
        download_id = await _seed_download(
            db_factory,
            issue_id,
            client_type=DownloadClientType.QBITTORRENT,
            download_url="https://indexer.example/api?t=get&id=7&apikey=secret",
            downloaded_path=None,
            error_message="Descriptor failed",
            indexer_id=indexer_id,
        )
        mock_client = AsyncMock()
        mock_client.client_type = "qbittorrent"
        mock_indexer = AsyncMock()
        mock_indexer.browser_resolver_enabled = True
        mock_indexer.fetch_torrent_descriptor = AsyncMock(
            side_effect=NewznabError("The Torznab descriptor is unavailable."),
        )

        with (
            patch(
                "pullbox.composition.providers.register_download_clients",
                new_callable=AsyncMock,
            ) as mock_register_clients,
            patch(
                "pullbox.composition.providers.register_indexers",
                new_callable=AsyncMock,
            ) as mock_register_indexers,
        ):

            async def _register_clients(_session: object, registry: object) -> None:
                registry.register_download_client(1, mock_client)  # type: ignore[union-attr]

            async def _register_indexers(_session: object, registry: object) -> None:
                registry.register_indexer(indexer_id, mock_indexer)  # type: ignore[union-attr]

            mock_register_clients.side_effect = _register_clients
            mock_register_indexers.side_effect = _register_indexers
            response = await client.post(f"/api/v1/downloads/{download_id}/retry")

        assert response.status_code == 502
        assert response.json()["detail"] == "The Torznab descriptor is unavailable."
        download = await _get_download(db_factory, download_id)
        assert download.state == DownloadState.FAILED
        assert download.error_message == "Descriptor failed"

    @pytest.mark.asyncio
    async def test_cancel_download_route_cancels_active_and_deletes_terminal_rows(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory, status=IssueStatus.DOWNLOADING)
        active_id = await _seed_download(
            db_factory,
            issue_id,
            state=DownloadState.DOWNLOADING,
            error_message=None,
        )
        terminal_id = await _seed_download(
            db_factory,
            issue_id,
            title="Old history row",
            state=DownloadState.COMPLETED,
            error_message=None,
        )
        mock_client = AsyncMock()
        mock_client.client_type = "sabnzbd"
        mock_client.remove_download = AsyncMock(return_value=True)

        async with db_factory() as session:
            with (
                patch(
                    "pullbox.composition.providers.register_download_clients",
                    new_callable=AsyncMock,
                ) as mock_register,
                patch("pullbox.tasks.download_task._clear_progress") as clear_progress,
            ):

                async def _register(_session: object, registry: object) -> None:
                    registry.register_download_client(1, mock_client)  # type: ignore[union-attr]

                mock_register.side_effect = _register
                await downloads_api.cancel_download(active_id, object(), session)  # type: ignore[arg-type]
                await downloads_api.cancel_download(terminal_id, object(), session)  # type: ignore[arg-type]
            await session.commit()

        mock_client.remove_download.assert_awaited_once_with("download-ext", delete_files=True)
        clear_progress.assert_called_once_with(active_id)
        download = await _get_download(db_factory, active_id)
        assert download.state == DownloadState.FAILED
        assert download.error_message == "Cancelled by user"
        issue = await _get_issue(db_factory, issue_id)
        assert issue.status == IssueStatus.WANTED
        async with db_factory() as session:
            deleted = await session.get(DownloadHistory, terminal_id)
        assert deleted is None

    @pytest.mark.asyncio
    async def test_cancel_download_route_uses_exact_client_config(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_client_config(
            db_factory,
            config_id=22,
            client_type=DownloadClientType.SABNZBD,
        )
        issue_id = await _seed_issue(db_factory, status=IssueStatus.DOWNLOADING)
        download_id = await _seed_download(
            db_factory,
            issue_id,
            state=DownloadState.DOWNLOADING,
            error_message=None,
            download_client_config_id=22,
        )
        first = AsyncMock(client_type="sabnzbd")
        first.remove_download = AsyncMock(return_value=True)
        exact = AsyncMock(client_type="sabnzbd")
        exact.remove_download = AsyncMock(return_value=True)

        async with db_factory() as session:
            with patch(
                "pullbox.composition.providers.register_download_clients",
                new_callable=AsyncMock,
            ) as mock_register:

                async def _register(_session: object, registry: object) -> None:
                    registry.register_download_client(11, first)  # type: ignore[union-attr]
                    registry.register_download_client(22, exact)  # type: ignore[union-attr]

                mock_register.side_effect = _register
                await downloads_api.cancel_download(download_id, object(), session)  # type: ignore[arg-type]

        exact.remove_download.assert_awaited_once()
        first.remove_download.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancel_download_routes_direct_rows_to_native_runner(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory, status=IssueStatus.DOWNLOADING)
        download_id = await _seed_download(
            db_factory,
            issue_id,
            state=DownloadState.DOWNLOADING,
            client_type=DownloadClientType.DIRECT,
            download_url="pullbox-direct://attempt/81",
            external_id="direct:81",
            error_message=None,
        )
        runner = MagicMock(cancel=AsyncMock(return_value=True))

        async with db_factory() as session:
            with (
                patch(
                    "pullbox.tasks.direct_acquisition_task.get_direct_acquisition_runner",
                    return_value=runner,
                ),
                patch(
                    "pullbox.composition.providers.register_download_clients",
                    new_callable=AsyncMock,
                ) as register_clients,
                patch("pullbox.tasks.download_task._clear_progress"),
            ):
                await downloads_api.cancel_download(download_id, object(), session)  # type: ignore[arg-type]
            await session.commit()

        runner.cancel.assert_awaited_once_with(81)
        register_clients.assert_not_awaited()
        download = await _get_download(db_factory, download_id)
        assert download.state is DownloadState.FAILED
        assert download.error_message == "Cancelled by user"

    @pytest.mark.asyncio
    async def test_cancel_download_removes_exact_airdcpp_bundle_without_files(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory, status=IssueStatus.DOWNLOADING)
        config_id = 77
        await _seed_client_config(
            db_factory,
            config_id=config_id,
            client_type=DownloadClientType.AIRDCPP,
        )
        download_id = await _seed_download(
            db_factory,
            issue_id,
            state=DownloadState.DOWNLOADING,
            client_type=DownloadClientType.AIRDCPP,
            external_id=f"airdcpp:{config_id}:bundle:91",
            download_client_config_id=config_id,
        )
        async with db_factory() as session:
            acquisition = AirDcppAcquisition(
                download_history_id=download_id,
                request_key="cancel-airdcpp",
                client_config_id=config_id,
                client_identity=f"airdcpp:{config_id}",
                tth="CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y",
                size_bytes=100_000_000,
                original_name="Batman 004 (2025).cbz",
                bundle_id=91,
                client_state="queued",
            )
            session.add(acquisition)
            await session.flush()
            acquisition_id = acquisition.id
            await session.commit()

        api = AsyncMock()
        supervisor = SimpleNamespace(
            state=AirDcppSupervisorState.READY,
            api_client=api,
        )
        registry = SimpleNamespace(
            get=lambda selected: supervisor if selected == config_id else None
        )
        with (
            patch(
                "pullbox.composition.airdcpp.get_airdcpp_supervisor_registry",
                return_value=registry,
            ),
            patch("pullbox.tasks.download_task._clear_progress"),
        ):
            async with db_factory() as session:
                await downloads_api.cancel_download(download_id, object(), session)  # type: ignore[arg-type]
                await session.commit()

        api.remove_queue_bundle.assert_awaited_once_with(91)
        async with db_factory() as session:
            download = await session.get(DownloadHistory, download_id)
            acquisition = await session.get(AirDcppAcquisition, acquisition_id)
            issue = await session.get(Issue, issue_id)
            assert download is not None and acquisition is not None and issue is not None
            assert download.state is DownloadState.FAILED
            assert acquisition.client_state == "cancelled"
            assert issue.status is IssueStatus.WANTED

    @pytest.mark.asyncio
    async def test_cancel_airdcpp_download_marks_pre_bundle_intent_cancelled(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory, status=IssueStatus.DOWNLOADING)
        config_id = 78
        await _seed_client_config(
            db_factory,
            config_id=config_id,
            client_type=DownloadClientType.AIRDCPP,
        )
        download_id = await _seed_download(
            db_factory,
            issue_id,
            state=DownloadState.QUEUED,
            client_type=DownloadClientType.AIRDCPP,
            external_id=None,
            download_client_config_id=config_id,
            error_message=None,
        )
        async with db_factory() as session:
            acquisition = AirDcppAcquisition(
                download_history_id=download_id,
                request_key="cancel-airdcpp-pre-bundle",
                client_config_id=config_id,
                client_identity=f"airdcpp:{config_id}",
                tth="CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y",
                size_bytes=100_000_000,
                original_name="Batman 004 (2025).cbz",
                client_state="mutation_pending",
            )
            session.add(acquisition)
            await session.flush()
            acquisition_id = acquisition.id
            await session.commit()

        with patch("pullbox.composition.airdcpp.get_airdcpp_supervisor_registry") as get_registry:
            async with db_factory() as session:
                await downloads_api.cancel_download(download_id, object(), session)  # type: ignore[arg-type]
                await session.commit()

        get_registry.assert_not_called()
        async with db_factory() as session:
            download = await session.get(DownloadHistory, download_id)
            acquisition = await session.get(AirDcppAcquisition, acquisition_id)
            issue = await session.get(Issue, issue_id)
            assert download is not None and acquisition is not None and issue is not None
            assert download.state is DownloadState.FAILED
            assert download.error_message == "Cancelled by user"
            assert acquisition.bundle_id is None
            assert acquisition.client_state == "cancelled"
            assert acquisition.next_retry_at is None
            assert issue.status is IssueStatus.WANTED

    @pytest.mark.asyncio
    async def test_cancel_airdcpp_download_preserves_active_state_when_client_unavailable(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory, status=IssueStatus.DOWNLOADING)
        config_id = 79
        await _seed_client_config(
            db_factory,
            config_id=config_id,
            client_type=DownloadClientType.AIRDCPP,
        )
        download_id = await _seed_download(
            db_factory,
            issue_id,
            state=DownloadState.DOWNLOADING,
            client_type=DownloadClientType.AIRDCPP,
            external_id=f"airdcpp:{config_id}:bundle:93",
            download_client_config_id=config_id,
            error_message=None,
        )
        async with db_factory() as session:
            acquisition = AirDcppAcquisition(
                download_history_id=download_id,
                request_key="cancel-airdcpp-unavailable",
                client_config_id=config_id,
                client_identity=f"airdcpp:{config_id}",
                tth="CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y",
                size_bytes=100_000_000,
                original_name="Batman 004 (2025).cbz",
                bundle_id=93,
                client_state="queued",
            )
            session.add(acquisition)
            await session.flush()
            acquisition_id = acquisition.id
            await session.commit()

        registry = SimpleNamespace(get=lambda _selected: None)
        with patch(
            "pullbox.composition.airdcpp.get_airdcpp_supervisor_registry",
            return_value=registry,
        ):
            async with db_factory() as session:
                with pytest.raises(HTTPException) as exc_info:
                    await downloads_api.cancel_download(download_id, object(), session)  # type: ignore[arg-type]
                await session.rollback()

        assert exc_info.value.status_code == 503
        async with db_factory() as session:
            download = await session.get(DownloadHistory, download_id)
            acquisition = await session.get(AirDcppAcquisition, acquisition_id)
            issue = await session.get(Issue, issue_id)
            assert download is not None and acquisition is not None and issue is not None
            assert download.state is DownloadState.DOWNLOADING
            assert download.error_message is None
            assert acquisition.client_state == "queued"
            assert issue.status is IssueStatus.DOWNLOADING

    @pytest.mark.asyncio
    async def test_retry_airdcpp_download_schedules_bounded_exact_bundle_source_search(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory, status=IssueStatus.WANTED)
        config_id = 78
        await _seed_client_config(
            db_factory,
            config_id=config_id,
            client_type=DownloadClientType.AIRDCPP,
        )
        download_id = await _seed_download(
            db_factory,
            issue_id,
            state=DownloadState.FAILED,
            client_type=DownloadClientType.AIRDCPP,
            external_id=f"airdcpp:{config_id}:bundle:92",
            download_client_config_id=config_id,
            error_message="AirDC++ reported a fatal download error.",
        )
        async with db_factory() as session:
            acquisition = AirDcppAcquisition(
                download_history_id=download_id,
                request_key="retry-airdcpp",
                client_config_id=config_id,
                client_identity=f"airdcpp:{config_id}",
                tth="CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y",
                size_bytes=100_000_000,
                original_name="Batman 004 (2025).cbz",
                bundle_id=92,
                client_state="download_error",
            )
            session.add(acquisition)
            await session.flush()
            acquisition_id = acquisition.id
            await session.commit()

        async with db_factory() as session:
            response = await downloads_api.retry_download(download_id, object(), session)  # type: ignore[arg-type]
            await session.commit()

        assert response == {"status": "sent"}
        async with db_factory() as session:
            download = await session.get(DownloadHistory, download_id)
            acquisition = await session.get(AirDcppAcquisition, acquisition_id)
            issue = await session.get(Issue, issue_id)
            assert download is not None and acquisition is not None and issue is not None
            assert download.state is DownloadState.RETRY_PENDING
            assert acquisition.client_state == "source_search_pending"
            assert acquisition.next_retry_at is not None
            assert issue.status is IssueStatus.DOWNLOADING

    @pytest.mark.asyncio
    async def test_retry_missing_airdcpp_download_recreates_bundle_from_provenance(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory, status=IssueStatus.WANTED)
        config_id = 80
        await _seed_client_config(
            db_factory,
            config_id=config_id,
            client_type=DownloadClientType.AIRDCPP,
        )
        download_id = await _seed_download(
            db_factory,
            issue_id,
            state=DownloadState.FAILED,
            client_type=DownloadClientType.AIRDCPP,
            external_id=f"airdcpp:{config_id}:bundle:94",
            download_client_config_id=config_id,
            error_message="The AirDC++ queue item was removed outside Pullbox.",
        )
        async with db_factory() as session:
            acquisition = AirDcppAcquisition(
                download_history_id=download_id,
                request_key="retry-missing-airdcpp",
                client_config_id=config_id,
                client_identity=f"airdcpp:{config_id}",
                tth="CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y",
                size_bytes=100_000_000,
                original_name="Batman 004 (2025).cbz",
                bundle_id=94,
                client_state="missing",
                remote_target="/Downloads/Batman 004 (2025).cbz",
                route_snapshot={"queue": {"downloaded_bytes": 10_000_000}},
                reconciliation_error="external_bundle_missing",
            )
            session.add(acquisition)
            await session.flush()
            acquisition_id = acquisition.id
            await session.commit()

        api = AsyncMock()
        api.create_file_bundle.return_value = SimpleNamespace(id=95, merged=False)
        supervisor = SimpleNamespace(
            state=AirDcppSupervisorState.READY,
            api_client=api,
        )
        registry = SimpleNamespace(
            get=lambda selected: supervisor if selected == config_id else None
        )
        with patch(
            "pullbox.composition.airdcpp.get_airdcpp_supervisor_registry",
            return_value=registry,
        ):
            async with db_factory() as session:
                response = await downloads_api.retry_download(download_id, object(), session)  # type: ignore[arg-type]
                await session.commit()

        assert response == {"status": "sent"}
        api.create_file_bundle.assert_awaited_once_with(
            tth="CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y",
            size=100_000_000,
            target_name="Batman 004 (2025).cbz",
            priority=None,
        )
        async with db_factory() as session:
            download = await session.get(DownloadHistory, download_id)
            acquisition = await session.get(AirDcppAcquisition, acquisition_id)
            issue = await session.get(Issue, issue_id)
            assert download is not None and acquisition is not None and issue is not None
            assert download.external_id == f"airdcpp:{config_id}:bundle:95"
            assert download.state is DownloadState.RETRY_PENDING
            assert acquisition.bundle_id == 95
            assert acquisition.client_state == "source_search_pending"
            assert acquisition.remote_target is None
            assert "queue" not in acquisition.route_snapshot
            assert acquisition.next_retry_at is not None
            assert issue.status is IssueStatus.DOWNLOADING

    @pytest.mark.asyncio
    async def test_retry_missing_airdcpp_download_claims_before_remote_mutation(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory, status=IssueStatus.WANTED)
        config_id = 81
        await _seed_client_config(
            db_factory,
            config_id=config_id,
            client_type=DownloadClientType.AIRDCPP,
        )
        download_id = await _seed_download(
            db_factory,
            issue_id,
            state=DownloadState.FAILED,
            client_type=DownloadClientType.AIRDCPP,
            external_id=f"airdcpp:{config_id}:bundle:96",
            download_client_config_id=config_id,
            error_message="The AirDC++ queue item was removed outside Pullbox.",
        )
        async with db_factory() as session:
            session.add(
                AirDcppAcquisition(
                    download_history_id=download_id,
                    request_key="retry-missing-airdcpp-concurrent",
                    client_config_id=config_id,
                    client_identity=f"airdcpp:{config_id}",
                    tth="CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y",
                    size_bytes=100_000_000,
                    original_name="Batman 004 (2025).cbz",
                    bundle_id=96,
                    client_state="missing",
                    reconciliation_error="external_bundle_missing",
                )
            )
            await session.commit()

        mutation_started = asyncio.Event()
        release_mutation = asyncio.Event()

        async def _create_file_bundle(**_kwargs: object) -> SimpleNamespace:
            mutation_started.set()
            await release_mutation.wait()
            return SimpleNamespace(id=97, merged=False)

        api = AsyncMock()
        api.create_file_bundle.side_effect = _create_file_bundle
        supervisor = SimpleNamespace(
            state=AirDcppSupervisorState.READY,
            api_client=api,
        )
        registry = SimpleNamespace(
            get=lambda selected: supervisor if selected == config_id else None
        )

        async def _retry() -> dict[str, str]:
            async with db_factory() as session:
                response = await downloads_api.retry_download(
                    download_id,
                    object(),  # type: ignore[arg-type]
                    session,
                )
                await session.commit()
                return response

        with patch(
            "pullbox.composition.airdcpp.get_airdcpp_supervisor_registry",
            return_value=registry,
        ):
            first = asyncio.create_task(_retry())
            await asyncio.wait_for(mutation_started.wait(), timeout=1)
            second = asyncio.create_task(_retry())
            await asyncio.sleep(0.05)
            release_mutation.set()
            results = await asyncio.gather(first, second, return_exceptions=True)

        assert api.create_file_bundle.await_count == 1
        assert {result["status"] for result in results if isinstance(result, dict)} == {"sent"}
        conflicts = [result for result in results if isinstance(result, HTTPException)]
        assert len(conflicts) == 1
        assert conflicts[0].status_code == 409

    @pytest.mark.parametrize("mutation_fails", [False, True])
    @pytest.mark.asyncio
    async def test_retry_missing_airdcpp_download_preserves_newer_claim(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        mutation_fails: bool,
    ) -> None:
        issue_id = await _seed_issue(db_factory, status=IssueStatus.WANTED)
        config_id = 82
        await _seed_client_config(
            db_factory,
            config_id=config_id,
            client_type=DownloadClientType.AIRDCPP,
        )
        download_id = await _seed_download(
            db_factory,
            issue_id,
            state=DownloadState.FAILED,
            client_type=DownloadClientType.AIRDCPP,
            external_id=f"airdcpp:{config_id}:bundle:96",
            download_client_config_id=config_id,
            error_message="The AirDC++ queue item was removed outside Pullbox.",
        )
        async with db_factory() as session:
            acquisition = AirDcppAcquisition(
                download_history_id=download_id,
                request_key=f"retry-missing-airdcpp-newer-{mutation_fails}",
                client_config_id=config_id,
                client_identity=f"airdcpp:{config_id}",
                tth="CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y",
                size_bytes=100_000_000,
                original_name="Batman 004 (2025).cbz",
                bundle_id=96,
                client_state="missing",
                reconciliation_error="external_bundle_missing",
            )
            session.add(acquisition)
            await session.flush()
            acquisition_id = acquisition.id
            await session.commit()

        newer_deadline = datetime.now(UTC) + timedelta(minutes=10)

        async def _create_file_bundle(**_kwargs: object) -> SimpleNamespace:
            async with db_factory() as newer_session:
                current_acquisition = await newer_session.get(
                    AirDcppAcquisition,
                    acquisition_id,
                )
                current_download = await newer_session.get(DownloadHistory, download_id)
                assert current_acquisition is not None and current_download is not None
                current_acquisition.client_state = "retry_mutation_pending"
                current_acquisition.next_retry_at = newer_deadline
                current_download.state = DownloadState.RETRY_PENDING
                current_download.next_retry_at = newer_deadline
                current_download.error_message = "A newer retry owns this claim."
                await newer_session.commit()
            if mutation_fails:
                raise AirDcppUnavailableError()
            return SimpleNamespace(id=98, merged=False)

        api = AsyncMock()
        api.create_file_bundle.side_effect = _create_file_bundle
        supervisor = SimpleNamespace(
            state=AirDcppSupervisorState.READY,
            api_client=api,
        )
        registry = SimpleNamespace(
            get=lambda selected: supervisor if selected == config_id else None
        )

        with (
            patch(
                "pullbox.composition.airdcpp.get_airdcpp_supervisor_registry",
                return_value=registry,
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            async with db_factory() as session:
                await downloads_api.retry_download(download_id, object(), session)  # type: ignore[arg-type]

        assert exc_info.value.status_code == (502 if mutation_fails else 409)
        if mutation_fails:
            api.remove_queue_bundle.assert_not_awaited()
        else:
            api.remove_queue_bundle.assert_awaited_once_with(98)
        async with db_factory() as session:
            acquisition = await session.get(AirDcppAcquisition, acquisition_id)
            download = await session.get(DownloadHistory, download_id)
            assert acquisition is not None and download is not None
            assert acquisition.bundle_id == 96
            assert acquisition.client_state == "retry_mutation_pending"
            assert acquisition.next_retry_at == newer_deadline
            assert download.state is DownloadState.RETRY_PENDING
            assert download.next_retry_at == newer_deadline
            assert download.error_message == "A newer retry owns this claim."

    @pytest.mark.asyncio
    async def test_direct_download_sources_list_only_verified_equivalent_routes(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory, status=IssueStatus.DOWNLOADING)
        download_id, _attempt_id = await _seed_direct_download_with_routes(
            db_factory,
            issue_id,
        )

        response = await client.get(f"/api/v1/downloads/{download_id}/sources")

        assert response.status_code == 200
        assert response.json() == {
            "download_id": download_id,
            "current": {
                "artifact_identity": "route:current",
                "host_kind": "generic_https",
                "host_label": "HTTPS",
                "bytes_transferred": 512,
            },
            "alternatives": [
                {
                    "artifact_identity": "route:pixel",
                    "host_kind": "pixeldrain",
                    "host_label": "PixelDrain",
                    "expected_size": 1_000,
                    "is_next": True,
                }
            ],
        }

    @pytest.mark.asyncio
    async def test_direct_download_source_switch_delegates_atomic_runner_action(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory, status=IssueStatus.DOWNLOADING)
        download_id, attempt_id = await _seed_direct_download_with_routes(
            db_factory,
            issue_id,
        )
        runner = MagicMock()
        runner.switch_source = AsyncMock(
            return_value=SimpleNamespace(
                previous_host=DirectArtifactHostKind.GENERIC_HTTPS,
                selected=SimpleNamespace(host_kind=DirectArtifactHostKind.PIXELDRAIN),
                current_route_blocklisted=True,
            )
        )

        with patch(
            "pullbox.tasks.direct_acquisition_task.get_direct_acquisition_runner",
            return_value=runner,
        ):
            response = await client.post(
                f"/api/v1/downloads/{download_id}/switch-source",
                json={
                    "artifact_identity": "route:pixel",
                    "block_current": True,
                },
            )

        assert response.status_code == 200
        assert response.json() == {
            "status": "queued",
            "previous_host": "HTTPS",
            "selected_host": "PixelDrain",
            "current_route_blocklisted": True,
        }
        runner.switch_source.assert_awaited_once_with(
            attempt_id,
            target_artifact_identity="route:pixel",
            block_current=True,
        )

    @pytest.mark.asyncio
    async def test_source_switch_rejects_non_direct_download(
        self,
        client: AsyncClient,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory)
        download_id = await _seed_download(
            db_factory,
            issue_id,
            state=DownloadState.DOWNLOADING,
        )

        response = await client.post(
            f"/api/v1/downloads/{download_id}/switch-source",
            json={"artifact_identity": None, "block_current": False},
        )

        assert response.status_code == 409
        assert response.json()["detail"] == "Only direct downloads can change sources."

    @pytest.mark.asyncio
    async def test_route_error_branches_raise_expected_http_errors(
        self,
        db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        issue_id = await _seed_issue(db_factory)
        failed_no_file_id = await _seed_download(
            db_factory,
            issue_id,
            title="Failed without file",
            downloaded_path=None,
        )
        completed_id = await _seed_download(
            db_factory,
            issue_id,
            title="Completed row",
            state=DownloadState.COMPLETED,
            error_message=None,
        )
        cancelled_id = await _seed_download(
            db_factory,
            issue_id,
            title="Cancelled row",
            error_message="Cancelled by user",
        )
        post_processing_failure_id = await _seed_download(
            db_factory,
            issue_id,
            title="Post processing failure",
            downloaded_path="/downloads/batman-004.cbz",
            error_message="ComicInfo write failed",
        )

        async with db_factory() as session:
            with pytest.raises(NotFoundError):
                await downloads_api.retry_post_processing(999_001, object(), session)  # type: ignore[arg-type]
            with pytest.raises(NotFoundError):
                await downloads_api.blocklist_failed_download(999_002, object(), session)  # type: ignore[arg-type]
            with pytest.raises(NotFoundError):
                await downloads_api.retry_download(999_003, object(), session)  # type: ignore[arg-type]
            with pytest.raises(NotFoundError):
                await downloads_api.cancel_download(999_004, object(), session)  # type: ignore[arg-type]

            with pytest.raises(HTTPException) as retry_processing_error:
                await downloads_api.retry_post_processing(failed_no_file_id, object(), session)  # type: ignore[arg-type]
            with pytest.raises(HTTPException) as blocklist_non_failed_error:
                await downloads_api.blocklist_failed_download(completed_id, object(), session)  # type: ignore[arg-type]
            with pytest.raises(HTTPException) as blocklist_cancelled_error:
                await downloads_api.blocklist_failed_download(cancelled_id, object(), session)  # type: ignore[arg-type]
            with pytest.raises(HTTPException) as retry_non_failed_error:
                await downloads_api.retry_download(completed_id, object(), session)  # type: ignore[arg-type]
            with pytest.raises(HTTPException) as retry_pp_failure_error:
                await downloads_api.retry_download(post_processing_failure_id, object(), session)  # type: ignore[arg-type]
            with (
                patch(
                    "pullbox.composition.providers.register_download_clients",
                    new_callable=AsyncMock,
                ),
                pytest.raises(HTTPException) as retry_no_client_error,
            ):
                await downloads_api.retry_download(failed_no_file_id, object(), session)  # type: ignore[arg-type]

        assert retry_processing_error.value.status_code == 409
        assert blocklist_non_failed_error.value.status_code == 409
        assert blocklist_cancelled_error.value.status_code == 409
        assert retry_non_failed_error.value.status_code == 409
        assert retry_pp_failure_error.value.status_code == 409
        assert retry_no_client_error.value.status_code == 503
