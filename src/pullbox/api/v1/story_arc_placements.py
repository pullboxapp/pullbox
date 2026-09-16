"""Authenticated story-arc placement policy and synchronization routes."""

from __future__ import annotations

from typing import Annotated, NoReturn

from fastapi import APIRouter, HTTPException, Query

from pullbox.api.deps import AuthenticatedUser, DbSession  # noqa: TC001
from pullbox.schemas.pagination import PaginatedResponse
from pullbox.schemas.story_arc_placement import (
    StoryArcPlacementPolicyPayload,
    StoryArcPlacementPolicyResponse,
    StoryArcPlacementPolicyUpdate,
    StoryArcPlacementPreviewItemResponse,
    StoryArcPlacementPreviewPageResponse,
    StoryArcPlacementRemovalResponse,
    StoryArcPlacementResponse,
    StoryArcPlacementSyncRequest,
    StoryArcPlacementSyncResponse,
    StoryArcPolicyMigrationConfirmationRequest,
    StoryArcPolicyMigrationConfirmationResponse,
    StoryArcPolicyMigrationPreviewItemResponse,
    StoryArcPolicyMigrationPreviewRequest,
    StoryArcPolicyMigrationPreviewResponse,
)
from pullbox.services.story_arc_placement_integration import (
    StoryArcPlacementIntegrationError,
    StoryArcPlacementPolicy,
    StoryArcPlacementPolicyInput,
    StoryArcPlacementRemovalView,
    StoryArcPlacementSyncResult,
    StoryArcPlacementSyncService,
)
from pullbox.services.story_arc_policy_migration import (
    StoryArcPolicyMigrationService,
)

router = APIRouter(prefix="/story-arcs", tags=["story-arc-placements"])

_service = StoryArcPlacementSyncService()
_migration_service = StoryArcPolicyMigrationService()


def _proposal(body: StoryArcPlacementPolicyPayload) -> StoryArcPlacementPolicyInput:
    return StoryArcPlacementPolicyInput(
        mode=body.mode,
        target_library_root_id=body.target_library_root_id,
        destination_root=body.destination_root,
        folder_template=body.folder_template,
        file_template=body.file_template,
        symlink_style=body.symlink_style,
        synchronize=body.synchronize,
    )


def _policy_response(policy: StoryArcPlacementPolicy) -> StoryArcPlacementPolicyResponse:
    return StoryArcPlacementPolicyResponse(
        configured=policy.configured,
        revision=policy.revision,
        mode=policy.mode,
        target_library_root_id=policy.target_library_root_id,
        destination_root=policy.destination_root,
        folder_template=policy.folder_template,
        file_template=policy.file_template,
        symlink_style=policy.symlink_style,
        synchronize=policy.synchronize,
        snapshot=policy.snapshot,
    )


def _sync_response(result: StoryArcPlacementSyncResult) -> StoryArcPlacementSyncResponse:
    return StoryArcPlacementSyncResponse(
        membership_id=result.membership_id,
        outcome=result.outcome,
        placement=(
            StoryArcPlacementResponse.model_validate(result.placement)
            if result.placement is not None
            else None
        ),
    )


def _removal_response(
    result: StoryArcPlacementRemovalView,
) -> StoryArcPlacementRemovalResponse:
    return StoryArcPlacementRemovalResponse(
        placement_id=result.placement_id,
        ownership=result.ownership,
        artifact_removed=result.artifact_removed,
        canonical_preserved=result.canonical_preserved,
        referenced_artifact_preserved=result.referenced_artifact_preserved,
        automatic_sync_disabled=result.automatic_sync_disabled,
    )


def _raise_integration_error(exc: StoryArcPlacementIntegrationError) -> NoReturn:
    status_code = (
        404
        if exc.category == "not_found"
        else 409
        if exc.category in {"conflict", "collision", "ownership", "cancelled"}
        else 422
    )
    raise HTTPException(
        status_code=status_code,
        detail={
            "code": exc.code,
            "category": exc.category,
            "message": str(exc),
        },
    ) from exc


