"""Configuration request/response schemas."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pullbox.schemas.import_job import FutureRootPolicyPayload


class ConfigResponse(BaseModel):
    """System configuration key-value pair."""

    model_config = ConfigDict(from_attributes=True)

    key: str = Field(description="Configuration key")
    value: str = Field(description="Configuration value")
    value_type: str = Field(description="Value type (string, int, bool, json)")
    description: str | None = None


class ConfigUpdate(BaseModel):
    """Request body for updating configuration values."""

    values: dict[str, str] = Field(..., min_length=1, description="Key-value pairs to update")


class LibraryRootCreate(BaseModel):
    """Create an explicit persistent container-visible library root."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=255)
    path: str = Field(..., min_length=1, max_length=1000)
    allow_referenced_registrations: bool = True
    allow_managed_writes: bool = True
    is_default_managed_destination: bool = False


class LibraryRootUpdate(BaseModel):
    """Update mutable root metadata without rebinding its path."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, max_length=255)
    enabled: bool | None = None
    allow_referenced_registrations: bool | None = None
    allow_managed_writes: bool | None = None
    is_default_managed_destination: bool | None = None


class LibraryRootState(BaseModel):
    """Persisted root configuration plus a live capability snapshot."""

    id: int
    name: str
    path: str
    enabled: bool
    allow_referenced_registrations: bool
    allow_managed_writes: bool
    is_default_managed_destination: bool
    available: bool
    readable: bool
    writable: bool
    free_bytes: int | None
    status: Literal["ready", "read_only", "low_capacity", "unavailable"]
    warnings: list[str]
    can_disable: bool


class LibraryRootPreviewResponse(BaseModel):
    """Non-persisting validation result for a proposed root."""

    name: str
    path: str
    allow_referenced_registrations: bool
    allow_managed_writes: bool
    is_default_managed_destination: bool
    available: bool
    readable: bool
    writable: bool
    free_bytes: int | None
    status: Literal["ready", "read_only", "low_capacity", "unavailable"]
    warnings: list[str]
    blocking_reasons: list[str]
    can_create: bool


class LibraryRootRebindPreviewRequest(BaseModel):
    """Request a write-free preview for an established root path replacement."""

    model_config = ConfigDict(extra="forbid")

    replacement_path: str = Field(..., min_length=1, max_length=1000)


class LibraryRootRebindImpact(BaseModel):
    """Aggregate persisted associations affected by changing one root identity path."""

    library_file_count: int = Field(..., ge=0)
    series_count: int = Field(..., ge=0)
    preferred_series_count: int = Field(..., ge=0)
    story_arc_placement_count: int = Field(..., ge=0)
    library_file_blocking_count: int = Field(..., ge=0)
    series_blocking_count: int = Field(..., ge=0)
    story_arc_placement_blocking_count: int = Field(..., ge=0)
    affects_default_destination: bool
    affects_preferred_series: bool


class LibraryRootRebindPreviewResponse(BaseModel):
    """Signed, non-persisting root path rebind preview."""

    library_root_id: int
    root_name: str
    current_path: str
    replacement_path: str
    available: bool
    readable: bool
    writable: bool
    free_bytes: int | None
    status: Literal["ready", "read_only", "low_capacity", "unavailable"]
    warnings: list[str]
    blocking_reasons: list[str]
    same_physical_directory: bool
    overlaps_current_path: bool
    impact: LibraryRootRebindImpact
    can_rebind: bool
    preview_token: str | None


class LibraryRootRebindConfirmRequest(BaseModel):
    """Explicit confirmation bound to one signed rebind preview."""

    model_config = ConfigDict(extra="forbid")

    replacement_path: str = Field(..., min_length=1, max_length=1000)
    preview_token: str = Field(..., min_length=1, max_length=4096)
    confirmation: Literal["REBIND"]


class NamingPreview(BaseModel):
    """Preview of file naming convention applied to sample data."""

    template: str = Field(description="The naming template string")
    examples: list[str] = Field(description="Sample filenames generated from the template")


class NamingPreviewEntry(BaseModel):
    """Single preview example showing input metadata and formatted output."""

    input: str = Field(description="Human-readable description of the source metadata")
    output: str = Field(description="Formatted filename or folder name")


class NamingPreviewGrouped(BaseModel):
    """Grouped preview results for a naming template."""

    template: str = Field(description="The naming template string")
    template_type: str = Field(description="Template type: folder, standard, annual, non_standard")
    examples: list[NamingPreviewEntry] = Field(description="Preview examples")


class LibraryRootPolicyUpdate(BaseModel):
    """Optimistic update for one library root's explicit naming policy."""

    expected_revision: int = Field(..., ge=0)
    policy: FutureRootPolicyPayload


class LibraryRootPolicyClear(BaseModel):
    """Optimistic removal of one library root's explicit naming policy."""

    expected_revision: int = Field(..., ge=0)


class LibraryRootPolicyPreviewExample(BaseModel):
    """One bounded real-source example used for old/new policy comparison."""

    publisher: str | None = Field(None, max_length=255)
    series: str = Field(..., min_length=1, max_length=500)
    year: int | None = Field(None, ge=1, le=9999)
    issue_number: float
    issue_title: str | None = Field(None, max_length=500)


class LibraryRootPolicyPreviewRequest(BaseModel):
    """Unsaved root-policy proposal to render against representative metadata."""

    policy: FutureRootPolicyPayload
    examples: list[LibraryRootPolicyPreviewExample] = Field(default_factory=list, max_length=5)


class EffectiveLibraryRootPolicy(BaseModel):
    """Complete effective naming policy returned for one root."""

    schema_version: Literal[1] = 1
    series_path_template: str
    series_folder_template: str
    comic_file_template: str
    annual_file_template: str
    non_standard_file_template: str
    single_non_standard_file_template: str
    replace_illegal_characters: bool
    colon_replacement: Literal["dash", "space", "empty", "smart"]
    source: Literal["global_default", "import_adoption", "manual"]
    source_import_job_id: int | None


class LibraryRootPolicyState(BaseModel):
    """Effective scope and optimistic revision for a library root policy."""

    library_root_id: int
    library_root_name: str
    scope: Literal["global_default", "root_override"]
    policy_id: int | None
    revision: int
    effective_policy: EffectiveLibraryRootPolicy


class LibraryRootPolicyPreviewResponse(BaseModel):
    """Current and proposed output examples without persisting a policy."""

    current_scope: Literal["global_default", "root_override"]
    current_series_paths: list[str]
    proposed_series_paths: list[str]
    current_file_names: list[str]
    proposed_file_names: list[str]
