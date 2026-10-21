"""Regression cases for orphaned, cancelled, and failed-retry activity."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from pullbox.api.v1.downloads import cancel_download
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.operation_progress import (
    OperationProgress,
)
from pullbox.models.operation_progress import (
    OperationProgressState as State,
)
from pullbox.models.operation_progress import (
    OperationProgressType as Kind,
)
from pullbox.models.series import Series
from pullbox.services.download_operation_reconciliation import (
    guard_download_progress,
    reconcile_download_operations,
)
from pullbox.services.download_service import DownloadService
from pullbox.services.operation_progress import OperationProgressUpdate, publish_operation_progress
from pullbox.services.series_service import SeriesService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


async def _download(session: AsyncSession, state: DownloadState) -> DownloadHistory:
    series = Series(title="Excalibur", sort_title="excalibur", year_start=2019)
    session.add(series)
    await session.flush()
    issue = Issue(series_id=series.id, issue_number=12, status=IssueStatus.DOWNLOADING)
    session.add(issue)
    await session.flush()
    download = DownloadHistory(
        issue_id=issue.id,
        title="Excalibur 12",
        state=state,
        download_client=DownloadClientType.AIRDCPP,
        download_url="adc://test.invalid",
    )
    session.add(download)
    await session.flush()
    return download


async def _operation(
    session: AsyncSession,
    download_id: int,
    *,
    kind: Kind = Kind.DOWNLOAD,
    state: State = State.QUEUED,
) -> OperationProgress:
    result = await publish_operation_progress(
        session,
        OperationProgressUpdate(
            operation_type=kind,
            operation_key=str(download_id),
            revision=None,
            state=state,
            phase="mutation_pending",
            title="Excalibur 12",
            attention_required=state is State.FAILED,
        ),
    )
    return result.operation


@pytest.mark.parametrize("kind", [Kind.DOWNLOAD, Kind.POST_PROCESSING])
async def test_series_deletion_retires_durable_activity(
    db_session: AsyncSession, kind: Kind
) -> None:
    download = await _download(db_session, DownloadState.QUEUED)
    issue = await db_session.get(Issue, download.issue_id)
    assert issue is not None
    operation = await _operation(db_session, download.id, kind=kind)
    await SeriesService.delete(db_session, issue.series_id)
    assert operation.state is State.CANCELLED
    assert operation.phase == "cancelled"
    assert not operation.attention_required


async def test_cancel_settles_activity_immediately(db_session: AsyncSession) -> None:
    download = await _download(db_session, DownloadState.QUEUED)
    operation = await _operation(db_session, download.id)
    with patch("pullbox.api.v1.downloads._cancel_on_client", new_callable=AsyncMock):
        await cancel_download(download.id, object(), db_session)  # type: ignore[arg-type]
    assert operation.state is State.CANCELLED
    assert operation.phase == "cancelled"
    assert not operation.attention_required


async def test_failed_client_cancel_keeps_active_job(db_session: AsyncSession) -> None:
    download = await _download(db_session, DownloadState.QUEUED)
    operation = await _operation(db_session, download.id)
    with (
        patch("pullbox.api.v1.downloads._cancel_on_client", side_effect=RuntimeError("offline")),
        pytest.raises(RuntimeError, match="offline"),
    ):
        await cancel_download(download.id, object(), db_session)  # type: ignore[arg-type]
    assert operation.state is State.QUEUED
    assert download.state is DownloadState.QUEUED


@pytest.mark.parametrize("method", ["retry_download", "manual_retry"])
async def test_failed_retry_publishes_terminal_state(db_session: AsyncSession, method: str) -> None:
    download = await _download(db_session, DownloadState.RETRY_PENDING)
    operation = await _operation(db_session, download.id, state=State.RETRYING)
    service = DownloadService(MagicMock(), MagicMock())
    with patch.object(service, "get_client_for_download", return_value=None):
        await getattr(service, method)(db_session, download.id)
    assert operation.state is State.FAILED
    assert operation.phase == "failed"
    assert operation.message == "No download client available"
    assert operation.attention_required


@pytest.mark.parametrize("kind", [Kind.DOWNLOAD, Kind.POST_PROCESSING])
@pytest.mark.parametrize("owner_state", [None, DownloadState.FAILED, DownloadState.IMPORTED])
async def test_repair_legacy_activity(
    db_session: AsyncSession,
    kind: Kind,
    owner_state: DownloadState | None,
) -> None:
    download_id = 999
    if owner_state is not None:
        download = await _download(db_session, owner_state)
        download_id = download.id
        if owner_state is DownloadState.IMPORTED:
            download.imported_at = datetime.now(UTC)
    operation = await _operation(db_session, download_id, kind=kind)
    assert await reconcile_download_operations(db_session) == 1
    expected = {
        None: State.CANCELLED,
        DownloadState.FAILED: State.FAILED,
        DownloadState.IMPORTED: State.COMPLETED,
    }[owner_state]
    assert operation.state is expected
    assert operation.attention_required is (expected is State.FAILED)
    assert await reconcile_download_operations(db_session) == 0


@pytest.mark.parametrize(
    "owner_state",
    [
        DownloadState.QUEUED,
        DownloadState.PAUSED,
        DownloadState.RETRY_PENDING,
        DownloadState.COMPLETED,
    ],
)
async def test_repair_preserves_live_work_and_pending_postprocessing(
    db_session: AsyncSession,
    owner_state: DownloadState,
) -> None:
    download = await _download(db_session, owner_state)
    operation = await _operation(db_session, download.id, kind=Kind.POST_PROCESSING)
    assert await reconcile_download_operations(db_session) == 0
    assert operation.state is State.QUEUED
    assert download.state is owner_state


async def test_repair_is_bounded_and_leaves_other_operation_types(db_session: AsyncSession) -> None:
    for key in range(1000, 1005):
        await _operation(db_session, key)
    untouched = await _operation(db_session, 1006, kind=Kind.UTILITY)
    assert await reconcile_download_operations(db_session, limit=2) == 2
    assert await reconcile_download_operations(db_session, limit=2) == 2
    assert await reconcile_download_operations(db_session, limit=2) == 1
    assert untouched.state is State.QUEUED


@pytest.mark.parametrize("owner_exists", [True, False])
async def test_detached_event_cannot_resurrect_cancelled_or_deleted_job(
    db_session: AsyncSession,
    owner_exists: bool,
) -> None:
    download_id = 999
    if owner_exists:
        download = await _download(db_session, DownloadState.FAILED)
        download.error_message = "Cancelled by user"
        download_id = download.id
    stale = OperationProgressUpdate(
        operation_type=Kind.POST_PROCESSING,
        operation_key=str(download_id),
        revision=None,
        state=State.RUNNING,
        phase="moving",
        title="Excalibur 12",
    )
    guarded = await guard_download_progress(db_session, stale)
    if owner_exists:
        assert guarded is not None and guarded.state is State.CANCELLED
        assert not guarded.attention_required
    else:
        assert guarded is None


async def test_detached_event_allows_real_retry(db_session: AsyncSession) -> None:
    download = await _download(db_session, DownloadState.DOWNLOADING)
    stale_terminal = await _operation(db_session, download.id, state=State.FAILED)
    update = OperationProgressUpdate(
        operation_type=Kind.DOWNLOAD,
        operation_key=str(download.id),
        revision=None,
        state=State.RUNNING,
        phase="downloading",
        title=download.title,
    )
    guarded = await guard_download_progress(db_session, update)
    assert guarded is not None
    await publish_operation_progress(db_session, guarded)
    assert stale_terminal.state is State.RUNNING
    assert len(list((await db_session.scalars(select(OperationProgress))).all())) == 1


async def test_detached_dispatch_does_not_recreate_deleted_activity(
    db_session: AsyncSession,
    async_engine: AsyncEngine,
) -> None:
    from pullbox.services import operation_progress_dispatch as dispatch

    update = OperationProgressUpdate(
        operation_type=Kind.POST_PROCESSING,
        operation_key="999",
        revision=None,
        state=State.RUNNING,
        phase="moving",
        title="2000 AD",
    )
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    with (
        patch.object(dispatch, "get_session_factory", return_value=factory),
        patch.object(dispatch, "_COALESCE_SECONDS", 0),
        patch.object(dispatch, "notify_activity_changed", new_callable=AsyncMock),
    ):
        await dispatch.queue_operation_progress(update)
        await dispatch.drain_operation_progress_updates()
    assert await db_session.scalar(select(OperationProgress.id)) is None


async def test_monitor_repairs_orphans_without_active_clients(
    db_session: AsyncSession,
    async_engine: AsyncEngine,
) -> None:
    from pullbox.tasks import download_task

    operation = await _operation(db_session, 999)
    await db_session.commit()
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    with (
        patch.object(download_task, "get_session_factory", return_value=factory),
        patch.object(download_task, "_last_recovery_check", 0),
        patch.object(download_task, "_process_direct_retry_pending", return_value=0),
        patch.object(download_task, "_load_monitor_poll_items", return_value=None),
    ):
        await download_task.monitor_downloads()
    await db_session.refresh(operation)
    assert operation.state is State.CANCELLED


async def test_terminal_history_and_genuine_failures_remain_visible(
    db_session: AsyncSession,
) -> None:
    download = await _download(db_session, DownloadState.FAILED)
    download.error_message = "Archive requires review"
    failed = await _operation(db_session, download.id, state=State.FAILED)
    completed = await _operation(db_session, 999, state=State.COMPLETED)
    assert await reconcile_download_operations(db_session) == 0
    assert failed.attention_required
    assert completed.state is State.COMPLETED


async def test_legacy_cancellation_is_not_left_as_actionable_failure(
    db_session: AsyncSession,
) -> None:
    download = await _download(db_session, DownloadState.FAILED)
    download.error_message = "Cancelled by user"
    operation = await _operation(db_session, download.id, state=State.FAILED)
    assert await reconcile_download_operations(db_session) == 1
    assert operation.state is State.CANCELLED
    assert not operation.attention_required
