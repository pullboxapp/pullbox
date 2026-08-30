"""Import job request/response schemas."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - Pydantic needs this at runtime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pullbox.models.import_job import (
    ImportControlRequest,
    ImportedFileStatus,
    ImportFileHandlingMode,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.schemas.import_layout import SourceLayoutSpecPayload
from pullbox.schemas.story_arc_placement import (  # noqa: TC001 - Pydantic resolves at runtime
    StoryArcPlacementPolicyPayload,
)

# ── Request Schemas ──────────────────────────────────────────────────────


class FutureRootPolicyPayload(BaseModel):
    """Complete proposed naming policy for a later per-root implementation."""

    schema_version: Literal[1] = 1
    series_path_template: str = Field(..., min_length=1, max_length=1024)
    comic_file_template: str = Field(..., min_length=1, max_length=1024)
    annual_file_template: str = Field(..., min_length=1, max_length=1024)
    non_standard_file_template: str = Field(..., min_length=1, max_length=1024)
    single_non_standard_file_template: str = Field(..., min_length=1, max_length=1024)
    replace_illegal_characters: bool
    colon_replacement: Literal["dash", "space", "empty", "smart"]


class ImportJobCreate(BaseModel):
    """Start a new import job."""

    source_path: str = Field(..., description="Absolute path to scan or Mylar3 DB file")
    file_paths: list[str] | None = Field(
        None,
        description="Explicit file paths to import (skips directory scan)",
    )
    source_type: ImportSourceType = Field(..., description="filesystem or mylar3")
    target_library_root_id: int | None = Field(
        None, gt=0, description="Assign imported series to this library root"
    )
    monitored: bool = Field(False, description="Monitor imported series for auto-search")
    search_on_add: bool | None = Field(
        None,
        description=(
            "Deprecated compatibility field. Search on add is now controlled "
            "by the global import policy."
        ),
    )
    mylar3_path_map: dict[str, str] = Field(
        default_factory=dict,
        description="Docker volume path mapping {container_prefix: host_prefix}",
    )
    cv_match_threshold: float = Field(
        0.70, ge=0.50, le=1.00, description="Minimum CV match score to accept"
    )
    min_files_per_series: int = Field(
        1, ge=1, le=50, description="Minimum comic files to consider a folder a series"
    )
    file_formats: str | None = Field(
        None, description="Comma-separated file extensions to scan (e.g. 'cbz, cbr, pdf')"
    )
    source_layout: SourceLayoutSpecPayload = Field(
        default_factory=SourceLayoutSpecPayload,
        description="Versioned interpretation of source folders and filenames",
    )
    file_handling_mode: ImportFileHandlingMode = Field(
        ImportFileHandlingMode.MANAGED_COPY,
        description="Whether Pullbox creates a managed artifact or references the source",
    )
    future_layout_requested: bool = Field(
        False,
        description="Whether the proposed layout should become the target root's future policy",
    )
    future_root_policy: FutureRootPolicyPayload | None = Field(
        None,
        description="Complete proposed future policy when future_layout_requested is true",
    )
    story_arc_import_requested: bool = Field(
        False,
        description=(
            "Step 1 intent to review and import detected logical story arcs and memberships"
        ),
    )
    story_arc_materialization_requested: bool = Field(
        False,
        description=(
            "Step 1 intent to review separate story-arc folder placements in addition to "
            "logical memberships"
        ),
    )

    @model_validator(mode="after")
    def validate_future_policy_pair(self) -> ImportJobCreate:
        """Require paired Step 1 intent fields to agree."""
        if self.future_layout_requested and self.future_root_policy is None:
            raise ValueError("future_root_policy is required when future_layout_requested is true")
        if not self.future_layout_requested and self.future_root_policy is not None:
            raise ValueError("future_root_policy requires future_layout_requested to be true")
        if self.story_arc_materialization_requested and not self.story_arc_import_requested:
            raise ValueError(
                "Story arc materialization requires story_arc_import_requested to be true"
            )
        return self

    @field_validator("file_formats")
    @classmethod
    def normalize_file_formats(cls, v: str | None) -> str | None:
        """Normalize file formats: strip, lowercase, remove dots, filter empties."""
        if v is None:
            return None
        parts = [p.strip().lower().lstrip(".") for p in v.split(",")]
        parts = [p for p in parts if p]
        if not parts:
            return None
        return ", ".join(parts)

    @field_validator("source_path")
    @classmethod
    def validate_source_path(cls, v: str) -> str:
        """Ensure the source path exists and resolve it."""
        from pathlib import Path

        p = Path(v)
        if not p.exists():
            msg = f"Path does not exist: {v}"
            raise ValueError(msg)
        return str(p.resolve())

    @field_validator("file_paths")
    @classmethod
    def validate_file_paths(cls, v: list[str] | None) -> list[str] | None:
        """Validate and resolve each file path."""
        if v is None:
            return None
        from pathlib import Path

        resolved = []
        for path_str in v:
            p = Path(path_str)
            if not p.exists():
                msg = f"File does not exist: {path_str}"
                raise ValueError(msg)
            resolved.append(str(p.resolve()))
        return resolved


# ── Confirm / Override Schemas ───────────────────────────────────────────


class FileMatchOverride(BaseModel):
    """User manually sets an issue match for an imported file."""

    imported_file_id: int = Field(..., gt=0, description="ImportedFile ID to update")
    issue_id: int = Field(..., gt=0, description="Issue ID to assign")


class ConflictResolution(BaseModel):
    """User resolves a conflict group by choosing a file."""

    conflict_group_id: int = Field(..., description="Conflict group ID")
    chosen_file_id: int = Field(..., gt=0, description="ImportedFile ID to keep")


class StoryArcReviewDecision(BaseModel):
    """Explicit Step 3 decision for one staged story arc."""

    imported_story_arc_id: int = Field(..., gt=0, description="ImportedStoryArc ID")
    action: Literal["select", "skip"] = Field(..., description="Import or skip this arc")
    proposed_story_arc_id: int | None = Field(
        None,
        gt=0,
        description="Existing StoryArc target when the staged arc should be merged",
    )

    @model_validator(mode="after")
    def validate_skip_has_no_merge_target(self) -> StoryArcReviewDecision:
        """A skipped staged arc cannot retain an active merge decision."""
        if self.action == "skip" and self.proposed_story_arc_id is not None:
            raise ValueError("proposed_story_arc_id is only valid when action is select")
        return self


class StoryArcReviewDecisionRequest(BaseModel):
    """Update one staged story-arc decision from the Step 3 review UI."""

    action: Literal["select", "skip"] = Field(..., description="Import or skip this arc")
    proposed_story_arc_id: int | None = Field(
        None,
        gt=0,
        description="Existing StoryArc target when the staged arc should be merged",
    )

    @model_validator(mode="after")
    def validate_skip_has_no_merge_target(self) -> StoryArcReviewDecisionRequest:
        """A skipped staged arc cannot retain an active merge decision."""
        if self.action == "skip" and self.proposed_story_arc_id is not None:
            raise ValueError("proposed_story_arc_id is only valid when action is select")
        return self


class StoryArcReviewDecisionResponse(BaseModel):
    """Persisted review state for one staged story arc."""

    imported_story_arc_id: int
    status: str
    selected_for_import: bool
    proposed_story_arc_id: int | None


class StoryArcPolicyConfirmationRequest(BaseModel):
    """Explicitly freeze one staged arc policy during Step 3 review."""

    confirm_policy: Literal[True]
    expected_policy_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    materialize_filesystem: bool
    monitored: bool
    search_missing: bool
    include_upcoming: bool
    placement_policy: StoryArcPlacementPolicyPayload

    @model_validator(mode="after")
    def validate_materialization_choice(self) -> StoryArcPolicyConfirmationRequest:
        """Keep logical membership and filesystem materialization independent."""
        logical = self.placement_policy.mode.value == "logical"
        if self.materialize_filesystem == logical:
            raise ValueError(
                "materialize_filesystem must be false for logical policy and true otherwise"
            )
        if not self.monitored and (self.search_missing or self.include_upcoming):
            raise ValueError("search_missing and include_upcoming require monitored to be true")
        return self


class StoryArcPolicyConfirmationResponse(BaseModel):
    """Sanitized persisted state for one confirmed staged policy."""

    imported_story_arc_id: int
    activation: Literal["confirmed"]
    materialize_filesystem: bool
    mode: str
    monitored: bool
    search_missing: bool
    include_upcoming: bool
    sync_enabled: bool
    policy_digest: str


class ConfirmImportRequest(BaseModel):
    """Confirm which series to import from a REVIEW-state job."""

    series_ids: list[int] = Field(
        ...,
        description=(
            "Deprecated compatibility field. Step 3 series selection is now "
            "persisted server-side and this payload is ignored once durable "
            "selection state exists."
        ),
    )
    target_library_root_id: int | None = Field(
        None, gt=0, description="Override library root for this batch"
    )
    monitored: bool | None = Field(None, description="Override monitored state")
    search_on_add: bool | None = Field(
        None,
        description=(
            "Deprecated compatibility field. Search on add is now controlled "
            "by the global import policy."
        ),
    )
    move_to_library: bool | None = Field(
        None,
        description=(
            "Deprecated compatibility field. Collection import always creates library artifacts."
        ),
    )
    update_embedded_comicinfo_from_match: bool | None = Field(
        None,
        description=(
            "Deprecated compatibility field. Embedded ComicInfo updates are "
            "now controlled by the global ingest policy."
        ),
    )
    file_overrides: list[FileMatchOverride] = Field(
        default_factory=list, description="Manual file→issue match overrides"
    )
    conflict_resolutions: list[ConflictResolution] = Field(
        default_factory=list, description="Conflict group resolutions"
    )
    story_arc_ids: list[int] = Field(
        default_factory=list,
        description=(
            "Compatibility list of staged story arcs to select. Durable per-arc "
            "review decisions remain authoritative when already present."
        ),
    )
    story_arc_decisions: list[StoryArcReviewDecision] = Field(
        default_factory=list,
        description="Explicit select/skip and optional merge decisions for staged story arcs",
    )

    @field_validator("series_ids")
    @classmethod
    def validate_series_ids(cls, v: list[int]) -> list[int]:
        """Ensure positive IDs only and deduplicate while preserving order."""
        if any(sid <= 0 for sid in v):
            msg = "All series_ids must be positive integers"
            raise ValueError(msg)
        return list(dict.fromkeys(v))

    @field_validator("story_arc_ids")
    @classmethod
    def validate_story_arc_ids(cls, v: list[int]) -> list[int]:
        """Keep story-arc identity separate from series selection."""
        if any(arc_id <= 0 for arc_id in v):
            raise ValueError("All story_arc_ids must be positive integers")
        return list(dict.fromkeys(v))

    @field_validator("story_arc_decisions")
    @classmethod
    def validate_unique_story_arc_decisions(
        cls,
        v: list[StoryArcReviewDecision],
    ) -> list[StoryArcReviewDecision]:
        """Reject ambiguous duplicate decisions in one confirmation request."""
        ids = [decision.imported_story_arc_id for decision in v]
        if len(ids) != len(set(ids)):
            raise ValueError("story_arc_decisions must contain at most one decision per arc")
        return v


class SeriesSearchOverride(BaseModel):
    """User manually sets a ComicVine ID for a series candidate."""

    imported_series_id: int = Field(..., gt=0, description="ImportedSeries ID to update")
    cv_id: int = Field(..., gt=0, description="ComicVine volume ID to assign")


class AssignOrphanRequest(BaseModel):
    """Assign a ComicVine ID to an unmatched series and start guided recovery."""

    cv_id: int = Field(..., gt=0, description="ComicVine volume ID to assign")


class OrphanRecoveryDecision(BaseModel):
    """One recovery decision for an orphaned import file."""

    imported_file_id: int = Field(..., gt=0, description="ImportedFile ID to recover")
    action: Literal["assign", "skip"] = Field(..., description="How to resolve this file")
    issue_cv_id: int | None = Field(
        None,
        gt=0,
        description="ComicVine issue ID to assign when action=assign",
    )


class ImportReconcileDecision(BaseModel):
    """One Step 3 reconciliation decision for an active import file."""

    imported_file_id: int = Field(..., gt=0, description="ImportedFile ID to reconcile")
    action: Literal["assign", "skip", "provisional"] = Field(
        ...,
        description="How to resolve this file before import",
    )
    issue_cv_id: int | None = Field(
        None,
        gt=0,
        description="ComicVine issue ID to assign when action=assign",
    )
    provisional_issue_number: float | None = Field(
        None,
        gt=0,
        description="Issue number to create locally when action=provisional",
    )


class RecoverOrphanRequest(BaseModel):
    """Commit a guided recovery pass for an orphaned import group."""

    target_library_root_id: int | None = Field(
        None,
        gt=0,
        description="Library root to use if the import job has no stored target root",
    )
    decisions: list[OrphanRecoveryDecision] = Field(
        default_factory=list,
        description="Per-file assignment or skip decisions for this recovery pass",
    )


class ImportReconcileRequest(BaseModel):
    """Save Step 3 file-to-issue decisions before an import executes."""

    decisions: list[ImportReconcileDecision] = Field(
        default_factory=list,
        description="Per-file assignment or skip decisions for active import review.",
    )


# ── Response Schemas ─────────────────────────────────────────────────────


class ImportedSeriesRead(BaseModel):
    """Full details of a discovered series candidate."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    status: ImportSeriesStatus
    raw_series_name: str
    raw_year: int | None
    raw_publisher: str | None
    file_count: int
    has_files: bool
    sample_paths: list[str]
    source_folder: str | None

    # CV match fields
    cv_id: int | None
    cv_title: str | None
    cv_year: int | None
    cv_publisher: str | None
    cv_issue_count: int | None
    cv_url: str | None
    cv_match_score: float | None
    cv_match_method: str | None
    user_selected_cv_id: int | None
    selected_for_import: bool = False

    # File counters
    files_total: int = 0
    files_matched: int = 0
    files_duplicate: int = 0
    files_already_owned: int = 0
    files_conflict: int = 0
    files_no_match: int = 0
    files_imported: int = 0
    files_failed: int = 0

    # Outcome
    series_id: int | None
    error_message: str | None
    diagnostics: dict[str, object] = Field(default_factory=dict)


