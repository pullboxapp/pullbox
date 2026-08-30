"""First-class story-arc, membership, identity, and placement ORM models."""

from __future__ import annotations

import enum
from datetime import datetime  # noqa: TC003 - SQLAlchemy needs this at runtime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from pullbox.core.story_arc_identity import normalize_story_arc_name
from pullbox.models.base import Base, IdentityMixin, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection
    from sqlalchemy.orm import Mapper

    from pullbox.models.import_job import ImportJob, ImportJobAction
    from pullbox.models.issue import Issue
    from pullbox.models.library import LibraryFile, LibraryRoot
    from pullbox.models.story_arc_import import ImportedStoryArc


class StoryArcSourceKind(enum.StrEnum):
    """Durable provenance for an arc, membership, import row, or placement."""

    LEGACY = "legacy"
    PULLBOX = "pullbox"
    MYLAR3 = "mylar3"
    FOLDER = "folder"
    COMICINFO = "comicinfo"
    PROVIDER = "provider"


class StoryArcLifecycle(enum.StrEnum):
    """User-visible lifecycle of a canonical story arc."""

    ACTIVE = "active"
    ARCHIVED = "archived"


class StoryArcResolutionState(enum.StrEnum):
    """Canonical issue-resolution state for an ordered arc entry."""

    PENDING = "pending"
    RESOLVED = "resolved"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    CONFLICT = "conflict"
    SKIPPED = "skipped"


class ImportedStoryArcStatus(enum.StrEnum):
    """Review and execution state for staged story-arc evidence."""

    DETECTED = "detected"
    NEEDS_REVIEW = "needs_review"
    READY = "ready"
    CONFIRMED = "confirmed"
    SKIPPED = "skipped"
    IMPORTED = "imported"
    FAILED = "failed"


class StoryArcPlacementMode(enum.StrEnum):
    """How an optional arc placement represents the canonical file."""

    COPY = "copy"
    HARDLINK = "hardlink"
    SYMLINK = "symlink"
    REFERENCE_ONLY = "reference_only"


class StoryArcSymlinkStyle(enum.StrEnum):
    """How a future managed symlink target is rendered."""

    ABSOLUTE = "absolute"
    RELATIVE = "relative"


class StoryArcPlacementOwnership(enum.StrEnum):
    """Whether Pullbox owns a story-arc placement artifact."""

    MANAGED = "managed"
    REFERENCED = "referenced"


class StoryArcPlacementState(enum.StrEnum):
    """Observed synchronization state of an arc placement."""

    CURRENT = "current"
    MISSING = "missing"
    DRIFTED = "drifted"
    FAILED = "failed"


def story_arc_enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    """Persist lowercase enum values instead of Python member names."""
    return [str(member.value) for member in enum_cls]


def story_arc_enum(enum_cls: type[enum.Enum]) -> SQLAlchemyEnum:
    """Build a portable constrained VARCHAR enum for SQLite and PostgreSQL."""
    return SQLAlchemyEnum(
        enum_cls,
        values_callable=story_arc_enum_values,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
    )


class StoryArc(Base, IdentityMixin, TimestampMixin):
    """A first-class ordered collection that references canonical issues."""

    __tablename__ = "story_arcs"
    __table_args__ = (
        Index("ix_story_arcs_normalized_id", "normalized_name", "id"),
        Index(
            "ix_story_arcs_lifecycle_monitored_id",
            "lifecycle",
            "monitored",
            "id",
        ),
        Index("ix_story_arcs_source_job_id", "source_import_job_id", "id"),
    )

    comicvine_id: Mapped[int | None] = mapped_column(unique=True, index=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    normalized_name: Mapped[str] = mapped_column(
        String(500), nullable=False, server_default="__legacy__"
    )
    description: Mapped[str | None] = mapped_column(Text)
    publisher_id: Mapped[int | None] = mapped_column(
        ForeignKey("publishers.id", ondelete="SET NULL")
    )
    comicvine_url: Mapped[str | None] = mapped_column(String(500))
    source_kind: Mapped[StoryArcSourceKind] = mapped_column(
        story_arc_enum(StoryArcSourceKind),
        default=StoryArcSourceKind.LEGACY,
        server_default=StoryArcSourceKind.LEGACY.value,
        nullable=False,
    )
    lifecycle: Mapped[StoryArcLifecycle] = mapped_column(
        story_arc_enum(StoryArcLifecycle),
        default=StoryArcLifecycle.ACTIVE,
        server_default=StoryArcLifecycle.ACTIVE.value,
        nullable=False,
    )
    monitored: Mapped[bool] = mapped_column(default=False, server_default="0", nullable=False)
    search_missing: Mapped[bool] = mapped_column(default=False, server_default="0", nullable=False)
    include_upcoming: Mapped[bool] = mapped_column(
        default=False, server_default="0", nullable=False
    )
    sync_enabled: Mapped[bool] = mapped_column(default=False, server_default="0", nullable=False)
    target_library_root_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_roots.id", ondelete="SET NULL")
    )
    policy_schema_version: Mapped[int | None] = mapped_column(Integer)
    policy_snapshot: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSON, default=dict, server_default="{}", nullable=False
    )
    source_import_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_jobs.id", ondelete="SET NULL")
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    diagnostics: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSON, default=dict, server_default="{}", nullable=False
    )

    memberships: Mapped[list[IssueStoryArc]] = relationship(
        back_populates="story_arc",
        cascade="all, delete-orphan",
        order_by=lambda: (
            IssueStoryArc.sequence_number,
            IssueStoryArc.source_ordinal,
            IssueStoryArc.id,
        ),
    )
    issues: Mapped[list[Issue]] = relationship(
        secondary="issue_story_arcs",
        back_populates="story_arcs",
        viewonly=True,
    )
    external_identities: Mapped[list[StoryArcExternalIdentity]] = relationship(
        back_populates="story_arc", cascade="all, delete-orphan"
    )
    target_library_root: Mapped[LibraryRoot | None] = relationship(
        foreign_keys=[target_library_root_id]
    )
    source_import_job: Mapped[ImportJob | None] = relationship(foreign_keys=[source_import_job_id])
    proposed_imports: Mapped[list[ImportedStoryArc]] = relationship(
        back_populates="proposed_story_arc",
        foreign_keys="ImportedStoryArc.proposed_story_arc_id",
    )
    materialized_imports: Mapped[list[ImportedStoryArc]] = relationship(
        back_populates="materialized_story_arc",
        foreign_keys="ImportedStoryArc.materialized_story_arc_id",
    )

    @validates("name")
    def _normalize_name(self, _key: str, value: str) -> str:
        self.normalized_name = normalize_story_arc_name(value)
        return value


