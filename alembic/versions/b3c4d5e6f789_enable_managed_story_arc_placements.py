"""Enable managed story-arc copy and link placement modes.

Revision ID: b3c4d5e6f789
Revises: a2b3c4d5e678
Create Date: 2026-08-30
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "b3c4d5e6f789"
down_revision: str | Sequence[str] | None = "a2b3c4d5e678"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REFERENCE_ONLY_CONSTRAINTS = (
    "ck_story_arc_placements_reference_only_mode",
    "ck_story_arc_placements_reference_only_owner",
    "ck_story_arc_placements_no_symlink_style",
)
_MODE_OWNERSHIP_CONSTRAINT = "ck_story_arc_placements_mode_ownership"
_SYMLINK_STYLE_CONSTRAINT = "ck_story_arc_placements_symlink_style"
_MODE_OWNERSHIP_SQL = (
    "((mode = 'reference_only' AND ownership = 'referenced') OR "
    "(mode IN ('copy', 'hardlink', 'symlink') AND ownership = 'managed'))"
)
_SYMLINK_STYLE_SQL = (
    "((mode = 'symlink' AND symlink_style IS NOT NULL) OR "
    "(mode != 'symlink' AND symlink_style IS NULL))"
)
_SYNC_WORK_STATES = (
    "queued",
    "running",
    "retry_wait",
    "failed",
    "completed",
    "cancelled",
)
_SYNC_WORK_REASONS = ("canonical_registered", "discrepancy_recovery")


def _enable_managed_combinations() -> None:
    with op.batch_alter_table("story_arc_placements") as batch_op:
        batch_op.add_column(sa.Column("operation_token", sa.String(length=32), nullable=True))
        for constraint_name in _REFERENCE_ONLY_CONSTRAINTS:
            batch_op.drop_constraint(constraint_name, type_="check")
        batch_op.create_check_constraint(
            _MODE_OWNERSHIP_CONSTRAINT,
            _MODE_OWNERSHIP_SQL,
        )
        batch_op.create_check_constraint(
            _SYMLINK_STYLE_CONSTRAINT,
            _SYMLINK_STYLE_SQL,
        )


def _assert_downgrade_is_lossless() -> None:
    managed = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT id FROM story_arc_placements "
                "WHERE mode != 'reference_only' OR ownership != 'referenced' "
                "OR symlink_style IS NOT NULL OR operation_token IS NOT NULL LIMIT 1"
            )
        )
        .first()
    )
    if managed is not None:
        raise RuntimeError(
            "Cannot downgrade while managed story-arc placements remain; "
            "remove or convert them to referenced placements first."
        )


def _restore_reference_only_combinations() -> None:
    with op.batch_alter_table("story_arc_placements") as batch_op:
        batch_op.drop_constraint(_SYMLINK_STYLE_CONSTRAINT, type_="check")
        batch_op.drop_constraint(_MODE_OWNERSHIP_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(
            "ck_story_arc_placements_reference_only_mode",
            "mode = 'reference_only'",
        )
        batch_op.create_check_constraint(
            "ck_story_arc_placements_reference_only_owner",
            "ownership = 'referenced'",
        )
        batch_op.create_check_constraint(
            "ck_story_arc_placements_no_symlink_style",
            "symlink_style IS NULL",
        )
        batch_op.drop_column("operation_token")


def _create_story_arc_sync_work() -> None:
    op.create_table(
        "story_arc_sync_work",
        sa.Column("issue_story_arc_id", sa.Integer(), nullable=False),
        sa.Column("library_file_id", sa.Integer(), nullable=False),
        sa.Column("desired_generation", sa.String(length=64), nullable=False),
        sa.Column("source_signature_hash", sa.String(length=64), nullable=False),
        sa.Column("source_file_path", sa.String(length=1000), nullable=False),
        sa.Column("source_file_size", sa.BigInteger(), nullable=False),
        sa.Column("source_file_modified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_file_hash", sa.String(length=64), nullable=True),
        sa.Column("source_signature_schema_version", sa.Integer(), nullable=True),
        sa.Column("source_signature_resolved_path", sa.String(length=1000), nullable=True),
        sa.Column("source_signature_size", sa.BigInteger(), nullable=True),
        sa.Column("source_signature_mtime_ns", sa.BigInteger(), nullable=True),
        sa.Column("source_signature_device", sa.BigInteger(), nullable=True),
        sa.Column("source_signature_inode", sa.BigInteger(), nullable=True),
        sa.Column("story_arc_revision", sa.Integer(), nullable=False),
        sa.Column("membership_sequence", sa.Integer(), nullable=False),
        sa.Column("policy_schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "reason",
            sa.Enum(
                *_SYNC_WORK_REASONS,
                name="storyarcsyncreason",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="canonical_registered",
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.Enum(
                *_SYNC_WORK_STATES,
                name="storyarcsyncworkstate",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_category", sa.String(length=50), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["issue_story_arc_id"],
            ["issue_story_arcs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["library_file_id"],
            ["library_files.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "issue_story_arc_id",
            "desired_generation",
            name="uq_story_arc_sync_work_generation",
        ),
    )
    op.create_index(
        "ix_story_arc_sync_work_ready",
        "story_arc_sync_work",
        ["state", "next_attempt_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_story_arc_sync_work_membership",
        "story_arc_sync_work",
        ["issue_story_arc_id", "id"],
        unique=False,
    )
    op.create_index(
        "ix_story_arc_sync_work_library_file",
        "story_arc_sync_work",
        ["library_file_id", "id"],
        unique=False,
    )


def _drop_story_arc_sync_work() -> None:
    op.drop_index(
        "ix_story_arc_sync_work_library_file",
        table_name="story_arc_sync_work",
    )
    op.drop_index(
        "ix_story_arc_sync_work_membership",
        table_name="story_arc_sync_work",
    )
    op.drop_index(
        "ix_story_arc_sync_work_ready",
        table_name="story_arc_sync_work",
    )
    op.drop_table("story_arc_sync_work")


def upgrade() -> None:
    """Admit only internally consistent managed copy/link placements."""
    _enable_managed_combinations()
    _create_story_arc_sync_work()


def downgrade() -> None:
    """Restore IU6-A checks after discarding rebuildable sync-queue state."""
    _assert_downgrade_is_lossless()
    # This is operational work state, not audit history. The periodic
    # discrepancy sweep recreates eligible intent after a later re-upgrade.
    _drop_story_arc_sync_work()
    _restore_reference_only_combinations()