class ImportedFileRead(BaseModel):
    """Full details of an imported file candidate."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    import_job_id: int
    import_series_id: int
    file_path: str
    file_name: str
    file_size: int
    file_format: str
    parsed_series: str | None
    parsed_issue_number: float | None
    parsed_year: int | None
    has_comicinfo: bool
    comicvine_issue_id: int | None
    issue_number_raw: str | None
    status: ImportedFileStatus
    matched_issue_id: int | None
    match_confidence: str | None
    match_method: str | None
    conflict_group_id: int | None
    duplicate_group_id: int | None = None
    duplicate_of_file_id: int | None = None
    is_preferred: bool
    include_in_import: bool = False
    content_hash: str | None = None
    library_file_id: int | None
    error_message: str | None
    diagnostics: dict[str, object] = Field(default_factory=dict)
    created_at: datetime


class FileConflictGroup(BaseModel):
    """A group of files that conflict on the same issue match."""

    kind: Literal["file_conflict", "series_conflict"] = "file_conflict"
    conflict_group_id: int | str
    matched_issue_id: int | None
    series_id: int | None = None
    issue_title: str | None = None
    diagnostics: dict[str, object] = Field(default_factory=dict)
    files: list[ImportedFileRead]


class ImportJobRead(BaseModel):
    """Full import job response (detail view and SSE status)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source_path: str
    selected_file_paths: list[str] = Field(default_factory=list)
    source_type: ImportSourceType
    status: ImportJobStatus

    # Counters
    scan_total_files: int
    scan_total_dirs: int
    series_found: int
    series_duplicate: int
    series_matched: int
    series_no_match: int
    series_new: int
    series_imported: int
    series_failed: int

    # Timestamps
    created_at: datetime
    scan_started_at: datetime | None
    scan_completed_at: datetime | None
    match_started_at: datetime | None
    match_completed_at: datetime | None
    import_started_at: datetime | None
    import_completed_at: datetime | None

    # Settings
    target_library_root_id: int | None
    monitored: bool
    search_on_add: bool
    move_to_library: bool = True
    transfer_method: str = "move"
    convert_to_preferred_format: bool = False
    update_embedded_comicinfo_from_match: bool = False
    file_handling_mode: ImportFileHandlingMode = ImportFileHandlingMode.MANAGED_COPY
    source_layout_snapshot: SourceLayoutSpecPayload = Field(default_factory=SourceLayoutSpecPayload)
    future_layout_requested: bool = False
    future_root_policy_snapshot: FutureRootPolicyPayload | None = None
    future_root_policy_applied_at: datetime | None = None
    story_arc_import_requested: bool = False
    story_arc_materialization_requested: bool = False
    cv_match_threshold: float
    min_files_per_series: int
    file_formats: str | None
    error_message: str | None
    progress_snapshot: dict[str, object] = Field(default_factory=dict)
    progress_revision: int = 0
    control_request: ImportControlRequest = ImportControlRequest.NONE
    total_files_duplicate: int = 0
    total_files_already_owned: int = 0


