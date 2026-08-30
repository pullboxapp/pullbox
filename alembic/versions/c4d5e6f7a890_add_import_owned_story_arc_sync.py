"""Add import provenance and cancellation intent to story-arc sync work.

Revision ID: c4d5e6f7a890
Revises: b3c4d5e6f789
Create Date: 2026-08-30
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "c4d5e6f7a890"
down_revision: str | Sequence[str] | None = "b3c4d5e6f789"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORIGIN_FK = "fk_story_arc_sync_work_origin_action_import_job_actions"
_ORIGIN_JOB_FK = "fk_story_arc_sync_work_origin_job_import_jobs"
_ORIGIN_ARC_FK = "fk_story_arc_sync_work_origin_arc_import_story_arcs"
_ORIGIN_ENTRY_FK = "fk_story_arc_sync_work_origin_entry_import_story_arc_entries"
_ORIGIN_UNIQUE = "uq_story_arc_sync_work_origin_import_action"
_ORIGIN_JOB_STATE_INDEX = "ix_story_arc_sync_work_origin_job_state"
_FOLLOWUP_INDEX = "ix_import_jobs_story_arc_followup"
_ROLLBACK_WAITING_FK = "fk_import_jobs_story_arc_rollback_work"
_ROLLBACK_WAITING_INDEX = "ix_import_jobs_story_arc_rollback_waiting"
_ACTION_KEYSET_INDEX = "ix_import_job_actions_job_id_keyset"
_PLACEMENT_ACTION_INDEX = "ix_story_arc_placements_creating_action"
_READY_INDEX = "ix_story_arc_sync_work_ready"
_QUEUED_INDEX = "ix_story_arc_sync_work_queued"
_STALE_CLAIM_INDEX = "ix_story_arc_sync_work_stale_claim"


def _add_import_sync_foundation() -> None:
    with op.batch_alter_table("story_arc_sync_work") as batch_op:
        batch_op.add_column(sa.Column("origin_import_action_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("origin_import_job_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("origin_imported_story_arc_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("origin_imported_story_arc_entry_id", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "claimable",
                sa.Boolean(),
                server_default=sa.true(),
                nullable=False,
            )
        )
        batch_op.create_foreign_key(
            _ORIGIN_FK,
            "import_job_actions",
            ["origin_import_action_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            _ORIGIN_JOB_FK,
            "import_jobs",
            ["origin_import_job_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            _ORIGIN_ARC_FK,
            "import_story_arcs",
            ["origin_imported_story_arc_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            _ORIGIN_ENTRY_FK,
            "import_story_arc_entries",
            ["origin_imported_story_arc_entry_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_unique_constraint(
            _ORIGIN_UNIQUE,
            ["origin_import_action_id"],
        )
    op.create_index(
        _ORIGIN_JOB_STATE_INDEX,
        "story_arc_sync_work",
        ["origin_import_job_id", "state", "id"],
        unique=False,
    )
    op.add_column(
        "import_jobs",
        sa.Column(
            "story_arc_placement_followup_pending",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    if op.get_bind().dialect.name == "sqlite":
        # SQLite can add one nullable REFERENCES column without rebuilding the
        # parent table. A batch rebuild would cascade-delete existing import
        # children when the old import_jobs table is dropped.
        op.execute(
            sa.text(
                "ALTER TABLE import_jobs ADD COLUMN story_arc_rollback_waiting_work_id "
                "INTEGER REFERENCES story_arc_sync_work(id) ON DELETE SET NULL"
            )
        )
    else:
        op.add_column(
            "import_jobs",
            sa.Column("story_arc_rollback_waiting_work_id", sa.Integer(), nullable=True),
        )
        op.create_foreign_key(
            _ROLLBACK_WAITING_FK,
            "import_jobs",
            "story_arc_sync_work",
            ["story_arc_rollback_waiting_work_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        _FOLLOWUP_INDEX,
        "import_jobs",
        ["status", "story_arc_placement_followup_pending", "id"],
        unique=False,
    )
    op.create_index(
        _ROLLBACK_WAITING_INDEX,
        "import_jobs",
        ["status", "story_arc_rollback_waiting_work_id", "id"],
        unique=False,
    )
    op.create_index(
        _ACTION_KEYSET_INDEX,
        "import_job_actions",
        ["import_job_id", "id"],
        unique=False,
    )
    op.create_index(
        _PLACEMENT_ACTION_INDEX,
        "story_arc_placements",
        ["creating_action_id", "id"],
        unique=False,
    )
    op.create_index(
        _QUEUED_INDEX,
        "story_arc_sync_work",
        ["claimable", "state", "created_at", "id"],
        unique=False,
    )
    op.drop_index(_READY_INDEX, table_name="story_arc_sync_work")
    op.create_index(
        _READY_INDEX,
        "story_arc_sync_work",
        ["claimable", "state", "next_attempt_at", "id"],
        unique=False,
    )
    op.create_index(
        _STALE_CLAIM_INDEX,
        "story_arc_sync_work",
        ["claimable", "state", "claimed_at", "id"],
        unique=False,
    )


def _assert_downgrade_is_lossless() -> None:
    held_work = (
        op.get_bind()
        .execute(sa.text("SELECT id FROM story_arc_sync_work WHERE claimable = false LIMIT 1"))
        .first()
    )
    if held_work is not None:
        raise RuntimeError(
            "Cannot downgrade while held story-arc sync work remains; releasing the "
            "claimability fence would revive work that has not been safely published."
        )

    linked_work = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT id FROM story_arc_sync_work "
                "WHERE origin_import_action_id IS NOT NULL LIMIT 1"
            )
        )
        .first()
    )
    if linked_work is not None:
        raise RuntimeError(
            "Cannot downgrade while import-owned story-arc sync work remains; "
            "finish, cancel, or detach the linked work before removing its provenance."
        )

    typed_provenance = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT id FROM story_arc_sync_work WHERE "
                "origin_import_job_id IS NOT NULL "
                "OR origin_imported_story_arc_id IS NOT NULL "
                "OR origin_imported_story_arc_entry_id IS NOT NULL LIMIT 1"
            )
        )
        .first()
    )
    if typed_provenance is not None:
        raise RuntimeError(
            "Cannot downgrade while typed import-owned story-arc sync provenance remains; "
            "finish, cancel, or detach the linked work before removing its provenance."
        )

    pending_cancellation = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT id FROM story_arc_sync_work WHERE cancel_requested_at IS NOT NULL LIMIT 1"
            )
        )
        .first()
    )
    if pending_cancellation is not None:
        raise RuntimeError(
            "Cannot downgrade while a pending story-arc sync cancellation remains; "
            "consume or clear it before removing durable cancellation intent."
        )

    pending_import_lifecycle = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT id FROM import_jobs WHERE story_arc_placement_followup_pending "
                "OR story_arc_rollback_waiting_work_id IS NOT NULL LIMIT 1"
            )
        )
        .first()
    )
    if pending_import_lifecycle is not None:
        raise RuntimeError(
            "Cannot downgrade while an import Story Arc follow-up or rollback wait remains; "
            "finish the pending lifecycle work before removing its durable routing state."
        )


def _remove_import_sync_foundation() -> None:
    op.drop_index(_ACTION_KEYSET_INDEX, table_name="import_job_actions")
    op.drop_index(_ROLLBACK_WAITING_INDEX, table_name="import_jobs")
    op.drop_index(_FOLLOWUP_INDEX, table_name="import_jobs")
    if op.get_bind().dialect.name == "sqlite":
        op.execute(
            sa.text("ALTER TABLE import_jobs DROP COLUMN story_arc_rollback_waiting_work_id")
        )
        op.execute(
            sa.text("ALTER TABLE import_jobs DROP COLUMN story_arc_placement_followup_pending")
        )
    else:
        op.drop_constraint(_ROLLBACK_WAITING_FK, "import_jobs", type_="foreignkey")
        op.drop_column("import_jobs", "story_arc_rollback_waiting_work_id")
        op.drop_column("import_jobs", "story_arc_placement_followup_pending")
    op.drop_index(_ORIGIN_JOB_STATE_INDEX, table_name="story_arc_sync_work")
    op.drop_index(_STALE_CLAIM_INDEX, table_name="story_arc_sync_work")
    op.drop_index(_READY_INDEX, table_name="story_arc_sync_work")
    op.create_index(
        _READY_INDEX,
        "story_arc_sync_work",
        ["state", "next_attempt_at", "id"],
        unique=False,
    )
    op.drop_index(_QUEUED_INDEX, table_name="story_arc_sync_work")
    op.drop_index(_PLACEMENT_ACTION_INDEX, table_name="story_arc_placements")
    with op.batch_alter_table("story_arc_sync_work") as batch_op:
        batch_op.drop_constraint(_ORIGIN_UNIQUE, type_="unique")
        batch_op.drop_constraint(_ORIGIN_ENTRY_FK, type_="foreignkey")
        batch_op.drop_constraint(_ORIGIN_ARC_FK, type_="foreignkey")
        batch_op.drop_constraint(_ORIGIN_JOB_FK, type_="foreignkey")
        batch_op.drop_constraint(_ORIGIN_FK, type_="foreignkey")
        batch_op.drop_column("claimable")
        batch_op.drop_column("cancel_requested_at")
        batch_op.drop_column("origin_imported_story_arc_entry_id")
        batch_op.drop_column("origin_imported_story_arc_id")
        batch_op.drop_column("origin_import_job_id")
        batch_op.drop_column("origin_import_action_id")


def upgrade() -> None:
    """Add optional action provenance and durable cancellation requests."""
    _add_import_sync_foundation()


def downgrade() -> None:
    """Remove additive fields only when doing so cannot erase provenance."""
    _assert_downgrade_is_lossless()
    _remove_import_sync_foundation()
