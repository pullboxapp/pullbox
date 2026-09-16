"""Typed contracts for Mylar path-mapping analysis in Import Step 1."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pullbox.core.filesystem_policy import resolve_preview_source
from pullbox.core.mylar3_path_mapping import (
    MAX_MYLAR3_PATH_MAPPINGS,
    normalize_mylar3_path_mapping_items,
)
from pullbox.models.import_job import ImportFileHandlingMode, ImportSourceType

MylarPathOutcome = Literal[
    "identity",
    "mapped",
    "mapped_missing",
    "missing",
    "unmapped",
    "outside_root",
    "unreadable",
    "ambiguous",
    "invalid",
]
MylarPathAttentionActionKind = Literal[
    "acknowledge_unavailable",
    "register_reference_root",
    "remove_ineffective_mapping",
]


class MylarPathMappingDraft(BaseModel):
    """One editable Mylar-stored to Pullbox-visible mapping row."""

    stored_prefix: str = Field(..., min_length=1, max_length=4096)
    pullbox_prefix: str = Field(..., min_length=1, max_length=4096)


class MylarPathPreviewRequest(BaseModel):
    """Read-only path-mapping preview request."""

    source_path: str = Field(
        ...,
        description="Existing Mylar database or directory containing mylar.db",
    )
    source_type: ImportSourceType = ImportSourceType.MYLAR3
    file_handling_mode: ImportFileHandlingMode = ImportFileHandlingMode.MANAGED_COPY
    auto_detect: bool = True
    mappings: list[MylarPathMappingDraft] = Field(
        default_factory=list,
        max_length=MAX_MYLAR3_PATH_MAPPINGS,
    )

    @field_validator("source_path")
    @classmethod
    def validate_source(cls, value: str) -> str:
        """Resolve the selected source without allowing a database path race."""
        selected = resolve_preview_source(value)
        database = selected / "mylar.db" if selected.is_dir() else selected
        database = resolve_preview_source(database)
        if not database.is_file():
            raise ValueError("Mylar path preview requires a mylar.db file")
        return str(selected)

    @model_validator(mode="after")
    def validate_mapping_drafts(self) -> MylarPathPreviewRequest:
        """Reject malformed or conflicting editor rows before filesystem probing."""
        if self.source_type != ImportSourceType.MYLAR3:
            raise ValueError("Mylar path preview only supports Mylar imports")
        normalize_mylar3_path_mapping_items(
            (mapping.stored_prefix, mapping.pullbox_prefix) for mapping in self.mappings
        )
        return self


class MylarPathResolutionCounts(BaseModel):
    """Complete aggregate resolution counts for inspected ComicLocation rows."""

    model_config = ConfigDict(from_attributes=True)

    locations: int = Field(0, ge=0)
    identity_resolved: int = Field(0, ge=0)
    mapped_existing: int = Field(0, ge=0)
    mapped_missing: int = Field(0, ge=0)
    missing: int = Field(0, ge=0)
    unmapped: int = Field(0, ge=0)
    outside_root: int = Field(0, ge=0)
    unreadable: int = Field(0, ge=0)
    ambiguous: int = Field(0, ge=0)
    invalid: int = Field(0, ge=0)


class MylarPathExample(BaseModel):
    """One bounded path relative to its displayed mapping prefix."""

    relative_path: str
    outcome: MylarPathOutcome


class MylarPathException(BaseModel):
    """An unresolved location, with enough context for an operator to repair it."""

    series_id: str
    series_name: str
    stored_path: str
    attempted_path: str
    outcome: MylarPathOutcome
    reason: str
    suggested_action: str


class MylarPathProblemGroup(BaseModel):
    """One root-level explanation for repeated path failures."""

    root_path: str
    outcome: MylarPathOutcome
    series_count: int = Field(ge=0)
    location_count: int = Field(ge=0)
    reason: str
    suggested_action: str
    can_register_reference_root: bool = False


class MylarPathAttentionAction(BaseModel):
    """One server-authorized, deterministic Step 1 resolution."""

    kind: MylarPathAttentionActionKind
    fingerprint: str = Field(min_length=64, max_length=64)
    root_path: str | None = None
    stored_prefix: str | None = None
    pullbox_prefix: str | None = None


class MylarPathAttentionDetails(BaseModel):
    """Plain-language evidence and manual next steps for one grouped finding."""

    title: str
    series_count: int = Field(ge=0)
    location_count: int = Field(ge=0)
    known_paths: list[str] = Field(default_factory=list, max_length=6)
    steps: list[str] = Field(default_factory=list, min_length=1, max_length=6)


class MylarPathAttentionItem(BaseModel):
    """One grouped issue that Step 1 can resolve or explain."""

    key: str = Field(min_length=64, max_length=64)
    code: str
    blocks_import: bool
    reason: str
    suggested_action: str
    root_path: str | None = None
    action: MylarPathAttentionAction | None = None
    details: MylarPathAttentionDetails


class MylarIdentityGroupPreview(BaseModel):
    """Identity-resolved paths grouped under one enabled root."""

    stored_prefix: str
    library_root_id: int
    library_root_name: str
    resolution: MylarPathResolutionCounts
    examples: list[MylarPathExample] = Field(default_factory=list)


class MylarPathMappingPreview(BaseModel):
    """One normalized mapping row with complete resolution evidence."""

    stored_prefix: str
    pullbox_prefix: str
    library_root_id: int | None = None
    library_root_name: str | None = None
    provenance: Literal["automatic", "manual"]
    status: Literal["ready", "review", "blocked"]
    resolution: MylarPathResolutionCounts
    examples: list[MylarPathExample] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)


class MylarPathPreviewResponse(BaseModel):
    """Safe, bounded Step 1 mapping evidence and frozen compatibility map."""

    source_type: ImportSourceType = ImportSourceType.MYLAR3
    resolution: MylarPathResolutionCounts
    identity_groups: list[MylarIdentityGroupPreview] = Field(default_factory=list)
    mappings: list[MylarPathMappingPreview] = Field(default_factory=list)
    path_map: dict[str, str] = Field(default_factory=dict)
    requires_confirmation: bool
    can_confirm: bool
    can_continue_with_unresolved: bool = False
    requires_unresolved_acknowledgement: bool = False
    unresolved_fingerprint: str | None = None
    exception_count: int = 0
    exceptions: list[MylarPathException] = Field(default_factory=list)
    problem_groups: list[MylarPathProblemGroup] = Field(default_factory=list)
    attention_items: list[MylarPathAttentionItem] = Field(default_factory=list)
    attention_fingerprint: str | None = None
    report_id: str | None = None
    blocking_reasons: list[str] = Field(default_factory=list)
    partial: bool = False
    warnings: list[str] = Field(default_factory=list)