class ImportActivityRead(BaseModel):
    """Current background import activity for the persistent app shell."""

    job: ImportJobRead | None = None
    queued_count: int = 0


class FileMatchUpdateRequest(BaseModel):
    """Request to manually set a file's issue match."""

    issue_id: int = Field(..., gt=0, description="Issue ID to assign to this file")
    repair_source_metadata: bool = Field(
        False,
        description="Whether Pullbox should repair embedded ComicInfo.xml after assignment",
    )


class FileMetadataRepairRequest(BaseModel):
    """Request to repair embedded metadata for an imported file."""

    issue_id: int | None = Field(
        None,
        gt=0,
        description=(
            "Optional issue to use when repairing metadata. Falls back to the current match."
        ),
    )


class ImportJobListItem(BaseModel):
    """Summary row for the import jobs list."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source_path: str
    source_type: ImportSourceType
    status: ImportJobStatus
    series_found: int
    series_imported: int
    series_failed: int
    created_at: datetime
    import_completed_at: datetime | None


class ImportPreviewResponse(BaseModel):
    """Paginated series preview for the review step."""

    job: ImportJobRead
    items: list[ImportedSeriesRead]
    total: int
    page: int
    page_size: int


class ImportProgressEvent(BaseModel):
    """SSE event payload during scan/match/import phases."""

    job_id: int
    status: ImportJobStatus
    ephemeral_progress: bool = False
    snapshot_version: int = 2
    mode: Literal["scan", "import", "rollback"] = "scan"
    phase: str = Field(..., description="Current phase: scanning, matching, importing")
    progress: int = Field(..., ge=0, le=100, description="Completion percentage")
    message: str = ""
    requested_action: ImportControlRequest = ImportControlRequest.NONE
    progress_revision: int = 0
    last_checkpoint_at: datetime | None = None
    current_series_id: int | None = None
    current_series_name: str | None = None
    current_file_id: int | None = None
    current_file_name: str | None = None
    current_file_stage: str | None = None
    current_file_progress_current: int | None = None
    current_file_progress_total: int | None = None
    current_file_progress_pct: int | None = None
    current_file_progress_unit: str | None = None
    current_item_kind: str | None = None
    current_item_stage: str | None = None
    current_item_stage_label: str | None = None
    current_item_progress_pct: int | None = None
    current_item_detail: str | None = None
    current_series: str | None = None
    current_series_status: ImportSeriesStatus | None = None
    estimated_seconds_remaining: int | None = None
    elapsed_seconds: int | None = None

    # Real-time stat counters (populated from ImportJob model)
    scan_total_files: int | None = None
    scan_total_dirs: int | None = None
    series_found: int | None = None
    series_duplicate: int | None = None
    series_matched: int | None = None
    series_no_match: int | None = None
    series_new: int | None = None
    series_imported: int | None = None
    series_failed: int | None = None

    # File-level counters
    total_files_found: int | None = None
    total_files_matched: int | None = None
    total_files_duplicate: int | None = None
    total_files_already_owned: int | None = None
    total_files_conflict: int | None = None
    total_files_no_match: int | None = None
    total_files_imported: int | None = None
    total_files_failed: int | None = None
    story_arc_placements_total: int | None = None
    story_arc_placements_queued: int | None = None
    story_arc_placements_running: int | None = None
    story_arc_placements_retry_wait: int | None = None
    story_arc_placements_failed: int | None = None
    story_arc_placements_completed: int | None = None
    story_arc_placements_cancelled: int | None = None
    review_summary: dict[str, int] | None = None
    control_state: dict[str, object] | None = None


# ── Per-Job Log Schemas ─────────────────────────────────────────────────


class ImportJobLogEntry(BaseModel):
    """Single structured log entry for an import job."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    logged_at: datetime
    level: str
    event: str
    message: str | None
    data: dict[str, object]


