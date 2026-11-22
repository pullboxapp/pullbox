"""add search_log details columns

Revision ID: b7a3c5d9e1f2
Revises: 49d9e835fbe2
Create Date: 2026-03-05 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7a3c5d9e1f2"
down_revision: str | Sequence[str] | None = "49d9e835fbe2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add details and best_confidence columns to search_logs; seed config key."""
    op.add_column(
        "search_logs",
        sa.Column("details", sa.JSON(), server_default="{}", nullable=False),
    )
    op.add_column(
        "search_logs",
        sa.Column("best_confidence", sa.String(length=20), nullable=True),
    )

    # Seed search_log_retention_days config key
    op.execute(
        sa.text(
            "INSERT INTO system_config (key, value, value_type) "
            "VALUES ('search_log_retention_days', '7', 'int') ON CONFLICT (key) DO NOTHING"
        )
    )


def downgrade() -> None:
    """Remove details and best_confidence columns from search_logs."""
    op.drop_column("search_logs", "best_confidence")
    op.drop_column("search_logs", "details")

    op.execute(sa.text("DELETE FROM system_config WHERE key = 'search_log_retention_days'"))
