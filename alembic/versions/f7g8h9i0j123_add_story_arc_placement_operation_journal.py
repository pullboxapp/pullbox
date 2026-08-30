"""Add durable arc-wide placement-operation coordination and journals.

Revision ID: f7g8h9i0j123
Revises: e6f7a8b9c012
Create Date: 2026-08-30
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "f7g8h9i0j123"
down_revision: str | Sequence[str] | None = "e6f7a8b9c012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPERATION_KINDS = ("policy_migration", "reorder", "arc_delete", "repair")
_OPERATION_STATES = (
    "building",
    "ready",
    "running",
    "reconciling",
    "cleanup_pending",
    "cancel_requested",
    "rolling_back",
    "recovery_required",
    "completed",
    "cancelled",
    "failed",
)
_RECOVERY_MODES = ("rollback", "forward_only")
_ACTION_KINDS = ("migrate_managed", "rebuild_managed", "remove_managed")
_ACTION_STATES = (
    "planned",
    "reserved",
    "staged",
    "published",
    "verified",
    "old_removed",
    "db_reconciled",
    "cleanup_pending",
    "cleaned",
    "rolling_back",
    "rolled_back",
    "recovery_required",
    "failed",
)
_PLACEMENT_MODES = ("copy", "hardlink", "symlink", "reference_only")
_PLACEMENT_OWNERSHIP = ("managed", "referenced")


def _enum(values: tuple[str, ...], *, name: str) -> sa.Enum:
    return sa.Enum(
        *values,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
    )


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def _add_story_arc_operation_fence() -> None:
    if _is_sqlite():
        # Native additive columns avoid a batch rebuild that could cascade
        # through the existing Story Arc graph when SQLite drops the old table.
        op.execute(
            sa.text(
                "ALTER TABLE story_arcs ADD COLUMN "
                "active_placement_operation_token VARCHAR(32) "
                "CONSTRAINT ck_story_arcs_active_placement_operation_token_length "
                "CHECK (active_placement_operation_token IS NULL OR "
                "length(active_placement_operation_token) = 32)"
            )
        )
        op.execute(
            sa.text(
                "ALTER TABLE story_arcs ADD COLUMN "
                "active_placement_operation_kind VARCHAR(16) "
                "CONSTRAINT storyarcplacementoperationkind "
                "CHECK (active_placement_operation_kind IN "
                "('policy_migration','reorder','arc_delete','repair')) "
                "CONSTRAINT ck_story_arcs_active_placement_operation_pair "
                "CHECK (((active_placement_operation_token IS NULL) AND "
                "(active_placement_operation_kind IS NULL)) OR "
                "((active_placement_operation_token IS NOT NULL) AND "
                "(active_placement_operation_kind IS NOT NULL)))"
            )
        )
    else:
        op.add_column(
            "story_arcs",
            sa.Column("active_placement_operation_token", sa.String(32), nullable=True),
        )
        op.add_column(
            "story_arcs",
            sa.Column("active_placement_operation_kind", sa.String(16), nullable=True),
        )
        op.create_check_constraint(
            "storyarcplacementoperationkind",
            "story_arcs",
            "active_placement_operation_kind IS NULL OR "
            "active_placement_operation_kind IN "
            "('policy_migration','reorder','arc_delete','repair')",
        )
        op.create_check_constraint(
            "ck_story_arcs_active_placement_operation_token_length",
            "story_arcs",
            "active_placement_operation_token IS NULL OR "
            "length(active_placement_operation_token) = 32",
        )
        op.create_check_constraint(
            "ck_story_arcs_active_placement_operation_pair",
            "story_arcs",
            "((active_placement_operation_token IS NULL AND "
            "active_placement_operation_kind IS NULL) OR "
            "(active_placement_operation_token IS NOT NULL AND "
            "active_placement_operation_kind IS NOT NULL))",
        )
    op.create_index(
        "ux_story_arcs_active_placement_operation_token",
        "story_arcs",
        ["active_placement_operation_token"],
        unique=True,
    )


def _create_operation_coordinator() -> None:
    op.create_table(
        "story_arc_placement_operations",
        sa.Column("story_arc_id", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column(
            "operation_kind",
            _enum(_OPERATION_KINDS, name="storyarcplacementoperationkind"),
            nullable=False,
        ),
        sa.Column(
            "state",
            _enum(_OPERATION_STATES, name="storyarcplacementoperationstate"),
            server_default="building",
            nullable=False,
        ),
        sa.Column("active_fence_slot", sa.Integer(), server_default="1", nullable=True),
        sa.Column("operation_token", sa.String(32), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(64), nullable=False),
        sa.Column("expected_story_arc_revision", sa.Integer(), nullable=False),
        sa.Column("confirmation_schema_version", sa.Integer(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_policy_snapshot", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("proposed_policy_snapshot", sa.JSON(), server_default="{}", nullable=False),
        # Root identities are historical recovery evidence rather than FKs;
        # roots can be repaired or removed while the journal must remain intact.
        sa.Column("source_library_root_id", sa.Integer(), nullable=True),
        sa.Column("target_library_root_id", sa.Integer(), nullable=True),
        sa.Column("source_root_fingerprint", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("target_root_fingerprint", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("scope_digest", sa.String(64), nullable=False),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column(
            "recovery_mode",
            _enum(_RECOVERY_MODES, name="storyarcplacementoperationrecoverymode"),
            server_default="rollback",
            nullable=False,
        ),
        sa.Column("claim_token", sa.String(64), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("policy_applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_policy_revision", sa.Integer(), nullable=True),
        sa.Column("applied_policy_digest", sa.String(64), nullable=True),
        sa.Column("total_action_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("completed_action_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("failed_action_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("required_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(100), nullable=True),
        sa.Column("last_error_category", sa.String(50), nullable=True),
        sa.Column("last_error_detail", sa.Text(), nullable=True),
        sa.Column("last_result", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "expected_story_arc_revision >= 1",
            name="ck_story_arc_placement_operations_expected_revision",
        ),
        sa.CheckConstraint(
            "length(operation_token) = 32 AND length(idempotency_key_hash) = 64",
            name="ck_story_arc_placement_operations_token_length",
        ),
        sa.CheckConstraint(
            "length(scope_digest) = 64 AND length(plan_digest) = 64",
            name="ck_story_arc_placement_operations_digest_length",
        ),
        sa.CheckConstraint(
            "confirmation_schema_version >= 1",
            name="ck_story_arc_placement_operations_confirmation_schema",
        ),
        sa.CheckConstraint(
            "total_action_count >= 0 AND completed_action_count >= 0 "
            "AND failed_action_count >= 0 "
            "AND completed_action_count + failed_action_count <= total_action_count "
            "AND required_bytes >= 0 AND attempt_count >= 0",
            name="ck_story_arc_placement_operations_action_counts",
        ),
        sa.CheckConstraint(
            "((recovery_mode = 'rollback' AND policy_applied_at IS NULL "
            "AND applied_policy_revision IS NULL AND applied_policy_digest IS NULL) OR "
            "(recovery_mode = 'forward_only' AND policy_applied_at IS NOT NULL "
            "AND applied_policy_revision IS NOT NULL AND applied_policy_revision >= 1 "
            "AND applied_policy_digest IS NOT NULL "
            "AND length(applied_policy_digest) = 64))",
            name="ck_story_arc_placement_operations_policy_boundary",
        ),
        sa.CheckConstraint(
            "state != 'rolling_back' OR recovery_mode = 'rollback'",
            name="ck_story_arc_placement_operations_rollback_mode",
        ),
        sa.CheckConstraint(
            "state != 'cancelled' OR recovery_mode = 'rollback'",
            name="ck_story_arc_placement_operations_cancel_mode",
        ),
        sa.CheckConstraint(
            "recovery_mode != 'forward_only' OR state IN "
            "('running','reconciling','cleanup_pending','recovery_required','completed')",
            name="ck_story_arc_placement_operations_forward_state",
        ),
        sa.CheckConstraint(
            "((state IN ('completed','cancelled','failed') "
            "AND active_fence_slot IS NULL AND completed_at IS NOT NULL) OR "
            "(state NOT IN ('completed','cancelled','failed') "
            "AND active_fence_slot = 1 AND completed_at IS NULL))",
            name="ck_story_arc_placement_operations_fence_state",
        ),
        sa.CheckConstraint(
            "((claim_token IS NULL AND claimed_at IS NULL "
            "AND claim_heartbeat_at IS NULL AND claim_expires_at IS NULL) OR "
            "(claim_token IS NOT NULL AND claimed_at IS NOT NULL "
            "AND claim_heartbeat_at IS NOT NULL AND claim_expires_at IS NOT NULL))",
            name="ck_story_arc_placement_operations_claim",
        ),
        sa.ForeignKeyConstraint(
            ["story_arc_id"],
            ["story_arcs.id"],
            name="fk_story_arc_placement_operations_story_arc",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_story_arc_placement_operations_actor_user",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_token", name="uq_story_arc_placement_operations_token"),
        sa.UniqueConstraint(
            "story_arc_id",
            "operation_kind",
            "idempotency_key_hash",
            name="uq_story_arc_placement_operations_idempotency",
        ),
        sa.UniqueConstraint(
            "story_arc_id",
            "active_fence_slot",
            name="uq_story_arc_placement_operations_active_fence",
        ),
    )
    op.create_index(
        "ix_story_arc_placement_operations_arc_state",
        "story_arc_placement_operations",
        ["story_arc_id", "state", "id"],
        unique=False,
    )
    op.create_index(
        "ix_story_arc_placement_operations_recovery",
        "story_arc_placement_operations",
        [
            "active_fence_slot",
            "state",
            "next_attempt_at",
            "claim_expires_at",
            "updated_at",
            "id",
        ],
        unique=False,
    )


def _create_operation_actions() -> None:
    op.create_table(
        "story_arc_placement_operation_actions",
        sa.Column("operation_id", sa.Integer(), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column(
            "action_kind",
            _enum(_ACTION_KINDS, name="storyarcplacementoperationactionkind"),
            nullable=False,
        ),
        sa.Column(
            "state",
            _enum(_ACTION_STATES, name="storyarcplacementoperationactionstate"),
            server_default="planned",
            nullable=False,
        ),
        # These are stable historical identities, intentionally not FKs.
        sa.Column("source_placement_id", sa.Integer(), nullable=False),
        sa.Column("shadow_placement_id", sa.Integer(), nullable=True),
        sa.Column("membership_id", sa.Integer(), nullable=False),
        sa.Column("library_file_id", sa.Integer(), nullable=True),
        sa.Column("source_library_root_id", sa.Integer(), nullable=True),
        sa.Column("target_library_root_id", sa.Integer(), nullable=True),
        # Private recovery paths are never presentation/progress data.
        sa.Column("source_path", sa.String(1000), nullable=False),
        sa.Column("target_path", sa.String(1000), nullable=True),
        sa.Column("target_path_exact_hash", sa.String(64), nullable=True),
        sa.Column("target_path_casefold_hash", sa.String(64), nullable=True),
        sa.Column("temporary_path", sa.String(1000), nullable=True),
        sa.Column("rollback_path", sa.String(1000), nullable=True),
        sa.Column(
            "source_mode",
            _enum(
                _PLACEMENT_MODES,
                name="storyarcplacementoperationactionsourcemode",
            ),
            nullable=False,
        ),
        sa.Column(
            "target_mode",
            _enum(
                _PLACEMENT_MODES,
                name="storyarcplacementoperationactiontargetmode",
            ),
            nullable=True,
        ),
        sa.Column(
            "source_ownership",
            _enum(
                _PLACEMENT_OWNERSHIP,
                name="storyarcplacementoperationactionsourceownership",
            ),
            nullable=False,
        ),
        sa.Column(
            "target_ownership",
            _enum(
                _PLACEMENT_OWNERSHIP,
                name="storyarcplacementoperationactiontargetownership",
            ),
            nullable=True,
        ),
        sa.Column("prior_placement_snapshot", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("source_root_fingerprint", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("target_root_fingerprint", sa.JSON(), server_default="{}", nullable=False),
        sa.Column(
            "expected_source_fingerprint",
            sa.JSON(),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("staged_target_fingerprint", sa.JSON(), server_default="{}", nullable=False),
        sa.Column(
            "published_target_fingerprint",
            sa.JSON(),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("required_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claim_token", sa.String(64), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recovery_required_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("staged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("old_removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("db_reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleaned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(100), nullable=True),
        sa.Column("last_error_category", sa.String(50), nullable=True),
        sa.Column("last_error_detail", sa.Text(), nullable=True),
        sa.Column("last_result", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "sequence_number >= 1",
            name="ck_story_arc_placement_operation_actions_sequence",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_story_arc_placement_operation_actions_attempt_count",
        ),
        sa.CheckConstraint(
            "required_bytes >= 0",
            name="ck_story_arc_placement_operation_actions_required_bytes",
        ),
        sa.CheckConstraint(
            "((action_kind = 'remove_managed' AND target_path IS NULL "
            "AND target_path_exact_hash IS NULL AND target_path_casefold_hash IS NULL "
            "AND target_mode IS NULL AND target_ownership IS NULL) OR "
            "(action_kind IN ('migrate_managed','rebuild_managed') "
            "AND target_path IS NOT NULL AND target_path_exact_hash IS NOT NULL "
            "AND length(target_path_exact_hash) = 64 "
            "AND target_path_casefold_hash IS NOT NULL "
            "AND length(target_path_casefold_hash) = 64 "
            "AND target_mode IS NOT NULL AND target_ownership IS NOT NULL))",
            name="ck_story_arc_placement_operation_actions_target_shape",
        ),
        sa.CheckConstraint(
            "source_mode IN ('copy','hardlink','symlink') "
            "AND source_ownership = 'managed' "
            "AND (target_ownership IS NULL OR target_ownership = 'managed')",
            name="ck_story_arc_placement_operation_actions_ownership",
        ),
        sa.CheckConstraint(
            "((claim_token IS NULL AND claimed_at IS NULL "
            "AND claim_heartbeat_at IS NULL AND claim_expires_at IS NULL) OR "
            "(claim_token IS NOT NULL AND claimed_at IS NOT NULL "
            "AND claim_heartbeat_at IS NOT NULL AND claim_expires_at IS NOT NULL))",
            name="ck_story_arc_placement_operation_actions_claim",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["story_arc_placement_operations.id"],
            name="fk_story_arc_placement_operation_actions_operation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_id",
            "sequence_number",
            name="uq_story_arc_placement_operation_actions_sequence",
        ),
        sa.UniqueConstraint(
            "operation_id",
            "target_path_exact_hash",
            name="uq_story_arc_placement_operation_actions_target_exact",
        ),
        sa.UniqueConstraint(
            "operation_id",
            "target_path_casefold_hash",
            name="uq_story_arc_placement_operation_actions_target_casefold",
        ),
        sa.UniqueConstraint(
            "operation_id",
            "source_placement_id",
            name="uq_story_arc_placement_operation_actions_source",
        ),
    )
    op.create_index(
        "ix_story_arc_placement_operation_actions_next",
        "story_arc_placement_operation_actions",
        ["operation_id", "state", "sequence_number", "id"],
        unique=False,
    )
    op.create_index(
        "ix_story_arc_placement_operation_actions_source",
        "story_arc_placement_operation_actions",
        ["operation_id", "source_placement_id", "id"],
        unique=False,
    )


def _assert_downgrade_is_lossless() -> None:
    active_arc = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT id FROM story_arcs "
                "WHERE active_placement_operation_token IS NOT NULL OR "
                "active_placement_operation_kind IS NOT NULL LIMIT 1"
            )
        )
        .first()
    )
    operation = (
        op.get_bind()
        .execute(sa.text("SELECT id FROM story_arc_placement_operations LIMIT 1"))
        .first()
    )
    if active_arc is not None or operation is not None:
        raise RuntimeError(
            "Cannot downgrade while a durable Story Arc placement-operation journal "
            "or active fence exists; finish recovery and retain or export its audit "
            "evidence first."
        )


def _drop_operation_schema() -> None:
    op.drop_index(
        "ix_story_arc_placement_operation_actions_source",
        table_name="story_arc_placement_operation_actions",
    )
    op.drop_index(
        "ix_story_arc_placement_operation_actions_next",
        table_name="story_arc_placement_operation_actions",
    )
    op.drop_table("story_arc_placement_operation_actions")
    op.drop_index(
        "ix_story_arc_placement_operations_recovery",
        table_name="story_arc_placement_operations",
    )
    op.drop_index(
        "ix_story_arc_placement_operations_arc_state",
        table_name="story_arc_placement_operations",
    )
    op.drop_table("story_arc_placement_operations")


def _drop_story_arc_operation_fence() -> None:
    op.drop_index(
        "ux_story_arcs_active_placement_operation_token",
        table_name="story_arcs",
    )
    if _is_sqlite():
        op.drop_column("story_arcs", "active_placement_operation_kind")
        op.drop_column("story_arcs", "active_placement_operation_token")
        return
    op.drop_constraint(
        "ck_story_arcs_active_placement_operation_pair",
        "story_arcs",
        type_="check",
    )
    op.drop_constraint(
        "ck_story_arcs_active_placement_operation_token_length",
        "story_arcs",
        type_="check",
    )
    op.drop_constraint(
        "storyarcplacementoperationkind",
        "story_arcs",
        type_="check",
    )
    op.drop_column("story_arcs", "active_placement_operation_kind")
    op.drop_column("story_arcs", "active_placement_operation_token")


def upgrade() -> None:
    """Add the arc fence, coordinator, and ordered private action journal."""
    _add_story_arc_operation_fence()
    _create_operation_coordinator()
    _create_operation_actions()


def downgrade() -> None:
    """Remove the schema only when no durable operation evidence exists."""
    _assert_downgrade_is_lossless()
    _drop_operation_schema()
    _drop_story_arc_operation_fence()
