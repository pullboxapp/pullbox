"""Latency-focused tests for download monitoring and handoff behavior."""

from __future__ import annotations

import os
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.client import DownloadClientConfig
from pullbox.models.config import SystemConfig
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-download-task-latency")


class _FakeLogger:
    """Minimal structlog-like logger for asserting emitted events."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def bind(self, **kwargs: object) -> _FakeLogger:
        del kwargs
        return self

    def info(self, event: str, **kwargs: object) -> None:
        self.events.append(("info", event, kwargs))

    def debug(self, event: str, **kwargs: object) -> None:
        self.events.append(("debug", event, kwargs))

    def warning(self, event: str, **kwargs: object) -> None:
        self.events.append(("warning", event, kwargs))

    def error(self, event: str, **kwargs: object) -> None:
        self.events.append(("error", event, kwargs))

    def exception(self, event: str, **kwargs: object) -> None:
        self.events.append(("exception", event, kwargs))


@pytest.fixture
async def db_factory():
    """Create an isolated async DB factory for task tests."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_active_download(factory: async_sessionmaker[AsyncSession]) -> int:
    """Seed a single active download row and return its ID."""
    async with factory() as session:
        series = Series(title="Batman", sort_title="batman")
        session.add(series)
        await session.flush()

        issue = Issue(series_id=series.id, issue_number=1.0, status=IssueStatus.DOWNLOADING)
        session.add(issue)
        await session.flush()

        download = DownloadHistory(
            title="Batman 001 (2024) (Digital).cbz",
            state=DownloadState.DOWNLOADING,
            download_client=DownloadClientType.SABNZBD,
            download_url="https://example.com/batman-001.nzb",
            external_id="sab-download-001",
            issue_id=issue.id,
            sent_at=datetime(2026, 4, 7, 15, 0, tzinfo=UTC),
            updated_at=datetime(2026, 4, 7, 15, 0, tzinfo=UTC),
        )
        session.add(download)
        await session.commit()
        return download.id


async def _seed_retry_pending_download(factory: async_sessionmaker[AsyncSession]) -> int:
    """Seed a retry-pending download that is due for retry."""
    async with factory() as session:
        series = Series(title="Wolverine", sort_title="wolverine")
        session.add(series)
        await session.flush()

        issue = Issue(series_id=series.id, issue_number=15.0, status=IssueStatus.DOWNLOADING)
        session.add(issue)
        await session.flush()

        download = DownloadHistory(
            title="Wolverine #15",
            state=DownloadState.RETRY_PENDING,
            download_client=DownloadClientType.QBITTORRENT,
            download_url="magnet:?xt=urn:btih:retry",
            external_id="retry-hash",
            issue_id=issue.id,
            retry_count=1,
            max_retries=3,
            next_retry_at=datetime(2026, 4, 7, 14, 0, tzinfo=UTC),
            sent_at=datetime(2026, 4, 7, 13, 0, tzinfo=UTC),
            updated_at=datetime(2026, 4, 7, 13, 45, tzinfo=UTC),
        )
        session.add(download)
        await session.commit()
        return download.id


async def _seed_post_processing_retry_pair(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int]:
    """Seed one active completed import and one failed post-processing row."""
    async with factory() as session:
        series = Series(title="Absolute Superman", sort_title="absolute superman")
        session.add(series)
        await session.flush()

        first_issue = Issue(series_id=series.id, issue_number=9.0, status=IssueStatus.DOWNLOADING)
        second_issue = Issue(series_id=series.id, issue_number=16.0, status=IssueStatus.DOWNLOADING)
        session.add_all([first_issue, second_issue])
        await session.flush()

        first = DownloadHistory(
            title="Absolute Superman 009.cbz",
            state=DownloadState.COMPLETED,
            download_client=DownloadClientType.SABNZBD,
            download_url="https://example.com/absolute-superman-009.nzb",
            downloaded_path="/downloads/comics/Absolute Superman 009.cbz",
            issue_id=first_issue.id,
            completed_at=datetime(2026, 5, 1, 6, 0, tzinfo=UTC),
            updated_at=datetime(2026, 5, 1, 6, 0, tzinfo=UTC),
        )
        second = DownloadHistory(
            title="Absolute Superman 016.cbr",
            state=DownloadState.FAILED,
            download_client=DownloadClientType.SABNZBD,
            download_url="https://example.com/absolute-superman-016.nzb",
            downloaded_path="/downloads/comics/Absolute Superman 016.cbr",
            issue_id=second_issue.id,
            completed_at=datetime(2026, 5, 1, 6, 1, tzinfo=UTC),
            updated_at=datetime(2026, 5, 1, 6, 1, tzinfo=UTC),
            error_message="Source file vanished",
        )
        session.add_all([first, second])
        await session.commit()
        return first.id, second.id


def _create_valid_cbz(path: Path) -> Path:
    """Create a minimal CBZ archive accepted by the quick integrity checker."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("page001.jpg", b"\xff\xd8\xff\xd9")
    return path


def _create_inspectable_bad_release_cbz(path: Path) -> Path:
    """Create a CBZ that is safe to inspect but fails comic integrity checks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("metadata.txt", b"not a comic page")
    return path


