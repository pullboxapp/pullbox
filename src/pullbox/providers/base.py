"""Provider protocol interfaces and shared data transfer objects.

Defines the contracts that all external service integrations must satisfy,
using Python Protocols (PEP 544) for structural subtyping.

Protocols:
    MetadataProvider — comic metadata sources (ComicVine, Metron, etc.)
    DownloadClient   — download managers (SABnzbd, qBittorrent, etc.)
    Indexer          — content indexers (Newznab, Torznab, Prowlarr)

DTOs are frozen dataclasses shared across all provider implementations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pullbox.core.acquisition import AcquisitionProtocol

if TYPE_CHECKING:
    from datetime import datetime


# ---------------------------------------------------------------------------
# Data Transfer Objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderHealthResult:
    """Result of a provider health/connectivity check."""

    healthy: bool
    message: str
    response_time_ms: float
    details: dict[str, str] | None = None


@dataclass(frozen=True)
class SeriesSearchResult:
    """A single series result from a metadata search."""

    provider_id: str
    title: str
    year_start: int | None
    publisher: str | None
    issue_count: int | None
    status: str | None
    cover_url: str | None
    description: str | None
    comicvine_url: str | None = None


@dataclass(frozen=True)
class SeriesMetadata:
    """Full series metadata retrieved from a provider."""

    provider_id: str
    title: str
    sort_title: str | None
    year_start: int | None
    year_end: int | None
    status: str | None
    publisher: str | None
    description: str | None
    cover_url: str | None
    issue_count: int | None
    comicvine_url: str | None


@dataclass(frozen=True)
class IssueSummary:
    """Compact issue info returned when listing issues for a series."""

    provider_id: str
    issue_number: float
    title: str | None
    release_date: str | None
    cover_url: str | None
    issue_type: str


@dataclass(frozen=True)
class IssueMetadata:
    """Full issue metadata retrieved from a provider."""

    provider_id: str
    series_provider_id: str
    issue_number: float
    title: str | None
    description: str | None
    release_date: str | None
    store_date: str | None
    cover_url: str | None
    page_count: int | None
    comicvine_url: str | None
    creators: list[dict[str, str]] = field(default_factory=list)
    story_arcs: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class DownloadStatus:
    """Status of a single download in a client's queue."""

    external_id: str
    title: str
    state: str  # "downloading", "completed", "failed", "paused", "queued"
    progress: float  # 0.0 to 1.0
    size_bytes: int | None
    speed_bytes: int | None
    eta_seconds: int | None
    error_message: str | None
    downloaded_path: str | None = None
    client_state: str | None = None  # human-readable sub-state from client


@dataclass(frozen=True)
class ClientOptions:
    """Available options fetched from a download client."""

    categories: list[str]
    default_category: str | None = None
    download_directories: list[str] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchQuery:
    """Parameters for an indexer release search."""

    series_title: str
    issue_number: float | None = None
    year: int | None = None
    issue_type: str = "issue"  # IssueType.value string
    categories: list[str] | None = None


