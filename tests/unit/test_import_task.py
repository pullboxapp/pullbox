"""Unit tests for import background tasks and startup recovery."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker

from pullbox.core.exceptions import JobCancelledError, JobPausedError
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportJob,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.models.operation_progress import (
    OperationProgress,
    OperationProgressState,
    OperationProgressType,
)
from pullbox.schemas.import_job import ImportProgressEvent
from pullbox.services.import_service import RunImportResult
from pullbox.services.operation_progress import (
    OperationProgressMeasure,
    OperationProgressUpdate,
    publish_operation_progress,
)
from pullbox.tasks.import_task import (
    ImportRunner,
    _build_import_service,
    _emit_terminal_event_for_job,
    _publish_progress_event,
    _review_rematch_locks,
    get_highest_visible_progress_revision,
    get_latest_progress_event,
    get_progress_queue,
    publish_story_arc_import_updates,
    recover_stuck_import_jobs,
    remove_progress_queue,
    run_import_execute_task,
    run_import_scan_task,
    set_latest_progress_event,
    trigger_import_execute,
    trigger_import_scan,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# ── Helpers ──────────────────────────────────────────────────────────────


async def _create_job(
    session: AsyncSession,
    *,
    status: ImportJobStatus = ImportJobStatus.PENDING,
    source_path: str = "/tmp/comics",
) -> ImportJob:
    """Insert an ImportJob for test setup."""
    job = ImportJob(
        source_path=source_path,
        source_type=ImportSourceType.FILESYSTEM,
        status=status,
    )
    session.add(job)
    await session.flush()
    return job


def _sqlite_database_locked_error() -> SQLAlchemyOperationalError:
    """Build the SQLAlchemy wrapper shape raised by SQLite busy writes."""
    return SQLAlchemyOperationalError(
        "UPDATE import_jobs SET progress_snapshot=? WHERE import_jobs.id = ?",
        {},
        sqlite3.OperationalError("database is locked"),
    )


@pytest.mark.asyncio
async def test_story_arc_import_followup_schedules_enrichment_and_clears_retry_marker(
    async_engine: object,
    db_session: AsyncSession,
) -> None:
    job = await _create_job(db_session, status=ImportJobStatus.COMPLETED)
    job.import_started_at = datetime.now(UTC)
    job.progress_snapshot = {
        "status": ImportJobStatus.COMPLETED.value,
        "mode": "import",
        "phase": "done",
        "progress": 100,
        "story_arc_placements_total": 1,
        "story_arc_placements_completed": 1,
        "story_arc_placement_followup_pending": True,
    }
    job.story_arc_placement_followup_pending = True
    await db_session.commit()
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    service = AsyncMock()
    service.schedule_comicinfo_enrichment = Mock()

    with (
        patch("pullbox.tasks.import_task._import_runner", None),
        patch(
            "pullbox.tasks.import_task._build_import_service",
            new=AsyncMock(return_value=service),
        ),
        patch("pullbox.tasks.import_task.publish", new_callable=AsyncMock),
    ):
        await publish_story_arc_import_updates(
            (job.id,),
            completed_job_ids=(job.id,),
            session_factory=factory,
        )

    service.schedule_comicinfo_enrichment.assert_called_once_with(
        factory,
        job_id=job.id,
    )
    await db_session.refresh(job)
    assert job.progress_snapshot["phase"] == "done"
    assert job.progress_snapshot["story_arc_placement_followup_pending"] is False
    assert job.story_arc_placement_followup_pending is False


@pytest.mark.asyncio
async def test_review_series_rematches_are_serialized_per_import_job() -> None:
    """Approved safety files must not rematch concurrently against one review job."""
    from pullbox.tasks.import_task import run_import_series_rematch_task

    started_first = asyncio.Event()
    release_first = asyncio.Event()
    calls: list[int] = []

    async def fake_rematch(job_id: int, imported_series_id: int) -> None:
        calls.append(imported_series_id)
        if imported_series_id == 10:
            started_first.set()
            await release_first.wait()

    _review_rematch_locks.pop(71, None)
    with patch(
        "pullbox.tasks.import_task._run_import_series_rematch_task",
        side_effect=fake_rematch,
    ):
        first = asyncio.create_task(run_import_series_rematch_task(71, 10))
        await started_first.wait()
        second = asyncio.create_task(run_import_series_rematch_task(71, 11))
        await asyncio.sleep(0)
        assert calls == [10]
        release_first.set()
        await asyncio.gather(first, second)

    assert calls == [10, 11]
    _review_rematch_locks.pop(71, None)


@pytest.mark.asyncio
async def test_bulk_safety_rematch_worker_queries_only_pending_series_for_one_job(
    async_engine: object,
    db_session: AsyncSession,
) -> None:
    """One bounded worker must not retain or dispatch another job's safety rows."""
    from pullbox.models.import_job import (
        ImportedFile,
        ImportedFileStatus,
        ImportedSeries,
        ImportSeriesStatus,
    )
    from pullbox.tasks.import_task import run_import_safety_bulk_rematch_task

    job = await _create_job(db_session, status=ImportJobStatus.REVIEW)
    other_job = await _create_job(db_session, status=ImportJobStatus.REVIEW)
    expected_ids: list[int] = []
    for index in range(30):
        series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name=f"Pending {index}",
            status=ImportSeriesStatus.MATCHED,
            diagnostics={"rematch_pending": True},
        )
        db_session.add(series)
        await db_session.flush()
        expected_ids.append(series.id)
        db_session.add(
            ImportedFile(
                import_job_id=job.id,
                import_series_id=series.id,
                file_path=f"/source/{index}.cbz",
                file_name=f"{index}.cbz",
                file_size=1,
                file_format="cbz",
                status=ImportedFileStatus.SAFETY_APPROVED,
            )
        )

    unrelated_series = ImportedSeries(
        import_job_id=other_job.id,
        raw_series_name="Other source",
        status=ImportSeriesStatus.MATCHED,
        diagnostics={"rematch_pending": True},
    )
    db_session.add(unrelated_series)
    await db_session.flush()
    db_session.add(
        ImportedFile(
            import_job_id=other_job.id,
            import_series_id=unrelated_series.id,
            file_path="/other/private.cbz",
            file_name="private.cbz",
            file_size=1,
            file_format="cbz",
            status=ImportedFileStatus.SAFETY_APPROVED,
        )
    )
    await db_session.commit()

    calls: list[tuple[int, int]] = []

    async def fake_rematch(job_id: int, imported_series_id: int) -> None:
        calls.append((job_id, imported_series_id))

    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    _review_rematch_locks.pop(job.id, None)
    with (
        patch("pullbox.tasks.import_task.get_session_factory", return_value=factory),
        patch(
            "pullbox.tasks.import_task._run_import_series_rematch_task",
            side_effect=fake_rematch,
        ),
    ):
        await run_import_safety_bulk_rematch_task(job.id)

    assert calls == [(job.id, series_id) for series_id in expected_ids]
    _review_rematch_locks.pop(job.id, None)


# ── Test: Progress Queues ────────────────────────────────────────────────


