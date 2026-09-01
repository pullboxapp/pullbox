"""Add first-class Story Arc cover storage.

Revision ID: f7a8b9c0d123
Revises: e6f7a8b9c012
Create Date: 2026-08-31
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "f7a8b9c0d123"
down_revision: str | Sequence[str] | None = "e6f7a8b9c012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Store provider and locally cached Story Arc artwork explicitly."""
    with op.batch_alter_table("story_arcs") as batch_op:
        batch_op.add_column(sa.Column("cover_path", sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column("cover_url", sa.String(length=500), nullable=True))


def downgrade() -> None:
    """Remove first-class Story Arc artwork fields."""
    if op.get_bind().dialect.name == "sqlite":
        # Native DROP preserves a2's inline foreign-key definitions. A batch
        # rebuild turns them into table-level constraints that a2's later
        # downgrade cannot remove column-by-column.
        op.drop_column("story_arcs", "cover_url")
        op.drop_column("story_arcs", "cover_path")
        return
    with op.batch_alter_table("story_arcs") as batch_op:
        batch_op.drop_column("cover_url")
        batch_op.drop_column("cover_path")
