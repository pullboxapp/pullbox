"""Import rollback journal action helpers."""

from __future__ import annotations

import contextlib
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import func as sa_func
from sqlalchemy import select as sa_select

from pullbox.core.exceptions import NotFoundError
from pullbox.models.import_job import (
    ImportedFile,
    ImportJob,
    ImportJobAction,
    ImportJobActionStatus,
)
from pullbox.models.library import LibraryFile
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcExternalIdentity,
    StoryArcPlacement,
    StoryArcPlacementMode,
    StoryArcPlacementOwnership,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.utilities.settings import restore_file_from_utility_trash

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_ACTION_SEQUENCE_CACHE_KEY = "pullbox.import_action_last_sequence"


class DeleteSeriesForRollback(Protocol):
    """Callable used to remove a series created by an import action."""

    async def __call__(
        self,
        session: AsyncSession,
        series_id: int,
        *,
        delete_files: bool,
        delete_folder: bool,
    ) -> None: ...


async def next_action_sequence(session: AsyncSession, job_id: int) -> int:
    """Return the next durable action sequence number for a job."""
    max_seq = await session.scalar(
        sa_select(sa_func.max(ImportJobAction.sequence_no)).where(
            ImportJobAction.import_job_id == job_id
        )
    )
    return int(max_seq or 0) + 1


async def record_action(
    session: AsyncSession,
    job: ImportJob,
    *,
    phase: str,
    action_type: str,
    payload: dict[str, Any],
) -> ImportJobAction:
    """Persist a durable rollback journal action.

    ``ImportRunner`` permits one active import execution at a time. Within that
    single-writer boundary, cache the last sequence per session/job so a large
    journal does not issue ``SELECT max(...)`` for every action. Rollback may
    leave legal sequence gaps; ordering, not contiguity, is the contract.
    """
    sequence_cache = _action_sequence_cache(session)
    last_sequence = sequence_cache.get(job.id)
    if last_sequence is None:
        sequence_no = await next_action_sequence(session, job.id)
    else:
        sequence_no = last_sequence + 1
    action = ImportJobAction(
        import_job_id=job.id,
        sequence_no=sequence_no,
        phase=phase,
        action_type=action_type,
        status=ImportJobActionStatus.COMPLETED,
        payload=payload,
    )
    session.add(action)
    await session.flush()
    sequence_cache[job.id] = sequence_no
    return action


def seed_action_sequence_cache(
    session: AsyncSession,
    job_id: int,
    *,
    last_sequence: int,
) -> None:
    """Seed a lock-serialized worker session without another max query."""
    if isinstance(last_sequence, bool) or last_sequence < 0:
        raise ValueError("Import action sequence seed must be non-negative")
    cache = _action_sequence_cache(session)
    cache[job_id] = max(cache.get(job_id, 0), last_sequence)


def _action_sequence_cache(session: AsyncSession) -> dict[int, int]:
    cached = session.info.setdefault(_ACTION_SEQUENCE_CACHE_KEY, {})
    if not isinstance(cached, dict):
        raise ValueError("Import action sequence cache has an invalid value")
    return cached


