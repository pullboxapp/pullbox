"""Add durable Mylar path-mapping confirmation.

Revision ID: g8b9c0d1e234
Revises: f7a8b9c0d123
Create Date: 2026-08-31
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "g8b9c0d1e234"
down_revision: str | Sequence[str] | None = "f7a8b9c0d123"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Distinguish a confirmed identity map from legacy auto-detection state."""
    op.add_column(
        "import_jobs",
        sa.Column(
            "mylar3_path_map_confirmed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    """Remove durable Mylar path-mapping confirmation."""
    op.drop_column("import_jobs", "mylar3_path_map_confirmed")
