"""Versioned source policy and provider-aware discovery responses."""

import enum
from datetime import date, datetime
from typing import Annotated, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from pullbox.core.metadata_identity import (
    ExternalIdentityRef,
    IdentityNamespace,
    MetadataEntityKind,
    MetadataSource,
)
from pullbox.schemas.metadata_credits import MetadataCredits


class MetadataDomain(enum.StrEnum):
    CORE = "core"
    ISSUES = "issues"
    ARTWORK = "artwork"
    STORY_ARCS = "story_arcs"


class SourceCapability(enum.StrEnum):
    SERIES_SEARCH = "series_search"
    SERIES_DETAILS = "series_details"
    ISSUE_LIST = "issue_list"
    RECENT_ISSUES = "recent_issues"
    ISSUE_DETAILS = "issue_details"
    CROSS_IDENTITIES = "cross_identities"
    CONDITIONAL_REFRESH = "conditional_refresh"
    OFFLINE = "offline"
    COVER_REFERENCE = "cover_reference"
    STORY_ARC_SEARCH = "story_arc_search"
    STORY_ARC_DETAILS = "story_arc_details"
    STORY_ARC_ISSUES = "story_arc_issues"


class SourceStatus(enum.StrEnum):
    OK = "ok"
    EMPTY = "empty"
    DISABLED = "disabled"
    FEATURE_DISABLED = "feature_disabled"
    NOT_IMPLEMENTED = "not_implemented"
    UNCONFIGURED = "unconfigured"
    INVALID_CONFIG = "invalid_configuration"
    AUTHENTICATION_FAILED = "authentication_failed"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    INCOMPATIBLE_RESPONSE = "incompatible_response"
    UNSUPPORTED = "unsupported"
    NOT_QUERIED = "not_queried"
    NOT_FOUND = "not_found"
    NOT_MODIFIED = "not_modified"


class SourceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    database_path: str | None = Field(default=None, min_length=1, max_length=4096)


class SourcePolicyWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0, lt=2**63, strict=True)
    enabled: bool
    priority: int = Field(ge=0, le=1000, strict=True)
    domain_priorities: dict[MetadataDomain, int] = Field(default_factory=dict, max_length=4)
    settings: SourceSettings = Field(default_factory=SourceSettings)
    credential: SecretStr | None = Field(default=None, exclude=True)
    clear_credential: bool = False


class SourcePolicyRead(BaseModel):
    source: MetadataSource
    identity_namespace: IdentityNamespace
    enabled: bool
    priority: int
    domain_priorities: dict[MetadataDomain, int]
    settings: SourceSettings
    credential_configured: bool
    revision: int
    last_tested_at: datetime | None = None
    last_success_at: datetime | None = None
    last_status: SourceStatus | None = None
    configuration_status: SourceStatus | None = None


class GcdSignInRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0, lt=2**63, strict=True)
    username: SecretStr = Field(min_length=1, max_length=1024, exclude=True)
    password: SecretStr = Field(min_length=1, max_length=4096, exclude=True)

    def clear_credentials(self) -> None:
        self.username = SecretStr("")
        self.password = SecretStr("")


class SourcePriorityWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order: list[MetadataSource] = Field(min_length=5, max_length=5)
    revisions: dict[MetadataSource, Annotated[int, Field(ge=0, lt=2**63, strict=True)]]
    domain_orders: dict[MetadataDomain, list[MetadataSource]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def complete_orders(self) -> Self:
        sources = set(MetadataSource)
        if set(self.order) != sources or set(self.revisions) != sources:
            raise ValueError("Include every metadata source exactly once with its saved revision")
        for domain, order in self.domain_orders.items():
            allowed = sources - (
                {MetadataSource.GCD_LOCAL, MetadataSource.GCD_API_V2}
                if domain is MetadataDomain.ARTWORK
                else set()
            )
            if len(order) != len(allowed) or set(order) != allowed:
                raise ValueError("Include every eligible source exactly once in each domain order")
        return self


class SeriesDiscoveryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=300)
    year: int | None = Field(default=None, ge=1, le=9999)
    sources: list[MetadataSource] | None = Field(default=None, min_length=1, max_length=5)
    mode: Literal["interactive", "automatic"] = "interactive"
    search_mode: Literal["preview", "full"] = "preview"
    limit_per_source: int = Field(default=20, ge=1, le=100)
    offsets: dict[MetadataSource, Annotated[int, Field(ge=0, le=10000, strict=True)]] = Field(
        default_factory=dict, max_length=5
    )

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        self.query = self.query.strip()
        if not self.query:
            raise ValueError("Enter a series title")
        if self.sources is not None and len(set(self.sources)) != len(self.sources):
            raise ValueError("Choose each source only once")
        if self.sources is not None and self.offsets.keys() - set(self.sources):
            raise ValueError("Pagination must refer to a selected source")
        if any(offset % self.limit_per_source for offset in self.offsets.values()):
            raise ValueError("Offsets must start at a source page boundary")
        return self


