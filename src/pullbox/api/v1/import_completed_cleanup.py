"""Authenticated completed-import cleanup endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request

from pullbox.api.deps import DbSession, InteractiveOperatorUser  # noqa: TC001
from pullbox.schemas.import_completed_cleanup import (
    CompletedImportCleanupApplyRequest,
    CompletedImportCleanupPreviewRead,
    CompletedImportCleanupResultRead,
)
from pullbox.services.audit_service import source_ip_from_request
from pullbox.services.import_completed_cleanup import (
    CompletedImportCleanupAction,
    apply_completed_import_cleanup,
    preview_completed_import_cleanup,
)
from pullbox.tasks.import_task import trigger_import_execute

router = APIRouter(prefix="/import", tags=["import"])


@router.get(
    "/{job_id}/cleanup/{action}/preview",
    response_model=CompletedImportCleanupPreviewRead,
)
async def preview_completed_import_cleanup_route(
    job_id: int,
    action: CompletedImportCleanupAction,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> CompletedImportCleanupPreviewRead:
    """Preview an exact completed-job recovery scope."""
    preview = await preview_completed_import_cleanup(
        session,
        job_id,
        action,
        actor_id=_user.id,
    )
    return CompletedImportCleanupPreviewRead.model_validate(preview, from_attributes=True)


@router.post(
    "/{job_id}/cleanup/{action}",
    response_model=CompletedImportCleanupResultRead,
)
async def apply_completed_import_cleanup_route(
    job_id: int,
    action: CompletedImportCleanupAction,
    body: CompletedImportCleanupApplyRequest,
    request: Request,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> CompletedImportCleanupResultRead:
    """Apply a signed recovery scope and resume only the affected work."""
    from pullbox.composition.services import build_import_service

    result = await apply_completed_import_cleanup(
        session,
        job_id,
        action,
        actor_id=_user.id,
        actor_username=_user.username,
        source_ip=source_ip_from_request(request),
        preview_token=body.preview_token,
    )

    if action is CompletedImportCleanupAction.RETRY_SOURCE_INSPECTION:
        service = await build_import_service(session)
        _job, retrying_count = await service.retry_failed_series(session, job_id)
        result_read = CompletedImportCleanupResultRead.model_validate(
            result,
            from_attributes=True,
        ).model_copy(update={"requires_import_retry": retrying_count > 0})
        await session.commit()
        if retrying_count > 0:
            trigger_import_execute(job_id)
        return result_read

    await session.commit()
    if result.requires_import_retry:
        trigger_import_execute(job_id)
    return CompletedImportCleanupResultRead.model_validate(result, from_attributes=True)