@dataclass(frozen=True, init=False)
class ReleaseResult:
    """A single release result from an indexer search."""

    title: str
    indexer_name: str
    download_url: str
    size_bytes: int | None
    age_days: int | None
    seeders: int | None  # Torrent only
    leechers: int | None  # Torrent only
    grabs: int | None  # Usenet only
    protocol: AcquisitionProtocol
    category: str | None
    published_at: datetime | None
    info_url: str | None = None
    indexer_id: int | None = None
    ranking_priority: int = 25

    def __init__(
        self,
        title: str,
        indexer_name: str,
        download_url: str,
        size_bytes: int | None,
        age_days: int | None,
        seeders: int | None,
        leechers: int | None,
        grabs: int | None,
        is_torrent: bool | None = None,
        category: str | None = None,
        published_at: datetime | None = None,
        info_url: str | None = None,
        indexer_id: int | None = None,
        ranking_priority: int = 25,
        *,
        protocol: AcquisitionProtocol | None = None,
    ) -> None:
        """Create a result using an explicit protocol or the legacy torrent flag."""
        if protocol is None:
            if is_torrent is None:
                raise TypeError("ReleaseResult requires protocol or is_torrent")
            protocol = AcquisitionProtocol.TORRENT if is_torrent else AcquisitionProtocol.USENET

        object.__setattr__(self, "title", title)
        object.__setattr__(self, "indexer_name", indexer_name)
        object.__setattr__(self, "download_url", download_url)
        object.__setattr__(self, "size_bytes", size_bytes)
        object.__setattr__(self, "age_days", age_days)
        object.__setattr__(self, "seeders", seeders)
        object.__setattr__(self, "leechers", leechers)
        object.__setattr__(self, "grabs", grabs)
        object.__setattr__(self, "protocol", protocol)
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "published_at", published_at)
        object.__setattr__(self, "info_url", info_url)
        object.__setattr__(self, "indexer_id", indexer_id)
        object.__setattr__(self, "ranking_priority", ranking_priority)

    @property
    def is_torrent(self) -> bool:
        """Return whether this result uses the BitTorrent protocol."""
        return self.protocol is AcquisitionProtocol.TORRENT


@dataclass(frozen=True)
class IndexerCapabilities:
    """Capabilities reported by an indexer."""

    categories: list[str] = field(default_factory=list)
    search_params: list[str] = field(default_factory=list)
    max_requests_per_day: int | None = None


