"""Download request/response schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.models.direct_acquisition import DirectArtifactHostKind
from pullbox.models.download import DownloadClientType, DownloadState


class DownloadRequest(BaseModel):
    """Request body for triggering a download of a specific release."""

    download_url: str = Field(..., min_length=1, description="NZB/torrent URL to download")
    title: str = Field(..., min_length=1, description="Release title")
    indexer_id: int | None = Field(None, gt=0, description="Indexer that provided the release")


class DownloadQueueItem(BaseModel):
    """Active download in the queue."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    issue_id: int
    title: str
    state: DownloadState
    download_client: DownloadClientType
    protocol: AcquisitionProtocol
    external_id: str | None = None
    file_size: int | None = Field(None, description="File size in bytes")
    sent_at: datetime | None = None
    series_title: str | None = Field(None, description="Parent series title")
    issue_number: float | None = Field(None, description="Issue number")


class DownloadHistoryItem(BaseModel):
    """Completed download in history."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    issue_id: int
    title: str
    state: DownloadState
    download_client: DownloadClientType
    protocol: AcquisitionProtocol
    file_size: int | None = None
    error_message: str | None = None
    sent_at: datetime | None = None
    completed_at: datetime | None = None
    imported_at: datetime | None = None
    series_title: str | None = Field(None, description="Parent series title")
    issue_number: float | None = Field(None, description="Issue number")
    created_at: datetime


class DirectSourceSwitchRequest(BaseModel):
    """Select an equivalent artifact route for one active direct download."""

    artifact_identity: str | None = Field(
        None,
        min_length=7,
        max_length=100,
        pattern=r"^route:[a-z0-9:]+$",
    )
    block_current: bool = False


class DirectSourceCurrent(BaseModel):
    """Current artifact route being replaced."""

    artifact_identity: str
    host_kind: DirectArtifactHostKind
    host_label: str
    bytes_transferred: int = Field(ge=0)


class DirectSourceAlternative(BaseModel):
    """One safe untried source option from the durable acquisition plan."""

    artifact_identity: str
    host_kind: DirectArtifactHostKind
    host_label: str
    expected_size: int | None = Field(None, ge=0)
    is_next: bool


class DirectSourceOptionsResponse(BaseModel):
    """Current source and safe alternatives for a direct download."""

    download_id: int
    current: DirectSourceCurrent
    alternatives: list[DirectSourceAlternative]


class DirectSourceSwitchResponse(BaseModel):
    """Result of an atomic user-directed source switch."""

    status: str = "queued"
    previous_host: str
    selected_host: str
    current_route_blocklisted: bool
