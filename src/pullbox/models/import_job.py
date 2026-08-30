"""Import job and imported series ORM models.

These models support the Collection Import feature — a wizard-driven
workflow that scans a user's existing comic collection (filesystem or
Mylar3 database), matches discovered series against ComicVine, and adds
them to the Pullbox library in a reviewed batch.
"""

from __future__ import annotations

import enum
from datetime import datetime  # noqa: TC003 - SQLAlchemy needs this at runtime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pullbox.models.base import Base, IdentityMixin, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from pullbox.models.library import LibraryRoot
    from pullbox.models.series import Series


class ImportSourceType(enum.StrEnum):
    """Type of import source."""

    FILESYSTEM = "filesystem"
    MYLAR3 = "mylar3"


class ImportFileHandlingMode(enum.StrEnum):
    """How files selected by an import become Pullbox library files."""

    MANAGED_COPY = "managed_copy"
    IN_PLACE = "in_place"


class ImportJobStatus(enum.StrEnum):
    """Lifecycle status of an import job."""

    PENDING = "pending"
    SCANNING = "scanning"
    PAUSING = "pausing"
    PAUSED = "paused"
    ANALYZING = "analyzing"
    MATCHING = "matching"
    FILE_MATCHING = "file_matching"
    REVIEW = "review"
    IMPORTING = "importing"
    STALLED = "stalled"
    CANCELLING = "cancelling"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ImportSeriesStatus(enum.StrEnum):
    """Status of an individual series candidate within an import job."""

    PENDING = "pending"
    MATCHED = "matched"
    NO_MATCH = "no_match"
    RECOVERY_PENDING = "recovery_pending"
    DUPLICATE = "duplicate"
    CONFIRMED = "confirmed"
    SKIPPED = "skipped"
    IMPORTING = "importing"
    IMPORTED = "imported"
    FAILED = "failed"


class ImportedFileStatus(enum.StrEnum):
    """Status of an individual file within an import job."""

    PENDING = "pending"
    MATCHED = "matched"
    SAFETY_BLOCKED = "safety_blocked"
    SAFETY_APPROVED = "safety_approved"
    DUPLICATE_FILE = "duplicate_file"
    ALREADY_OWNED = "already_owned"
    CONFLICT = "conflict"
    NO_MATCH = "no_match"
    CONFIRMED = "confirmed"
    SKIPPED = "skipped"
    IMPORTED = "imported"
    FAILED = "failed"


class ImportJobActionStatus(enum.StrEnum):
    """Lifecycle state of a recorded import action."""

    COMPLETED = "completed"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"


class ImportControlRequest(enum.StrEnum):
    """Cooperative runtime control signal stored on the import job."""

    NONE = "none"
    PAUSE = "pause"
    CANCEL = "cancel"


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    """Persist SQLAlchemy enums using member values instead of names."""
    return [str(member.value) for member in enum_cls]


