"""add duplicate merge review fields

Revision ID: o0j1k2l3m456
Revises: n9i0j1k2l345
Create Date: 2026-04-29 18:15:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "o0j1k2l3m456"
down_revision: str | Sequence[str] | None = "n9i0j1k2l345"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("import_jobs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "total_files_already_owned",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )

    with op.batch_alter_table("import_series") as batch_op:
        batch_op.add_column(
            sa.Column(
                "files_already_owned",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )

    with op.batch_alter_table("import_files") as batch_op:
        batch_op.add_column(
            sa.Column(
                "include_in_import",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("import_files") as batch_op:
        batch_op.drop_column("include_in_import")

    with op.batch_alter_table("import_series") as batch_op:
        batch_op.drop_column("files_already_owned")

    with op.batch_alter_table("import_jobs") as batch_op:
        batch_op.drop_column("total_files_already_owned")
