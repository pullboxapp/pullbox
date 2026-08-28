"""Library file and root ORM models."""

from __future__ import annotations

import enum
from datetime import datetime  # noqa: TC003 - SQLAlchemy needs this at runtime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    BigInteger,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy import (
    Enum as SQLAlchemyEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pullbox.models.base import Base, IdentityMixin, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from pullbox.models.issue import Issue
    from pullbox.models.series import Series


class FileFormat(enum.StrEnum):
    CBZ = "cbz"
    CBR = "cbr"
    CB7 = "cb7"
    CBT = "cbt"
    PDF = "pdf"
    EPUB = "epub"


class MatchConfidence(enum.StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNMATCHED = "unmatched"
    MANUAL = "manual"


class LibraryFile(Base, IdentityMixin, TimestampMixin):
    __tablename__ = "library_files"
    __table_args__ = (
        Index("ix_library_files_path", "file_path", unique=True),
        Index("ix_library_files_match", "match_confidence"),
        Index("ix_library_files_issue", "issue_id"),
    )

    # File information
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False, unique=True)
    file_name: Mapped[str] = mapped_column(String(500), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_format: Mapped[FileFormat] = mapped_column(SQLAlchemyEnum(FileFormat))
    file_hash: Mapped[str | None] = mapped_column(String(64))
    file_modified_at: Mapped[datetime] = mapped_column(UTCDateTime)

    # Matching
    match_confidence: Mapped[MatchConfidence] = mapped_column(
        SQLAlchemyEnum(MatchConfidence), default=MatchConfidence.UNMATCHED
    )

    # Parsed metadata (from filename/ComicInfo.xml before matching)
    parsed_series: Mapped[str | None] = mapped_column(String(500))
    parsed_issue_number: Mapped[float | None] = mapped_column(Float)
    parsed_year: Mapped[int | None] = mapped_column(Integer)
    parsed_publisher: Mapped[str | None] = mapped_column(String(255))
    has_comicinfo: Mapped[bool] = mapped_column(default=False)

    # Naming audit snapshot captured when the file was placed.
    naming_snapshot: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSON, default=dict, server_default="{}", nullable=False
    )

    # Foreign keys
    issue_id: Mapped[int | None] = mapped_column(ForeignKey("issues.id", ondelete="SET NULL"))
    library_root_id: Mapped[int] = mapped_column(ForeignKey("library_roots.id", ondelete="CASCADE"))

    # Relationships
    issue: Mapped[Issue | None] = relationship(back_populates="library_file")
    library_root: Mapped[LibraryRoot] = relationship(back_populates="files")


class LibraryRoot(Base, IdentityMixin, TimestampMixin):
    __tablename__ = "library_roots"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str] = mapped_column(String(1000), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    last_scan_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_scan_duration_seconds: Mapped[float | None] = mapped_column(Float)
    last_scan_files_found: Mapped[int | None] = mapped_column(Integer)

    # Relationships
    files: Mapped[list[LibraryFile]] = relationship(
        back_populates="library_root", cascade="all, delete-orphan"
    )
    series: Mapped[list[Series]] = relationship(back_populates="library_root")