async def rollback_action(
    session: AsyncSession,
    *,
    action_id: int,
    action_type: str,
    payload: dict[str, Any],
    delete_series: DeleteSeriesForRollback,
) -> None:
    """Reverse a recorded import action in reverse execution order."""
    action = await session.get(ImportJobAction, action_id)
    if action is None:
        return
    if action.action_type != action_type or dict(action.payload or {}) != payload:
        raise ValueError("Import action changed after it was selected for rollback")

    if action_type == "story_arc_referenced_placement_attached":
        await _rollback_attached_story_arc_reference(session, action, payload)
    elif action_type == "story_arc_membership_created":
        await _rollback_created_story_arc_membership(session, action, payload)
    elif action_type == "story_arc_membership_updated":
        await _rollback_updated_story_arc_membership(session, action, payload)
    elif action_type == "story_arc_external_identity_created":
        await _rollback_created_story_arc_external_identity(session, action, payload)
    elif action_type == "story_arc_policy_updated":
        await _rollback_story_arc_policy_update(session, action, payload)
    elif action_type == "story_arc_created":
        await _rollback_created_story_arc(session, action, payload)
    elif action_type == "library_file_registered":
        library_file_id = int(payload.get("library_file_id") or 0)
        destination_path = Path(str(payload.get("destination_path") or ""))
        original_source_path = Path(str(payload.get("original_source_path") or ""))
        transfer_method = str(payload.get("transfer_method") or "move")
        storage_mode = str(payload.get("storage_mode") or "")
        referenced_file = storage_mode == "referenced" or transfer_method == "leave_in_place"
        original_trash_path = str(payload.get("original_trash_path") or "")
        created_series_folder = bool(payload.get("created_series_folder"))
        created_series_folder_path_raw = str(payload.get("created_series_folder_path") or "")
        permission_restores = list(payload.get("permission_restores") or [])

        library_file = await session.get(LibraryFile, library_file_id)
        if library_file is not None:
            await session.delete(library_file)

        if not referenced_file:
            if transfer_method == "move":
                if original_trash_path:
                    restore_file_from_utility_trash(Path(original_trash_path), original_source_path)
                    if destination_path.exists():
                        destination_path.unlink(missing_ok=True)
                elif destination_path.exists():
                    original_source_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(destination_path), str(original_source_path))
            elif destination_path.exists():
                destination_path.unlink(missing_ok=True)

            for entry in permission_restores:
                restore_path = Path(str(entry.get("path") or ""))
                restore_mode = entry.get("mode")
                if not restore_path or restore_mode is None or not restore_path.exists():
                    continue
                try:
                    restore_path.chmod(int(restore_mode))
                except OSError:
                    continue

            if created_series_folder and created_series_folder_path_raw:
                created_series_folder_path = Path(created_series_folder_path_raw)
            else:
                created_series_folder_path = None

            if created_series_folder_path is not None and created_series_folder_path.exists():
                try:
                    next(created_series_folder_path.iterdir())
                except StopIteration:
                    created_series_folder_path.rmdir()
                except OSError:
                    pass

    elif action_type == "library_file_placement_started":
        destination_path_raw = str(payload.get("destination_path") or "")
        partial_destination_path = Path(destination_path_raw) if destination_path_raw else None
        original_source_path_raw = str(payload.get("original_source_path") or "")
        partial_original_source_path = (
            Path(original_source_path_raw) if original_source_path_raw else None
        )
        artifact_source_path_raw = str(payload.get("artifact_source_path") or "")
        partial_artifact_source_path = (
            Path(artifact_source_path_raw) if artifact_source_path_raw else None
        )
        transfer_method = str(payload.get("transfer_method") or "move")
        created_series_folder = bool(payload.get("created_series_folder"))
        created_series_folder_path_raw = str(payload.get("created_series_folder_path") or "")
        temp_paths = [Path(str(path)) for path in payload.get("temp_paths") or [] if str(path)]

        for temp_path in temp_paths:
            if temp_path.exists() and temp_path.is_file():
                temp_path.unlink(missing_ok=True)

        destination_is_original_source = (
            partial_destination_path is not None
            and partial_original_source_path is not None
            and partial_destination_path.resolve(strict=False)
            == partial_original_source_path.resolve(strict=False)
        )
        if (
            partial_destination_path is not None
            and partial_destination_path.exists()
            and not destination_is_original_source
        ):
            can_restore_move = (
                transfer_method == "move"
                and partial_original_source_path is not None
                and partial_artifact_source_path is not None
                and partial_artifact_source_path == partial_original_source_path
                and not partial_original_source_path.exists()
            )
            if can_restore_move:
                assert partial_original_source_path is not None
                partial_original_source_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(partial_destination_path), str(partial_original_source_path))
            elif partial_destination_path.is_file() or partial_destination_path.is_symlink():
                partial_destination_path.unlink(missing_ok=True)

        if created_series_folder and created_series_folder_path_raw:
            created_series_folder_path = Path(created_series_folder_path_raw)
            if created_series_folder_path.exists():
                try:
                    next(created_series_folder_path.iterdir())
                except StopIteration:
                    created_series_folder_path.rmdir()
                except OSError:
                    pass

    elif action_type == "series_created":
        series_id = int(payload.get("series_id") or 0)
        if series_id:
            with contextlib.suppress(NotFoundError):
                await delete_series(
                    session,
                    series_id,
                    delete_files=False,
                    delete_folder=True,
                )
                # A series-created action can be replayed after a partial rollback or
                # after multiple import rows converged on the same real series. Missing
                # here means the rollback objective is already satisfied.

    elif action_type == "series_folder_renamed":
        series_id = int(payload.get("series_id") or 0)
        import_series_id = int(payload.get("import_series_id") or 0)
        old_folder_path = Path(str(payload.get("old_folder_path") or ""))
        new_folder_path = Path(str(payload.get("new_folder_path") or ""))
        old_series_path = str(payload.get("old_series_path") or "")
        old_library_root_id_raw = payload.get("old_library_root_id")

        if new_folder_path.exists() and not old_folder_path.exists():
            old_folder_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(new_folder_path), str(old_folder_path))
        if import_series_id:
            await _restore_imported_file_paths_after_folder_rollback(
                session,
                import_series_id=import_series_id,
                old_folder_path=old_folder_path,
                new_folder_path=new_folder_path,
            )

        if series_id:
            series = await session.get(Series, series_id)
            if series is not None:
                series.path = old_series_path or None
                series.library_root_id = (
                    int(old_library_root_id_raw) if old_library_root_id_raw is not None else None
                )

    elif action_type == "library_root_policy_applied":
        from pullbox.services.import_root_policy_activation import (
            rollback_future_root_policy,
        )

        action = await session.get(ImportJobAction, action_id)
        if action is None:
            return
        job = await session.get(ImportJob, action.import_job_id)
        if job is None:
            raise NotFoundError("ImportJob", action.import_job_id)
        await rollback_future_root_policy(session, job=job, action=action)
        return

    action.status = ImportJobActionStatus.ROLLED_BACK
    action.rolled_back_at = datetime.now(UTC)
    await session.flush()


