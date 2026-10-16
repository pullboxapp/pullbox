"""Durable discovery intent, independent of replaceable release caches."""

from datetime import date
from enum import StrEnum

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column

from pullbox.models.base import Base, IdentityMixin, TimestampMixin


class SeriesInterestState(StrEnum):
    WATCHING = "watching"
    NEEDS_CONFIRMATION = "needs_confirmation"
    PROMOTED = "promoted"
    CANCELLED = "cancelled"


class SeriesInterest(Base, IdentityMixin, TimestampMixin):
    """One reusable intent per exact source identity; titles are display snapshots."""

    __tablename__ = "series_interests"
    __table_args__ = (
        UniqueConstraint("source_namespace", "source_series_id", name="uq_series_interest_source"),
        CheckConstraint("source_namespace = 'locg'", name="ck_series_interest_namespace"),
        Index("ix_series_interests_state", "state"),
    )

    source_namespace: Mapped[str] = mapped_column(String(32), default="locg")
    source_series_id: Mapped[str] = mapped_column(String(32))
    title_snapshot: Mapped[str] = mapped_column(String(512))
    publisher_snapshot: Mapped[str] = mapped_column(String(255), default="")
    year_snapshot: Mapped[int | None]
    next_known_release_date: Mapped[date | None] = mapped_column(Date)
    target_library_root_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_roots.id", ondelete="SET NULL")
    )
    resolved_series_id: Mapped[int | None] = mapped_column(
        ForeignKey("series.id", ondelete="SET NULL")
    )
    last_actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    state: Mapped[SeriesInterestState] = mapped_column(
        SQLAlchemyEnum(
            SeriesInterestState,
            native_enum=False,
            create_constraint=True,
            name="series_interest_state",
            values_callable=lambda enum: [item.value for item in enum],
        ),
        default=SeriesInterestState.WATCHING,
    )
