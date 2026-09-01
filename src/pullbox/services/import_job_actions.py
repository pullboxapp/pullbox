"""Import rollback journal action helpers."""

from __future__ import annotations

import contextlib
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypedDict

from sqlalchemy import func as sa_func
from sqlalchemy import insert as sa_insert
from sqlalchemy import or_
from sqlalchemy import select as sa_select
from sqlalchemy import update as sa_update

from pullbox.core.exceptions import ConfigurationError, NotFoundError
from pullbox.core.library_file_ownership import build_managed_placement_signature
from pullbox.models.blocklist import BlocklistEntry
from pullbox.models.direct_acquisition import DirectAcquisitionAttempt
from pullbox.models.download import DownloadHistory
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobAction,
    ImportJobActionStatus,
)
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import LibraryFile, LibraryFileStorageMode
from pullbox.models.pending_match import PendingMatch
from pullbox.models.reader import IssueReaderState
from pullbox.models.search_log import SearchLog
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
from pullbox.models.story_arc_sync import StoryArcSyncWork, StoryArcSyncWorkState
from pullbox.utilities.settings import restore_file_from_utility_trash

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


_ACTION_SEQUENCE_CACHE_KEY = "pullbox.import_action_last_sequence"
_ACTION_INSERT_BATCH_SIZE = 200
_STORY_ARC_MANAGED_PLACEMENT_ACTION = "story_arc_managed_placement_requested"
_STORY_ARC_PLACEMENT_PHASE = "story_arc_placements"
_STORY_ARC_MANAGED_PLACEMENT_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "sync_work_id",
        "membership_id",
        "desired_generation",
        "imported_story_arc_id",
        "imported_story_arc_entry_id",
        "source_import_job_id",
    }
)
_CANCELLABLE_STORY_ARC_WORK_STATES = frozenset(
    {
        StoryArcSyncWorkState.QUEUED,
        StoryArcSyncWorkState.RETRY_WAIT,
        StoryArcSyncWorkState.FAILED,
    }
)


class _ManagedPlacementRollbackPayload(TypedDict):
    sync_work_id: int
    membership_id: int
    desired_generation: str
    imported_story_arc_id: int
    imported_story_arc_entry_id: int
    source_import_job_id: int


class StoryArcManagedPlacementRollbackDeferredError(RuntimeError):
    """A running placement must acknowledge cooperative cancellation first."""

    def __init__(self, work_id: int) -> None:
        self.work_id = work_id
        super().__init__(
            "Story Arc placement rollback is waiting for running work "
            f"{work_id} to acknowledge cancellation"
        )


@dataclass(frozen=True, slots=True)
class ImportJobActionSpec:
    """One ordered rollback-journal action awaiting sequence allocation."""

    phase: str
    action_type: str
    payload: dict[str, Any]


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


async def build_series_created_action_payload(
    session: AsyncSession,
    *,
    series_id: int,
    import_series_id: int,
) -> dict[str, Any]:
    """Capture the user-owned series state required for safe rollback.

    Metadata hydration may legitimately update provider-owned fields after the
    series is created, so this seal intentionally covers only user-owned
    choices. Related files and activity are checked independently at rollback
    time. Older journal rows without this seal are preserved for manual review.
    """
    await session.flush()
    series = await session.get(Series, series_id)
    if series is None:
        raise NotFoundError("Series", series_id)
    imported_series = await session.get(ImportedSeries, import_series_id)
    if imported_series is None:
        raise NotFoundError("ImportedSeries", import_series_id)
    issue_rows = (
        await session.execute(
            sa_select(Issue.id, Issue.status, Issue.manual_skip)
            .where(Issue.series_id == series_id)
            .order_by(Issue.id)
        )
    ).all()
    payload: dict[str, Any] = {
        "series_id": series_id,
        "import_series_id": import_series_id,
        "series_ownership_snapshot": _series_user_owned_snapshot(series),
        "issue_ownership_snapshot": {
            str(issue_id): {
                "status": status.value,
                "manual_skip": bool(manual_skip),
            }
            for issue_id, status, manual_skip in issue_rows
        },
    }
    cover_cache_ownership = _validated_cover_cache_ownership(
        (imported_series.diagnostics or {}).get("cover_cache_ownership")
    )
    if cover_cache_ownership is not None:
        payload["cover_cache_ownership"] = cover_cache_ownership
    series_folder_ownership = _validated_series_folder_ownership(
        (imported_series.diagnostics or {}).get("series_folder_ownership")
    )
    if series_folder_ownership is not None and _series_path_matches_folder_ownership(
        series,
        series_folder_ownership,
    ):
        payload["series_folder_ownership"] = _folder_ownership_with_installed_state(
            series_folder_ownership,
            series,
        )
    return payload


async def build_series_cover_cache_action_payload(
    session: AsyncSession,
    *,
    series_id: int,
    import_series_id: int,
    previous_cover_path: str | None,
) -> dict[str, Any] | None:
    """Build a rollback action only for a cover artifact this import created."""
    await session.flush()
    series = await session.get(Series, series_id)
    imported_series = await session.get(ImportedSeries, import_series_id)
    if series is None:
        raise NotFoundError("Series", series_id)
    if imported_series is None:
        raise NotFoundError("ImportedSeries", import_series_id)
    ownership = _validated_cover_cache_ownership(
        (imported_series.diagnostics or {}).get("cover_cache_ownership")
    )
    if ownership is None:
        return None
    installed_cover_path = series.cover_path
    if not isinstance(installed_cover_path, str) or not installed_cover_path:
        return None
    return {
        "series_id": series_id,
        "import_series_id": import_series_id,
        "previous_cover_path": previous_cover_path,
        "installed_cover_path": installed_cover_path,
        "cover_cache_ownership": ownership,
    }


async def build_series_cover_path_updated_action_payload(
    session: AsyncSession,
    *,
    series_id: int,
    import_series_id: int,
    previous_cover_path: str | None,
) -> dict[str, Any] | None:
    """Journal a DB-only cover-path mutation without claiming its artifact."""
    await session.flush()
    series = await session.get(Series, series_id)
    imported_series = await session.get(ImportedSeries, import_series_id)
    if series is None:
        raise NotFoundError("Series", series_id)
    if imported_series is None:
        raise NotFoundError("ImportedSeries", import_series_id)
    if series.cover_path == previous_cover_path:
        return None
    return {
        "series_id": series_id,
        "import_series_id": import_series_id,
        "previous_cover_path": previous_cover_path,
        "installed_cover_path": series.cover_path,
    }


async def build_series_monitoring_updated_action_payload(
    session: AsyncSession,
    *,
    series_id: int,
    import_series_id: int,
    previous_monitored: bool,
) -> dict[str, Any] | None:
    """Journal an existing Series monitoring mutation made by search-on-add."""
    await session.flush()
    series = await session.get(Series, series_id)
    imported_series = await session.get(ImportedSeries, import_series_id)
    if series is None:
        raise NotFoundError("Series", series_id)
    if imported_series is None:
        raise NotFoundError("ImportedSeries", import_series_id)
    installed_monitored = bool(series.monitored)
    if installed_monitored == previous_monitored:
        return None
    return {
        "series_id": series_id,
        "import_series_id": import_series_id,
        "previous_monitored": previous_monitored,
        "installed_monitored": installed_monitored,
    }


async def build_series_folder_created_action_payload(
    session: AsyncSession,
    *,
    series_id: int,
    import_series_id: int,
    previous_series_path: str | None,
    previous_library_root_id: int | None,
    previous_preferred_library_root_id: int | None,
) -> dict[str, Any] | None:
    """Build the replay-safe state restoration for an existing pathless Series."""
    await session.flush()
    series = await session.get(Series, series_id)
    imported_series = await session.get(ImportedSeries, import_series_id)
    if series is None:
        raise NotFoundError("Series", series_id)
    if imported_series is None:
        raise NotFoundError("ImportedSeries", import_series_id)
    ownership = _validated_series_folder_ownership(
        (imported_series.diagnostics or {}).get("series_folder_ownership")
    )
    if ownership is None or not _series_path_matches_folder_ownership(series, ownership):
        return None
    return {
        "series_id": series_id,
        "import_series_id": import_series_id,
        "previous_series_path": previous_series_path,
        "previous_library_root_id": previous_library_root_id,
        "previous_preferred_library_root_id": previous_preferred_library_root_id,
        "series_folder_ownership": _folder_ownership_with_installed_state(
            ownership,
            series,
        ),
    }


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


