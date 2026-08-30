"""Typed request and response contracts for Story Arc Step 1 analysis."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pullbox.models.import_job import ImportSourceType


class StoryArcPreflightRequest(BaseModel):
    """Read-only source request made before an import job exists."""

    source_path: str = Field(..., description="Existing folder or Mylar database to inspect")
    source_type: ImportSourceType

    @model_validator(mode="after")
    def validate_source(self) -> StoryArcPreflightRequest:
        """Resolve a source without broadening the import source contract."""
        path = Path(self.source_path).expanduser()
        if not path.exists():
            raise ValueError(f"Path does not exist: {self.source_path}")
        if self.source_type is ImportSourceType.FILESYSTEM and not path.is_dir():
            raise ValueError("Filesystem Story Arc analysis requires a directory")
        if self.source_type is ImportSourceType.MYLAR3:
            database = path / "mylar.db" if path.is_dir() else path
            if not database.is_file():
                raise ValueError("Mylar Story Arc analysis requires a mylar.db file")
        self.source_path = str(path.resolve())
        return self


class StoryArcResolutionPreview(BaseModel):
    """Pre-match counts; unresolved states remain explicit rather than inferred."""

    model_config = ConfigDict(from_attributes=True)

    resolved: int = Field(0, ge=0)
    pending: int = Field(0, ge=0)
    missing: int = Field(0, ge=0)
    ambiguous: int = Field(0, ge=0)
    conflicts: int = Field(0, ge=0)
    duplicates: int = Field(0, ge=0)


class StoryArcSettingPreview(BaseModel):
    """One allowlisted, path-safe Mylar setting shown in Step 1."""

    model_config = ConfigDict(from_attributes=True)

    key: str
    value: bool | str | None
    used_default: bool


class StoryArcEvidenceExample(BaseModel):
    """One bounded, sanitized source-relative Story Arc example."""

    model_config = ConfigDict(from_attributes=True)

    story_arc: str | None
    series: str | None
    issue_number: str | None
    issue_title: str | None
    reading_order: str | None
    status: str | None
    relative_path: str | None = None


class StoryArcPolicyPreview(BaseModel):
    """Path-safe summary of a policy that still requires Step 3 confirmation."""

    model_config = ConfigDict(from_attributes=True)

    mode: str
    destination_root_configured: bool
    folder_template: str
    file_template: str
    reading_order_prefix: bool
    synchronize: bool
    requires_confirmation: bool = True


class StoryArcPreflightResponse(BaseModel):
    """Bounded source evidence for conditional Story Arc controls in Step 1."""

    model_config = ConfigDict(from_attributes=True)

    source_type: ImportSourceType
    evidence_detected: bool
    arcs_detected: int = Field(0, ge=0)
    entries_detected: int = Field(0, ge=0)
    resolution: StoryArcResolutionPreview = Field(default_factory=StoryArcResolutionPreview)
    existing_arc_files_detected: bool = False
    existing_arc_folders_detected: bool = False
    pattern_summary: str | None = None
    settings: list[StoryArcSettingPreview] = Field(default_factory=list)
    examples: list[StoryArcEvidenceExample] = Field(default_factory=list)
    provider_calls_required: bool
    provider_call_summary: str
    proposed_policy: StoryArcPolicyPreview
    readlist_present: bool = False
    readlist_count: int = Field(0, ge=0)
    readlist_import_state: str | None = None
    archive_probes: int = Field(0, ge=0)
    partial: bool = False
    warnings: list[str] = Field(default_factory=list)
