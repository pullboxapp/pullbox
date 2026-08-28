"""Search result schemas for ComicVine, indexer, and library searches."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class SeriesSearchResult(BaseModel):
    """ComicVine series search result."""

    comicvine_id: int = Field(description="ComicVine volume ID")
    title: str
    year_start: int | None = None
    publisher_name: str | None = None
    issue_count: int | None = None
    description: str | None = None
    cover_url: str | None = Field(None, description="Remote cover image URL")
    comicvine_url: str | None = None
    already_added: bool = Field(False, description="Whether this series is already in the library")


class ReleaseSearchResult(BaseModel):
    """Indexer release search result."""

    title: str = Field(description="Release title from the indexer")
    download_url: str = Field(description="NZB/torrent download URL")
    indexer_id: int = Field(description="Indexer that provided this result")
    indexer_name: str = Field(description="Display name of the indexer")
    size_bytes: int | None = Field(None, description="File size in bytes")
    publish_date: date | None = Field(None, description="When the release was published")
    seeders: int | None = Field(None, description="Number of seeders (torrent only)")
    leechers: int | None = Field(None, description="Number of leechers (torrent only)")


class LibrarySearchResult(BaseModel):
    """Local library search result."""

    file_id: int = Field(description="Library file ID")
    file_name: str
    file_path: str
    series_title: str | None = None
    issue_number: float | None = None
    matched: bool = Field(description="Whether the file is matched to an issue")


class SearchHistoryBulkDeleteResponse(BaseModel):
    """Response from clearing search history entries."""

    deleted: int = Field(description="Number of search history rows deleted")


# ── Interactive Search Results ─────────────────────────────────────────


class MatchDetails(BaseModel):
    """How a release was matched to the wanted issue."""

    parsed_series: str | None = Field(description="Series name extracted from the release title")
    parsed_issue: float | None = Field(description="Issue number extracted from the release title")
    parsed_year: int | None = Field(description="Year extracted from the release title")
    series_similarity: float = Field(
        description="Similarity score (0.0-1.0) between parsed and wanted series",
    )
    match_type: str = Field(
        description="How the series name matched (exact, alternate, token_set, fuzzy)",
    )


class SearchResultItem(BaseModel):
    """A validated (matched) search result for interactive display."""

    title: str = Field(description="Release title from the indexer")
    indexer_name: str = Field(description="Display name of the indexer")
    indexer_id: int | None = Field(default=None, description="Originating indexer config ID")
    download_url: str | None = Field(description="NZB/torrent URL for legacy results only")
    info_url: str | None = Field(None, description="Link to release page on indexer website")
    size_bytes: int | None = Field(None, description="File size in bytes")
    age_days: int | None = Field(None, description="Release age in days")
    seeders: int | None = Field(None, description="Number of seeders (torrent only)")
    leechers: int | None = Field(None, description="Number of leechers (torrent only)")
    is_torrent: bool = Field(description="Whether this is a torrent result")
    category: str | None = Field(None, description="Newznab category ID or name")
    confidence: str = Field(description="Match confidence level (high, medium, low)")
    quality_score: float = Field(description="Quality score (0-100)")
    auto_grabbable: bool = Field(description="Whether this result would be auto-grabbed")
    match_details: MatchDetails = Field(description="Details of how the release matched")
    source_kind: Literal["indexer", "direct", "dc"] = Field(
        default="indexer",
        description="Typed indexer, direct-provider, or Direct Connect source",
    )
    method: str = Field(default="Indexer", description="Acquisition method")
    direct_attempt_id: int | None = Field(
        default=None,
        description="Server-issued direct candidate identity",
    )
    coverage: list[str] = Field(default_factory=list, description="Issues covered by the result")
    format: str | None = Field(default=None, description="Parsed artifact format")
    quality: str | None = Field(default=None, description="Parsed artifact quality")
    preferred_route: str | None = Field(
        default=None,
        description="How Pullbox will select an artifact route",
    )
    ranking_priority: int = Field(default=25, exclude=True, repr=False)


class RejectedResultItem(BaseModel):
    """A rejected search result with rejection reason."""

    title: str = Field(description="Release title from the indexer")
    indexer_name: str = Field(description="Display name of the indexer")
    indexer_id: int | None = Field(default=None, description="Originating indexer config ID")
    download_url: str | None = Field(description="NZB/torrent URL for legacy results only")
    info_url: str | None = Field(None, description="Link to release page on indexer website")
    size_bytes: int | None = Field(None, description="File size in bytes")
    age_days: int | None = Field(None, description="Release age in days")
    seeders: int | None = Field(None, description="Number of seeders (torrent only)")
    leechers: int | None = Field(None, description="Number of leechers (torrent only)")
    is_torrent: bool = Field(description="Whether this is a torrent result")
    category: str | None = Field(None, description="Newznab category ID or name")
    rejection_reason: str = Field(description="Why this result was rejected")
    confidence: str | None = Field(None, description="Match confidence level if partially matched")
    source_kind: Literal["indexer", "direct", "dc"] = Field(
        default="indexer",
        description="Typed indexer, direct-provider, or Direct Connect source",
    )
    method: str = Field(default="Indexer", description="Acquisition method")
    direct_attempt_id: int | None = Field(
        default=None,
        description="Server-issued direct candidate identity",
    )
    coverage: list[str] = Field(default_factory=list, description="Issues covered by the result")
    format: str | None = Field(default=None, description="Parsed artifact format")
    quality: str | None = Field(default=None, description="Parsed artifact quality")
    preferred_route: str | None = Field(
        default=None,
        description="How Pullbox will select an artifact route",
    )
    ranking_priority: int = Field(default=25, exclude=True, repr=False)


class InteractiveSearchIssue(BaseModel):
    """Issue context within an interactive search response."""

    id: int
    series_title: str
    issue_number: float
    issue_type: str
    year: int | None = None


class InteractiveSearchResponse(BaseModel):
    """Full response for the interactive search results endpoint."""

    issue: InteractiveSearchIssue = Field(description="Issue context for the search")
    matched: list[SearchResultItem] = Field(description="Validated matched results")
    rejected: list[RejectedResultItem] = Field(description="Rejected results with reasons")
    search_time_ms: int = Field(description="Total search and validation time in milliseconds")
    search_log_id: int | None = Field(
        default=None,
        description="Search history row created for this interactive search",
    )


# ── Grab Release ─────────────────────────────────────────────────────


class GrabReleaseRequest(BaseModel):
    """Request body for grabbing a specific release."""

    download_url: str = Field(description="NZB/torrent download URL")
    title: str = Field(description="Release title")
    indexer_name: str = Field(description="Display name of the indexer")
    indexer_id: int | None = Field(default=None, description="Originating indexer config ID")
    is_torrent: bool = Field(False, description="Whether this is a torrent release")
    file_size: int | None = Field(None, description="File size in bytes")
    search_log_id: int | None = Field(
        default=None,
        description="Originating search history row to update after a successful manual grab",
    )


class GrabReleaseResponse(BaseModel):
    """Response from a grab release request."""

    issue_id: int = Field(description="Issue ID")
    download_id: int = Field(description="Created download history record ID")
    title: str = Field(description="Release title that was grabbed")
    status: str = Field(description="Download status")


class DirectGrabRequest(BaseModel):
    """Plan and queue one server-issued direct search result."""

    direct_attempt_id: int = Field(gt=0, description="Server-issued direct candidate identity")
    pinned_route_identity: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="Optional server-issued mirror route identity",
    )


class DirectGrabResponse(BaseModel):
    """Durable queue acknowledgement for a direct acquisition."""

    issue_id: int
    acquisition_id: int
    artifact_id: int
    title: str
    status: str


class DcGrabRequest(BaseModel):
    """Queue one server-owned transient Direct Connect route."""

    dc_route_token: str = Field(
        min_length=20,
        max_length=200,
        description="Opaque server-side Direct Connect route grant",
    )


class DcGrabResponse(BaseModel):
    """Durable acknowledgement for one Direct Connect queue intent."""

    issue_id: int
    acquisition_id: int
    download_id: int
    bundle_id: int | None
    title: str
    status: str
