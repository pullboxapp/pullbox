"""Series request/response schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pullbox.core.metadata_identity import ExternalIdentityRef, MetadataEntityKind, MetadataSource
from pullbox.models.series import (
    IssueCatalogState,
    SeriesStatus,
    SeriesStatusOverride,
    SeriesType,
)
from pullbox.schemas.metadata_sources import CatalogExcludedIssue
from pullbox.schemas.whats_new import WhatsNewSeriesSelection


class SeriesCreate(BaseModel):
    """Request body for adding a series from ComicVine."""

    comicvine_id: int = Field(..., gt=0, description="ComicVine volume ID")
    library_root_id: int | None = Field(
        None, gt=0, description="Library root folder for this series"
    )
    search_on_add: bool | None = Field(
        None,
        description=(
            "Deprecated compatibility field. Search on add is now controlled "
            "by the global import policy."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def reject_mixed_source_identity(cls, value: object) -> object:
        if isinstance(value, dict) and {"source", "external_id", "source_revision"}.intersection(
            value
        ):
            raise ValueError("Choose a source identity or a legacy ComicVine ID, not both.")
        return value


class SourceSeriesCreate(BaseModel):
    """Select a source identity; metadata is always fetched by the server."""

    model_config = ConfigDict(extra="forbid")

    source: MetadataSource
    external_id: str = Field(..., min_length=1, max_length=320)
    source_revision: int = Field(..., ge=0, strict=True)
    library_root_id: int | None = Field(None, gt=0)
    search_on_add: bool | None = None
    catalog_review_token: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    whats_new_selection: WhatsNewSeriesSelection | None = None

    @model_validator(mode="after")
    def canonical_identity(self) -> "SourceSeriesCreate":
        self.external_id = ExternalIdentityRef(
            self.source.identity_namespace, MetadataEntityKind.SERIES, self.external_id
        ).external_id
        return self


class SeriesUpdate(BaseModel):
    """Request body for updating a series."""

    monitored: bool | None = Field(None, description="Whether the series is monitored")
    status_override: SeriesStatusOverride | None = Field(
        None,
        description="Manual lifecycle status, or null to resume automatic status updates",
    )


class SeriesResponse(BaseModel):
    """Series data returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    comicvine_id: int | None = None
    title: str
    sort_title: str
    year_start: int | None = None
    year_end: int | None = None
    status: SeriesStatus
    status_override: SeriesStatusOverride | None = None
    series_type: SeriesType = SeriesType.STANDARD
    parent_series_id: int | None = None
    description: str | None = None
    cover_path: str | None = None
    issue_count: int = Field(0, description="Total known issues")
    issue_catalog_state: IssueCatalogState = IssueCatalogState.COMPLETE
    issue_catalog_last_synced_at: datetime | None = None
    issue_catalog_last_checked_at: datetime | None = None
    issue_catalog_error: str | None = None
    catalog_exclusions: list[CatalogExcludedIssue] = Field(default_factory=list)
    comicvine_url: str | None = None
    monitored: bool
    metadata_last_refreshed: datetime | None = None
    metadata_source: str | None = None
    publisher_id: int | None = None
    path: str | None = Field(None, description="Absolute path to series folder")
    library_root_id: int | None = Field(None, description="Library root this series belongs to")
    preferred_library_root_id: int | None = Field(
        None,
        description="Managed library root selected for future acquisitions",
    )
    alternate_names: list[str] = Field(default_factory=list, description="Alternate series names")
    publisher_name: str | None = Field(
        None, description="Publisher name, resolved from relationship"
    )
    owned_count: int = Field(0, description="Issues in library")
    wanted_count: int = Field(0, description="Issues actively wanted")
    created_at: datetime
    updated_at: datetime


SERIES_BULK_UPDATE_MAX_IDS = 10_000


class SeriesBulkUpdate(BaseModel):
    """Request body for bulk-updating series monitored status."""

    series_ids: list[int] = Field(
        ...,
        min_length=1,
        max_length=SERIES_BULK_UPDATE_MAX_IDS,
        description="IDs to update",
    )
    monitored: bool = Field(..., description="New monitored status")


class SeriesBulkDelete(BaseModel):
    """Request body for bulk-deleting series."""

    series_ids: list[int] = Field(..., min_length=1, max_length=100, description="IDs to delete")
    delete_files: bool = Field(False, description="Also delete library files from disk")
    delete_folder: bool = Field(False, description="Also delete series folders from disk")


class SeriesDeleteContextRequest(BaseModel):
    """Request body for series delete-modal preview context."""

    series_ids: list[int] = Field(..., min_length=1, max_length=100, description="IDs to inspect")


class SeriesDeleteContextResponse(BaseModel):
    """Delete-modal preview context for one or more series."""

    series_count: int = Field(..., description="Number of resolved series in the request")
    linked_file_count: int = Field(..., ge=0, description="Linked files that still exist on disk")
    managed_file_count: int = Field(..., ge=0, description="Pullbox-owned files in scope")
    referenced_file_count: int = Field(..., ge=0, description="Referenced files detached only")


class SeriesListResponse(BaseModel):
    """Compact series data for list endpoints."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    sort_title: str
    year_start: int | None = None
    status: SeriesStatus
    series_type: SeriesType = SeriesType.STANDARD
    parent_series_id: int | None = None
    monitored: bool
    issue_count: int = 0
    issue_catalog_state: IssueCatalogState = IssueCatalogState.COMPLETE
    issue_catalog_last_synced_at: datetime | None = None
    issue_catalog_last_checked_at: datetime | None = None
    issue_catalog_error: str | None = None
    publisher_name: str | None = None
    path: str | None = None
    library_root_id: int | None = None
    preferred_library_root_id: int | None = None
    owned_count: int = Field(0, description="Issues in library")
    wanted_count: int = Field(0, description="Issues actively wanted")
    cover_path: str | None = None
