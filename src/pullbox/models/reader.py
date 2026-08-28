"""Private per-user embedded-reader resume and completion state."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - SQLAlchemy needs this at runtime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, UniqueConstraint, false, text
from sqlalchemy.orm import Mapped, mapped_column

from pullbox.models.base import Base, IdentityMixin, TimestampMixin, UTCDateTime


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
