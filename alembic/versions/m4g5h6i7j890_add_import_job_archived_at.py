"""Add non-destructive import history archival.

Revision ID: m4g5h6i7j890
Revises: l3f4a5b6c789
Create Date: 2026-09-04
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "m4g5h6i7j890"
down_revision: str | Sequence[str] | None = "l3f4a5b6c789"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable archive timestamp and history lookup index."""
    op.add_column(
        "import_jobs",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_import_jobs_archived_created",
        "import_jobs",
        ["archived_at", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Remove non-destructive import archival support."""
    op.drop_index("ix_import_jobs_archived_created", table_name="import_jobs")
    op.drop_column("import_jobs", "archived_at")
