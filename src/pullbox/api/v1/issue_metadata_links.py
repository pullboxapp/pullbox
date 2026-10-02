"""Operator review and descriptive refresh for existing issues."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path
from sqlalchemy import JSON, Text, cast, select, type_coerce

from pullbox.api.deps import DbSession, InteractiveOperatorUser, Settings
from pullbox.core.exceptions import ValidationError
from pullbox.models.operation_progress import OperationProgress, OperationProgressType
from pullbox.schemas.issue_file_metadata import (
    FileMetadataApproval,
    FileMetadataPreview,
    FileMetadataReview,
)
from pullbox.schemas.issue_metadata_links import (
    IssueCandidatesQuery,
    IssueLinkPreview,
    IssueLinkQuery,
    IssueLinksRead,
    IssueRefreshRead,
)
from pullbox.schemas.metadata_identity_review import IdentityReviewReceiptRead
from pullbox.schemas.metadata_sources import MetadataFetch, MetadataPage, ProviderIssueRead
from pullbox.schemas.series_metadata_links import SeriesLinkConfirm
from pullbox.services.archive_metadata_publication import ArchivePublicationError
from pullbox.services.issue_file_metadata import file_metadata_error, prepare_file_metadata
from pullbox.services.issue_metadata_links import (
    confirm_issue_link,
    issue_link_candidates,
    preview_issue_link,
    read_issue_links,
)
from pullbox.services.metadata_issue_refresh import refresh_issue_from_sources
from pullbox.utilities.import_guards import ensure_no_active_import_file_mutation
from pullbox.utilities.models import JobType, UtilityJob, UtilityJobItem
from pullbox.utilities.router import _get_manager, _schedule_dispatch

router = APIRouter(prefix="/issues", tags=["issues"])
LocalId = Annotated[int, Path(gt=0, lt=2**63)]


@router.get("/{issue_id}/metadata-links", response_model=IssueLinksRead)
async def links(
    issue_id: LocalId, session: DbSession, _user: InteractiveOperatorUser, settings: Settings
) -> IssueLinksRead:
    try:
        return await read_issue_links(
            session, issue_id, gcd_api_enabled=settings.metadata_gcd_api_v2_enabled
        )
    except (ValueError, ValidationError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post(
    "/{issue_id}/metadata-links/candidates",
    response_model=MetadataFetch[MetadataPage[ProviderIssueRead]],
)
async def candidates(
    issue_id: LocalId,
    body: IssueCandidatesQuery,
    session: DbSession,
    _user: InteractiveOperatorUser,
    settings: Settings,
) -> MetadataFetch[MetadataPage[ProviderIssueRead]]:
    try:
        return await issue_link_candidates(
            session, issue_id, body, gcd_api_enabled=settings.metadata_gcd_api_v2_enabled
        )
    except (ValueError, ValidationError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{issue_id}/metadata-links/preview", response_model=IssueLinkPreview)
async def preview(
    issue_id: LocalId,
    body: IssueLinkQuery,
    session: DbSession,
    _user: InteractiveOperatorUser,
    settings: Settings,
) -> IssueLinkPreview:
    try:
        return await preview_issue_link(
            session, issue_id, body, gcd_api_enabled=settings.metadata_gcd_api_v2_enabled
        )
    except (ValueError, ValidationError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{issue_id}/metadata-links/confirm", response_model=IdentityReviewReceiptRead)
async def confirm(
    issue_id: LocalId,
    body: SeriesLinkConfirm,
    session: DbSession,
    user: InteractiveOperatorUser,
    settings: Settings,
) -> IdentityReviewReceiptRead:
    try:
        return await confirm_issue_link(
            session,
            issue_id,
            body,
            user_id=user.id,
            gcd_api_enabled=settings.metadata_gcd_api_v2_enabled,
        )
    except (ValueError, ValidationError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{issue_id}/refresh-metadata", response_model=IssueRefreshRead)
async def refresh(
    issue_id: LocalId, session: DbSession, _user: InteractiveOperatorUser, settings: Settings
) -> IssueRefreshRead:
    try:
        return await refresh_issue_from_sources(
            session, issue_id, gcd_api_enabled=settings.metadata_gcd_api_v2_enabled
        )
    except (ValueError, ValidationError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{issue_id}/file-metadata/preview", response_model=FileMetadataPreview)
async def file_preview(
    issue_id: LocalId,
    session: DbSession,
    _user: InteractiveOperatorUser,
    body: FileMetadataReview | None = None,
) -> FileMetadataPreview:
    try:
        return (
            await prepare_file_metadata(session, issue_id, choices=body.choices if body else None)
        ).preview
    except (ValueError, OSError) as exc:
        raise HTTPException(409, file_metadata_error(exc)) from exc


@router.post("/{issue_id}/file-metadata/write", status_code=202)
async def file_write(
    issue_id: LocalId, body: FileMetadataApproval, session: DbSession, user: InteractiveOperatorUser
) -> dict[str, str]:
    try:
        prepared = await prepare_file_metadata(session, issue_id, choices=body.choices)
        if not prepared.preview.ready:
            raise ArchivePublicationError("unresolved_conflicts")
        if prepared.preview.review_key != body.review_key:
            raise ArchivePublicationError("approval_changed")
        await ensure_no_active_import_file_mutation(session)
        manager = _get_manager()
        action = (
            "Convert and write metadata" if prepared.preview.converts_to_cbz else "Write metadata"
        )
        job = await manager.create_job(
            session,
            JobType.FILE_METADATA,
            f"{action}: {prepared.preview.file_name}",
            {"issue_id": issue_id, "review_key": body.review_key, "choices": body.choices},
            created_by=user.username,
        )
        await session.commit()
        _schedule_dispatch(manager)
        return {"job_id": job.id, "state": job.state}
    except (ValueError, OSError) as exc:
        raise HTTPException(409, file_metadata_error(exc)) from exc


@router.get("/{issue_id}/file-metadata/job")
async def file_job(
    issue_id: LocalId, session: DbSession, _user: InteractiveOperatorUser
) -> dict[str, object]:
    document = JSON().with_variant(Text(), "sqlite")
    job = await session.scalar(
        select(UtilityJob)
        .where(
            UtilityJob.job_type == JobType.FILE_METADATA,
            type_coerce(cast(UtilityJob.config, document), JSON)["issue_id"].as_integer()
            == issue_id,
        )
        .order_by(UtilityJob.created_at.desc(), UtilityJob.id.desc())
        .limit(1)
    )
    if job is None:
        return {"job": None}
    item = await session.scalar(
        select(UtilityJobItem).where(UtilityJobItem.job_id == job.id).limit(1)
    )
    progress = await session.scalar(
        select(OperationProgress).where(
            OperationProgress.operation_type == OperationProgressType.UTILITY,
            OperationProgress.operation_key == job.id,
        )
    )
    return {
        "job": {
            "id": job.id,
            "state": job.state,
            "percent": progress.overall_percent if progress else job.progress_pct,
            "message": progress.message if progress else job.state,
            "error": (item.error_message if item else None) or job.error_message,
        }
    }
