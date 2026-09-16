"""Review-only import staging models for story-arc evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from pullbox.core.story_arc_identity import normalize_story_arc_name
from pullbox.models.base import Base, IdentityMixin, TimestampMixin
from pullbox.models.story_arc import (
    ImportedStoryArcStatus,
    IssueStoryArc,
    StoryArc,
    StoryArcResolutionState,
    StoryArcSourceKind,
    story_arc_enum,
)

if TYPE_CHECKING:
    from pullbox.models.import_job import ImportedFile, ImportJob
    from pullbox.models.issue import Issue


class ImportedStoryArc(Base, IdentityMixin, TimestampMixin):
    """One detected arc held for review before Step 4 materialization."""

    __tablename__ = "import_story_arcs"
    __table_args__ = (
        UniqueConstraint(
            "import_job_id",
            "source_key",
            name="uq_import_story_arcs_job_source_key",
        ),
        Index("ix_import_story_arcs_job_status_id", "import_job_id", "status", "id"),
        Index(
            "ix_import_story_arcs_job_normalized_id",
            "import_job_id",
            "normalized_name",
            "id",
        ),
    )

    import_job_id: Mapped[int] = mapped_column(
        ForeignKey("import_jobs.id", ondelete="CASCADE"), nullable=False
    )
    source_kind: Mapped[StoryArcSourceKind] = mapped_column(
        story_arc_enum(StoryArcSourceKind), nullable=False
    )
    source_key: Mapped[str] = mapped_column(String(255), nullable=False)
    source_arc_id: Mapped[str | None] = mapped_column(String(255))
    source_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str | None] = mapped_column(String(500))
    normalized_name: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ImportedStoryArcStatus] = mapped_column(
        story_arc_enum(ImportedStoryArcStatus),
        default=ImportedStoryArcStatus.DETECTED,
        server_default=ImportedStoryArcStatus.DETECTED.value,
        nullable=False,
    )
    selected_for_import: Mapped[bool] = mapped_column(
        default=False, server_default="0", nullable=False
    )
    proposed_story_arc_id: Mapped[int | None] = mapped_column(
        ForeignKey("story_arcs.id", ondelete="SET NULL")
    )
    materialized_story_arc_id: Mapped[int | None] = mapped_column(
        ForeignKey("story_arcs.id", ondelete="SET NULL")
    )
    proposed_policy_snapshot: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSON, default=dict, server_default="{}", nullable=False
    )
    source_settings_snapshot: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSON, default=dict, server_default="{}", nullable=False
    )
    diagnostics: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSON, default=dict, server_default="{}", nullable=False
    )

    import_job: Mapped[ImportJob] = relationship(back_populates="story_arcs")
    entries: Mapped[list[ImportedStoryArcEntry]] = relationship(
        back_populates="imported_story_arc",
        cascade="all, delete-orphan",
        order_by=lambda: (
            ImportedStoryArcEntry.source_ordinal,
            ImportedStoryArcEntry.id,
        ),
    )
    proposed_story_arc: Mapped[StoryArc | None] = relationship(
        back_populates="proposed_imports",
        foreign_keys=[proposed_story_arc_id],
    )
    materialized_story_arc: Mapped[StoryArc | None] = relationship(
        back_populates="materialized_imports",
        foreign_keys=[materialized_story_arc_id],
    )

    @validates("name")
    def _normalize_name(self, _key: str, value: str | None) -> str | None:
        self.normalized_name = normalize_story_arc_name(value) if value is not None else None
        return value


class ImportedStoryArcEntry(Base, IdentityMixin, TimestampMixin):
    """One staged ordered entry with review evidence and no final side effect."""

    __tablename__ = "import_story_arc_entries"
    __table_args__ = (
        UniqueConstraint(
            "imported_story_arc_id",
            "source_ordinal",
            name="uq_import_story_arc_entries_arc_ordinal",
        ),
        Index(
            "ix_import_story_arc_entries_arc_resolution_order",
            "imported_story_arc_id",
            "resolution_state",
            "reading_order",
            "source_ordinal",
            "id",
        ),
        Index("ix_import_story_arc_entries_import_file_id", "import_file_id"),
        Index("ix_import_story_arc_entries_matched_issue_id", "matched_issue_id", "id"),
    )

    imported_story_arc_id: Mapped[int] = mapped_column(
        ForeignKey("import_story_arcs.id", ondelete="CASCADE"), nullable=False
    )
    import_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_files.id", ondelete="SET NULL")
    )
    matched_issue_id: Mapped[int | None] = mapped_column(
        ForeignKey("issues.id", ondelete="SET NULL")
    )
    materialized_membership_id: Mapped[int | None] = mapped_column(
        ForeignKey("issue_story_arcs.id", ondelete="SET NULL")
    )
    source_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    reading_order: Mapped[int | None] = mapped_column(Integer)
    reading_order_raw: Mapped[str | None] = mapped_column(String(50))
    resolution_state: Mapped[StoryArcResolutionState] = mapped_column(
        story_arc_enum(StoryArcResolutionState),
        default=StoryArcResolutionState.PENDING,
        server_default=StoryArcResolutionState.PENDING.value,
        nullable=False,
    )
    source_kind: Mapped[StoryArcSourceKind] = mapped_column(
        story_arc_enum(StoryArcSourceKind), nullable=False
    )
    source_entry_id: Mapped[str | None] = mapped_column(String(255))
    source_arc_id: Mapped[str | None] = mapped_column(String(255))
    source_issue_id: Mapped[str | None] = mapped_column(String(255))
    source_series_id: Mapped[str | None] = mapped_column(String(255))
    source_issue_number_text: Mapped[str | None] = mapped_column(String(320))
    source_series_name: Mapped[str | None] = mapped_column(String(500))
    source_issue_title: Mapped[str | None] = mapped_column(String(500))
    source_publisher: Mapped[str | None] = mapped_column(String(255))
    source_release_date_text: Mapped[str | None] = mapped_column(String(50))
    source_issue_date_text: Mapped[str | None] = mapped_column(String(50))
    resolution_confidence: Mapped[float | None] = mapped_column(Float)
    resolution_method: Mapped[str | None] = mapped_column(String(50))
    evidence: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSON, default=dict, server_default="{}", nullable=False
    )
    source_location: Mapped[str | None] = mapped_column(String(1000))
    selected_for_import: Mapped[bool] = mapped_column(
        default=False, server_default="0", nullable=False
    )
    diagnostics: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSON, default=dict, server_default="{}", nullable=False
    )

    imported_story_arc: Mapped[ImportedStoryArc] = relationship(back_populates="entries")
    import_file: Mapped[ImportedFile | None] = relationship(
        back_populates="story_arc_entries",
        foreign_keys=[import_file_id],
    )
    matched_issue: Mapped[Issue | None] = relationship(foreign_keys=[matched_issue_id])
    materialized_membership: Mapped[IssueStoryArc | None] = relationship(
        foreign_keys=[materialized_membership_id]
    )
