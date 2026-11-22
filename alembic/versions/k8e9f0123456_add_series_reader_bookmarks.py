"""Remember the user's place in a series without inferring it from arc activity."""

import sqlalchemy as sa

from alembic import op

revision = "k8e9f0123456"
down_revision = "j7d8e9f01234"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "series_reader_bookmarks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "series_id",
            sa.Integer(),
            sa.ForeignKey("series.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("issue_id", sa.Integer(), sa.ForeignKey("issues.id", ondelete="SET NULL")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("user_id", "series_id", name="uq_series_reader_user_series"),
    )
    op.create_index("ix_series_reader_bookmarks_series", "series_reader_bookmarks", ["series_id"])
    op.create_index("ix_series_reader_bookmarks_issue", "series_reader_bookmarks", ["issue_id"])


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.select(sa.func.count()).select_from(sa.table("series_reader_bookmarks"))
    ):
        raise RuntimeError("Cannot downgrade while private series bookmarks exist.")
    op.drop_table("series_reader_bookmarks")
