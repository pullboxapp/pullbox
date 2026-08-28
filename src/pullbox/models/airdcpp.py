"""AirDC++-specific persistence models."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - SQLAlchemy needs this at runtime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pullbox.models.base import Base, IdentityMixin, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from pullbox.models.client import DownloadClientConfig
    from pullbox.models.download import DownloadHistory
    from pullbox.models.search_log import SearchLog


class AirDcppClientSettings(Base, IdentityMixin, TimestampMixin):
    """Bounded AirDC++ settings owned by one download-client configuration."""

    __tablename__ = "airdcpp_client_settings"
    __table_args__ = (
        UniqueConstraint("client_config_id", name="uq_airdcpp_settings_client_config"),
        CheckConstraint(
            "minimum_search_interval_seconds BETWEEN 45 AND 3600",
            name="ck_airdcpp_settings_minimum_search_interval",
        ),
        CheckConstraint(
            "manual_collection_seconds BETWEEN 1 AND 120",
            name="ck_airdcpp_settings_manual_collection",
        ),
        CheckConstraint(
            "automatic_collection_seconds BETWEEN 1 AND 120",
            name="ck_airdcpp_settings_automatic_collection",
        ),
        CheckConstraint(
            "max_results BETWEEN 1 AND 1000",
            name="ck_airdcpp_settings_max_results",
        ),
        CheckConstraint(
            "max_retained_routes BETWEEN max_results AND 2000",
            name="ck_airdcpp_settings_retained_routes",
        ),
        CheckConstraint(
            "max_concurrent_searches BETWEEN 1 AND 4",
            name="ck_airdcpp_settings_concurrent_searches",
        ),
        CheckConstraint(
            "request_timeout_seconds BETWEEN 1 AND 120",
            name="ck_airdcpp_settings_request_timeout",
        ),
        CheckConstraint(
            "search_dispatch_deadline_seconds BETWEEN 5 AND 300",
            name="ck_airdcpp_settings_dispatch_deadline",
        ),
        CheckConstraint(
            "reconciliation_interval_seconds BETWEEN 10 AND 300",
            name="ck_airdcpp_settings_reconciliation_interval",
        ),
        CheckConstraint(
            "queue_priority IS NULL OR queue_priority BETWEEN -1 AND 6",
            name="ck_airdcpp_settings_queue_priority",
        ),
    )

    client_config_id: Mapped[int] = mapped_column(
        ForeignKey("download_client_configs.id", ondelete="CASCADE"),
        nullable=False,
    )
    search_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="1",
        nullable=False,
    )
    automatic_search_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="0",
        nullable=False,
    )
    minimum_search_interval_seconds: Mapped[int] = mapped_column(
        Integer,
        default=45,
        server_default="45",
        nullable=False,
    )
    manual_collection_seconds: Mapped[int] = mapped_column(
        Integer,
        default=8,
        server_default="8",
        nullable=False,
    )
    automatic_collection_seconds: Mapped[int] = mapped_column(
        Integer,
        default=15,
        server_default="15",
        nullable=False,
    )
    max_results: Mapped[int] = mapped_column(
        Integer,
        default=200,
        server_default="200",
        nullable=False,
    )
    max_retained_routes: Mapped[int] = mapped_column(
        Integer,
        default=400,
        server_default="400",
        nullable=False,
    )
    max_concurrent_searches: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default="1",
        nullable=False,
    )
    request_timeout_seconds: Mapped[int] = mapped_column(
        Integer,
        default=15,
        server_default="15",
        nullable=False,
    )
    search_dispatch_deadline_seconds: Mapped[int] = mapped_column(
        Integer,
        default=45,
        server_default="45",
        nullable=False,
    )
    reconciliation_interval_seconds: Mapped[int] = mapped_column(
        Integer,
        default=30,
        server_default="30",
        nullable=False,
    )
    hub_allowlist: Mapped[list[str]] = mapped_column(
        JSON,
        default=list,
        server_default="[]",
        nullable=False,
    )
    queue_priority: Mapped[int | None] = mapped_column(Integer)
    next_search_allowed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    client_config: Mapped[DownloadClientConfig] = relationship(back_populates="airdcpp_settings")


class AirDcppAcquisition(Base, IdentityMixin, TimestampMixin):
    """Durable Direct Connect queue intent and reconciliation provenance."""

    __tablename__ = "airdcpp_acquisitions"
    __table_args__ = (
        UniqueConstraint("download_history_id", name="uq_airdcpp_acquisition_history"),
        UniqueConstraint("request_key", name="uq_airdcpp_acquisition_request_key"),
        UniqueConstraint(
            "client_identity",
            "bundle_id",
            name="uq_airdcpp_acquisition_client_bundle",
        ),
        CheckConstraint("size_bytes > 0", name="ck_airdcpp_acquisition_size_positive"),
        CheckConstraint("retry_count >= 0", name="ck_airdcpp_acquisition_retry_nonnegative"),
        CheckConstraint("max_retries >= 0", name="ck_airdcpp_acquisition_max_retry_nonnegative"),
        CheckConstraint(
            "retry_count <= max_retries",
            name="ck_airdcpp_acquisition_retry_within_max",
        ),
        Index("ix_airdcpp_acquisition_client_bundle", "client_config_id", "bundle_id"),
        Index(
            "ix_airdcpp_acquisition_client_reconciled",
            "client_config_id",
            "last_reconciled_at",
        ),
        Index("ix_airdcpp_acquisition_content", "tth", "size_bytes"),
        Index("ix_airdcpp_acquisition_next_retry", "next_retry_at"),
    )

    download_history_id: Mapped[int] = mapped_column(
        ForeignKey("download_history.id", ondelete="CASCADE"),
        nullable=False,
    )
    request_key: Mapped[str] = mapped_column(String(255), nullable=False)
    client_config_id: Mapped[int | None] = mapped_column(
        ForeignKey("download_client_configs.id", ondelete="SET NULL")
    )
    client_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    search_log_id: Mapped[int | None] = mapped_column(
        ForeignKey("search_logs.id", ondelete="SET NULL")
    )
    tth: Mapped[str] = mapped_column(String(39), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    original_name: Mapped[str] = mapped_column(String(500), nullable=False)
    search_instance_id: Mapped[int | None] = mapped_column(BigInteger)
    grouped_result_id: Mapped[str | None] = mapped_column(String(1000))
    result_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    bundle_id: Mapped[int | None] = mapped_column(BigInteger)
    client_state: Mapped[str | None] = mapped_column(String(100))
    remote_target: Mapped[str | None] = mapped_column(String(1000))
    route_snapshot: Mapped[dict[str, object]] = mapped_column(
        JSON,
        default=dict,
        server_default="{}",
        nullable=False,
    )
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_retries: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    next_retry_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_event_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_reconciled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reconciliation_error: Mapped[str | None] = mapped_column(Text)

    download_history: Mapped[DownloadHistory] = relationship()
    client_config: Mapped[DownloadClientConfig | None] = relationship()
    search_log: Mapped[SearchLog | None] = relationship()
