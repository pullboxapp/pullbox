"""Schemas for completed-import recovery cleanup."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from pullbox.services.import_completed_cleanup import (  # noqa: TC001 - Pydantic enum
    CompletedImportCleanupAction,
)


class CompletedImportCleanupPreviewRead(BaseModel):
    """Bounded preview for one recovery action."""

    job_id: int
    action: CompletedImportCleanupAction
    affected_count: int
    affected_file_count: int
    item_unit: str
    examples: list[str] = Field(default_factory=list)
    preview_token: str
    confirmation_text: Literal["APPLY CLEANUP"] = "APPLY CLEANUP"


class CompletedImportCleanupApplyRequest(BaseModel):
    """Signed cleanup confirmation."""

    preview_token: str = Field(..., min_length=1)
    confirmation: Literal["APPLY CLEANUP"]


class CompletedImportCleanupResultRead(BaseModel):
    """Completed recovery mutation summary."""

    job_id: int
    action: CompletedImportCleanupAction
    affected_count: int
    affected_file_count: int
    requires_import_retry: bool


class CleanLibraryImportPreviewRead(BaseModel):
    """Exact referenced scope that will become a managed library."""

    source_job_id: int
    target_root_id: int
    eligible_file_count: int
    eligible_series_count: int
    total_bytes: int
    source_preserved: bool
    preview_token: str
    confirmation_text: Literal["BUILD CLEAN LIBRARY"] = "BUILD CLEAN LIBRARY"


class CleanLibraryImportCreateRequest(BaseModel):
    """Signed confirmation for a clean managed-library build."""

    target_root_id: int = Field(..., gt=0)
    preview_token: str = Field(..., min_length=1)
    confirmation: Literal["BUILD CLEAN LIBRARY"]


class CleanLibraryImportResultRead(BaseModel):
    """Background managed-copy import created from a reference import."""

    source_job_id: int
    job_id: int
    eligible_file_count: int
    eligible_series_count: int
