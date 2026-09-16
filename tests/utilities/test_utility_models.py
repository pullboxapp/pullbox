"""Tests for UT-F.2 — utility ORM models.

Verifies UtilityJob, UtilityJobItem, UtilityJobLog models persist
correctly, relationships work, helper properties return expected values,
and edge cases (zero division, large JSON, null file_path) are handled.

Run:
    pytest tests/utilities/test_utility_models.py -v
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from pullbox.utilities.models import (
    ItemState,
    JobState,
    JobType,
    LogLevel,
    UtilityJob,
    UtilityJobItem,
    UtilityJobLog,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# ── Fixtures ────────────────────────────────────────────────


@pytest.fixture
def sample_job() -> UtilityJob:
    """Create a UtilityJob with typical values."""
    return UtilityJob(
        id="abc123",
        job_type=JobType.FILE_CONVERT,
        display_name="Convert 10 files",
        state=JobState.QUEUED,
        config=json.dumps({"target_format": "cbz"}),
        total_items=10,
        completed_items=0,
        failed_items=0,
        skipped_items=0,
        warning_count=0,
    )


# ── Model Creation & Persistence ───────────────────────────


class TestUtilityJobPersistence:
    """Verify UtilityJob ORM persistence and field access."""

    @pytest.mark.asyncio
    async def test_create_job_all_fields(self, db_session: AsyncSession) -> None:
        job = UtilityJob(
            id="job-001",
            job_type=JobType.MASS_CONVERT_PIPELINE,
            display_name="Mass Convert CBR→CBZ",
            state=JobState.RUNNING,
            config=json.dumps({"source_dir": "/comics"}),
            total_items=100,
            completed_items=50,
            failed_items=2,
            skipped_items=3,
            warning_count=1,
            queue_position=None,
            created_by="admin",
            error_message=None,
        )
        db_session.add(job)
        await db_session.flush()

        assert job.id == "job-001"
        assert job.job_type == JobType.MASS_CONVERT_PIPELINE
        assert job.display_name == "Mass Convert CBR→CBZ"
        assert job.state == JobState.RUNNING
        assert job.total_items == 100
        assert job.completed_items == 50

    @pytest.mark.asyncio
    async def test_create_job_minimal_fields(self, db_session: AsyncSession) -> None:
        """Job with only required fields uses defaults."""
        job = UtilityJob(
            id="job-002",
            job_type=JobType.EXPORT_LIBRARY,
            display_name="Export Library",
        )
        db_session.add(job)
        await db_session.flush()

        assert job.state == JobState.QUEUED
        assert job.config == "{}"

    @pytest.mark.asyncio
    async def test_job_config_round_trips(self, db_session: AsyncSession) -> None:
        """Deeply nested config JSON round-trips through ORM."""
        nested_config = json.dumps(
            {
                "filters": {"formats": ["cbr", "cb7"], "min_size": 1024},
                "options": {"preserve_metadata": True, "workers": 4},
                "paths": [f"/comics/series_{i}" for i in range(100)],
            }
        )
        job = UtilityJob(
            id="job-003",
            job_type=JobType.FILE_CONVERT,
            display_name="Test",
            config=nested_config,
        )
        db_session.add(job)
        await db_session.flush()

        assert job.config == nested_config
        parsed = json.loads(job.config)
        assert len(parsed["paths"]) == 100


class TestUtilityJobItemPersistence:
    """Verify UtilityJobItem ORM persistence."""

    @pytest.mark.asyncio
    async def test_create_item_with_job(self, db_session: AsyncSession) -> None:
        job = UtilityJob(
            id="job-010",
            job_type=JobType.FILE_CONVERT,
            display_name="Convert",
        )
        item = UtilityJobItem(
            id="item-001",
            job_id="job-010",
            item_index=0,
            state=ItemState.COMPLETED,
            file_path="/comics/batman_001.cbr",
            operation="cbr_to_cbz",
            before_state=json.dumps({"path": "/comics/batman_001.cbr"}),
            after_state=json.dumps({"path": "/comics/batman_001.cbz"}),
            duration_ms=1500,
            worker_id=2,
        )
        db_session.add(job)
        db_session.add(item)
        await db_session.flush()

        assert item.job_id == "job-010"
        assert item.file_path == "/comics/batman_001.cbr"
        assert item.duration_ms == 1500

    @pytest.mark.asyncio
    async def test_item_null_file_path(self, db_session: AsyncSession) -> None:
        """Items for DB-only operations can have null file_path."""
        job = UtilityJob(
            id="job-011",
            job_type=JobType.DB_CHECK_CLEANUP,
            display_name="DB Check",
        )
        item = UtilityJobItem(
            id="item-002",
            job_id="job-011",
            item_index=0,
            operation="orphan_check",
            file_path=None,
        )
        db_session.add(job)
        db_session.add(item)
        await db_session.flush()

        assert item.file_path is None

    @pytest.mark.asyncio
    async def test_item_large_before_after_state(self, db_session: AsyncSession) -> None:
        """Large JSON blobs in before_state/after_state round-trip correctly."""
        job = UtilityJob(
            id="job-012",
            job_type=JobType.MASS_RENAME,
            display_name="Rename",
        )
        large_state = json.dumps(
            {
                "files": [f"/comics/file_{i}.cbr" for i in range(1000)],
                "metadata": {"series": "Batman", "publisher": "DC Comics"},
            }
        )
        item = UtilityJobItem(
            id="item-003",
            job_id="job-012",
            item_index=0,
            operation="rename",
            before_state=large_state,
            after_state=large_state,
        )
        db_session.add(job)
        db_session.add(item)
        await db_session.flush()

        assert item.before_state == large_state
        assert item.after_state == large_state


class TestUtilityJobLogPersistence:
    """Verify UtilityJobLog ORM persistence."""

    @pytest.mark.asyncio
    async def test_create_log_entry(self, db_session: AsyncSession) -> None:
        job = UtilityJob(
            id="job-020",
            job_type=JobType.INTEGRITY_CHECK,
            display_name="Integrity Check",
        )
        log = UtilityJobLog(
            job_id="job-020",
            level=LogLevel.INFO,
            message="Starting integrity check",
            file_path="/comics/batman_001.cbz",
            extra=json.dumps({"file_size": 52428800}),
        )
        db_session.add(job)
        db_session.add(log)
        await db_session.flush()

        assert log.job_id == "job-020"
        assert log.level == LogLevel.INFO
        assert log.message == "Starting integrity check"

    @pytest.mark.asyncio
    async def test_log_with_item_reference(self, db_session: AsyncSession) -> None:
        job = UtilityJob(
            id="job-021",
            job_type=JobType.FILE_CONVERT,
            display_name="Convert",
        )
        item = UtilityJobItem(
            id="item-010",
            job_id="job-021",
            item_index=0,
            operation="convert",
        )
        log = UtilityJobLog(
            job_id="job-021",
            item_id="item-010",
            level=LogLevel.ERROR,
            message="Conversion failed: corrupt archive",
        )
        db_session.add_all([job, item, log])
        await db_session.flush()

        assert log.item_id == "item-010"


# ── Relationships ───────────────────────────────────────────


class TestRelationships:
    """Verify ORM relationships between utility models."""

    @pytest.mark.asyncio
    async def test_job_items_relationship(self, db_session: AsyncSession) -> None:
        job = UtilityJob(
            id="job-030",
            job_type=JobType.FILE_CONVERT,
            display_name="Convert",
        )
        items = [
            UtilityJobItem(
                id=f"item-03{i}",
                job_id="job-030",
                item_index=i,
                operation="convert",
            )
            for i in range(3)
        ]
        db_session.add(job)
        db_session.add_all(items)
        await db_session.flush()

        # Reload with eager loading for async compatibility
        result = await db_session.execute(
            select(UtilityJob)
            .where(UtilityJob.id == "job-030")
            .options(selectinload(UtilityJob.items))
        )
        loaded_job = result.scalar_one()
        assert len(loaded_job.items) == 3
        assert loaded_job.items[0].item_index == 0

    @pytest.mark.asyncio
    async def test_job_logs_relationship(self, db_session: AsyncSession) -> None:
        job = UtilityJob(
            id="job-031",
            job_type=JobType.FILE_CONVERT,
            display_name="Convert",
        )
        logs = [
            UtilityJobLog(
                job_id="job-031",
                level=LogLevel.INFO,
                message=f"Step {i}",
            )
            for i in range(3)
        ]
        db_session.add(job)
        db_session.add_all(logs)
        await db_session.flush()

        result = await db_session.execute(
            select(UtilityJob)
            .where(UtilityJob.id == "job-031")
            .options(selectinload(UtilityJob.logs))
        )
        loaded_job = result.scalar_one()
        assert len(loaded_job.logs) == 3

    @pytest.mark.asyncio
    async def test_item_back_reference_to_job(self, db_session: AsyncSession) -> None:
        job = UtilityJob(
            id="job-032",
            job_type=JobType.FILE_CONVERT,
            display_name="Convert",
        )
        item = UtilityJobItem(
            id="item-032",
            job_id="job-032",
            item_index=0,
            operation="convert",
        )
        db_session.add_all([job, item])
        await db_session.flush()

        assert item.job.id == "job-032"
        assert item.job.display_name == "Convert"

    @pytest.mark.asyncio
    async def test_parent_job_relationship(self, db_session: AsyncSession) -> None:
        parent = UtilityJob(
            id="job-parent",
            job_type=JobType.FILE_CONVERT,
            display_name="Original",
            state=JobState.COMPLETED,
        )
        rollback = UtilityJob(
            id="job-rollback",
            job_type=JobType.ROLLBACK,
            display_name="Rollback",
            parent_job_id="job-parent",
        )
        db_session.add_all([parent, rollback])
        await db_session.flush()

        assert rollback.parent_job is not None
        assert rollback.parent_job.id == "job-parent"

    @pytest.mark.asyncio
    async def test_deleting_parent_job_clears_rollback_parent_reference(
        self, db_session: AsyncSession
    ) -> None:
        parent = UtilityJob(
            id="job-parent-delete",
            job_type=JobType.FILE_CONVERT,
            display_name="Original",
            state=JobState.COMPLETED,
        )
        rollback = UtilityJob(
            id="job-rollback-delete",
            job_type=JobType.ROLLBACK,
            display_name="Rollback",
            parent_job_id="job-parent-delete",
        )
        await db_session.execute(text("PRAGMA foreign_keys=ON"))
        db_session.add_all([parent, rollback])
        await db_session.flush()

        await db_session.delete(parent)
        await db_session.flush()
        await db_session.refresh(rollback)

        assert rollback.parent_job_id is None

    @pytest.mark.asyncio
    async def test_deleting_item_clears_log_item_reference(self, db_session: AsyncSession) -> None:
        job = UtilityJob(
            id="job-item-delete",
            job_type=JobType.FILE_CONVERT,
            display_name="Convert",
        )
        item = UtilityJobItem(
            id="item-delete",
            job_id="job-item-delete",
            item_index=0,
            operation="convert",
        )
        log = UtilityJobLog(
            job_id="job-item-delete",
            item_id="item-delete",
            level=LogLevel.INFO,
            message="Item started",
        )
        await db_session.execute(text("PRAGMA foreign_keys=ON"))
        db_session.add_all([job, item, log])
        await db_session.flush()

        await db_session.delete(item)
        await db_session.flush()
        await db_session.refresh(log)

        assert log.item_id is None


# ── Enum Values ─────────────────────────────────────────────


class TestEnums:
    """Verify enum values match migration CHECK constraints."""

    def test_job_type_values(self) -> None:
        assert JobType.FILE_CONVERT == "file_convert"
        assert JobType.MASS_CONVERT_PIPELINE == "mass_convert_pipeline"
        assert JobType.MASS_RENAME == "mass_rename"
        assert JobType.DB_CHECK_CLEANUP == "db_check_cleanup"
        assert JobType.EXPORT_LIBRARY == "export_library"
        assert JobType.INTEGRITY_CHECK == "integrity_check"
        assert JobType.LIBRARY_PERMISSIONS == "library_permissions"
        assert JobType.ROLLBACK == "rollback"
        assert len(JobType) == 9
        assert JobType.SERIES_RESCAN == "series_rescan"

    def test_job_state_values(self) -> None:
        assert JobState.QUEUED == "QUEUED"
        assert JobState.RUNNING == "RUNNING"
        assert JobState.PAUSING == "PAUSING"
        assert JobState.PAUSED == "PAUSED"
        assert JobState.CANCELLING == "CANCELLING"
        assert JobState.CANCELLED == "CANCELLED"
        assert JobState.COMPLETED == "COMPLETED"
        assert JobState.FAILED == "FAILED"
        assert JobState.ROLLING_BACK == "ROLLING_BACK"
        assert JobState.ROLLED_BACK == "ROLLED_BACK"
        assert len(JobState) == 10

    def test_item_state_values(self) -> None:
        assert ItemState.PENDING == "PENDING"
        assert ItemState.IN_PROGRESS == "IN_PROGRESS"
        assert ItemState.COMPLETED == "COMPLETED"
        assert ItemState.FAILED == "FAILED"
        assert ItemState.SKIPPED == "SKIPPED"
        assert ItemState.ROLLED_BACK == "ROLLED_BACK"
        assert ItemState.ROLLBACK_FAILED == "ROLLBACK_FAILED"
        assert len(ItemState) == 7

    def test_log_level_values(self) -> None:
        assert LogLevel.DEBUG == "DEBUG"
        assert LogLevel.INFO == "INFO"
        assert LogLevel.WARNING == "WARNING"
        assert LogLevel.ERROR == "ERROR"
        assert len(LogLevel) == 4


# ── Helper Properties: UtilityJob ───────────────────────────


class TestJobHelperProperties:
    """Verify computed helper properties on UtilityJob."""

    def test_progress_pct_normal(self) -> None:
        job = UtilityJob(
            id="j1",
            job_type=JobType.FILE_CONVERT,
            display_name="T",
            total_items=10,
            completed_items=3,
            failed_items=1,
            skipped_items=1,
        )
        # (3 + 1 + 1) / 10 * 100 = 50.0
        assert job.progress_pct == 50.0

    def test_progress_pct_zero_total(self) -> None:
        """No ZeroDivisionError when total_items is 0."""
        job = UtilityJob(
            id="j2",
            job_type=JobType.FILE_CONVERT,
            display_name="T",
            total_items=0,
            completed_items=0,
            failed_items=0,
            skipped_items=0,
        )
        assert job.progress_pct == 0.0

    def test_progress_pct_none_total(self) -> None:
        """No error when total_items is None (job not yet scanned)."""
        job = UtilityJob(
            id="j3",
            job_type=JobType.FILE_CONVERT,
            display_name="T",
            total_items=None,
        )
        assert job.progress_pct == 0.0

    def test_progress_pct_exceeds_total(self) -> None:
        """Returns actual ratio even if processed > total (shouldn't happen)."""
        job = UtilityJob(
            id="j4",
            job_type=JobType.FILE_CONVERT,
            display_name="T",
            total_items=5,
            completed_items=6,
            failed_items=0,
            skipped_items=0,
        )
        assert job.progress_pct == 100.0

    def test_progress_pct_all_completed(self) -> None:
        job = UtilityJob(
            id="j5",
            job_type=JobType.FILE_CONVERT,
            display_name="T",
            total_items=10,
            completed_items=10,
            failed_items=0,
            skipped_items=0,
        )
        assert job.progress_pct == 100.0

    # ── is_terminal ─────────────────────────────────────────

    def test_is_terminal_for_terminal_states(self) -> None:
        for state in [
            JobState.COMPLETED,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.ROLLED_BACK,
        ]:
            job = UtilityJob(
                id="jt",
                job_type=JobType.FILE_CONVERT,
                display_name="T",
                state=state,
            )
            assert job.is_terminal is True, f"{state} should be terminal"

    def test_is_terminal_false_for_active_states(self) -> None:
        for state in [
            JobState.QUEUED,
            JobState.RUNNING,
            JobState.PAUSING,
            JobState.PAUSED,
            JobState.CANCELLING,
            JobState.ROLLING_BACK,
        ]:
            job = UtilityJob(
                id="jt",
                job_type=JobType.FILE_CONVERT,
                display_name="T",
                state=state,
            )
            assert job.is_terminal is False, f"{state} should NOT be terminal"

    # ── is_pausable ─────────────────────────────────────────

    def test_is_pausable_only_for_running(self) -> None:
        for state in JobState:
            job = UtilityJob(
                id="jp",
                job_type=JobType.FILE_CONVERT,
                display_name="T",
                state=state,
            )
            expected = state == JobState.RUNNING
            assert job.is_pausable is expected, f"{state}: is_pausable should be {expected}"

    # ── is_cancellable ──────────────────────────────────────

    def test_is_cancellable_for_active_states(self) -> None:
        cancellable = {JobState.QUEUED, JobState.RUNNING, JobState.PAUSED}
        for state in JobState:
            job = UtilityJob(
                id="jc",
                job_type=JobType.FILE_CONVERT,
                display_name="T",
                state=state,
            )
            expected = state in cancellable
            assert job.is_cancellable is expected, f"{state}: is_cancellable should be {expected}"


# ── Helper Properties: UtilityJobItem ───────────────────────


class TestItemHelperProperties:
    """Verify computed helper properties on UtilityJobItem."""

    def test_duration_display_zero_ms(self) -> None:
        item = UtilityJobItem(
            id="i1",
            job_id="j",
            item_index=0,
            operation="op",
            duration_ms=0,
        )
        assert item.duration_display == "0ms"

    def test_duration_display_sub_second(self) -> None:
        item = UtilityJobItem(
            id="i2",
            job_id="j",
            item_index=0,
            operation="op",
            duration_ms=500,
        )
        assert item.duration_display == "500ms"

    def test_duration_display_seconds(self) -> None:
        item = UtilityJobItem(
            id="i3",
            job_id="j",
            item_index=0,
            operation="op",
            duration_ms=3200,
        )
        assert item.duration_display == "3.2s"

    def test_duration_display_minutes_and_seconds(self) -> None:
        item = UtilityJobItem(
            id="i4",
            job_id="j",
            item_index=0,
            operation="op",
            duration_ms=65000,
        )
        assert item.duration_display == "1m 5s"

    def test_duration_display_hours_and_minutes(self) -> None:
        item = UtilityJobItem(
            id="i5",
            job_id="j",
            item_index=0,
            operation="op",
            duration_ms=7200000,
        )
        assert item.duration_display == "2h 0m"

    def test_duration_display_none(self) -> None:
        """None duration_ms (not yet timed) returns empty string."""
        item = UtilityJobItem(
            id="i6",
            job_id="j",
            item_index=0,
            operation="op",
            duration_ms=None,
        )
        assert item.duration_display == ""

    def test_duration_display_one_hour_one_minute(self) -> None:
        item = UtilityJobItem(
            id="i7",
            job_id="j",
            item_index=0,
            operation="op",
            duration_ms=3660000,
        )
        assert item.duration_display == "1h 1m"


# ── Issue Integrity Columns ─────────────────────────────────


class TestIssueIntegrityColumns:
    """Verify integrity columns are accessible on the Issue model."""

    @pytest.mark.asyncio
    async def test_issue_integrity_columns_exist(self, db_session: AsyncSession) -> None:
        from pullbox.models.issue import Issue
        from pullbox.models.series import Series, SeriesStatus

        series = Series(
            title="Batman",
            sort_title="batman",
            status=SeriesStatus.CONTINUING,
            issue_count=0,
            monitored=False,
        )
        db_session.add(series)
        await db_session.flush()

        issue = Issue(
            series_id=series.id,
            issue_number=1.0,
            status="wanted",
        )
        db_session.add(issue)
        await db_session.flush()

        assert issue.integrity_status == "unchecked"
        assert issue.integrity_details == "{}"
        assert issue.integrity_checked_at is None

    @pytest.mark.asyncio
    async def test_issue_integrity_status_update(self, db_session: AsyncSession) -> None:
        from pullbox.models.issue import Issue
        from pullbox.models.series import Series, SeriesStatus

        series = Series(
            title="Batman",
            sort_title="batman",
            status=SeriesStatus.CONTINUING,
            issue_count=0,
            monitored=False,
        )
        db_session.add(series)
        await db_session.flush()

        issue = Issue(
            series_id=series.id,
            issue_number=1.0,
            status="wanted",
        )
        db_session.add(issue)
        await db_session.flush()

        issue.integrity_status = "healthy"
        issue.integrity_details = json.dumps({"pages": 24, "format": "cbz"})
        issue.integrity_checked_at = "2026-03-17T12:00:00"
        await db_session.flush()

        assert issue.integrity_status == "healthy"
        assert json.loads(issue.integrity_details)["pages"] == 24