async def _rollback_attached_story_arc_reference(
    session: AsyncSession,
    action: ImportJobAction,
    payload: dict[str, Any],
) -> None:
    """Detach import-owned database evidence without touching the user artifact."""
    if payload.get("journal_state") == "prepared" and payload.get("placement_id") is None:
        return
    if payload.get("journal_state") != "completed":
        raise ValueError("Referenced story-arc placement journal is incomplete")
    placement_id = _positive_int(payload.get("placement_id"), "placement_id")
    membership_id = _positive_int(payload.get("issue_story_arc_id"), "issue_story_arc_id")
    imported_entry_id = _positive_int(
        payload.get("imported_story_arc_entry_id"),
        "imported_story_arc_entry_id",
    )
    if (
        _positive_int(payload.get("source_import_job_id"), "source_import_job_id")
        != action.import_job_id
    ):
        raise ValueError("Referenced story-arc placement journal job changed")
    placement = await session.get(StoryArcPlacement, placement_id)
    if placement is None:
        return
    await _require_story_arc_entry_ownership(
        session,
        action=action,
        imported_story_arc_entry_id=imported_entry_id,
        membership_id=membership_id,
    )
    expected_identity = {
        "issue_story_arc_id": membership_id,
        "placement_path": _required_string(payload, "placement_path"),
        "mode": StoryArcPlacementMode.REFERENCE_ONLY.value,
        "ownership": StoryArcPlacementOwnership.REFERENCED.value,
        "source_kind": _required_string(payload, "source_kind"),
        "source_import_job_id": action.import_job_id,
        "creating_action_id": int(action.id),
    }
    if _referenced_placement_rollback_identity(placement) != expected_identity:
        raise ValueError("Referenced story-arc placement ownership changed; rollback refused")
    await session.delete(placement)


