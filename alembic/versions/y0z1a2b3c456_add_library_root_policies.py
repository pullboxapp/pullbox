"""Add complete per-root library naming policies.

Revision ID: y0z1a2b3c456
Revises: x9y0z1a2b345
Create Date: 2026-08-29
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "y0z1a2b3c456"
down_revision: str | Sequence[str] | None = "x9y0z1a2b345"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create root policy storage without inventing overrides for existing roots."""
    op.create_table(
        "library_root_policies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("library_root_id", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("series_path_template", sa.String(length=1024), nullable=False),
        sa.Column("comic_file_template", sa.String(length=1024), nullable=False),
        sa.Column("annual_file_template", sa.String(length=1024), nullable=False),
        sa.Column("non_standard_file_template", sa.String(length=1024), nullable=False),
        sa.Column(
            "single_non_standard_file_template",
            sa.String(length=1024),
            nullable=False,
        ),
        sa.Column("replace_illegal_characters", sa.Boolean(), nullable=False),
        sa.Column("colon_replacement", sa.String(length=16), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "global_default",
                "import_adoption",
                "manual",
                name="libraryrootpolicysource",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("source_import_job_id", sa.Integer(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["library_root_id"],
            ["library_roots.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_import_job_id"],
            ["import_jobs.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("library_root_id"),
    )


def downgrade() -> None:
    """Remove per-root naming policies and return to global fallback only."""
    op.drop_table("library_root_policies")
