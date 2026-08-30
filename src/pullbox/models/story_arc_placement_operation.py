"""Durable coordination and journals for arc-wide placement operations.

The coordinator is authoritative execution state.  ``OperationProgress`` may
project it for presentation later, but it is intentionally not part of the
recovery graph.  Action path columns are private operational evidence and must
not be copied into progress snapshots, ordinary logs, or diagnostics without
the established redaction rules.
"""

from __future__ import annotations

import enum
from datetime import datetime  # noqa: TC003 - SQLAlchemy needs this at runtime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
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
from pullbox.models.story_arc import (
    StoryArcPlacementMode,
    StoryArcPlacementOperationKind,
    StoryArcPlacementOwnership,
)

if TYPE_CHECKING:
    from pullbox.models.story_arc import StoryArc
    from pullbox.models.user import User


class StoryArcPlacementOperationState(enum.StrEnum):
    """Authoritative lifecycle for one fenced arc-wide operation."""

    BUILDING = "building"
    READY = "ready"
    RUNNING = "running"
    RECONCILING = "reconciling"
    CLEANUP_PENDING = "cleanup_pending"
    CANCEL_REQUESTED = "cancel_requested"
    ROLLING_BACK = "rolling_back"
    RECOVERY_REQUIRED = "recovery_required"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class StoryArcPlacementOperationRecoveryMode(enum.StrEnum):
    """Safe recovery direction on either side of policy activation."""

    ROLLBACK = "rollback"
    FORWARD_ONLY = "forward_only"


class StoryArcPlacementOperationActionKind(enum.StrEnum):
    """Per-placement consequences recorded by a policy migration plan."""

    MIGRATE_MANAGED = "migrate_managed"
    REBUILD_MANAGED = "rebuild_managed"
    REMOVE_MANAGED = "remove_managed"


class StoryArcPlacementOperationActionState(enum.StrEnum):
    """Durable execution checkpoints for one sequential journal action."""

    PLANNED = "planned"
    RESERVED = "reserved"
    STAGED = "staged"
    PUBLISHED = "published"
    VERIFIED = "verified"
    OLD_REMOVED = "old_removed"
    DB_RECONCILED = "db_reconciled"
    CLEANUP_PENDING = "cleanup_pending"
    CLEANED = "cleaned"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    RECOVERY_REQUIRED = "recovery_required"
    FAILED = "failed"


def _enum_type(enum_class: type[enum.Enum], *, name: str) -> SQLAlchemyEnum:
    return SQLAlchemyEnum(
        enum_class,
        name=name,
        values_callable=lambda cls: [str(member.value) for member in cls],
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
    )


