"""Add cover_url column to series and issues tables.

Stores the ComicVine CDN URL so covers can be displayed immediately
from the CDN while local copies are downloaded in the background.

Revision ID: i4d5e6f7g890
Revises: h3c4d5e6f789
Create Date: 2026-03-01
"""

import sqlalchemy as sa

from alembic import op

revision = "i4d5e6f7g890"
down_revision = "e23015086078"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    return any(col["name"] == column for col in sa.inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    if not _column_exists("series", "cover_url"):
        op.add_column("series", sa.Column("cover_url", sa.String(500), nullable=True))
    if not _column_exists("issues", "cover_url"):
        op.add_column("issues", sa.Column("cover_url", sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column("issues", "cover_url")
    op.drop_column("series", "cover_url")
