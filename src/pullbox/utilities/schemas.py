"""Pydantic request/response schemas for utility API endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ── Request Schemas ────────────────────────────────────────────


class JobCreateRequest(BaseModel):
    """Request body for creating a new utility job."""

    job_type: str = Field(..., description="Job type identifier (e.g., file_convert, mass_rename)")
    display_name: str = Field(
        ..., min_length=1, max_length=200, description="Human-readable job name"
    )
    config: dict[str, Any] = Field(default_factory=dict, description="Job-specific configuration")


# ── Response Schemas ───────────────────────────────────────────


class JobResponse(BaseModel):
    """Utility job summary for list views."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    job_type: str
    display_name: str
    state: str
    total_items: int | None = None
    completed_items: int | None = None
    failed_items: int | None = None
    skipped_items: int | None = None
    warning_count: int | None = None
    queue_position: int | None = None
    created_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    created_by: str | None = None
    error_message: str | None = None
    parent_job_id: str | None = None
    progress_pct: float = Field(default=0.0, description="Percentage complete")


class JobDetailResponse(JobResponse):
    """Full job detail including config."""

    config: str = Field(default="{}", description="Job configuration JSON")


class JobItemResponse(BaseModel):
    """Single job item response."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    job_id: str
    item_index: int
    state: str
    file_path: str | None = None
    operation: str
    duration_ms: int | None = None
    error_message: str | None = None
    warning_message: str | None = None
    worker_id: int | None = None


class JobLogResponse(BaseModel):
    """Single job log entry response."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: str
    item_id: str | None = None
    timestamp: str | None = None
    level: str
    message: str
    extra: dict[str, Any] | str | None = None
    file_path: str | None = None


class JobLogListResponse(BaseModel):
    """Paginated job log response with total count."""

    entries: list[JobLogResponse]
    total_count: int


class JobListResponse(BaseModel):
    """Paginated list of jobs."""

    jobs: list[JobResponse]
    total: int


class QueueStatusResponse(BaseModel):
    """Current queue status summary."""

    queued: int = Field(description="Number of jobs waiting")
    running: int = Field(description="Number of jobs currently executing")
    paused: int = Field(description="Number of paused jobs")
    total_completed: int = Field(description="Total completed jobs")


# ── Converter Preview ──────────────────────────────────────────


class ConvertPreviewRequest(BaseModel):
    """Request body for conversion preview."""

    source_format: str = Field(..., description="Source format (cbr, cb7, pdf, cbz)")
    target_format: str = Field(..., description="Target format (cbz)")
    scope: str = Field("manual", description="Scope (manual)")
    file_paths: list[str] = Field(default_factory=list, description="File paths for conversion")


class ConvertPreviewFileInfo(BaseModel):
    """Single file in preview response."""

    path: str
    output_path: str
    size_bytes: int


class ConvertPreviewResponse(BaseModel):
    """Conversion preview result."""

    source_format: str
    target_format: str
    total_count: int = Field(description="Total matching files")
    total_size_bytes: int = Field(description="Total size of matching files")
    lossless: bool = Field(description="Whether conversion is lossless")
    files: list[ConvertPreviewFileInfo] = Field(
        default_factory=list, description="Sample files (max 100)"
    )


# ── Mass Convert Preview ──────────────────────────────────────


class MassConvertPreviewRequest(BaseModel):
    """Request body for mass-convert preview."""

    scope: str = Field(
        "library",
        description="Convert scope: library, folder, or manual",
    )
    file_paths: list[str] = Field(
        default_factory=list,
        description="Selected file or folder paths to preview",
    )
    trash_folder: str | None = Field(
        default=None,
        description="Optional effective trash folder to exclude from the preview",
    )


class MassConvertPreviewItem(BaseModel):
    """Single preview row for a mass-convert candidate."""

    file_path: str
    source_name: str
    source_format: str
    output_name: str
    size_bytes: int


class MassConvertPreviewResponse(BaseModel):
    """Preview payload for the mass-convert workflow."""

    scope: str
    item_count: int
    total_size_bytes: int
    items: list[MassConvertPreviewItem] = Field(default_factory=list)


# ── Library Permissions Preview ───────────────────────────────


class LibraryPermissionsPreviewRequest(BaseModel):
    """Request body for recursive library permissions preview."""

    scope: str = Field(
        "library",
        description="Permission scope: library, folder, or files",
    )
    file_paths: list[str] = Field(
        default_factory=list,
        description="Selected folder or file paths to preview",
    )
    folder_mode: str = Field("755", description="Target chmod mode for directories")
    file_mode: str = Field("644", description="Target chmod mode for files")
    include_folders: bool = Field(True, description="Whether directories are in scope")
    include_files: bool = Field(True, description="Whether files are in scope")


class LibraryPermissionsPreviewItem(BaseModel):
    """Single path that would be evaluated by the permissions utility."""

    file_path: str
    name: str
    item_type: str = Field(description="Preview item type: folder, file, or symlink")
    target_mode: str


class LibraryPermissionsPreviewResponse(BaseModel):
    """Preview payload for the recursive permissions workflow."""

    scope: str
    item_count: int
    folder_count: int
    file_count: int
    items: list[LibraryPermissionsPreviewItem] = Field(default_factory=list)


# ── Mass Rename Preview ───────────────────────────────────────


class MassRenamePreviewRequest(BaseModel):
    """Request body for mass rename preview."""

    target: str = Field(..., description="Rename target type: files or folders")
    scope: str = Field(
        "manual",
        description="Rename scope: library, folder, or manual",
    )
    file_paths: list[str] = Field(
        default_factory=list,
        description="Selected file or folder paths to preview",
    )


class MassRenamePreviewItem(BaseModel):
    """Single preview row for a rename candidate."""

    file_path: str
    current_name: str
    proposed_name: str
    template_key: str | None = None
    template_label: str | None = None
    actionable: bool = Field(description="Whether this item would attempt a rename")
    status: str = Field(
        description="Preview status: ready, unchanged, unmatched, conflict, blocked"
    )
    reason: str | None = Field(default=None, description="Optional warning or skip reason")


class MassRenamePreviewResponse(BaseModel):
    """Preview payload for the mass rename workflow."""

    target: str
    scope: str
    item_count: int
    actionable_count: int
    items: list[MassRenamePreviewItem] = Field(default_factory=list)


# ── DB Check Preview ──────────────────────────────────────────


class DBCheckPreviewRequest(BaseModel):
    """Request body for DB check preview."""

    checks: list[str] = Field(default_factory=list, description="Selected DB checks to run")
    library_root: str | None = Field(
        default=None,
        description="Library root to scan when stale-file detection is enabled",
    )


class DBCheckPreviewFinding(BaseModel):
    """Single finding returned from the DB check preview flow."""

    finding_id: str
    check_type: str
    record_id: int | None = None
    record_type: str
    file_path: str | None = None
    description: str
    suggested_action: str
    allowed_actions: list[str] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)


class DBCheckPreviewResponse(BaseModel):
    """Preview payload for the DB check workflow."""

    checks: list[str]
    finding_count: int
    findings: list[DBCheckPreviewFinding] = Field(default_factory=list)