class ImportJob(Base, IdentityMixin, TimestampMixin):
    """A collection import job tracking the full scan-match-import lifecycle.

    Each job represents a single import attempt from a filesystem path or
    Mylar3 database. The job progresses through statuses as background tasks
    scan, analyze, match, and ultimately import confirmed series.
    """

    __tablename__ = "import_jobs"

    # Source
    source_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    selected_file_paths: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    source_type: Mapped[ImportSourceType] = mapped_column(
        SQLAlchemyEnum(ImportSourceType), nullable=False
    )
    status: Mapped[ImportJobStatus] = mapped_column(
        SQLAlchemyEnum(ImportJobStatus),
        default=ImportJobStatus.PENDING,
        nullable=False,
    )

    # Scan counters
    scan_total_files: Mapped[int] = mapped_column(Integer, default=0)
    scan_total_dirs: Mapped[int] = mapped_column(Integer, default=0)

    # Series counters
    series_found: Mapped[int] = mapped_column(Integer, default=0)
    series_duplicate: Mapped[int] = mapped_column(Integer, default=0)
    series_matched: Mapped[int] = mapped_column(Integer, default=0)
    series_no_match: Mapped[int] = mapped_column(Integer, default=0)
    series_new: Mapped[int] = mapped_column(Integer, default=0)

    # Import counters
    series_imported: Mapped[int] = mapped_column(Integer, default=0)
    series_failed: Mapped[int] = mapped_column(Integer, default=0)

    # File-level counters
    total_files_found: Mapped[int] = mapped_column(Integer, default=0)
    total_files_matched: Mapped[int] = mapped_column(Integer, default=0)
    total_files_duplicate: Mapped[int] = mapped_column(Integer, default=0)
    total_files_already_owned: Mapped[int] = mapped_column(Integer, default=0)
    total_files_conflict: Mapped[int] = mapped_column(Integer, default=0)
    total_files_no_match: Mapped[int] = mapped_column(Integer, default=0)
    total_files_imported: Mapped[int] = mapped_column(Integer, default=0)
    total_files_failed: Mapped[int] = mapped_column(Integer, default=0)

    # Phase timestamps
    scan_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    scan_completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    match_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    match_completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    import_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    import_completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # Error
    error_message: Mapped[str | None] = mapped_column(Text)
    progress_snapshot: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSON, default=dict, server_default="{}"
    )
    progress_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    control_request: Mapped[ImportControlRequest] = mapped_column(
        SQLAlchemyEnum(ImportControlRequest, values_callable=_enum_values),
        default=ImportControlRequest.NONE,
        server_default=ImportControlRequest.NONE.value,
        nullable=False,
    )

    # Import settings (captured from wizard)
    target_library_root_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_roots.id", ondelete="SET NULL")
    )
    monitored: Mapped[bool] = mapped_column(default=False)
    search_on_add: Mapped[bool] = mapped_column(default=False)
    move_to_library: Mapped[bool] = mapped_column(default=True)
    transfer_method: Mapped[str] = mapped_column(String(20), default="move", server_default="move")
    torrent_import_strategy: Mapped[str] = mapped_column(
        String(20), default="standard", server_default="standard"
    )
    effective_import_strategy: Mapped[str] = mapped_column(
        String(30), default="standard", server_default="standard"
    )
    effective_transfer_method: Mapped[str] = mapped_column(
        String(20), default="move", server_default="move"
    )
    source_preserved: Mapped[bool] = mapped_column(default=False, server_default="0")
    convert_to_preferred_format: Mapped[bool] = mapped_column(default=False)
    update_embedded_comicinfo_from_match: Mapped[bool] = mapped_column(default=False)
    ingest_policy_snapshot: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSON, default=dict, server_default="{}"
    )
    file_handling_mode: Mapped[ImportFileHandlingMode] = mapped_column(
        SQLAlchemyEnum(
            ImportFileHandlingMode,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
        ),
        default=ImportFileHandlingMode.MANAGED_COPY,
        server_default=ImportFileHandlingMode.MANAGED_COPY.value,
        nullable=False,
    )
    source_layout_snapshot: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSON,
        default=lambda: {
            "schema_version": 1,
            "mode": "auto",
            "preset": None,
            "series_path_template": None,
            "issue_filename_template": None,
            "selected_cluster_id": None,
            "fallback_to_auto": True,
        },
        server_default=(
            '{"schema_version":1,"mode":"auto","preset":null,'
            '"series_path_template":null,"issue_filename_template":null,'
            '"selected_cluster_id":null,"fallback_to_auto":true}'
        ),
        nullable=False,
    )
    future_layout_requested: Mapped[bool] = mapped_column(
        default=False,
        server_default="0",
        nullable=False,
    )
    future_root_policy_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # type: ignore[type-arg]
    future_root_policy_applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # Per-job configuration
    cv_match_threshold: Mapped[float] = mapped_column(Float, default=0.70)
    auto_accept_high_confidence: Mapped[bool] = mapped_column(default=True)
    skip_no_match: Mapped[bool] = mapped_column(default=False)
    min_files_per_series: Mapped[int] = mapped_column(Integer, default=1)
    file_formats: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Mylar3 path mapping for Docker volume translation
    mylar3_path_map: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSON, default=dict, server_default="{}"
    )

    # Relationships
    series: Mapped[list[ImportedSeries]] = relationship(
        back_populates="import_job", cascade="all, delete-orphan"
    )
    files: Mapped[list[ImportedFile]] = relationship(
        back_populates="import_job", cascade="all, delete-orphan"
    )
    target_library_root: Mapped[LibraryRoot | None] = relationship()
    logs: Mapped[list[ImportJobLog]] = relationship(
        back_populates="import_job",
        cascade="all, delete-orphan",
        order_by="ImportJobLog.logged_at",
    )
    actions: Mapped[list[ImportJobAction]] = relationship(
        back_populates="import_job",
        cascade="all, delete-orphan",
        order_by="ImportJobAction.sequence_no",
    )


