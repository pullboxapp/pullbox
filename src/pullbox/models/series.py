"""Series ORM model and related enums."""

from __future__ import annotations

import enum
from datetime import datetime  # noqa: TC003 - SQLAlchemy needs this at runtime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pullbox.models.base import Base, IdentityMixin, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from pullbox.models.issue import Issue
    from pullbox.models.library import LibraryRoot
    from pullbox.models.publisher import Publisher


class SeriesType(enum.StrEnum):
    """Classification of series format/type for organization."""

    STANDARD = "standard"
    ANNUAL = "annual"
    TPB = "tpb"
    OMNIBUS = "omnibus"
    GRAPHIC_NOVEL = "graphic_novel"
    HARDCOVER = "hardcover"
    ONE_SHOT = "one_shot"
    SPECIAL = "special"
    COMPENDIUM = "compendium"
    DELUXE = "deluxe"
    VOLUME = "volume"

    @property
    def display_name(self) -> str:
        """Human-readable display name."""
        return _SERIES_TYPE_DISPLAY_NAMES.get(self, self.value.replace("_", " ").title())

    @property
    def ui_display_name(self) -> str:
        """UI-facing display name for badges and labels."""
        return _SERIES_TYPE_UI_DISPLAY_NAMES.get(self, self.display_name)


_SERIES_TYPE_DISPLAY_NAMES: dict[SeriesType, str] = {
    SeriesType.STANDARD: "Standard",
    SeriesType.ANNUAL: "Annual",
    SeriesType.TPB: "TPB",
    SeriesType.OMNIBUS: "Omnibus",
    SeriesType.GRAPHIC_NOVEL: "GN",
    SeriesType.HARDCOVER: "Hardcover",
    SeriesType.ONE_SHOT: "One-Shot",
    SeriesType.SPECIAL: "Special",
    SeriesType.COMPENDIUM: "Compendium",
    SeriesType.DELUXE: "Deluxe",
}

_SERIES_TYPE_UI_DISPLAY_NAMES: dict[SeriesType, str] = {
    **_SERIES_TYPE_DISPLAY_NAMES,
    SeriesType.GRAPHIC_NOVEL: "Graphic Novel",
}


class SeriesStatus(enum.StrEnum):
    CONTINUING = "continuing"
    ENDED = "ended"
    UNKNOWN = "unknown"


class SeriesStatusOverride(enum.StrEnum):
    """User-owned lifecycle states that metadata refreshes must preserve."""

    CONTINUING = "continuing"
    ENDED = "ended"


class IssueCatalogState(enum.StrEnum):
    """Whether the local issue list is complete for this ComicVine series."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    HYDRATING = "hydrating"
    FAILED = "failed"


class Series(Base, IdentityMixin, TimestampMixin):
    __tablename__ = "series"
    __table_args__ = (Index("ix_series_title_year", "title", "year_start"),)

    comicvine_id: Mapped[int | None] = mapped_column(unique=True, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    sort_title: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    year_start: Mapped[int | None] = mapped_column(Integer)
    year_end: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[SeriesStatus] = mapped_column(
        SQLAlchemyEnum(SeriesStatus), default=SeriesStatus.CONTINUING
    )
    status_override: Mapped[SeriesStatusOverride | None] = mapped_column(
        SQLAlchemyEnum(SeriesStatusOverride),
        nullable=True,
    )
    description: Mapped[str | None] = mapped_column(Text)
    cover_path: Mapped[str | None] = mapped_column(String(500))
    cover_url: Mapped[str | None] = mapped_column(String(500))
    issue_count: Mapped[int] = mapped_column(Integer, default=0)
    comicvine_url: Mapped[str | None] = mapped_column(String(500))
    issue_catalog_state: Mapped[IssueCatalogState] = mapped_column(
        SQLAlchemyEnum(IssueCatalogState),
        default=IssueCatalogState.COMPLETE,
        server_default=IssueCatalogState.COMPLETE.name,
        nullable=False,
        index=True,
    )
    issue_catalog_last_synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    issue_catalog_last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    issue_catalog_error: Mapped[str | None] = mapped_column(Text)

    # Monitoring — binary on/off, controls auto-search and issue status transitions
    monitored: Mapped[bool] = mapped_column(default=False, index=True)

    # Metadata management
    metadata_last_refreshed: Mapped[datetime | None] = mapped_column(UTCDateTime)
    metadata_source: Mapped[str | None] = mapped_column(String(50))

    # Series folder (set when added to a library root)
    path: Mapped[str | None] = mapped_column(String(1000))

    # Series classification and hierarchy
    series_type: Mapped[SeriesType] = mapped_column(
        SQLAlchemyEnum(SeriesType),
        default=SeriesType.STANDARD,
        index=True,
    )
    parent_series_id: Mapped[int | None] = mapped_column(
        ForeignKey("series.id", ondelete="SET NULL"),
        index=True,
    )

    # Alternate names for matching (e.g., "TMNT" for "Teenage Mutant Ninja Turtles")
    alternate_names: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")

    # Foreign keys
    publisher_id: Mapped[int | None] = mapped_column(
        ForeignKey("publishers.id", ondelete="SET NULL")
    )
    library_root_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_roots.id", ondelete="RESTRICT")
    )
    preferred_library_root_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_roots.id", ondelete="RESTRICT")
    )

    # Relationships
    publisher: Mapped[Publisher | None] = relationship(back_populates="series")
    library_root: Mapped[LibraryRoot | None] = relationship(
        back_populates="series",
        foreign_keys=[library_root_id],
    )
    preferred_library_root: Mapped[LibraryRoot | None] = relationship(
        back_populates="preferred_series",
        foreign_keys=[preferred_library_root_id],
    )
    issues: Mapped[list[Issue]] = relationship(
        back_populates="series", cascade="all, delete-orphan"
    )
    parent_series: Mapped[Series | None] = relationship(
        "Series",
        remote_side="Series.id",
        foreign_keys=[parent_series_id],
        back_populates="child_series",
    )
    child_series: Mapped[list[Series]] = relationship(
        "Series",
        foreign_keys=[parent_series_id],
        back_populates="parent_series",
    )
