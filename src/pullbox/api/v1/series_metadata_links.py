"""Operator review of additional identities for an existing series."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path

from pullbox.api.deps import DbSession, InteractiveOperatorUser, Settings
from pullbox.core.exceptions import ValidationError
from pullbox.schemas.metadata_identity_review import IdentityReviewReceiptRead
from pullbox.schemas.metadata_sources import SeriesPreviewQuery
from pullbox.schemas.series_metadata_editor import (
    SeriesMetadataEditorApproval,
    SeriesMetadataEditorPreview,
    SeriesMetadataEditorRetry,
    SeriesMetadataEditorReview,
)
from pullbox.schemas.series_metadata_links import (
    SeriesLinkConfirm,
    SeriesLinkPreview,
    SeriesLinksRead,
)
from pullbox.schemas.series_sidecar import (
    SeriesSidecarApproval,
    SeriesSidecarPreview,
    SeriesSidecarWriteRead,
)
from pullbox.services.series_metadata_editor import (
    PLAN,
    editor_job_details,
    latest_editor_job,
    prepare_series_editor,
    retry_editor_plan,
    save_series_editor,
)
from pullbox.services.series_metadata_links import (
    confirm_series_link,
    preview_series_link,
    read_series_links,
)
from pullbox.services.series_sidecar import prepare_series_sidecar, write_series_sidecar
from pullbox.utilities.models import JobType
from pullbox.utilities.router import _get_manager, _schedule_dispatch

router = APIRouter(prefix="/series", tags=["series"])
LocalId = Annotated[int, Path(gt=0, lt=2**63)]


@router.post("/{series_id}/metadata-editor/preview", response_model=SeriesMetadataEditorPreview)
async def editor_preview(
    series_id: LocalId,
    body: SeriesMetadataEditorReview,
    session: DbSession,
    _user: InteractiveOperatorUser,
) -> SeriesMetadataEditorPreview:
    try:
        return (await prepare_series_editor(session, series_id, body)).preview
    except (ValueError, ValidationError) as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/{series_id}/metadata-editor/write", status_code=202)
async def editor_write(
    series_id: LocalId,
    body: SeriesMetadataEditorApproval,
    session: DbSession,
    user: InteractiveOperatorUser,
) -> dict[str, object]:
    username = user.username
    try:
        plan = await save_series_editor(
            session,
            series_id,
            SeriesMetadataEditorReview(mode=body.mode, edits=body.edits),
            body.review_key,
        )
        manager = _get_manager()
        job = await manager.create_job(
            session,
            JobType.FILE_METADATA,
            f"Save series metadata: {plan.snapshot.values.title}",
            {"series_id": series_id, "series_editor": PLAN.dump_python(plan, mode="json")},
            created_by=username,
        )
        await session.commit()
        _schedule_dispatch(manager)
        return {"job_id": job.id, "state": job.state, "library_saved": True}
    except (ValueError, ValidationError) as exc:
        await session.rollback()
        raise HTTPException(409, str(exc)) from None


@router.get("/{series_id}/metadata-editor/job")
async def editor_job(
    series_id: LocalId,
    session: DbSession,
    _user: InteractiveOperatorUser,
) -> dict[str, object]:
    from pullbox.api.v1.issue_metadata_links import file_job_status

    job = await latest_editor_job(session, series_id)
    if job is None:
        return {"job": None}
    result = await file_job_status(session, job)
    details = await editor_job_details(session, job)
    projected = result["job"]
    if isinstance(projected, dict):
        projected["message"] = (
            "Library metadata saved. Series.json and comic files were left unchanged."
            if details["mode"] == "library_only"
            else "Library saved; unfinished sidecars need attention."
            if details["partial"] and job.is_terminal
            else "Library saved; writing approved sidecars."
            if not job.is_terminal
            else "Library saved; approved sidecar work finished."
        )
    return {**result, **details}


@router.post("/{series_id}/metadata-editor/retry", status_code=202)
async def editor_retry(
    series_id: LocalId,
    body: SeriesMetadataEditorRetry,
    session: DbSession,
    user: InteractiveOperatorUser,
) -> dict[str, object]:
    try:
        plan = await retry_editor_plan(session, series_id, body.job_id)
        manager = _get_manager()
        job = await manager.create_job(
            session,
            JobType.FILE_METADATA,
            f"Retry series sidecars: {plan.snapshot.values.title}",
            {"series_id": series_id, "series_editor": PLAN.dump_python(plan, mode="json")},
            created_by=user.username,
        )
        job.parent_job_id = body.job_id
        await session.commit()
        _schedule_dispatch(manager)
        return {"job_id": job.id, "state": job.state, "library_saved": True}
    except (ValueError, ValidationError) as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/{series_id}/sidecar/preview", response_model=SeriesSidecarPreview)
async def sidecar_preview(
    series_id: LocalId, session: DbSession, _user: InteractiveOperatorUser
) -> SeriesSidecarPreview:
    try:
        return (await prepare_series_sidecar(session, series_id)).preview
    except (ValueError, ValidationError):
        raise HTTPException(
            409,
            "Series metadata is not ready for file output. Finish metadata syncing "
            "and verify its provider links, then preview again.",
        ) from None


@router.post("/{series_id}/sidecar/write", response_model=SeriesSidecarWriteRead)
async def sidecar_write(
    series_id: LocalId,
    body: SeriesSidecarApproval,
    session: DbSession,
    _user: InteractiveOperatorUser,
) -> SeriesSidecarWriteRead:
    try:
        return await write_series_sidecar(session, series_id, body.review_key)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/{series_id}/metadata-links", response_model=SeriesLinksRead)
async def links(
    series_id: LocalId, session: DbSession, _user: InteractiveOperatorUser, settings: Settings
) -> SeriesLinksRead:
    return await read_series_links(
        session, series_id, gcd_api_enabled=settings.metadata_gcd_api_v2_enabled
    )


@router.post("/{series_id}/metadata-links/preview", response_model=SeriesLinkPreview)
async def preview(
    series_id: LocalId,
    body: SeriesPreviewQuery,
    session: DbSession,
    _user: InteractiveOperatorUser,
    settings: Settings,
) -> SeriesLinkPreview:
    try:
        return await preview_series_link(
            session, series_id, body, gcd_api_enabled=settings.metadata_gcd_api_v2_enabled
        )
    except (ValueError, ValidationError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{series_id}/metadata-links/confirm", response_model=IdentityReviewReceiptRead)
async def confirm(
    series_id: LocalId,
    body: SeriesLinkConfirm,
    session: DbSession,
    user: InteractiveOperatorUser,
    settings: Settings,
) -> IdentityReviewReceiptRead:
    try:
        return await confirm_series_link(
            session,
            series_id,
            body,
            user_id=user.id,
            gcd_api_enabled=settings.metadata_gcd_api_v2_enabled,
        )
    except (ValueError, ValidationError) as exc:
        raise HTTPException(409, str(exc)) from exc