# ---------------------------------------------------------------------------
# Provider Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class MetadataProvider(Protocol):
    """Interface for comic metadata sources (ComicVine, Metron, etc.)."""

    @property
    def name(self) -> str:
        """Human-readable provider name."""
        ...

    async def search_series(
        self,
        query: str,
        year: int | None = None,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> list[SeriesSearchResult]:
        """Search for series by title and optional year."""
        ...

    async def get_series(self, provider_id: str) -> SeriesMetadata:
        """Get full series metadata by provider-specific ID."""
        ...

    async def get_issue(self, provider_id: str) -> IssueMetadata:
        """Get full issue metadata by provider-specific ID."""
        ...

    async def get_issues_for_series(self, series_provider_id: str) -> list[IssueSummary]:
        """Get all issues for a series."""
        ...

    async def get_cover_image(self, image_url: str) -> bytes:
        """Download a cover image. Returns raw bytes."""
        ...

    async def test_connection(self) -> ProviderHealthResult:
        """Verify connectivity and authentication."""
        ...


@runtime_checkable
class DownloadClient(Protocol):
    """Interface for download clients (SABnzbd, qBittorrent, etc.)."""

    @property
    def name(self) -> str:
        """Human-readable client name."""
        ...

    @property
    def client_type(self) -> str:
        """Client type identifier."""
        ...

    async def add_nzb(self, url: str, title: str, category: str | None = None) -> str:
        """Add an NZB download. Returns external download ID."""
        ...

    async def add_torrent(self, url: str, title: str, category: str | None = None) -> str | None:
        """Add a torrent/magnet download. Returns external download ID or None."""
        ...

    async def add_torrent_data(
        self,
        content: bytes,
        title: str,
        category: str | None = None,
    ) -> str | None:
        """Add trusted torrent descriptor bytes fetched by Pullbox."""
        ...

    async def get_download_status(self, external_id: str) -> DownloadStatus:
        """Get status of a specific download."""
        ...

    async def get_queue(self) -> list[DownloadStatus]:
        """Get all active downloads."""
        ...

    async def remove_download(self, external_id: str, delete_files: bool = False) -> bool:
        """Remove a download from the client."""
        ...

    async def test_connection(self) -> ProviderHealthResult:
        """Verify connectivity and authentication."""
        ...


@runtime_checkable
class Indexer(Protocol):
    """Interface for content indexers (Newznab, Torznab, Prowlarr)."""

    @property
    def name(self) -> str:
        """Human-readable indexer name."""
        ...

    @property
    def indexer_type(self) -> str:
        """Indexer protocol type."""
        ...

    @property
    def supports_nzb(self) -> bool:
        """Whether this indexer provides NZB downloads."""
        ...

    @property
    def supports_torrent(self) -> bool:
        """Whether this indexer provides torrent downloads."""
        ...

    async def search(self, query: SearchQuery) -> list[ReleaseResult]:
        """Search for releases matching the query."""
        ...

    async def get_capabilities(self) -> IndexerCapabilities:
        """Get indexer capabilities (categories, search params, limits)."""
        ...

    async def test_connection(self) -> ProviderHealthResult:
        """Verify connectivity and authentication."""
        ...


# ---------------------------------------------------------------------------
# Provider Registry
# ---------------------------------------------------------------------------


_USENET_TYPES = {"sabnzbd", "nzbget"}
_TORRENT_TYPES = {"qbittorrent", "transmission", "deluge"}


class ProviderRegistry:
    """Simple registry for provider instances with priority-based selection."""

    def __init__(self) -> None:
        self._metadata_providers: dict[str, MetadataProvider] = {}
        self._download_clients: dict[int, DownloadClient] = {}
        self._download_priorities: dict[int, int] = {}
        self._indexers: dict[int, Indexer] = {}

    def register_metadata_provider(self, name: str, provider: MetadataProvider) -> None:
        self._metadata_providers[name] = provider

    def register_download_client(
        self,
        config_id: int,
        client: DownloadClient,
        priority: int = 50,
    ) -> None:
        self._download_clients[config_id] = client
        self._download_priorities[config_id] = priority

    def register_indexer(self, config_id: int, indexer: Indexer) -> None:
        self._indexers[config_id] = indexer

    def get_metadata_provider(self, name: str = "comicvine") -> MetadataProvider:
        """Get a metadata provider by name."""
        return self._metadata_providers[name]

    def get_download_clients(self) -> list[DownloadClient]:
        """Get all registered download clients."""
        return list(self._download_clients.values())

    def get_download_client(self, config_id: int) -> DownloadClient | None:
        """Get one registered download client by its persisted config ID."""
        return self._download_clients.get(config_id)

    def get_indexers(self) -> list[Indexer]:
        """Get all registered indexers."""
        return list(self._indexers.values())

    def get_indexer(self, config_id: int) -> Indexer | None:
        """Get one registered indexer by its persisted config ID."""
        return self._indexers.get(config_id)

    def get_indexer_items(self) -> list[tuple[int, Indexer]]:
        """Get (config_id, indexer) pairs for all registered indexers."""
        return list(self._indexers.items())

    def get_nzb_client(self) -> DownloadClient | None:
        """Get the highest-priority enabled NZB client."""
        return self._get_client_by_protocol(_USENET_TYPES)

    def get_torrent_client(self) -> DownloadClient | None:
        """Get the highest-priority enabled torrent client."""
        return self._get_client_by_protocol(_TORRENT_TYPES)

    def _get_client_by_protocol(self, client_types: set[str]) -> DownloadClient | None:
        """Get the highest-priority client matching any of the given types.

        Lower priority number = higher priority. Registration order is the
        stable tiebreaker for equal priorities.
        """
        candidates = [
            (config_id, client)
            for config_id, client in self._download_clients.items()
            if client.client_type in client_types
        ]
        if not candidates:
            return None
        # Sort by priority (lower wins), stable sort preserves insertion order
        candidates.sort(key=lambda c: self._download_priorities.get(c[0], 50))
        return candidates[0][1]

    def get_client_for_type(self, client_type: str) -> DownloadClient | None:
        """Get a client by its specific type string."""
        for client in self._download_clients.values():
            if client.client_type == client_type:
                return client
        return None

    def get_download_client_items(self) -> list[tuple[int, DownloadClient]]:
        """Get (config_id, client) pairs for all registered download clients."""
        return list(self._download_clients.items())

    def has_metadata_provider(self, name: str = "comicvine") -> bool:
        """Check if a metadata provider is registered without raising KeyError."""
        return name in self._metadata_providers
