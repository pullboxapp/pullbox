"""Managed-copy destination capability and capacity preflight."""

from __future__ import annotations

import asyncio
import enum
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from sqlalchemy import func as sa_func
from sqlalchemy import or_ as sa_or
from sqlalchemy import select as sa_select
from sqlalchemy import update as sa_update

from pullbox.config import get_settings
from pullbox.core.exceptions import ConfigurationError, ValidationError
from pullbox.core.library_root_resolution import resolve_library_root
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportFileHandlingMode,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.models.library import LibraryFile, LibraryRoot
from pullbox.models.story_arc import ImportedStoryArcStatus, StoryArcResolutionState
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.services.library_root_management import validate_managed_library_root

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.import_job import ImportJob

_ONE_GIB = 1024**3
_CAPACITY_SNAPSHOT_KEY = "managed_copy_capacity"
_STORY_ARC_PAGE_SIZE = 250
_STORY_ARC_ENTRY_PAGE_SIZE = 1_000
_CONVERSION_WORKSPACE_MULTIPLIER = 2
ManagedCopyPreflightStage = Literal["confirmation", "execution"]


class ManagedCopyPreflightFailure(enum.StrEnum):
    """Stable failure reasons for a managed-copy preflight block."""

    TARGET_MISSING = "target_missing"
    TARGET_DISABLED = "target_disabled"
    TARGET_REFERENCE_ONLY = "target_reference_only"
    TARGET_UNAVAILABLE = "target_unavailable"
    CAPACITY_UNKNOWN = "capacity_unknown"
    CAPACITY_INSUFFICIENT = "capacity_insufficient"