class ImportJobLog(Base, IdentityMixin):
    """Structured log entry for a single import job event.

    Written by ImportService._log_event() during all import phases.
    Dual-writes alongside structlog so per-job logs can be viewed and
    downloaded in the import history UI without reading the server log file.
    """

    __tablename__ = "import_job_logs"
    __table_args__ = (Index("ix_import_job_logs_job_id_ts", "import_job_id", "logged_at"),)

    import_job_id: Mapped[int] = mapped_column(
        ForeignKey("import_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    logged_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=func.now())
    level: Mapped[str] = mapped_column(String(10), nullable=False)
    event: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSON, default=dict, server_default="{}"
    )

    # Relationships
    import_job: Mapped[ImportJob] = relationship(back_populates="logs")


class ImportJobAction(Base, IdentityMixin, TimestampMixin):
    """Durable execution journal entry for import rollback and recovery."""

    __tablename__ = "import_job_actions"
    __table_args__ = (
        Index("ix_import_job_actions_job_seq", "import_job_id", "sequence_no"),
        Index("ix_import_job_actions_job_status", "import_job_id", "status"),
    )

    import_job_id: Mapped[int] = mapped_column(
        ForeignKey("import_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(50), nullable=False)
    action_type: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[ImportJobActionStatus] = mapped_column(
        SQLAlchemyEnum(ImportJobActionStatus),
        default=ImportJobActionStatus.COMPLETED,
        nullable=False,
    )
    payload: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSON, default=dict, server_default="{}"
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    rolled_back_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    import_job: Mapped[ImportJob] = relationship(back_populates="actions")


class ImportedSeries(Base, IdentityMixin, TimestampMixin):
    """A series candidate discovered during an import scan.

    Tracks the full lifecycle of a discovered series from initial scan
    through CV matching, user review, and final import into Pullbox.
    """

    __tablename__ = "import_series"
    __table_args__ = (Index("ix_import_series_job_status", "import_job_id", "status"),)

    # Parent job
    import_job_id: Mapped[int] = mapped_column(
        ForeignKey("import_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[ImportSeriesStatus] = mapped_column(
        SQLAlchemyEnum(ImportSeriesStatus), default=ImportSeriesStatus.PENDING
    )

    # Raw data extracted from scan
    raw_series_name: Mapped[str] = mapped_column(String(500), nullable=False)
    raw_year: Mapped[int | None] = mapped_column(Integer)
    raw_publisher: Mapped[str | None] = mapped_column(String(255))
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    sample_paths: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    source_folder: Mapped[str | None] = mapped_column(String(1000))
    has_files: Mapped[bool] = mapped_column(default=True)

    # ComicVine match results
    cv_id: Mapped[int | None] = mapped_column(Integer, index=True)
    cv_title: Mapped[str | None] = mapped_column(String(500))
    cv_year: Mapped[int | None] = mapped_column(Integer)
    cv_publisher: Mapped[str | None] = mapped_column(String(255))
    cv_issue_count: Mapped[int | None] = mapped_column(Integer)
    cv_url: Mapped[str | None] = mapped_column(String(500))
    cv_match_score: Mapped[float | None] = mapped_column(Float)
    cv_match_method: Mapped[str | None] = mapped_column(String(50))

    # User override
    user_selected_cv_id: Mapped[int | None] = mapped_column(Integer)
    selected_for_import: Mapped[bool] = mapped_column(default=False, server_default="0")

    # File-level counters
    files_total: Mapped[int] = mapped_column(Integer, default=0)
    files_matched: Mapped[int] = mapped_column(Integer, default=0)
    files_duplicate: Mapped[int] = mapped_column(Integer, default=0)
    files_already_owned: Mapped[int] = mapped_column(Integer, default=0)
    files_conflict: Mapped[int] = mapped_column(Integer, default=0)
    files_no_match: Mapped[int] = mapped_column(Integer, default=0)
    files_imported: Mapped[int] = mapped_column(Integer, default=0)
    files_failed: Mapped[int] = mapped_column(Integer, default=0)

    # Import outcome
    series_id: Mapped[int | None] = mapped_column(
        ForeignKey("series.id", ondelete="SET NULL"), index=True
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    diagnostics: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSON, default=dict, server_default="{}"
    )

    # Relationships
    import_job: Mapped[ImportJob] = relationship(back_populates="series")
    series: Mapped[Series | None] = relationship()
    files: Mapped[list[ImportedFile]] = relationship(
        back_populates="import_series", cascade="all, delete-orphan"
    )


class ImportedFile(Base, IdentityMixin, TimestampMixin):
    """A single comic file discovered during an import scan.

    Tracks per-file metadata, matching results, conflict state, and
    import outcome throughout the import lifecycle.
    """

    __tablename__ = "import_files"
    __table_args__ = (Index("ix_import_files_job_series", "import_job_id", "import_series_id"),)

    # Parent references
    import_job_id: Mapped[int] = mapped_column(
        ForeignKey("import_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    import_series_id: Mapped[int] = mapped_column(
        ForeignKey("import_series.id", ondelete="CASCADE"),
        nullable=False,
    )

    # File metadata (populated from DiscoveredFile during scan)
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    file_name: Mapped[str] = mapped_column(String(500), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    file_format: Mapped[str] = mapped_column(String(10), nullable=False)

    # Parsed metadata from filename
    parsed_series: Mapped[str | None] = mapped_column(String(500))
    parsed_issue_number: Mapped[float | None] = mapped_column(Float)
    parsed_year: Mapped[int | None] = mapped_column(Integer)

    # ComicInfo.xml metadata
    has_comicinfo: Mapped[bool] = mapped_column(default=False)
    comicvine_issue_id: Mapped[int | None] = mapped_column(Integer)
    issue_number_raw: Mapped[str | None] = mapped_column(String(50))

    # Matching results (populated during FILE_MATCHING phase)
    status: Mapped[ImportedFileStatus] = mapped_column(
        SQLAlchemyEnum(ImportedFileStatus),
        default=ImportedFileStatus.PENDING,
        nullable=False,
    )
    matched_issue_id: Mapped[int | None] = mapped_column(
        ForeignKey("issues.id", ondelete="SET NULL"),
    )
    matched_issue_cv_id: Mapped[int | None] = mapped_column(Integer)
    match_confidence: Mapped[str | None] = mapped_column(String(20))
    match_method: Mapped[str | None] = mapped_column(String(50))

    # Conflict resolution
    conflict_group_id: Mapped[int | None] = mapped_column(Integer)
    duplicate_group_id: Mapped[int | None] = mapped_column(Integer)
    duplicate_of_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_files.id", ondelete="SET NULL"),
    )
    is_preferred: Mapped[bool] = mapped_column(default=False)
    include_in_import: Mapped[bool] = mapped_column(default=False, server_default="0")
    content_hash: Mapped[str | None] = mapped_column(String(64))

    # Import outcome
    library_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_files.id", ondelete="SET NULL"),
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    diagnostics: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSON, default=dict, server_default="{}"
    )

    # Relationships
    import_job: Mapped[ImportJob] = relationship(back_populates="files")
    import_series: Mapped[ImportedSeries] = relationship(back_populates="files")
