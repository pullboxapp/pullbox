"""Tests for UT-F.7 — job queue manager.

Verifies state machine transitions, job creation with queue ordering,
startup recovery, job controls (pause/resume/cancel), batch execution
with checkpointing, and edge cases.

Run:
    pytest tests/utilities/test_job_queue.py -v
"""

from __future__ import annotations

import asyncio
import json
import time
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import pytest
from sqlalchemy import select

from pullbox.models.config import SystemConfig
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.operation_progress import OperationProgress, OperationProgressState
from pullbox.models.publisher import Publisher
from pullbox.models.series import Series
from pullbox.utilities.base_executor import (
    ApplyResult,
    ExecutionMode,
    ItemResult,
    JobExecutor,
    JobRunSummary,
    ProcessedItem,
    RuntimeLogEntry,
)
from pullbox.utilities.executors.db_check_cleanup import DBCheckCleanupExecutor
from pullbox.utilities.executors.file_converter import FileConverterExecutor
from pullbox.utilities.executors.integrity_checker import IntegrityCheckerExecutor
from pullbox.utilities.executors.mass_convert_pipeline import MassConvertPipelineExecutor
from pullbox.utilities.executors.mass_rename import MassRenameExecutor
from pullbox.utilities.executors.rollback_executor import RollbackExecutor
from pullbox.utilities.job_queue import VALID_TRANSITIONS, JobQueueManager
from pullbox.utilities.logging_config import configure_utility_logging
from pullbox.utilities.models import (
    ItemState,
    JobState,
    JobType,
    UtilityJob,
    UtilityJobItem,
    UtilityJobLog,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# ── Test Executor (for batch execution tests) ──────────────────


class StubExecutor(JobExecutor):
    """Executor that completes all items for testing."""

    async def generate_items(self, job_config: dict[str, Any]) -> list[dict[str, Any]]:
        count = job_config.get("count", 3)
        return [{"file_path": f"/comics/file_{i}.cbr", "operation": "test"} for i in range(count)]

    def process_item(self, item_data: dict[str, Any], job_config: dict[str, Any]) -> ProcessedItem:
        return ProcessedItem(
            item_id=item_data.get("id", ""),
            result=ItemResult.COMPLETED,
            before_state={"path": item_data.get("file_path", "")},
            after_state={"processed": True},
            duration_ms=10,
            log_entries=[("INFO", f"Processed {item_data.get('file_path', '')}", {})],
        )

    def rollback_item(self, item_data: dict[str, Any], job_config: dict[str, Any]) -> ProcessedItem:
        return ProcessedItem(
            item_id=item_data.get("id", ""),
            result=ItemResult.COMPLETED,
        )


class FailingGenerateExecutor(JobExecutor):
    """Executor whose generate_items raises."""

    async def generate_items(self, job_config: dict[str, Any]) -> list[dict[str, Any]]:
        raise RuntimeError("Cannot discover items")

    def process_item(self, item_data: dict[str, Any], job_config: dict[str, Any]) -> ProcessedItem:
        return ProcessedItem(item_id="", result=ItemResult.COMPLETED)

    def rollback_item(self, item_data: dict[str, Any], job_config: dict[str, Any]) -> ProcessedItem:
        return ProcessedItem(item_id="", result=ItemResult.COMPLETED)


class FailedItemGuidanceExecutor(JobExecutor):
    """Executor that fails an item and emits both failure and remediation logs."""

    async def generate_items(self, job_config: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"file_path": "/comics/bad_file.cbz", "operation": "test"}]

    def process_item(self, item_data: dict[str, Any], job_config: dict[str, Any]) -> ProcessedItem:
        return ProcessedItem(
            item_id=item_data.get("id", ""),
            result=ItemResult.FAILED,
            error_message="Archive integrity check failed",
            duration_ms=12,
            log_entries=[
                ("ERROR", "Archive integrity check failed", {"path": item_data.get("file_path")}),
                (
                    "INFO",
                    "Try converting the archive or replacing the source file before retrying.",
                    {"path": item_data.get("file_path")},
                ),
            ],
        )

    def rollback_item(self, item_data: dict[str, Any], job_config: dict[str, Any]) -> ProcessedItem:
        return ProcessedItem(item_id=item_data.get("id", ""), result=ItemResult.COMPLETED)


class SlowPauseExecutor(JobExecutor):
    """Executor that gives the test enough time to request a pause.

    Uses a longer sleep (0.5s) to ensure the event loop can process
    pause/cancel signals between batch boundaries.
    """

    async def generate_items(self, job_config: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {"file_path": "/comics/file_0.cbr", "operation": "test"},
            {"file_path": "/comics/file_1.cbr", "operation": "test"},
            {"file_path": "/comics/file_2.cbr", "operation": "test"},
            {"file_path": "/comics/file_3.cbr", "operation": "test"},
        ]

    def process_item(self, item_data: dict[str, Any], job_config: dict[str, Any]) -> ProcessedItem:
        time.sleep(0.5)
        return ProcessedItem(
            item_id=item_data.get("id", ""),
            result=ItemResult.COMPLETED,
            duration_ms=150,
            log_entries=[("INFO", f"Processed {item_data.get('file_path', '')}", {})],
        )

    def rollback_item(self, item_data: dict[str, Any], job_config: dict[str, Any]) -> ProcessedItem:
        return ProcessedItem(item_id=item_data.get("id", ""), result=ItemResult.COMPLETED)


class ResumePendingExecutor(JobExecutor):
    """Executor used to verify resumed jobs reuse checkpointed pending items."""

    async def generate_items(self, job_config: dict[str, Any]) -> list[dict[str, Any]]:
        raise AssertionError("generate_items should not be called for resumed jobs")

    def process_item(self, item_data: dict[str, Any], job_config: dict[str, Any]) -> ProcessedItem:
        return ProcessedItem(
            item_id=item_data.get("id", ""),
            result=ItemResult.COMPLETED,
            duration_ms=10,
            log_entries=[("INFO", f"Resumed {item_data.get('file_path', '')}", {})],
        )

    def rollback_item(self, item_data: dict[str, Any], job_config: dict[str, Any]) -> ProcessedItem:
        return ProcessedItem(item_id=item_data.get("id", ""), result=ItemResult.COMPLETED)


class BatchRecordingExecutor(JobExecutor):
    """Executor used to verify worker-pool batching and worker assignment."""

    async def generate_items(self, job_config: dict[str, Any]) -> list[dict[str, Any]]:
        count = job_config.get("count", 5)
        return [{"file_path": f"/comics/batch_{i}.cbz", "operation": "batch"} for i in range(count)]

    def process_item(self, item_data: dict[str, Any], job_config: dict[str, Any]) -> ProcessedItem:
        return ProcessedItem(
            item_id=item_data.get("id", ""),
            result=ItemResult.COMPLETED,
            duration_ms=5,
            log_entries=[("INFO", f"Processed {item_data.get('file_path', '')}", {})],
        )

    def rollback_item(self, item_data: dict[str, Any], job_config: dict[str, Any]) -> ProcessedItem:
        return ProcessedItem(item_id=item_data.get("id", ""), result=ItemResult.COMPLETED)


class AfterCommitPayloadExecutor(JobExecutor):
    """Executor that records payloads received by after_item_commit."""

    execution_mode = ExecutionMode.THREAD
    seen_after_commit_paths: ClassVar[list[str]] = []

    async def generate_items(self, job_config: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {"file_path": "/comics/first.cbz", "operation": "first"},
            {"file_path": "/comics/second.cbz", "operation": "second"},
        ]

    def process_item(self, item_data: dict[str, Any], job_config: dict[str, Any]) -> ProcessedItem:
        return ProcessedItem(
            item_id=item_data.get("id", ""),
            result=ItemResult.COMPLETED,
            duration_ms=1,
        )

    def rollback_item(self, item_data: dict[str, Any], job_config: dict[str, Any]) -> ProcessedItem:
        return ProcessedItem(item_id=item_data.get("id", ""), result=ItemResult.COMPLETED)

    async def after_item_commit(
        self,
        item_data: dict[str, Any],
        processed: ProcessedItem,
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None,
        summary: JobRunSummary,
        apply_result: ApplyResult,
    ) -> list[RuntimeLogEntry]:
        self.seen_after_commit_paths.append(str(item_data.get("file_path")))
        return []


class MixedLevelLogExecutor(JobExecutor):
    """Executor that emits one log entry at each severity."""

    async def generate_items(self, job_config: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"file_path": "/comics/mixed_levels.cbz", "operation": "test"}]

    def process_item(self, item_data: dict[str, Any], job_config: dict[str, Any]) -> ProcessedItem:
        return ProcessedItem(
            item_id=item_data.get("id", ""),
            result=ItemResult.COMPLETED,
            duration_ms=8,
            log_entries=[
                ("DEBUG", "Debug detail", {"step": "debug"}),
                ("INFO", "Info detail", {"step": "info"}),
                ("WARNING", "Warning detail", {"step": "warning"}),
                ("ERROR", "Error detail", {"step": "error"}),
            ],
        )

    def rollback_item(self, item_data: dict[str, Any], job_config: dict[str, Any]) -> ProcessedItem:
        return ProcessedItem(item_id=item_data.get("id", ""), result=ItemResult.COMPLETED)


