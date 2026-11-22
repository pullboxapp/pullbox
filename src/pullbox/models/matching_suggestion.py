"""MatchingSuggestion ORM model — smart matching suggestions for unmatched files.

When a library file appears to be an annual, TPB, or other variant of
an existing series but no matching series exists in the database, the
matching service creates a suggestion that the user can accept with one click.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pullbox.models.base import Base, IdentityMixin, TimestampMixin

if TYPE_CHECKING:
    from pullbox.models.library import LibraryFile
    from pullbox.models.series import Series


class SuggestionStatus(enum.StrEnum):
    """Status of a matching suggestion."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    DISMISSED = "dismissed"


class MatchingSuggestion(Base, IdentityMixin, TimestampMixin):
    """A suggested series to add for an unmatched file.

    Created when the matching service detects a file that looks like a
    variant (annual, TPB, etc.) of an existing series but can't find
    the corresponding CV volume in the local database.
    """

    __tablename__ = "matching_suggestions"
    __table_args__ = (
        Index("ix_matching_suggestions_status", "status"),
        Index("ix_matching_suggestions_file", "library_file_id"),
    )

    # The unmatched file that triggered this suggestion
    library_file_id: Mapped[int] = mapped_column(
        ForeignKey("library_files.id", ondelete="CASCADE"),
        nullable=False,
    )

    # The existing parent series the file appears to relate to
    parent_series_id: Mapped[int | None] = mapped_column(
        ForeignKey("series.id", ondelete="SET NULL"),
    )

    # The suggested ComicVine volume to add
    suggested_comicvine_id: Mapped[int | None] = mapped_column(Integer)
    suggested_title: Mapped[str | None] = mapped_column(String(500))
    suggested_year: Mapped[int | None] = mapped_column(Integer)
    suggested_publisher: Mapped[str | None] = mapped_column(String(200))
    suggested_series_type: Mapped[str] = mapped_column(
        String(50),
        default="standard",
    )

    # How the suggestion was determined
    detection_source: Mapped[str] = mapped_column(
        String(50),
        default="filename",
    )  # "filename", "comicinfo", "cv_title"
    confidence_score: Mapped[float] = mapped_column(default=0.0)
    reason: Mapped[str | None] = mapped_column(Text)

    # Status tracking
    status: Mapped[SuggestionStatus] = mapped_column(
        SQLAlchemyEnum(SuggestionStatus, values_callable=lambda cls: [item.value for item in cls]),
        default=SuggestionStatus.PENDING,
        server_default=SuggestionStatus.PENDING.value,
    )

    # Relationships
    library_file: Mapped[LibraryFile] = relationship()
    parent_series: Mapped[Series | None] = relationship()
