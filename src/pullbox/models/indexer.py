"""Indexer configuration ORM model."""

import enum
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column

from pullbox.models.base import Base, IdentityMixin, TimestampMixin, UTCDateTime


class IndexerType(enum.StrEnum):
    NEWZNAB = "newznab"
    TORZNAB = "torznab"
    PROWLARR = "prowlarr"


class IndexerSource(enum.StrEnum):
    """Where the indexer configuration originated."""

    MANUAL = "manual"
    PROWLARR = "prowlarr"
    JACKETT = "jackett"


class IndexerConfig(Base, IdentityMixin, TimestampMixin):
    __tablename__ = "indexer_configs"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    indexer_type: Mapped[IndexerType] = mapped_column(SQLAlchemyEnum(IndexerType))
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    api_key: Mapped[str] = mapped_column(String(1024), nullable=False)  # encrypted at rest
    enabled: Mapped[bool] = mapped_column(default=True)
    priority: Mapped[int] = mapped_column(Integer, default=50)
    categories: Mapped[str | None] = mapped_column(Text)

    # Download client every grab from this indexer is sent to. Null means the
    # highest-priority client for the release's protocol.
    download_client_id: Mapped[int | None] = mapped_column(
        ForeignKey("download_client_configs.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Manager sync tracking. The legacy Prowlarr integer remains during the
    # additive migration so existing databases and integrations stay readable.
    source: Mapped[str] = mapped_column(String(20), default="manual")
    prowlarr_indexer_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    manager_indexer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    manager_available: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="1",
        nullable=False,
    )

    # Search capability flags (Prowlarr sync profiles)
    enable_rss: Mapped[bool] = mapped_column(default=True)
    enable_automatic_search: Mapped[bool] = mapped_column(default=True)
    enable_interactive_search: Mapped[bool] = mapped_column(default=True)
    resolver_enabled: Mapped[bool] = mapped_column(
        default=False,
        server_default="0",
        nullable=False,
    )

    # Health tracking
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_failure_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    disabled_until: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # Capabilities (cached from indexer)
    capabilities_json: Mapped[str | None] = mapped_column(Text)
