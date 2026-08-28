"""Download client configuration request/response schemas."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - Pydantic needs this at runtime
from pathlib import PurePosixPath, PureWindowsPath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pullbox.core.url_validation import normalize_peer_base_url
from pullbox.models.download import DownloadClientType
from pullbox.schemas.airdcpp import (
    AirDcppSettingsInput,
    AirDcppSettingsResponse,
    AirDcppSettingsUpdate,
)


def is_absolute_client_path(value: str | None) -> bool:
    if not value or "\x00" in value:
        return False
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


class ClientCreate(BaseModel):
    """Request body for adding a download client."""

    name: str = Field(..., min_length=1, max_length=255, description="Display name")
    client_type: DownloadClientType = Field(description="Client type")
    url: str = Field(..., min_length=1, max_length=500, description="Base URL")
    enabled: bool = Field(True, description="Whether this client is active")
    priority: int = Field(50, ge=1, le=100, description="Priority (lower = higher)")
    api_key: str | None = Field(None, max_length=255, description="API key (SABnzbd)")
    username: str | None = Field(None, max_length=255, description="Username (qBittorrent)")
    password: str | None = Field(None, max_length=255, description="Password (qBittorrent)")
    category: str | None = Field(None, max_length=100, description="Download category")
    download_dir: str | None = Field(
        None, max_length=1000, description="Local path where Pullbox can access completed downloads"
    )
    remote_path: str | None = Field(
        None,
        max_length=1000,
        description="Path prefix reported by the download client (for remote path mapping)",
    )
    # SABnzbd-specific
    sab_priority: str | None = Field(None, max_length=20)
    sab_post_processing: str | None = Field(None, max_length=20)
    # qBittorrent-specific
    qbt_content_layout: str | None = Field(None, max_length=30)
    qbt_ratio_limit: float | None = Field(None, ge=0)
    qbt_seeding_time_limit: int | None = Field(None, ge=0)
    # NZBGet-specific
    nzbget_priority: str | None = Field(None, max_length=20)
    nzbget_post_processing: str | None = Field(None, max_length=20)
    # Transmission-specific
    transmission_bandwidth_priority: int | None = Field(None, ge=-1, le=1)
    transmission_seed_ratio_limit: float | None = Field(None, ge=0)
    transmission_seed_idle_limit: int | None = Field(None, ge=0)
    # Deluge-specific
    deluge_label: str | None = Field(None, max_length=100)
    deluge_max_ratio: float | None = Field(None, ge=0)
    deluge_move_completed_path: str | None = Field(None, max_length=1000)
    # AirDC++-specific extension
    airdcpp: AirDcppSettingsInput | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        """Normalize and validate the configured download client URL."""
        return normalize_peer_base_url(value)

    @model_validator(mode="after")
    def validate_client_specific_fields(self) -> ClientCreate:
        """Require AirDC++ connection roots and reject misplaced extension data."""
        if self.client_type is not DownloadClientType.AIRDCPP:
            if self.airdcpp is not None:
                raise ValueError("airdcpp settings are only valid for an AirDC++ client")
            return self

        self.url = normalize_peer_base_url(self.url, reject_query_or_fragment=True)
        if not self.username or not self.username.strip():
            raise ValueError("username is required for an AirDC++ client")
        if not is_absolute_client_path(self.remote_path):
            raise ValueError("remote_path must be absolute for an AirDC++ client")
        if not is_absolute_client_path(self.download_dir):
            raise ValueError("download_dir must be absolute for an AirDC++ client")
        if self.category:
            raise ValueError("category is not used by an AirDC++ client")
        if self.airdcpp is None:
            self.airdcpp = AirDcppSettingsInput()
        return self


class ClientUpdate(BaseModel):
    """Request body for updating a download client."""

    name: str | None = Field(None, min_length=1, max_length=255)
    url: str | None = Field(None, min_length=1, max_length=500)
    enabled: bool | None = None
    priority: int | None = Field(None, ge=1, le=100)
    api_key: str | None = Field(None, max_length=255)
    username: str | None = Field(None, max_length=255)
    password: str | None = Field(None, max_length=255)
    category: str | None = Field(None, max_length=100)
    download_dir: str | None = Field(None, max_length=1000)
    remote_path: str | None = Field(None, max_length=1000)
    sab_priority: str | None = Field(None, max_length=20)
    sab_post_processing: str | None = Field(None, max_length=20)
    qbt_content_layout: str | None = Field(None, max_length=30)
    qbt_ratio_limit: float | None = Field(None, ge=0)
    qbt_seeding_time_limit: int | None = Field(None, ge=0)
    nzbget_priority: str | None = Field(None, max_length=20)
    nzbget_post_processing: str | None = Field(None, max_length=20)
    transmission_bandwidth_priority: int | None = Field(None, ge=-1, le=1)
    transmission_seed_ratio_limit: float | None = Field(None, ge=0)
    transmission_seed_idle_limit: int | None = Field(None, ge=0)
    deluge_label: str | None = Field(None, max_length=100)
    deluge_max_ratio: float | None = Field(None, ge=0)
    deluge_move_completed_path: str | None = Field(None, max_length=1000)
    airdcpp: AirDcppSettingsUpdate | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        """Normalize and validate the configured download client URL."""
        if value is None:
            return None
        return normalize_peer_base_url(value)


class ClientResponse(BaseModel):
    """Download client configuration returned by the API.

    Sensitive fields (password) are redacted.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    client_type: DownloadClientType
    url: str
    enabled: bool
    priority: int
    has_api_key: bool = Field(description="Whether an API key is configured")
    username: str | None = None
    has_password: bool = Field(description="Whether a password is configured")
    category: str | None = None
    download_dir: str | None = None
    remote_path: str | None = None
    sab_priority: str | None = None
    sab_post_processing: str | None = None
    qbt_content_layout: str | None = None
    qbt_ratio_limit: float | None = None
    qbt_seeding_time_limit: int | None = None
    nzbget_priority: str | None = None
    nzbget_post_processing: str | None = None
    transmission_bandwidth_priority: int | None = None
    transmission_seed_ratio_limit: float | None = None
    transmission_seed_idle_limit: int | None = None
    deluge_label: str | None = None
    deluge_max_ratio: float | None = None
    deluge_move_completed_path: str | None = None
    airdcpp: AirDcppSettingsResponse | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error: str | None = None
    last_test_message: str | None = None
    created_at: datetime
    updated_at: datetime
