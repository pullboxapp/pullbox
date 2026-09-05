"""Schemas for non-destructive import history archival."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - Pydantic resolves this at runtime

from pydantic import BaseModel


class ImportJobArchiveResponse(BaseModel):
    """Current archive state for one import job."""

    job_id: int
    archived: bool
    archived_at: datetime | None
