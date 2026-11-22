"""Private per-user embedded-reader resume and completion state."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - SQLAlchemy needs this at runtime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, UniqueConstraint, false, text
from sqlalchemy.orm import Mapped, mapped_column

from pullbox.models.base import Base, IdentityMixin, TimestampMixin, UTCDateTime


class StoryArcReaderBookmark(Base, IdentityMixin, TimestampMixin):
    """A user's sequence position, never a second copy of comic page progress."""

    __tablename__ = "story_arc_reader_bookmarks"
    __table_args__ = (
        UniqueConstraint("user_id", "story_arc_id", name="uq_story_arc_reader_user_arc"),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    story_arc_id: Mapped[int] = mapped_column(
        ForeignKey("story_arcs.id", ondelete="CASCADE"), nullable=False
    )
    entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("issue_story_arcs.id", ondelete="SET NULL")
    )
    issue_id: Mapped[int | None] = mapped_column(ForeignKey("issues.id", ondelete="SET NULL"))


class SeriesReaderBookmark(Base, IdentityMixin, TimestampMixin):
    """The user's last issue in series order, independent of any arc."""

    __tablename__ = "series_reader_bookmarks"
    __table_args__ = (
        UniqueConstraint("user_id", "series_id", name="uq_series_reader_user_series"),
        Index("ix_series_reader_bookmarks_series", "series_id"),
        Index("ix_series_reader_bookmarks_issue", "issue_id"),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    series_id: Mapped[int] = mapped_column(ForeignKey("series.id", ondelete="CASCADE"))
    issue_id: Mapped[int | None] = mapped_column(ForeignKey("issues.id", ondelete="SET NULL"))


class IssueReaderState(Base, IdentityMixin, TimestampMixin):
    """Private reader state, intentionally separate from acquisition status."""

    __tablename__ = "issue_reader_states"
    __table_args__ = (
        UniqueConstraint("user_id", "issue_id", name="uq_issue_reader_state_user_issue"),
        Index("ix_issue_reader_states_issue", "issue_id"),
        Index("ix_issue_reader_states_user_last_opened", "user_id", "last_opened_at"),
        Index(
            "ix_issue_reader_states_user_want_updated",
            "user_id",
            "want_to_read",
            "want_to_read_updated_at",
        ),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    issue_id: Mapped[int] = mapped_column(
        ForeignKey("issues.id", ondelete="CASCADE"), nullable=False
    )
    last_page_index: Mapped[int | None] = mapped_column(Integer)
    content_revision: Mapped[str | None] = mapped_column(String(64))
    page_count: Mapped[int | None] = mapped_column(Integer)
    progress_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_opened_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completion_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    want_to_read: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
    want_to_read_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    state_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