async def _rollback_created_story_arc_membership(
    session: AsyncSession,
    action: ImportJobAction,
    payload: dict[str, Any],
) -> None:
    membership_id = _positive_int(payload.get("membership_id"), "membership_id")
    story_arc_id = _positive_int(payload.get("story_arc_id"), "story_arc_id")
    expected_after = _payload_mapping(payload, "expected_after")
    membership = await session.get(IssueStoryArc, membership_id)
    if membership is None:
        return
    await _require_story_arc_entry_ownership(
        session,
        action=action,
        imported_story_arc_entry_id=_positive_int(
            payload.get("imported_story_arc_entry_id"),
            "imported_story_arc_entry_id",
        ),
        membership_id=membership_id,
    )
    if membership.story_arc_id != story_arc_id or _membership_state(membership) != expected_after:
        raise ValueError("Story-arc membership changed after import; rollback refused")
    placement_count = int(
        await session.scalar(
            sa_select(sa_func.count())
            .select_from(StoryArcPlacement)
            .where(StoryArcPlacement.issue_story_arc_id == membership_id)
        )
        or 0
    )
    if placement_count:
        raise ValueError("Story-arc membership has placements; rollback refused")
    arc = await session.get(StoryArc, story_arc_id)
    if arc is None:
        raise ValueError("Story arc disappeared before membership rollback")
    revision_after = _non_negative_int(payload.get("arc_revision_after"), "arc_revision_after")
    revision_before = _non_negative_int(payload.get("arc_revision_before"), "arc_revision_before")
    if int(arc.revision) != revision_after:
        raise ValueError("Story arc changed after import; rollback refused")
    await session.delete(membership)
    arc.revision = revision_before


async def _rollback_updated_story_arc_membership(
    session: AsyncSession,
    action: ImportJobAction,
    payload: dict[str, Any],
) -> None:
    membership_id = _positive_int(payload.get("membership_id"), "membership_id")
    story_arc_id = _positive_int(payload.get("story_arc_id"), "story_arc_id")
    expected_after = _payload_mapping(payload, "expected_after")
    restore_before = _payload_mapping(payload, "restore_before")
    membership = await session.get(IssueStoryArc, membership_id)
    if membership is None:
        raise ValueError("Updated story-arc membership disappeared; rollback refused")
    await _require_story_arc_entry_ownership(
        session,
        action=action,
        imported_story_arc_entry_id=_positive_int(
            payload.get("imported_story_arc_entry_id"),
            "imported_story_arc_entry_id",
        ),
        membership_id=membership_id,
    )
    if membership.story_arc_id != story_arc_id or _membership_state(membership) != expected_after:
        raise ValueError("Story-arc membership changed after import; rollback refused")
    arc = await session.get(StoryArc, story_arc_id)
    if arc is None:
        raise ValueError("Story arc disappeared before membership rollback")
    revision_after = _non_negative_int(payload.get("arc_revision_after"), "arc_revision_after")
    revision_before = _non_negative_int(payload.get("arc_revision_before"), "arc_revision_before")
    if int(arc.revision) != revision_after:
        raise ValueError("Story arc changed after import; rollback refused")
    _restore_membership_state(membership, restore_before)
    arc.revision = revision_before


async def _rollback_created_story_arc_external_identity(
    session: AsyncSession,
    action: ImportJobAction,
    payload: dict[str, Any],
) -> None:
    identity_id = _positive_int(payload.get("external_identity_id"), "external_identity_id")
    story_arc_id = _positive_int(payload.get("story_arc_id"), "story_arc_id")
    expected_after = _payload_mapping(payload, "expected_after")
    identity = await session.get(StoryArcExternalIdentity, identity_id)
    if identity is None:
        return
    await _require_story_arc_ownership(
        session,
        action=action,
        imported_story_arc_id=_positive_int(
            payload.get("imported_story_arc_id"), "imported_story_arc_id"
        ),
        story_arc_id=story_arc_id,
    )
    if (
        identity.story_arc_id != story_arc_id
        or _external_identity_state(identity) != expected_after
    ):
        raise ValueError("Story-arc external identity changed after import; rollback refused")
    await session.delete(identity)