class StoryArcPlacementOperation(Base, IdentityMixin, TimestampMixin):
    """One durable coordinator and active fence for an arc-wide operation.

    The matching token and operation kind on ``StoryArc`` are the authoritative
    generic fence.  Coordinator rows retain history after the fence is released.
    """

    __tablename__ = "story_arc_placement_operations"
    __table_args__ = (
        UniqueConstraint(
            "operation_token",
            name="uq_story_arc_placement_operations_token",
        ),
        UniqueConstraint(
            "story_arc_id",
            "operation_kind",
            "idempotency_key_hash",
            name="uq_story_arc_placement_operations_idempotency",
        ),
        UniqueConstraint(
            "story_arc_id",
            "active_fence_slot",
            name="uq_story_arc_placement_operations_active_fence",
        ),
        CheckConstraint(
            "expected_story_arc_revision >= 1",
            name="ck_story_arc_placement_operations_expected_revision",
        ),
        CheckConstraint(
            "length(operation_token) = 32 AND length(idempotency_key_hash) = 64",
            name="ck_story_arc_placement_operations_token_length",
        ),
        CheckConstraint(
            "length(scope_digest) = 64 AND length(plan_digest) = 64",
            name="ck_story_arc_placement_operations_digest_length",
        ),
        CheckConstraint(
            "total_action_count >= 0 AND completed_action_count >= 0 "
            "AND failed_action_count >= 0 "
            "AND completed_action_count + failed_action_count <= total_action_count "
            "AND required_bytes >= 0 AND attempt_count >= 0",
            name="ck_story_arc_placement_operations_action_counts",
        ),
        CheckConstraint(
            "confirmation_schema_version >= 1",
            name="ck_story_arc_placement_operations_confirmation_schema",
        ),
        CheckConstraint(
            "((recovery_mode = 'rollback' AND policy_applied_at IS NULL "
            "AND applied_policy_revision IS NULL AND applied_policy_digest IS NULL) OR "
            "(recovery_mode = 'forward_only' AND policy_applied_at IS NOT NULL "
            "AND applied_policy_revision IS NOT NULL AND applied_policy_revision >= 1 "
            "AND applied_policy_digest IS NOT NULL "
            "AND length(applied_policy_digest) = 64))",
            name="ck_story_arc_placement_operations_policy_boundary",
        ),
        CheckConstraint(
            "state != 'rolling_back' OR recovery_mode = 'rollback'",
            name="ck_story_arc_placement_operations_rollback_mode",
        ),
        CheckConstraint(
            "state != 'cancelled' OR recovery_mode = 'rollback'",
            name="ck_story_arc_placement_operations_cancel_mode",
        ),
        CheckConstraint(
            "recovery_mode != 'forward_only' OR state IN "
            "('running','reconciling','cleanup_pending','recovery_required','completed')",
            name="ck_story_arc_placement_operations_forward_state",
        ),
        CheckConstraint(
            "((state IN ('completed','cancelled','failed') "
            "AND active_fence_slot IS NULL AND completed_at IS NOT NULL) OR "
            "(state NOT IN ('completed','cancelled','failed') "
            "AND active_fence_slot = 1 AND completed_at IS NULL))",
            name="ck_story_arc_placement_operations_fence_state",
        ),
        CheckConstraint(
            "((claim_token IS NULL AND claimed_at IS NULL "
            "AND claim_heartbeat_at IS NULL AND claim_expires_at IS NULL) OR "
            "(claim_token IS NOT NULL AND claimed_at IS NOT NULL "
            "AND claim_heartbeat_at IS NOT NULL AND claim_expires_at IS NOT NULL))",
            name="ck_story_arc_placement_operations_claim",
        ),
        Index(
            "ix_story_arc_placement_operations_arc_state",
            "story_arc_id",
            "state",
            "id",
        ),
        Index(
            "ix_story_arc_placement_operations_recovery",
            "active_fence_slot",
            "state",
            "next_attempt_at",
            "claim_expires_at",
            "updated_at",
            "id",
        ),
    )

    story_arc_id: Mapped[int] = mapped_column(
        ForeignKey("story_arcs.id", ondelete="RESTRICT"), nullable=False
    )
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    operation_kind: Mapped[StoryArcPlacementOperationKind] = mapped_column(
        _enum_type(
            StoryArcPlacementOperationKind,
            name="storyarcplacementoperationkind",
        ),
        nullable=False,
    )
    state: Mapped[StoryArcPlacementOperationState] = mapped_column(
        _enum_type(
            StoryArcPlacementOperationState,
            name="storyarcplacementoperationstate",
        ),
        default=StoryArcPlacementOperationState.BUILDING,
        server_default=StoryArcPlacementOperationState.BUILDING.value,
        nullable=False,
    )
    active_fence_slot: Mapped[int | None] = mapped_column(
        Integer,
        server_default="1",
    )
    operation_token: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_story_arc_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    confirmation_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    source_policy_snapshot: Mapped[dict[str, object]] = mapped_column(
        JSON,
        default=dict,
        server_default="{}",
        nullable=False,
    )
    proposed_policy_snapshot: Mapped[dict[str, object]] = mapped_column(
        JSON,
        default=dict,
        server_default="{}",
        nullable=False,
    )
    source_library_root_id: Mapped[int | None] = mapped_column(Integer)
    target_library_root_id: Mapped[int | None] = mapped_column(Integer)
    source_root_fingerprint: Mapped[dict[str, object]] = mapped_column(
        JSON,
        default=dict,
        server_default="{}",
        nullable=False,
    )
    target_root_fingerprint: Mapped[dict[str, object]] = mapped_column(
        JSON,
        default=dict,
        server_default="{}",
        nullable=False,
    )
    scope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    recovery_mode: Mapped[StoryArcPlacementOperationRecoveryMode] = mapped_column(
        _enum_type(
            StoryArcPlacementOperationRecoveryMode,
            name="storyarcplacementoperationrecoverymode",
        ),
        default=StoryArcPlacementOperationRecoveryMode.ROLLBACK,
        server_default=StoryArcPlacementOperationRecoveryMode.ROLLBACK.value,
        nullable=False,
    )
    claim_token: Mapped[str | None] = mapped_column(String(64))
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    claim_heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    claim_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    policy_applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    applied_policy_revision: Mapped[int | None] = mapped_column(Integer)
    applied_policy_digest: Mapped[str | None] = mapped_column(String(64))
    total_action_count: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default="0",
        nullable=False,
    )
    completed_action_count: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default="0",
        nullable=False,
    )
    failed_action_count: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default="0",
        nullable=False,
    )
    required_bytes: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default="0",
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_category: Mapped[str | None] = mapped_column(String(50))
    last_error_detail: Mapped[str | None] = mapped_column(Text)
    last_result: Mapped[dict[str, object]] = mapped_column(
        JSON,
        default=dict,
        server_default="{}",
        nullable=False,
    )

    story_arc: Mapped[StoryArc] = relationship(back_populates="placement_operations")
    actor_user: Mapped[User | None] = relationship(foreign_keys=[actor_user_id])
    actions: Mapped[list[StoryArcPlacementOperationAction]] = relationship(
        back_populates="operation",
        cascade="all, delete-orphan",
        order_by=lambda: (
            StoryArcPlacementOperationAction.sequence_number,
            StoryArcPlacementOperationAction.id,
        ),
    )


