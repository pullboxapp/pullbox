"""API schemas for category-scoped import safety review."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pullbox.models.import_job import ImportSourceType  # noqa: TC001
from pullbox.services.import_safety_diagnostics import ImportSafetyCategory  # noqa: TC001


class ImportSafetyBulkAllowRequest(BaseModel):
    """Explicit confirmation of a signed category preview."""

    preview_token: str = Field(min_length=1, max_length=4096)
    confirmation: Literal["ALLOW ONCE"]


class ImportSafetyBulkPreviewRead(BaseModel):
    """Sanitized bounded preview of one job/category scope."""

    model_config = ConfigDict(from_attributes=True)

    job_id: int
    source_type: ImportSourceType
    category: ImportSafetyCategory
    action: Literal["allow_once"] = "allow_once"
    matching_count: int
    affected_count: int
    skipped_count: int
    examples: list[str]
    overrideable: bool
    requires_confirmation: bool = False
    confirmation_text: Literal["ALLOW ONCE"] | None = None
    preview_token: str | None


class ImportSafetyBulkResultRead(BaseModel):
    """Sanitized outcome counts for one category-scoped action."""

    model_config = ConfigDict(from_attributes=True)

    job_id: int
    source_type: ImportSourceType
    category: ImportSafetyCategory
    action: Literal["allow_once"] = "allow_once"
    affected_count: int
    skipped_count: int
