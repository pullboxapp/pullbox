"""add utility tables

Revision ID: a9eff3b660f5
Revises: d2ddfd88f309
Create Date: 2026-03-17 12:25:32.121339

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a9eff3b660f5"
down_revision: str | Sequence[str] | None = "d2ddfd88f309"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ── Enum values ─────────────────────────────────────────────

_JOB_TYPES = (
    "file_convert",
    "mass_convert_pipeline",
    "mass_rename",
    "db_check_cleanup",
    "export_library",
    "integrity_check",
    "rollback",
)

_JOB_STATES = (
    "QUEUED",
    "RUNNING",
    "PAUSING",
    "PAUSED",
    "CANCELLING",
    "CANCELLED",
    "COMPLETED",
    "FAILED",
    "ROLLING_BACK",
    "ROLLED_BACK",
)

_ITEM_STATES = (
    "PENDING",
    "IN_PROGRESS",
    "COMPLETED",
    "FAILED",
    "SKIPPED",
    "ROLLED_BACK",
    "ROLLBACK_FAILED",
)

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def _check_in(column: str, values: tuple[str, ...]) -> str:
    """Build a CHECK constraint expression for allowed values."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    """Create utility_jobs, utility_job_items, utility_job_logs tables.

    Also adds integrity columns to the issues table.
    """
    # ── utility_jobs ────────────────────────────────────────
    op.create_table(
        "utility_jobs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("job_type", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'QUEUED'"),
        ),
        sa.Column(
            "config",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("total_items", sa.Integer(), nullable=True),
        sa.Column("completed_items", sa.Integer(), nullable=True),
        sa.Column("failed_items", sa.Integer(), nullable=True),
        sa.Column("skipped_items", sa.Integer(), nullable=True),
        sa.Column("warning_count", sa.Integer(), nullable=True),
        sa.Column("queue_position", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=True),
        sa.Column("started_at", sa.Text(), nullable=True),
        sa.Column("paused_at", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column(
            "parent_job_id",
            sa.Text(),
            sa.ForeignKey("utility_jobs.id"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(_check_in("state", _JOB_STATES), name="ck_utility_jobs_state"),
        sa.CheckConstraint(_check_in("job_type", _JOB_TYPES), name="ck_utility_jobs_job_type"),
    )
    op.create_index("ix_utility_jobs_state", "utility_jobs", ["state"])
    op.create_index("ix_utility_jobs_created_at", "utility_jobs", ["created_at"])

    # ── utility_job_items ───────────────────────────────────
    op.create_table(
        "utility_job_items",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("item_index", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'PENDING'"),
        ),
        sa.Column("file_path", sa.Text(), nullable=True),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column(
            "before_state",
            sa.Text(),
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "after_state",
            sa.Text(),
            server_default=sa.text("'{}'"),
        ),
        sa.Column("started_at", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("warning_message", sa.Text(), nullable=True),
        sa.Column("worker_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["job_id"], ["utility_jobs.id"], ondelete="CASCADE"),
        sa.CheckConstraint(_check_in("state", _ITEM_STATES), name="ck_utility_job_items_state"),
    )
    op.create_index("ix_utility_job_items_job_id", "utility_job_items", ["job_id"])
    op.create_index("ix_utility_job_items_job_id_state", "utility_job_items", ["job_id", "state"])
    op.create_index(
        "ix_utility_job_items_job_id_item_index",
        "utility_job_items",
        ["job_id", "item_index"],
    )

    # ── utility_job_logs ────────────────────────────────────
    op.create_table(
        "utility_job_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("item_id", sa.Text(), nullable=True),
        sa.Column(
            "timestamp",
            sa.Text(),
            server_default=(
                sa.text("to_char(CURRENT_TIMESTAMP, 'YYYY-MM-DD HH24:MI:SS')")
                if op.get_bind().dialect.name == "postgresql"
                else sa.text("(datetime('now', 'localtime'))")
            ),
        ),
        sa.Column(
            "level",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'INFO'"),
        ),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=True),
        sa.Column(
            "extra",
            sa.Text(),
            server_default=sa.text("'{}'"),
        ),
        sa.ForeignKeyConstraint(["job_id"], ["utility_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["item_id"], ["utility_job_items.id"]),
        sa.CheckConstraint(_check_in("level", _LOG_LEVELS), name="ck_utility_job_logs_level"),
    )
    op.create_index("ix_utility_job_logs_job_id", "utility_job_logs", ["job_id"])
    op.create_index("ix_utility_job_logs_job_id_level", "utility_job_logs", ["job_id", "level"])
    op.create_index(
        "ix_utility_job_logs_job_id_timestamp",
        "utility_job_logs",
        ["job_id", "timestamp"],
    )

    # ── issues: integrity columns ───────────────────────────
    with op.batch_alter_table("issues") as batch_op:
        batch_op.add_column(
            sa.Column(
                "integrity_status",
                sa.Text(),
                server_default=sa.text("'unchecked'"),
            )
        )
        batch_op.add_column(sa.Column("integrity_checked_at", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "integrity_details",
                sa.Text(),
                server_default=sa.text("'{}'"),
            )
        )
        batch_op.create_index("ix_issues_integrity_status", ["integrity_status"])


def downgrade() -> None:
    """Remove utility tables and integrity columns from issues."""
    # ── issues: remove integrity columns ────────────────────
    with op.batch_alter_table("issues") as batch_op:
        batch_op.drop_index("ix_issues_integrity_status")
        batch_op.drop_column("integrity_details")
        batch_op.drop_column("integrity_checked_at")
        batch_op.drop_column("integrity_status")

    # ── Drop utility tables (order matters for FK) ──────────
    op.drop_table("utility_job_logs")
    op.drop_table("utility_job_items")
    op.drop_table("utility_jobs")
