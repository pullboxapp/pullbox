"""Review-action response helpers for the import job API."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from pullbox.core.exceptions import ValidationError
from pullbox.models.import_job import ImportedFileStatus, ImportSeriesStatus
from pullbox.schemas.import_job import (
    ConflictGroupsResponse,
    ConflictResetRequest,
    ConflictResolveBulkRequest,
    ConflictResolveBulkResponse,
    ConflictResolveRequest,
    FileConflictGroup,
    FileMatchUpdateRequest,
    FileMetadataRepairRequest,
    FileSelectionBulkUpdateRequest,
    FileSelectionUpdateRequest,
    ImportedFileRead,
    ImportedFilesResponse,
    ImportedSeriesRead,
    ImportReviewSelectionState,
    ImportSelectionBulkUpdateResponse,
    SeriesSelectionBulkUpdateRequest,
    SeriesSelectionUpdateRequest,
    StoryArcPolicyConfirmationRequest,
    StoryArcPolicyConfirmationResponse,
    StoryArcReviewDecisionRequest,
    StoryArcReviewDecisionResponse,
)
from pullbox.services.import_story_arc_policy_confirmation import (
    confirm_import_story_arc_policy,
)
from pullbox.services.story_arc_placement_integration import StoryArcPlacementPolicyInput


@dataclass(frozen=True)
class SeriesMutationResult:
    """Result for series mutations that may require a background rematch."""

    payload: ImportedSeriesRead
    rematch_series_id: int | None = None


def _raise_validation_http(exc: ValidationError, *, conflict_action: bool = False) -> None:
    status = 409
    if conflict_action and "REVIEW" not in exc.message:
        status = 400
    raise HTTPException(status_code=status, detail=exc.message) from exc


def _build_file_conflict_group(group_id: int, files: list[Any]) -> FileConflictGroup:
    return FileConflictGroup(
        conflict_group_id=group_id,
        matched_issue_id=files[0].matched_issue_id or 0,
        files=[ImportedFileRead.model_validate(f) for f in files],
    )


async def list_import_files_response(
    service: Any,
    session: Any,
    job_id: int,
    series_id: int,
    *,
    status: str | None,
    page: int,
    page_size: int,
) -> ImportedFilesResponse:
    """Return paginated file rows for one import-review series."""
    status_filter: ImportedFileStatus | None = None
    if status:
        with contextlib.suppress(ValueError):
            status_filter = ImportedFileStatus(status)

    files, total = await service.get_files_for_series(
        session,
        job_id,
        series_id,
        status_filter=status_filter,
        page=page,
        page_size=page_size,
    )

    return ImportedFilesResponse(
        items=[ImportedFileRead.model_validate(f) for f in files],
        total=total,
        page=page,
        page_size=page_size,
    )


async def list_conflicts_response(
    service: Any,
    session: Any,
    job_id: int,
) -> ConflictGroupsResponse:
    """Return all conflict groups for an import job."""
    groups = await service.get_conflict_groups(session, job_id)
    return ConflictGroupsResponse(
        job_id=job_id,
        groups=[
            FileConflictGroup(
                conflict_group_id=g["conflict_group_id"],
                matched_issue_id=g["matched_issue_id"],
                files=[ImportedFileRead.model_validate(f) for f in g["files"]],
            )
            for g in groups
        ],
        total=len(groups),
    )


async def override_file_match_response(
    service: Any,
    session: Any,
    job_id: int,
    file_id: int,
    body: FileMatchUpdateRequest,
) -> ImportedFileRead:
    """Persist a manual file-to-issue match override."""
    try:
        imp_file = await service.override_file_match(
            session,
            job_id,
            file_id,
            body.issue_id,
            repair_source_metadata=body.repair_source_metadata,
        )
    except ValidationError as exc:
        _raise_validation_http(exc)
    return ImportedFileRead.model_validate(imp_file)


async def repair_file_metadata_response(
    service: Any,
    session: Any,
    job_id: int,
    file_id: int,
    body: FileMetadataRepairRequest | None,
) -> ImportedFileRead:
    """Persist a file metadata repair request."""
    try:
        imp_file = await service.repair_file_metadata(
            session,
            job_id,
            file_id,
            issue_id=body.issue_id if body is not None else None,
        )
    except ValidationError as exc:
        _raise_validation_http(exc)
    return ImportedFileRead.model_validate(imp_file)


async def update_series_selection_response(
    service: Any,
    session: Any,
    job_id: int,
    imported_series_id: int,
    body: SeriesSelectionUpdateRequest,
) -> ImportedSeriesRead:
    """Persist include/exclude state for one matched review series."""
    try:
        imported_series = await service.update_series_selection(
            session,
            job_id,
            imported_series_id,
            include_in_import=body.include_in_import,
        )
    except ValidationError as exc:
        _raise_validation_http(exc)
    return ImportedSeriesRead.model_validate(imported_series)


async def update_story_arc_decision_response(
    service: Any,
    session: Any,
    job_id: int,
    imported_story_arc_id: int,
    body: StoryArcReviewDecisionRequest,
) -> StoryArcReviewDecisionResponse:
    """Persist one explicit staged story-arc review decision."""
    try:
        staged_arc = await service.update_story_arc_decision(
            session,
            job_id,
            imported_story_arc_id,
            action=body.action,
            proposed_story_arc_id=body.proposed_story_arc_id,
        )
    except ValidationError as exc:
        _raise_validation_http(exc)
    return StoryArcReviewDecisionResponse(
        imported_story_arc_id=int(staged_arc.id),
        status=staged_arc.status.value,
        selected_for_import=bool(staged_arc.selected_for_import),
        proposed_story_arc_id=staged_arc.proposed_story_arc_id,
    )


async def confirm_story_arc_policy_response(
    session: Any,
    job_id: int,
    imported_story_arc_id: int,
    body: StoryArcPolicyConfirmationRequest,
) -> StoryArcPolicyConfirmationResponse:
    """Validate and persist one explicitly confirmed staged arc policy."""
    payload = body.placement_policy
    try:
        result = await confirm_import_story_arc_policy(
            session,
            job_id=job_id,
            imported_story_arc_id=imported_story_arc_id,
            expected_policy_digest=body.expected_policy_digest,
            explicit_confirmation=body.confirm_policy,
            materialize_filesystem=body.materialize_filesystem,
            monitored=body.monitored,
            search_missing=body.search_missing,
            include_upcoming=body.include_upcoming,
            placement_policy=StoryArcPlacementPolicyInput(
                mode=payload.mode,
                target_library_root_id=payload.target_library_root_id,
                destination_root=payload.destination_root,
                folder_template=payload.folder_template,
                file_template=payload.file_template,
                symlink_style=payload.symlink_style,
                synchronize=payload.synchronize,
            ),
        )
    except ValidationError as exc:
        _raise_validation_http(exc)
    return StoryArcPolicyConfirmationResponse.model_validate(result, from_attributes=True)


async def get_selection_state_response(
    service: Any,
    session: Any,
    job_id: int,
) -> ImportReviewSelectionState:
    """Return canonical Step 3 selection state."""
    selection_state = await service.get_review_selection_state(session, job_id)
    return ImportReviewSelectionState.model_validate(selection_state)


async def unmatch_series_match_response(
    service: Any,
    session: Any,
    job_id: int,
    imported_series_id: int,
) -> ImportedSeriesRead:
    """Reject an incorrect Step 3 ComicVine/library match."""
    try:
        imported_series = await service.unmatch_series_match(session, job_id, imported_series_id)
    except ValidationError as exc:
        _raise_validation_http(exc)
    return ImportedSeriesRead.model_validate(imported_series)


async def unmatch_duplicate_series_response(
    service: Any,
    session: Any,
    job_id: int,
    imported_series_id: int,
) -> ImportedSeriesRead:
    """Reject an incorrect existing-library duplicate match."""
    try:
        imported_series = await service.unmatch_duplicate_series(
            session,
            job_id,
            imported_series_id,
        )
    except ValidationError as exc:
        _raise_validation_http(exc)
    return ImportedSeriesRead.model_validate(imported_series)


async def allow_safety_blocked_file_once_response(
    service: Any,
    session: Any,
    job_id: int,
    file_id: int,
) -> SeriesMutationResult:
    """Allow one safety-blocked import file to re-enter normal matching."""
    try:
        imported_series = await service.allow_safety_blocked_file_once(session, job_id, file_id)
    except ValidationError as exc:
        _raise_validation_http(exc)

    should_rematch = imported_series.status in {
        ImportSeriesStatus.MATCHED,
        ImportSeriesStatus.DUPLICATE,
    }
    return SeriesMutationResult(
        payload=ImportedSeriesRead.model_validate(imported_series),
        rematch_series_id=imported_series.id if should_rematch else None,
    )


async def skip_safety_blocked_file_response(
    service: Any,
    session: Any,
    job_id: int,
    file_id: int,
) -> ImportedSeriesRead:
    """Skip one safety-blocked import file from active review."""
    try:
        imported_series = await service.skip_safety_blocked_file(session, job_id, file_id)
    except ValidationError as exc:
        _raise_validation_http(exc)
    return ImportedSeriesRead.model_validate(imported_series)


async def bulk_update_series_selection_response(
    service: Any,
    session: Any,
    job_id: int,
    body: SeriesSelectionBulkUpdateRequest,
) -> ImportSelectionBulkUpdateResponse:
    """Persist include/exclude state for matched review series in scope."""
    try:
        updated = await service.bulk_update_series_selection(
            session,
            job_id,
            include_in_import=body.include_in_import,
            imported_series_ids=body.imported_series_ids,
        )
    except ValidationError as exc:
        _raise_validation_http(exc)

    selection_state = await service.get_review_selection_state(session, job_id)
    return ImportSelectionBulkUpdateResponse(
        updated=updated,
        include_in_import=body.include_in_import,
        selection_state=ImportReviewSelectionState.model_validate(selection_state),
    )


async def update_file_selection_response(
    service: Any,
    session: Any,
    job_id: int,
    file_id: int,
    body: FileSelectionUpdateRequest,
) -> ImportedFileRead:
    """Persist include/exclude state for a duplicate-series review file."""
    try:
        imp_file = await service.update_file_selection(
            session,
            job_id,
            file_id,
            include_in_import=body.include_in_import,
        )
    except ValidationError as exc:
        _raise_validation_http(exc)
    return ImportedFileRead.model_validate(imp_file)


async def bulk_update_file_selection_response(
    service: Any,
    session: Any,
    job_id: int,
    body: FileSelectionBulkUpdateRequest,
) -> ImportSelectionBulkUpdateResponse:
    """Persist include/exclude state for duplicate-series files in scope."""
    try:
        updated = await service.bulk_update_file_selection(
            session,
            job_id,
            include_in_import=body.include_in_import,
            imported_series_id=body.imported_series_id,
        )
    except ValidationError as exc:
        _raise_validation_http(exc)

    selection_state = await service.get_review_selection_state(session, job_id)
    return ImportSelectionBulkUpdateResponse(
        updated=updated,
        include_in_import=body.include_in_import,
        selection_state=ImportReviewSelectionState.model_validate(selection_state),
    )


async def resolve_conflict_response(
    service: Any,
    session: Any,
    job_id: int,
    group_id: int,
    body: ConflictResolveRequest,
) -> FileConflictGroup:
    """Resolve one conflict group and return the updated group."""
    try:
        files = await service.resolve_conflict(session, job_id, group_id, body.chosen_file_id)
    except ValidationError as exc:
        _raise_validation_http(exc, conflict_action=True)
    return _build_file_conflict_group(group_id, files)


async def resolve_conflicts_bulk_response(
    service: Any,
    session: Any,
    job_id: int,
    body: ConflictResolveBulkRequest,
) -> ConflictResolveBulkResponse:
    """Resolve multiple conflict groups in one request."""
    try:
        resolved_group_ids, resolved_series_ids = await service.resolve_conflicts(
            session,
            job_id,
            [(item.conflict_group_id, item.chosen_file_id) for item in body.resolutions],
        )
    except ValidationError as exc:
        _raise_validation_http(exc, conflict_action=True)
    return ConflictResolveBulkResponse(
        resolved_group_ids=resolved_group_ids,
        resolved_series_ids=resolved_series_ids,
        resolved_count=len(resolved_group_ids),
    )


async def reset_conflicts_response(
    service: Any,
    session: Any,
    job_id: int,
    body: ConflictResetRequest | None,
) -> dict[str, object]:
    """Reset resolved conflict groups back to review state."""
    try:
        group_ids = body.group_ids if body is not None else []
        reset_group_ids, reset_series_ids = await service.reset_conflicts(
            session,
            job_id,
            group_ids,
        )
    except ValidationError as exc:
        _raise_validation_http(exc, conflict_action=True)
    return {
        "reset_group_ids": reset_group_ids,
        "reset_series_ids": reset_series_ids,
        "reset_count": len(reset_group_ids),
    }