class ProviderSeriesRead(BaseModel):
    source: MetadataSource
    identity_namespace: IdentityNamespace
    external_id: str
    title: str
    year_start: int | None = None
    publisher: str | None = None
    issue_count: int | None = None
    description: str | None = None
    resource_url: str | None = None
    image_url: str | None = None
    also_from: list[MetadataSource] = Field(default_factory=list)
    cross_identities: list[ExternalIdentityRef] = Field(default_factory=list)
    source_updated_at: datetime | None = None
    sort_title: str | None = None
    year_end: int | None = None
    volume: str | None = None
    series_type: str | None = None
    status: str | None = None
    language: str | None = None


class ProviderIssueRead(BaseModel):
    credits: MetadataCredits | None = None
    source: MetadataSource
    identity_namespace: IdentityNamespace
    external_id: str
    series_external_id: str
    issue_number_text: str
    issue_number_key: str | None = None
    title: str | None = None
    description: str | None = None
    cover_date: date | None = None
    store_date: date | None = None
    page_count: int | None = None
    resource_url: str | None = None
    image_url: str | None = None
    cross_identities: list[ExternalIdentityRef] = Field(default_factory=list)
    source_updated_at: datetime | None = None


class ProviderStoryArcRead(BaseModel):
    source: MetadataSource
    identity_namespace: IdentityNamespace
    external_id: str
    title: str
    description: str | None = None
    resource_url: str | None = None
    image_url: str | None = None
    cross_identities: list[ExternalIdentityRef] = Field(default_factory=list)
    source_updated_at: datetime | None = None
    publisher: str | None = None
    declared_issue_count: int | None = None
    issue_external_ids: list[str] | None = None
    membership_complete: bool = False
    order_basis: Literal["response_order"] = "response_order"
    warnings: list[str] = Field(default_factory=list)
    also_from: list[MetadataSource] = Field(default_factory=list)


class StoryArcDiscoveryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=200)
    sources: list[MetadataSource] | None = Field(default=None, min_length=1, max_length=5)
    mode: Literal["interactive", "automatic"] = "interactive"
    pages: dict[MetadataSource, Annotated[int, Field(ge=1, le=100, strict=True)]] = Field(
        default_factory=dict, max_length=5
    )

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        self.query = self.query.strip()
        if not self.query:
            raise ValueError("Enter a story arc title")
        if self.sources is not None and len(set(self.sources)) != len(self.sources):
            raise ValueError("Choose each source only once")
        if self.sources is not None and self.pages.keys() - set(self.sources):
            raise ValueError("Pagination must refer to a selected source")
        return self


class MetadataPage[T](BaseModel):
    results: list[T]
    total: int
    next_page: int | None = None
    truncated: bool = False
    # Provider publication order is not a reviewed story-arc reading order.
    order_is_reading_order: bool = False


class MetadataFetch[T](BaseModel):
    status: SourceStatus
    data: T | None = None
    validator: str | None = None
    retry_after_seconds: int | None = None


class RecentIssueWindow(BaseModel):
    """One bounded slice, never evidence of complete series membership."""

    model_config = ConfigDict(extra="forbid")
    results: list[ProviderIssueRead] = Field(max_length=100)
    matched_total: int = Field(
        ge=0,
        le=1_000_000_000,
        strict=True,
        description="Count matching this query, not proof of complete series membership.",
    )
    scope: Literal["recent_publication", "modified_since"]
    since: datetime | None = None
    truncated: bool = Field(strict=True)
    full_catalog: Literal[False] = False
    source_updated_at: datetime | None = None


class SeriesPreviewQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_kind: ClassVar[MetadataEntityKind] = MetadataEntityKind.SERIES
    source: MetadataSource
    external_id: str = Field(min_length=1, max_length=255, strict=True)

    @model_validator(mode="after")
    def canonical_identity(self) -> Self:
        self.external_id = ExternalIdentityRef(
            self.source.identity_namespace, self.entity_kind, self.external_id
        ).external_id
        if (
            self.source.identity_namespace is IdentityNamespace.COMICVINE
            and int(self.external_id) >= 2**63
        ):
            raise ValueError("ComicVine identity exceeds the supported range")
        return self


class SeriesAddPreviewQuery(SeriesPreviewQuery):
    library_root_id: int | None = Field(default=None, gt=0, lt=2**63, strict=True)


class SeriesIssuePageQuery(SeriesPreviewQuery):
    page: int = Field(default=1, ge=1, le=10000, strict=True)
    source_revision: int = Field(ge=0, lt=2**63, strict=True)


class SeriesIssuePageRead(MetadataFetch[MetadataPage[ProviderIssueRead]]):
    series_cover_url: str | None = None


class CatalogExcludedIssue(BaseModel):
    """An exact provider entry explicitly left out, never an issue identity claim."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal[MetadataSource.GCD_LOCAL]
    series_external_id: str = Field(min_length=1, max_length=255)
    external_id: str = Field(min_length=1, max_length=255)
    issue_number_text: str = Field(max_length=100)


class CatalogReviewRead(BaseModel):
    token: str
    total: int
    supported_count: int
    excluded: tuple[CatalogExcludedIssue, ...]


class SeriesPreviewRead(BaseModel):
    source: MetadataSource
    external_id: str
    source_revision: int
    series: MetadataFetch[ProviderSeriesRead]
    issues: MetadataFetch[MetadataPage[ProviderIssueRead]]
    folder_preview: str | None = None
    catalog_review: CatalogReviewRead | None = None


class StoryArcPreviewQuery(SeriesPreviewQuery):
    entity_kind: ClassVar[MetadataEntityKind] = MetadataEntityKind.STORY_ARC


class StoryArcIssuePageQuery(StoryArcPreviewQuery):
    page: int = Field(default=1, ge=1, le=50, strict=True)
    source_revision: int = Field(ge=0, lt=2**63, strict=True)


class StoryArcPreviewRead(BaseModel):
    source: MetadataSource
    external_id: str
    source_revision: int
    arc: MetadataFetch[ProviderStoryArcRead]
    issues: MetadataFetch[MetadataPage[ProviderIssueRead]]


class SourceOutcome(BaseModel):
    source: MetadataSource
    status: SourceStatus
    total: int | None = None
    next_offset: int | None = None
    retry_after_seconds: int | None = None
    rejected_results: int = 0
    truncated: bool = False


class SeriesDiscoveryRead(BaseModel):
    results: list[ProviderSeriesRead]
    sources: list[SourceOutcome]


class StoryArcSourceOutcome(SourceOutcome):
    next_page: int | None = None


class StoryArcDiscoveryRead(BaseModel):
    results: list[ProviderStoryArcRead]
    sources: list[StoryArcSourceOutcome]


class SourceAccountRead(BaseModel):
    status: SourceStatus | None
    retry_at: datetime | None
    probe_until: datetime | None


class DeferredMetadataRead(BaseModel):
    id: int
    series_id: int
    series_title: str
    source: str
    task_id: Literal["refresh_metadata", "sync_new_issues"]
    state: Literal["ready", "waiting", "authentication_required", "source_disabled"]
    retry_at: datetime | None


class SourceDescriptor(SourcePolicyRead):
    capabilities: list[SourceCapability]
    availability: SourceStatus | None = None
    account: SourceAccountRead | None = None
    deferred_series: int = 0
    deferred_work: int = 0


class SourceTestRead(BaseModel):
    outcome: SourceOutcome
    recorded: bool