@dataclass(frozen=True, slots=True)
class ManagedCopyTargetCapacitySnapshot:
    """One path-free managed-root capacity result."""

    target_library_root_id: int
    selected_source_bytes: int
    reserve_bytes: int
    required_bytes: int
    free_bytes: int | None
    status: str

    def as_dict(self) -> dict[str, int | str | None]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ConversionWorkspaceCapacitySnapshot:
    """Path-free estimate and live capacity for the system temp filesystem."""

    selected_source_bytes: int
    active_worker_count: int
    estimated_workspace_bytes: int
    reserve_bytes: int
    required_bytes: int
    free_bytes: int | None
    status: str

    def as_dict(self) -> dict[str, int | str | None]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ManagedCopyCapacitySnapshot:
    """Path-free capacity evidence persisted with an import job.

    Schema version 1 remains byte-for-byte compatible for the common single
    root/no-conversion case. Version 2 adds bounded per-root and temporary
    conversion-workspace evidence while retaining every v1 top-level field.
    """

    schema_version: int
    stage: ManagedCopyPreflightStage
    target_library_root_id: int | None
    selected_source_bytes: int
    reserve_bytes: int
    required_bytes: int
    free_bytes: int | None
    status: str
    target_capacities: tuple[ManagedCopyTargetCapacitySnapshot, ...] = ()
    conversion_workspace: ConversionWorkspaceCapacitySnapshot | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the JSON-safe durable representation."""
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "stage": self.stage,
            "target_library_root_id": self.target_library_root_id,
            "selected_source_bytes": self.selected_source_bytes,
            "reserve_bytes": self.reserve_bytes,
            "required_bytes": self.required_bytes,
            "free_bytes": self.free_bytes,
            "status": self.status,
        }
        if self.schema_version >= 2:
            result["target_capacities"] = [target.as_dict() for target in self.target_capacities]
            result["conversion_workspace"] = (
                self.conversion_workspace.as_dict()
                if self.conversion_workspace is not None
                else None
            )
        return result


class ManagedCopyPreflightError(ValidationError):
    """Managed-copy validation failed before any library mutation."""

    def __init__(
        self,
        reason: ManagedCopyPreflightFailure,
        message: str,
        *,
        snapshot: ManagedCopyCapacitySnapshot | None = None,
    ) -> None:
        self.reason = reason
        self.snapshot = snapshot
        details: dict[str, object] = {"reason": reason.value}
        if snapshot is not None:
            details["capacity"] = snapshot.as_dict()
        super().__init__(message, details=details)


def managed_copy_capacity_reserve(selected_source_bytes: int) -> int:
    """Return the required fixed-or-proportional free-space reserve."""
    if selected_source_bytes < 0:
        raise ValueError("Selected source bytes cannot be negative")
    ten_percent = (selected_source_bytes + 9) // 10
    return max(_ONE_GIB, ten_percent)


async def selected_managed_copy_source_bytes(
    session: AsyncSession,
    job_id: int,
) -> int:
    """Sum files that remain selected for managed placement."""
    selected_new_series = ImportedSeries.status.in_(
        [ImportSeriesStatus.CONFIRMED, ImportSeriesStatus.IMPORTING]
    )
    selected_duplicate_series = (
        ImportedSeries.status == ImportSeriesStatus.DUPLICATE
    ) & ImportedFile.include_in_import.is_(True)
    total = await session.scalar(
        sa_select(sa_func.coalesce(sa_func.sum(ImportedFile.file_size), 0))
        .join(ImportedSeries, ImportedFile.import_series_id == ImportedSeries.id)
        .where(
            ImportedFile.import_job_id == job_id,
            ImportedFile.status.in_([ImportedFileStatus.MATCHED, ImportedFileStatus.CONFIRMED]),
            sa_or(selected_new_series, selected_duplicate_series),
        )
    )
    return max(int(total or 0), 0)


def _selected_import_file(
    imp_file: ImportedFile | None,
    imported_series: ImportedSeries | None,
    *,
    job_id: int,
    matched_issue_id: int | None,
) -> bool:
    if (
        imp_file is None
        or imported_series is None
        or imp_file.import_job_id != job_id
        or imported_series.import_job_id != job_id
        or imp_file.status not in {ImportedFileStatus.MATCHED, ImportedFileStatus.CONFIRMED}
        or imp_file.matched_issue_id is None
        or (
            matched_issue_id is not None and int(imp_file.matched_issue_id) != int(matched_issue_id)
        )
    ):
        return False
    return bool(
        imported_series.status in {ImportSeriesStatus.CONFIRMED, ImportSeriesStatus.IMPORTING}
        or (
            imported_series.status is ImportSeriesStatus.DUPLICATE
            and imp_file.include_in_import is True
        )
    )


def _story_arc_copy_target_id(snapshot: object) -> int | None:
    if not isinstance(snapshot, dict) or snapshot.get("activation") != "confirmed":
        return None
    placement = snapshot.get("placement_policy")
    if not isinstance(placement, dict) or placement.get("mode") != "copy":
        return None
    target_id = placement.get("target_library_root_id")
    if isinstance(target_id, bool) or not isinstance(target_id, int) or target_id < 1:
        raise ManagedCopyPreflightError(
            ManagedCopyPreflightFailure.TARGET_MISSING,
            "A confirmed Story Arc copy policy no longer has a valid managed root.",
        )
    return target_id


async def selected_story_arc_copy_source_bytes_by_root(
    session: AsyncSession,
    job_id: int,
) -> dict[int, int]:
    """Return confirmed Story Arc COPY bytes grouped by actual target root.

    Use the same minimum-id canonical LibraryFile chosen by materialization
    when one already exists. Otherwise, a selected import file is the best
    pre-execution size evidence for the job-owned artifact that will be
    registered before Story Arc materialization.
    Each arc entry is charged independently because one issue may intentionally
    produce a separate copy in more than one Story Arc destination.
    """
    totals: dict[int, int] = {}
    after_arc_id = 0
    while True:
        arc_rows = (
            await session.execute(
                sa_select(
                    ImportedStoryArc.id,
                    ImportedStoryArc.proposed_policy_snapshot,
                )
                .where(
                    ImportedStoryArc.import_job_id == job_id,
                    ImportedStoryArc.status == ImportedStoryArcStatus.CONFIRMED,
                    ImportedStoryArc.selected_for_import.is_(True),
                    ImportedStoryArc.id > after_arc_id,
                )
                .order_by(ImportedStoryArc.id.asc())
                .limit(_STORY_ARC_PAGE_SIZE)
            )
        ).all()
        if not arc_rows:
            break
        copy_root_by_arc_id = {
            int(row.id): target_id
            for row in arc_rows
            if (target_id := _story_arc_copy_target_id(row.proposed_policy_snapshot)) is not None
        }
        if copy_root_by_arc_id:
            await _accumulate_story_arc_copy_entry_page_bytes(
                session,
                job_id=job_id,
                copy_root_by_arc_id=copy_root_by_arc_id,
                totals=totals,
            )
        after_arc_id = int(arc_rows[-1].id)
    return totals


async def _accumulate_story_arc_copy_entry_page_bytes(
    session: AsyncSession,
    *,
    job_id: int,
    copy_root_by_arc_id: dict[int, int],
    totals: dict[int, int],
) -> None:
    after_entry_id = 0
    while True:
        rows = (
            await session.execute(
                sa_select(
                    ImportedStoryArcEntry,
                    ImportedFile,
                    ImportedSeries,
                )
                .outerjoin(ImportedFile, ImportedFile.id == ImportedStoryArcEntry.import_file_id)
                .outerjoin(ImportedSeries, ImportedSeries.id == ImportedFile.import_series_id)
                .where(
                    ImportedStoryArcEntry.imported_story_arc_id.in_(copy_root_by_arc_id),
                    ImportedStoryArcEntry.selected_for_import.is_(True),
                    ImportedStoryArcEntry.resolution_state == StoryArcResolutionState.RESOLVED,
                    ImportedStoryArcEntry.id > after_entry_id,
                )
                .order_by(ImportedStoryArcEntry.id.asc())
                .limit(_STORY_ARC_ENTRY_PAGE_SIZE)
            )
        ).all()
        if not rows:
            break
        issue_ids = {
            int(row[0].matched_issue_id) for row in rows if row[0].matched_issue_id is not None
        }
        canonical_sizes = await _canonical_library_file_sizes(session, issue_ids)
        for entry, imp_file, imported_series in rows:
            issue_id = int(entry.matched_issue_id) if entry.matched_issue_id is not None else None
            if issue_id is not None and issue_id in canonical_sizes:
                # Materialization selects the minimum-id canonical file. Any
                # file registered later by this job receives a larger id, so
                # current canonical evidence must win when it already exists.
                size = canonical_sizes[issue_id]
            elif _selected_import_file(
                imp_file,
                imported_series,
                job_id=job_id,
                matched_issue_id=issue_id,
            ):
                # No canonical file exists yet. The selected job-owned file is
                # the source that normal import will register before Story Arc
                # materialization, so its staged size is the fail-closed fallback.
                size = max(int(imp_file.file_size or 0), 0)
            else:
                size = 0
            root_id = copy_root_by_arc_id[int(entry.imported_story_arc_id)]
            totals[root_id] = totals.get(root_id, 0) + size
        after_entry_id = int(rows[-1][0].id)


async def _canonical_library_file_sizes(
    session: AsyncSession,
    issue_ids: set[int],
) -> dict[int, int]:
    if not issue_ids:
        return {}
    canonical = (
        sa_select(
            LibraryFile.issue_id.label("issue_id"),
            sa_func.min(LibraryFile.id).label("library_file_id"),
        )
        .where(LibraryFile.issue_id.in_(issue_ids))
        .group_by(LibraryFile.issue_id)
        .subquery()
    )
    rows = (
        await session.execute(
            sa_select(LibraryFile.issue_id, LibraryFile.file_size).join(
                canonical,
                LibraryFile.id == canonical.c.library_file_id,
            )
        )
    ).all()
    return {
        int(issue_id): max(int(file_size or 0), 0)
        for issue_id, file_size in rows
        if issue_id is not None
    }


async def estimate_conversion_workspace_source_bytes(
    session: AsyncSession,
    job: ImportJob,
    *,
    worker_count: int | None = None,
) -> tuple[int, int]:
    """Return largest concurrently staged non-CBZ source bytes and worker count."""
    if not (
        job.move_to_library
        and (job.convert_to_preferred_format or job.update_embedded_comicinfo_from_match)
    ):
        return 0, 0
    configured_workers = (
        get_settings().import_file_worker_count if worker_count is None else worker_count
    )
    active_limit = max(int(configured_workers), 1)
    selected_new_series = ImportedSeries.status.in_(
        [ImportSeriesStatus.CONFIRMED, ImportSeriesStatus.IMPORTING]
    )
    selected_duplicate_series = (
        ImportedSeries.status == ImportSeriesStatus.DUPLICATE
    ) & ImportedFile.include_in_import.is_(True)
    sizes = list(
        (
            await session.scalars(
                sa_select(ImportedFile.file_size)
                .join(ImportedSeries, ImportedFile.import_series_id == ImportedSeries.id)
                .where(
                    ImportedFile.import_job_id == job.id,
                    ImportedFile.status.in_(
                        [ImportedFileStatus.MATCHED, ImportedFileStatus.CONFIRMED]
                    ),
                    sa_or(selected_new_series, selected_duplicate_series),
                    sa_func.lower(ImportedFile.file_format) != "cbz",
                )
                .order_by(ImportedFile.file_size.desc(), ImportedFile.id.asc())
                .limit(active_limit)
            )
        ).all()
    )
    return sum(max(int(size or 0), 0) for size in sizes), len(sizes)


async def validate_managed_copy_preflight(
    session: AsyncSession,
    job: ImportJob,
    *,
    stage: ManagedCopyPreflightStage,
) -> ManagedCopyCapacitySnapshot | None:
    """Revalidate every actual copy destination plus conversion workspace."""
    selected_bytes_by_root: dict[int, int] = {}
    if job.file_handling_mode != ImportFileHandlingMode.IN_PLACE:
        selected_source_bytes = await selected_managed_copy_source_bytes(session, job.id)
        # Preserve the original managed-copy contract even for sparse/legacy
        # review rows with zero recorded bytes: validate the configured root and
        # retain the fixed reserve evidence in the v1 snapshot.
        job_root = await _resolve_job_managed_root(session, job)
        selected_bytes_by_root[job_root.id] = selected_source_bytes

    story_arc_bytes = await selected_story_arc_copy_source_bytes_by_root(session, job.id)
    for root_id, selected_source_bytes in story_arc_bytes.items():
        selected_bytes_by_root[root_id] = (
            selected_bytes_by_root.get(root_id, 0) + selected_source_bytes
        )

    conversion_source_bytes, conversion_workers = await estimate_conversion_workspace_source_bytes(
        session, job
    )
    if not selected_bytes_by_root and conversion_source_bytes == 0:
        return None

    target_snapshots: list[ManagedCopyTargetCapacitySnapshot] = []
    for root_id in sorted(selected_bytes_by_root):
        root = await _load_managed_root(session, root_id)
        selected_source_bytes = selected_bytes_by_root[root_id]
        reserve_bytes = managed_copy_capacity_reserve(selected_source_bytes)
        required_bytes = selected_source_bytes + reserve_bytes
        capabilities = await _validate_live_managed_root(root)
        free_bytes = _free_bytes_from_capabilities(capabilities)
        status = (
            "unknown"
            if free_bytes is None
            else "insufficient"
            if free_bytes < required_bytes
            else "ready"
        )
        target = ManagedCopyTargetCapacitySnapshot(
            target_library_root_id=root.id,
            selected_source_bytes=selected_source_bytes,
            reserve_bytes=reserve_bytes,
            required_bytes=required_bytes,
            free_bytes=free_bytes,
            status=status,
        )
        target_snapshots.append(target)
        if status != "ready":
            snapshot = _target_result_snapshot(
                stage=stage,
                target=target,
                targets=target_snapshots,
                force_v2=len(selected_bytes_by_root) > 1,
            )
            _record_capacity_snapshot(job, snapshot)
            if status == "unknown":
                raise ManagedCopyPreflightError(
                    ManagedCopyPreflightFailure.CAPACITY_UNKNOWN,
                    "Available space for a selected managed library root could not be determined.",
                    snapshot=snapshot,
                )
            raise ManagedCopyPreflightError(
                ManagedCopyPreflightFailure.CAPACITY_INSUFFICIENT,
                "A selected managed library root does not have enough free space for this import.",
                snapshot=snapshot,
            )

    workspace = await _validate_conversion_workspace(
        selected_source_bytes=conversion_source_bytes,
        active_worker_count=conversion_workers,
    )
    if workspace is not None and workspace.status != "ready":
        snapshot = _workspace_result_snapshot(
            stage=stage,
            targets=target_snapshots,
            workspace=workspace,
        )
        _record_capacity_snapshot(job, snapshot)
        if workspace.status == "unknown":
            raise ManagedCopyPreflightError(
                ManagedCopyPreflightFailure.CAPACITY_UNKNOWN,
                "Available space for the temporary conversion workspace could not be determined.",
                snapshot=snapshot,
            )
        raise ManagedCopyPreflightError(
            ManagedCopyPreflightFailure.CAPACITY_INSUFFICIENT,
            "The temporary conversion workspace does not have enough free space for this import.",
            snapshot=snapshot,
        )

    primary_root_id = (
        job.target_library_root_id
        if job.target_library_root_id in selected_bytes_by_root
        else min(selected_bytes_by_root)
    )
    primary = next(
        target for target in target_snapshots if target.target_library_root_id == primary_root_id
    )
    snapshot = _target_result_snapshot(
        stage=stage,
        target=primary,
        targets=target_snapshots,
        workspace=workspace,
        force_v2=len(target_snapshots) > 1 or workspace is not None,
    )
    _record_capacity_snapshot(job, snapshot)
    return snapshot


async def reopen_review_after_managed_copy_preflight_failure(
    session: AsyncSession,
    job: ImportJob,
    error: ManagedCopyPreflightError,
) -> None:
    """Return an unstarted execution to a retryable review state."""
    await session.execute(
        sa_update(ImportedFile)
        .where(
            ImportedFile.import_job_id == job.id,
            ImportedFile.status == ImportedFileStatus.CONFIRMED,
        )
        .values(status=ImportedFileStatus.MATCHED)
    )
    await session.execute(
        sa_update(ImportedSeries)
        .where(
            ImportedSeries.import_job_id == job.id,
            ImportedSeries.status.in_([ImportSeriesStatus.CONFIRMED, ImportSeriesStatus.IMPORTING]),
        )
        .values(status=ImportSeriesStatus.MATCHED, selected_for_import=True)
    )
    await session.execute(
        sa_update(ImportedStoryArc)
        .where(
            ImportedStoryArc.import_job_id == job.id,
            ImportedStoryArc.status == ImportedStoryArcStatus.CONFIRMED,
        )
        .values(status=ImportedStoryArcStatus.READY)
    )
    job.status = ImportJobStatus.REVIEW
    job.control_request = ImportControlRequest.NONE
    job.import_started_at = None
    job.error_message = error.message
    snapshot = dict(job.progress_snapshot or {})
    snapshot.update(
        {
            "status": ImportJobStatus.REVIEW.value,
            "mode": "scan",
            "phase": "review",
            "progress": 100,
            "message": error.message,
        }
    )
    if error.snapshot is not None:
        snapshot[_CAPACITY_SNAPSHOT_KEY] = error.snapshot.as_dict()
    job.progress_snapshot = snapshot
    await session.flush()


async def _resolve_job_managed_root(
    session: AsyncSession,
    job: ImportJob,
) -> LibraryRoot:
    if job.target_library_root_id is not None:
        root = await session.get(LibraryRoot, job.target_library_root_id)
        if root is None:
            raise ManagedCopyPreflightError(
                ManagedCopyPreflightFailure.TARGET_MISSING,
                "The selected managed library root does not exist.",
            )
    else:
        try:
            root = await resolve_library_root(session, Path(job.source_path), None)
        except ConfigurationError as exc:
            raise ManagedCopyPreflightError(
                ManagedCopyPreflightFailure.TARGET_MISSING,
                exc.message,
            ) from exc
        job.target_library_root_id = root.id

    return _require_managed_root_roles(root)


async def _load_managed_root(session: AsyncSession, root_id: int) -> LibraryRoot:
    root = await session.get(LibraryRoot, root_id)
    if root is None:
        raise ManagedCopyPreflightError(
            ManagedCopyPreflightFailure.TARGET_MISSING,
            "A selected managed library root does not exist.",
        )
    return _require_managed_root_roles(root)


def _require_managed_root_roles(root: LibraryRoot) -> LibraryRoot:
    if not root.enabled:
        raise ManagedCopyPreflightError(
            ManagedCopyPreflightFailure.TARGET_DISABLED,
            "A selected managed library root is disabled.",
        )
    if not root.allow_managed_writes:
        raise ManagedCopyPreflightError(
            ManagedCopyPreflightFailure.TARGET_REFERENCE_ONLY,
            "A selected library root does not allow managed writes.",
        )
    return root


async def _validate_live_managed_root(root: LibraryRoot) -> dict[str, object]:
    try:
        return await validate_managed_library_root(root)
    except ValidationError as exc:
        raise ManagedCopyPreflightError(
            ManagedCopyPreflightFailure.TARGET_UNAVAILABLE,
            exc.message,
        ) from exc


def _free_bytes_from_capabilities(capabilities: dict[str, object]) -> int | None:
    free_value = capabilities.get("free_bytes")
    return free_value if isinstance(free_value, int) and not isinstance(free_value, bool) else None


async def _validate_conversion_workspace(
    *,
    selected_source_bytes: int,
    active_worker_count: int,
) -> ConversionWorkspaceCapacitySnapshot | None:
    if selected_source_bytes <= 0 or active_worker_count <= 0:
        return None
    # Each active conversion can hold extracted/rendered members plus a new CBZ
    # at the same time. Two times the largest N concurrent source sizes is a
    # conservative, deterministic estimate rather than a claim about final
    # compression ratio; the fixed-or-10% reserve supplies additional headroom.
    estimated_workspace_bytes = selected_source_bytes * _CONVERSION_WORKSPACE_MULTIPLIER
    reserve_bytes = managed_copy_capacity_reserve(estimated_workspace_bytes)
    required_bytes = estimated_workspace_bytes + reserve_bytes
    free_bytes: int | None
    try:
        temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
        usage = await asyncio.to_thread(shutil.disk_usage, temp_root)
        free_bytes = int(usage.free)
    except (OSError, RuntimeError, ValueError):
        free_bytes = None
    status = (
        "unknown"
        if free_bytes is None
        else "insufficient"
        if free_bytes < required_bytes
        else "ready"
    )
    return ConversionWorkspaceCapacitySnapshot(
        selected_source_bytes=selected_source_bytes,
        active_worker_count=active_worker_count,
        estimated_workspace_bytes=estimated_workspace_bytes,
        reserve_bytes=reserve_bytes,
        required_bytes=required_bytes,
        free_bytes=free_bytes,
        status=status,
    )


def _target_result_snapshot(
    *,
    stage: ManagedCopyPreflightStage,
    target: ManagedCopyTargetCapacitySnapshot,
    targets: list[ManagedCopyTargetCapacitySnapshot],
    workspace: ConversionWorkspaceCapacitySnapshot | None = None,
    force_v2: bool,
) -> ManagedCopyCapacitySnapshot:
    return ManagedCopyCapacitySnapshot(
        schema_version=2 if force_v2 else 1,
        stage=stage,
        target_library_root_id=target.target_library_root_id,
        selected_source_bytes=target.selected_source_bytes,
        reserve_bytes=target.reserve_bytes,
        required_bytes=target.required_bytes,
        free_bytes=target.free_bytes,
        status=target.status,
        target_capacities=tuple(targets) if force_v2 else (),
        conversion_workspace=workspace if force_v2 else None,
    )


def _workspace_result_snapshot(
    *,
    stage: ManagedCopyPreflightStage,
    targets: list[ManagedCopyTargetCapacitySnapshot],
    workspace: ConversionWorkspaceCapacitySnapshot,
) -> ManagedCopyCapacitySnapshot:
    return ManagedCopyCapacitySnapshot(
        schema_version=2,
        stage=stage,
        target_library_root_id=None,
        selected_source_bytes=workspace.estimated_workspace_bytes,
        reserve_bytes=workspace.reserve_bytes,
        required_bytes=workspace.required_bytes,
        free_bytes=workspace.free_bytes,
        status=workspace.status,
        target_capacities=tuple(targets),
        conversion_workspace=workspace,
    )


def _record_capacity_snapshot(
    job: ImportJob,
    snapshot: ManagedCopyCapacitySnapshot,
) -> None:
    progress_snapshot = dict(job.progress_snapshot or {})
    progress_snapshot[_CAPACITY_SNAPSHOT_KEY] = snapshot.as_dict()
    job.progress_snapshot = progress_snapshot
