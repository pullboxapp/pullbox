"""Add durable library-file ownership and source signatures.

Revision ID: x9y0z1a2b345
Revises: w8x9y0z1a234
Create Date: 2026-08-29
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "x9y0z1a2b345"
down_revision: str | Sequence[str] | None = "w8x9y0z1a234"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Backfill existing artifacts as managed and add signature storage."""
    with op.batch_alter_table("library_files") as batch_op:
        batch_op.add_column(
            sa.Column(
                "storage_mode",
                sa.Enum(
                    "managed",
                    "referenced",
                    name="libraryfilestoragemode",
                    native_enum=False,
                    create_constraint=True,
                ),
                nullable=False,
                server_default="managed",
            )
        )
        batch_op.add_column(
            sa.Column(
                "source_signature",
                sa.JSON(),
                nullable=False,
                server_default="{}",
            )
        )

    with op.batch_alter_table("import_files") as batch_op:
        batch_op.add_column(
            sa.Column(
                "source_signature",
                sa.JSON(),
                nullable=False,
                server_default="{}",
            )
        )


def downgrade() -> None:
    """Remove ownership fields after referenced-row safety is handled externally."""
    referenced_count = (
        op.get_bind()
        .execute(sa.text("SELECT COUNT(*) FROM library_files WHERE storage_mode = 'referenced'"))
        .scalar_one()
    )
    if referenced_count:
        raise RuntimeError("Cannot downgrade library-file ownership while referenced files remain.")

    with op.batch_alter_table("import_files") as batch_op:
        batch_op.drop_column("source_signature")

    with op.batch_alter_table("library_files") as batch_op:
        batch_op.drop_constraint("libraryfilestoragemode", type_="check")
        batch_op.drop_column("source_signature")
        batch_op.drop_column("storage_mode")
