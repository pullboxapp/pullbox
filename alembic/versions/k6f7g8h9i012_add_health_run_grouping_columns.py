"""Add health run grouping columns.

Revision ID: k6f7g8h9i012
Revises: j5e6f7g8h901
Create Date: 2026-04-21
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "k6f7g8h9i012"
down_revision = "j5e6f7g8h901"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add run grouping metadata to persisted health check rows."""
    op.add_column(
        "health_check_results",
        sa.Column("run_id", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "health_check_results",
        sa.Column(
            "is_summary",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.create_index(
        "ix_health_check_component_summary_time",
        "health_check_results",
        ["component", "is_summary", "checked_at"],
        unique=False,
    )
    op.create_index(
        "ix_health_check_component_run",
        "health_check_results",
        ["component", "run_id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove run grouping metadata from persisted health check rows."""
    op.drop_index("ix_health_check_component_run", table_name="health_check_results")
    op.drop_index("ix_health_check_component_summary_time", table_name="health_check_results")
    op.drop_column("health_check_results", "is_summary")
    op.drop_column("health_check_results", "run_id")
