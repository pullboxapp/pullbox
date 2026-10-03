"""Internal response schemas for cached What's New release data."""

from __future__ import annotations

from datetime import date, datetime  # noqa: TC003 - Pydantic resolves these at runtime

from pydantic import BaseModel, ConfigDict, Field


class WhatsNewWatchRequest(BaseModel):
    """Watch uses cached context and a configured root, never client title/identity."""

    model_config = ConfigDict(extra="forbid")
    selection: WhatsNewSeriesSelection
    library_root_id: int | None = Field(default=None, strict=True, gt=0, le=2**31 - 1)


class WhatsNewSeriesSelection(BaseModel):
    """Bind a user selection to server-cached release evidence, not browser IDs."""

    model_config = ConfigDict(extra="forbid")
    cache_id: int = Field(gt=0, le=2**31 - 1, strict=True)
    release_id: int = Field(gt=0, le=2**63 - 1, strict=True)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class WhatsNewCommunityCounts(BaseModel):
    """Community activity counters from the upstream summary contract."""

    pull: int = 0
    have: int = 0
    read: int = 0
    want: int = 0
    pick: int = 0


class WhatsNewPublisherSummary(BaseModel):
    """Publisher fields included in upstream release summaries."""

    name: str
    locg_publisher_id: int | None = None
    excluded: bool = False
    excluded_reason: str | None = None


class WhatsNewSeriesSummary(BaseModel):
    """Series fields included in upstream release summaries."""

    title: str
    locg_series_id: int | None = None
    locg_url: str | None = None
    start_year: int | None = None
    volume: str | None = None


class WhatsNewIssueSummary(BaseModel):
    """Release summary card data from pullbox-data."""

    locg_issue_id: int
    locg_series_id: int | None = None
    locg_url: str
    title: str
    display_title: str
    issue_number: str | None = None
    price: float | None = None
    currency: str | None = None
    store_date: date
    release_week_date: date | None = None
    cover_url: str | None = None
    variant_count: int = 0
    community_rating: float | None = None
    community_counts: WhatsNewCommunityCounts = Field(default_factory=WhatsNewCommunityCounts)
    publisher: WhatsNewPublisherSummary
    series: WhatsNewSeriesSummary


class WhatsNewCacheMetadata(BaseModel):
    """Local cache/freshness metadata added by Pullbox."""

    status: str
    fetched_at: datetime
    last_successful_refresh_at: datetime
    stale: bool


class WhatsNewCurrentWeekResponse(BaseModel):
    """Current-week releases plus local cache metadata."""

    store_date: date
    count: int
    last_updated: datetime | None = None
    issues: list[WhatsNewIssueSummary] = Field(default_factory=list)
    cache: WhatsNewCacheMetadata


class WhatsNewUpcomingWeek(BaseModel):
    """One upcoming store week from pullbox-data."""

    store_date: date
    count: int
    issues: list[WhatsNewIssueSummary] = Field(default_factory=list)


class WhatsNewUpcomingResponse(BaseModel):
    """Upcoming release weeks plus local cache metadata."""

    weeks: list[WhatsNewUpcomingWeek] = Field(default_factory=list)
    lookahead_weeks: int
    cache: WhatsNewCacheMetadata