@router.get(
    "/{story_arc_id}/placement-policy",
    response_model=StoryArcPlacementPolicyResponse,
)
async def get_story_arc_placement_policy(
    story_arc_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> StoryArcPlacementPolicyResponse:
    """Read the complete effective policy without touching the filesystem."""
    try:
        return _policy_response(await _service.get_policy(session, story_arc_id))
    except StoryArcPlacementIntegrationError as exc:
        _raise_integration_error(exc)


@router.post(
    "/{story_arc_id}/placement-policy/preview",
    response_model=StoryArcPlacementPreviewPageResponse,
)
async def preview_story_arc_placement_policy(
    story_arc_id: int,
    body: StoryArcPlacementPolicyPayload,
    _user: AuthenticatedUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> StoryArcPlacementPreviewPageResponse:
    """Validate and preview a complete candidate policy without persisting it."""
    proposal = _proposal(body)
    try:
        policy = await _service.validate_policy(session, story_arc_id, proposal)
        page = await _service.preview_arc(
            session,
            story_arc_id,
            limit=limit,
            offset=offset,
            proposal=proposal,
        )
    except StoryArcPlacementIntegrationError as exc:
        _raise_integration_error(exc)
    return StoryArcPlacementPreviewPageResponse(
        policy=_policy_response(policy),
        items=[StoryArcPlacementPreviewItemResponse.model_validate(item) for item in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
        has_more=page.has_more,
    )


@router.post(
    "/{story_arc_id}/placement-policy/migration-preview",
    response_model=StoryArcPolicyMigrationPreviewResponse,
)
async def preview_story_arc_policy_migration(
    story_arc_id: int,
    body: StoryArcPolicyMigrationPreviewRequest,
    user: AuthenticatedUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    after_placement_id: Annotated[int, Query(ge=0)] = 0,
) -> StoryArcPolicyMigrationPreviewResponse:
    """Preview every policy consequence while returning one bounded path page."""
    try:
        actor_id = int(user.id)
        # Authentication may update API-key audit/upgrade fields. Persist that
        # short transaction before the preview service begins rollback-only scans.
        await session.commit()
        preview = await _migration_service.preview_policy_change(
            session,
            story_arc_id,
            actor_id=actor_id,
            expected_revision=body.expected_revision,
            proposal=_proposal(body),
            limit=limit,
            after_placement_id=after_placement_id,
        )
    except StoryArcPlacementIntegrationError as exc:
        _raise_integration_error(exc)
    return StoryArcPolicyMigrationPreviewResponse(
        story_arc_id=preview.story_arc_id,
        expected_revision=preview.expected_revision,
        current_policy=_policy_response(preview.current_policy),
        proposed_policy=_policy_response(preview.proposed_policy),
        scope_digest=preview.scope_digest,
        preview_token=preview.preview_token,
        required_confirmation=preview.required_confirmation,
        total_placement_count=preview.total_placement_count,
        managed_migrate_count=preview.managed_migrate_count,
        managed_remove_count=preview.managed_remove_count,
        managed_unchanged_count=preview.managed_unchanged_count,
        referenced_preserved_count=preview.referenced_preserved_count,
        collision_count=preview.collision_count,
        blocked_count=preview.blocked_count,
        required_bytes=preview.required_bytes,
        available_bytes=preview.available_bytes,
        global_block_codes=list(preview.global_block_codes),
        items=[
            StoryArcPolicyMigrationPreviewItemResponse.model_validate(item)
            for item in preview.items
        ],
        limit=preview.limit,
        after_placement_id=preview.after_placement_id,
        next_cursor=preview.next_cursor,
        has_more=preview.has_more,
        requires_confirmation=preview.requires_confirmation,
        execution_supported=preview.execution_supported,
        filesystem_mutated=preview.filesystem_mutated,
    )


@router.post(
    "/{story_arc_id}/placement-policy/migration-confirmation",
    response_model=StoryArcPolicyMigrationConfirmationResponse,
)
async def prepare_story_arc_policy_migration_confirmation(
    story_arc_id: int,
    body: StoryArcPolicyMigrationConfirmationRequest,
    user: AuthenticatedUser,
    session: DbSession,
) -> StoryArcPolicyMigrationConfirmationResponse:
    """Validate signed, exact actor intent without mutating policy or files."""
    try:
        actor_id = int(user.id)
        await session.commit()
        confirmation = await _migration_service.prepare_confirmation(
            session,
            story_arc_id,
            actor_id=actor_id,
            expected_revision=body.expected_revision,
            proposal=_proposal(body),
            preview_token=body.preview_token,
            confirmation=body.confirmation,
        )
    except StoryArcPlacementIntegrationError as exc:
        _raise_integration_error(exc)
    return StoryArcPolicyMigrationConfirmationResponse(
        story_arc_id=confirmation.story_arc_id,
        expected_revision=confirmation.expected_revision,
        scope_digest=confirmation.scope_digest,
        confirmed=confirmation.confirmed,
        ready_for_execution=confirmation.ready_for_execution,
        execution_supported=confirmation.execution_supported,
        mutation_performed=confirmation.mutation_performed,
        policy_update_block_code=confirmation.policy_update_block_code,
    )


@router.put(
    "/{story_arc_id}/placement-policy",
    response_model=StoryArcPlacementPolicyResponse,
)
async def update_story_arc_placement_policy(
    story_arc_id: int,
    body: StoryArcPlacementPolicyUpdate,
    _user: AuthenticatedUser,
    session: DbSession,
) -> StoryArcPlacementPolicyResponse:
    """Freeze one complete policy with the arc's optimistic revision token."""
    try:
        policy = await _service.update_policy(
            session,
            story_arc_id,
            expected_revision=body.expected_revision,
            proposal=_proposal(body),
        )
    except StoryArcPlacementIntegrationError as exc:
        _raise_integration_error(exc)
    return _policy_response(policy)


@router.get(
    "/{story_arc_id}/placements",
    response_model=PaginatedResponse[StoryArcPlacementResponse],
)
async def list_story_arc_placements(
    story_arc_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PaginatedResponse[StoryArcPlacementResponse]:
    """List bounded placement ownership and synchronization state."""
    try:
        page = await _service.list_placements(
            session,
            story_arc_id,
            limit=limit,
            offset=offset,
        )
    except StoryArcPlacementIntegrationError as exc:
        _raise_integration_error(exc)
    return PaginatedResponse[StoryArcPlacementResponse](
        items=[StoryArcPlacementResponse.model_validate(item) for item in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
        has_more=page.has_more,
    )


@router.post(
    "/{story_arc_id}/memberships/{membership_id}/placement-sync",
    response_model=StoryArcPlacementSyncResponse,
)
async def sync_story_arc_membership_placement(
    story_arc_id: int,
    membership_id: int,
    body: StoryArcPlacementSyncRequest,
    _user: AuthenticatedUser,
    session: DbSession,
) -> StoryArcPlacementSyncResponse:
    """Synchronize one resolved membership after canonical registration."""
    try:
        result = await _service.sync_membership(
            session,
            story_arc_id,
            membership_id,
            adopt_identical_existing=body.adopt_identical_existing,
        )
    except StoryArcPlacementIntegrationError as exc:
        _raise_integration_error(exc)
    return _sync_response(result)


@router.post(
    "/{story_arc_id}/placements/{placement_id}/retry",
    response_model=StoryArcPlacementSyncResponse,
)
async def retry_story_arc_placement(
    story_arc_id: int,
    placement_id: int,
    body: StoryArcPlacementSyncRequest,
    _user: AuthenticatedUser,
    session: DbSession,
) -> StoryArcPlacementSyncResponse:
    """Retry one failed or interrupted placement idempotently."""
    try:
        result = await _service.retry_placement(
            session,
            story_arc_id,
            placement_id,
            adopt_identical_existing=body.adopt_identical_existing,
        )
    except StoryArcPlacementIntegrationError as exc:
        _raise_integration_error(exc)
    return _sync_response(result)


@router.post(
    "/{story_arc_id}/placements/{placement_id}/repair",
    response_model=StoryArcPlacementSyncResponse,
)
async def repair_story_arc_placement(
    story_arc_id: int,
    placement_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> StoryArcPlacementSyncResponse:
    """Repair only a safely evidenced Pullbox-managed placement."""
    try:
        result = await _service.repair_placement(session, story_arc_id, placement_id)
    except StoryArcPlacementIntegrationError as exc:
        _raise_integration_error(exc)
    return _sync_response(result)


@router.delete(
    "/{story_arc_id}/placements/{placement_id}",
    response_model=StoryArcPlacementRemovalResponse,
)
async def remove_story_arc_placement(
    story_arc_id: int,
    placement_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
    confirm_managed_artifact_removal: bool = False,
) -> StoryArcPlacementRemovalResponse:
    """Remove managed artifacts safely or forget referenced evidence without unlinking it."""
    try:
        result = await _service.remove_placement(
            session,
            story_arc_id,
            placement_id,
            confirm_managed_artifact_removal=confirm_managed_artifact_removal,
        )
    except StoryArcPlacementIntegrationError as exc:
        _raise_integration_error(exc)
    return _removal_response(result)
