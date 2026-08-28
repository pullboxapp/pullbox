"""Evolve private reader state into independent semantic dimensions.

Revision ID: p1q2r3s4t567
Revises: o0p1q2r3s456
Create Date: 2026-08-25

Downgrade is intentionally destructive for queue-only, manual-read-only, and
explicit-unread-only rows because the previous schema requires page progress.
Feature-flag rollback is preferred when preserving those intents matters.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "p1q2r3s4t567"
down_revision: str | None = "o0p1q2r3s456"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("issue_reader_states") as batch_op:
        batch_op.add_column(sa.Column("progress_updated_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("last_opened_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("completion_updated_at", sa.DateTime(timezone=True)))
        batch_op.add_column(
            sa.Column(
                "want_to_read",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("want_to_read_updated_at", sa.DateTime(timezone=True)))
        batch_op.add_column(
            sa.Column(
                "state_version",
                sa.Integer(),
                server_default=sa.text("1"),
                nullable=False,
            )
        )
        batch_op.alter_column(
            "last_page_index",
            existing_type=sa.Integer(),
            nullable=True,
            server_default=None,
        )
        batch_op.alter_column(
            "content_revision",
            existing_type=sa.String(length=64),
            nullable=True,
        )
        batch_op.alter_column(
            "page_count",
            existing_type=sa.Integer(),
            nullable=True,
        )

    op.execute(
        sa.text(
            "UPDATE issue_reader_states "
            "SET progress_updated_at = updated_at, "
            "last_opened_at = updated_at, "
            "completion_updated_at = completed_at"
        )
    )
    op.create_index(
        "ix_issue_reader_states_user_last_opened",
        "issue_reader_states",
        ["user_id", "last_opened_at"],
    )
    op.create_index(
        "ix_issue_reader_states_user_want_updated",
        "issue_reader_states",
        ["user_id", "want_to_read", "want_to_read_updated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_issue_reader_states_user_want_updated",
        table_name="issue_reader_states",
    )
    op.drop_index(
        "ix_issue_reader_states_user_last_opened",
        table_name="issue_reader_states",
    )
    op.execute(
        sa.text(
            "DELETE FROM issue_reader_states "
            "WHERE last_page_index IS NULL "
            "OR content_revision IS NULL "
            "OR page_count IS NULL"
        )
    )
    with op.batch_alter_table("issue_reader_states") as batch_op:
        batch_op.alter_column(
            "last_page_index",
            existing_type=sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        )
        batch_op.alter_column(
            "content_revision",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch_op.alter_column(
            "page_count",
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch_op.drop_column("state_version")
        batch_op.drop_column("want_to_read_updated_at")
        batch_op.drop_column("want_to_read")
        batch_op.drop_column("completion_updated_at")
        batch_op.drop_column("last_opened_at")
        batch_op.drop_column("progress_updated_at")
