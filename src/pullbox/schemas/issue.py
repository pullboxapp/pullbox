"""Issue request/response schemas."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pullbox.models.issue import IssueStatus


class IssueUpdate(BaseModel):
    """Request body for updating an issue."""

    status: IssueStatus | None = Field(None, description="Issue status")


class IssueResponse(BaseModel):
    """Issue data returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    comicvine_id: int | None = None
    series_id: int
    issue_number: float
    issue_number_text: str
    title: str | None = None
    description: str | None = None
    release_date: date | None = None
    cover_path: str | None = None
    comicvine_url: str | None = None
    status: IssueStatus
    manual_skip: bool = False
    page_count: int | None = None
    store_date: date | None = None
    metadata_source: str | None = None
    series_title: str | None = Field(None, description="Parent series title")
    has_file: bool = Field(False, description="Whether a library file is linked")
    created_at: datetime
    updated_at: datetime


class ManualFileImportRequest(BaseModel):
    """Request body for manually importing a file for an issue."""

    file_path: str = Field(..., min_length=1, description="Absolute path to the comic file")
    move_to_library: bool | None = Field(
        None,
        description=(
            "Deprecated compatibility field. Manual import always creates a library artifact."
        ),
    )
    allow_resource_safety_exception: bool = Field(
        False,
        description="Allow one explicit resource safety exception for this manual import attempt.",
    )


class ManualFileImportResponse(BaseModel):
    """Response after successfully importing a file for an issue."""

    issue_id: int
    library_file_id: int
    file_name: str
    file_path: str
    file_size: int
    file_format: str
    match_confidence: str


class IssueFileDeleteResponse(BaseModel):
    """Response after deleting or trashing the library file linked to an issue."""

    issue_id: int
    status: IssueStatus
    file_deleted: bool
    trashed: bool
    trash_path: str | None = None


class ManualFileImportProgressResponse(BaseModel):
    """Live progress snapshot for background manual issue import."""

    issue_id: int
    state: Literal["idle", "running", "completed", "failed", "safety_blocked", "cancelled"] = "idle"
    message: str = ""
    current_file_name: str | None = None
    current_file_stage: str | None = None
    current_file_progress_current: int | None = None
    current_file_progress_total: int | None = None
    current_file_progress_pct: int | None = None
    current_file_progress_unit: str | None = None
    file_index: int | None = None
    total_files: int | None = None
    library_file_id: int | None = None
    file_name: str | None = None
    file_path: str | None = None
    file_size: int | None = None
    file_format: str | None = None
    match_confidence: str | None = None
    error_message: str | None = None
    safety_exception: dict[str, object] | None = None


class IssueListResponse(BaseModel):
    """Compact issue data for list endpoints."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    series_id: int
    issue_number: float
    issue_number_text: str
    title: str | None = None
    release_date: date | None = None
    status: IssueStatus
    manual_skip: bool = False
    cover_path: str | None = None
    has_file: bool = Field(False, description="Whether a library file is linked")
