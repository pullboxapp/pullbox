"""API schemas for shared background-operation progress."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - Pydantic resolves this at runtime

from pydantic import BaseModel, ConfigDict

from pullbox.models.operation_progress import (  # noqa: TC001 - Pydantic field types
    OperationProgressState,
    OperationProgressTone,
    OperationProgressType,
    OperationProgressVisibility,
)


class OperationProgressMeasureRead(BaseModel):
    current: int | None = None
    total: int | None = None
    percent: float | None = None
    unit: str | None = None
    indeterminate: bool = True


class OperationItemProgressRead(OperationProgressMeasureRead):
    key: str
    label: str
    phase: str
    message: str = ""


class OperationProgressRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    operation_type: OperationProgressType
    operation_key: str
    group_key: str | None = None
    state: OperationProgressState
    phase: str
    title: str
    message: str
    source_label: str | None = None
    detail_url: str | None = None
    visibility: OperationProgressVisibility
    tone: OperationProgressTone
    attention_required: bool
    acknowledged_at: datetime | None = None
    revision: int
    overall: OperationProgressMeasureRead
    item: OperationItemProgressRead | None = None
    rate: float | None = None
    rate_unit: str | None = None
    eta_seconds: int | None = None
    detail_snapshot: dict[str, object]
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_event_at: datetime
    updated_at: datetime


class OperationActivityRead(BaseModel):
    operations: list[OperationProgressRead]
    active_count: int
    spinner_count: int
    attention_count: int


class OperationAcknowledgementRead(BaseModel):
    acknowledged: bool
