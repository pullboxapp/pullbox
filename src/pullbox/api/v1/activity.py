"""Authenticated API for persistent global background activity."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from pullbox.api.deps import (  # noqa: TC001 - FastAPI resolves route annotations
    AuthenticatedStreamUser,
    AuthenticatedUser,
    DbSession,
)
from pullbox.schemas.operation_progress import (
    OperationAcknowledgementRead,
    OperationActivityRead,
    OperationItemProgressRead,
    OperationProgressMeasureRead,
    OperationProgressRead,
)
from pullbox.services.operation_activity import acknowledge_operation, list_operation_activity
from pullbox.utilities.sse import subscribe

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pullbox.models.operation_progress import OperationProgress

router = APIRouter(prefix="/activity", tags=["activity"])
_HEARTBEAT_SECONDS = 15.0


def operation_progress_read(operation: OperationProgress) -> OperationProgressRead:
    """Serialize one ORM projection without exposing internal model structure."""
    item = None
    if operation.item_key is not None and operation.item_label is not None:
        item = OperationItemProgressRead(
            key=operation.item_key,
            label=operation.item_label,
            phase=operation.item_phase or operation.phase,
            message=operation.item_message or "",
            current=operation.item_current,
            total=operation.item_total,
            percent=operation.item_percent,
            unit=operation.item_unit,
            indeterminate=operation.item_indeterminate,
        )
    return OperationProgressRead(
        id=operation.id,
        operation_type=operation.operation_type,
        operation_key=operation.operation_key,
        group_key=operation.group_key,
        state=operation.state,
        phase=operation.phase,
        title=operation.title,
        message=operation.message,
        source_label=operation.source_label,
        detail_url=operation.detail_url,
        visibility=operation.visibility,
        tone=operation.tone,
        attention_required=operation.attention_required,
        acknowledged_at=operation.acknowledged_at,
        revision=operation.revision,
        overall=OperationProgressMeasureRead(
            current=operation.overall_current,
            total=operation.overall_total,
            percent=operation.overall_percent,
            unit=operation.overall_unit,
            indeterminate=operation.overall_indeterminate,
        ),
        item=item,
        rate=operation.rate,
        rate_unit=operation.rate_unit,
        eta_seconds=operation.eta_seconds,
        detail_snapshot=operation.detail_snapshot,
        started_at=operation.started_at,
        completed_at=operation.completed_at,
        last_event_at=operation.last_event_at,
        updated_at=operation.updated_at,
    )


@router.get("", response_model=OperationActivityRead)
async def get_activity(
    _user: AuthenticatedUser,
    session: DbSession,
) -> OperationActivityRead:
    """Return visible active, recent, and attention-requiring operations."""
    activity = await list_operation_activity(session)
    return OperationActivityRead(
        operations=[operation_progress_read(item) for item in activity.operations],
        active_count=activity.active_count,
        spinner_count=activity.spinner_count,
        attention_count=activity.attention_count,
    )


@router.post("/{operation_id}/acknowledge", response_model=OperationAcknowledgementRead)
async def acknowledge_activity(
    operation_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> OperationAcknowledgementRead:
    """Acknowledge one attention item so it can leave the global popover."""
    operation = await acknowledge_operation(session, operation_id)
    if operation is None:
        raise HTTPException(status_code=404, detail="Activity item not found")
    return OperationAcknowledgementRead(acknowledged=True)


async def _iter_activity_events() -> AsyncIterator[str]:
    yield "event: ready\ndata: {}\n\n"
    async with subscribe("activity") as queue:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
            except TimeoutError:
                yield ": heartbeat\n\n"
                continue
            if event is None:
                break
            yield event.format_sse()


@router.get("/stream", response_class=StreamingResponse)
async def stream_activity(_user: AuthenticatedStreamUser) -> StreamingResponse:
    """Stream lightweight invalidations; clients hydrate from the durable API."""
    return StreamingResponse(
        _iter_activity_events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
