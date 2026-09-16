"""Utility job ORM models — UtilityJob, UtilityJobItem, UtilityJobLog.

Persistent storage for the utility job queue system. Jobs track
file conversions, mass renames, integrity checks, and other
batch operations with per-item checkpointing and rollback support.

State machine follows the same StrEnum pattern as DownloadState
in models/download.py for cross-sprint consistency.
"""

from __future__ import annotations

import enum

from sqlalchemy import ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pullbox.models.base import Base

# ── Enums ───────────────────────────────────────────────────


class JobType(enum.StrEnum):
    """Utility job type identifiers — matches migration CHECK constraint."""

    FILE_CONVERT = "file_convert"
    MASS_CONVERT_PIPELINE = "mass_convert_pipeline"
    MASS_RENAME = "mass_rename"
    DB_CHECK_CLEANUP = "db_check_cleanup"
    SERIES_RESCAN = "series_rescan"
    EXPORT_LIBRARY = "export_library"
    INTEGRITY_CHECK = "integrity_check"
    LIBRARY_PERMISSIONS = "library_permissions"
    ROLLBACK = "rollback"


class JobState(enum.StrEnum):
    """Job lifecycle states — matches migration CHECK constraint.

    Follows the same StrEnum pattern as DownloadState for consistency.
    Terminal states: COMPLETED, FAILED, CANCELLED, ROLLED_BACK.
    """

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"


class ItemState(enum.StrEnum):
    """Per-item processing states — matches migration CHECK constraint."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    ROLLED_BACK = "ROLLED_BACK"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"


class LogLevel(enum.StrEnum):
    """Structured log levels — matches migration CHECK constraint."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


# ── Terminal / controllable state sets ──────────────────────

_TERMINAL_STATES = frozenset(
    {
        JobState.COMPLETED,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.ROLLED_BACK,
    }
)

_CANCELLABLE_STATES = frozenset(
    {
        JobState.QUEUED,
        JobState.RUNNING,
        JobState.PAUSED,
    }
)


# ── Models ──────────────────────────────────────────────────


class UtilityJob(Base):
    """A utility job with state machine, progress tracking, and rollback support.

    Primary key is a hex string (not auto-incrementing integer) to allow
    ID generation before database insertion. Timestamps are stored as TEXT
    for SQLite compatibility with the migration schema.
    """

    __tablename__ = "utility_jobs"
    __table_args__ = (
        Index("ix_utility_jobs_state", "state"),
        Index("ix_utility_jobs_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default=JobState.QUEUED)
    config: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    # Progress counters
    total_items: Mapped[int | None] = mapped_column(Integer)
    completed_items: Mapped[int | None] = mapped_column(Integer)
    failed_items: Mapped[int | None] = mapped_column(Integer)
    skipped_items: Mapped[int | None] = mapped_column(Integer)
    warning_count: Mapped[int | None] = mapped_column(Integer)
    queue_position: Mapped[int | None] = mapped_column(Integer)

    # Timestamps (TEXT for SQLite)
    created_at: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[str | None] = mapped_column(Text)
    paused_at: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[str | None] = mapped_column(Text)

    # Metadata
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(Text)
    parent_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("utility_jobs.id", ondelete="SET NULL"), nullable=True
    )

    # Relationships
    items: Mapped[list[UtilityJobItem]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="UtilityJobItem.item_index",
    )
    logs: Mapped[list[UtilityJobLog]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
    )
    parent_job: Mapped[UtilityJob | None] = relationship(
        remote_side=[id],
    )

    # ── Helper properties ───────────────────────────────────

    @property
    def progress_pct(self) -> float:
        """Percentage of items processed (completed + failed + skipped).

        Returns 0.0 when total_items is None or 0. Capped at 100.0.
        """
        total = self.total_items or 0
        if total == 0:
            return 0.0
        completed = self.completed_items or 0
        failed = self.failed_items or 0
        skipped = self.skipped_items or 0
        return min((completed + failed + skipped) / total * 100, 100.0)

    @property
    def is_terminal(self) -> bool:
        """Whether the job is in a final state."""
        return JobState(self.state) in _TERMINAL_STATES

    @property
    def is_pausable(self) -> bool:
        """Whether the job can be paused (only when RUNNING)."""
        return JobState(self.state) == JobState.RUNNING

    @property
    def is_cancellable(self) -> bool:
        """Whether the job can be cancelled."""
        return JobState(self.state) in _CANCELLABLE_STATES


class UtilityJobItem(Base):
    """A single work item within a utility job.

    Tracks per-item state, before/after snapshots for rollback,
    timing, and worker assignment.
    """

    __tablename__ = "utility_job_items"
    __table_args__ = (
        Index("ix_utility_job_items_job_id", "job_id"),
        Index("ix_utility_job_items_job_id_state", "job_id", "state"),
        Index("ix_utility_job_items_job_id_item_index", "job_id", "item_index"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("utility_jobs.id", ondelete="CASCADE"), nullable=False
    )
    item_index: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default=ItemState.PENDING)
    file_path: Mapped[str | None] = mapped_column(Text)
    operation: Mapped[str] = mapped_column(Text, nullable=False)

    # Rollback snapshots (JSON)
    before_state: Mapped[str | None] = mapped_column(Text, default="{}")
    after_state: Mapped[str | None] = mapped_column(Text, default="{}")

    # Timing
    started_at: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    # Results
    error_message: Mapped[str | None] = mapped_column(Text)
    warning_message: Mapped[str | None] = mapped_column(Text)
    worker_id: Mapped[int | None] = mapped_column(Integer)

    # Relationships
    job: Mapped[UtilityJob] = relationship(back_populates="items")

    # ── Helper properties ───────────────────────────────────

    @property
    def duration_display(self) -> str:
        """Human-readable duration string.

        Returns:
            "0ms", "0.5s", "3.2s", "1m 5s", "2h 0m", or "" if not timed.
        """
        if self.duration_ms is None:
            return ""
        ms = self.duration_ms
        if ms < 1000:
            return f"{ms}ms"
        seconds = ms / 1000
        if seconds < 60:
            return f"{seconds:.1f}s"
        minutes = int(seconds // 60)
        remaining_seconds = int(seconds % 60)
        if minutes < 60:
            return f"{minutes}m {remaining_seconds}s"
        hours = minutes // 60
        remaining_minutes = minutes % 60
        return f"{hours}h {remaining_minutes}m"


class UtilityJobLog(Base):
    """Structured log entry for a utility job.

    Provides per-job log storage (separate from application logs) for
    display in the job queue UI and diagnostic downloads.
    """

    __tablename__ = "utility_job_logs"
    __table_args__ = (
        Index("ix_utility_job_logs_job_id", "job_id"),
        Index("ix_utility_job_logs_job_id_level", "job_id", "level"),
        Index("ix_utility_job_logs_job_id_timestamp", "job_id", "timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("utility_jobs.id", ondelete="CASCADE"), nullable=False
    )
    item_id: Mapped[str | None] = mapped_column(
        ForeignKey("utility_job_items.id", ondelete="SET NULL"), nullable=True
    )
    timestamp: Mapped[str | None] = mapped_column(Text)
    level: Mapped[str] = mapped_column(Text, nullable=False, default=LogLevel.INFO)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    file_path: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[str | None] = mapped_column(Text, default="{}")

    # Relationships
    job: Mapped[UtilityJob] = relationship(back_populates="logs")