class TestProgressQueues:
    """Test SSE progress queue management."""

    def test_get_creates_queue(self) -> None:
        """get_progress_queue creates a new queue if none exists."""
        queue = get_progress_queue(99999)
        assert queue is not None
        assert queue.empty()
        remove_progress_queue(99999)

    def test_get_returns_same_queue(self) -> None:
        """get_progress_queue returns same queue for same job_id."""
        q1 = get_progress_queue(88888)
        q2 = get_progress_queue(88888)
        assert q1 is q2
        remove_progress_queue(88888)

    def test_remove_cleans_up(self) -> None:
        """remove_progress_queue removes the queue."""
        get_progress_queue(77777)
        set_latest_progress_event(
            ImportProgressEvent(
                job_id=77777,
                status=ImportJobStatus.SCANNING,
                phase="scanning",
                progress=10,
                message="progress",
            )
        )
        remove_progress_queue(77777)
        q = get_progress_queue(77777)
        assert q.empty()
        assert get_latest_progress_event(77777) is None
        remove_progress_queue(77777)

    def test_latest_event_cache_round_trips(self) -> None:
        """Latest progress event cache returns the most recent event."""
        event = ImportProgressEvent(
            job_id=66666,
            status=ImportJobStatus.MATCHING,
            phase="matching",
            progress=55,
            message="Matching 5/9...",
        )

        set_latest_progress_event(event)
        cached = get_latest_progress_event(66666)

        assert cached is not None
        assert cached.progress == 55
        remove_progress_queue(66666)

    @pytest.mark.asyncio
    async def test_ephemeral_progress_does_not_replace_latest_durable_event(self) -> None:
        job_id = 55555
        queue = get_progress_queue(job_id)
        durable_event = ImportProgressEvent(
            job_id=job_id,
            status=ImportJobStatus.IMPORTING,
            phase="importing",
            progress=40,
            progress_revision=4,
            message="Processed 2/5 review groups",
        )
        ephemeral_event = ImportProgressEvent(
            job_id=job_id,
            status=ImportJobStatus.IMPORTING,
            ephemeral_progress=True,
            phase="importing",
            progress=43,
            progress_revision=99,
            current_file_name="Persephone.2022.Hybrid.Comic.eBook-BitBook.pdf",
            current_file_stage="finalizing",
            current_file_progress_current=3,
            current_file_progress_total=4,
            current_file_progress_pct=99,
            current_file_progress_unit="steps",
            message="Processing file 1/1 in review group 3/5",
        )

        with patch("pullbox.tasks.import_task.publish", new_callable=AsyncMock):
            await _publish_progress_event(durable_event)
            await _publish_progress_event(ephemeral_event)

        cached = get_latest_progress_event(job_id)
        assert cached is not None
        assert cached.progress_revision == 4
        assert cached.message == "Processed 2/5 review groups"
        assert get_highest_visible_progress_revision(job_id) == 99
        queued = await asyncio.wait_for(queue.get(), timeout=0.1)
        assert queued.progress_revision == 4
        assert queue.empty()
        remove_progress_queue(job_id)

    @pytest.mark.asyncio
    async def test_durable_progress_revision_beats_prior_ephemeral_progress(self) -> None:
        job_id = 44444
        durable_event = ImportProgressEvent(
            job_id=job_id,
            status=ImportJobStatus.FILE_MATCHING,
            phase="file_matching",
            progress=74,
            progress_revision=12,
            message="Matching files to issues...",
        )
        ephemeral_event = ImportProgressEvent(
            job_id=job_id,
            status=ImportJobStatus.FILE_MATCHING,
            ephemeral_progress=True,
            phase="file_matching",
            progress=74,
            progress_revision=27,
            message="Still loading issue targets...",
        )
        review_event = ImportProgressEvent(
            job_id=job_id,
            status=ImportJobStatus.REVIEW,
            phase="review",
            progress=100,
            progress_revision=13,
            message="Ready for review",
        )

        with patch("pullbox.tasks.import_task.publish", new_callable=AsyncMock) as publish:
            await _publish_progress_event(durable_event)
            await _publish_progress_event(ephemeral_event)
            await _publish_progress_event(review_event)

        cached = get_latest_progress_event(job_id)
        assert cached is not None
        assert cached.status == ImportJobStatus.REVIEW
        assert cached.progress == 100
        assert cached.progress_revision == 28
        assert get_highest_visible_progress_revision(job_id) == 28
        published_payload = publish.call_args_list[-1].args[2]
        assert published_payload["progress_revision"] == 28
        remove_progress_queue(job_id)


# ── Test: run_import_scan_task ───────────────────────────────────────────


class TestRunImportScanTask:
    """Test the background scan task function."""

    @pytest.mark.asyncio
    async def test_scan_task_calls_service(self, db_session: AsyncSession) -> None:
        """run_import_scan_task creates session and calls start_scan."""
        job = await _create_job(db_session)
        await db_session.commit()

        mock_service = AsyncMock()

        @asynccontextmanager
        async def mock_session_ctx():
            yield db_session

        with (
            patch("pullbox.tasks.import_task.get_session_factory") as mock_factory,
            patch(
                "pullbox.tasks.import_task._build_import_service",
                return_value=mock_service,
            ),
        ):
            mock_factory.return_value = mock_session_ctx
            await run_import_scan_task(job.id)

        mock_service.start_scan.assert_called_once()

    @pytest.mark.asyncio
    async def test_scan_task_marks_failed_on_exception(self, db_session: AsyncSession) -> None:
        """If start_scan raises, job is marked FAILED."""
        job = await _create_job(db_session)
        await db_session.commit()

        mock_service = AsyncMock()
        mock_service.start_scan.side_effect = RuntimeError("Scan exploded")

        @asynccontextmanager
        async def mock_session_ctx():
            yield db_session

        with (
            patch("pullbox.tasks.import_task.get_session_factory") as mock_factory,
            patch(
                "pullbox.tasks.import_task._build_import_service",
                return_value=mock_service,
            ),
        ):
            mock_factory.return_value = mock_session_ctx
            await run_import_scan_task(job.id)

        await db_session.refresh(job)
        assert job.status == ImportJobStatus.FAILED
        assert job.error_message is not None

    @pytest.mark.asyncio
    async def test_scan_task_emits_review_terminal_event_and_cleans_queue(
        self, db_session: AsyncSession
    ) -> None:
        """Successful scan emits REVIEW so step 2 can complete cleanly."""
        job = await _create_job(db_session)
        await db_session.commit()
        queue = get_progress_queue(job.id)

        mock_service = AsyncMock()

        async def complete_scan(session, job_id, progress_callback=None):
            import_job = await session.get(ImportJob, job_id)
            assert import_job is not None
            import_job.status = ImportJobStatus.REVIEW
            import_job.error_message = None
            await session.flush()

        mock_service.start_scan.side_effect = complete_scan

        @asynccontextmanager
        async def mock_session_ctx():
            yield db_session

        with (
            patch("pullbox.tasks.import_task.get_session_factory") as mock_factory,
            patch(
                "pullbox.tasks.import_task._build_import_service",
                return_value=mock_service,
            ),
        ):
            mock_factory.return_value = mock_session_ctx
            await run_import_scan_task(job.id)

        event = await asyncio.wait_for(queue.get(), timeout=0.1)
        assert event.status == ImportJobStatus.REVIEW
        assert event.phase == "review"
        assert event.progress == 100
        assert event.message == "Ready for review"

        replacement_queue = get_progress_queue(job.id)
        assert replacement_queue is not queue
        assert replacement_queue.empty()
        remove_progress_queue(job.id)

    @pytest.mark.asyncio
    async def test_scan_task_purges_cancelled_job_and_cleans_queue(
        self, db_session: AsyncSession
    ) -> None:
        """Cancelled scans emit CANCELLED and purge the transient job row."""
        job = await _create_job(db_session, status=ImportJobStatus.CANCELLED)
        job.error_message = "Import cancelled by user."
        await db_session.commit()
        queue = get_progress_queue(job.id)

        mock_service = AsyncMock()
        mock_service.start_scan.side_effect = JobCancelledError("cancelled")

        @asynccontextmanager
        async def mock_session_ctx():
            yield db_session

        with (
            patch("pullbox.tasks.import_task.get_session_factory") as mock_factory,
            patch(
                "pullbox.tasks.import_task._build_import_service",
                return_value=mock_service,
            ),
        ):
            mock_factory.return_value = mock_session_ctx
            await run_import_scan_task(job.id)

        event = await asyncio.wait_for(queue.get(), timeout=0.1)
        assert event.status == ImportJobStatus.CANCELLED
        assert event.phase == "done"
        assert event.progress == 100
        assert event.message == "Import cancelled by user."
        assert await db_session.get(ImportJob, job.id) is None

        replacement_queue = get_progress_queue(job.id)
        assert replacement_queue is not queue
        assert replacement_queue.empty()
        remove_progress_queue(job.id)