class TestMonitorDownloadsImmediateHandoff:
    """Completed downloads should immediately trigger post-processing."""

    @pytest.mark.asyncio
    async def test_retry_pending_recovery_runs_without_active_downloads(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pullbox.tasks.download_task as download_task

        await _seed_retry_pending_download(db_factory)

        fake_service = MagicMock()
        retry_pending = AsyncMock(return_value=1)
        recover_orphans = AsyncMock(return_value=0)
        poll_clients = AsyncMock(return_value=[])

        monkeypatch.setattr(download_task, "get_session_factory", lambda: db_factory)
        monkeypatch.setattr(
            download_task,
            "_build_download_registry",
            AsyncMock(return_value=object()),
        )
        monkeypatch.setattr(
            download_task,
            "DownloadService",
            lambda registry, event_bus: fake_service,
        )
        monkeypatch.setattr(download_task, "_process_retry_pending", retry_pending)
        monkeypatch.setattr(download_task, "_recover_orphaned_downloads", recover_orphans)
        monkeypatch.setattr(download_task, "_poll_download_clients", poll_clients)
        monkeypatch.setattr(download_task, "_last_recovery_check", 0.0)
        monkeypatch.setattr(download_task, "logger", _FakeLogger())

        await download_task.monitor_downloads()

        retry_pending.assert_awaited_once()
        recover_orphans.assert_awaited_once()
        poll_clients.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_direct_retry_recovery_runs_without_download_client_registry(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pullbox.tasks.download_task as download_task

        direct_retry = AsyncMock(return_value=1)
        monkeypatch.setattr(download_task, "get_session_factory", lambda: db_factory)
        monkeypatch.setattr(
            download_task,
            "_build_download_registry",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(download_task, "_process_direct_retry_pending", direct_retry)
        monkeypatch.setattr(download_task, "_last_recovery_check", 0.0)
        monkeypatch.setattr(download_task, "logger", _FakeLogger())

        await download_task.monitor_downloads()

        direct_retry.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_completed_download_triggers_immediate_post_processing(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pullbox.tasks.download_task as download_task

        dl_id = await _seed_active_download(db_factory)

        fake_client = MagicMock()
        fake_client.get_download_status = AsyncMock(
            return_value=SimpleNamespace(
                state="completed",
                progress=1.0,
                speed_bytes=None,
                eta_seconds=None,
                size_bytes=42_000_000,
                downloaded_path="/downloads/batman-001.cbz",
                error_message=None,
                client_state="Completed",
            )
        )
        fake_service = MagicMock()
        fake_service.get_client_for_identity.return_value = fake_client
        trigger = MagicMock()
        fake_logger = _FakeLogger()

        monkeypatch.setattr(download_task, "get_session_factory", lambda: db_factory)
        monkeypatch.setattr(
            download_task,
            "_build_download_registry",
            AsyncMock(return_value=object()),
        )
        monkeypatch.setattr(
            download_task,
            "DownloadService",
            lambda registry, event_bus: fake_service,
        )
        monkeypatch.setattr(download_task, "_process_retry_pending", AsyncMock(return_value=0))
        monkeypatch.setattr(download_task, "_recover_orphaned_downloads", AsyncMock(return_value=0))
        monkeypatch.setattr(download_task, "_trigger_process_completed_now", trigger)
        monkeypatch.setattr(download_task, "_last_recovery_check", time.monotonic())
        monkeypatch.setattr(download_task, "logger", fake_logger)

        await download_task.monitor_downloads()

        trigger.assert_called_once_with()
        assert any(
            event == "download_completion_detected" and payload.get("download_id") == dl_id
            for _, event, payload in fake_logger.events
        )
        async with db_factory() as session:
            download = await session.get(DownloadHistory, dl_id)
            issue = await session.get(Issue, download.issue_id) if download else None

        assert download is not None
        assert download.state == DownloadState.COMPLETED
        assert download.completed_at is not None
        assert download.downloaded_path == "/downloads/batman-001.cbz"
        assert issue is not None
        assert issue.status == IssueStatus.DOWNLOADING

        summaries = [
            payload
            for _, event, payload in fake_logger.events
            if event == "download_lifecycle_summary"
        ]
        assert len(summaries) == 1
        assert summaries[0]["download_id"] == dl_id
        assert summaries[0]["outcome"] == "completed"
        assert summaries[0]["final_state"] == DownloadState.COMPLETED.value
        assert summaries[0]["final_client_state"] == "Completed"
        assert summaries[0]["downloaded_path"] == "/downloads/batman-001.cbz"
        assert summaries[0]["duration_basis"] == "sent_at"
        assert isinstance(summaries[0]["lifecycle_duration_ms"], float)

    @pytest.mark.asyncio
    async def test_failed_download_emits_retry_scheduled_summary_once(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pullbox.tasks.download_task as download_task

        dl_id = await _seed_active_download(db_factory)

        fake_client = MagicMock()
        fake_client.get_download_status = AsyncMock(
            return_value=SimpleNamespace(
                state="failed",
                progress=0.42,
                speed_bytes=None,
                eta_seconds=None,
                size_bytes=42_000_000,
                downloaded_path="/downloads/batman-001.cbz",
                error_message="client failed",
                client_state="Failed",
            )
        )
        fake_service = MagicMock()
        fake_service.get_client_for_identity.return_value = fake_client
        fake_logger = _FakeLogger()

        monkeypatch.setattr(download_task, "get_session_factory", lambda: db_factory)
        monkeypatch.setattr(
            download_task,
            "_build_download_registry",
            AsyncMock(return_value=object()),
        )
        monkeypatch.setattr(
            download_task,
            "DownloadService",
            lambda registry, event_bus: fake_service,
        )
        monkeypatch.setattr(download_task, "_process_retry_pending", AsyncMock(return_value=0))
        monkeypatch.setattr(download_task, "_recover_orphaned_downloads", AsyncMock(return_value=0))
        monkeypatch.setattr(download_task, "_last_recovery_check", time.monotonic())
        monkeypatch.setattr(download_task, "logger", fake_logger)

        await download_task.monitor_downloads()

        summaries = [
            payload
            for _, event, payload in fake_logger.events
            if event == "download_lifecycle_summary"
        ]
        assert len(summaries) == 1
        assert summaries[0]["download_id"] == dl_id
        assert summaries[0]["outcome"] == "retry_scheduled"
        assert summaries[0]["final_state"] == DownloadState.RETRY_PENDING.value
        assert summaries[0]["error_message"] == "client failed"
        assert summaries[0]["final_client_state"] == "Failed"

    @pytest.mark.asyncio
    async def test_failed_download_emits_terminal_failure_summary_once(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pullbox.tasks.download_task as download_task

        dl_id = await _seed_active_download(db_factory)
        async with db_factory() as session:
            download = await session.get(DownloadHistory, dl_id)
            assert download is not None
            download.max_retries = 1
            await session.commit()

        fake_client = MagicMock()
        fake_client.get_download_status = AsyncMock(
            return_value=SimpleNamespace(
                state="failed",
                progress=0.42,
                speed_bytes=None,
                eta_seconds=None,
                size_bytes=42_000_000,
                downloaded_path="/downloads/batman-001.cbz",
                error_message="client failed permanently",
                client_state="Failed",
            )
        )
        fake_service = MagicMock()
        fake_service.get_client_for_identity.return_value = fake_client
        fake_logger = _FakeLogger()

        monkeypatch.setattr(download_task, "get_session_factory", lambda: db_factory)
        monkeypatch.setattr(
            download_task,
            "_build_download_registry",
            AsyncMock(return_value=object()),
        )
        monkeypatch.setattr(
            download_task,
            "DownloadService",
            lambda registry, event_bus: fake_service,
        )
        monkeypatch.setattr(download_task, "_process_retry_pending", AsyncMock(return_value=0))
        monkeypatch.setattr(download_task, "_recover_orphaned_downloads", AsyncMock(return_value=0))
        monkeypatch.setattr(download_task, "_last_recovery_check", time.monotonic())
        monkeypatch.setattr(download_task, "logger", fake_logger)

        await download_task.monitor_downloads()

        summaries = [
            payload
            for _, event, payload in fake_logger.events
            if event == "download_lifecycle_summary"
        ]
        assert len(summaries) == 1
        assert summaries[0]["download_id"] == dl_id
        assert summaries[0]["outcome"] == "failed"
        assert summaries[0]["final_state"] == DownloadState.FAILED.value
        assert summaries[0]["error_message"] == "client failed permanently"

    @pytest.mark.asyncio
    async def test_heartbeat_only_poll_does_not_emit_lifecycle_summary(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pullbox.tasks.download_task as download_task

        await _seed_active_download(db_factory)

        fake_client = MagicMock()
        fake_client.get_download_status = AsyncMock(
            return_value=SimpleNamespace(
                state="downloading",
                progress=0.42,
                speed_bytes=1_024,
                eta_seconds=12,
                size_bytes=42_000_000,
                downloaded_path="/downloads/batman-001.cbz",
                error_message=None,
                client_state="Downloading",
            )
        )
        fake_service = MagicMock()
        fake_service.get_client_for_identity.return_value = fake_client
        fake_logger = _FakeLogger()

        monkeypatch.setattr(download_task, "get_session_factory", lambda: db_factory)
        monkeypatch.setattr(
            download_task,
            "_build_download_registry",
            AsyncMock(return_value=object()),
        )
        monkeypatch.setattr(
            download_task,
            "DownloadService",
            lambda registry, event_bus: fake_service,
        )
        monkeypatch.setattr(download_task, "_process_retry_pending", AsyncMock(return_value=0))
        monkeypatch.setattr(download_task, "_recover_orphaned_downloads", AsyncMock(return_value=0))
        monkeypatch.setattr(download_task, "_last_recovery_check", time.monotonic())
        monkeypatch.setattr(download_task, "logger", fake_logger)

        await download_task.monitor_downloads()

        assert not any(event == "download_lifecycle_summary" for _, event, _ in fake_logger.events)

    @pytest.mark.asyncio
    async def test_removed_externally_emits_summary_and_restores_issue(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pullbox.tasks.download_task as download_task

        dl_id = await _seed_active_download(db_factory)

        fake_client = MagicMock()
        fake_client.get_download_status = AsyncMock(side_effect=RuntimeError("not found"))
        fake_service = MagicMock()
        fake_service.get_client_for_identity.return_value = fake_client
        fake_logger = _FakeLogger()

        monkeypatch.setattr(download_task, "get_session_factory", lambda: db_factory)
        monkeypatch.setattr(
            download_task,
            "_build_download_registry",
            AsyncMock(return_value=object()),
        )
        monkeypatch.setattr(
            download_task,
            "DownloadService",
            lambda registry, event_bus: fake_service,
        )
        monkeypatch.setattr(download_task, "_process_retry_pending", AsyncMock(return_value=0))
        monkeypatch.setattr(download_task, "_recover_orphaned_downloads", AsyncMock(return_value=0))
        monkeypatch.setattr(download_task, "_last_recovery_check", time.monotonic())
        monkeypatch.setattr(download_task, "logger", fake_logger)

        await download_task.monitor_downloads()

        summaries = [
            payload
            for _, event, payload in fake_logger.events
            if event == "download_lifecycle_summary"
        ]
        assert len(summaries) == 1
        assert summaries[0]["outcome"] == "removed_externally"
        assert summaries[0]["final_state"] == DownloadState.FAILED.value
        assert summaries[0]["error_message"] == "Download was removed from the client externally"

        async with db_factory() as session:
            download = await session.get(DownloadHistory, dl_id)
            assert download is not None
            issue = await session.get(Issue, download.issue_id)

        assert download.state == DownloadState.FAILED
        assert issue is not None
        assert issue.status == IssueStatus.WANTED


class TestProcessCompletedOverlapGuard:
    """process_completed should not overlap with itself."""

    def test_process_completed_is_registered_as_five_minute_backstop(self) -> None:
        """The scheduled recovery sweep should never revert to a hot 5-second poller."""
        import inspect

        import pullbox.tasks.download_scheduler_task as download_scheduler_task

        source = inspect.getsource(download_scheduler_task)
        assert 'task_id="process_completed"' in source
        assert "seconds=300" in source

    @pytest.mark.asyncio
    async def test_process_completed_skips_when_a_run_is_already_active(self) -> None:
        import pullbox.tasks.download_task as download_task

        await download_task._process_completed_lock.acquire()
        try:
            with patch("pullbox.tasks.download_task.get_session_factory") as mock_factory:
                await download_task.process_completed()
            mock_factory.assert_not_called()
        finally:
            download_task._process_completed_lock.release()

    @pytest.mark.asyncio
    async def test_process_completed_drains_items_queued_mid_run(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pullbox.tasks.download_task as download_task

        first_id, second_id = await _seed_post_processing_retry_pair(db_factory)
        processed_ids: list[int] = []

        async def _fake_run_post_processing(
            session: AsyncSession,
            download: DownloadHistory,
        ) -> None:
            processed_ids.append(download.id)
            if download.id == first_id:
                queued = await session.get(DownloadHistory, second_id)
                assert queued is not None
                queued.state = DownloadState.COMPLETED

        monkeypatch.setattr(download_task, "get_session_factory", lambda: db_factory)
        monkeypatch.setattr(download_task, "_run_post_processing", _fake_run_post_processing)

        await download_task.process_completed()

        assert processed_ids == [first_id, second_id]

        async with db_factory() as session:
            first = await session.get(DownloadHistory, first_id)
            second = await session.get(DownloadHistory, second_id)

        assert first is not None
        assert first.imported_at is not None
        assert first.error_message is None

        assert second is not None
        assert second.state == DownloadState.COMPLETED
        assert second.imported_at is not None
        assert second.error_message is None

    @pytest.mark.asyncio
    async def test_process_completed_logs_handoff_timings_for_success(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pullbox.tasks.download_task as download_task

        first_id, _ = await _seed_post_processing_retry_pair(db_factory)
        fake_logger = _FakeLogger()

        async def _fake_run_post_processing(
            session: AsyncSession,
            download: DownloadHistory,
        ) -> None:
            del session
            del download

        monkeypatch.setattr(download_task, "get_session_factory", lambda: db_factory)
        monkeypatch.setattr(download_task, "_run_post_processing", _fake_run_post_processing)
        monkeypatch.setattr(download_task, "logger", fake_logger)

        await download_task.process_completed()

        assert any(
            event == "post_processing_handoff_started" and payload.get("download_id") == first_id
            for _, event, payload in fake_logger.events
        )
        assert any(
            event == "post_processing_handoff_complete"
            and payload.get("download_id") == first_id
            and isinstance(payload.get("post_processing_duration_ms"), float)
            for _, event, payload in fake_logger.events
        )

    @pytest.mark.asyncio
    async def test_process_completed_logs_handoff_timings_for_failure(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pullbox.tasks.download_task as download_task

        first_id, _ = await _seed_post_processing_retry_pair(db_factory)
        fake_logger = _FakeLogger()

        async def _fake_run_post_processing(
            session: AsyncSession,
            download: DownloadHistory,
        ) -> None:
            del session
            del download
            raise RuntimeError("probe failed")

        monkeypatch.setattr(download_task, "get_session_factory", lambda: db_factory)
        monkeypatch.setattr(download_task, "_run_post_processing", _fake_run_post_processing)
        monkeypatch.setattr(download_task, "logger", fake_logger)

        await download_task.process_completed()

        assert any(
            event == "post_processing_handoff_failed" and payload.get("download_id") == first_id
            for _, event, payload in fake_logger.events
        )
        async with db_factory() as session:
            failed = await session.get(DownloadHistory, first_id)
        assert failed is not None
        assert failed.state == DownloadState.FAILED
        assert failed.error_message == "probe failed"


class TestPostProcessingTransferTelemetry:
    """Transfer snapshots should expose enough data for determinate queue progress."""

    def test_transfer_progress_snapshot_tracks_speed_and_eta(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pullbox.tasks.download_task as download_task

        times = iter([100.0, 101.0, 103.0, 103.0])
        monkeypatch.setattr(download_task._time, "time", lambda: next(times))

        download_task._clear_post_processing(42)
        download_task._set_post_processing_phase(
            42, download_task.PostProcessingPhase.RESOLVING_SOURCE
        )
        download_task._set_post_processing_transfer_progress(
            42,
            total_bytes=200,
            done_bytes=50,
        )
        download_task._set_post_processing_transfer_progress(
            42,
            total_bytes=200,
            done_bytes=150,
        )

        snapshot = download_task.get_all_post_processing_progress()[42]
        assert snapshot.phase_label == "Transferring file"
        assert snapshot.phase is download_task.PostProcessingPhase.TRANSFERRING_FILE
        assert snapshot.started_at_epoch == 100.0
        assert snapshot.transfer_total_bytes == 200
        assert snapshot.transfer_done_bytes == 150
        assert snapshot.transfer_speed_bytes == 50
        assert snapshot.transfer_eta_seconds == 1

        download_task._clear_post_processing(42)

    def test_completed_snapshot_lingers_briefly_with_success_tone(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pullbox.tasks.download_task as download_task

        monkeypatch.setattr(download_task._time, "time", lambda: 200.0)

        download_task._clear_post_processing(77)
        download_task._set_post_processing_transfer_progress(
            77,
            total_bytes=400,
            done_bytes=400,
        )
        download_task._mark_post_processing_complete(77)

        snapshot = download_task.get_all_post_processing_progress()[77]
        assert snapshot.phase_label == "Import complete"
        assert snapshot.state_tone == "success"
        assert snapshot.transfer_total_bytes == 400
        assert snapshot.transfer_done_bytes == 400
        assert snapshot.transfer_eta_seconds == 0
        assert snapshot.visible_until_epoch == 204.0

        download_task._clear_post_processing(77)

    def test_completed_snapshot_is_purged_after_grace_window(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pullbox.tasks.download_task as download_task

        now = {"value": 300.0}
        monkeypatch.setattr(download_task._time, "time", lambda: now["value"])

        download_task._clear_post_processing(88)
        download_task._mark_post_processing_complete(88)

        assert 88 in download_task.get_all_post_processing_progress()

        now["value"] = 305.0
        assert 88 not in download_task.get_all_post_processing_progress()


class TestDownloadLifecycleHelpers:
    """Helper behavior should stay deterministic and easy to reason about."""

    def test_phase_helpers_and_trace_reentry_are_stable(self) -> None:
        import pullbox.tasks.download_task as download_task

        assert download_task.PostProcessingPhase.TRANSFERRING_FILE.status_label == "Transferring"
        assert download_task.PostProcessingPhase.TRANSFERRING_FILE.shows_transfer_metrics is True
        assert download_task.PostProcessingPhase.RESOLVING_SOURCE.status_label == "Resolving source"
        assert download_task.PostProcessingPhase.RESOLVING_SOURCE.shows_transfer_metrics is False

        trace = download_task.PostProcessingRunTrace(download_id=42)
        initial_started = trace.phase_started_monotonic
        trace.enter_phase(download_task.PostProcessingPhase.RESOLVING_SOURCE)
        assert trace.current_phase is download_task.PostProcessingPhase.RESOLVING_SOURCE
        assert trace.phase_started_monotonic >= initial_started
        assert trace.phase_timings_ms == {}

    def test_duration_and_error_classification_helpers_cover_edge_cases(self) -> None:
        import pullbox.tasks.download_task as download_task

        observed_at = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        duration_ms, duration_basis = download_task._compute_download_lifecycle_duration(
            SimpleNamespace(sent_at=None),
            observed_at=observed_at,
        )
        assert duration_basis == "first_observed"
        assert duration_ms is not None

        duration_ms, duration_basis = download_task._compute_download_lifecycle_duration(
            SimpleNamespace(sent_at=None),
            observed_at=None,
        )
        assert duration_ms is None
        assert duration_basis is None

        assert (
            download_task._classify_post_processing_error(
                FileNotFoundError("release failed quick integrity check: unreadable archive")
            )
            == "source_unreadable"
        )
        assert (
            download_task._classify_post_processing_error(
                FileNotFoundError("source did not become visible after retries")
            )
            == "source_visibility"
        )
        assert (
            download_task._classify_post_processing_error(FileNotFoundError("missing path"))
            == "path_not_found"
        )
        assert (
            download_task._classify_post_processing_error(RuntimeError("File safety: blocked"))
            == "file_safety"
        )
        assert (
            download_task._classify_post_processing_error(RuntimeError("unexpected runtime"))
            == "runtime_error"
        )
        assert download_task._classify_post_processing_error(Exception("boom")) == "unexpected"


class TestPostProcessingSourceProbe:
    """Shared-storage source probing should be resilient to brief visibility lag."""

    def test_find_comic_file_skips_permission_denied_tombstones(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pullbox.tasks.download_task as download_task

        source_dir = tmp_path / "sab-job"
        source_dir.mkdir()
        tombstone = source_dir / ".smbdeleteAAA123"
        tombstone.write_text("tombstone")
        actual_file = source_dir / "Absolute Superman 009.cbz"
        actual_file.write_bytes(b"PK" + b"\x00" * 10)

        original_is_file = Path.is_file

        def _flaky_is_file(self: Path) -> bool:
            if self.name.startswith(".smbdelete"):
                raise PermissionError("Operation not permitted")
            return original_is_file(self)

        monkeypatch.setattr(Path, "is_file", _flaky_is_file)

        found = download_task._find_comic_file(source_dir, {".cbz"})

        assert found == actual_file

    @pytest.mark.asyncio
    async def test_probe_uses_parent_directory_when_reported_file_is_not_visible(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pullbox.tasks.download_task as download_task

        download_dir = tmp_path / "sab-job"
        download_dir.mkdir()
        actual_file = download_dir / "Absolute Superman 009.cbz"
        actual_file.write_bytes(b"PK" + b"\x00" * 10)
        reported_file = download_dir / "Different Reported Name.cbz"

        monkeypatch.setattr(download_task, "_POST_PROCESSING_SOURCE_RETRY_DELAYS", (0.0,))

        probe = await download_task._probe_post_processing_source(
            reported_file,
            {".cbz", ".cbr"},
        )

        assert probe.comic_file == actual_file
        assert probe.probe_root == download_dir
        assert probe.source_seen is True
        assert probe.attempts == 1

    @pytest.mark.asyncio
    async def test_probe_retries_until_comic_file_becomes_visible(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pullbox.tasks.download_task as download_task

        source_dir = tmp_path / "sab-job"
        source_dir.mkdir()
        actual_file = source_dir / "Absolute Superman 016.cbr"
        actual_file.write_bytes(b"Rar!" + b"\x00" * 10)

        monkeypatch.setattr(download_task, "_POST_PROCESSING_SOURCE_RETRY_DELAYS", (0.0, 0.0, 0.0))

        calls = {"count": 0}

        def _late_find(
            download_path: Path,
            allowed_extensions: set[str] | None = None,
        ) -> Path | None:
            calls["count"] += 1
            if calls["count"] < 3:
                return None
            return actual_file

        monkeypatch.setattr(download_task, "_find_comic_file", _late_find)

        probe = await download_task._probe_post_processing_source(
            source_dir,
            {".cbz", ".cbr"},
        )

        assert probe.comic_file == actual_file
        assert probe.probe_root == source_dir
        assert probe.source_seen is True
        assert probe.attempts == 3


class TestPostProcessingIntegrityCheck:
    """Post-processing should distinguish corrupt releases from path visibility issues."""

    def test_integrity_error_classifies_missing_file_as_visibility_problem(self) -> None:
        import pullbox.tasks.download_task as download_task

        exc = download_task._build_post_processing_integrity_exception(
            Path("/downloads/comics/bad.cbz"),
            ["File not found: /downloads/comics/bad.cbz"],
        )

        assert isinstance(exc, FileNotFoundError)
        assert "became unreadable during the quick integrity check" in str(exc)

    def test_integrity_error_classifies_bad_archive_as_bad_release(self) -> None:
        import pullbox.tasks.download_task as download_task

        exc = download_task._build_post_processing_integrity_exception(
            Path("/downloads/comics/bad.cbz"),
            ["Invalid ZIP archive: File is not a zip file"],
        )

        assert isinstance(exc, RuntimeError)
        assert "Release failed quick integrity check" in str(exc)
        assert "Try another release" in str(exc)

    @pytest.mark.asyncio
    async def test_run_post_processing_surfaces_corrupt_release(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pullbox.tasks.download_task as download_task

        corrupt_release = tmp_path / "broken.cbz"
        _create_inspectable_bad_release_cbz(corrupt_release)

        async with db_factory() as session:
            series = Series(title="Absolute Superman", sort_title="absolute superman")
            session.add(series)
            await session.flush()

            issue = Issue(series_id=series.id, issue_number=9.0, status=IssueStatus.DOWNLOADING)
            session.add(issue)
            await session.flush()

            download = DownloadHistory(
                title="Absolute Superman 009.cbz",
                state=DownloadState.COMPLETED,
                download_client=DownloadClientType.SABNZBD,
                download_url="https://example.com/absolute-superman-009.nzb",
                downloaded_path="/downloads/comics/broken.cbz",
                issue_id=issue.id,
                completed_at=datetime(2026, 5, 1, 6, 0, tzinfo=UTC),
                updated_at=datetime(2026, 5, 1, 6, 0, tzinfo=UTC),
            )
            session.add(download)
            await session.flush()

            monkeypatch.setattr(
                download_task,
                "_resolve_local_path",
                AsyncMock(return_value=str(corrupt_release)),
            )

            with pytest.raises(RuntimeError, match="Release failed quick integrity check"):
                await download_task._run_post_processing(session, download)

    @pytest.mark.asyncio
    async def test_run_post_processing_logs_failure_summary_with_classification(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pullbox.tasks.download_task as download_task

        corrupt_release = tmp_path / "broken.cbz"
        _create_inspectable_bad_release_cbz(corrupt_release)
        fake_logger = _FakeLogger()
        monkeypatch.setattr(download_task, "logger", fake_logger)

        async with db_factory() as session:
            series = Series(title="Absolute Superman", sort_title="absolute superman")
            session.add(series)
            await session.flush()

            issue = Issue(series_id=series.id, issue_number=9.0, status=IssueStatus.DOWNLOADING)
            session.add(issue)
            await session.flush()

            download = DownloadHistory(
                title="Absolute Superman 009.cbz",
                state=DownloadState.COMPLETED,
                download_client=DownloadClientType.SABNZBD,
                download_url="https://example.com/absolute-superman-009.nzb",
                downloaded_path="/downloads/comics/broken.cbz",
                issue_id=issue.id,
                completed_at=datetime(2026, 5, 1, 6, 0, tzinfo=UTC),
                updated_at=datetime(2026, 5, 1, 6, 0, tzinfo=UTC),
            )
            session.add(download)
            await session.flush()

            monkeypatch.setattr(
                download_task,
                "_resolve_local_path",
                AsyncMock(return_value=str(corrupt_release)),
            )

            with pytest.raises(RuntimeError, match="Release failed quick integrity check"):
                await download_task._run_post_processing(session, download)

        summaries = [
            payload
            for _, event, payload in fake_logger.events
            if event == "post_processing_lifecycle_summary"
        ]
        assert len(summaries) == 1
        assert summaries[0]["outcome"] == "failed"
        assert summaries[0]["error_classification"] == "bad_release"
        assert summaries[0]["source_path"] == str(corrupt_release)
        assert summaries[0]["integrity_ms"] is not None

    @pytest.mark.asyncio
    async def test_run_post_processing_logs_success_summary_with_phase_timings(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pullbox.tasks.download_task as download_task

        source_dir = tmp_path / "downloads" / "sab-job"
        release_path = _create_valid_cbz(source_dir / "Absolute Superman 009.cbz")
        library_root_path = tmp_path / "comics"
        library_root_path.mkdir(parents=True, exist_ok=True)
        fake_logger = _FakeLogger()
        monkeypatch.setattr(download_task, "logger", fake_logger)

        async with db_factory() as session:
            root = LibraryRoot(name="Comics", path=str(library_root_path), enabled=True)
            session.add(root)
            session.add(
                SystemConfig(
                    key="comics_directory",
                    value=str(library_root_path),
                    value_type="string",
                )
            )
            session.add(
                SystemConfig(
                    key="post_processing_method",
                    value="move",
                    value_type="string",
                )
            )
            session.add(
                DownloadClientConfig(
                    name="SABnzbd",
                    client_type=DownloadClientType.SABNZBD,
                    url="http://sabnzbd:8080",
                    download_dir=str(source_dir.parent),
                )
            )
            await session.flush()

            series = Series(
                title="Absolute Superman",
                sort_title="absolute superman",
                year_start=2025,
                library_root_id=root.id,
            )
            session.add(series)
            await session.flush()

            issue = Issue(series_id=series.id, issue_number=9.0, status=IssueStatus.DOWNLOADING)
            session.add(issue)
            await session.flush()

            download = DownloadHistory(
                title="Absolute Superman 009.cbz",
                state=DownloadState.COMPLETED,
                download_client=DownloadClientType.SABNZBD,
                download_url="https://example.com/absolute-superman-009.nzb",
                downloaded_path="/data/download/comics/Absolute Superman 009.cbz",
                issue_id=issue.id,
                completed_at=datetime(2026, 5, 1, 6, 0, tzinfo=UTC),
                updated_at=datetime(2026, 5, 1, 6, 0, tzinfo=UTC),
            )
            session.add(download)
            await session.flush()

            monkeypatch.setattr(
                download_task,
                "_resolve_local_path",
                AsyncMock(return_value=str(release_path)),
            )

            await download_task._run_post_processing(session, download)

            assert download.final_path is not None
            assert Path(download.final_path).exists()
            assert not release_path.exists()
            assert not source_dir.exists()

        summaries = [
            payload
            for _, event, payload in fake_logger.events
            if event == "post_processing_lifecycle_summary"
        ]
        assert len(summaries) == 1
        summary = summaries[0]
        assert summary["outcome"] == "success"
        assert summary["transfer_method"] == "move"
        assert summary["source_path"] == str(release_path)
        assert summary["final_path"] is not None
        assert summary["source_probe_ms"] is not None
        assert summary["safety_ms"] is not None
        assert summary["integrity_ms"] is not None
        assert summary["destination_prep_ms"] is not None
        assert summary["transfer_ms"] is not None
        assert summary["post_processing_duration_ms"] is not None
        assert any(event == "post_processing_source_cleaned" for _, event, _ in fake_logger.events)

    @pytest.mark.asyncio
    async def test_standard_torrent_move_post_processing_moves_source_file(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pullbox.tasks.download_task as download_task

        source_dir = tmp_path / "downloads" / "qb-job"
        release_path = _create_valid_cbz(source_dir / "Absolute Flash 001.cbz")
        library_root_path = tmp_path / "comics"
        library_root_path.mkdir(parents=True, exist_ok=True)
        fake_logger = _FakeLogger()
        monkeypatch.setattr(download_task, "logger", fake_logger)

        async with db_factory() as session:
            root = LibraryRoot(name="Comics", path=str(library_root_path), enabled=True)
            session.add(root)
            session.add(
                SystemConfig(
                    key="comics_directory",
                    value=str(library_root_path),
                    value_type="string",
                )
            )
            session.add(
                SystemConfig(
                    key="post_processing_method",
                    value="move",
                    value_type="string",
                )
            )
            await session.flush()

            series = Series(
                title="Absolute Flash",
                sort_title="absolute flash",
                year_start=2025,
                library_root_id=root.id,
            )
            session.add(series)
            await session.flush()

            issue = Issue(series_id=series.id, issue_number=1.0, status=IssueStatus.DOWNLOADING)
            session.add(issue)
            await session.flush()

            download = DownloadHistory(
                title="Absolute Flash 001.cbz",
                state=DownloadState.COMPLETED,
                download_client=DownloadClientType.QBITTORRENT,
                download_url="https://example.com/absolute-flash-001.torrent",
                downloaded_path="/downloads/comics/Absolute Flash 001.cbz",
                issue_id=issue.id,
                completed_at=datetime(2026, 5, 1, 6, 0, tzinfo=UTC),
                updated_at=datetime(2026, 5, 1, 6, 0, tzinfo=UTC),
            )
            session.add(download)
            await session.flush()

            monkeypatch.setattr(
                download_task,
                "_resolve_local_path",
                AsyncMock(return_value=str(release_path)),
            )

            await download_task._run_post_processing(session, download)

            assert download.final_path is not None
            assert Path(download.final_path).exists()
            assert not release_path.exists()

        summaries = [
            payload
            for _, event, payload in fake_logger.events
            if event == "post_processing_lifecycle_summary"
        ]
        assert len(summaries) == 1
        assert summaries[0]["outcome"] == "success"
        assert summaries[0]["transfer_method"] == "move"
        assert summaries[0]["source_path"] == str(release_path)
        assert summaries[0]["final_path"] is not None

    @pytest.mark.asyncio
    async def test_seed_safe_torrent_post_processing_preserves_source_file(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pullbox.tasks.download_task as download_task

        source_dir = tmp_path / "downloads" / "qb-job"
        release_path = _create_valid_cbz(source_dir / "Absolute Flash 001.cbz")
        library_root_path = tmp_path / "comics"
        library_root_path.mkdir(parents=True, exist_ok=True)
        fake_logger = _FakeLogger()
        monkeypatch.setattr(download_task, "logger", fake_logger)

        async with db_factory() as session:
            root = LibraryRoot(name="Comics", path=str(library_root_path), enabled=True)
            session.add(root)
            session.add_all(
                [
                    SystemConfig(
                        key="comics_directory",
                        value=str(library_root_path),
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
                title="Absolute Flash",
                sort_title="absolute flash",
                year_start=2025,
                library_root_id=root.id,
            )
            session.add(series)
            await session.flush()

            issue = Issue(series_id=series.id, issue_number=1.0, status=IssueStatus.DOWNLOADING)
            session.add(issue)
            await session.flush()

            download = DownloadHistory(
                title="Absolute Flash 001.cbz",
                state=DownloadState.COMPLETED,
                download_client=DownloadClientType.QBITTORRENT,
                download_url="https://example.com/absolute-flash-001.torrent",
                downloaded_path="/downloads/comics/Absolute Flash 001.cbz",
                issue_id=issue.id,
                completed_at=datetime(2026, 5, 1, 6, 0, tzinfo=UTC),
                updated_at=datetime(2026, 5, 1, 6, 0, tzinfo=UTC),
            )
            session.add(download)
            await session.flush()

            monkeypatch.setattr(
                download_task,
                "_resolve_local_path",
                AsyncMock(return_value=str(release_path)),
            )

            await download_task._run_post_processing(session, download)

            assert download.final_path is not None
            final_path = Path(download.final_path)
            assert final_path.exists()
            assert release_path.exists()
            assert release_path.stat().st_ino == final_path.stat().st_ino

        summaries = [
            payload
            for _, event, payload in fake_logger.events
            if event == "post_processing_lifecycle_summary"
        ]
        assert len(summaries) == 1
        assert summaries[0]["outcome"] == "success"
        assert summaries[0]["source_path"] == str(release_path)
        assert summaries[0]["final_path"] is not None
        assert summaries[0]["torrent_import_strategy"] == "seed_safe"
        assert summaries[0]["seed_safe_torrent_import"] is True
        assert summaries[0]["configured_transfer_method"] == "move"
        assert summaries[0]["effective_transfer_method"] == "hardlink"
        assert summaries[0]["source_preserved"] is True

    @pytest.mark.asyncio
    async def test_run_post_processing_skipped_existing_emits_summary(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pullbox.core.library_policy as library_policy
        import pullbox.tasks.download_task as download_task

        release_path = _create_valid_cbz(tmp_path / "downloads" / "sab-job" / "issue-009.cbz")
        library_root_path = tmp_path / "comics"
        library_root_path.mkdir(parents=True, exist_ok=True)
        existing_path = library_root_path / "Absolute Superman (2025) #009.cbz"
        existing_path.write_bytes(b"existing")
        fake_logger = _FakeLogger()
        monkeypatch.setattr(download_task, "logger", fake_logger)
        monkeypatch.setattr(
            library_policy,
            "load_library_ingest_policy",
            AsyncMock(
                return_value=SimpleNamespace(
                    skip_existing_files=True,
                    post_processing_method="move",
                )
            ),
        )

        async with db_factory() as session:
            root = LibraryRoot(name="Comics", path=str(library_root_path), enabled=True)
            session.add(root)
            await session.flush()

            series = Series(
                title="Absolute Superman",
                sort_title="absolute superman",
                year_start=2025,
                library_root_id=root.id,
            )
            session.add(series)
            await session.flush()

            issue = Issue(series_id=series.id, issue_number=9.0, status=IssueStatus.DOWNLOADING)
            session.add(issue)
            await session.flush()

            library_file = LibraryFile(
                file_path=str(existing_path),
                file_name=existing_path.name,
                file_size=existing_path.stat().st_size,
                file_format=FileFormat.CBZ,
                file_hash=None,
                file_modified_at=datetime.now(UTC),
                match_confidence=MatchConfidence.HIGH,
                parsed_series="Absolute Superman",
                parsed_issue_number=9.0,
                parsed_year=2025,
                parsed_publisher=None,
                has_comicinfo=False,
                issue_id=issue.id,
                library_root_id=root.id,
            )
            session.add(library_file)
            await session.flush()

            download = DownloadHistory(
                title="Absolute Superman 009.cbz",
                state=DownloadState.COMPLETED,
                download_client=DownloadClientType.SABNZBD,
                download_url="https://example.com/absolute-superman-009.nzb",
                downloaded_path="/data/download/comics/Absolute Superman 009.cbz",
                issue_id=issue.id,
                completed_at=datetime(2026, 5, 1, 6, 0, tzinfo=UTC),
                updated_at=datetime(2026, 5, 1, 6, 0, tzinfo=UTC),
            )
            session.add(download)
            await session.flush()

            monkeypatch.setattr(
                download_task,
                "_resolve_local_path",
                AsyncMock(return_value=str(release_path)),
            )

            await download_task._run_post_processing(session, download)

            assert download.imported_at is not None
            assert issue.status == IssueStatus.OWNED

        summaries = [
            payload
            for _, event, payload in fake_logger.events
            if event == "post_processing_lifecycle_summary"
        ]
        assert len(summaries) == 1
        assert summaries[0]["outcome"] == "skipped_existing"
        assert summaries[0]["final_path"] == str(existing_path)
        assert summaries[0]["file_size_bytes"] == existing_path.stat().st_size
        assert summaries[0]["transferred_bytes"] == existing_path.stat().st_size

    @pytest.mark.asyncio
    async def test_run_post_processing_replacement_bypasses_skip_existing(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pullbox.core.file_ops as file_ops
        import pullbox.core.library_policy as library_policy
        import pullbox.tasks.download_task as download_task

        release_path = _create_valid_cbz(tmp_path / "downloads" / "sab-job" / "issue-009.cbz")
        library_root_path = tmp_path / "comics"
        library_root_path.mkdir(parents=True, exist_ok=True)
        existing_path = library_root_path / "Absolute Superman (2025) #009.cbz"
        existing_path.write_bytes(b"existing")
        replacement_path = library_root_path / "Absolute Superman (2025) #009 replacement.cbz"

        async def fake_register_library_file(_session: object, *args: object, **kwargs: object):
            assert kwargs["replace_existing_library_file"] is True
            replacement_path.write_bytes(b"replacement")
            return SimpleNamespace(
                file_path=str(replacement_path),
                file_size=replacement_path.stat().st_size,
            )

        monkeypatch.setattr(download_task, "logger", _FakeLogger())
        monkeypatch.setattr(file_ops, "register_library_file", fake_register_library_file)
        monkeypatch.setattr(
            library_policy,
            "load_library_ingest_policy",
            AsyncMock(
                return_value=SimpleNamespace(
                    skip_existing_files=True,
                    post_processing_method="move",
                )
            ),
        )

        async with db_factory() as session:
            root = LibraryRoot(name="Comics", path=str(library_root_path), enabled=True)
            session.add(root)
            await session.flush()

            series = Series(
                title="Absolute Superman",
                sort_title="absolute superman",
                year_start=2025,
                library_root_id=root.id,
            )
            session.add(series)
            await session.flush()

            issue = Issue(series_id=series.id, issue_number=9.0, status=IssueStatus.DOWNLOADING)
            session.add(issue)
            await session.flush()

            session.add(
                LibraryFile(
                    file_path=str(existing_path),
                    file_name=existing_path.name,
                    file_size=existing_path.stat().st_size,
                    file_format=FileFormat.CBZ,
                    file_hash=None,
                    file_modified_at=datetime.now(UTC),
                    match_confidence=MatchConfidence.HIGH,
                    parsed_series="Absolute Superman",
                    parsed_issue_number=9.0,
                    parsed_year=2025,
                    parsed_publisher=None,
                    has_comicinfo=False,
                    issue_id=issue.id,
                    library_root_id=root.id,
                )
            )
            await session.flush()

            download = DownloadHistory(
                title="Absolute Superman 009.cbz",
                state=DownloadState.COMPLETED,
                download_client=DownloadClientType.SABNZBD,
                download_url="https://example.com/absolute-superman-009.nzb",
                downloaded_path="/data/download/comics/Absolute Superman 009.cbz",
                issue_id=issue.id,
                replace_existing_file=True,
                completed_at=datetime(2026, 5, 1, 6, 0, tzinfo=UTC),
                updated_at=datetime(2026, 5, 1, 6, 0, tzinfo=UTC),
            )
            session.add(download)
            await session.flush()

            monkeypatch.setattr(
                download_task,
                "_resolve_local_path",
                AsyncMock(return_value=str(release_path)),
            )

            await download_task._run_post_processing(session, download)

            assert download.imported_at is None
            assert download.final_path == str(replacement_path)

    @pytest.mark.asyncio
    async def test_recover_orphaned_downloads_restores_owned_for_imported_issue_with_library_file(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        import pullbox.tasks.download_task as download_task

        library_root_path = tmp_path / "comics"
        library_root_path.mkdir(parents=True, exist_ok=True)
        existing_path = library_root_path / "Absolute Superman (2025) #012.cbr"
        existing_path.write_bytes(b"existing")

        async with db_factory() as session:
            root = LibraryRoot(name="Comics", path=str(library_root_path), enabled=True)
            session.add(root)
            await session.flush()

            series = Series(
                title="Absolute Superman",
                sort_title="absolute superman",
                year_start=2025,
                library_root_id=root.id,
            )
            session.add(series)
            await session.flush()

            issue = Issue(series_id=series.id, issue_number=12.0, status=IssueStatus.DOWNLOADING)
            session.add(issue)
            await session.flush()

            session.add(
                LibraryFile(
                    file_path=str(existing_path),
                    file_name=existing_path.name,
                    file_size=existing_path.stat().st_size,
                    file_format=FileFormat.CBR,
                    file_hash=None,
                    file_modified_at=datetime.now(UTC),
                    match_confidence=MatchConfidence.HIGH,
                    parsed_series="Absolute Superman",
                    parsed_issue_number=12.0,
                    parsed_year=2025,
                    parsed_publisher=None,
                    has_comicinfo=False,
                    issue_id=issue.id,
                    library_root_id=root.id,
                )
            )
            session.add(
                DownloadHistory(
                    title="Absolute Superman 012 [2025].cbr",
                    state=DownloadState.COMPLETED,
                    download_client=DownloadClientType.SABNZBD,
                    download_url="https://example.com/absolute-superman-012.nzb",
                    downloaded_path="/downloads/comics/Absolute Superman 012.cbr",
                    issue_id=issue.id,
                    completed_at=datetime(2026, 5, 1, 6, 0, tzinfo=UTC),
                    imported_at=datetime(2026, 5, 1, 6, 5, tzinfo=UTC),
                    updated_at=datetime(2026, 5, 1, 6, 5, tzinfo=UTC),
                )
            )
            await session.commit()

        async with db_factory() as session:
            recovered = await download_task._recover_orphaned_downloads(session)
            await session.commit()

            repaired_issue = await session.get(Issue, issue.id)

        assert recovered == 1
        assert repaired_issue is not None
        assert repaired_issue.status == IssueStatus.OWNED

    @pytest.mark.asyncio
    async def test_run_post_processing_registers_existing_destination_with_alt_extension(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pullbox.core.file_ops as file_ops
        import pullbox.core.library_policy as library_policy
        import pullbox.tasks.download_task as download_task

        library_root_path = tmp_path / "comics"
        library_root_path.mkdir(parents=True, exist_ok=True)
        alt_dest_path = library_root_path / "Absolute Superman (2025) #009.cbr"
        alt_dest_path.write_bytes(b"already-there")
        fake_logger = _FakeLogger()
        register_library_file = AsyncMock(return_value=None)

        monkeypatch.setattr(download_task, "logger", fake_logger)
        monkeypatch.setattr(
            download_task,
            "_resolve_local_path",
            AsyncMock(return_value="/downloads/missing/Absolute Superman 009.cbz"),
        )
        monkeypatch.setattr(
            download_task,
            "_probe_post_processing_source",
            AsyncMock(
                return_value=SimpleNamespace(
                    source_seen=False,
                    probe_root=Path("/downloads/missing"),
                    comic_file=None,
                    attempts=2,
                )
            ),
        )
        monkeypatch.setattr(
            library_policy,
            "load_library_ingest_policy",
            AsyncMock(
                return_value=SimpleNamespace(
                    skip_existing_files=False,
                    post_processing_method="move",
                )
            ),
        )
        monkeypatch.setattr(
            file_ops,
            "resolve_library_destination",
            AsyncMock(
                return_value=(
                    library_root_path / "Absolute Superman (2025) #009.cbz",
                    library_root_path,
                )
            ),
        )
        monkeypatch.setattr(file_ops, "register_library_file", register_library_file)

        async with db_factory() as session:
            series = Series(
                title="Absolute Superman",
                sort_title="absolute superman",
                year_start=2025,
            )
            session.add(series)
            await session.flush()

            issue = Issue(series_id=series.id, issue_number=9.0, status=IssueStatus.DOWNLOADING)
            session.add(issue)
            await session.flush()

            download = DownloadHistory(
                title="Absolute Superman 009.cbz",
                state=DownloadState.COMPLETED,
                download_client=DownloadClientType.SABNZBD,
                download_url="https://example.com/absolute-superman-009.nzb",
                downloaded_path="/downloads/missing/Absolute Superman 009.cbz",
                issue_id=issue.id,
                completed_at=datetime(2026, 5, 1, 6, 0, tzinfo=UTC),
                updated_at=datetime(2026, 5, 1, 6, 0, tzinfo=UTC),
            )
            session.add(download)
            await session.flush()

            await download_task._run_post_processing(session, download)

            assert download.final_path == str(alt_dest_path)

        register_library_file.assert_awaited_once()
        summaries = [
            payload
            for _, event, payload in fake_logger.events
            if event == "post_processing_lifecycle_summary"
        ]
        assert len(summaries) == 1
        assert summaries[0]["outcome"] == "success"
        assert summaries[0]["final_path"] == str(alt_dest_path)
        assert summaries[0]["file_size_bytes"] == alt_dest_path.stat().st_size
        assert summaries[0]["transferred_bytes"] == alt_dest_path.stat().st_size

    @pytest.mark.asyncio
    async def test_run_post_processing_missing_source_and_destination_logs_failure_summary(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pullbox.core.file_ops as file_ops
        import pullbox.core.library_policy as library_policy
        import pullbox.tasks.download_task as download_task

        library_root_path = tmp_path / "comics"
        library_root_path.mkdir(parents=True, exist_ok=True)
        fake_logger = _FakeLogger()

        monkeypatch.setattr(download_task, "logger", fake_logger)
        monkeypatch.setattr(
            download_task,
            "_resolve_local_path",
            AsyncMock(return_value="/downloads/missing/Absolute Superman 009.cbz"),
        )
        monkeypatch.setattr(
            download_task,
            "_probe_post_processing_source",
            AsyncMock(
                return_value=SimpleNamespace(
                    source_seen=False,
                    probe_root=Path("/downloads/missing"),
                    comic_file=None,
                    attempts=3,
                )
            ),
        )
        monkeypatch.setattr(
            library_policy,
            "load_library_ingest_policy",
            AsyncMock(
                return_value=SimpleNamespace(
                    skip_existing_files=False,
                    post_processing_method="move",
                )
            ),
        )
        monkeypatch.setattr(
            file_ops,
            "resolve_library_destination",
            AsyncMock(
                return_value=(
                    library_root_path / "Absolute Superman (2025) #009.cbz",
                    library_root_path,
                )
            ),
        )

        async with db_factory() as session:
            series = Series(
                title="Absolute Superman",
                sort_title="absolute superman",
                year_start=2025,
            )
            session.add(series)
            await session.flush()

            issue = Issue(series_id=series.id, issue_number=9.0, status=IssueStatus.DOWNLOADING)
            session.add(issue)
            await session.flush()

            download = DownloadHistory(
                title="Absolute Superman 009.cbz",
                state=DownloadState.COMPLETED,
                download_client=DownloadClientType.SABNZBD,
                download_url="https://example.com/absolute-superman-009.nzb",
                downloaded_path="/downloads/missing/Absolute Superman 009.cbz",
                issue_id=issue.id,
                completed_at=datetime(2026, 5, 1, 6, 0, tzinfo=UTC),
                updated_at=datetime(2026, 5, 1, 6, 0, tzinfo=UTC),
            )
            session.add(download)
            await session.flush()

            with pytest.raises(FileNotFoundError, match="did not become visible"):
                await download_task._run_post_processing(session, download)

        summaries = [
            payload
            for _, event, payload in fake_logger.events
            if event == "post_processing_lifecycle_summary"
        ]
        assert len(summaries) == 1
        assert summaries[0]["outcome"] == "failed"
        assert summaries[0]["error_classification"] == "source_visibility"
        assert summaries[0]["probe_root"] == "/downloads/missing"
