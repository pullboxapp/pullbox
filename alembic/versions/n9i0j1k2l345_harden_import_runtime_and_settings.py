"""harden import runtime and settings

Revision ID: n9i0j1k2l345
Revises: m8h9i0j1k234
Create Date: 2026-04-29 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "n9i0j1k2l345"
down_revision: str | Sequence[str] | None = "m8h9i0j1k234"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("import_jobs") as batch_op:
        batch_op.add_column(
            sa.Column("selected_file_paths", sa.JSON(), nullable=False, server_default="[]")
        )
        batch_op.add_column(
            sa.Column("progress_snapshot", sa.JSON(), nullable=False, server_default="{}")
        )
        batch_op.add_column(
            sa.Column(
                "transfer_method",
                sa.String(length=20),
                nullable=False,
                server_default="move",
            )
        )
        batch_op.add_column(
            sa.Column(
                "convert_to_preferred_format",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    with op.batch_alter_table("import_files") as batch_op:
        batch_op.add_column(sa.Column("matched_issue_cv_id", sa.Integer(), nullable=True))

    op.create_table(
        "import_job_actions",
        sa.Column("import_job_id", sa.Integer(), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(length=50), nullable=False),
        sa.Column("action_type", sa.String(length=100), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "COMPLETED",
                "ROLLED_BACK",
                "ROLLBACK_FAILED",
                name="importjobactionstatus",
            ),
            nullable=False,
        ),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["import_job_id"], ["import_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_import_job_actions_import_job_id"),
        "import_job_actions",
        ["import_job_id"],
        unique=False,
    )
    op.create_index(
        "ix_import_job_actions_job_seq",
        "import_job_actions",
        ["import_job_id", "sequence_no"],
        unique=False,
    )
    op.create_index(
        "ix_import_job_actions_job_status",
        "import_job_actions",
        ["import_job_id", "status"],
        unique=False,
    )

    op.execute(
        sa.text(
            """
            INSERT INTO system_config (key, value, value_type)
            VALUES (:key, :value, :value_type)
            ON CONFLICT(key) DO NOTHING
            """
        ).bindparams(
            key="convert_to_preferred_format_on_import",
            value="false",
            value_type="bool",
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        sa.text("DELETE FROM system_config WHERE key = :key").bindparams(
            key="convert_to_preferred_format_on_import"
        )
    )

    op.drop_index("ix_import_job_actions_job_status", table_name="import_job_actions")
    op.drop_index("ix_import_job_actions_job_seq", table_name="import_job_actions")
    op.drop_index(op.f("ix_import_job_actions_import_job_id"), table_name="import_job_actions")
    op.drop_table("import_job_actions")

    with op.batch_alter_table("import_files") as batch_op:
        batch_op.drop_column("matched_issue_cv_id")

    with op.batch_alter_table("import_jobs") as batch_op:
        batch_op.drop_column("convert_to_preferred_format")
        batch_op.drop_column("transfer_method")
        batch_op.drop_column("progress_snapshot")
        batch_op.drop_column("selected_file_paths")