class ImportJobLogsResponse(BaseModel):
    """Paginated log entries for an import job."""

    job_id: int
    items: list[ImportJobLogEntry]
    total: int
    page: int
    page_size: int


# ── Post-Import Recovery Schemas ──────────────────────────────────────


class ImportedFilesResponse(BaseModel):
    """Paginated list of imported files for a series."""

    items: list[ImportedFileRead]
    total: int
    page: int
    page_size: int


class ConflictGroupsResponse(BaseModel):
    """One bounded page of conflict groups for an import job."""

    job_id: int
    groups: list[FileConflictGroup]
    total: int
    page: int
    page_size: int


class FileSelectionUpdateRequest(BaseModel):
    """Request to include or exclude a matched import file from execution."""

    include_in_import: bool = Field(
        ...,
        description="Whether this file should be included in the final import run",
    )


class FileSelectionBulkUpdateRequest(BaseModel):
    """Bulk request to include or exclude importable duplicate-series files."""

    include_in_import: bool = Field(
        ...,
        description="Whether matching files in the current scope should be included",
    )
    imported_series_id: int | None = Field(
        None,
        gt=0,
        description=(
            "Optional duplicate imported-series scope to update. When omitted, all "
            "importable duplicate-series files for the job are updated."
        ),
    )


class SeriesSelectionUpdateRequest(BaseModel):
    """Request to include or exclude a matched import series from execution."""

    include_in_import: bool = Field(
        ...,
        description="Whether this matched series should be included in the final import run",
    )