async def _rollback_story_arc_policy_update(
    session: AsyncSession,
    action: ImportJobAction,
    payload: dict[str, Any],
) -> None:
    story_arc_id = _positive_int(payload.get("story_arc_id"), "story_arc_id")
    expected_after = _payload_mapping(payload, "expected_after")
    restore_before = _payload_mapping(payload, "restore_before")
    arc = await session.get(StoryArc, story_arc_id)
    if arc is None:
        raise ValueError("Updated story arc disappeared; rollback refused")
    await _require_story_arc_ownership(
        session,
        action=action,
        imported_story_arc_id=_positive_int(
            payload.get("imported_story_arc_id"), "imported_story_arc_id"
        ),
        story_arc_id=story_arc_id,
    )
    if _story_arc_policy_state(arc) != expected_after:
        raise ValueError("Story arc changed after import; rollback refused")
    _restore_story_arc_policy_state(arc, restore_before)


async def _rollback_created_story_arc(
    session: AsyncSession,
    action: ImportJobAction,
    payload: dict[str, Any],
) -> None:
    story_arc_id = _positive_int(payload.get("story_arc_id"), "story_arc_id")
    expected_after = _payload_mapping(payload, "expected_after")
    arc = await session.get(StoryArc, story_arc_id)
    if arc is None:
        return
    await _require_story_arc_ownership(
        session,
        action=action,
        imported_story_arc_id=_positive_int(
            payload.get("imported_story_arc_id"), "imported_story_arc_id"
        ),
        story_arc_id=story_arc_id,
    )
    if arc.source_import_job_id != action.import_job_id:
        raise ValueError("Story arc is not owned by this import; rollback refused")
    if _story_arc_created_state(arc) != expected_after:
        raise ValueError("Story arc changed after import; rollback refused")
    membership_count = int(
        await session.scalar(
            sa_select(sa_func.count())
            .select_from(IssueStoryArc)
            .where(IssueStoryArc.story_arc_id == story_arc_id)
        )
        or 0
    )
    identity_count = int(
        await session.scalar(
            sa_select(sa_func.count())
            .select_from(StoryArcExternalIdentity)
            .where(StoryArcExternalIdentity.story_arc_id == story_arc_id)
        )
        or 0
    )
    placement_count = int(
        await session.scalar(
            sa_select(sa_func.count())
            .select_from(StoryArcPlacement)
            .join(
                IssueStoryArc,
                IssueStoryArc.id == StoryArcPlacement.issue_story_arc_id,
            )
            .where(IssueStoryArc.story_arc_id == story_arc_id)
        )
        or 0
    )
    if membership_count or identity_count or placement_count:
        raise ValueError("Story arc still has related rows; rollback refused")
    await session.delete(arc)


async def _require_story_arc_ownership(
    session: AsyncSession,
    *,
    action: ImportJobAction,
    imported_story_arc_id: int,
    story_arc_id: int,
) -> None:
    staged_arc_id = await session.scalar(
        sa_select(ImportedStoryArc.id).where(
            ImportedStoryArc.id == imported_story_arc_id,
            ImportedStoryArc.import_job_id == action.import_job_id,
            ImportedStoryArc.materialized_story_arc_id == story_arc_id,
        )
    )
    if staged_arc_id is None:
        raise ValueError("Story-arc rollback ownership changed; rollback refused")


async def _require_story_arc_entry_ownership(
    session: AsyncSession,
    *,
    action: ImportJobAction,
    imported_story_arc_entry_id: int,
    membership_id: int,
) -> None:
    staged_entry_id = await session.scalar(
        sa_select(ImportedStoryArcEntry.id)
        .join(
            ImportedStoryArc,
            ImportedStoryArc.id == ImportedStoryArcEntry.imported_story_arc_id,
        )
        .where(
            ImportedStoryArcEntry.id == imported_story_arc_entry_id,
            ImportedStoryArcEntry.materialized_membership_id == membership_id,
            ImportedStoryArc.import_job_id == action.import_job_id,
        )
    )
    if staged_entry_id is None:
        raise ValueError("Story-arc membership rollback ownership changed; rollback refused")


