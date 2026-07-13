"""Add the paused import job status.

Revision ID: c8e1f4a7b902
Revises: a5d6e7f8g901
Create Date: 2026-07-12
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "c8e1f4a7b902"
down_revision: str | Sequence[str] | None = "a5d6e7f8g901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the PostgreSQL enum value used by paused import jobs."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE importjobstatus ADD VALUE IF NOT EXISTS 'PAUSED'")


def downgrade() -> None:
    """Leave the PostgreSQL enum value in place."""
    # PostgreSQL enum value removal is intentionally omitted.
    pass
