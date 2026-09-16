"""Index staged-file references used by database-side cascade deletes.

Revision ID: l3f4a5b6c789
Revises: k2f3a4b5c678
Create Date: 2026-09-01
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "l3f4a5b6c789"
down_revision: str | Sequence[str] | None = "k2f3a4b5c678"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Avoid full child-table scans while staged import files are deleted."""
    op.create_index(
        "ix_import_files_duplicate_of_file_id",
        "import_files",
        ["duplicate_of_file_id"],
        unique=False,
    )
    op.create_index(
        "ix_import_story_arc_entries_import_file_id",
        "import_story_arc_entries",
        ["import_file_id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove staged-file cascade support indexes."""
    op.drop_index(
        "ix_import_story_arc_entries_import_file_id",
        table_name="import_story_arc_entries",
    )
    op.drop_index(
        "ix_import_files_duplicate_of_file_id",
        table_name="import_files",
    )
