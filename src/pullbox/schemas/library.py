"""Library file request/response schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from pullbox.models.library import FileFormat, MatchConfidence


class LibraryFileResponse(BaseModel):
    """Library file data returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    file_path: str
    file_name: str
    file_size: int = Field(description="File size in bytes")
    file_format: FileFormat
    file_hash: str | None = None
    file_modified_at: datetime
    match_confidence: MatchConfidence
    parsed_series: str | None = None
    parsed_issue_number: float | None = None
    parsed_year: int | None = None
    parsed_publisher: str | None = None
    has_comicinfo: bool
    issue_id: int | None = Field(None, description="Matched issue ID")
    library_root_id: int
    created_at: datetime
    updated_at: datetime


class LibraryStats(BaseModel):
    """Library statistics overview."""

    total_files: int = Field(description="Total files across all roots")
    matched_files: int = Field(description="Files matched to an issue")
    unmatched_files: int = Field(description="Files not yet matched")
    total_size_bytes: int = Field(description="Total library size in bytes")
    roots_count: int = Field(description="Number of library roots")
    format_counts: dict[str, int] = Field(
        default_factory=dict, description="File count by format (cbz, cbr, etc.)"
    )


class ManualMatchRequest(BaseModel):
    """Request body for manually matching a file to an issue."""

    library_file_id: int = Field(..., gt=0, description="Library file to match")
    issue_id: int = Field(..., gt=0, description="Issue to match the file to")


class LibraryBrowserActionFlags(BaseModel):
    """Available single-item actions for a library browser entry."""

    can_properties: bool = True
    can_rename: bool = False
    can_auto_rename: bool = False
    can_convert: bool = False
    can_delete: bool = False


class LibraryBrowserStorageSummary(BaseModel):
    """Storage usage summary for the active library root."""

    total_bytes: int | None = None
    used_bytes: int | None = None
    free_bytes: int | None = None
    used_pct: float | None = None


class LibraryBrowserDeleteContext(BaseModel):
    """Delete behavior metadata for a library browser entry."""

    mode: str = Field(description="Delete mode: series, folder, or file")
    trash_enabled: bool = False
    series_id: int | None = None
    series_title: str | None = None
    linked_file_count: int = 0
    tracked_file_count: int = 0
    tracked_series_count: int = 0
    managed_file_count: int = 0
    referenced_file_count: int = 0
    has_linked_issue: bool = False
    issue_status_after_delete: str | None = None
    issue_status_reason: str | None = None


class LibraryBrowserRenameContext(BaseModel):
    """Rename safety metadata for a library browser entry."""

    stale_reference: bool = False
    reason_code: str | None = None
    message: str | None = None
    db_check_url: str | None = None


class LibraryBrowserEntryResponse(BaseModel):
    """Detailed entry metadata for the Library browser context-menu modals."""

    name: str
    path: str
    kind: str = Field(description="Entry kind: root, folder, or file")
    kind_label: str
    root_name: str
    root_path: str
    file_format: str | None = None
    size_bytes: int | None = None
    item_count: int | None = None
    modified_at: datetime | None = None
    permissions_label: str | None = None
    actions: LibraryBrowserActionFlags
    delete_context: LibraryBrowserDeleteContext
    rename_context: LibraryBrowserRenameContext
    storage: LibraryBrowserStorageSummary


class LibraryBrowserManualRenameRequest(BaseModel):
    """Validate a single-item manual rename request from the Library page."""

    path: str = Field(..., min_length=1, description="Absolute library path to rename")
    proposed_name: str = Field(..., min_length=1, description="Requested new file/folder name")


class LibraryBrowserManualRenameValidationResponse(BaseModel):
    """Validated single-item rename payload returned to the Library UI."""

    path: str
    current_name: str
    proposed_name: str
    target_path: str
    kind: str


class LibraryBrowserDeleteRequest(BaseModel):
    """Delete a single library entry from the Library browser."""

    path: str = Field(..., min_length=1, description="Absolute library path to trash")
    delete_files: bool = False
    delete_folder: bool = False


class LibraryBrowserConvertRequest(BaseModel):
    """Convert a single library file to CBZ from the Library browser."""

    path: str = Field(..., min_length=1, description="Absolute library file path to convert")