def _action_insert_rows(
    *,
    job_id: int,
    first_sequence: int,
    specs: Sequence[ImportJobActionSpec],
) -> list[dict[str, Any]]:
    """Render an ordered sequence block as Core insert mappings."""
    return [
        {
            "import_job_id": job_id,
            "sequence_no": first_sequence + offset,
            "phase": spec.phase,
            "action_type": spec.action_type,
            "status": ImportJobActionStatus.COMPLETED,
            "payload": dict(spec.payload),
        }
        for offset, spec in enumerate(specs)
    ]


def _action_insert_statement(
    *,
    returning: bool,
) -> Any:
    """Build one portable executemany INSERT, optionally returning ORM rows."""
    statement = sa_insert(ImportJobAction)
    return statement.returning(ImportJobAction) if returning else statement


def _supports_multirow_insert_returning(session: AsyncSession) -> bool:
    dialect = session.get_bind().dialect
    return bool(
        getattr(dialect, "insert_returning", False)
        and getattr(dialect, "supports_multivalues_insert", False)
    )


async def record_actions(
    session: AsyncSession,
    job: ImportJob,
    specs: Sequence[ImportJobActionSpec],
) -> list[ImportJobAction]:
    """Persist an ordered action batch inside the caller-owned transaction.

    One sequence range is allocated up front. Inserts are page-bounded and use
    multi-row ``INSERT ... RETURNING`` on SQLite/PostgreSQL; other dialects use
    bounded multi-row inserts followed by one range readback. The session cache
    advances only after the complete batch succeeds, and this helper never
    commits the surrounding transaction.
    """
    ordered_specs = tuple(specs)
    if not ordered_specs:
        return []

    sequence_cache = _action_sequence_cache(session)
    last_sequence = sequence_cache.get(job.id)
    first_sequence = (
        await next_action_sequence(session, job.id) if last_sequence is None else last_sequence + 1
    )
    rows = _action_insert_rows(
        job_id=job.id,
        first_sequence=first_sequence,
        specs=ordered_specs,
    )
    use_returning = _supports_multirow_insert_returning(session)
    actions: list[ImportJobAction] = []
    for offset in range(0, len(rows), _ACTION_INSERT_BATCH_SIZE):
        page = rows[offset : offset + _ACTION_INSERT_BATCH_SIZE]
        if use_returning:
            statement = _action_insert_statement(returning=True)
            actions.extend((await session.scalars(statement, page)).all())
        else:
            await session.execute(sa_insert(ImportJobAction), page)

    last_allocated_sequence = first_sequence + len(rows) - 1
    if not use_returning:
        actions = list(
            (
                await session.scalars(
                    sa_select(ImportJobAction)
                    .where(
                        ImportJobAction.import_job_id == job.id,
                        ImportJobAction.sequence_no >= first_sequence,
                        ImportJobAction.sequence_no <= last_allocated_sequence,
                    )
                    .order_by(ImportJobAction.sequence_no.asc())
                )
            ).all()
        )
    else:
        actions.sort(key=lambda action: action.sequence_no)

    expected_sequences = list(range(first_sequence, last_allocated_sequence + 1))
    if [action.sequence_no for action in actions] != expected_sequences:
        raise RuntimeError("Import action batch did not return its exact allocated sequence block")
    sequence_cache[job.id] = last_allocated_sequence
    return actions


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


