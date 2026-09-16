"""Category-scoped bulk review endpoints for structured import safety blocks."""

from __future__ import annotations

from fastapi import APIRouter, Request

from pullbox.api.deps import DbSession, InteractiveOperatorUser  # noqa: TC001
from pullbox.core.exceptions import ValidationError
from pullbox.schemas.import_safety_bulk import (
    ImportSafetyBulkAllowRequest,
    ImportSafetyBulkPreviewRead,
    ImportSafetyBulkResultRead,
)
from pullbox.services.audit_service import source_ip_from_request
from pullbox.services.import_safety_bulk_review import (
    IMPORT_SAFETY_BULK_CONFIRMATION,
    ImportSafetyBulkInterruptedError,
    allow_import_safety_category_once,
    preview_import_safety_category,
)
from pullbox.services.import_safety_diagnostics import ImportSafetyCategory  # noqa: TC001
from pullbox.tasks.import_task import trigger_import_safety_bulk_rematch

router = APIRouter(prefix="/import", tags=["import"])


def _preview_response(preview: object) -> ImportSafetyBulkPreviewRead:
    payload = ImportSafetyBulkPreviewRead.model_validate(preview, from_attributes=True)
    return payload.model_copy(
        update={
            "requires_confirmation": payload.overrideable,
            "confirmation_text": (
                IMPORT_SAFETY_BULK_CONFIRMATION if payload.overrideable else None
            ),
        }
    )


@router.get(
    "/{job_id}/safety/categories/{category}/preview",
    response_model=ImportSafetyBulkPreviewRead,
)
async def preview_import_safety_bulk_action(
    job_id: int,
    category: ImportSafetyCategory,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> ImportSafetyBulkPreviewRead:
    """Preview one existing structured safety category in one import job."""
    preview = await preview_import_safety_category(
        session,
        job_id,
        category,
        actor_id=_user.id,
    )
    return _preview_response(preview)


@router.post(
    "/{job_id}/safety/categories/{category}/allow-once",
    response_model=ImportSafetyBulkResultRead,
)
async def allow_import_safety_category_once_route(
    job_id: int,
    category: ImportSafetyCategory,
    body: ImportSafetyBulkAllowRequest,
    request: Request,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> ImportSafetyBulkResultRead:
    """Apply a signed and explicitly confirmed category preview once."""
    try:
        result = await allow_import_safety_category_once(
            session,
            job_id,
            category,
            actor_id=_user.id,
            actor_username=_user.username,
            source_ip=source_ip_from_request(request),
            preview_token=body.preview_token,
        )
    except ImportSafetyBulkInterruptedError as exc:
        trigger_import_safety_bulk_rematch(job_id)
        raise ValidationError(
            "The safety bulk action stopped because the import job changed state."
        ) from exc

    trigger_import_safety_bulk_rematch(job_id)
    return ImportSafetyBulkResultRead.model_validate(result, from_attributes=True)
