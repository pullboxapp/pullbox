"""Index import-file references to matched issues.

Revision ID: j1e2f3a4b567
Revises: i0d1e2f3a456
Create Date: 2026-09-01
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "j1e2f3a4b567"
down_revision: str | Sequence[str] | None = "i0d1e2f3a456"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Avoid full import-file scans when an issue is deleted or rolled back."""
    op.create_index(
        "ix_import_files_matched_issue_id",
        "import_files",
        ["matched_issue_id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the matched-issue lookup index."""
    op.drop_index(
        "ix_import_files_matched_issue_id",
        table_name="import_files",
    )