class SeriesSelectionBulkUpdateRequest(BaseModel):
    """Bulk request to include or exclude matched import series in review."""

    include_in_import: bool = Field(
        ...,
        description="Whether the referenced matched series should be included",
    )
    imported_series_ids: list[int] = Field(
        default_factory=list,
        description=(
            "Matched ImportedSeries IDs to update. When empty and include_in_import=true, "
            "all importable matched series for the job are selected. When empty and "
            "include_in_import=false, all selected matched series for the job are cleared."
        ),
    )

    @field_validator("imported_series_ids")
    @classmethod
    def validate_imported_series_ids(cls, v: list[int]) -> list[int]:
        """Ensure positive IDs only and deduplicate while preserving order."""
        if any(series_id <= 0 for series_id in v):
            msg = "All imported_series_ids must be positive integers"
            raise ValueError(msg)
        return list(dict.fromkeys(v))


class ImportReviewSelectionState(BaseModel):
    """Canonical Step 3 selection state across matched and in-library rows."""

    matched_series_importable: int = 0
    matched_series_selected: int = 0
    duplicate_series_importable: int = 0
    duplicate_series_selected: int = 0
    duplicate_files_importable: int = 0
    duplicate_files_selected: int = 0
    importable_item_count: int = 0
    selected_item_count: int = 0
    selected_series_ids: list[int] = Field(default_factory=list)
    selected_duplicate_series_ids: list[int] = Field(default_factory=list)
    duplicate_selected_file_counts: dict[int, int] = Field(default_factory=dict)


