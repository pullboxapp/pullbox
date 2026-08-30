"""Add durable Step 1 story-arc import intent.

Revision ID: d5e6f7a8b901
Revises: c4d5e6f7a890
Create Date: 2026-08-30
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "d5e6f7a8b901"
down_revision: str | Sequence[str] | None = "c4d5e6f7a890"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _intent_columns() -> tuple[sa.Column[bool], sa.Column[bool]]:
    """Return fresh column objects for SQLite/native and batch operations."""
    return (
        sa.Column(
            "story_arc_import_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "story_arc_materialization_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def upgrade() -> None:
    """Record compatible, non-authorizing Story Arc wizard choices."""
    import_requested, materialization_requested = _intent_columns()
    if op.get_bind().dialect.name == "sqlite":
        # Both fields are simple constant-default additions. Native ALTER keeps
        # c4's rollback-work foreign key exactly as introduced; a batch rebuild
        # here changes that older FK into a shape SQLite cannot later drop.
        op.add_column("import_jobs", import_requested)
        op.add_column("import_jobs", materialization_requested)
        return
    with op.batch_alter_table("import_jobs") as batch_op:
        batch_op.add_column(import_requested)
        batch_op.add_column(materialization_requested)


def downgrade() -> None:
    """Remove Story Arc wizard intent fields."""
    if op.get_bind().dialect.name == "sqlite":
        op.drop_column("import_jobs", "story_arc_materialization_requested")
        op.drop_column("import_jobs", "story_arc_import_requested")
        return
    with op.batch_alter_table("import_jobs") as batch_op:
        batch_op.drop_column("story_arc_materialization_requested")
        batch_op.drop_column("story_arc_import_requested")