# ── Helpers ────────────────────────────────────────────────────


def _make_job(
    job_id: str = "job-001",
    state: JobState = JobState.QUEUED,
    job_type: str = JobType.FILE_CONVERT,
    config: str = "{}",
    queue_position: int | None = 0,
) -> UtilityJob:
    return UtilityJob(
        id=job_id,
        job_type=job_type,
        display_name="Test Job",
        state=state,
        config=config,
        total_items=0,
        completed_items=0,
        failed_items=0,
        skipped_items=0,
        warning_count=0,
        queue_position=queue_position,
    )


# ── State Machine: VALID_TRANSITIONS ───────────────────────────


class TestValidTransitions:
    """Verify the state machine transition table."""

    def test_all_states_have_entries(self) -> None:
        for state in JobState:
            assert state in VALID_TRANSITIONS, f"Missing entry for {state}"

    def test_queued_can_transition_to_running(self) -> None:
        assert JobState.RUNNING in VALID_TRANSITIONS[JobState.QUEUED]

    def test_queued_can_transition_to_cancelling(self) -> None:
        assert JobState.CANCELLING in VALID_TRANSITIONS[JobState.QUEUED]

    def test_queued_can_transition_to_failed(self) -> None:
        assert JobState.FAILED in VALID_TRANSITIONS[JobState.QUEUED]

    def test_running_transitions(self) -> None:
        allowed = VALID_TRANSITIONS[JobState.RUNNING]
        assert JobState.COMPLETED in allowed
        assert JobState.PAUSING in allowed
        assert JobState.CANCELLING in allowed
        assert JobState.FAILED in allowed

    def test_pausing_transitions(self) -> None:
        allowed = VALID_TRANSITIONS[JobState.PAUSING]
        assert JobState.PAUSED in allowed
        assert JobState.FAILED in allowed

    def test_paused_transitions(self) -> None:
        allowed = VALID_TRANSITIONS[JobState.PAUSED]
        assert JobState.RUNNING in allowed
        assert JobState.CANCELLING in allowed

    def test_cancelling_transitions(self) -> None:
        allowed = VALID_TRANSITIONS[JobState.CANCELLING]
        assert JobState.CANCELLED in allowed
        assert JobState.FAILED in allowed

    def test_completed_can_rollback(self) -> None:
        assert JobState.ROLLING_BACK in VALID_TRANSITIONS[JobState.COMPLETED]

    def test_cancelled_can_rollback(self) -> None:
        assert JobState.ROLLING_BACK in VALID_TRANSITIONS[JobState.CANCELLED]

    def test_failed_is_terminal(self) -> None:
        assert VALID_TRANSITIONS[JobState.FAILED] == set()

    def test_rolled_back_is_terminal(self) -> None:
        assert VALID_TRANSITIONS[JobState.ROLLED_BACK] == set()

    def test_rolling_back_transitions(self) -> None:
        allowed = VALID_TRANSITIONS[JobState.ROLLING_BACK]
        assert JobState.ROLLED_BACK in allowed
        assert JobState.FAILED in allowed


# ── State Machine: transition() ────────────────────────────────


class TestTransitionMethod:
    """Verify _transition enforces valid transitions and updates timestamps."""

    def _make_manager(self) -> JobQueueManager:
        return JobQueueManager.__new__(JobQueueManager)

    def test_valid_transition_succeeds(self) -> None:
        mgr = self._make_manager()
        job = _make_job(state=JobState.QUEUED)
        mgr.transition(job, JobState.RUNNING)
        assert job.state == JobState.RUNNING

    def test_invalid_transition_raises(self) -> None:
        mgr = self._make_manager()
        job = _make_job(state=JobState.QUEUED)
        with pytest.raises(ValueError, match="Invalid transition"):
            mgr.transition(job, JobState.COMPLETED)

    def test_running_sets_started_at(self) -> None:
        mgr = self._make_manager()
        job = _make_job(state=JobState.QUEUED)
        assert job.started_at is None
        mgr.transition(job, JobState.RUNNING)
        assert job.started_at is not None

    def test_paused_sets_paused_at(self) -> None:
        mgr = self._make_manager()
        job = _make_job(state=JobState.RUNNING)
        mgr.transition(job, JobState.PAUSING)
        mgr.transition(job, JobState.PAUSED)
        assert job.paused_at is not None

    def test_completed_sets_completed_at(self) -> None:
        mgr = self._make_manager()
        job = _make_job(state=JobState.RUNNING)
        mgr.transition(job, JobState.COMPLETED)
        assert job.completed_at is not None

    def test_terminal_state_clears_queue_position(self) -> None:
        mgr = self._make_manager()
        job = _make_job(state=JobState.RUNNING, queue_position=0)
        mgr.transition(job, JobState.COMPLETED)
        assert job.queue_position is None

    def test_failed_terminal_no_further_transitions(self) -> None:
        mgr = self._make_manager()
        job = _make_job(state=JobState.RUNNING)
        mgr.transition(job, JobState.FAILED)
        with pytest.raises(ValueError, match="none \\(terminal\\)"):
            mgr.transition(job, JobState.RUNNING)

    def test_every_valid_transition_from_table(self) -> None:
        """Exhaustively test every valid transition in the table."""
        mgr = self._make_manager()
        for from_state, to_states in VALID_TRANSITIONS.items():
            for to_state in to_states:
                job = _make_job(state=from_state)
                mgr.transition(job, to_state)
                assert job.state == to_state

    def test_every_invalid_transition_from_table(self) -> None:
        """Every state NOT in the allowed set should raise ValueError."""
        mgr = self._make_manager()
        all_states = set(JobState)
        for from_state, allowed in VALID_TRANSITIONS.items():
            invalid = all_states - allowed - {from_state}
            for bad_state in invalid:
                job = _make_job(state=from_state)
                with pytest.raises(ValueError):
                    mgr.transition(job, bad_state)

    def test_cancelling_to_cancelled_sets_completed_at(self) -> None:
        """CANCELLING → CANCELLED must set completed_at timestamp."""
        mgr = self._make_manager()
        job = _make_job(state=JobState.RUNNING)
        mgr.transition(job, JobState.CANCELLING)
        assert job.completed_at is None
        mgr.transition(job, JobState.CANCELLED)
        assert job.completed_at is not None


# ── Job Creation ───────────────────────────────────────────────


class TestJobCreation:
    """Verify job creation with proper defaults and queue ordering."""

    @pytest.mark.asyncio
    async def test_create_job_queued_state(self, db_session: AsyncSession) -> None:
        mgr = JobQueueManager(session_factory=None)
        job = await mgr.create_job(
            session=db_session,
            job_type=JobType.FILE_CONVERT,
            display_name="Convert files",
            config={"target_format": "cbz"},
        )
        assert job.state == JobState.QUEUED
        assert job.job_type == JobType.FILE_CONVERT
        assert job.display_name == "Convert files"
        assert json.loads(job.config)["target_format"] == "cbz"

    @pytest.mark.asyncio
    async def test_create_job_assigns_queue_position(self, db_session: AsyncSession) -> None:
        mgr = JobQueueManager(session_factory=None)
        job1 = await mgr.create_job(
            session=db_session,
            job_type=JobType.FILE_CONVERT,
            display_name="Job 1",
            config={},
        )
        job2 = await mgr.create_job(
            session=db_session,
            job_type=JobType.MASS_RENAME,
            display_name="Job 2",
            config={},
        )
        assert job1.queue_position == 0
        assert job2.queue_position == 1

    @pytest.mark.asyncio
    async def test_create_job_with_created_by(self, db_session: AsyncSession) -> None:
        mgr = JobQueueManager(session_factory=None)
        job = await mgr.create_job(
            session=db_session,
            job_type=JobType.EXPORT_LIBRARY,
            display_name="Export",
            config={},
            created_by="admin",
        )
        assert job.created_by == "admin"

    @pytest.mark.asyncio
    async def test_create_job_initializes_counters(self, db_session: AsyncSession) -> None:
        mgr = JobQueueManager(session_factory=None)
        job = await mgr.create_job(
            session=db_session,
            job_type=JobType.FILE_CONVERT,
            display_name="Test",
            config={},
        )
        assert job.completed_items == 0
        assert job.failed_items == 0
        assert job.skipped_items == 0
        assert job.warning_count == 0

    @pytest.mark.asyncio
    async def test_create_job_projects_queued_shared_activity(
        self,
        db_session: AsyncSession,
    ) -> None:
        mgr = JobQueueManager(session_factory=None)
        job = await mgr.create_job(
            session=db_session,
            job_type=JobType.MASS_RENAME,
            display_name="Rename files",
            config={},
            created_by="admin",
        )

        result = await db_session.execute(
            select(OperationProgress).where(OperationProgress.operation_key == job.id)
        )
        operation = result.scalar_one()

        assert operation.state is OperationProgressState.QUEUED
        assert operation.title == "Rename files"