def _series_rollback_lock_statement(series_id: int) -> Any:
    """Lock the candidate Series against concurrent user-owned changes."""
    return (
        sa_select(Series)
        .where(Series.id == series_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )


def _series_issue_rollback_lock_statement(series_id: int) -> Any:
    """Lock every Issue before checking activity and deleting its Series."""
    return (
        sa_select(Issue.id).where(Issue.series_id == series_id).order_by(Issue.id).with_for_update()
    )


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

    if action_type == _STORY_ARC_MANAGED_PLACEMENT_ACTION:
        await _rollback_import_managed_story_arc_placement(session, action, payload)
    elif action_type == "story_arc_referenced_placement_attached":
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
        permission_restores = list(payload.get("permission_restores") or [])

        library_file = await session.get(LibraryFile, library_file_id)
        source_reappeared = (
            not referenced_file
            and transfer_method == "move"
            and os.path.lexists(original_source_path)
            and (
                (
                    os.path.lexists(destination_path)
                    and destination_path.resolve(strict=False)
                    != original_source_path.resolve(strict=False)
                )
                or (bool(original_trash_path) and os.path.lexists(original_trash_path))
            )
        )
        if source_reappeared:
            action.status = ImportJobActionStatus.ROLLBACK_FAILED
            action.error_message = (
                "The original move source reappeared after import. Pullbox preserved both "
                "the source and managed destination for review."
            )
            action.rolled_back_at = None
            await session.flush()
            return
        if (
            not referenced_file
            and os.path.lexists(destination_path)
            and not _managed_destination_matches_rollback_action(
                destination_path,
                library_file=library_file,
                expected_signature=payload.get("destination_signature"),
            )
        ):
            action.status = ImportJobActionStatus.ROLLBACK_FAILED
            action.error_message = (
                "Managed library file changed after import or no longer matches its "
                "rollback ownership record. Pullbox preserved the file."
            )
            action.rolled_back_at = None
            await session.flush()
            return
        if library_file is not None:
            await session.delete(library_file)

        if not referenced_file:
            if transfer_method == "move":
                if original_trash_path:
                    restore_file_from_utility_trash(Path(original_trash_path), original_source_path)
                    if os.path.lexists(destination_path):
                        destination_path.unlink(missing_ok=True)
                elif os.path.lexists(destination_path):
                    original_source_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(destination_path), str(original_source_path))
            elif os.path.lexists(destination_path):
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

            _cleanup_import_created_directories(
                payload,
                destination_parent=destination_path.parent,
            )

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
        temp_paths = [Path(str(path)) for path in payload.get("temp_paths") or [] if str(path)]
        surviving_temp_paths = [path for path in temp_paths if os.path.lexists(path)]
        if surviving_temp_paths:
            action.status = ImportJobActionStatus.ROLLBACK_FAILED
            action.error_message = (
                "Import staging artifacts have no completed ownership signature. "
                "Pullbox preserved them for review."
            )
            action.rolled_back_at = None
            await session.flush()
            return

        destination_is_original_source = (
            partial_destination_path is not None
            and partial_original_source_path is not None
            and partial_destination_path.resolve(strict=False)
            == partial_original_source_path.resolve(strict=False)
        )
        if (
            partial_destination_path is not None
            and os.path.lexists(partial_destination_path)
            and not destination_is_original_source
        ):
            placement_completed = payload.get("placement_completed") is True
            destination_signature = payload.get("destination_signature")
            if not placement_completed or not _path_matches_signature(
                partial_destination_path,
                destination_signature,
            ):
                action.status = ImportJobActionStatus.ROLLBACK_FAILED
                action.error_message = (
                    "Import destination is incomplete, changed, or lacks durable ownership "
                    "evidence. Pullbox preserved it for review."
                )
                action.rolled_back_at = None
                await session.flush()
                return
            can_restore_move = (
                transfer_method == "move"
                and partial_original_source_path is not None
                and partial_artifact_source_path is not None
                and partial_artifact_source_path == partial_original_source_path
                and not os.path.lexists(partial_original_source_path)
            )
            if can_restore_move:
                assert partial_original_source_path is not None
                partial_original_source_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(partial_destination_path), str(partial_original_source_path))
            elif (
                transfer_method == "move"
                and partial_original_source_path is not None
                and partial_artifact_source_path == partial_original_source_path
                and os.path.lexists(partial_original_source_path)
            ):
                action.status = ImportJobActionStatus.ROLLBACK_FAILED
                action.error_message = (
                    "The original move source reappeared after import. Pullbox preserved both "
                    "the source and managed destination for review."
                )
                action.rolled_back_at = None
                await session.flush()
                return
            elif partial_destination_path.is_file() or partial_destination_path.is_symlink():
                partial_destination_path.unlink(missing_ok=True)

        if partial_destination_path is not None:
            _cleanup_import_created_directories(
                payload,
                destination_parent=partial_destination_path.parent,
            )

    elif action_type == "series_preferred_root_updated":
        series_id = _positive_int(payload.get("series_id"), "series_id")
        old_root_id = _optional_positive_int(
            payload.get("old_preferred_library_root_id"),
            "old_preferred_library_root_id",
        )
        new_root_id = _positive_int(
            payload.get("new_preferred_library_root_id"),
            "new_preferred_library_root_id",
        )
        series = await session.get(Series, series_id)
        if series is not None:
            if series.preferred_library_root_id != new_root_id:
                action.status = ImportJobActionStatus.ROLLBACK_FAILED
                action.error_message = (
                    "The series preferred destination changed after import. "
                    "Pullbox preserved the current choice for review."
                )
                action.rolled_back_at = None
                await session.flush()
                return
            series.preferred_library_root_id = old_root_id

    elif action_type == "series_monitoring_updated":
        series_id = _positive_int(payload.get("series_id"), "series_id")
        import_series_id = _positive_int(
            payload.get("import_series_id"),
            "import_series_id",
        )
        previous_monitored = payload.get("previous_monitored")
        installed_monitored = payload.get("installed_monitored")
        if not isinstance(previous_monitored, bool) or not isinstance(
            installed_monitored,
            bool,
        ):
            raise ValueError("Monitoring rollback states must be booleans")
        if previous_monitored == installed_monitored:
            raise ValueError("Monitoring rollback states must differ")

        series = await session.get(Series, series_id)
        if series is not None:
            imported_series = await session.get(ImportedSeries, import_series_id)
            if (
                imported_series is None
                or imported_series.import_job_id != action.import_job_id
                or imported_series.series_id not in {None, series_id}
            ):
                action.status = ImportJobActionStatus.ROLLBACK_FAILED
                action.error_message = (
                    "The Series monitoring change is no longer owned by this import. "
                    "Pullbox preserved the current setting."
                )
                action.rolled_back_at = None
                await session.flush()
                return
            expected_monitoring_state = (
                previous_monitored
                if action.status == ImportJobActionStatus.ROLLED_BACK
                else installed_monitored
            )
            if bool(series.monitored) != expected_monitoring_state:
                action.status = ImportJobActionStatus.ROLLBACK_FAILED
                action.error_message = (
                    "Series monitoring changed after import. Pullbox preserved the current setting."
                )
                action.rolled_back_at = None
                await session.flush()
                return
            series.monitored = previous_monitored

    elif action_type == "series_folder_created":
        series_id = _positive_int(payload.get("series_id"), "series_id")
        import_series_id = _positive_int(
            payload.get("import_series_id"),
            "import_series_id",
        )
        previous_series_path = payload.get("previous_series_path")
        if previous_series_path is not None and not isinstance(previous_series_path, str):
            raise ValueError("previous_series_path must be a string or null")
        previous_library_root_id = _optional_positive_int(
            payload.get("previous_library_root_id"),
            "previous_library_root_id",
        )
        previous_preferred_root_id = _optional_positive_int(
            payload.get("previous_preferred_library_root_id"),
            "previous_preferred_library_root_id",
        )
        ownership = _validated_series_folder_action_ownership(
            payload.get("series_folder_ownership")
        )
        if ownership is None:
            action.status = ImportJobActionStatus.ROLLBACK_FAILED
            action.error_message = (
                "The import journal does not contain valid series-folder ownership evidence. "
                "Pullbox preserved the folder for manual recovery."
            )
            action.rolled_back_at = None
            await session.flush()
            return

        series = await session.get(Series, series_id)
        if series is not None:
            imported_series = await session.get(ImportedSeries, import_series_id)
            if (
                imported_series is None
                or imported_series.import_job_id != action.import_job_id
                or imported_series.series_id not in {None, series_id}
            ):
                action.status = ImportJobActionStatus.ROLLBACK_FAILED
                action.error_message = (
                    "The imported series folder is no longer owned by this import. "
                    "Pullbox preserved it for manual recovery."
                )
                action.rolled_back_at = None
                await session.flush()
                return
            installed_state = _series_matches_installed_folder_state(series, ownership)
            restored_state = _series_matches_folder_state(
                series,
                path=previous_series_path,
                library_root_id=previous_library_root_id,
                preferred_library_root_id=previous_preferred_root_id,
            )
            if not installed_state and not restored_state:
                action.status = ImportJobActionStatus.ROLLBACK_FAILED
                action.error_message = (
                    "The series folder or storage selection changed after import. "
                    "Pullbox preserved the current state."
                )
                action.rolled_back_at = None
                await session.flush()
                return

        folder_error = _series_folder_rollback_block_reason(
            payload,
            series=series,
            require_installed_state=False,
        )
        if folder_error is not None:
            action.status = ImportJobActionStatus.ROLLBACK_FAILED
            action.error_message = folder_error
            action.rolled_back_at = None
            await session.flush()
            return

        _cleanup_owned_series_folder_directories(payload)
        if series is not None:
            series.path = previous_series_path
            series.library_root_id = previous_library_root_id
            series.preferred_library_root_id = previous_preferred_root_id

    elif action_type == "series_cover_path_updated":
        series_id = _positive_int(payload.get("series_id"), "series_id")
        import_series_id = _positive_int(
            payload.get("import_series_id"),
            "import_series_id",
        )
        previous_cover_path = payload.get("previous_cover_path")
        installed_cover_path = payload.get("installed_cover_path")
        if previous_cover_path is not None and not isinstance(previous_cover_path, str):
            raise ValueError("previous_cover_path must be a string or null")
        if installed_cover_path is not None and not isinstance(installed_cover_path, str):
            raise ValueError("installed_cover_path must be a string or null")
        if previous_cover_path == installed_cover_path:
            raise ValueError("Cover-path rollback states must differ")

        series = await session.get(Series, series_id)
        if series is not None:
            imported_series = await session.get(ImportedSeries, import_series_id)
            if (
                imported_series is None
                or imported_series.import_job_id != action.import_job_id
                or imported_series.series_id not in {None, series_id}
            ):
                action.status = ImportJobActionStatus.ROLLBACK_FAILED
                action.error_message = (
                    "The Series cover-path change is no longer owned by this import. "
                    "Pullbox preserved the current value."
                )
                action.rolled_back_at = None
                await session.flush()
                return
            expected_cover_path = (
                previous_cover_path
                if action.status == ImportJobActionStatus.ROLLED_BACK
                else installed_cover_path
            )
            if series.cover_path != expected_cover_path:
                action.status = ImportJobActionStatus.ROLLBACK_FAILED
                action.error_message = (
                    "The Series cover path changed after import. Pullbox preserved the current "
                    "value."
                )
                action.rolled_back_at = None
                await session.flush()
                return
            series.cover_path = previous_cover_path

    elif action_type == "series_cover_cache_created":
        series_id = _positive_int(payload.get("series_id"), "series_id")
        import_series_id = _positive_int(
            payload.get("import_series_id"),
            "import_series_id",
        )
        installed_cover_path = payload.get("installed_cover_path")
        previous_cover_path = payload.get("previous_cover_path")
        if not isinstance(installed_cover_path, str) or not installed_cover_path:
            raise ValueError("installed_cover_path must be a non-empty string")
        if previous_cover_path is not None and not isinstance(previous_cover_path, str):
            raise ValueError("previous_cover_path must be a string or null")

        cover_error = _cover_cache_rollback_block_reason(payload)
        if cover_error is not None:
            action.status = ImportJobActionStatus.ROLLBACK_FAILED
            action.error_message = cover_error
            action.rolled_back_at = None
            await session.flush()
            return

        series = await session.get(Series, series_id)
        if series is not None:
            imported_series = await session.get(ImportedSeries, import_series_id)
            if (
                imported_series is None
                or imported_series.import_job_id != action.import_job_id
                or imported_series.series_id not in {None, series_id}
            ):
                action.status = ImportJobActionStatus.ROLLBACK_FAILED
                action.error_message = (
                    "The imported cover is no longer owned by this import. "
                    "Pullbox preserved it for manual recovery."
                )
                action.rolled_back_at = None
                await session.flush()
                return
            artifact_missing = not os.path.lexists(
                Path(payload["cover_cache_ownership"]["artifact_path"])
            )
            already_restored = series.cover_path == previous_cover_path and artifact_missing
            if series.cover_path != installed_cover_path and not already_restored:
                action.status = ImportJobActionStatus.ROLLBACK_FAILED
                action.error_message = (
                    "The series cover changed after import. Pullbox preserved the current cover."
                )
                action.rolled_back_at = None
                await session.flush()
                return

        _remove_owned_cover_cache_artifact(payload)
        if series is not None:
            series.cover_path = previous_cover_path

    elif action_type == "series_created":
        series_id = int(payload.get("series_id") or 0)
        if series_id:
            series = await session.scalar(_series_rollback_lock_statement(series_id))
            folder_error = _series_folder_rollback_block_reason(payload, series=series)
            if folder_error is not None:
                action.status = ImportJobActionStatus.ROLLBACK_FAILED
                action.error_message = folder_error
                action.rolled_back_at = None
                await session.flush()
                return
            cover_error = _cover_cache_rollback_block_reason(payload)
            if cover_error is not None:
                action.status = ImportJobActionStatus.ROLLBACK_FAILED
                action.error_message = cover_error
                action.rolled_back_at = None
                await session.flush()
                return
            if series is not None:
                unsafe_reason = await _created_series_rollback_block_reason(
                    session,
                    action=action,
                    series=series,
                    payload=payload,
                )
                if unsafe_reason is not None:
                    action.status = ImportJobActionStatus.ROLLBACK_FAILED
                    action.error_message = unsafe_reason
                    action.rolled_back_at = None
                    await session.flush()
                    return

                with contextlib.suppress(NotFoundError):
                    # SeriesService.delete has broader user-facing behavior: it can
                    # cancel downloads and recursively delete a folder. The guards
                    # above prove there are no related files/activity. Folder
                    # ownership is not durably proven, so even an empty directory is
                    # preserved rather than being inferred safe to remove.
                    await delete_series(
                        session,
                        series_id,
                        delete_files=False,
                        delete_folder=False,
                    )
                    # A series-created action can be replayed after a partial rollback.
                    # Missing here means the rollback objective is already satisfied.
            # This cleanup is intentionally replayable. A prior rollback may have
            # deleted the Series and its cover leaf before interruption, while the
            # import-owned empty cache ancestors still need removal.
            _remove_owned_cover_cache_artifact(payload)
            _cleanup_owned_series_folder_directories(payload)

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