# ── Test: run_import_execute_task ────────────────────────────────────────


class TestRunImportExecuteTask:
    """Test the background import execution task."""

    @pytest.mark.asyncio
    async def test_execute_task_calls_service(self, db_session: AsyncSession) -> None:
        """run_import_execute_task calls run_import on the service."""
        job = await _create_job(db_session, status=ImportJobStatus.IMPORTING)
        await db_session.commit()

        mock_service = AsyncMock()

        @asynccontextmanager
        async def mock_session_ctx():
            yield db_session

        with (
            patch("pullbox.tasks.import_task.get_session_factory") as mock_factory,
            patch(
                "pullbox.tasks.import_task._build_import_service",
                return_value=mock_service,
            ),
        ):
            mock_factory.return_value = mock_session_ctx
            await run_import_execute_task(job.id)

        mock_service.run_import.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_task_schedules_comicinfo_enrichment_after_commit(
        self, db_session: AsyncSession
    ) -> None:
        """Deferred ComicInfo enrichment starts only after Step 4 commits."""
        job = await _create_job(db_session, status=ImportJobStatus.IMPORTING)
        await db_session.commit()
        events: list[str] = []

        mock_service = AsyncMock()

        async def complete_import(session, job_id, progress_callback=None):
            import_job = await session.get(ImportJob, job_id)
            assert import_job is not None
            import_job.status = ImportJobStatus.COMPLETED
            await session.flush()
            return RunImportResult(schedule_comicinfo_enrichment=True)

        async def commit_with_event() -> None:
            await original_commit()
            events.append("commit")

        mock_service.run_import.side_effect = complete_import
        mock_service.schedule_comicinfo_enrichment = Mock(
            side_effect=lambda *_args, **_kwargs: events.append("schedule")
        )
        original_commit = db_session.commit

        @asynccontextmanager
        async def mock_session_ctx():
            yield db_session

        with (
            patch.object(db_session, "commit", new=commit_with_event),
            patch("pullbox.tasks.import_task.get_session_factory") as mock_factory,
            patch(
                "pullbox.tasks.import_task._build_import_service",
                return_value=mock_service,
            ),
        ):
            mock_factory.return_value = mock_session_ctx
            await run_import_execute_task(job.id)

        assert events[:2] == ["commit", "schedule"]
        mock_service.schedule_comicinfo_enrichment.assert_called_once_with(
            mock_session_ctx,
            job_id=job.id,
        )

    @pytest.mark.asyncio
    async def test_execute_task_nudges_story_arc_sync_after_commit(
        self, db_session: AsyncSession
    ) -> None:
        """Import-owned placement work becomes runnable only after its commit."""
        job = await _create_job(db_session, status=ImportJobStatus.IMPORTING)
        await db_session.commit()
        events: list[str] = []
        mock_service = AsyncMock()

        async def queue_placements(session, job_id, progress_callback=None):
            import_job = await session.get(ImportJob, job_id)
            assert import_job is not None
            import_job.progress_snapshot = {
                "mode": "import",
                "phase": "story_arc_placements",
                "progress": 99,
                "story_arc_placements_total": 2,
                "story_arc_placements_queued": 2,
                "story_arc_placements_completed": 0,
                "story_arc_placements_failed": 0,
            }
            await session.flush()
            return RunImportResult(schedule_story_arc_sync=True)

        async def commit_with_event() -> None:
            await original_commit()
            events.append("commit")

        mock_service.run_import.side_effect = queue_placements
        mock_service.schedule_story_arc_sync = Mock(
            side_effect=lambda: events.append("story_arc_sync")
        )
        original_commit = db_session.commit

        @asynccontextmanager
        async def mock_session_ctx():
            yield db_session

        with (
            patch.object(db_session, "commit", new=commit_with_event),
            patch("pullbox.tasks.import_task.get_session_factory") as mock_factory,
            patch(
                "pullbox.tasks.import_task._build_import_service",
                return_value=mock_service,
            ),
        ):
            mock_factory.return_value = mock_session_ctx
            await run_import_execute_task(job.id)

        assert events[:2] == ["commit", "story_arc_sync"]
        mock_service.schedule_story_arc_sync.assert_called_once_with()
        await db_session.refresh(job)
        assert job.status is ImportJobStatus.IMPORTING
        assert job.progress_snapshot["phase"] == "story_arc_placements"
        assert job.progress_snapshot["progress"] == 99
        assert job.progress_snapshot["story_arc_placements_total"] == 2

    @pytest.mark.asyncio
    async def test_execute_task_cancelled_import_runs_automatic_rollback(
        self, db_session: AsyncSession
    ) -> None:
        """Cancelled imports unwind recorded changes before becoming CANCELLED history rows."""
        from datetime import UTC, datetime

        job = await _create_job(db_session, status=ImportJobStatus.IMPORTING)
        job.import_started_at = datetime.now(UTC)
        job.error_message = "Import cancelled by user."
        await db_session.commit()

        mock_service = AsyncMock()
        mock_service.run_import.side_effect = JobCancelledError("cancelled")

        async def _complete_cancel_rollback(session, job_id, progress_callback=None):
            import_job = await session.get(ImportJob, job_id)
            assert import_job is not None
            import_job.status = ImportJobStatus.CANCELLED
            import_job.control_request = "none"
            import_job.progress_snapshot = {
                "status": ImportJobStatus.CANCELLED.value,
                "mode": "import",
                "phase": "done",
                "progress": 100,
                "message": "Import cancelled by user.",
            }
            await session.flush()

        mock_service.rollback_import.side_effect = _complete_cancel_rollback

        @asynccontextmanager
        async def mock_session_ctx():
            yield db_session

        with (
            patch("pullbox.tasks.import_task.get_session_factory") as mock_factory,
            patch(
                "pullbox.tasks.import_task._build_import_service",
                return_value=mock_service,
            ),
        ):
            mock_factory.return_value = mock_session_ctx
            await run_import_execute_task(job.id)

        await db_session.refresh(job)
        mock_service.rollback_import.assert_awaited_once()
        assert job.status == ImportJobStatus.CANCELLED
        assert job.progress_snapshot["status"] == ImportJobStatus.CANCELLED.value
        assert job.progress_snapshot["phase"] == "done"
        assert job.progress_snapshot["message"] == "Import cancelled by user."

    @pytest.mark.asyncio
    async def test_execute_task_keeps_truthful_paused_snapshot(
        self, db_session: AsyncSession
    ) -> None:
        """Paused imports keep their exact import checkpoint instead of being terminalized."""
        job = await _create_job(db_session, status=ImportJobStatus.IMPORTING)
        job.progress_snapshot = {
            "status": ImportJobStatus.IMPORTING.value,
            "mode": "import",
            "phase": "importing",
            "progress": 64,
            "message": "Processing file 2/5 in review group 8/25",
            "current_series_name": "Fearscape",
            "current_file_name": "Fearscape Vol 02.pdf",
            "current_file_stage": "rendering",
            "current_file_progress_pct": 42,
        }
        await db_session.commit()

        mock_service = AsyncMock()
        mock_service.run_import.side_effect = JobPausedError("paused")

        @asynccontextmanager
        async def mock_session_ctx():
            yield db_session

        with (
            patch("pullbox.tasks.import_task.get_session_factory") as mock_factory,
            patch(
                "pullbox.tasks.import_task._build_import_service",
                return_value=mock_service,
            ),
        ):
            mock_factory.return_value = mock_session_ctx
            await run_import_execute_task(job.id)

        await db_session.refresh(job)
        assert job.status == ImportJobStatus.PAUSED
        assert job.progress_snapshot["status"] == ImportJobStatus.PAUSED.value
        assert job.progress_snapshot["phase"] == "importing"
        assert job.progress_snapshot["progress"] == 64
        assert job.progress_snapshot["message"] == "Import is paused."
        assert job.progress_snapshot["current_file_name"] == "Fearscape Vol 02.pdf"
        assert job.progress_snapshot["current_file_stage"] == "rendering"
        assert job.progress_snapshot["control_state"]["can_resume"] is True
        assert job.progress_snapshot["control_state"]["can_cancel"] is True
        assert int(job.progress_snapshot["progress_revision"]) > 0

    @pytest.mark.asyncio
    async def test_execute_task_marks_failed_on_exception(self, db_session: AsyncSession) -> None:
        """If run_import raises, job is marked FAILED."""
        job = await _create_job(db_session, status=ImportJobStatus.IMPORTING)
        await db_session.commit()

        mock_service = AsyncMock()
        mock_service.run_import.side_effect = RuntimeError("Import exploded")

        @asynccontextmanager
        async def mock_session_ctx():
            yield db_session

        with (
            patch("pullbox.tasks.import_task.get_session_factory") as mock_factory,
            patch(
                "pullbox.tasks.import_task._build_import_service",
                return_value=mock_service,
            ),
        ):
            mock_factory.return_value = mock_session_ctx
            await run_import_execute_task(job.id)

        await db_session.refresh(job)
        assert job.status == ImportJobStatus.FAILED
        assert job.error_message is not None

    @pytest.mark.asyncio
    async def test_runner_marks_database_lock_as_stalled(
        self, async_engine: object, db_session: AsyncSession
    ) -> None:
        """Transient SQLite lock failures leave imports resumable instead of failed."""
        job = await _create_job(db_session, status=ImportJobStatus.IMPORTING)
        job.import_started_at = datetime.now(UTC)
        job.progress_snapshot = {
            "status": ImportJobStatus.IMPORTING.value,
            "mode": "import",
            "phase": "importing",
            "progress": 41,
            "message": "Processing review group 7/24...",
            "current_series_name": "King Dracula",
        }
        await db_session.commit()

        factory = async_sessionmaker(async_engine, expire_on_commit=False)
        runner = ImportRunner(factory)
        mock_service = AsyncMock()
        mock_service.run_import.side_effect = _sqlite_database_locked_error()

        with (
            patch("pullbox.tasks.import_task._build_import_service", return_value=mock_service),
            patch("pullbox.tasks.import_task.publish", new_callable=AsyncMock),
        ):
            await runner._run_job(job.id)

        await db_session.refresh(job)
        assert job.status == ImportJobStatus.STALLED
        assert (
            job.error_message == "Import stalled because the database was busy. Resume when ready."
        )
        assert job.progress_snapshot["status"] == ImportJobStatus.STALLED.value
        assert job.progress_snapshot["phase"] == "importing"
        assert job.progress_snapshot["progress"] == 41
        assert job.progress_snapshot["control_state"]["can_resume"] is True
        assert job.progress_snapshot["control_state"]["can_cancel"] is True

    @pytest.mark.asyncio
    async def test_runner_resumes_stalled_scan_from_snapshot_phase(
        self, async_engine: object, db_session: AsyncSession
    ) -> None:
        """A stalled scan resumes from the stalled phase instead of restarting."""
        job = await _create_job(db_session, status=ImportJobStatus.STALLED)
        job.error_message = "Import stalled because the database was busy. Resume when ready."
        job.progress_snapshot = {
            "status": ImportJobStatus.STALLED.value,
            "mode": "scan",
            "phase": "file_matching",
            "progress": 87,
            "message": "Import stalled because the database was busy. Resume when ready.",
        }
        await db_session.commit()

        factory = async_sessionmaker(async_engine, expire_on_commit=False)
        runner = ImportRunner(factory)
        mock_service = AsyncMock()

        async def complete_resume(session, job_id, progress_callback=None):
            import_job = await session.get(ImportJob, job_id)
            assert import_job is not None
            import_job.status = ImportJobStatus.REVIEW
            import_job.error_message = None
            await session.flush()

        mock_service.resume_scan_phase.side_effect = complete_resume

        with (
            patch("pullbox.tasks.import_task._build_import_service", return_value=mock_service),
            patch("pullbox.tasks.import_task.publish", new_callable=AsyncMock),
        ):
            await runner._run_job(job.id)

        mock_service.resume_scan_phase.assert_awaited_once()
        mock_service.start_scan.assert_not_called()
        await db_session.refresh(job)
        assert job.status == ImportJobStatus.REVIEW

    @pytest.mark.asyncio
    async def test_execute_task_terminal_failure_revision_beats_live_only_progress(
        self, db_session: AsyncSession
    ) -> None:
        """Terminal failure snapshots must outrank any live-only file progress already seen."""
        job = await _create_job(db_session, status=ImportJobStatus.IMPORTING)
        job.progress_snapshot = {
            "status": "importing",
            "mode": "import",
            "phase": "importing",
            "progress": 91,
            "message": "Finalizing imported file",
            "progress_revision": 12,
        }
        job.progress_revision = 12
        await db_session.commit()

        set_latest_progress_event(
            ImportProgressEvent(
                job_id=job.id,
                status=ImportJobStatus.IMPORTING,
                mode="import",
                phase="importing",
                progress=99,
                message="Finalizing imported file",
                progress_revision=25,
                current_file_name="Persephone.2022.Hybrid.Comic.eBook-BitBook.pdf",
                current_file_stage="finalizing",
                current_file_progress_current=3,
                current_file_progress_total=4,
                current_file_progress_pct=99,
                current_file_progress_unit="steps",
            )
        )

        mock_service = AsyncMock()
        mock_service.run_import.side_effect = RuntimeError("Import exploded")

        @asynccontextmanager
        async def mock_session_ctx():
            yield db_session

        with (
            patch("pullbox.tasks.import_task.get_session_factory") as mock_factory,
            patch(
                "pullbox.tasks.import_task._build_import_service",
                return_value=mock_service,
            ),
        ):
            mock_factory.return_value = mock_session_ctx
            await run_import_execute_task(job.id)

        await db_session.refresh(job)
        assert job.status == ImportJobStatus.FAILED
        assert int(job.progress_snapshot["progress_revision"]) > 25
        remove_progress_queue(job.id)

    @pytest.mark.asyncio
    async def test_execute_task_persists_completed_terminal_snapshot(
        self, db_session: AsyncSession
    ) -> None:
        """Successful execute persists a truthful completed snapshot for refresh-driven UI."""
        job = await _create_job(db_session, status=ImportJobStatus.IMPORTING)
        job.progress_snapshot = {
            "status": "importing",
            "mode": "import",
            "phase": "importing",
            "progress": 64,
            "message": "Processing file 2/5 in review group 8/25",
            "current_series_name": "Fearscape",
            "current_file_name": "Fearscape Vol 02.pdf",
            "current_file_stage": "rendering",
            "current_file_progress_pct": 42,
            "control_state": {"can_pause": True, "can_cancel": True},
        }
        await db_session.commit()

        mock_service = AsyncMock()

        async def complete_import(session, job_id, progress_callback=None):
            import_job = await session.get(ImportJob, job_id)
            assert import_job is not None
            import_job.status = ImportJobStatus.COMPLETED
            import_job.total_files_imported = 1
            import_job.error_message = None
            await session.flush()

        mock_service.run_import.side_effect = complete_import

        @asynccontextmanager
        async def mock_session_ctx():
            yield db_session

        with (
            patch("pullbox.tasks.import_task.get_session_factory") as mock_factory,
            patch(
                "pullbox.tasks.import_task._build_import_service",
                return_value=mock_service,
            ),
        ):
            mock_factory.return_value = mock_session_ctx
            await run_import_execute_task(job.id)

        await db_session.refresh(job)
        assert job.progress_snapshot["status"] == ImportJobStatus.COMPLETED.value
        assert job.progress_snapshot["phase"] == "done"
        assert job.progress_snapshot["progress"] == 100
        assert job.progress_snapshot["message"] == "Import complete."
        assert job.progress_snapshot["current_file_name"] is None
        assert job.progress_snapshot["current_file_stage"] is None
        assert job.progress_snapshot["control_state"]["can_pause"] is False
        assert "control_state" in job.progress_snapshot

    @pytest.mark.asyncio
    async def test_execute_task_projects_completed_terminal_state_to_global_activity(
        self, db_session: AsyncSession
    ) -> None:
        """A completed import must stop the global spinner at 100 percent."""
        job = await _create_job(db_session, status=ImportJobStatus.IMPORTING)
        job.progress_revision = 7
        job.progress_snapshot = {
            "status": ImportJobStatus.IMPORTING.value,
            "mode": "import",
            "phase": "importing",
            "progress": 64,
            "progress_revision": 7,
            "message": "Processing file 2/5 in review group 8/25",
        }
        await publish_operation_progress(
            db_session,
            OperationProgressUpdate(
                operation_type=OperationProgressType.IMPORT,
                operation_key=str(job.id),
                revision=7,
                state=OperationProgressState.RUNNING,
                phase="importing",
                title="Folder import",
                message="Processing file 2/5 in review group 8/25",
                overall=OperationProgressMeasure(percent=64),
            ),
        )
        await db_session.commit()

        mock_service = AsyncMock()

        async def complete_import(session, job_id, progress_callback=None):
            import_job = await session.get(ImportJob, job_id)
            assert import_job is not None
            import_job.status = ImportJobStatus.COMPLETED
            import_job.total_files_imported = 1
            await session.flush()

        mock_service.run_import.side_effect = complete_import

        @asynccontextmanager
        async def mock_session_ctx():
            yield db_session

        with (
            patch("pullbox.tasks.import_task.get_session_factory") as mock_factory,
            patch(
                "pullbox.tasks.import_task._build_import_service",
                return_value=mock_service,
            ),
        ):
            mock_factory.return_value = mock_session_ctx
            await run_import_execute_task(job.id)

        operation = (
            await db_session.execute(
                select(OperationProgress).where(
                    OperationProgress.operation_type == OperationProgressType.IMPORT,
                    OperationProgress.operation_key == str(job.id),
                )
            )
        ).scalar_one()
        await db_session.refresh(job)

        assert operation.state is OperationProgressState.COMPLETED
        assert operation.revision == job.progress_revision
        assert operation.overall_percent == 100
        assert operation.overall_indeterminate is False
        assert operation.completed_at is not None

    @pytest.mark.asyncio
    async def test_rollback_terminal_event_stops_global_activity_spinner(
        self, db_session: AsyncSession
    ) -> None:
        """Rollback completion must replace its last running activity revision."""
        job = await _create_job(db_session, status=ImportJobStatus.ROLLED_BACK)
        job.progress_revision = 18
        job.progress_snapshot = {
            "status": ImportJobStatus.ROLLED_BACK.value,
            "mode": "rollback",
            "phase": "rollback",
            "progress": 100,
            "progress_revision": 18,
            "message": "Import rollback completed.",
        }
        await publish_operation_progress(
            db_session,
            OperationProgressUpdate(
                operation_type=OperationProgressType.IMPORT,
                operation_key=str(job.id),
                revision=17,
                state=OperationProgressState.RUNNING,
                phase="rollback",
                title="Folder import",
                message="Rolling back 376/376 actions...",
                overall=OperationProgressMeasure(percent=100),
            ),
        )
        await db_session.commit()

        with patch("pullbox.tasks.import_task.publish", new_callable=AsyncMock):
            terminal_status = await _emit_terminal_event_for_job(db_session, job.id)

        operation = (
            await db_session.execute(
                select(OperationProgress).where(
                    OperationProgress.operation_type == OperationProgressType.IMPORT,
                    OperationProgress.operation_key == str(job.id),
                )
            )
        ).scalar_one()

        assert terminal_status is ImportJobStatus.ROLLED_BACK
        assert operation.state is OperationProgressState.COMPLETED
        assert operation.phase == "rollback"
        assert operation.overall_percent == 100
        assert operation.completed_at is not None


