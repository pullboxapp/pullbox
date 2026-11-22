"""Preserve large byte counts and existing import decisions on PostgreSQL."""

import sqlalchemy as sa

from alembic import op

revision = "o2c3456789ab"
down_revision = "n1b23456789a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column(
            "import_files", "file_size", existing_type=sa.Integer(), type_=sa.BigInteger()
        )
        op.execute("ALTER TYPE importedfilestatus ADD VALUE IF NOT EXISTS 'ALREADY_OWNED'")
        op.execute("ALTER TYPE importedfilestatus ADD VALUE IF NOT EXISTS 'DUPLICATE_FILE'")
        op.execute("ALTER TYPE importedfilestatus ADD VALUE IF NOT EXISTS 'SAFETY_APPROVED'")
        op.execute("ALTER TYPE seriestype ADD VALUE IF NOT EXISTS 'VOLUME'")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        # Refuse the narrowing conversion without changing rows. The additive labels
        # remain so a downgrade never erases a recorded review decision.
        if op.get_bind().scalar(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM import_files WHERE file_size NOT BETWEEN "
                "-2147483648 AND 2147483647)"
            )
        ):
            raise RuntimeError("Cannot narrow import byte counts without losing history.")
        op.alter_column(
            "import_files", "file_size", existing_type=sa.BigInteger(), type_=sa.Integer()
        )
