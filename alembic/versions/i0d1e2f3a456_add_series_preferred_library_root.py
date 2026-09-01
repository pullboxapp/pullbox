"""Add a durable preferred managed destination to series.

Revision ID: i0d1e2f3a456
Revises: h9c0d1e2f345
Create Date: 2026-08-31
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "i0d1e2f3a456"
down_revision: str | Sequence[str] | None = "h9c0d1e2f345"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FOREIGN_KEY = "fk_series_preferred_library_root_id"


def upgrade() -> None:
    """Add the preferred root and retain managed current roots as the default."""
    with op.batch_alter_table("series") as batch_op:
        batch_op.add_column(sa.Column("preferred_library_root_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            _FOREIGN_KEY,
            "library_roots",
            ["preferred_library_root_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.execute(
        sa.text(
            "UPDATE series SET preferred_library_root_id = library_root_id "
            "WHERE library_root_id IN ("
            "SELECT id FROM library_roots WHERE allow_managed_writes = true"
            ")"
        )
    )


def downgrade() -> None:
    """Remove the independent preferred destination from series."""
    nonrepresentable_count = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT COUNT(*) FROM series "
                "WHERE preferred_library_root_id IS NOT NULL "
                "AND (library_root_id IS NULL "
                "OR preferred_library_root_id != library_root_id)"
            )
        )
        .scalar_one()
    )
    if nonrepresentable_count:
        raise RuntimeError(
            "Cannot downgrade series preferred roots while a series uses a preferred "
            "managed destination different from its current library root."
        )

    with op.batch_alter_table("series") as batch_op:
        batch_op.drop_constraint(_FOREIGN_KEY, type_="foreignkey")
        batch_op.drop_column("preferred_library_root_id")
