"""Add restart-safe post-processing claims.

Revision ID: u6v7w8x9y012
Revises: t5u6v7w8x901
Create Date: 2026-08-25
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "u6v7w8x9y012"
down_revision: str | Sequence[str] | None = "t5u6v7w8x901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("download_history") as batch_op:
        batch_op.add_column(
            sa.Column("post_processing_claimed_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("post_processing_claim_token", sa.String(length=64), nullable=True)
        )
        batch_op.create_index(
            "ix_download_history_post_processing_claim",
            ["state", "post_processing_claimed_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("download_history") as batch_op:
        batch_op.drop_index("ix_download_history_post_processing_claim")
        batch_op.drop_column("post_processing_claim_token")
        batch_op.drop_column("post_processing_claimed_at")