def _validated_series_folder_ownership(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return None
    folder_path_raw = value.get("folder_path")
    boundary_path_raw = value.get("ownership_boundary_path")
    directory_paths_raw = value.get("created_directory_paths")
    if not isinstance(folder_path_raw, str) or not folder_path_raw:
        return None
    if not isinstance(boundary_path_raw, str) or not boundary_path_raw:
        return None
    if not isinstance(directory_paths_raw, list):
        return None

    try:
        folder_resolved = Path(folder_path_raw).resolve(strict=False)
        boundary_resolved = Path(boundary_path_raw).resolve(strict=False)
        folder_relative = folder_resolved.relative_to(boundary_resolved)
    except (OSError, RuntimeError, ValueError):
        return None
    if not folder_relative.parts:
        return None

    validated_paths: list[str] = []
    for raw_path in directory_paths_raw:
        if not isinstance(raw_path, str) or not raw_path:
            return None
        try:
            path_resolved = Path(raw_path).resolve(strict=False)
            relative = path_resolved.relative_to(boundary_resolved)
            folder_resolved.relative_to(path_resolved)
        except (OSError, RuntimeError, ValueError):
            return None
        if not relative.parts:
            return None
        if raw_path not in validated_paths:
            validated_paths.append(raw_path)
    if validated_paths:
        try:
            last_resolved = Path(validated_paths[-1]).resolve(strict=False)
        except (OSError, RuntimeError):
            return None
        if last_resolved != folder_resolved:
            return None

    return {
        "schema_version": 1,
        "folder_path": folder_path_raw,
        "ownership_boundary_path": boundary_path_raw,
        "created_directory_paths": validated_paths,
    }


def _series_path_matches_folder_ownership(
    series: Series,
    ownership: dict[str, Any],
) -> bool:
    if not series.path:
        return False
    try:
        return Path(series.path).resolve(strict=False) == Path(ownership["folder_path"]).resolve(
            strict=False
        )
    except (OSError, RuntimeError):
        return False


def _folder_ownership_with_installed_state(
    ownership: dict[str, Any],
    series: Series,
) -> dict[str, Any]:
    return {
        **ownership,
        "installed_library_root_id": series.library_root_id,
        "installed_preferred_library_root_id": series.preferred_library_root_id,
    }


def _validated_series_folder_action_ownership(value: object) -> dict[str, Any] | None:
    ownership = _validated_series_folder_ownership(value)
    if ownership is None or not isinstance(value, dict):
        return None
    if (
        "installed_library_root_id" not in value
        or "installed_preferred_library_root_id" not in value
    ):
        return None
    installed_root_id = value.get("installed_library_root_id")
    installed_preferred_root_id = value.get("installed_preferred_library_root_id")
    if installed_root_id is not None and (
        not isinstance(installed_root_id, int)
        or isinstance(installed_root_id, bool)
        or installed_root_id <= 0
    ):
        return None
    if installed_preferred_root_id is not None and (
        not isinstance(installed_preferred_root_id, int)
        or isinstance(installed_preferred_root_id, bool)
        or installed_preferred_root_id <= 0
    ):
        return None
    return {
        **ownership,
        "installed_library_root_id": installed_root_id,
        "installed_preferred_library_root_id": installed_preferred_root_id,
    }


def _series_matches_folder_state(
    series: Series,
    *,
    path: str | None,
    library_root_id: int | None,
    preferred_library_root_id: int | None,
) -> bool:
    if series.library_root_id != library_root_id:
        return False
    if series.preferred_library_root_id != preferred_library_root_id:
        return False
    if series.path is None or path is None:
        return series.path is path
    try:
        return Path(series.path).resolve(strict=False) == Path(path).resolve(strict=False)
    except (OSError, RuntimeError):
        return False


def _series_matches_installed_folder_state(
    series: Series,
    ownership: dict[str, Any],
) -> bool:
    return _series_matches_folder_state(
        series,
        path=ownership["folder_path"],
        library_root_id=ownership["installed_library_root_id"],
        preferred_library_root_id=ownership["installed_preferred_library_root_id"],
    )


def _series_folder_rollback_block_reason(
    payload: dict[str, Any],
    *,
    series: Series | None,
    require_installed_state: bool = True,
) -> str | None:
    raw_ownership = payload.get("series_folder_ownership")
    if raw_ownership is None:
        return None
    ownership = _validated_series_folder_action_ownership(raw_ownership)
    if ownership is None:
        return (
            "The import journal does not contain valid series-folder ownership evidence. "
            "Pullbox preserved the folder for manual recovery."
        )
    if (
        require_installed_state
        and series is not None
        and not _series_matches_installed_folder_state(series, ownership)
    ):
        return (
            "The import-created series folder or storage selection changed after import. "
            "Pullbox preserved the current state."
        )
    if not ownership["created_directory_paths"]:
        return None

    folder_path = Path(ownership["folder_path"])
    if not os.path.lexists(folder_path):
        return None
    if folder_path.is_symlink() or not folder_path.is_dir():
        return (
            "The import-created series folder changed after import. Pullbox preserved the "
            "current path for manual recovery."
        )
    try:
        next(folder_path.iterdir())
    except StopIteration:
        return None
    except OSError:
        return (
            "The import-created series folder could not be verified as empty. Pullbox "
            "preserved it for manual recovery."
        )
    return (
        "The import-created series folder contains later files. Pullbox preserved the series "
        "and folder for manual recovery."
    )


def _cleanup_owned_series_folder_directories(payload: dict[str, Any]) -> None:
    ownership = _validated_series_folder_action_ownership(payload.get("series_folder_ownership"))
    if ownership is None:
        return
    _remove_empty_directories(Path(path) for path in ownership["created_directory_paths"])


def _validated_cover_cache_ownership(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return None
    base_path_raw = value.get("base_path")
    boundary_path_raw = value.get("ownership_boundary_path")
    directory_paths_raw = value.get("created_directory_paths")
    artifact_path_raw = value.get("artifact_path")
    artifact_signature = value.get("artifact_signature")
    if not isinstance(base_path_raw, str) or not base_path_raw:
        return None
    if not isinstance(boundary_path_raw, str) or not boundary_path_raw:
        return None
    if not isinstance(directory_paths_raw, list):
        return None
    if not isinstance(artifact_path_raw, str) or not artifact_path_raw:
        return None
    if not isinstance(artifact_signature, dict) or not artifact_signature:
        return None

    base_path = Path(base_path_raw)
    boundary_path = Path(boundary_path_raw)
    try:
        base_resolved = base_path.resolve(strict=False)
        boundary_resolved = boundary_path.resolve(strict=False)
        base_resolved.relative_to(boundary_resolved)
        artifact_resolved = Path(artifact_path_raw).resolve(strict=False)
        artifact_relative = artifact_resolved.relative_to(base_resolved)
    except (OSError, RuntimeError, ValueError):
        return None
    if not artifact_relative.parts:
        return None
    artifact_parent_resolved = artifact_resolved.parent
    validated_paths: list[str] = []
    for raw_path in directory_paths_raw:
        if not isinstance(raw_path, str) or not raw_path:
            return None
        path = Path(raw_path)
        try:
            path_resolved = path.resolve(strict=False)
            relative_path = path_resolved.relative_to(boundary_resolved)
        except (OSError, RuntimeError, ValueError):
            return None
        if not relative_path.parts:
            return None
        try:
            artifact_parent_resolved.relative_to(path_resolved)
        except ValueError:
            return None
        if raw_path not in validated_paths:
            validated_paths.append(raw_path)
    return {
        "schema_version": 1,
        "base_path": base_path_raw,
        "ownership_boundary_path": boundary_path_raw,
        "created_directory_paths": validated_paths,
        "artifact_path": artifact_path_raw,
        "artifact_signature": dict(artifact_signature),
    }


def _cover_cache_rollback_block_reason(payload: dict[str, Any]) -> str | None:
    raw_ownership = payload.get("cover_cache_ownership")
    if raw_ownership is None:
        return None
    ownership = _validated_cover_cache_ownership(raw_ownership)
    if ownership is None:
        return (
            "The import journal does not contain valid cover ownership evidence. "
            "Pullbox preserved the cover for manual recovery."
        )
    artifact_path = Path(ownership["artifact_path"])
    if not os.path.lexists(artifact_path):
        return None
    if not artifact_path.is_file() or not _path_matches_signature(
        artifact_path,
        ownership["artifact_signature"],
    ):
        return (
            "The imported cover changed after import. Pullbox preserved the current cover "
            "for manual recovery."
        )
    return None


def _remove_owned_cover_cache_artifact(payload: dict[str, Any]) -> None:
    ownership = _validated_cover_cache_ownership(payload.get("cover_cache_ownership"))
    if ownership is None:
        return
    artifact_path = Path(ownership["artifact_path"])
    if os.path.lexists(artifact_path) and artifact_path.is_file():
        artifact_path.unlink()
    _remove_empty_directories(Path(path) for path in ownership["created_directory_paths"])


def _cleanup_import_created_directories(
    payload: dict[str, Any],
    *,
    destination_parent: Path,
) -> None:
    raw_paths = payload.get("created_directory_paths")
    if raw_paths is None:
        if not bool(payload.get("created_series_folder")):
            return
        legacy_path = str(payload.get("created_series_folder_path") or "")
        if not legacy_path:
            return
        try:
            if Path(legacy_path).resolve(strict=False) != destination_parent.resolve(strict=False):
                return
        except (OSError, RuntimeError):
            return
        _remove_empty_directories((Path(legacy_path),))
        return
    if not isinstance(raw_paths, list):
        return

    boundary_raw = payload.get("directory_ownership_boundary_path")
    if not isinstance(boundary_raw, str) or not boundary_raw:
        return
    try:
        boundary_resolved = Path(boundary_raw).resolve(strict=False)
        destination_parent_resolved = destination_parent.resolve(strict=False)
        destination_relative = destination_parent_resolved.relative_to(boundary_resolved)
    except (OSError, RuntimeError, ValueError):
        return
    if not destination_relative.parts:
        return

    owned_paths: list[Path] = []
    for raw_path in raw_paths:
        if not isinstance(raw_path, str) or not raw_path:
            continue
        path = Path(raw_path)
        try:
            path_resolved = path.resolve(strict=False)
            relative = path_resolved.relative_to(boundary_resolved)
            destination_parent_resolved.relative_to(path_resolved)
        except (OSError, RuntimeError, ValueError):
            continue
        if not relative.parts:
            continue
        owned_paths.append(path)
    _remove_empty_directories(owned_paths)


def _remove_empty_directories(paths: Iterable[Path]) -> None:
    unique_paths = {Path(path) for path in paths}
    for directory in sorted(unique_paths, key=lambda path: len(path.parts), reverse=True):
        try:
            directory.rmdir()
        except (FileNotFoundError, OSError):
            continue


def _series_user_owned_snapshot(series: Series) -> dict[str, Any]:
    status_override = series.status_override
    return {
        "schema_version": 1,
        "comicvine_id": series.comicvine_id,
        "monitored": bool(series.monitored),
        "status_override": status_override.value if status_override is not None else None,
        "alternate_names": list(series.alternate_names or []),
        "parent_series_id": series.parent_series_id,
        "preferred_library_root_id": series.preferred_library_root_id,
    }


async def _created_series_rollback_block_reason(
    session: AsyncSession,
    *,
    action: ImportJobAction,
    series: Series,
    payload: dict[str, Any],
) -> str | None:
    """Return why a job-created series is no longer safe to remove."""
    # The locked Series row prevents concurrent Series changes and new rows
    # that reference it. Locking its Issues also prevents concurrent issue
    # state changes and new dependent activity until deletion completes.
    await session.execute(_series_issue_rollback_lock_statement(series.id))

    import_series_id = _optional_positive_int(payload.get("import_series_id"), "import_series_id")
    expected_snapshot = payload.get("series_ownership_snapshot")
    expected_issue_snapshot = payload.get("issue_ownership_snapshot")
    if (
        import_series_id is None
        or not isinstance(expected_snapshot, dict)
        or not isinstance(expected_issue_snapshot, dict)
    ):
        return (
            "The import journal does not contain enough series ownership evidence. "
            "Pullbox preserved the series for manual recovery."
        )

    imported_series = await session.get(ImportedSeries, import_series_id)
    if (
        imported_series is None
        or imported_series.import_job_id != action.import_job_id
        or imported_series.series_id not in {None, series.id}
    ):
        return (
            "The import-created series is no longer owned exclusively by this import. "
            "Pullbox preserved it for manual recovery."
        )
    if expected_snapshot != _series_user_owned_snapshot(series):
        return (
            "The import-created series changed after import. Pullbox preserved the current "
            "series and its data for manual recovery."
        )

    issue_state_changed = await _created_series_issue_state_changed(
        session,
        action=action,
        series=series,
        import_series_id=import_series_id,
        expected_snapshot=expected_issue_snapshot,
        monitored=bool(expected_snapshot.get("monitored")),
    )
    if issue_state_changed:
        return (
            "An issue in the import-created series changed after import. Pullbox preserved "
            "the series and its issue state for manual recovery."
        )

    other_import_reference = await session.scalar(
        sa_select(ImportedSeries.id)
        .where(
            ImportedSeries.series_id == series.id,
            ImportedSeries.import_job_id != action.import_job_id,
        )
        .limit(1)
    )
    child_series = await session.scalar(
        sa_select(Series.id).where(Series.parent_series_id == series.id).limit(1)
    )
    issue_ids = sa_select(Issue.id).where(Issue.series_id == series.id)
    related_activity_checks = (
        sa_select(LibraryFile.id).where(LibraryFile.issue_id.in_(issue_ids)).limit(1),
        sa_select(DownloadHistory.id).where(DownloadHistory.issue_id.in_(issue_ids)).limit(1),
        sa_select(IssueReaderState.id).where(IssueReaderState.issue_id.in_(issue_ids)).limit(1),
        sa_select(PendingMatch.id).where(PendingMatch.issue_id.in_(issue_ids)).limit(1),
        sa_select(SearchLog.id).where(SearchLog.issue_id.in_(issue_ids)).limit(1),
        sa_select(DirectAcquisitionAttempt.id)
        .where(DirectAcquisitionAttempt.issue_id.in_(issue_ids))
        .limit(1),
        sa_select(IssueStoryArc.id).where(IssueStoryArc.issue_id.in_(issue_ids)).limit(1),
        sa_select(BlocklistEntry.id)
        .where(
            or_(
                BlocklistEntry.series_id == series.id,
                BlocklistEntry.issue_id.in_(issue_ids),
            )
        )
        .limit(1),
        sa_select(Issue.id)
        .where(Issue.series_id == series.id, Issue.manual_skip.is_(True))
        .limit(1),
    )
    has_related_activity = False
    for statement in related_activity_checks:
        if await session.scalar(statement) is not None:
            has_related_activity = True
            break
    if other_import_reference is not None or child_series is not None or has_related_activity:
        return (
            "The import-created series has later files or activity. Pullbox preserved the "
            "series and related data for manual recovery."
        )
    return None


async def _created_series_issue_state_changed(
    session: AsyncSession,
    *,
    action: ImportJobAction,
    series: Series,
    import_series_id: int,
    expected_snapshot: dict[str, Any],
    monitored: bool,
) -> bool:
    """Detect user-owned issue-state changes while allowing import-owned OWNED state."""
    imported_issue_ids = set(
        (
            await session.scalars(
                sa_select(ImportedFile.matched_issue_id).where(
                    ImportedFile.import_job_id == action.import_job_id,
                    ImportedFile.import_series_id == import_series_id,
                    ImportedFile.status == ImportedFileStatus.IMPORTED,
                    ImportedFile.matched_issue_id.is_not(None),
                )
            )
        ).all()
    )
    current_rows = (
        await session.execute(
            sa_select(Issue.id, Issue.status, Issue.manual_skip)
            .where(Issue.series_id == series.id)
            .order_by(Issue.id)
        )
    ).all()
    current_ids = {str(issue_id) for issue_id, _status, _manual_skip in current_rows}
    if not set(expected_snapshot).issubset(current_ids):
        return True

    default_status = IssueStatus.WANTED if monitored else IssueStatus.SKIPPED
    for issue_id, status, manual_skip in current_rows:
        expected = expected_snapshot.get(str(issue_id))
        if expected is None:
            if issue_id in imported_issue_ids:
                if bool(manual_skip) or status != IssueStatus.OWNED:
                    return True
            elif bool(manual_skip) or status != default_status:
                return True
            continue
        if not isinstance(expected, dict):
            return True
        expected_manual_skip = expected.get("manual_skip")
        expected_status = expected.get("status")
        if not isinstance(expected_manual_skip, bool) or not isinstance(expected_status, str):
            return True
        if bool(manual_skip) != expected_manual_skip:
            return True
        if issue_id in imported_issue_ids:
            if status != IssueStatus.OWNED:
                return True
        elif status.value != expected_status:
            return True
    return False


def _managed_destination_matches_rollback_action(
    destination_path: Path,
    *,
    library_file: LibraryFile | None,
    expected_signature: object,
) -> bool:
    """Require path, ownership, and creation signature before managed removal."""
    if library_file is None or library_file.storage_mode != LibraryFileStorageMode.MANAGED:
        return False
    if Path(library_file.file_path).resolve(strict=False) != destination_path.resolve(strict=False):
        return False
    if not isinstance(expected_signature, dict) or not expected_signature:
        return False
    try:
        current_signature = build_managed_placement_signature(destination_path)
    except (ConfigurationError, OSError, RuntimeError, ValueError):
        return False
    return expected_signature == current_signature


def _path_matches_signature(path: Path, expected_signature: object) -> bool:
    if not isinstance(expected_signature, dict) or not expected_signature:
        return False
    try:
        return build_managed_placement_signature(path) == expected_signature
    except (ConfigurationError, OSError, RuntimeError, ValueError):
        return False


async def _rollback_import_managed_story_arc_placement(
    session: AsyncSession,
    action: ImportJobAction,
    payload: dict[str, Any],
) -> None:
    """Cancel or remove exactly one placement owned by an import action.

    The durable sync-work row is the rollback checkpoint. Running work is
    asked to cancel and deliberately stops the reverse action walk until its
    worker reaches a terminal state. Filesystem removal is delegated to the
    ownership- and fingerprint-aware Story Arc placement lifecycle.
    """
    if action.status is ImportJobActionStatus.ROLLED_BACK:
        return
    if action.status is not ImportJobActionStatus.COMPLETED:
        raise ValueError("Import Story Arc placement action is not rollback-eligible")
    parsed = _managed_placement_rollback_payload(action, payload)
    work = await session.get(StoryArcSyncWork, parsed["sync_work_id"])
    if work is None:
        raise ValueError("Import Story Arc placement work is missing; rollback refused")
    action_id = int(action.id)
    _require_import_story_arc_work_identity(action_id, work, parsed)

    membership = await session.get(IssueStoryArc, parsed["membership_id"])
    if membership is None:
        raise ValueError("Import Story Arc placement membership is missing; rollback refused")
    library_file = await session.get(LibraryFile, work.library_file_id)
    if (
        library_file is None
        or membership.issue_id is None
        or library_file.issue_id != membership.issue_id
    ):
        raise ValueError("Import Story Arc canonical binding changed; rollback refused")
    membership_id = int(membership.id)
    story_arc_id = int(membership.story_arc_id)
    await _require_import_story_arc_staged_binding(
        session,
        action=action,
        story_arc_id=story_arc_id,
        membership_id=membership_id,
        imported_story_arc_id=parsed["imported_story_arc_id"],
        imported_story_arc_entry_id=parsed["imported_story_arc_entry_id"],
    )

    pre_fence_work_state = work.state
    work = await _fence_import_story_arc_work_for_rollback(
        session,
        action=action,
        work=work,
        payload=parsed,
    )
    current_action = await session.get(ImportJobAction, action_id)
    if current_action is None:
        raise ValueError("Import Story Arc placement action disappeared during rollback")
    placements = list(
        (
            await session.scalars(
                sa_select(StoryArcPlacement)
                .where(StoryArcPlacement.creating_action_id == current_action.id)
                .order_by(StoryArcPlacement.id.asc())
                .limit(2)
            )
        ).all()
    )
    if len(placements) > 1:
        raise ValueError("Import Story Arc placement action owns multiple rows; rollback refused")
    placement = placements[0] if placements else None

    if work.state not in {
        StoryArcSyncWorkState.COMPLETED,
        StoryArcSyncWorkState.CANCELLED,
    }:
        raise ValueError("Import Story Arc placement work is not terminal; rollback refused")
    if placement is None:
        if _completed_story_arc_removal_checkpoint_matches(
            work,
            action=current_action,
            membership_id=membership_id,
        ):
            return
        prepared_removal = _prepared_story_arc_removal_checkpoint(
            work,
            action=current_action,
            membership_id=membership_id,
        )
        if prepared_removal is not None:
            placement_id, placement_ownership = prepared_removal
            recovered_status = _recovered_story_arc_removal_status(
                membership,
                placement_id=placement_id,
                placement_ownership=placement_ownership,
            )
            await _persist_story_arc_work_rollback_checkpoint(
                session,
                work=work,
                action=current_action,
                membership_id=membership_id,
                status=recovered_status,
                placement_id=placement_id,
                placement_ownership=placement_ownership.value,
            )
            return
        if work.state is StoryArcSyncWorkState.CANCELLED:
            await _persist_story_arc_work_rollback_checkpoint(
                session,
                work=work,
                action=current_action,
                membership_id=membership_id,
                status="cancelled_before_publish",
            )
            return
        raise ValueError("Import Story Arc placement evidence is missing; rollback refused")

    _require_action_owned_placement_identity(
        placement,
        action=current_action,
        work=work,
        membership_id=membership_id,
    )
    abandoned_published_operation_token = _terminal_published_operation_token(
        placement,
        work=work,
        pre_fence_work_state=pre_fence_work_state,
    )
    removal_status = (
        "referenced_placement_detached"
        if placement.ownership is StoryArcPlacementOwnership.REFERENCED
        else "managed_placement_removed"
    )
    placement_id = int(placement.id)
    placement_ownership = placement.ownership
    await _persist_story_arc_work_rollback_checkpoint(
        session,
        work=work,
        action=current_action,
        membership_id=membership_id,
        status="placement_removal_prepared",
        placement_id=placement_id,
        placement_ownership=placement_ownership.value,
    )

    # Local import avoids the sync queue -> import action helper cycle.
    from pullbox.services.story_arc_placement_integration import (
        StoryArcPlacementSyncService,
    )

    await StoryArcPlacementSyncService().remove_placement(
        session,
        story_arc_id,
        placement_id,
        confirm_managed_artifact_removal=(
            placement_ownership is StoryArcPlacementOwnership.MANAGED
        ),
        abandoned_published_operation_token=abandoned_published_operation_token,
    )
    session.expire_all()
    current_work = await session.get(StoryArcSyncWork, int(parsed["sync_work_id"]))
    if current_work is None:
        raise ValueError("Import Story Arc placement work disappeared during rollback")
    _require_import_story_arc_work_identity(action_id, current_work, parsed)
    reloaded_action = await session.get(ImportJobAction, action_id)
    if reloaded_action is None:
        raise ValueError("Import Story Arc placement action disappeared during rollback")
    await _persist_story_arc_work_rollback_checkpoint(
        session,
        work=current_work,
        action=reloaded_action,
        membership_id=membership_id,
        status=removal_status,
        placement_id=placement_id,
        placement_ownership=placement_ownership.value,
    )


def _managed_placement_rollback_payload(
    action: ImportJobAction,
    payload: dict[str, Any],
) -> _ManagedPlacementRollbackPayload:
    if action.phase != _STORY_ARC_PLACEMENT_PHASE:
        raise ValueError("Import Story Arc placement action phase changed; rollback refused")
    if set(payload) != _STORY_ARC_MANAGED_PLACEMENT_PAYLOAD_KEYS:
        raise ValueError("Import Story Arc placement payload shape changed; rollback refused")
    schema_version = _non_negative_int(payload.get("schema_version"), "schema_version")
    if schema_version != 1:
        raise ValueError("Import Story Arc placement payload version changed; rollback refused")
    desired_generation = _required_string(payload, "desired_generation")
    if len(desired_generation) != 64:
        raise ValueError("Import Story Arc placement generation changed; rollback refused")
    source_import_job_id = _positive_int(
        payload.get("source_import_job_id"),
        "source_import_job_id",
    )
    if source_import_job_id != action.import_job_id:
        raise ValueError("Import Story Arc placement job changed; rollback refused")
    return {
        "sync_work_id": _positive_int(payload.get("sync_work_id"), "sync_work_id"),
        "membership_id": _positive_int(payload.get("membership_id"), "membership_id"),
        "desired_generation": desired_generation,
        "imported_story_arc_id": _positive_int(
            payload.get("imported_story_arc_id"),
            "imported_story_arc_id",
        ),
        "imported_story_arc_entry_id": _positive_int(
            payload.get("imported_story_arc_entry_id"),
            "imported_story_arc_entry_id",
        ),
        "source_import_job_id": source_import_job_id,
    }


def _require_import_story_arc_work_identity(
    action_id: int,
    work: StoryArcSyncWork,
    payload: _ManagedPlacementRollbackPayload,
) -> None:
    if (
        work.origin_import_action_id != action_id
        or work.origin_import_job_id != payload["source_import_job_id"]
        or work.origin_imported_story_arc_id != payload["imported_story_arc_id"]
        or work.origin_imported_story_arc_entry_id != payload["imported_story_arc_entry_id"]
        or work.issue_story_arc_id != payload["membership_id"]
    ):
        raise ValueError("Import Story Arc placement work ownership changed; rollback refused")
    if work.desired_generation != payload["desired_generation"]:
        raise ValueError("Import Story Arc placement generation changed; rollback refused")


async def _require_import_story_arc_staged_binding(
    session: AsyncSession,
    *,
    action: ImportJobAction,
    story_arc_id: int,
    membership_id: int,
    imported_story_arc_id: int,
    imported_story_arc_entry_id: int,
) -> None:
    staged_entry_id = await session.scalar(
        sa_select(ImportedStoryArcEntry.id)
        .join(
            ImportedStoryArc,
            ImportedStoryArc.id == ImportedStoryArcEntry.imported_story_arc_id,
        )
        .where(
            ImportedStoryArc.id == imported_story_arc_id,
            ImportedStoryArc.import_job_id == action.import_job_id,
            ImportedStoryArc.materialized_story_arc_id == story_arc_id,
            ImportedStoryArcEntry.id == imported_story_arc_entry_id,
            ImportedStoryArcEntry.materialized_membership_id == membership_id,
        )
    )
    if staged_entry_id is None:
        raise ValueError("Import Story Arc staged provenance changed; rollback refused")


async def _fence_import_story_arc_work_for_rollback(
    session: AsyncSession,
    *,
    action: ImportJobAction,
    work: StoryArcSyncWork,
    payload: _ManagedPlacementRollbackPayload,
) -> StoryArcSyncWork:
    """Atomically stop claimable work or durably request running cancellation."""
    action_id = int(action.id)
    work_id = int(work.id)
    for _attempt in range(3):
        state = work.state
        now = datetime.now(UTC)
        if state is not StoryArcSyncWorkState.RUNNING and work.claim_token is not None:
            raise StoryArcManagedPlacementRollbackDeferredError(work_id)
        if state is StoryArcSyncWorkState.RUNNING:
            result = await session.execute(
                sa_update(StoryArcSyncWork)
                .where(
                    StoryArcSyncWork.id == work.id,
                    StoryArcSyncWork.origin_import_action_id == action_id,
                    StoryArcSyncWork.origin_import_job_id == payload["source_import_job_id"],
                    StoryArcSyncWork.origin_imported_story_arc_id
                    == payload["imported_story_arc_id"],
                    StoryArcSyncWork.origin_imported_story_arc_entry_id
                    == payload["imported_story_arc_entry_id"],
                    StoryArcSyncWork.issue_story_arc_id == payload["membership_id"],
                    StoryArcSyncWork.desired_generation == payload["desired_generation"],
                    StoryArcSyncWork.state == StoryArcSyncWorkState.RUNNING,
                )
                .values(cancel_requested_at=now)
            )
            await session.commit()
            if result.rowcount == 1:  # type: ignore[attr-defined]
                raise StoryArcManagedPlacementRollbackDeferredError(work_id)
        elif state in _CANCELLABLE_STORY_ARC_WORK_STATES:
            checkpoint = _story_arc_work_rollback_result(
                work,
                action=action,
                membership_id=int(payload["membership_id"]),
                status="work_fenced_for_rollback",
            )
            result = await session.execute(
                sa_update(StoryArcSyncWork)
                .where(
                    StoryArcSyncWork.id == work.id,
                    StoryArcSyncWork.origin_import_action_id == action_id,
                    StoryArcSyncWork.origin_import_job_id == payload["source_import_job_id"],
                    StoryArcSyncWork.origin_imported_story_arc_id
                    == payload["imported_story_arc_id"],
                    StoryArcSyncWork.origin_imported_story_arc_entry_id
                    == payload["imported_story_arc_entry_id"],
                    StoryArcSyncWork.issue_story_arc_id == payload["membership_id"],
                    StoryArcSyncWork.desired_generation == payload["desired_generation"],
                    StoryArcSyncWork.state == state,
                )
                .values(
                    state=StoryArcSyncWorkState.CANCELLED,
                    claim_token=None,
                    claimed_at=None,
                    next_attempt_at=None,
                    cancel_requested_at=now,
                    last_error_code="import_rollback_requested",
                    last_error_category="cancelled",
                    last_error_detail="Import rollback fenced Story Arc placement work.",
                    last_result=checkpoint,
                )
            )
            await session.commit()
            if result.rowcount == 1:  # type: ignore[attr-defined]
                session.expire_all()
                cancelled = await session.get(StoryArcSyncWork, work_id)
                if cancelled is None:
                    raise ValueError("Import Story Arc placement work disappeared during rollback")
                return cancelled
        else:
            return work
        session.expire_all()
        reloaded = await session.get(StoryArcSyncWork, work_id)
        if reloaded is None:
            raise ValueError("Import Story Arc placement work disappeared during rollback")
        _require_import_story_arc_work_identity(action_id, reloaded, payload)
        current_action = await session.get(ImportJobAction, action_id)
        if current_action is None:
            raise ValueError("Import Story Arc placement action disappeared during rollback")
        action = current_action
        work = reloaded
    raise ValueError("Import Story Arc placement work changed concurrently; rollback refused")


def _require_action_owned_placement_identity(
    placement: StoryArcPlacement,
    *,
    action: ImportJobAction,
    work: StoryArcSyncWork,
    membership_id: int,
) -> None:
    if (
        placement.issue_story_arc_id != membership_id
        or placement.library_file_id != work.library_file_id
        or placement.source_import_job_id != action.import_job_id
        or placement.creating_action_id != action.id
        or placement.source_kind is not StoryArcSourceKind.PULLBOX
    ):
        raise ValueError("Import Story Arc placement provenance changed; rollback refused")
    if placement.ownership is StoryArcPlacementOwnership.MANAGED:
        if placement.mode is StoryArcPlacementMode.REFERENCE_ONLY:
            raise ValueError("Managed Story Arc placement mode changed; rollback refused")
    elif (
        placement.ownership is not StoryArcPlacementOwnership.REFERENCED
        or placement.mode is not StoryArcPlacementMode.REFERENCE_ONLY
    ):
        raise ValueError("Referenced Story Arc placement mode changed; rollback refused")


def _terminal_published_operation_token(
    placement: StoryArcPlacement,
    *,
    work: StoryArcSyncWork,
    pre_fence_work_state: StoryArcSyncWorkState,
) -> str | None:
    """Return only a terminal, unclaimed, exactly checkpointed publish token."""
    if (
        work.state
        not in {
            StoryArcSyncWorkState.COMPLETED,
            StoryArcSyncWorkState.CANCELLED,
        }
        or work.claim_token is not None
    ):
        raise StoryArcManagedPlacementRollbackDeferredError(int(work.id))
    observed_token = placement.operation_token
    if observed_token is None:
        return None
    if pre_fence_work_state not in {
        StoryArcSyncWorkState.COMPLETED,
        StoryArcSyncWorkState.CANCELLED,
        StoryArcSyncWorkState.FAILED,
    }:
        raise ValueError(
            "Import Story Arc published checkpoint did not belong to terminal work; "
            "rollback refused"
        )
    last_result = dict(placement.last_result or {})
    target_fingerprint = last_result.get("target_fingerprint")
    if (
        not observed_token
        or last_result.get("schema_version") != 1
        or last_result.get("status") != "published_pending_reconcile"
        or last_result.get("operation_token") != observed_token
        or not isinstance(target_fingerprint, dict)
        or not target_fingerprint
    ):
        raise ValueError(
            "Import Story Arc placement token is not an exact published checkpoint; "
            "rollback refused"
        )
    return observed_token


async def _persist_story_arc_work_rollback_checkpoint(
    session: AsyncSession,
    *,
    work: StoryArcSyncWork,
    action: ImportJobAction,
    membership_id: int,
    status: str,
    placement_id: int | None = None,
    placement_ownership: str | None = None,
) -> None:
    work.cancel_requested_at = work.cancel_requested_at or datetime.now(UTC)
    work.last_result = _story_arc_work_rollback_result(
        work,
        action=action,
        membership_id=membership_id,
        status=status,
        placement_id=placement_id,
        placement_ownership=placement_ownership,
    )
    await session.commit()


def _story_arc_work_rollback_result(
    work: StoryArcSyncWork,
    *,
    action: ImportJobAction,
    membership_id: int,
    status: str,
    placement_id: int | None = None,
    placement_ownership: str | None = None,
) -> dict[str, object]:
    marker: dict[str, object] = {
        "schema_version": 1,
        "status": status,
        "import_job_id": action.import_job_id,
        "import_action_id": action.id,
        "sync_work_id": work.id,
        "membership_id": membership_id,
        "desired_generation": work.desired_generation,
    }
    if placement_id is not None:
        marker["placement_id"] = placement_id
    if placement_ownership is not None:
        marker["placement_ownership"] = placement_ownership
    return {**dict(work.last_result or {}), "rollback": marker}


def _completed_story_arc_removal_checkpoint_matches(
    work: StoryArcSyncWork,
    *,
    action: ImportJobAction,
    membership_id: int,
) -> bool:
    marker = dict(work.last_result or {}).get("rollback")
    if not isinstance(marker, dict) or marker.get("status") not in {
        "managed_placement_removed",
        "referenced_placement_detached",
    }:
        return False
    expected_ownership = (
        StoryArcPlacementOwnership.MANAGED
        if marker.get("status") == "managed_placement_removed"
        else StoryArcPlacementOwnership.REFERENCED
    )
    if not _story_arc_removal_marker_matches(
        marker,
        work=work,
        action=action,
        membership_id=membership_id,
        expected_status=str(marker["status"]),
        expected_ownership=expected_ownership,
    ):
        raise ValueError("Import Story Arc completed removal checkpoint changed")
    return True


def _prepared_story_arc_removal_checkpoint(
    work: StoryArcSyncWork,
    *,
    action: ImportJobAction,
    membership_id: int,
) -> tuple[int, StoryArcPlacementOwnership] | None:
    marker = dict(work.last_result or {}).get("rollback")
    if not isinstance(marker, dict) or marker.get("status") != "placement_removal_prepared":
        return None
    ownership_raw = marker.get("placement_ownership")
    if not isinstance(ownership_raw, str):
        raise ValueError("Import Story Arc prepared removal checkpoint changed")
    try:
        ownership = StoryArcPlacementOwnership(ownership_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("Import Story Arc prepared removal checkpoint changed") from exc
    if ownership not in {
        StoryArcPlacementOwnership.MANAGED,
        StoryArcPlacementOwnership.REFERENCED,
    } or not _story_arc_removal_marker_matches(
        marker,
        work=work,
        action=action,
        membership_id=membership_id,
        expected_status="placement_removal_prepared",
        expected_ownership=ownership,
    ):
        raise ValueError("Import Story Arc prepared removal checkpoint changed")
    return int(marker["placement_id"]), ownership


def _story_arc_removal_marker_matches(
    marker: dict[str, object],
    *,
    work: StoryArcSyncWork,
    action: ImportJobAction,
    membership_id: int,
    expected_status: str,
    expected_ownership: StoryArcPlacementOwnership,
) -> bool:
    placement_id = marker.get("placement_id")
    return bool(
        set(marker)
        == {
            "schema_version",
            "status",
            "import_job_id",
            "import_action_id",
            "sync_work_id",
            "membership_id",
            "desired_generation",
            "placement_id",
            "placement_ownership",
        }
        and marker.get("schema_version") == 1
        and marker.get("status") == expected_status
        and marker.get("import_job_id") == action.import_job_id
        and marker.get("import_action_id") == action.id
        and marker.get("sync_work_id") == work.id
        and marker.get("membership_id") == membership_id
        and marker.get("desired_generation") == work.desired_generation
        and isinstance(placement_id, int)
        and not isinstance(placement_id, bool)
        and placement_id > 0
        and marker.get("placement_ownership") == expected_ownership.value
    )


def _recovered_story_arc_removal_status(
    membership: IssueStoryArc,
    *,
    placement_id: int,
    placement_ownership: StoryArcPlacementOwnership,
) -> str:
    """Validate the transactionally paired membership checkpoint after row deletion."""
    result = dict(membership.last_materialization_result or {})
    if placement_ownership is StoryArcPlacementOwnership.MANAGED:
        valid = (
            set(result)
            == {
                "schema_version",
                "status",
                "placement_id",
                "artifact_removed",
                "canonical_preserved",
            }
            and result.get("schema_version") == 1
            and result.get("status") == "placement_removed"
            and result.get("placement_id") == placement_id
            and isinstance(result.get("artifact_removed"), bool)
            and result.get("canonical_preserved") is True
        )
        recovered_status = "managed_placement_removed"
    else:
        valid = (
            set(result)
            == {
                "schema_version",
                "status",
                "placement_id",
                "artifact_removed",
                "canonical_preserved",
                "referenced_artifact_preserved",
            }
            and result.get("schema_version") == 1
            and result.get("status") == "placement_reference_removed"
            and result.get("placement_id") == placement_id
            and result.get("artifact_removed") is False
            and result.get("canonical_preserved") is True
            and result.get("referenced_artifact_preserved") is True
        )
        recovered_status = "referenced_placement_detached"
    if not valid or membership.sync_eligible is not False:
        raise ValueError("Import Story Arc membership removal checkpoint is missing or changed")
    return recovered_status


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