def _story_arc_policy_state(arc: StoryArc) -> dict[str, object]:
    return {
        "monitored": bool(arc.monitored),
        "search_missing": bool(arc.search_missing),
        "include_upcoming": bool(arc.include_upcoming),
        "sync_enabled": bool(arc.sync_enabled),
        "target_library_root_id": arc.target_library_root_id,
        "policy_schema_version": arc.policy_schema_version,
        "policy_snapshot": dict(arc.policy_snapshot or {}),
        "revision": int(arc.revision),
    }


def _story_arc_created_state(arc: StoryArc) -> dict[str, object]:
    return {
        "name": arc.name,
        "normalized_name": arc.normalized_name,
        "description": arc.description,
        "comicvine_id": arc.comicvine_id,
        "publisher_id": arc.publisher_id,
        "comicvine_url": arc.comicvine_url,
        "source_kind": arc.source_kind.value,
        "lifecycle": arc.lifecycle.value,
        "source_import_job_id": arc.source_import_job_id,
        "diagnostics": dict(arc.diagnostics or {}),
        **_story_arc_policy_state(arc),
    }


def _membership_state(membership: IssueStoryArc) -> dict[str, object]:
    return {
        "story_arc_id": int(membership.story_arc_id),
        "issue_id": membership.issue_id,
        "sequence_number": int(membership.sequence_number),
        "source_ordinal": int(membership.source_ordinal),
        "legacy_sequence_was_null": bool(membership.legacy_sequence_was_null),
        "resolution_state": membership.resolution_state.value,
        "source_kind": membership.source_kind.value,
        "source_entry_id": membership.source_entry_id,
        "source_arc_id": membership.source_arc_id,
        "source_issue_id": membership.source_issue_id,
        "source_series_id": membership.source_series_id,
        "source_issue_number_text": membership.source_issue_number_text,
        "source_series_name": membership.source_series_name,
        "source_issue_title": membership.source_issue_title,
        "source_publisher": membership.source_publisher,
        "source_release_date_text": membership.source_release_date_text,
        "source_issue_date_text": membership.source_issue_date_text,
        "resolution_confidence": membership.resolution_confidence,
        "resolution_method": membership.resolution_method,
        "evidence": dict(membership.evidence or {}),
        "sync_eligible": bool(membership.sync_eligible),
        "last_materialization_result": dict(membership.last_materialization_result or {}),
    }


def _external_identity_state(identity: StoryArcExternalIdentity) -> dict[str, object]:
    return {
        "story_arc_id": int(identity.story_arc_id),
        "source": identity.source,
        "namespace": identity.namespace,
        "external_id": identity.external_id,
        "source_url": identity.source_url,
        "evidence": dict(identity.evidence or {}),
    }


def _referenced_placement_rollback_identity(
    placement: StoryArcPlacement,
) -> dict[str, object]:
    """Return only immutable ownership fields; observed drift is intentionally excluded."""
    return {
        "issue_story_arc_id": int(placement.issue_story_arc_id),
        "placement_path": placement.placement_path,
        "mode": placement.mode.value,
        "ownership": placement.ownership.value,
        "source_kind": placement.source_kind.value,
        "source_import_job_id": placement.source_import_job_id,
        "creating_action_id": placement.creating_action_id,
    }