class IssueStoryArc(Base, IdentityMixin, TimestampMixin):
    """One deterministic ordered arc entry, resolved or unresolved."""

    __tablename__ = "issue_story_arcs"
    __table_args__ = (
        UniqueConstraint(
            "story_arc_id",
            "issue_id",
            name="uq_issue_story_arcs_arc_issue",
        ),
        Index(
            "ix_issue_story_arcs_order",
            "story_arc_id",
            "sequence_number",
            "source_ordinal",
            "id",
        ),
        Index(
            "ix_issue_story_arcs_review",
            "story_arc_id",
            "resolution_state",
            "sequence_number",
            "source_ordinal",
            "id",
        ),
        Index("ix_issue_story_arcs_issue", "issue_id", "story_arc_id", "id"),
    )

    issue_id: Mapped[int | None] = mapped_column(
        ForeignKey("issues.id", ondelete="SET NULL"), nullable=True
    )
    story_arc_id: Mapped[int] = mapped_column(
        ForeignKey("story_arcs.id", ondelete="CASCADE"), nullable=False
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    legacy_sequence_was_null: Mapped[bool] = mapped_column(
        default=False, server_default="0", nullable=False
    )
    resolution_state: Mapped[StoryArcResolutionState] = mapped_column(
        story_arc_enum(StoryArcResolutionState),
        default=StoryArcResolutionState.PENDING,
        server_default=StoryArcResolutionState.PENDING.value,
        nullable=False,
    )
    source_kind: Mapped[StoryArcSourceKind] = mapped_column(
        story_arc_enum(StoryArcSourceKind),
        default=StoryArcSourceKind.LEGACY,
        server_default=StoryArcSourceKind.LEGACY.value,
        nullable=False,
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
    sync_eligible: Mapped[bool] = mapped_column(default=False, server_default="0", nullable=False)
    last_materialization_result: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSON, default=dict, server_default="{}", nullable=False
    )

    story_arc: Mapped[StoryArc] = relationship(back_populates="memberships")
    issue: Mapped[Issue | None] = relationship(back_populates="story_arc_memberships")
    placements: Mapped[list[StoryArcPlacement]] = relationship(
        back_populates="issue_story_arc", cascade="all, delete-orphan"
    )


class StoryArcExternalIdentity(Base, IdentityMixin, TimestampMixin):
    """Provider-neutral external identity attached to a canonical story arc."""

    __tablename__ = "story_arc_external_identities"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "namespace",
            "external_id",
            name="uq_story_arc_external_identity",
        ),
        Index("ix_story_arc_external_identities_arc_id", "story_arc_id", "id"),
    )

    story_arc_id: Mapped[int] = mapped_column(
        ForeignKey("story_arcs.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    namespace: Mapped[str] = mapped_column(String(100), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1000))
    evidence: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSON, default=dict, server_default="{}", nullable=False
    )

    story_arc: Mapped[StoryArc] = relationship(back_populates="external_identities")


class StoryArcPlacement(Base, IdentityMixin, TimestampMixin):
    """Optional representation of a canonical issue in an arc location.

    Database constraints are authoritative for the complete placement matrix.
    ORM writes also validate the final combination immediately before insert or
    update so keyword and attribute assignment order cannot change the result.
    """

    __tablename__ = "story_arc_placements"
    __table_args__ = (
        UniqueConstraint("placement_path", name="uq_story_arc_placements_path"),
        CheckConstraint(
            "((mode = 'reference_only' AND ownership = 'referenced') OR "
            "(mode IN ('copy', 'hardlink', 'symlink') AND ownership = 'managed'))",
            name="ck_story_arc_placements_mode_ownership",
        ),
        CheckConstraint(
            "((mode = 'symlink' AND symlink_style IS NOT NULL) OR "
            "(mode != 'symlink' AND symlink_style IS NULL))",
            name="ck_story_arc_placements_symlink_style",
        ),
        Index("ix_story_arc_placements_membership", "issue_story_arc_id", "id"),
        Index("ix_story_arc_placements_library_file", "library_file_id", "id"),
        Index("ix_story_arc_placements_state", "state", "id"),
    )

    issue_story_arc_id: Mapped[int] = mapped_column(
        ForeignKey("issue_story_arcs.id", ondelete="CASCADE"), nullable=False
    )
    library_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_files.id", ondelete="SET NULL")
    )
    library_root_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_roots.id", ondelete="SET NULL")
    )
    placement_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    mode: Mapped[StoryArcPlacementMode] = mapped_column(
        story_arc_enum(StoryArcPlacementMode),
        default=StoryArcPlacementMode.REFERENCE_ONLY,
        server_default=StoryArcPlacementMode.REFERENCE_ONLY.value,
        nullable=False,
    )
    ownership: Mapped[StoryArcPlacementOwnership] = mapped_column(
        story_arc_enum(StoryArcPlacementOwnership),
        default=StoryArcPlacementOwnership.REFERENCED,
        server_default=StoryArcPlacementOwnership.REFERENCED.value,
        nullable=False,
    )
    symlink_style: Mapped[StoryArcSymlinkStyle | None] = mapped_column(
        story_arc_enum(StoryArcSymlinkStyle)
    )
    source_kind: Mapped[StoryArcSourceKind] = mapped_column(
        story_arc_enum(StoryArcSourceKind),
        default=StoryArcSourceKind.LEGACY,
        server_default=StoryArcSourceKind.LEGACY.value,
        nullable=False,
    )
    source_import_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_jobs.id", ondelete="SET NULL")
    )
    creating_action_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_job_actions.id", ondelete="SET NULL")
    )
    rendered_reading_order: Mapped[int | None] = mapped_column(Integer)
    policy_schema_version: Mapped[int | None] = mapped_column(Integer)
    operation_token: Mapped[str | None] = mapped_column(String(32))
    source_fingerprint: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSON, default=dict, server_default="{}", nullable=False
    )
    state: Mapped[StoryArcPlacementState] = mapped_column(
        story_arc_enum(StoryArcPlacementState),
        default=StoryArcPlacementState.CURRENT,
        server_default=StoryArcPlacementState.CURRENT.value,
        nullable=False,
    )
    last_result: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSON, default=dict, server_default="{}", nullable=False
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    issue_story_arc: Mapped[IssueStoryArc] = relationship(back_populates="placements")
    library_file: Mapped[LibraryFile | None] = relationship(foreign_keys=[library_file_id])
    library_root: Mapped[LibraryRoot | None] = relationship(foreign_keys=[library_root_id])
    source_import_job: Mapped[ImportJob | None] = relationship(foreign_keys=[source_import_job_id])
    creating_action: Mapped[ImportJobAction | None] = relationship(
        foreign_keys=[creating_action_id]
    )

    def validate_configuration(self) -> None:
        """Validate one complete placement combination independent of assignment order."""
        raw_mode = self.__dict__.get("mode", StoryArcPlacementMode.REFERENCE_ONLY)
        raw_ownership = self.__dict__.get(
            "ownership",
            StoryArcPlacementOwnership.REFERENCED,
        )
        raw_symlink_style = self.__dict__.get("symlink_style")
        try:
            mode = StoryArcPlacementMode(raw_mode)
            ownership = StoryArcPlacementOwnership(raw_ownership)
            symlink_style = (
                StoryArcSymlinkStyle(raw_symlink_style) if raw_symlink_style is not None else None
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Story arc placement uses an unsupported mode or style") from exc

        expected_ownership = (
            StoryArcPlacementOwnership.REFERENCED
            if mode is StoryArcPlacementMode.REFERENCE_ONLY
            else StoryArcPlacementOwnership.MANAGED
        )
        if ownership is not expected_ownership:
            raise ValueError(
                f"Story arc placement mode {mode.value} requires "
                f"{expected_ownership.value} ownership"
            )
        if mode is StoryArcPlacementMode.SYMLINK and symlink_style is None:
            raise ValueError("A symlink story arc placement requires a symlink style")
        if mode is not StoryArcPlacementMode.SYMLINK and symlink_style is not None:
            raise ValueError("Only a symlink story arc placement may specify a symlink style")


@event.listens_for(StoryArcPlacement, "before_insert")
@event.listens_for(StoryArcPlacement, "before_update")
def _validate_story_arc_placement_before_write(
    _mapper: Mapper[StoryArcPlacement],
    _connection: Connection,
    target: StoryArcPlacement,
) -> None:
    """Fail ORM writes before SQL while leaving database checks authoritative."""
    target.validate_configuration()
