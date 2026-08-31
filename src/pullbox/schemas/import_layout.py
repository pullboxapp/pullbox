"""Request and response schemas for read-only import layout analysis."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pullbox.core.filesystem_policy import resolve_preview_source
from pullbox.core.library_layout import (
    ImportLayoutMode,
    LayoutClassification,
    LayoutTemplateError,
    SourceLayoutSpec,
    resolve_source_layout_spec,
)
from pullbox.models.import_job import ImportSourceType


class SourceLayoutSpecPayload(BaseModel):
    """Versioned source layout selection supplied by an API client."""

    model_config = ConfigDict(from_attributes=True)

    schema_version: Literal[1] = 1
    mode: ImportLayoutMode = ImportLayoutMode.AUTO
    preset: str | None = None
    series_path_template: str | None = None
    issue_filename_template: str | None = None
    selected_cluster_id: str | None = None
    fallback_to_auto: bool = True

    def to_core(self) -> SourceLayoutSpec:
        """Return the validated internal DTO without API-layer dependencies."""
        return SourceLayoutSpec(
            schema_version=self.schema_version,
            mode=self.mode,
            preset=self.preset,
            series_path_template=self.series_path_template,
            issue_filename_template=self.issue_filename_template,
            selected_cluster_id=self.selected_cluster_id,
            fallback_to_auto=self.fallback_to_auto,
        )

    @model_validator(mode="after")
    def validate_layout_contract(self) -> SourceLayoutSpecPayload:
        """Apply the same grammar validation used by the analyzer."""
        try:
            effective = resolve_source_layout_spec(self.to_core())
            if effective.mode != ImportLayoutMode.AUTO:
                from pullbox.core.library_layout import compile_source_layout

                compile_source_layout(effective)
        except LayoutTemplateError as exc:
            raise ValueError(str(exc)) from exc
        return self


class LayoutPreviewRequest(BaseModel):
    """Read-only source layout preflight request."""

    source_path: str = Field(..., description="Existing filesystem directory to inspect")
    source_type: ImportSourceType = Field(..., description="Import source type")
    layout: SourceLayoutSpecPayload = Field(default_factory=SourceLayoutSpecPayload)

    @field_validator("source_path")
    @classmethod
    def validate_source_directory(cls, value: str) -> str:
        """Resolve an existing directory without accepting a file as a scan root."""
        path = resolve_preview_source(value)
        if not path.is_dir():
            raise ValueError("Layout preview source must be a directory")
        return str(path)

    @model_validator(mode="after")
    def validate_supported_source_type(self) -> LayoutPreviewRequest:
        """Keep the first endpoint read-only and filesystem-specific."""
        if self.source_type != ImportSourceType.FILESYSTEM:
            raise ValueError("Layout preview currently supports filesystem sources only")
        return self


class LayoutExampleResponse(BaseModel):
    """One bounded root-relative layout example."""

    model_config = ConfigDict(from_attributes=True)

    relative_path: str
    publisher: str | None
    series: str | None
    year: int | None
    issue_number: str | None
    issue_title: str | None
    evidence: list[str]
    warnings: list[str]


class LayoutClusterResponse(BaseModel):
    """Bounded summary of one detected source layout cluster."""

    model_config = ConfigDict(from_attributes=True)

    cluster_id: str
    classification: LayoutClassification
    file_count: int
    directory_count: int
    confidence: Literal["high", "medium", "low"]
    proposed_series_path_template: str | None
    proposed_issue_filename_template: str | None
    examples: list[LayoutExampleResponse]


class LayoutAnalysisResponse(BaseModel):
    """Serializable read-only layout analysis result."""

    model_config = ConfigDict(from_attributes=True)

    effective_spec: SourceLayoutSpecPayload
    classification: LayoutClassification
    clusters: list[LayoutClusterResponse]
    directories_considered: int
    files_considered: int
    files_fitting: int
    files_ambiguous: int
    files_outside_root: int
    archive_probes: int
    can_keep_in_place: bool
    can_apply_future_policy: bool
    partial: bool
    warnings: list[str]