# ── Queue Ordering ─────────────────────────────────────────────


class TestQueueOrdering:
    """Verify FIFO ordering by queue_position."""

    @pytest.mark.asyncio
    async def test_fifo_order(self, db_session: AsyncSession) -> None:
        mgr = JobQueueManager(session_factory=None)
        jobs = []
        for i in range(3):
            job = await mgr.create_job(
                session=db_session,
                job_type=JobType.FILE_CONVERT,
                display_name=f"Job {i}",
                config={},
            )
            jobs.append(job)

        result = await db_session.execute(
            select(UtilityJob)
            .where(UtilityJob.state == JobState.QUEUED)
            .order_by(UtilityJob.queue_position)
        )
        queued = list(result.scalars().all())
        assert [j.display_name for j in queued] == ["Job 0", "Job 1", "Job 2"]


# ── Startup Recovery ───────────────────────────────────────────


class TestStartupRecovery:
    """Verify interrupted jobs are recovered on startup."""

    @pytest.mark.asyncio
    async def test_running_job_transitions_to_paused(self, db_session: AsyncSession) -> None:
        job = _make_job(job_id="run-1", state=JobState.RUNNING)
        db_session.add(job)
        await db_session.flush()

        mgr = JobQueueManager(session_factory=None)
        await mgr.recover_interrupted_jobs(db_session)

        await db_session.refresh(job)
        assert job.state == JobState.PAUSED

    @pytest.mark.asyncio
    async def test_pausing_job_transitions_to_paused(self, db_session: AsyncSession) -> None:
        job = _make_job(job_id="pause-1", state=JobState.PAUSING)
        db_session.add(job)
        await db_session.flush()

        mgr = JobQueueManager(session_factory=None)
        await mgr.recover_interrupted_jobs(db_session)

        await db_session.refresh(job)
        assert job.state == JobState.PAUSED

    @pytest.mark.asyncio
    async def test_in_progress_items_reset_to_pending(self, db_session: AsyncSession) -> None:
        job = _make_job(job_id="run-2", state=JobState.RUNNING)
        item = UtilityJobItem(
            id="item-ip",
            job_id="run-2",
            item_index=0,
            state=ItemState.IN_PROGRESS,
            operation="test",
        )
        db_session.add(job)
        db_session.add(item)
        await db_session.flush()

        mgr = JobQueueManager(session_factory=None)
        await mgr.recover_interrupted_jobs(db_session)

        await db_session.refresh(item)
        assert item.state == ItemState.PENDING

    @pytest.mark.asyncio
    async def test_multiple_interrupted_jobs(self, db_session: AsyncSession) -> None:
        for i in range(3):
            db_session.add(_make_job(job_id=f"int-{i}", state=JobState.RUNNING))
        await db_session.flush()

        mgr = JobQueueManager(session_factory=None)
        await mgr.recover_interrupted_jobs(db_session)

        result = await db_session.execute(
            select(UtilityJob).where(UtilityJob.state == JobState.PAUSED)
        )
        assert len(list(result.scalars().all())) == 3

    @pytest.mark.asyncio
    async def test_completed_jobs_unaffected(self, db_session: AsyncSession) -> None:
        job = _make_job(job_id="done-1", state=JobState.COMPLETED)
        db_session.add(job)
        await db_session.flush()

        mgr = JobQueueManager(session_factory=None)
        await mgr.recover_interrupted_jobs(db_session)

        await db_session.refresh(job)
        assert job.state == JobState.COMPLETED


# ── Job Controls ───────────────────────────────────────────────


class TestJobControls:
    """Verify pause, resume, and cancel operations."""

    @pytest.mark.asyncio
    async def test_pause_running_job(self, db_session: AsyncSession) -> None:
        job = _make_job(job_id="ctrl-p", state=JobState.RUNNING)
        db_session.add(job)
        await db_session.flush()

        mgr = JobQueueManager(session_factory=None)
        await mgr.pause_job(db_session, "ctrl-p")

        await db_session.refresh(job)
        assert job.state == JobState.PAUSING

    @pytest.mark.asyncio
    async def test_pause_non_running_raises(self, db_session: AsyncSession) -> None:
        job = _make_job(job_id="ctrl-pq", state=JobState.QUEUED)
        db_session.add(job)
        await db_session.flush()

        mgr = JobQueueManager(session_factory=None)
        with pytest.raises(ValueError, match="Invalid transition"):
            await mgr.pause_job(db_session, "ctrl-pq")

    @pytest.mark.asyncio
    async def test_pause_already_paused_raises(self, db_session: AsyncSession) -> None:
        job = _make_job(job_id="ctrl-pp", state=JobState.PAUSED)
        db_session.add(job)
        await db_session.flush()

        mgr = JobQueueManager(session_factory=None)
        with pytest.raises(ValueError, match="Invalid transition"):
            await mgr.pause_job(db_session, "ctrl-pp")

    @pytest.mark.asyncio
    async def test_resume_paused_job(self, db_session: AsyncSession) -> None:
        job = _make_job(job_id="ctrl-r", state=JobState.PAUSED, queue_position=5)
        db_session.add(job)
        await db_session.flush()

        mgr = JobQueueManager(session_factory=None)
        await mgr.resume_job(db_session, "ctrl-r")

        await db_session.refresh(job)
        assert job.state == JobState.QUEUED
        assert job.queue_position == 0

    @pytest.mark.asyncio
    async def test_resume_non_paused_raises(self, db_session: AsyncSession) -> None:
        job = _make_job(job_id="ctrl-rr", state=JobState.RUNNING)
        db_session.add(job)
        await db_session.flush()

        mgr = JobQueueManager(session_factory=None)
        with pytest.raises(ValueError, match="Can only resume"):
            await mgr.resume_job(db_session, "ctrl-rr")

    @pytest.mark.asyncio
    async def test_cancel_running_job(self, db_session: AsyncSession) -> None:
        job = _make_job(job_id="ctrl-c", state=JobState.RUNNING)
        db_session.add(job)
        await db_session.flush()

        mgr = JobQueueManager(session_factory=None)
        await mgr.cancel_job(db_session, "ctrl-c")

        await db_session.refresh(job)
        assert job.state == JobState.CANCELLING

    @pytest.mark.asyncio
    async def test_cancel_queued_job(self, db_session: AsyncSession) -> None:
        """Cancelling a QUEUED job goes directly to CANCELLED."""
        job = _make_job(job_id="ctrl-cq", state=JobState.QUEUED)
        db_session.add(job)
        await db_session.flush()

        mgr = JobQueueManager(session_factory=None)
        await mgr.cancel_job(db_session, "ctrl-cq")

        await db_session.refresh(job)
        assert job.state == JobState.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_paused_job(self, db_session: AsyncSession) -> None:
        job = _make_job(job_id="ctrl-cp", state=JobState.PAUSED)
        db_session.add(job)
        await db_session.flush()

        mgr = JobQueueManager(session_factory=None)
        await mgr.cancel_job(db_session, "ctrl-cp")

        await db_session.refresh(job)
        assert job.state == JobState.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_completed_raises(self, db_session: AsyncSession) -> None:
        job = _make_job(job_id="ctrl-cd", state=JobState.COMPLETED, queue_position=None)
        db_session.add(job)
        await db_session.flush()

        mgr = JobQueueManager(session_factory=None)
        with pytest.raises(ValueError, match="Cannot cancel"):
            await mgr.cancel_job(db_session, "ctrl-cd")

    @pytest.mark.asyncio
    async def test_cancel_with_rollback_creates_rollback_job(
        self, db_session: AsyncSession
    ) -> None:
        """Cancel with rollback=True creates a new rollback job."""
        job = _make_job(job_id="ctrl-cr", state=JobState.RUNNING)
        db_session.add(job)
        await db_session.flush()

        mgr = JobQueueManager(session_factory=None)
        await mgr.cancel_job(db_session, "ctrl-cr", rollback=True)

        result = await db_session.execute(
            select(UtilityJob).where(UtilityJob.job_type == JobType.ROLLBACK)
        )
        rollback_job = result.scalar_one()
        assert rollback_job.parent_job_id == "ctrl-cr"
        assert rollback_job.state == JobState.QUEUED
        assert json.loads(rollback_job.config)["parent_job_id"] == "ctrl-cr"

    @pytest.mark.asyncio
    async def test_queue_rollback_for_completed_job_creates_rollback_job(
        self,
        db_session: AsyncSession,
    ) -> None:
        job = _make_job(job_id="ctrl-rb", state=JobState.COMPLETED, queue_position=None)
        db_session.add(job)
        await db_session.flush()

        mgr = JobQueueManager(session_factory=None)
        rollback_job = await mgr.queue_rollback_job(db_session, "ctrl-rb", created_by="admin")

        await db_session.refresh(rollback_job)
        assert rollback_job.job_type == JobType.ROLLBACK
        assert rollback_job.parent_job_id == "ctrl-rb"
        assert rollback_job.state == JobState.QUEUED
        assert rollback_job.created_by == "admin"
        assert json.loads(rollback_job.config)["parent_job_id"] == "ctrl-rb"

    @pytest.mark.asyncio
    async def test_queue_rollback_rejects_duplicate_completed_job(
        self,
        db_session: AsyncSession,
    ) -> None:
        job = _make_job(job_id="ctrl-rb-dup", state=JobState.COMPLETED, queue_position=None)
        rollback_job = _make_job(
            job_id="ctrl-rb-dup-child",
            state=JobState.QUEUED,
            job_type=JobType.ROLLBACK,
            config=json.dumps({"parent_job_id": "ctrl-rb-dup"}),
            queue_position=0,
        )
        rollback_job.parent_job_id = "ctrl-rb-dup"
        db_session.add_all([job, rollback_job])
        await db_session.flush()

        mgr = JobQueueManager(session_factory=None)
        with pytest.raises(ValueError, match="Rollback already queued"):
            await mgr.queue_rollback_job(db_session, "ctrl-rb-dup")

    @pytest.mark.asyncio
    async def test_dispatch_completed_rollback_marks_parent_job_rolled_back(
        self,
        db_session: AsyncSession,
        session_factory,
    ) -> None:
        parent = _make_job(
            job_id="parent-rb",
            state=JobState.COMPLETED,
            job_type=JobType.FILE_CONVERT,
            queue_position=None,
        )
        db_session.add(parent)
        await db_session.flush()

        db_session.add(
            UtilityJobItem(
                id="parent-rb-item",
                job_id=parent.id,
                item_index=0,
                state=ItemState.COMPLETED,
                file_path="/comics/file_0.cbr",
                operation="convert",
                before_state=json.dumps({"path": "/comics/file_0.cbr"}),
                after_state=json.dumps({"path": "/comics/file_0.cbz"}),
            )
        )
        await db_session.flush()

        mgr = JobQueueManager(session_factory=session_factory)
        mgr.register_executor(JobType.FILE_CONVERT, StubExecutor)
        mgr.register_executor(JobType.ROLLBACK, RollbackExecutor)

        rollback_job = await mgr.queue_rollback_job(db_session, parent.id, created_by="admin")

        await mgr.dispatch_next()

        await db_session.refresh(parent)
        await db_session.refresh(rollback_job)
        assert parent.state == JobState.ROLLED_BACK
        assert rollback_job.state == JobState.COMPLETED

    @pytest.mark.asyncio
    async def test_dispatch_failed_rollback_keeps_parent_completed(
        self,
        db_session: AsyncSession,
        session_factory,
        tmp_path: Path,
    ) -> None:
        parent = _make_job(
            job_id="parent-rb-fail",
            state=JobState.COMPLETED,
            job_type=JobType.FILE_CONVERT,
            queue_position=None,
        )
        db_session.add(parent)
        await db_session.flush()

        converted = tmp_path / "comics" / "test.cbz"
        converted.parent.mkdir(parents=True)
        converted.write_text("converted")
        missing_trash = tmp_path / ".trash" / "test.cb7"

        db_session.add(
            UtilityJobItem(
                id="parent-rb-fail-item",
                job_id=parent.id,
                item_index=0,
                state=ItemState.COMPLETED,
                file_path=str(converted),
                operation="convert",
                before_state=json.dumps({"path": str(tmp_path / "comics" / "test.cb7")}),
                after_state=json.dumps(
                    {
                        "path": str(converted),
                        "original_path": str(missing_trash),
                    }
                ),
            )
        )
        await db_session.flush()

        mgr = JobQueueManager(session_factory=session_factory)
        mgr.register_executor(JobType.FILE_CONVERT, FileConverterExecutor)
        mgr.register_executor(JobType.ROLLBACK, RollbackExecutor)

        rollback_job = await mgr.queue_rollback_job(db_session, parent.id, created_by="admin")

        await mgr.dispatch_next()

        await db_session.refresh(parent)
        await db_session.refresh(rollback_job)
        assert parent.state == JobState.COMPLETED
        assert rollback_job.state == JobState.FAILED