def _restore_story_arc_policy_state(arc: StoryArc, state: dict[str, object]) -> None:
    arc.monitored = _bool_value(state, "monitored")
    arc.search_missing = _bool_value(state, "search_missing")
    arc.include_upcoming = _bool_value(state, "include_upcoming")
    arc.sync_enabled = _bool_value(state, "sync_enabled")
    arc.target_library_root_id = _optional_positive_int(
        state.get("target_library_root_id"), "target_library_root_id"
    )
    arc.policy_schema_version = _optional_non_negative_int(
        state.get("policy_schema_version"), "policy_schema_version"
    )
    arc.policy_snapshot = dict(_mapping_value(state.get("policy_snapshot"), "policy_snapshot"))
    arc.revision = _non_negative_int(state.get("revision"), "revision")


def _restore_membership_state(
    membership: IssueStoryArc,
    state: dict[str, object],
) -> None:
    if _positive_int(state.get("story_arc_id"), "story_arc_id") != membership.story_arc_id:
        raise ValueError("Membership rollback story arc does not match")
    membership.issue_id = _optional_positive_int(state.get("issue_id"), "issue_id")
    membership.sequence_number = _non_negative_int(state.get("sequence_number"), "sequence_number")
    membership.source_ordinal = _non_negative_int(state.get("source_ordinal"), "source_ordinal")
    membership.legacy_sequence_was_null = _bool_value(state, "legacy_sequence_was_null")
    membership.resolution_state = StoryArcResolutionState(
        _required_string(state, "resolution_state")
    )
    membership.source_kind = StoryArcSourceKind(_required_string(state, "source_kind"))
    for field in (
        "source_entry_id",
        "source_arc_id",
        "source_issue_id",
        "source_series_id",
        "source_issue_number_text",
        "source_series_name",
        "source_issue_title",
        "source_publisher",
        "source_release_date_text",
        "source_issue_date_text",
        "resolution_method",
    ):
        setattr(membership, field, _optional_string(state.get(field), field))
    confidence = state.get("resolution_confidence")
    if confidence is not None and (
        isinstance(confidence, bool) or not isinstance(confidence, int | float)
    ):
        raise ValueError("Invalid resolution_confidence in story-arc rollback action")
    membership.resolution_confidence = float(confidence) if confidence is not None else None
    membership.evidence = dict(_mapping_value(state.get("evidence"), "evidence"))
    membership.sync_eligible = _bool_value(state, "sync_eligible")
    membership.last_materialization_result = dict(
        _mapping_value(state.get("last_materialization_result"), "last_materialization_result")
    )


def _payload_mapping(payload: dict[str, Any], key: str) -> dict[str, object]:
    return dict(_mapping_value(payload.get(key), key))


def _mapping_value(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"Invalid {label} in story-arc rollback action")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Invalid {label} in story-arc rollback action")
    return value


def _non_negative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Invalid {label} in story-arc rollback action")
    return value


def _optional_positive_int(value: object, label: str) -> int | None:
    return None if value is None else _positive_int(value, label)


def _optional_non_negative_int(value: object, label: str) -> int | None:
    return None if value is None else _non_negative_int(value, label)


def _bool_value(state: dict[str, object], key: str) -> bool:
    value = state.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"Invalid {key} in story-arc rollback action")
    return value


def _required_string(state: dict[str, object], key: str) -> str:
    value = state.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Invalid {key} in story-arc rollback action")
    return value


def _optional_string(value: object, label: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"Invalid {label} in story-arc rollback action")
    return value


async def _restore_imported_file_paths_after_folder_rollback(
    session: AsyncSession,
    *,
    import_series_id: int,
    old_folder_path: Path,
    new_folder_path: Path,
) -> None:
    """Point import-review file paths back at the restored source folder."""
    resolved_old_folder = old_folder_path.expanduser().resolve(strict=False)
    resolved_new_folder = new_folder_path.expanduser().resolve(strict=False)
    result = await session.execute(
        sa_select(ImportedFile).where(ImportedFile.import_series_id == import_series_id)
    )
    for imported_file in result.scalars().all():
        current_path = Path(imported_file.file_path)
        resolved_current_path = current_path.expanduser().resolve(strict=False)
        try:
            relative_path = resolved_current_path.relative_to(resolved_new_folder)
        except ValueError:
            continue
        imported_file.file_path = str(resolved_old_folder / relative_path)