class ImportSelectionBulkUpdateResponse(BaseModel):
    """Bulk selection mutation response with the refreshed canonical state."""

    updated: int
    include_in_import: bool
    selection_state: ImportReviewSelectionState


class ConflictResetRequest(BaseModel):
    """Request to reset resolved conflict groups back to review state."""

    group_ids: list[int] = Field(default_factory=list)


class ConflictResolveRequest(BaseModel):
    """Request to resolve a conflict group by choosing a file."""

    chosen_file_id: int = Field(..., gt=0, description="ImportedFile ID to keep")


class ConflictResolveBulkRequest(BaseModel):
    """Request to resolve multiple conflict groups in one operation."""

    resolutions: list[ConflictResolution] = Field(
        ...,
        min_length=1,
        description="Conflict group resolutions to apply",
    )


class ConflictResolveBulkResponse(BaseModel):
    """Summary response for a bulk conflict resolution operation."""

    resolved_group_ids: list[int] = Field(default_factory=list)
    resolved_series_ids: list[int] = Field(default_factory=list)
    resolved_count: int = 0


class OrphanedSeriesResponse(BaseModel):
    """Paginated list of active unmatched series from completed import jobs."""

    items: list[ImportedSeriesRead]
    total: int
    page: int
    page_size: int


class AssignOrphanResponse(BaseModel):
    """Result of selecting a ComicVine series for delayed orphan recovery."""

    imported_series_id: int
    status: ImportSeriesStatus
    cv_title: str
    recovery_required: bool = True
    files_remaining: int