# ── Executor Registry ──────────────────────────────────────────


class TestExecutorRegistry:
    """Verify executor registration and lookup."""

    def test_register_executor(self) -> None:
        mgr = JobQueueManager(session_factory=None)
        mgr.register_executor(JobType.FILE_CONVERT, StubExecutor)
        assert mgr.get_executor(JobType.FILE_CONVERT) is not None

    def test_unknown_executor_returns_none(self) -> None:
        mgr = JobQueueManager(session_factory=None)
        assert mgr.get_executor("nonexistent_type") is None


def _create_valid_archive(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("page_000.jpg", b"\xff\xd8" + b"X" * 500)
    return path


class TestMassConvertQueueIntegration:
    """Verify queue-side effects for the mass convert pipeline."""

    @pytest.mark.asyncio
    async def test_dispatch_updates_tracked_library_file_after_mass_convert(
        self,
        db_session: AsyncSession,
        session_factory,
        tmp_path,
    ) -> None:
        library_root = tmp_path / "library"
        series_dir = library_root / "Batman (2016)"
        series_dir.mkdir(parents=True)
        source = series_dir / "Batman 001.cbr"
        with zipfile.ZipFile(source, "w") as zf:
            zf.writestr("page_000.jpg", b"\xff\xd8" + b"X" * 500)

        publisher = Publisher(name="DC")
        root = LibraryRoot(name="Library", path=str(library_root), enabled=True)
        series = Series(
            title="Batman",
            sort_title="Batman",
            year_start=2016,
            path=str(series_dir),
            publisher=publisher,
            library_root=root,
            monitored=True,
        )
        issue = Issue(
            series=series,
            issue_number=1.0,
            title="Issue Title",
            release_date=date(2016, 1, 1),
        )
        library_file = LibraryFile(
            file_path=str(source),
            file_name=source.name,
            file_size=source.stat().st_size,
            file_format=FileFormat.CBR,
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.HIGH,
            issue=issue,
            library_root=root,
        )
        db_session.add(library_file)
        await db_session.flush()

        mgr = JobQueueManager(session_factory=session_factory)
        # Pass session_factory so executor can query library data during generate_items
        mgr.register_executor(JobType.MASS_CONVERT_PIPELINE, MassConvertPipelineExecutor)

        trash_dir = library_root / ".trash"
        job = await mgr.create_job(
            session=db_session,
            job_type=JobType.MASS_CONVERT_PIPELINE,
            display_name="Mass Convert",
            config={
                "steps": [1],
                "scope": "manual",
                "file_paths": [str(source)],
                "trash_folder": str(trash_dir),
            },
        )

        await mgr.dispatch_next()

        await db_session.refresh(job)
        await db_session.refresh(library_file)
        assert job.state == JobState.COMPLETED
        assert library_file.file_path.endswith(".cbz")
        assert library_file.file_name == "Batman 001.cbz"
        assert library_file.file_format == FileFormat.CBZ
        assert Path(library_file.file_path).exists()
        assert (trash_dir / source.name).exists()

        log_result = await db_session.execute(
            select(UtilityJobLog).where(UtilityJobLog.job_id == job.id).order_by(UtilityJobLog.id)
        )
        messages = [log.message for log in log_result.scalars().all()]
        assert any("updated library record" in message.lower() for message in messages)


class TestIntegrityQueueIntegration:
    """Verify queue-side effects for integrity checks."""

    @pytest.mark.asyncio
    async def test_dispatch_updates_issue_integrity_for_healthy_tracked_file(
        self,
        db_session: AsyncSession,
        session_factory,
        tmp_path: Path,
    ) -> None:
        library_root = tmp_path / "library"
        series_dir = library_root / "Batman (2016)"
        source = _create_valid_archive(series_dir / "Batman 001.cbz")

        publisher = Publisher(name="DC")
        root = LibraryRoot(name="Library", path=str(library_root), enabled=True)
        series = Series(
            title="Batman",
            sort_title="Batman",
            year_start=2016,
            path=str(series_dir),
            publisher=publisher,
            library_root=root,
            monitored=True,
        )
        issue = Issue(
            series=series,
            issue_number=1.0,
            title="Batman #1",
            status=IssueStatus.OWNED,
        )
        library_file = LibraryFile(
            file_path=str(source),
            file_name=source.name,
            file_size=source.stat().st_size,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.HIGH,
            issue=issue,
            library_root=root,
        )
        db_session.add(library_file)
        await db_session.flush()

        mgr = JobQueueManager(session_factory=session_factory)
        mgr.register_executor(JobType.INTEGRITY_CHECK, IntegrityCheckerExecutor)

        job = await mgr.create_job(
            session=db_session,
            job_type=JobType.INTEGRITY_CHECK,
            display_name="Integrity Check",
            config={
                "scan_depth": "quick",
                "scope": "manual",
                "file_paths": [str(source)],
                "corrupt_action": "report",
                "requeue_search": False,
            },
        )

        await mgr.dispatch_next()

        await db_session.refresh(job)
        await db_session.refresh(issue)
        await db_session.refresh(library_file)
        assert job.state == JobState.COMPLETED
        assert issue.integrity_status == "healthy"
        assert issue.integrity_checked_at is not None
        assert library_file.file_hash is not None
        assert json.loads(issue.integrity_details or "{}")["page_count"] == 1

    @pytest.mark.asyncio
    async def test_dispatch_quarantines_corrupt_tracked_file_and_requeues_search(
        self,
        db_session: AsyncSession,
        session_factory,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        library_root = tmp_path / "library"
        series_dir = library_root / "Saga (2012)"
        series_dir.mkdir(parents=True)
        source = series_dir / "Saga 001.cbz"
        source.write_bytes(b"GARBAGE")
        trash_dir = library_root / ".trash"

        publisher = Publisher(name="Image")
        root = LibraryRoot(name="Library", path=str(library_root), enabled=True)
        series = Series(
            title="Saga",
            sort_title="Saga",
            year_start=2012,
            path=str(series_dir),
            publisher=publisher,
            library_root=root,
            monitored=True,
        )
        issue = Issue(
            series=series,
            issue_number=1.0,
            title="Saga #1",
            status=IssueStatus.OWNED,
        )
        library_file = LibraryFile(
            file_path=str(source),
            file_name=source.name,
            file_size=source.stat().st_size,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.HIGH,
            issue=issue,
            library_root=root,
        )
        db_session.add(library_file)
        await db_session.flush()

        searched_series_ids: list[int] = []

        async def _fake_search(series_id: int) -> dict[str, int]:
            searched_series_ids.append(series_id)
            return {"wanted": 1, "sent": 0, "queued": 0}

        monkeypatch.setattr("pullbox.tasks.search_task.search_series_issues", _fake_search)

        mgr = JobQueueManager(session_factory=session_factory)
        mgr.register_executor(JobType.INTEGRITY_CHECK, IntegrityCheckerExecutor)

        job = await mgr.create_job(
            session=db_session,
            job_type=JobType.INTEGRITY_CHECK,
            display_name="Integrity Check",
            config={
                "scan_depth": "quick",
                "scope": "manual",
                "file_paths": [str(source)],
                "corrupt_action": "quarantine",
                "trash_folder": str(trash_dir),
                "requeue_search": True,
            },
        )

        await mgr.dispatch_next()

        await db_session.refresh(job)
        await db_session.refresh(issue)
        assert job.state == JobState.COMPLETED
        assert issue.status == IssueStatus.WANTED
        assert issue.integrity_status == "corrupt"
        assert searched_series_ids == [series.id]
        assert not source.exists()
        assert list(trash_dir.rglob(source.name))
        integrity_details = json.loads(issue.integrity_details or "{}")

        library_file_id = library_file.id
        db_session.expire_all()
        orphaned = await db_session.get(LibraryFile, library_file_id)
        assert orphaned is None
        assert integrity_details["action"] == "quarantine"

    @pytest.mark.asyncio
    async def test_rollback_restores_quarantined_integrity_item_state(
        self,
        db_session: AsyncSession,
        session_factory,
        tmp_path: Path,
    ) -> None:
        library_root = tmp_path / "library"
        series_dir = library_root / "Planetary (1999)"
        series_dir.mkdir(parents=True)
        source = series_dir / "Planetary 001.cbz"
        original_bytes = b"GARBAGE"
        source.write_bytes(original_bytes)
        trash_dir = library_root / ".trash"

        publisher = Publisher(name="WildStorm")
        root = LibraryRoot(name="Library", path=str(library_root), enabled=True)
        series = Series(
            title="Planetary",
            sort_title="Planetary",
            year_start=1999,
            path=str(series_dir),
            publisher=publisher,
            library_root=root,
            monitored=True,
        )
        issue = Issue(
            series=series,
            issue_number=1.0,
            title="Planetary #1",
            status=IssueStatus.OWNED,
        )
        library_file = LibraryFile(
            file_path=str(source),
            file_name=source.name,
            file_size=source.stat().st_size,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.HIGH,
            issue=issue,
            library_root=root,
        )
        db_session.add(library_file)
        await db_session.flush()

        mgr = JobQueueManager(session_factory=session_factory)
        mgr.register_executor(JobType.INTEGRITY_CHECK, IntegrityCheckerExecutor)
        mgr.register_executor(JobType.ROLLBACK, RollbackExecutor)

        job = await mgr.create_job(
            session=db_session,
            job_type=JobType.INTEGRITY_CHECK,
            display_name="Integrity Check",
            config={
                "scan_depth": "quick",
                "scope": "manual",
                "file_paths": [str(source)],
                "corrupt_action": "quarantine",
                "trash_folder": str(trash_dir),
                "requeue_search": False,
            },
        )

        await mgr.dispatch_next()
        await db_session.refresh(job)
        rollback_job = await mgr.queue_rollback_job(db_session, job.id, created_by="admin")
        await mgr.dispatch_next()

        await db_session.refresh(job)
        await db_session.refresh(rollback_job)
        await db_session.refresh(issue)
        assert job.state == JobState.ROLLED_BACK
        assert rollback_job.state == JobState.COMPLETED
        assert source.exists()
        assert source.read_bytes() == original_bytes
        assert issue.status == IssueStatus.OWNED
        assert issue.integrity_status == "unchecked"
        assert issue.integrity_checked_at is None
        assert issue.integrity_details == "{}"

        restored_file = await db_session.get(LibraryFile, library_file.id)
        assert restored_file is not None
        assert restored_file.file_path == str(source)
        assert restored_file.file_name == source.name


# ── Edge Cases ─────────────────────────────────────────────────


class TestQueueContinuationEdgeCases:
    """Verify queue draining and resumed ordering edge cases."""

    @pytest.mark.asyncio
    async def test_resume_places_at_position_zero(self, db_session: AsyncSession) -> None:
        """Resumed job gets queue_position=0 to run ahead of others."""
        mgr = JobQueueManager(session_factory=None)
        # Create two queued jobs first
        await mgr.create_job(db_session, JobType.FILE_CONVERT, "Queued 1", {})
        await mgr.create_job(db_session, JobType.FILE_CONVERT, "Queued 2", {})

        # Create and resume a paused job
        paused = _make_job(job_id="resumed", state=JobState.PAUSED, queue_position=99)
        db_session.add(paused)
        await db_session.flush()

        await mgr.resume_job(db_session, "resumed")

        await db_session.refresh(paused)
        assert paused.queue_position == 0

    @pytest.mark.asyncio
    async def test_dispatch_transitions_pausing_job_to_paused(
        self,
        db_session: AsyncSession,
        session_factory,
    ) -> None:
        db_session.add(SystemConfig(key="utility_worker_count", value="1", value_type="int"))
        await db_session.flush()

        mgr = JobQueueManager(session_factory=session_factory)
        mgr.register_executor(JobType.FILE_CONVERT, SlowPauseExecutor)

        job = await mgr.create_job(
            session=db_session,
            job_type=JobType.FILE_CONVERT,
            display_name="Pause During Dispatch",
            config={},
        )

        dispatch_task = asyncio.create_task(mgr.dispatch_next())
        # Wait until job starts running
        for _ in range(50):
            await asyncio.sleep(0.05)
            await db_session.refresh(job)
            if job.state == JobState.RUNNING:
                break
        assert job.state == JobState.RUNNING
        # Wait for at least one item to start processing
        await asyncio.sleep(0.3)
        await mgr.pause_job(db_session, job.id)
        await dispatch_task

        await db_session.refresh(job)
        assert job.state == JobState.PAUSED
        assert job.total_items == 4
        assert (job.completed_items or 0) < job.total_items

        result = await db_session.execute(
            select(UtilityJobItem)
            .where(UtilityJobItem.job_id == job.id)
            .order_by(UtilityJobItem.item_index)
        )
        items = list(result.scalars().all())
        assert any(item.state == ItemState.PENDING for item in items)

    @pytest.mark.asyncio
    async def test_dispatch_reuses_pending_items_for_resumed_jobs(
        self,
        db_session: AsyncSession,
        session_factory,
    ) -> None:
        mgr = JobQueueManager(session_factory=session_factory)
        mgr.register_executor(JobType.FILE_CONVERT, ResumePendingExecutor)

        job = UtilityJob(
            id="resume-existing",
            job_type=JobType.FILE_CONVERT,
            display_name="Resume Existing",
            state=JobState.QUEUED,
            config="{}",
            total_items=2,
            completed_items=1,
            failed_items=0,
            skipped_items=0,
            warning_count=0,
            queue_position=0,
        )
        db_session.add(job)
        db_session.add(
            UtilityJobItem(
                id="resume-done",
                job_id=job.id,
                item_index=0,
                state=ItemState.COMPLETED,
                file_path="/comics/already_done.cbr",
                operation="test",
                before_state='{"file_path":"/comics/already_done.cbr","operation":"test"}',
            )
        )
        db_session.add(
            UtilityJobItem(
                id="resume-pending",
                job_id=job.id,
                item_index=1,
                state=ItemState.PENDING,
                file_path="/comics/still_pending.cbr",
                operation="test",
                before_state='{"file_path":"/comics/still_pending.cbr","operation":"test"}',
            )
        )
        await db_session.flush()

        await mgr.dispatch_next()

        await db_session.refresh(job)
        assert job.state == JobState.COMPLETED
        assert job.completed_items == 2
        assert job.total_items == 2

        pending_item = await db_session.get(UtilityJobItem, "resume-pending")
        assert pending_item is not None
        assert pending_item.state == ItemState.COMPLETED

    @pytest.mark.asyncio
    async def test_dispatch_drains_into_next_queued_job_after_completion(
        self,
        db_session: AsyncSession,
        session_factory,
    ) -> None:
        mgr = JobQueueManager(session_factory=session_factory)
        mgr.register_executor(JobType.FILE_CONVERT, StubExecutor)

        first = await mgr.create_job(
            session=db_session,
            job_type=JobType.FILE_CONVERT,
            display_name="First Job",
            config={"count": 1},
        )
        second = await mgr.create_job(
            session=db_session,
            job_type=JobType.FILE_CONVERT,
            display_name="Second Job",
            config={"count": 1},
        )

        await mgr.dispatch_next()

        await db_session.refresh(first)
        await db_session.refresh(second)
        assert first.state == JobState.COMPLETED
        assert second.state == JobState.COMPLETED


class TestMassRenamePathSync:
    """Verify tracked DB paths stay in sync with Mass Rename operations."""

    @pytest.mark.asyncio
    async def test_apply_mass_rename_updates_library_file_record(
        self,
        db_session: AsyncSession,
        tmp_path: Path,
    ) -> None:
        library_root_path = tmp_path / "library"
        series_path = library_root_path / "Batman (2016)"
        series_path.mkdir(parents=True)
        original_path = series_path / "batman-001.cbz"
        renamed_path = series_path / "Batman (2016) #001.cbz"
        renamed_path.write_text("comic")

        publisher = Publisher(name="DC Comics")
        root = LibraryRoot(name="Library", path=str(library_root_path), enabled=True)
        db_session.add_all([publisher, root])
        await db_session.flush()

        series = Series(
            title="Batman",
            sort_title="Batman",
            year_start=2016,
            publisher_id=publisher.id,
            library_root_id=root.id,
            path=str(series_path),
        )
        db_session.add(series)
        await db_session.flush()

        issue = Issue(series_id=series.id, issue_number=1.0, title="Batman #1")
        db_session.add(issue)
        await db_session.flush()

        library_file = LibraryFile(
            issue_id=issue.id,
            library_root_id=root.id,
            file_path=str(original_path),
            file_name=original_path.name,
            file_size=10,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(tz=UTC),
            match_confidence=MatchConfidence.HIGH,
        )
        db_session.add(library_file)
        await db_session.flush()

        executor = MassRenameExecutor()
        update = await executor.apply_item_result(
            db_session,
            None,
            {"file_path": str(original_path)},
            ProcessedItem(
                item_id="rename-file",
                result=ItemResult.COMPLETED,
                after_state={"path": str(renamed_path)},
            ),
            {"target": "files"},
            None,
            JobRunSummary(),
        )

        assert update.extra_logs
        assert library_file.file_path == str(renamed_path)
        assert library_file.file_name == renamed_path.name

    @pytest.mark.asyncio
    async def test_apply_mass_rename_updates_series_and_descendant_file_paths(
        self,
        db_session: AsyncSession,
        tmp_path: Path,
    ) -> None:
        library_root_path = tmp_path / "library"
        original_series_path = library_root_path / "batman-folder"
        renamed_series_path = library_root_path / "Batman (2016)"
        original_series_path.mkdir(parents=True)

        publisher = Publisher(name="DC Comics")
        root = LibraryRoot(name="Library", path=str(library_root_path), enabled=True)
        db_session.add_all([publisher, root])
        await db_session.flush()

        series = Series(
            title="Batman",
            sort_title="Batman",
            year_start=2016,
            publisher_id=publisher.id,
            library_root_id=root.id,
            path=str(original_series_path),
        )
        db_session.add(series)
        await db_session.flush()

        issue = Issue(series_id=series.id, issue_number=1.0, title="Batman #1")
        db_session.add(issue)
        await db_session.flush()

        nested_original = original_series_path / "Batman 001.cbz"
        library_file = LibraryFile(
            issue_id=issue.id,
            library_root_id=root.id,
            file_path=str(nested_original),
            file_name=nested_original.name,
            file_size=10,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(tz=UTC),
            match_confidence=MatchConfidence.HIGH,
        )
        db_session.add(library_file)
        await db_session.flush()

        executor = MassRenameExecutor()
        update = await executor.apply_item_result(
            db_session,
            None,
            {"file_path": str(original_series_path)},
            ProcessedItem(
                item_id="rename-folder",
                result=ItemResult.COMPLETED,
                after_state={"path": str(renamed_series_path)},
            ),
            {"target": "folders"},
            None,
            JobRunSummary(),
        )

        assert update.extra_logs
        assert series.path == str(renamed_series_path)
        assert library_file.file_path == str(renamed_series_path / "Batman 001.cbz")
        assert library_file.file_name == "Batman 001.cbz"


class TestEdgeCases:
    """Verify edge case handling."""

    @pytest.mark.asyncio
    async def test_dispatch_drains_into_next_queued_job_after_pause(
        self,
        db_session: AsyncSession,
        session_factory,
    ) -> None:
        db_session.add(SystemConfig(key="utility_worker_count", value="1", value_type="int"))
        await db_session.flush()

        mgr = JobQueueManager(session_factory=session_factory)
        mgr.register_executor(JobType.FILE_CONVERT, SlowPauseExecutor)
        mgr.register_executor(JobType.MASS_RENAME, StubExecutor)

        first = await mgr.create_job(
            session=db_session,
            job_type=JobType.FILE_CONVERT,
            display_name="Pause During Dispatch",
            config={},
        )
        second = await mgr.create_job(
            session=db_session,
            job_type=JobType.MASS_RENAME,
            display_name="Queued Behind Pause",
            config={},
        )

        dispatch_task = asyncio.create_task(mgr.dispatch_next())
        for _ in range(50):
            await asyncio.sleep(0.05)
            await db_session.refresh(first)
            if first.state == JobState.RUNNING:
                break
        assert first.state == JobState.RUNNING
        await asyncio.sleep(0.3)

        await mgr.pause_job(db_session, first.id)
        await dispatch_task

        await db_session.refresh(first)
        await db_session.refresh(second)
        assert first.state == JobState.PAUSED
        assert second.state == JobState.COMPLETED

    @pytest.mark.asyncio
    async def test_dispatch_drains_into_next_queued_job_after_cancel(
        self,
        db_session: AsyncSession,
        session_factory,
    ) -> None:
        db_session.add(SystemConfig(key="utility_worker_count", value="1", value_type="int"))
        await db_session.flush()

        mgr = JobQueueManager(session_factory=session_factory)
        mgr.register_executor(JobType.FILE_CONVERT, SlowPauseExecutor)
        mgr.register_executor(JobType.MASS_RENAME, StubExecutor)

        first = await mgr.create_job(
            session=db_session,
            job_type=JobType.FILE_CONVERT,
            display_name="Cancel During Dispatch",
            config={},
        )
        second = await mgr.create_job(
            session=db_session,
            job_type=JobType.MASS_RENAME,
            display_name="Queued Behind Cancel",
            config={},
        )

        dispatch_task = asyncio.create_task(mgr.dispatch_next())
        for _ in range(50):
            await asyncio.sleep(0.05)
            await db_session.refresh(first)
            if first.state == JobState.RUNNING:
                break
        assert first.state == JobState.RUNNING
        await asyncio.sleep(0.3)

        await mgr.cancel_job(db_session, first.id)
        await dispatch_task

        await db_session.refresh(first)
        await db_session.refresh(second)
        assert first.state == JobState.CANCELLED
        assert second.state == JobState.COMPLETED

    @pytest.mark.asyncio
    async def test_resume_preserves_existing_resumed_priority_order(
        self,
        db_session: AsyncSession,
        session_factory,
    ) -> None:
        mgr = JobQueueManager(session_factory=session_factory)

        already_resumed = UtilityJob(
            id="resume-priority-existing",
            job_type=JobType.FILE_CONVERT,
            display_name="Already Resumed",
            state=JobState.QUEUED,
            config="{}",
            total_items=0,
            completed_items=0,
            failed_items=0,
            skipped_items=0,
            warning_count=0,
            queue_position=0,
            created_at="2026-04-05T00:00:00+00:00",
            started_at="2026-04-05T00:05:00+00:00",
        )
        fresh_one = UtilityJob(
            id="resume-priority-fresh-1",
            job_type=JobType.FILE_CONVERT,
            display_name="Fresh One",
            state=JobState.QUEUED,
            config="{}",
            total_items=0,
            completed_items=0,
            failed_items=0,
            skipped_items=0,
            warning_count=0,
            queue_position=1,
            created_at="2026-04-05T00:10:00+00:00",
        )
        fresh_two = UtilityJob(
            id="resume-priority-fresh-2",
            job_type=JobType.FILE_CONVERT,
            display_name="Fresh Two",
            state=JobState.QUEUED,
            config="{}",
            total_items=0,
            completed_items=0,
            failed_items=0,
            skipped_items=0,
            warning_count=0,
            queue_position=2,
            created_at="2026-04-05T00:15:00+00:00",
        )
        paused = UtilityJob(
            id="resume-priority-paused",
            job_type=JobType.FILE_CONVERT,
            display_name="Paused Job",
            state=JobState.PAUSED,
            config="{}",
            total_items=0,
            completed_items=0,
            failed_items=0,
            skipped_items=0,
            warning_count=0,
            queue_position=None,
            created_at="2026-04-05T00:20:00+00:00",
            started_at="2026-04-05T00:25:00+00:00",
        )
        db_session.add_all([already_resumed, fresh_one, fresh_two, paused])
        await db_session.flush()

        await mgr.resume_job(db_session, paused.id)

        await db_session.refresh(already_resumed)
        await db_session.refresh(fresh_one)
        await db_session.refresh(fresh_two)
        await db_session.refresh(paused)

        assert already_resumed.queue_position == 0
        assert paused.queue_position == 1
        assert fresh_one.queue_position == 2
        assert fresh_two.queue_position == 3

    @pytest.mark.asyncio
    async def test_recover_and_dispatch_starts_next_queued_job(
        self,
        db_session: AsyncSession,
        session_factory,
    ) -> None:
        mgr = JobQueueManager(session_factory=session_factory)
        mgr.register_executor(JobType.FILE_CONVERT, StubExecutor)

        interrupted = UtilityJob(
            id="recovery-running",
            job_type=JobType.FILE_CONVERT,
            display_name="Interrupted Job",
            state=JobState.RUNNING,
            config='{"count": 1}',
            total_items=0,
            completed_items=0,
            failed_items=0,
            skipped_items=0,
            warning_count=0,
            queue_position=None,
            created_at="2026-04-05T00:00:00+00:00",
            started_at="2026-04-05T00:01:00+00:00",
        )
        queued = UtilityJob(
            id="recovery-queued",
            job_type=JobType.FILE_CONVERT,
            display_name="Queued Job",
            state=JobState.QUEUED,
            config='{"count": 1}',
            total_items=0,
            completed_items=0,
            failed_items=0,
            skipped_items=0,
            warning_count=0,
            queue_position=0,
            created_at="2026-04-05T00:02:00+00:00",
        )
        db_session.add_all([interrupted, queued])
        await db_session.flush()

        recovered = await mgr.recover_and_dispatch()

        await db_session.refresh(interrupted)
        await db_session.refresh(queued)
        assert recovered == 1
        assert interrupted.state == JobState.PAUSED
        assert queued.state == JobState.COMPLETED

    @pytest.mark.asyncio
    async def test_after_item_commit_receives_matching_item_payload(
        self,
        db_session: AsyncSession,
        session_factory,
    ) -> None:
        AfterCommitPayloadExecutor.seen_after_commit_paths = []
        mgr = JobQueueManager(session_factory=session_factory)
        mgr.register_executor(JobType.FILE_CONVERT, AfterCommitPayloadExecutor)

        job = await mgr.create_job(
            session=db_session,
            job_type=JobType.FILE_CONVERT,
            display_name="Post Commit Payloads",
            config={},
        )

        await mgr.dispatch_next()

        await db_session.refresh(job)
        assert job.state == JobState.COMPLETED
        assert AfterCommitPayloadExecutor.seen_after_commit_paths == [
            "/comics/first.cbz",
            "/comics/second.cbz",
        ]

    @pytest.mark.asyncio
    async def test_dispatch_uses_configured_worker_count_for_batches(
        self,
        db_session: AsyncSession,
        session_factory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_session.add(SystemConfig(key="utility_worker_count", value="2", value_type="int"))
        await db_session.flush()

        batch_sizes: list[int] = []
        worker_counts: list[int] = []

        class FakeWorkerPool:
            def __init__(self, max_workers: int = 4) -> None:
                worker_counts.append(max_workers)
                self._max_workers = max_workers

            async def iter_batch_results(
                self,
                items: list[dict[str, Any]],
                executor: JobExecutor,
                job_config: dict[str, Any],
            ):
                batch_sizes.append(len(items))
                for idx, item in enumerate(items):
                    result = executor.process_item(item, job_config)
                    result.worker_id = (idx % self._max_workers) + 1
                    yield result

            async def process_batch(
                self,
                items: list[dict[str, Any]],
                executor: JobExecutor,
                job_config: dict[str, Any],
            ) -> list[ProcessedItem]:
                batch_sizes.append(len(items))
                results: list[ProcessedItem] = []
                for idx, item in enumerate(items):
                    result = executor.process_item(item, job_config)
                    result.worker_id = (idx % self._max_workers) + 1
                    results.append(result)
                return results

            def shutdown(self) -> None:
                return None

        monkeypatch.setattr("pullbox.utilities.job_queue.WorkerPool", FakeWorkerPool)

        mgr = JobQueueManager(session_factory=session_factory)
        mgr.register_executor(JobType.FILE_CONVERT, BatchRecordingExecutor)

        job = await mgr.create_job(
            session=db_session,
            job_type=JobType.FILE_CONVERT,
            display_name="Batch Worker Count",
            config={"count": 5},
        )

        await mgr.dispatch_next()

        await db_session.refresh(job)
        assert job.state == JobState.COMPLETED
        assert worker_counts == [2]
        assert batch_sizes == [2, 2, 1]

        result = await db_session.execute(
            select(UtilityJobItem)
            .where(UtilityJobItem.job_id == job.id)
            .order_by(UtilityJobItem.item_index)
        )
        items = list(result.scalars().all())
        assert len(items) == 5
        assert all(item.worker_id in {1, 2} for item in items)

    @pytest.mark.asyncio
    async def test_dispatch_defaults_worker_count_when_setting_missing(
        self,
        db_session: AsyncSession,
        session_factory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worker_counts: list[int] = []

        class FakeWorkerPool:
            def __init__(self, max_workers: int = 4) -> None:
                worker_counts.append(max_workers)

            async def iter_batch_results(
                self,
                items: list[dict[str, Any]],
                executor: JobExecutor,
                job_config: dict[str, Any],
            ):
                for item in items:
                    yield ProcessedItem(
                        item_id=item["id"],
                        result=ItemResult.COMPLETED,
                        duration_ms=5,
                        worker_id=1,
                        log_entries=[("INFO", f"Processed {item['id']}", {})],
                    )

            async def process_batch(
                self,
                items: list[dict[str, Any]],
                executor: JobExecutor,
                job_config: dict[str, Any],
            ) -> list[ProcessedItem]:
                return [
                    ProcessedItem(
                        item_id=item["id"],
                        result=ItemResult.COMPLETED,
                        duration_ms=5,
                        worker_id=1,
                        log_entries=[("INFO", f"Processed {item['id']}", {})],
                    )
                    for item in items
                ]

            def shutdown(self) -> None:
                return None

        monkeypatch.setattr("pullbox.utilities.job_queue.WorkerPool", FakeWorkerPool)

        mgr = JobQueueManager(session_factory=session_factory)
        mgr.register_executor(JobType.FILE_CONVERT, BatchRecordingExecutor)

        await mgr.create_job(
            session=db_session,
            job_type=JobType.FILE_CONVERT,
            display_name="Default Worker Count",
            config={"count": 1},
        )

        await mgr.dispatch_next()

        assert worker_counts == [4]

    @pytest.mark.asyncio
    async def test_dispatch_updates_progress_before_full_batch_completes(
        self,
        db_session: AsyncSession,
        session_factory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_session.add(SystemConfig(key="utility_worker_count", value="2", value_type="int"))
        await db_session.flush()

        first_result_persisted = asyncio.Event()
        release_remaining = asyncio.Event()

        class FakeWorkerPool:
            def __init__(self, max_workers: int = 4) -> None:
                self._max_workers = max_workers

            async def iter_batch_results(
                self,
                items: list[dict[str, Any]],
                executor: JobExecutor,
                job_config: dict[str, Any],
            ):
                first = executor.process_item(items[0], job_config)
                first.worker_id = 1
                yield first
                first_result_persisted.set()
                await release_remaining.wait()

                for idx, item in enumerate(items[1:], start=1):
                    result = executor.process_item(item, job_config)
                    result.worker_id = (idx % self._max_workers) + 1
                    yield result

            async def process_batch(
                self,
                items: list[dict[str, Any]],
                executor: JobExecutor,
                job_config: dict[str, Any],
            ) -> list[ProcessedItem]:
                await release_remaining.wait()
                results: list[ProcessedItem] = []
                for idx, item in enumerate(items):
                    result = executor.process_item(item, job_config)
                    result.worker_id = (idx % self._max_workers) + 1
                    results.append(result)
                return results

            def shutdown(self) -> None:
                return None

        monkeypatch.setattr("pullbox.utilities.job_queue.WorkerPool", FakeWorkerPool)

        mgr = JobQueueManager(session_factory=session_factory)
        mgr.register_executor(JobType.FILE_CONVERT, BatchRecordingExecutor)

        job = await mgr.create_job(
            session=db_session,
            job_type=JobType.FILE_CONVERT,
            display_name="Incremental Batch Progress",
            config={"count": 3},
        )

        dispatch_task = asyncio.create_task(mgr.dispatch_next())
        try:
            await asyncio.wait_for(first_result_persisted.wait(), timeout=0.5)

            await db_session.refresh(job)
            assert job.state == JobState.RUNNING
            assert job.completed_items == 1
            assert job.failed_items == 0

            result = await db_session.execute(
                select(UtilityJobItem)
                .where(UtilityJobItem.job_id == job.id)
                .order_by(UtilityJobItem.item_index)
            )
            items = list(result.scalars().all())
            assert items[0].state == ItemState.COMPLETED
            assert items[1].state == ItemState.IN_PROGRESS
            assert items[2].state == ItemState.PENDING
        finally:
            release_remaining.set()
            await dispatch_task

    @pytest.mark.asyncio
    async def test_dispatch_marks_items_in_progress_before_worker_results(
        self,
        db_session: AsyncSession,
        session_factory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_session.add(SystemConfig(key="utility_worker_count", value="2", value_type="int"))
        await db_session.flush()

        class FakeWorkerPool:
            def __init__(self, max_workers: int = 4) -> None:
                self._max_workers = max_workers

            async def iter_batch_results(
                self,
                items: list[dict[str, Any]],
                executor: JobExecutor,
                job_config: dict[str, Any],
            ):
                async with session_factory() as inspect_session:
                    result = await inspect_session.execute(
                        select(UtilityJobItem)
                        .where(UtilityJobItem.job_id == job.id)
                        .order_by(UtilityJobItem.item_index)
                    )
                    persisted_items = list(result.scalars().all())
                    assert len(persisted_items) == 2
                    assert all(item.state == ItemState.IN_PROGRESS for item in persisted_items)
                    assert all(item.started_at is not None for item in persisted_items)

                for idx, item in enumerate(items):
                    result = executor.process_item(item, job_config)
                    result.worker_id = (idx % self._max_workers) + 1
                    yield result

            def shutdown(self) -> None:
                return None

        monkeypatch.setattr("pullbox.utilities.job_queue.WorkerPool", FakeWorkerPool)

        mgr = JobQueueManager(session_factory=session_factory)
        mgr.register_executor(JobType.FILE_CONVERT, BatchRecordingExecutor)

        job = await mgr.create_job(
            session=db_session,
            job_type=JobType.FILE_CONVERT,
            display_name="In Progress Marking",
            config={"count": 2},
        )

        await mgr.dispatch_next()

        await db_session.refresh(job)
        assert job.state == JobState.COMPLETED

    @pytest.mark.asyncio
    async def test_dispatch_marks_item_failed_when_queue_side_persistence_blows_up(
        self,
        db_session: AsyncSession,
        session_factory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("simulated persistence failure")

        monkeypatch.setattr(DBCheckCleanupExecutor, "apply_item_result", _boom)

        mgr = JobQueueManager(session_factory=session_factory)
        mgr.register_executor(JobType.DB_CHECK_CLEANUP, DBCheckCleanupExecutor)

        job = await mgr.create_job(
            session=db_session,
            job_type=JobType.DB_CHECK_CLEANUP,
            display_name="Persistence Failure",
            config={
                "checks": ["reindex"],
                "mode": "execute",
                "actions": [
                    {
                        "operation": "repair",
                        "record_type": "library_file",
                        "record_id": 1,
                        "file_path": "/comics/failure.cbz",
                        "description": "Trigger queue-side persistence failure",
                    }
                ],
            },
        )

        await mgr.dispatch_next()

        await db_session.refresh(job)
        assert job.state == JobState.FAILED
        assert job.failed_items == 1
        assert job.completed_items == 0

        result = await db_session.execute(
            select(UtilityJobItem).where(UtilityJobItem.job_id == job.id)
        )
        items = list(result.scalars().all())
        assert len(items) == 1
        assert items[0].state == ItemState.FAILED
        assert "Result persistence failed" in (items[0].error_message or "")

    @pytest.mark.asyncio
    async def test_failed_item_promotes_guidance_logs_to_error(
        self,
        db_session: AsyncSession,
        session_factory,
    ) -> None:
        mgr = JobQueueManager(session_factory=session_factory)
        mgr.register_executor(JobType.FILE_CONVERT, FailedItemGuidanceExecutor)

        job = await mgr.create_job(
            session=db_session,
            job_type=JobType.FILE_CONVERT,
            display_name="Failed Guidance Job",
            config={},
        )

        await mgr.dispatch_next()

        result = await db_session.execute(
            select(UtilityJobLog).where(UtilityJobLog.job_id == job.id).order_by(UtilityJobLog.id)
        )
        logs = list(result.scalars().all())
        guidance = [
            log
            for log in logs
            if "Try converting the archive or replacing the source file" in log.message
        ]
        assert guidance
        assert guidance[0].level == "ERROR"

    @pytest.mark.asyncio
    async def test_utility_log_level_filters_db_history_and_file_output(
        self,
        db_session: AsyncSession,
        session_factory,
        tmp_path,
    ) -> None:
        db_session.add(SystemConfig(key="utility_log_level", value="WARNING", value_type="string"))
        await db_session.flush()
        configure_utility_logging(tmp_path, level="WARNING")

        mgr = JobQueueManager(session_factory=session_factory)
        mgr.register_executor(JobType.FILE_CONVERT, MixedLevelLogExecutor)

        job = await mgr.create_job(
            session=db_session,
            job_type=JobType.FILE_CONVERT,
            display_name="Mixed Level Log Job",
            config={},
        )

        await mgr.dispatch_next()

        result = await db_session.execute(
            select(UtilityJobLog).where(UtilityJobLog.job_id == job.id).order_by(UtilityJobLog.id)
        )
        logs = list(result.scalars().all())
        messages = [log.message for log in logs]

        assert "Debug detail" not in messages
        assert "Info detail" not in messages
        assert "Job started: Mixed Level Log Job" not in messages
        assert "Job completed. 1 succeeded." not in messages
        assert "Warning detail" in messages
        assert "Error detail" in messages

        content = (tmp_path / "utilities.log").read_text()
        assert "Debug detail" not in content
        assert "Info detail" not in content
        assert "Warning detail" in content
        assert "Error detail" in content


# ── Job Lifecycle Edge Cases ──────────────────────────────────


class TestJobLifecycleEdgeCases:
    """Verify edge cases in job lifecycle and executor registry."""

    def test_unknown_job_type_returns_none(self) -> None:
        """Executor registry returns None for unregistered job type."""
        mgr = JobQueueManager(session_factory=None)
        result = mgr.get_executor("totally_unknown_type_xyz")
        assert result is None

    @pytest.mark.asyncio
    async def test_cancel_queued_job_directly_to_cancelled_clears_queue_position(
        self, db_session: AsyncSession
    ) -> None:
        """Cancelling QUEUED job goes CANCELLING then CANCELLED, clears queue_position."""
        job = _make_job(job_id="cancel-qp", state=JobState.QUEUED, queue_position=5)
        db_session.add(job)
        await db_session.flush()

        mgr = JobQueueManager(session_factory=None)
        await mgr.cancel_job(db_session, "cancel-qp")

        await db_session.refresh(job)
        assert job.state == JobState.CANCELLED
        assert job.queue_position is None
