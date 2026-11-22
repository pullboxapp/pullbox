"""Add series_type, parent_series_id, and matching_suggestions table.

Revision ID: h3c4d5e6f789
Revises: g2b3d4e5f678_series_folders_and_issue_type
Create Date: 2026-03-01
"""

import sqlalchemy as sa

from alembic import op

revision = "h3c4d5e6f789"
down_revision = "g2b3d4e5f678"
branch_labels = None
depends_on = None

_SERIES_TYPE = sa.Enum(
    "STANDARD",
    "ANNUAL",
    "TPB",
    "OMNIBUS",
    "GRAPHIC_NOVEL",
    "HARDCOVER",
    "ONE_SHOT",
    "SPECIAL",
    "COMPENDIUM",
    "DELUXE",
    name="seriestype",
)
_SUGGESTION_STATUS = sa.Enum("pending", "accepted", "dismissed", name="suggestionstatus")


def _column_exists(table: str, column: str) -> bool:
    return any(col["name"] == column for col in sa.inspect(op.get_bind()).get_columns(table))


def _index_exists(index_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_index("series", index_name)


def upgrade() -> None:
    # Add series_type column to series table
    if not _column_exists("series", "series_type"):
        if op.get_bind().dialect.name == "postgresql":
            _SERIES_TYPE.create(op.get_bind(), checkfirst=True)
        op.add_column(
            "series",
            sa.Column(
                "series_type",
                _SERIES_TYPE,
                nullable=False,
                server_default="STANDARD",
            ),
        )
    if not _index_exists("ix_series_series_type"):
        op.create_index("ix_series_series_type", "series", ["series_type"])

    # Add parent_series_id column (skip if already present from partial run)
    if not _column_exists("series", "parent_series_id"):
        op.add_column(
            "series",
            sa.Column("parent_series_id", sa.Integer(), nullable=True),
        )
    if not _index_exists("ix_series_parent_series_id"):
        op.create_index("ix_series_parent_series_id", "series", ["parent_series_id"])

    # Create matching_suggestions table (skip if already exists)
    if sa.inspect(op.get_bind()).has_table("matching_suggestions"):
        return
    op.create_table(
        "matching_suggestions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "library_file_id",
            sa.Integer(),
            sa.ForeignKey("library_files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_series_id",
            sa.Integer(),
            sa.ForeignKey("series.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("suggested_comicvine_id", sa.Integer(), nullable=True),
        sa.Column("suggested_title", sa.String(500), nullable=True),
        sa.Column("suggested_year", sa.Integer(), nullable=True),
        sa.Column("suggested_publisher", sa.String(200), nullable=True),
        sa.Column("suggested_series_type", sa.String(50), server_default="standard"),
        sa.Column("detection_source", sa.String(50), server_default="filename"),
        sa.Column("confidence_score", sa.Float(), server_default="0.0"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "status",
            _SUGGESTION_STATUS,
            server_default="pending",
            nullable=False,
        ),
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
    )
    op.create_index(
        "ix_matching_suggestions_status",
        "matching_suggestions",
        ["status"],
    )
    op.create_index(
        "ix_matching_suggestions_file",
        "matching_suggestions",
        ["library_file_id"],
    )


def downgrade() -> None:
    op.drop_table("matching_suggestions")
    op.drop_index("ix_series_parent_series_id", "series")
    with op.batch_alter_table("series", schema=None) as batch_op:
        batch_op.drop_column("parent_series_id")
    op.drop_index("ix_series_series_type", "series")
    op.drop_column("series", "series_type")
    if op.get_bind().dialect.name == "postgresql":
        _SUGGESTION_STATUS.drop(op.get_bind(), checkfirst=True)
        _SERIES_TYPE.drop(op.get_bind(), checkfirst=True)
