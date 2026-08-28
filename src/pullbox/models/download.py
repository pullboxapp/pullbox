"""Download history ORM model and related enums."""

from __future__ import annotations

import enum
from datetime import datetime  # noqa: TC003 - SQLAlchemy needs this at runtime
from typing import TYPE_CHECKING, assert_never

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy import (
    Enum as SQLAlchemyEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.models.base import Base, IdentityMixin, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from pullbox.models.client import DownloadClientConfig
    from pullbox.models.indexer import IndexerConfig
    from pullbox.models.issue import Issue


def _enum_values(enum_class: type[enum.Enum]) -> list[str]:
    """Persist public enum values rather than Python member names."""
    return [str(member.value) for member in enum_class]


class DownloadState(enum.StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    DOWNLOADING = "downloading"
    FINALIZING = "finalizing"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRY_PENDING = "retry_pending"
    # DEPRECATED: kept for DB compatibility only. Application code no longer
    # sets these states.  Use imported_at timestamp to determine import status.
    POST_PROCESSING = "post_processing"
    IMPORTED = "imported"


class DownloadClientType(enum.StrEnum):
    SABNZBD = "sabnzbd"
    NZBGET = "nzbget"
    QBITTORRENT = "qbittorrent"
    TRANSMISSION = "transmission"
    DELUGE = "deluge"
    DIRECT = "direct"
    AIRDCPP = "airdcpp"

    @property
    def acquisition_protocol(self) -> AcquisitionProtocol:
        """Return the acquisition protocol owned by this client type."""
        match self:
            case DownloadClientType.SABNZBD | DownloadClientType.NZBGET:
                return AcquisitionProtocol.USENET
            case (
                DownloadClientType.QBITTORRENT
                | DownloadClientType.TRANSMISSION
                | DownloadClientType.DELUGE
            ):
                return AcquisitionProtocol.TORRENT
            case DownloadClientType.DIRECT:
                return AcquisitionProtocol.DIRECT
            case DownloadClientType.AIRDCPP:
                return AcquisitionProtocol.DC

        assert_never(self)

    @property
    def is_usenet(self) -> bool:
        return self.acquisition_protocol is AcquisitionProtocol.USENET

    @property
    def is_torrent(self) -> bool:
        return self.acquisition_protocol is AcquisitionProtocol.TORRENT

    @property
    def is_direct(self) -> bool:
        return self.acquisition_protocol is AcquisitionProtocol.DIRECT


class DownloadHistory(Base, IdentityMixin, TimestampMixin):
    __tablename__ = "download_history"
    __table_args__ = (
        Index("ix_download_history_state", "state"),
        Index("ix_download_history_issue", "issue_id"),
        Index("ix_download_history_protocol_state", "protocol", "state"),
        Index(
            "ix_download_history_client_config_state",
            "download_client_config_id",
            "state",
        ),
        Index(
            "ix_download_history_post_processing_claim",
            "state",
            "post_processing_claimed_at",
        ),
    )

    issue_id: Mapped[int] = mapped_column(
        ForeignKey("issues.id", ondelete="CASCADE"), nullable=False
    )
    indexer_id: Mapped[int | None] = mapped_column(
        ForeignKey("indexer_configs.id", ondelete="SET NULL")
    )
    download_client_config_id: Mapped[int | None] = mapped_column(
        ForeignKey("download_client_configs.id", ondelete="SET NULL")
    )

    # Download details
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    download_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    download_client: Mapped[DownloadClientType] = mapped_column(SQLAlchemyEnum(DownloadClientType))
    protocol: Mapped[AcquisitionProtocol] = mapped_column(
        SQLAlchemyEnum(
            AcquisitionProtocol,
            name="acquisitionprotocol",
            native_enum=False,
            create_constraint=True,
            values_callable=_enum_values,
        ),
        nullable=False,
    )
    external_id: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[DownloadState] = mapped_column(
        SQLAlchemyEnum(DownloadState), default=DownloadState.QUEUED
    )

    # Result details
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    downloaded_path: Mapped[str | None] = mapped_column(String(1000))
    final_path: Mapped[str | None] = mapped_column(String(1000))
    error_message: Mapped[str | None] = mapped_column(Text)

    # Retry
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_retries: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    next_retry_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # Replacement intent for explicit user-driven "find alternative" downloads.
    replace_existing_file: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="0",
        nullable=False,
    )

    # Timing
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    imported_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    post_processing_claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    post_processing_claim_token: Mapped[str | None] = mapped_column(String(64))

    # Relationships
    issue: Mapped[Issue] = relationship()
    indexer: Mapped[IndexerConfig | None] = relationship()
    download_client_config: Mapped[DownloadClientConfig | None] = relationship()

    @validates("download_client")
    def _derive_protocol_from_client_type(
        self,
        _key: str,
        value: DownloadClientType | str,
    ) -> DownloadClientType:
        """Preserve construction compatibility while protocol becomes explicit."""
        try:
            client_type = DownloadClientType(str(value))
        except ValueError:
            client_type = DownloadClientType[str(value)]
        if self.protocol is None:
            self.protocol = client_type.acquisition_protocol
        return client_type
