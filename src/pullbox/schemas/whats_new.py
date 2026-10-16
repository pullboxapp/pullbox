"""Internal response schemas for cached What's New release data."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal, Self

import structlog
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

logger = structlog.get_logger(__name__)


def _optional_provider_id(value: object, info: ValidationInfo) -> int | None:
    if value is None:
        return None
    if type(value) is int and 0 < value < 2**63:
        return value
    logger.warning("whats_new_action_context_ignored", field=info.field_name, reason="invalid_id")
    return None


_ProviderID = Annotated[int | None, BeforeValidator(_optional_provider_id)]


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


class WhatsNewIssueSelection(WhatsNewSeriesSelection):
    """An expected local issue, revalidated against the entire cached release group."""

    issue_id: int = Field(gt=0, le=2**31 - 1, strict=True)


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
    gcd_series_id: _ProviderID = None
    metron_series_id: _ProviderID = None
    comicvine_series_id: _ProviderID = None
    publication_state: Literal["published", "prepublication", "unknown"] = "unknown"
    publication_as_of: date | None = None

    @field_validator("publication_state", mode="before")
    @classmethod
    def validate_publication_state(cls, value: object) -> object:
        if value is None:
            return "unknown"
        if isinstance(value, str) and value in {"published", "prepublication", "unknown"}:
            return value
        logger.warning(
            "whats_new_action_context_ignored", field="publication_state", reason="unknown_state"
        )
        return "unknown"

    @field_validator("publication_as_of", mode="before")
    @classmethod
    def validate_publication_date(cls, value: object) -> date | None:
        if value is None or type(value) is date:
            return value
        if isinstance(value, str):
            try:
                parsed = date.fromisoformat(value)
            except ValueError:
                logger.warning(
                    "whats_new_action_context_ignored",
                    field="publication_as_of",
                    reason="invalid_date",
                )
                return None
            if parsed.isoformat() == value:
                return parsed
        logger.warning(
            "whats_new_action_context_ignored", field="publication_as_of", reason="invalid_date"
        )
        return None

    @model_validator(mode="after")
    def require_dated_publication_evidence(self) -> Self:
        if self.publication_state != "unknown" and self.publication_as_of is None:
            logger.warning(
                "whats_new_action_context_ignored",
                field="publication_state",
                reason="missing_as_of",
            )
            self.publication_state = "unknown"
        return self


class WhatsNewIssueSummary(BaseModel):
    """Release summary card data from pullbox-data."""

    locg_issue_id: int
    locg_series_id: int | None = None
    gcd_issue_id: _ProviderID = None
    metron_issue_id: _ProviderID = None
    comicvine_issue_id: _ProviderID = None
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
