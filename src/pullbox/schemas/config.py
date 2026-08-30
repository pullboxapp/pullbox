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


class LibraryRootPolicyPreviewRequest(BaseModel):
    """Unsaved root-policy proposal to render against representative metadata."""

    policy: FutureRootPolicyPayload


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
