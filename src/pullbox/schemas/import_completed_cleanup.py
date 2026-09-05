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