# ── Test: Startup Recovery ───────────────────────────────────────────────


class TestStartupRecovery:
    """Test recover_stuck_import_jobs at startup."""

    @pytest.mark.asyncio
    async def test_scanning_job_recovered(
        self, async_engine: object, db_session: AsyncSession
    ) -> None:
        """Job in SCANNING state is paused on startup recovery."""
        job = await _create_job(db_session, status=ImportJobStatus.SCANNING)
        await db_session.commit()

        factory = async_sessionmaker(async_engine, expire_on_commit=False)
        recovered = await recover_stuck_import_jobs(factory)

        assert recovered == 1
        await db_session.refresh(job)
        assert job.status == ImportJobStatus.PAUSED
        snapshot = job.progress_snapshot or {}
        assert snapshot.get("status") == ImportJobStatus.PAUSED.value
        assert snapshot.get("pause_reason") == "startup_recovery"
        assert snapshot.get("recovered_status") == ImportJobStatus.SCANNING.value

    @pytest.mark.asyncio
    async def test_analyzing_job_recovered(
        self, async_engine: object, db_session: AsyncSession
    ) -> None:
        """Job in ANALYZING state is paused on startup recovery."""
        job = await _create_job(db_session, status=ImportJobStatus.ANALYZING)
        await db_session.commit()

        factory = async_sessionmaker(async_engine, expire_on_commit=False)
        recovered = await recover_stuck_import_jobs(factory)
        assert recovered == 1
        await db_session.refresh(job)
        assert job.status == ImportJobStatus.PAUSED
        assert (job.progress_snapshot or {}).get("phase") == "analyzing"

    @pytest.mark.asyncio
    async def test_importing_job_recovered(
        self, async_engine: object, db_session: AsyncSession
    ) -> None:
        """Job in IMPORTING state is paused and keeps importing phase on restart."""
        job = await _create_job(db_session, status=ImportJobStatus.IMPORTING)
        await db_session.commit()

        factory = async_sessionmaker(async_engine, expire_on_commit=False)
        recovered = await recover_stuck_import_jobs(factory)
        assert recovered == 1
        await db_session.refresh(job)
        assert job.status == ImportJobStatus.PAUSED
        assert (job.progress_snapshot or {}).get("phase") == "importing"
        assert (job.progress_snapshot or {}).get("recovered_status") == (
            ImportJobStatus.IMPORTING.value
        )

    @pytest.mark.asyncio
    async def test_story_arc_placement_phase_survives_startup_recovery(
        self, async_engine: object, db_session: AsyncSession
    ) -> None:
        """A durable placement wait resumes its finalizer instead of replaying import."""
        job = await _create_job(db_session, status=ImportJobStatus.IMPORTING)
        job.progress_snapshot = {
            "status": ImportJobStatus.IMPORTING.value,
            "mode": "import",
            "phase": "story_arc_placements",
            "progress": 99,
            "story_arc_placements_total": 3,
            "story_arc_placements_queued": 2,
            "story_arc_placements_completed": 1,
            "story_arc_placements_failed": 0,
        }
        await db_session.commit()

        factory = async_sessionmaker(async_engine, expire_on_commit=False)
        recovered = await recover_stuck_import_jobs(factory)

        assert recovered == 1
        await db_session.refresh(job)
        assert job.status == ImportJobStatus.PAUSED
        snapshot = dict(job.progress_snapshot or {})
        assert snapshot["phase"] == "story_arc_placements"
        assert snapshot["story_arc_placements_total"] == 3
        assert snapshot["recovered_status"] == ImportJobStatus.IMPORTING.value

    @pytest.mark.asyncio
    async def test_file_matching_job_recovered(
        self, async_engine: object, db_session: AsyncSession
    ) -> None:
        """Job in FILE_MATCHING state is paused on startup recovery."""
        job = await _create_job(db_session, status=ImportJobStatus.FILE_MATCHING)
        await db_session.commit()

        factory = async_sessionmaker(async_engine, expire_on_commit=False)
        recovered = await recover_stuck_import_jobs(factory)
        assert recovered == 1
        await db_session.refresh(job)
        assert job.status == ImportJobStatus.PAUSED
        assert (job.progress_snapshot or {}).get("phase") == "file_matching"

    @pytest.mark.asyncio
    async def test_recover_and_dispatch_auto_resumes_startup_recovered_job(
        self, async_engine: object, db_session: AsyncSession
    ) -> None:
        """Startup-recovered paused jobs are restored and handed to the runner."""
        job = await _create_job(db_session, status=ImportJobStatus.FILE_MATCHING)
        await db_session.commit()

        factory = async_sessionmaker(async_engine, expire_on_commit=False)
        runner = ImportRunner(factory)
        start_worker = Mock()
        runner._start_worker_locked = start_worker

        recovered = await runner.recover_and_dispatch()

        assert recovered == 1
        start_worker.assert_called_once_with(job.id)
        await db_session.refresh(job)
        assert job.status == ImportJobStatus.FILE_MATCHING
        snapshot = job.progress_snapshot or {}
        assert snapshot["status"] == ImportJobStatus.FILE_MATCHING.value
        assert snapshot["phase"] == "file_matching"
        assert "pause_reason" not in snapshot
        assert "recovered_status" not in snapshot

    @pytest.mark.asyncio
    async def test_recover_and_dispatch_drains_multiple_recovered_jobs_serially(
        self, async_engine: object, db_session: AsyncSession
    ) -> None:
        """Every recovered job is resumed in order without overlapping import runners."""
        first = await _create_job(db_session, status=ImportJobStatus.IMPORTING)
        second = await _create_job(db_session, status=ImportJobStatus.IMPORTING)
        first.created_at = datetime.now(UTC) - timedelta(minutes=1)
        second.created_at = datetime.now(UTC)
        for job in (first, second):
            job.import_started_at = datetime.now(UTC)
            job.progress_snapshot = {
                "status": ImportJobStatus.IMPORTING.value,
                "mode": "import",
                "phase": "story_arc_placements",
                "progress": 99,
                "story_arc_placements_total": 1,
            }
        await db_session.commit()

        factory = async_sessionmaker(async_engine, expire_on_commit=False)
        runner = ImportRunner(factory)
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        calls: list[int] = []
        running = 0
        maximum_running = 0

        async def finish_recovered_job(job_id: int) -> None:
            nonlocal maximum_running, running
            running += 1
            maximum_running = max(maximum_running, running)
            calls.append(job_id)
            try:
                async with factory() as session:
                    recovered = await session.get(ImportJob, job_id)
                    assert recovered is not None
                    assert recovered.status is ImportJobStatus.IMPORTING
                    snapshot = dict(recovered.progress_snapshot or {})
                    assert snapshot.get("pause_reason") is None
                    assert snapshot.get("recovered_status") is None
                if job_id == first.id:
                    first_started.set()
                    await release_first.wait()
                async with factory() as session:
                    recovered = await session.get(ImportJob, job_id)
                    assert recovered is not None
                    recovered.status = ImportJobStatus.COMPLETED
                    recovered.progress_snapshot = {
                        "status": ImportJobStatus.COMPLETED.value,
                        "mode": "import",
                        "phase": "done",
                        "progress": 100,
                    }
                    await session.commit()
            finally:
                running -= 1

        runner._run_job = finish_recovered_job  # type: ignore[method-assign]

        recovered_count = await runner.recover_and_dispatch()
        await asyncio.wait_for(first_started.wait(), timeout=1)
        worker = runner._worker_task
        assert worker is not None

        await db_session.refresh(first)
        await db_session.refresh(second)
        assert first.status is ImportJobStatus.IMPORTING
        assert second.status is ImportJobStatus.PAUSED
        assert (second.progress_snapshot or {}).get("pause_reason") == "startup_recovery"

        release_first.set()
        await asyncio.wait_for(worker, timeout=1)

        assert recovered_count == 2
        assert calls == [first.id, second.id]
        assert maximum_running == 1
        await db_session.refresh(first)
        await db_session.refresh(second)
        assert first.status is ImportJobStatus.COMPLETED
        assert second.status is ImportJobStatus.COMPLETED

    @pytest.mark.parametrize(
        "status",
        [ImportJobStatus.MATCHING, ImportJobStatus.ROLLING_BACK],
    )
    @pytest.mark.asyncio
    async def test_recovered_dispatch_ignores_unmarked_active_jobs(
        self,
        async_engine: object,
        db_session: AsyncSession,
        status: ImportJobStatus,
    ) -> None:
        """Normal live work must never be claimed as startup-recovered work."""
        job = await _create_job(db_session, status=status)
        job.import_started_at = datetime.now(UTC)
        job.progress_snapshot = {
            "status": status.value,
            "mode": "rollback" if status is ImportJobStatus.ROLLING_BACK else "scan",
            "phase": "rollback" if status is ImportJobStatus.ROLLING_BACK else "matching",
        }
        await db_session.commit()

        factory = async_sessionmaker(async_engine, expire_on_commit=False)
        runner = ImportRunner(factory)
        start_worker = Mock()
        runner._start_worker_locked = start_worker

        await runner.request_recovered_dispatch()

        start_worker.assert_not_called()
        await db_session.refresh(job)
        assert job.status is status

    @pytest.mark.asyncio
    async def test_stalled_job_blocks_later_startup_recovery_until_terminal(
        self, async_engine: object, db_session: AsyncSession
    ) -> None:
        """A stalled import remains the single active owner until it is resolved."""
        stalled = await _create_job(db_session, status=ImportJobStatus.STALLED)
        recovered = await _create_job(db_session, status=ImportJobStatus.IMPORTING)
        for job in (stalled, recovered):
            job.import_started_at = datetime.now(UTC)
            job.progress_snapshot = {
                "status": job.status.value,
                "mode": "import",
                "phase": "story_arc_placements",
                "progress": 99,
                "story_arc_placements_total": 1,
            }
        await db_session.commit()

        factory = async_sessionmaker(async_engine, expire_on_commit=False)
        runner = ImportRunner(factory)
        start_worker = Mock()
        runner._start_worker_locked = start_worker

        assert await runner.recover_and_dispatch() == 1
        start_worker.assert_not_called()
        await db_session.refresh(recovered)
        assert recovered.status is ImportJobStatus.PAUSED
        assert (recovered.progress_snapshot or {}).get("pause_reason") == "startup_recovery"

        async with factory() as session:
            resolved = await session.get(ImportJob, stalled.id)
            assert resolved is not None
            resolved.status = ImportJobStatus.COMPLETED
            resolved.progress_snapshot = {
                "status": ImportJobStatus.COMPLETED.value,
                "mode": "import",
                "phase": "done",
                "progress": 100,
            }
            await session.commit()

        await runner.request_recovered_dispatch()

        start_worker.assert_called_once_with(recovered.id)
        await db_session.refresh(recovered)
        assert recovered.status is ImportJobStatus.IMPORTING
        assert (recovered.progress_snapshot or {}).get("pause_reason") is None

    @pytest.mark.asyncio
    async def test_completed_placement_finalizer_wakes_next_recovered_job(
        self, async_engine: object, db_session: AsyncSession
    ) -> None:
        """Async placement completion resumes the next startup-recovered import."""
        first = await _create_job(db_session, status=ImportJobStatus.IMPORTING)
        second = await _create_job(db_session, status=ImportJobStatus.IMPORTING)
        first.created_at = datetime.now(UTC) - timedelta(minutes=1)
        second.created_at = datetime.now(UTC)
        for job in (first, second):
            job.import_started_at = datetime.now(UTC)
            job.progress_snapshot = {
                "status": ImportJobStatus.IMPORTING.value,
                "mode": "import",
                "phase": "story_arc_placements",
                "progress": 99,
                "story_arc_placements_total": 1,
            }
        await db_session.commit()

        factory = async_sessionmaker(async_engine, expire_on_commit=False)
        runner = ImportRunner(factory)
        calls: list[int] = []

        async def finish_only_the_second(job_id: int) -> None:
            calls.append(job_id)
            if job_id == first.id:
                return
            async with factory() as session:
                recovered = await session.get(ImportJob, job_id)
                assert recovered is not None
                assert recovered.status is ImportJobStatus.IMPORTING
                recovered.status = ImportJobStatus.COMPLETED
                recovered.progress_snapshot = {
                    "status": ImportJobStatus.COMPLETED.value,
                    "mode": "import",
                    "phase": "done",
                    "progress": 100,
                }
                await session.commit()

        runner._run_job = finish_only_the_second  # type: ignore[method-assign]
        assert await runner.recover_and_dispatch() == 2
        initial_worker = runner._worker_task
        assert initial_worker is not None
        await asyncio.wait_for(initial_worker, timeout=1)
        await asyncio.sleep(0)

        await db_session.refresh(first)
        await db_session.refresh(second)
        assert first.status is ImportJobStatus.IMPORTING
        assert second.status is ImportJobStatus.PAUSED

        async with factory() as session:
            completed = await session.get(ImportJob, first.id)
            assert completed is not None
            completed.status = ImportJobStatus.COMPLETED
            completed.import_completed_at = datetime.now(UTC)
            completed.story_arc_placement_followup_pending = True
            completed.progress_snapshot = {
                "status": ImportJobStatus.COMPLETED.value,
                "mode": "import",
                "phase": "done",
                "progress": 100,
                "story_arc_placements_total": 1,
                "story_arc_placements_completed": 1,
                "story_arc_placement_followup_pending": True,
            }
            await session.commit()

        service = AsyncMock()
        service.schedule_comicinfo_enrichment = Mock()
        with (
            patch("pullbox.tasks.import_task._import_runner", runner),
            patch(
                "pullbox.tasks.import_task._build_import_service",
                new=AsyncMock(return_value=service),
            ),
            patch("pullbox.tasks.import_task.publish", new_callable=AsyncMock),
        ):
            await publish_story_arc_import_updates(
                (first.id,),
                completed_job_ids=(first.id,),
                session_factory=factory,
            )
            resumed_worker = runner._worker_task
            assert resumed_worker is not None
            await asyncio.wait_for(resumed_worker, timeout=1)

        assert calls == [first.id, second.id]
        await db_session.refresh(second)
        assert second.status is ImportJobStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_recover_and_dispatch_leaves_user_paused_job_alone(
        self, async_engine: object, db_session: AsyncSession
    ) -> None:
        """Ordinary user-paused jobs do not auto-resume at startup."""
        job = await _create_job(db_session, status=ImportJobStatus.PAUSED)
        job.progress_snapshot = {
            "status": ImportJobStatus.PAUSED.value,
            "mode": "scan",
            "phase": "file_matching",
            "message": "Import scan is paused.",
        }
        await db_session.commit()

        factory = async_sessionmaker(async_engine, expire_on_commit=False)
        runner = ImportRunner(factory)
        start_worker = Mock()
        runner._start_worker_locked = start_worker

        recovered = await runner.recover_and_dispatch()

        assert recovered == 0
        start_worker.assert_not_called()
        await db_session.refresh(job)
        assert job.status == ImportJobStatus.PAUSED

    @pytest.mark.asyncio
    async def test_startup_preserves_in_flight_user_pause_without_auto_resume(
        self, async_engine: object, db_session: AsyncSession
    ) -> None:
        job = await _create_job(db_session, status=ImportJobStatus.PAUSING)
        job.import_started_at = datetime.now(UTC)
        job.control_request = ImportControlRequest.PAUSE
        job.progress_snapshot = {
            "status": ImportJobStatus.PAUSING.value,
            "mode": "import",
            "phase": "importing",
            "requested_action": ImportControlRequest.PAUSE.value,
        }
        await db_session.commit()

        factory = async_sessionmaker(async_engine, expire_on_commit=False)
        runner = ImportRunner(factory)
        start_worker = Mock()
        runner._start_worker_locked = start_worker

        recovered = await runner.recover_and_dispatch()

        assert recovered == 1
        start_worker.assert_not_called()
        await db_session.refresh(job)
        assert job.status is ImportJobStatus.PAUSED
        assert job.control_request is ImportControlRequest.NONE
        snapshot = dict(job.progress_snapshot or {})
        assert snapshot["requested_action"] == ImportControlRequest.NONE.value
        assert snapshot.get("pause_reason") != "startup_recovery"

    @pytest.mark.asyncio
    async def test_startup_preserves_cancel_as_rollback_and_dispatches_it(
        self, async_engine: object, db_session: AsyncSession
    ) -> None:
        job = await _create_job(db_session, status=ImportJobStatus.CANCELLING)
        job.import_started_at = datetime.now(UTC)
        job.control_request = ImportControlRequest.CANCEL
        job.story_arc_placement_followup_pending = True
        job.progress_snapshot = {
            "status": ImportJobStatus.CANCELLING.value,
            "mode": "import",
            "phase": "story_arcs",
            "requested_action": ImportControlRequest.CANCEL.value,
        }
        await db_session.commit()

        factory = async_sessionmaker(async_engine, expire_on_commit=False)
        runner = ImportRunner(factory)
        start_worker = Mock()
        runner._start_worker_locked = start_worker

        recovered = await runner.recover_and_dispatch()

        assert recovered == 1
        start_worker.assert_called_once_with(job.id)
        await db_session.refresh(job)
        assert job.status is ImportJobStatus.ROLLING_BACK
        assert job.control_request is ImportControlRequest.CANCEL
        assert job.story_arc_placement_followup_pending is False
        snapshot = dict(job.progress_snapshot or {})
        assert snapshot["status"] == ImportJobStatus.ROLLING_BACK.value
        assert snapshot["mode"] == "rollback"
        assert snapshot["phase"] == "queued"
        assert snapshot["requested_action"] == ImportControlRequest.CANCEL.value

    @pytest.mark.asyncio
    async def test_runner_uses_phase_resume_for_scan_states(
        self, async_engine: object, db_session: AsyncSession
    ) -> None:
        """Recovered matching phases resume in-place instead of restarting scan."""
        job = await _create_job(db_session, status=ImportJobStatus.FILE_MATCHING)
        job.progress_snapshot = {
            "status": ImportJobStatus.FILE_MATCHING.value,
            "mode": "scan",
            "phase": "file_matching",
        }
        await db_session.commit()

        factory = async_sessionmaker(async_engine, expire_on_commit=False)
        runner = ImportRunner(factory)
        mock_service = AsyncMock()

        async def complete_resume(session, job_id, progress_callback=None):
            import_job = await session.get(ImportJob, job_id)
            assert import_job is not None
            import_job.status = ImportJobStatus.REVIEW
            await session.flush()

        mock_service.resume_scan_phase.side_effect = complete_resume

        with (
            patch("pullbox.tasks.import_task._build_import_service", return_value=mock_service),
            patch("pullbox.tasks.import_task.publish", new_callable=AsyncMock),
        ):
            await runner._run_job(job.id)

        mock_service.resume_scan_phase.assert_awaited_once()
        mock_service.start_scan.assert_not_called()
        await db_session.refresh(job)
        assert job.status == ImportJobStatus.REVIEW

    @pytest.mark.asyncio
    async def test_runner_schedules_comicinfo_enrichment_after_commit(
        self, db_session: AsyncSession
    ) -> None:
        """The durable runner schedules deferred ComicInfo work post-commit."""
        job = await _create_job(db_session, status=ImportJobStatus.IMPORTING)
        await db_session.commit()
        events: list[str] = []

        mock_service = AsyncMock()

        async def complete_import(session, job_id, progress_callback=None):
            import_job = await session.get(ImportJob, job_id)
            assert import_job is not None
            import_job.status = ImportJobStatus.COMPLETED
            await session.flush()
            return RunImportResult(schedule_comicinfo_enrichment=True)

        async def commit_with_event() -> None:
            await original_commit()
            events.append("commit")

        mock_service.run_import.side_effect = complete_import
        mock_service.schedule_comicinfo_enrichment = Mock(
            side_effect=lambda *_args, **_kwargs: events.append("schedule")
        )
        original_commit = db_session.commit

        @asynccontextmanager
        async def mock_session_ctx():
            yield db_session

        runner = ImportRunner(mock_session_ctx)
        with (
            patch.object(db_session, "commit", new=commit_with_event),
            patch("pullbox.tasks.import_task._build_import_service", return_value=mock_service),
            patch("pullbox.tasks.import_task.publish", new_callable=AsyncMock),
        ):
            await runner._run_job(job.id)

        assert events[:2] == ["commit", "schedule"]
        mock_service.schedule_comicinfo_enrichment.assert_called_once_with(
            mock_session_ctx,
            job_id=job.id,
        )

    @pytest.mark.asyncio
    async def test_runner_nudges_story_arc_sync_after_commit(
        self, db_session: AsyncSession
    ) -> None:
        """The durable runner exposes import placement work post-commit only."""
        job = await _create_job(db_session, status=ImportJobStatus.IMPORTING)
        await db_session.commit()
        events: list[str] = []
        mock_service = AsyncMock()

        async def queue_placements(session, job_id, progress_callback=None):
            import_job = await session.get(ImportJob, job_id)
            assert import_job is not None
            import_job.progress_snapshot = {"phase": "story_arc_placements"}
            await session.flush()
            return RunImportResult(schedule_story_arc_sync=True)

        async def commit_with_event() -> None:
            await original_commit()
            events.append("commit")

        mock_service.run_import.side_effect = queue_placements
        mock_service.schedule_story_arc_sync = Mock(
            side_effect=lambda: events.append("story_arc_sync")
        )
        original_commit = db_session.commit

        @asynccontextmanager
        async def mock_session_ctx():
            yield db_session

        runner = ImportRunner(mock_session_ctx)
        with (
            patch.object(db_session, "commit", new=commit_with_event),
            patch("pullbox.tasks.import_task._build_import_service", return_value=mock_service),
            patch("pullbox.tasks.import_task.publish", new_callable=AsyncMock),
        ):
            await runner._run_job(job.id)

        assert events[:2] == ["commit", "story_arc_sync"]
        mock_service.schedule_story_arc_sync.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_runner_requeues_deferred_story_arc_rollback_after_commit(
        self, db_session: AsyncSession
    ) -> None:
        job = await _create_job(db_session, status=ImportJobStatus.ROLLING_BACK)
        await db_session.commit()
        events: list[str] = []
        mock_service = AsyncMock()
        mock_service.rollback_import.return_value = False
        mock_service.schedule_story_arc_sync = Mock(
            side_effect=lambda: events.append("story_arc_sync")
        )
        original_commit = db_session.commit

        async def commit_with_event() -> None:
            await original_commit()
            events.append("commit")

        @asynccontextmanager
        async def mock_session_ctx():
            yield db_session

        runner = ImportRunner(mock_session_ctx)
        with (
            patch.object(db_session, "commit", new=commit_with_event),
            patch("pullbox.tasks.import_task._build_import_service", return_value=mock_service),
            patch("pullbox.tasks.import_task.publish", new_callable=AsyncMock),
        ):
            await runner._run_job(job.id)

        assert events[:2] == ["commit", "story_arc_sync"]
        mock_service.schedule_story_arc_sync.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_review_job_untouched(
        self, async_engine: object, db_session: AsyncSession
    ) -> None:
        """Job in REVIEW state is NOT reset — awaiting user action."""
        job = await _create_job(db_session, status=ImportJobStatus.REVIEW)
        await db_session.commit()

        factory = async_sessionmaker(async_engine, expire_on_commit=False)
        recovered = await recover_stuck_import_jobs(factory)

        assert recovered == 0
        await db_session.refresh(job)
        assert job.status == ImportJobStatus.REVIEW

    @pytest.mark.asyncio
    async def test_completed_job_untouched(
        self, async_engine: object, db_session: AsyncSession
    ) -> None:
        """Job in COMPLETED state is NOT reset."""
        await _create_job(db_session, status=ImportJobStatus.COMPLETED)
        await db_session.commit()

        factory = async_sessionmaker(async_engine, expire_on_commit=False)
        recovered = await recover_stuck_import_jobs(factory)
        assert recovered == 0

    @pytest.mark.asyncio
    async def test_no_stuck_jobs(self, async_engine: object) -> None:
        """No stuck jobs returns 0."""
        factory = async_sessionmaker(async_engine, expire_on_commit=False)
        recovered = await recover_stuck_import_jobs(factory)
        assert recovered == 0


