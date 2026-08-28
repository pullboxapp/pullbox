"""Index library-file joins by canonical issue identity.

Revision ID: q2r3s4t5u678
Revises: p1q2r3s4t567
Create Date: 2026-08-25
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "q2r3s4t5u678"
down_revision: str | None = "p1q2r3s4t567"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_library_files_issue",
        "library_files",
        ["issue_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_library_files_issue", table_name="library_files")