class StoryArcPlacementOperationAction(Base, IdentityMixin, TimestampMixin):
    """One ordered, private recovery record in an arc-wide operation plan."""

    __tablename__ = "story_arc_placement_operation_actions"
    __table_args__ = (
        UniqueConstraint(
            "operation_id",
            "sequence_number",
            name="uq_story_arc_placement_operation_actions_sequence",
        ),
        UniqueConstraint(
            "operation_id",
            "target_path_exact_hash",
            name="uq_story_arc_placement_operation_actions_target_exact",
        ),
        UniqueConstraint(
            "operation_id",
            "target_path_casefold_hash",
            name="uq_story_arc_placement_operation_actions_target_casefold",
        ),
        UniqueConstraint(
            "operation_id",
            "source_placement_id",
            name="uq_story_arc_placement_operation_actions_source",
        ),
        CheckConstraint(
            "sequence_number >= 1",
            name="ck_story_arc_placement_operation_actions_sequence",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_story_arc_placement_operation_actions_attempt_count",
        ),
        CheckConstraint(
            "required_bytes >= 0",
            name="ck_story_arc_placement_operation_actions_required_bytes",
        ),
        CheckConstraint(
            "((action_kind = 'remove_managed' AND target_path IS NULL "
            "AND target_path_exact_hash IS NULL AND target_path_casefold_hash IS NULL "
            "AND target_mode IS NULL AND target_ownership IS NULL) OR "
            "(action_kind IN ('migrate_managed','rebuild_managed') "
            "AND target_path IS NOT NULL "
            "AND target_path_exact_hash IS NOT NULL "
            "AND length(target_path_exact_hash) = 64 "
            "AND target_path_casefold_hash IS NOT NULL "
            "AND length(target_path_casefold_hash) = 64 "
            "AND target_mode IS NOT NULL AND target_ownership IS NOT NULL))",
            name="ck_story_arc_placement_operation_actions_target_shape",
        ),
        CheckConstraint(
            "source_mode IN ('copy','hardlink','symlink') "
            "AND source_ownership = 'managed' "
            "AND (target_ownership IS NULL OR target_ownership = 'managed')",
            name="ck_story_arc_placement_operation_actions_ownership",
        ),
        CheckConstraint(
            "((claim_token IS NULL AND claimed_at IS NULL "
            "AND claim_heartbeat_at IS NULL AND claim_expires_at IS NULL) OR "
            "(claim_token IS NOT NULL AND claimed_at IS NOT NULL "
            "AND claim_heartbeat_at IS NOT NULL AND claim_expires_at IS NOT NULL))",
            name="ck_story_arc_placement_operation_actions_claim",
        ),
        Index(
            "ix_story_arc_placement_operation_actions_next",
            "operation_id",
            "state",
            "sequence_number",
            "id",
        ),
        Index(
            "ix_story_arc_placement_operation_actions_source",
            "operation_id",
            "source_placement_id",
            "id",
        ),
    )

    operation_id: Mapped[int] = mapped_column(
        ForeignKey("story_arc_placement_operations.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    action_kind: Mapped[StoryArcPlacementOperationActionKind] = mapped_column(
        _enum_type(
            StoryArcPlacementOperationActionKind,
            name="storyarcplacementoperationactionkind",
        ),
        nullable=False,
    )
    state: Mapped[StoryArcPlacementOperationActionState] = mapped_column(
        _enum_type(
            StoryArcPlacementOperationActionState,
            name="storyarcplacementoperationactionstate",
        ),
        default=StoryArcPlacementOperationActionState.PLANNED,
        server_default=StoryArcPlacementOperationActionState.PLANNED.value,
        nullable=False,
    )

    # Historical identities deliberately are not foreign keys.  Placement,
    # membership, and canonical rows may be removed while recovery evidence
    # must remain durable and understandable.
    source_placement_id: Mapped[int] = mapped_column(Integer, nullable=False)
    shadow_placement_id: Mapped[int | None] = mapped_column(Integer)
    membership_id: Mapped[int] = mapped_column(Integer, nullable=False)
    library_file_id: Mapped[int | None] = mapped_column(Integer)
    source_library_root_id: Mapped[int | None] = mapped_column(Integer)
    target_library_root_id: Mapped[int | None] = mapped_column(Integer)

    # These paths are private action-journal evidence, never presentation data.
    source_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    target_path: Mapped[str | None] = mapped_column(String(1000))
    target_path_exact_hash: Mapped[str | None] = mapped_column(String(64))
    target_path_casefold_hash: Mapped[str | None] = mapped_column(String(64))
    temporary_path: Mapped[str | None] = mapped_column(String(1000))
    rollback_path: Mapped[str | None] = mapped_column(String(1000))

    source_mode: Mapped[StoryArcPlacementMode] = mapped_column(
        _enum_type(
            StoryArcPlacementMode,
            name="storyarcplacementoperationactionsourcemode",
        ),
        nullable=False,
    )
    target_mode: Mapped[StoryArcPlacementMode | None] = mapped_column(
        _enum_type(
            StoryArcPlacementMode,
            name="storyarcplacementoperationactiontargetmode",
        )
    )
    source_ownership: Mapped[StoryArcPlacementOwnership] = mapped_column(
        _enum_type(
            StoryArcPlacementOwnership,
            name="storyarcplacementoperationactionsourceownership",
        ),
        nullable=False,
    )
    target_ownership: Mapped[StoryArcPlacementOwnership | None] = mapped_column(
        _enum_type(
            StoryArcPlacementOwnership,
            name="storyarcplacementoperationactiontargetownership",
        )
    )
    prior_placement_snapshot: Mapped[dict[str, object]] = mapped_column(
        JSON,
        default=dict,
        server_default="{}",
        nullable=False,
    )
    source_root_fingerprint: Mapped[dict[str, object]] = mapped_column(
        JSON,
        default=dict,
        server_default="{}",
        nullable=False,
    )
    target_root_fingerprint: Mapped[dict[str, object]] = mapped_column(
        JSON,
        default=dict,
        server_default="{}",
        nullable=False,
    )
    expected_source_fingerprint: Mapped[dict[str, object]] = mapped_column(
        JSON,
        default=dict,
        server_default="{}",
        nullable=False,
    )
    staged_target_fingerprint: Mapped[dict[str, object]] = mapped_column(
        JSON,
        default=dict,
        server_default="{}",
        nullable=False,
    )
    published_target_fingerprint: Mapped[dict[str, object]] = mapped_column(
        JSON,
        default=dict,
        server_default="{}",
        nullable=False,
    )
    required_bytes: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default="0",
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    claim_token: Mapped[str | None] = mapped_column(String(64))
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    claim_heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    claim_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancel_observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    recovery_required_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reserved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    staged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    old_removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    db_reconciled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cleaned_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    rolled_back_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_category: Mapped[str | None] = mapped_column(String(50))
    last_error_detail: Mapped[str | None] = mapped_column(Text)
    last_result: Mapped[dict[str, object]] = mapped_column(
        JSON,
        default=dict,
        server_default="{}",
        nullable=False,
    )

    operation: Mapped[StoryArcPlacementOperation] = relationship(back_populates="actions")