class OrphanRecoveryIssueOption(BaseModel):
    """Issue choice shown in the orphan recovery UI."""

    issue_cv_id: int
    issue_number: float
    title: str | None = None
    release_date: str | None = None
    already_imported: bool = False


class OrphanRecoveryFileRead(BaseModel):
    """One imported file row shown in the orphan recovery UI."""

    imported_file_id: int
    file_name: str
    file_path: str
    file_format: str
    parsed_issue_number: float | None = None
    parsed_year: int | None = None
    comicvine_issue_id: int | None = None
    status: ImportedFileStatus
    error_message: str | None = None
    matched_issue_cv_id: int | None = None
    suggested_issue_cv_id: int | None = None
    suggested_issue_label: str | None = None
    decision_locked: bool = False
    diagnostics: dict[str, object] = Field(default_factory=dict)


class OrphanRecoveryResponse(BaseModel):
    """Recovery modal payload for an orphaned import series."""

    imported_series: ImportedSeriesRead
    issue_options: list[OrphanRecoveryIssueOption] = Field(default_factory=list)
    files: list[OrphanRecoveryFileRead] = Field(default_factory=list)
    requires_library_root: bool = False
    selected_library_root_id: int | None = None
    available_library_roots: list[dict[str, object]] = Field(default_factory=list)
    files_remaining: int = 0
    files_completed: int = 0


class RecoverOrphanResponse(BaseModel):
    """Result of applying an orphan recovery pass."""

    imported_series_id: int
    status: ImportSeriesStatus
    series_id: int | None = None
    imported_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    files_remaining: int = 0


class OrphanRecoveryProgressResponse(BaseModel):
    """Live progress snapshot for a background orphan-recovery run."""

    imported_series_id: int
    state: Literal["idle", "running", "completed", "failed"] = "idle"
    message: str = ""
    current_file_name: str | None = None
    current_file_stage: str | None = None
    current_file_progress_current: int | None = None
    current_file_progress_total: int | None = None
    current_file_progress_pct: int | None = None
    current_file_progress_unit: str | None = None
    file_index: int | None = None
    total_files: int | None = None
    result_status: ImportSeriesStatus | None = None
    imported_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    files_remaining: int | None = None
    error_message: str | None = None


class RetryFailedResponse(BaseModel):
    """Result of retrying failed series in a completed import job."""

    job_id: int
    retrying_count: int


class ImportJobDeleteResponse(BaseModel):
    """Accepted deletion whose cooperative rollback has not finished yet."""

    status: Literal["rollback_pending"] = "rollback_pending"
    message: str


class RetryStoryArcPlacementsResponse(BaseModel):
    """Result of reopening failed/cancelled placement work for a stalled import."""

    job_id: int
    retrying_count: int


class RetryImportResponse(BaseModel):
    """Result of creating a brand-new retry job from history."""

    job_id: int
    redirect_url: str
