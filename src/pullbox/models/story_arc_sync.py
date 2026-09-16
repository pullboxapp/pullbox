"""Durable work records for automatic story-arc placement synchronization."""

from __future__ import annotations

import enum
from datetime import datetime  # noqa: TC003 - SQLAlchemy needs this at runtime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pullbox.models.base import Base, IdentityMixin, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from pullbox.models.import_job import ImportJobAction
    from pullbox.models.library import LibraryFile
    from pullbox.models.story_arc import IssueStoryArc


class StoryArcSyncWorkState(enum.StrEnum):
    """Durable lifecycle for one desired story-arc placement generation."""

    QUEUED = "queued"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class StoryArcSyncReason(enum.StrEnum):
    """Why automatic synchronization work was created."""

    CANONICAL_REGISTERED = "canonical_registered"
    DISCREPANCY_RECOVERY = "discrepancy_recovery"


def _enum(enum_cls: type[enum.Enum]) -> SQLAlchemyEnum:
    return SQLAlchemyEnum(
        enum_cls,
        values_callable=lambda cls: [str(member.value) for member in cls],
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
    )


class StoryArcSyncWork(Base, IdentityMixin, TimestampMixin):
    """One idempotent, leased request to synchronize a canonical file into an arc."""

    __tablename__ = "story_arc_sync_work"
    __table_args__ = (
        UniqueConstraint(
            "issue_story_arc_id",
            "desired_generation",
            name="uq_story_arc_sync_work_generation",
        ),
        UniqueConstraint(
            "origin_import_action_id",
            name="uq_story_arc_sync_work_origin_import_action",
        ),
        Index(
            "ix_story_arc_sync_work_queued",
            "claimable",
            "state",
            "created_at",
            "id",
        ),
        Index(
            "ix_story_arc_sync_work_ready",
            "claimable",
            "state",
            "next_attempt_at",
            "id",
        ),
        Index(
            "ix_story_arc_sync_work_stale_claim",
            "claimable",
            "state",
            "claimed_at",
            "id",
        ),
        Index(
            "ix_story_arc_sync_work_origin_job_state",
            "origin_import_job_id",
            "state",
            "id",
        ),
        Index(
            "ix_story_arc_sync_work_membership",
            "issue_story_arc_id",
            "id",
        ),
        Index(
            "ix_story_arc_sync_work_library_file",
            "library_file_id",
            "id",
        ),
    )

    issue_story_arc_id: Mapped[int] = mapped_column(
        ForeignKey("issue_story_arcs.id", ondelete="CASCADE"),
        nullable=False,
    )
    library_file_id: Mapped[int] = mapped_column(
        ForeignKey("library_files.id", ondelete="CASCADE"),
        nullable=False,
    )
    origin_import_action_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_job_actions.id", ondelete="SET NULL")
    )
    origin_import_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_jobs.id", ondelete="SET NULL")
    )
    origin_imported_story_arc_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_story_arcs.id", ondelete="SET NULL")
    )
    origin_imported_story_arc_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_story_arc_entries.id", ondelete="SET NULL")
    )
    desired_generation: Mapped[str] = mapped_column(String(64), nullable=False)
    source_signature_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_file_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    source_file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_file_modified_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    source_file_hash: Mapped[str | None] = mapped_column(String(64))
    source_signature_schema_version: Mapped[int | None] = mapped_column(Integer)
    source_signature_resolved_path: Mapped[str | None] = mapped_column(String(1000))
    source_signature_size: Mapped[int | None] = mapped_column(BigInteger)
    source_signature_mtime_ns: Mapped[int | None] = mapped_column(BigInteger)
    source_signature_device: Mapped[int | None] = mapped_column(BigInteger)
    source_signature_inode: Mapped[int | None] = mapped_column(BigInteger)
    story_arc_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    membership_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[StoryArcSyncReason] = mapped_column(
        _enum(StoryArcSyncReason),
        default=StoryArcSyncReason.CANONICAL_REGISTERED,
        server_default=StoryArcSyncReason.CANONICAL_REGISTERED.value,
        nullable=False,
    )
    state: Mapped[StoryArcSyncWorkState] = mapped_column(
        _enum(StoryArcSyncWorkState),
        default=StoryArcSyncWorkState.QUEUED,
        server_default=StoryArcSyncWorkState.QUEUED.value,
        nullable=False,
    )
    claimable: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="1",
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    claim_token: Mapped[str | None] = mapped_column(String(64))
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_category: Mapped[str | None] = mapped_column(String(50))
    last_error_detail: Mapped[str | None] = mapped_column(Text)
    last_result: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSON,
        default=dict,
        server_default="{}",
        nullable=False,
    )

    issue_story_arc: Mapped[IssueStoryArc] = relationship()
    library_file: Mapped[LibraryFile] = relationship()
    origin_import_action: Mapped[ImportJobAction | None] = relationship(
        foreign_keys=[origin_import_action_id]
    )
