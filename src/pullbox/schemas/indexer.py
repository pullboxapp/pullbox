"""Indexer configuration request/response schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pullbox.core.url_validation import normalize_peer_base_url
from pullbox.models.indexer import IndexerType


class IndexerCreate(BaseModel):
    """Request body for adding an indexer."""

    name: str = Field(..., min_length=1, max_length=255, description="Display name")
    indexer_type: IndexerType = Field(description="Indexer protocol type")
    url: str = Field(..., min_length=1, max_length=500, description="Base URL of the indexer")
    api_key: str = Field("", max_length=255, description="Optional API key for authentication")
    enabled: bool = Field(True, description="Whether this indexer is active")
    priority: int = Field(50, ge=1, le=100, description="Priority (lower = higher priority)")
    categories: str | None = Field(None, description="Comma-separated category IDs")
    enable_rss: bool = Field(True, description="Enable RSS sync")
    enable_automatic_search: bool = Field(True, description="Enable automatic search")
    enable_interactive_search: bool = Field(True, description="Enable interactive (manual) search")
    resolver_enabled: bool = Field(
        False,
        description="Allow a manual Torznab indexer to use the ranked browser resolver chain",
    )
    download_client_id: int | None = Field(
        None,
        description="Send every grab from this indexer to this download client",
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        """Normalize and validate the configured indexer URL."""
        return normalize_peer_base_url(value)


class IndexerUpdate(BaseModel):
    """Request body for updating an indexer."""

    name: str | None = Field(None, min_length=1, max_length=255, description="Display name")
    url: str | None = Field(None, min_length=1, max_length=500, description="Base URL")
    api_key: str | None = Field(None, min_length=1, max_length=255, description="API key")
    enabled: bool | None = Field(None, description="Whether this indexer is active")
    priority: int | None = Field(None, ge=1, le=100, description="Priority")
    categories: str | None = Field(None, description="Comma-separated category IDs")
    enable_rss: bool | None = Field(None, description="Enable RSS sync")
    enable_automatic_search: bool | None = Field(None, description="Enable automatic search")
    enable_interactive_search: bool | None = Field(
        None, description="Enable interactive (manual) search"
    )
    resolver_enabled: bool | None = Field(
        None,
        description="Allow a manual Torznab indexer to use the ranked browser resolver chain",
    )
    download_client_id: int | None = Field(
        None,
        description="Send every grab to this download client; null for the default client",
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        """Normalize and validate the configured indexer URL."""
        if value is None:
            return None
        return normalize_peer_base_url(value)


class IndexerResponse(BaseModel):
    """Indexer configuration data returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    indexer_type: IndexerType
    url: str
    has_api_key: bool = Field(description="Whether an API key is configured")
    enabled: bool
    priority: int
    categories: str | None = None
    source: str = "manual"
    prowlarr_indexer_id: int | None = None
    manager_indexer_id: str | None = None
    manager_available: bool = True
    enable_rss: bool = True
    enable_automatic_search: bool = True
    enable_interactive_search: bool = True
    resolver_enabled: bool = False
    download_client_id: int | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error: str | None = None
    failure_count: int
    disabled_until: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ProwlarrSyncRequest(BaseModel):
    """Request to sync indexers from a Prowlarr instance."""

    prowlarr_url: str = Field(..., min_length=1, description="Prowlarr base URL")
    prowlarr_api_key: str = Field(..., min_length=1, description="Prowlarr API key")

    @field_validator("prowlarr_url")
    @classmethod
    def validate_prowlarr_url(cls, value: str) -> str:
        """Normalize and validate the configured Prowlarr URL."""
        return normalize_peer_base_url(value)


class ProwlarrSyncResult(BaseModel):
    """Result of a Prowlarr indexer sync operation."""

    added: int = 0
    updated: int = 0
    removed: int = 0
    total: int = 0
    indexers: list[IndexerResponse] = []


class JackettSyncRequest(BaseModel):
    """Request to discover configured trackers from a Jackett instance."""

    jackett_url: str = Field(..., min_length=1, description="Jackett base URL")
    jackett_api_key: str = Field(..., min_length=1, description="Jackett API key")

    @field_validator("jackett_url")
    @classmethod
    def validate_jackett_url(cls, value: str) -> str:
        """Normalize and validate the configured Jackett URL."""
        return normalize_peer_base_url(value)


class JackettSyncResult(BaseModel):
    """Result of a Jackett tracker sync operation."""

    added: int = 0
    updated: int = 0
    retired: int = 0
    reactivated: int = 0
    total: int = 0
    indexers: list[IndexerResponse] = []