# ── Test: _build_import_service ──────────────────────────────────────────


class TestBuildImportService:
    """Test service construction helper."""

    @pytest.mark.asyncio
    async def test_builds_service_with_cv_key(
        self, db_session: AsyncSession, tmp_path: object
    ) -> None:
        """_build_import_service constructs ImportService with dependencies."""
        from unittest.mock import MagicMock

        mock_settings = MagicMock()
        mock_settings.comicvine_rate_limit = 1.0
        mock_settings.covers_dir = tmp_path
        mock_settings.metadata_refresh_days = 30

        with (
            patch(
                "pullbox.composition.services.get_comicvine_api_key",
                new_callable=AsyncMock,
                return_value="test-key",
            ),
            patch("pullbox.composition.services.get_settings", return_value=mock_settings),
        ):
            from pullbox.services.import_service import ImportService

            service = await _build_import_service(db_session)
            assert isinstance(service, ImportService)

    @pytest.mark.asyncio
    async def test_builds_service_without_cv_key(
        self, db_session: AsyncSession, tmp_path: object
    ) -> None:
        """_build_import_service works even without a CV API key."""
        from unittest.mock import MagicMock

        mock_settings = MagicMock()
        mock_settings.comicvine_rate_limit = 1.0
        mock_settings.covers_dir = tmp_path
        mock_settings.metadata_refresh_days = 30

        with (
            patch(
                "pullbox.composition.services.get_comicvine_api_key",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("pullbox.composition.services.get_settings", return_value=mock_settings),
        ):
            from pullbox.services.import_service import ImportService

            service = await _build_import_service(db_session)
            assert isinstance(service, ImportService)


# ── Test: Trigger functions ──────────────────────────────────────────────


class TestTriggerFunctions:
    """Test on-demand trigger functions."""

    @pytest.mark.asyncio
    async def test_trigger_import_scan(self) -> None:
        """trigger_import_scan hands work to the durable import runner."""
        mock_runner = AsyncMock()
        with patch("pullbox.tasks.import_task.get_import_runner", return_value=mock_runner):
            trigger_import_scan(42)
            await asyncio.sleep(0)
        mock_runner.request_scan.assert_awaited_once_with(42)

    @pytest.mark.asyncio
    async def test_trigger_import_execute(self) -> None:
        """trigger_import_execute hands work to the durable import runner."""
        mock_runner = AsyncMock()
        with patch("pullbox.tasks.import_task.get_import_runner", return_value=mock_runner):
            trigger_import_execute(42)
            await asyncio.sleep(0)
        mock_runner.request_execute.assert_awaited_once_with(42)
