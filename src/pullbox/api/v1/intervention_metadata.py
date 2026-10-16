"""Explicit retained-file review; no provider requests or acquisition replanning."""

import json

from fastapi import APIRouter, HTTPException
from sqlalchemy import JSON, Text, cast, select, type_coerce

from pullbox.api.deps import DbSession, InteractiveOperatorUser
from pullbox.api.v1.issue_metadata_links import LocalId, file_job_status
from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
from pullbox.schemas.issue_file_metadata import FileMetadataApproval, FileMetadataReview
from pullbox.services.direct_metadata_recovery import (
    DirectMetadataRecoveryError,
    DirectMetadataRecoveryPreview,
    prepare_direct_metadata_recovery,
)
from pullbox.services.issue_file_metadata import file_metadata_error
from pullbox.services.library_mutation_coordination import lock_file_mutation_admission
from pullbox.utilities.import_guards import ensure_no_active_import_file_mutation
from pullbox.utilities.models import JobState, JobType, UtilityJob
from pullbox.utilities.router import _get_manager, _schedule_dispatch

router = APIRouter(prefix="/intervention", tags=["intervention"])


def _error(exc: ValueError | OSError) -> HTTPException:
    return HTTPException(
        409, str(exc) if isinstance(exc, DirectMetadataRecoveryError) else file_metadata_error(exc)
    )


@router.post("/{pending_id}/file-metadata/preview", response_model=DirectMetadataRecoveryPreview)
async def preview(
    pending_id: LocalId,
    session: DbSession,
    _user: InteractiveOperatorUser,
    body: FileMetadataReview | None = None,
) -> DirectMetadataRecoveryPreview:
    try:
        return (
            await prepare_direct_metadata_recovery(
                session, pending_id, choices=body.choices if body else None
            )
        ).preview
    except (ValueError, OSError) as exc:
        raise _error(exc) from exc


@router.post("/{pending_id}/file-metadata/write", status_code=202)
async def write(
    pending_id: LocalId,
    body: FileMetadataApproval,
    session: DbSession,
    user: InteractiveOperatorUser,
) -> dict[str, str]:
    try:
        prepared = await prepare_direct_metadata_recovery(session, pending_id, choices=body.choices)
        if not prepared.preview.ready or prepared.preview.review_key != body.review_key:
            raise DirectMetadataRecoveryError(
                "Review each conflicting field and update the preview before recovering this file."
            )
        await lock_file_mutation_admission(session)
        await ensure_no_active_import_file_mutation(session)
        document = JSON().with_variant(Text(), "sqlite")
        existing = await session.scalar(
            select(UtilityJob)
            .where(
                UtilityJob.job_type == JobType.FILE_METADATA,
                UtilityJob.state.in_(
                    (
                        JobState.QUEUED,
                        JobState.RUNNING,
                        JobState.PAUSING,
                        JobState.PAUSED,
                        JobState.CANCELLING,
                    )
                ),
                type_coerce(cast(UtilityJob.config, document), JSON)["issue_id"].as_integer()
                == prepared.owner.issue_id,
            )
            .limit(1)
        )
        if existing is not None:
            config = json.loads(existing.config)
            if (
                config.get("recovery_pending_id") != pending_id
                or config.get("review_key") != body.review_key
            ):
                raise DirectMetadataRecoveryError(
                    "A metadata job already owns this issue. "
                    "Wait for it to finish or cancel it before retrying."
                )
            return {"job_id": existing.id, "state": existing.state}
        manager = _get_manager()
        job = await manager.create_job(
            session,
            JobType.FILE_METADATA,
            f"Recover file metadata: {prepared.preview.file_name}",
            {
                "issue_id": prepared.owner.issue_id,
                "recovery_pending_id": pending_id,
                "review_key": body.review_key,
                "choices": body.choices,
            },
            created_by=user.username,
        )
        await session.commit()
        _schedule_dispatch(manager)
        return {"job_id": job.id, "state": job.state}
    except (ValueError, OSError) as exc:
        raise _error(exc) from exc


@router.get("/{pending_id}/file-metadata/job")
async def job(
    pending_id: LocalId, session: DbSession, _user: InteractiveOperatorUser
) -> dict[str, object]:
    document = JSON().with_variant(Text(), "sqlite")
    row = await session.scalar(
        select(UtilityJob)
        .where(
            UtilityJob.job_type == JobType.FILE_METADATA,
            type_coerce(cast(UtilityJob.config, document), JSON)["recovery_pending_id"].as_integer()
            == pending_id,
        )
        .order_by(UtilityJob.created_at.desc(), UtilityJob.id.desc())
        .limit(1)
    )
    pending = await session.get(PendingMatch, pending_id)
    result: dict[str, object] = await file_job_status(session, row) if row else {"job": None}
    result["resolved"] = bool(
        pending
        and pending.status == PendingMatchStatus.APPROVED
        and (pending.match_details or {}).get("resolution_reason")
        in {"metadata_completed", "metadata_superseded"}
    )
    return result
