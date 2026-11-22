"""Remember the last settled chapter per user and arc, independently of comic progress."""

import sqlalchemy as sa

from alembic import op

revision = "j7d8e9f01234"
down_revision = "i6c7d8e9f012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "story_arc_reader_bookmarks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "story_arc_id",
            sa.Integer(),
            sa.ForeignKey("story_arcs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "entry_id", sa.Integer(), sa.ForeignKey("issue_story_arcs.id", ondelete="SET NULL")
        ),
        sa.Column("issue_id", sa.Integer(), sa.ForeignKey("issues.id", ondelete="SET NULL")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("user_id", "story_arc_id", name="uq_story_arc_reader_user_arc"),
    )


def downgrade() -> None:
    bookmarks = sa.table("story_arc_reader_bookmarks")
    if op.get_bind().scalar(sa.select(sa.func.count()).select_from(bookmarks)):
        raise RuntimeError("Cannot downgrade while private story arc bookmarks exist.")
    op.drop_table("story_arc_reader_bookmarks")
