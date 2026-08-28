"""Download client configuration ORM model."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - SQLAlchemy needs this at runtime
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pullbox.models.base import Base, IdentityMixin, TimestampMixin, UTCDateTime
from pullbox.models.download import DownloadClientType

if TYPE_CHECKING:
    from pullbox.models.airdcpp import AirDcppClientSettings


class DownloadClientConfig(Base, IdentityMixin, TimestampMixin):
    __tablename__ = "download_client_configs"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    client_type: Mapped[DownloadClientType] = mapped_column(SQLAlchemyEnum(DownloadClientType))
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True)
    priority: Mapped[int] = mapped_column(Integer, default=50)

    # Auth (varies by client type) — encrypted at rest via Fernet
    api_key: Mapped[str | None] = mapped_column(String(1024))
    username: Mapped[str | None] = mapped_column(String(255))
    password: Mapped[str | None] = mapped_column(String(1024))

    # Configuration
    category: Mapped[str | None] = mapped_column(String(100))
    download_dir: Mapped[str | None] = mapped_column(String(1000))
    remote_path: Mapped[str | None] = mapped_column(String(1000))

    # SABnzbd-specific options
    sab_priority: Mapped[str | None] = mapped_column(String(20))  # force/high/normal/low
    sab_post_processing: Mapped[str | None] = mapped_column(String(20))  # 0/1/2/3

    # qBittorrent-specific options
    qbt_content_layout: Mapped[str | None] = mapped_column(String(30))
    qbt_ratio_limit: Mapped[float | None] = mapped_column()
    qbt_seeding_time_limit: Mapped[int | None] = mapped_column()  # minutes

    # NZBGet-specific options
    # Valid priorities: force/very-high/high/normal/low/very-low
    nzbget_priority: Mapped[str | None] = mapped_column(String(20))
    nzbget_post_processing: Mapped[str | None] = mapped_column(String(20))  # pp0-pp3

    # Transmission-specific options
    transmission_download_dir: Mapped[str | None] = mapped_column(String(1000))
    # -1=low, 0=normal, 1=high
    transmission_bandwidth_priority: Mapped[int | None] = mapped_column()
    transmission_seed_ratio_limit: Mapped[float | None] = mapped_column()
    transmission_seed_idle_limit: Mapped[int | None] = mapped_column()  # minutes

    # Deluge-specific options
    deluge_label: Mapped[str | None] = mapped_column(String(100))  # Label plugin category
    deluge_max_ratio: Mapped[float | None] = mapped_column()
    deluge_move_completed_path: Mapped[str | None] = mapped_column(String(1000))

    # Health tracking
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_failure_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_test_message: Mapped[str | None] = mapped_column(Text)

    airdcpp_settings: Mapped[AirDcppClientSettings | None] = relationship(
        back_populates="client_config",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
