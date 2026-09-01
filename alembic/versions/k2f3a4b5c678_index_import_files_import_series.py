"""Index import-file references to their staged series.

Revision ID: k2f3a4b5c678
Revises: j1e2f3a4b567
Create Date: 2026-09-01
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "k2f3a4b5c678"
down_revision: str | Sequence[str] | None = "j1e2f3a4b567"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Avoid full import-file scans for per-series matching and cleanup."""
    op.create_index(
        "ix_import_files_import_series_id",
        "import_files",
        ["import_series_id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the staged-series lookup index."""
    op.drop_index(
        "ix_import_files_import_series_id",
        table_name="import_files",
    )
