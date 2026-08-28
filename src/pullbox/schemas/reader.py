"""Internal web-reader API response contracts."""

from datetime import datetime

from pydantic import BaseModel, Field, StrictBool

from pullbox.models.library import FileFormat


class ReaderStateResponse(BaseModel):
    """Revision- and path-free private reading state."""

    page_index: int | None
    page_count: int | None
    progress_updated_at: datetime | None
    last_opened_at: datetime | None
    completed_at: datetime | None
    completion_updated_at: datetime | None
    want_to_read: bool
    want_to_read_updated_at: datetime | None
    state_version: int


class ReaderAdjacentIssueResponse(BaseModel):
    """Server-owned links for one adjacent readable issue."""

    issue_id: int
    issue_label: str
    title: str | None
    manifest_url: str
    issue_detail_url: str
    download_url: str


class ReaderManifestResponse(BaseModel):
    """Thin manifest consumed by the embedded web reader."""

    issue_id: int
    title: str
    issue_label: str
    format: FileFormat
    page_count: int
    revision: str
    initial_page_index: int = 0
    page_url_template: str
    progress_url: str
    completion_url: str
    want_to_read_url: str
    issue_detail_url: str
    download_url: str
    state: ReaderStateResponse
    previous_issue: ReaderAdjacentIssueResponse | None
    next_issue: ReaderAdjacentIssueResponse | None


class ReaderProgressUpdate(BaseModel):
    """Explicit settled-page state sent independently from page requests."""

    revision: str = Field(min_length=1, max_length=64)
    page_index: int = Field(ge=0)
    page_count: int = Field(ge=1)
    completion_candidate: bool = False
    reread_started: StrictBool = False


class ReaderProgressResponse(BaseModel):
    """Persisted private reader state returned to the embedded client."""

    page_index: int
    page_count: int
    revision: str
    completed_at: datetime | None
    updated_at: datetime
    state: ReaderStateResponse


class ReaderCompletionUpdate(BaseModel):
    """Explicit manual completion intent."""

    completed: StrictBool


class ReaderWantToReadUpdate(BaseModel):
    """Explicit private reading queue intent."""

    want_to_read: StrictBool


class ReaderStateMutationResponse(BaseModel):
    """Canonical result of an idempotent private state command."""

    changed: bool
    state: ReaderStateResponse


class ReaderFormatCapabilityResponse(BaseModel):
    """Runtime readiness for one supported reader format."""

    format: FileFormat
    available: bool
    detail: str


class ReaderCacheDiagnosticsResponse(BaseModel):
    """Path-free bounded reader cache diagnostics."""

    cache_file_count: int
    cache_bytes: int
    max_cache_bytes: int
    open_source_count: int
    max_open_sources: int
    max_workers: int


class ReaderCapabilitiesResponse(BaseModel):
    """Private reader capability and cache report."""

    enabled: bool
    formats: list[ReaderFormatCapabilityResponse]
    cache: ReaderCacheDiagnosticsResponse


class ReaderCacheClearResponse(BaseModel):
    """Generated cache cleanup result."""

    files_removed: int
    bytes_removed: int
