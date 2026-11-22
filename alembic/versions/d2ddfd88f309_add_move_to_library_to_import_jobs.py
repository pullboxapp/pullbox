"""add move_to_library to import_jobs

Revision ID: d2ddfd88f309
Revises: 085ebdab96ee
Create Date: 2026-03-12 17:58:31.757874

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2ddfd88f309"
down_revision: str | Sequence[str] | None = "085ebdab96ee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add move_to_library column to import_jobs."""
    op.add_column(
        "import_jobs",
        sa.Column("move_to_library", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    """Remove move_to_library column."""
    op.drop_column("import_jobs", "move_to_library")
