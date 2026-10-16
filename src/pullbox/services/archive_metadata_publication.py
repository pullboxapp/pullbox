"""Durable intent and serialized publication; canonical finalization stays separate."""

import asyncio
import hashlib
import os
import stat
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.archive_metadata import read_archive_metadata
from pullbox.core.metadata_identity import MetadataEntityKind
from pullbox.models.archive_metadata_publication import ArchiveMetadataPublication, PublicationState
from pullbox.schemas.archive_publication_owner import ImportArchiveOwner
from pullbox.schemas.metadata_snapshot import MetadataSnapshot
from pullbox.services.archive_metadata_binding import (
    ArchiveMetadataTarget,
    FileFingerprint,
    lock_archive_metadata_binding,
    revalidate_archive_metadata_target,
)
from pullbox.services.archive_metadata_rendering import (
    ArchiveMetadataRenderError,
    render_archive_metadata,
)
from pullbox.services.library_mutation_coordination import lock_file_mutation_admission
from pullbox.services.metadata_writer_identity import metadata_write_scope
from pullbox.utilities.executors.archive_metadata_staging import StagedArchiveMetadata

_MAX_PLAN_BYTES = 4 * 1024 * 1024


class ArchivePublicationError(ValueError):
    """Fixed publication diagnostic without paths or embedded payloads."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ArchivePublicationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    target: ArchiveMetadataTarget
    stage_path: Path
    stage_fingerprint: FileFingerprint
    source_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    output_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    series: MetadataSnapshot
    issue: MetadataSnapshot
    import_owner: ImportArchiveOwner | None = None
    metadata_job_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


@dataclass(frozen=True)
class ArchivePublicationReceipt:
    operation_id: UUID
    revision: int
    state: PublicationState
    plan: ArchivePublicationPlan


@dataclass(frozen=True)
class ArchivePublicationInspection:
    operation_id: UUID
    revision: int
    fingerprint: FileFingerprint | None
    digest: str | None


def _fingerprint(path: Path) -> FileFingerprint | None:
    try:
        return _stat_fingerprint(path.lstat())
    except FileNotFoundError:
        return None


def _stat_fingerprint(info: os.stat_result) -> FileFingerprint:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
    )


def _check_cancelled(stop: Event) -> None:
    if stop.is_set():
        raise ArchivePublicationError("cancelled")


async def _file_work[T](work: Callable[[Event], T]) -> T:
    stop = Event()
    task = asyncio.create_task(asyncio.to_thread(work, stop))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stop.set()
        while not task.done():
            with suppress(asyncio.CancelledError, OSError, ValueError):
                await asyncio.shield(task)
        if not task.cancelled():
            task.exception()
        raise


def _digest(path: Path, expected: FileFingerprint, stop: Event) -> str:
    _check_cancelled(stop)
    if not stat.S_ISREG(expected[5]):
        raise ArchivePublicationError("file_changed")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        if _stat_fingerprint(os.fstat(stream.fileno())) != expected:
            raise ArchivePublicationError("file_changed")
        digest = hashlib.sha256()
        remaining = expected[2]
        while remaining:
            _check_cancelled(stop)
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ArchivePublicationError("file_changed")
            remaining -= len(chunk)
            digest.update(chunk)
        if (
            stream.read(1)
            or _stat_fingerprint(os.fstat(stream.fileno())) != expected
            or _fingerprint(path) != expected
        ):
            raise ArchivePublicationError("file_changed")
    return digest.hexdigest()


async def prepare_archive_publication(
    target: ArchiveMetadataTarget,
    staged: StagedArchiveMetadata,
    series: MetadataSnapshot,
    issue: MetadataSnapshot,
) -> ArchivePublicationPlan:
    """Hash verified evidence outside DB sessions; change only the private stage mode."""
    return await _file_work(lambda stop: _prepare(target, staged, series, issue, stop))


def _prepare(
    target: ArchiveMetadataTarget,
    staged: StagedArchiveMetadata,
    series: MetadataSnapshot,
    issue: MetadataSnapshot,
    stop: Event,
) -> ArchivePublicationPlan:
    _check_cancelled(stop)
    bound = target.binding.metadata
    if (
        series.entity_kind is not MetadataEntityKind.SERIES
        or issue.entity_kind is not MetadataEntityKind.ISSUE
        or series.identities != bound.series.identities
        or issue.identities != bound.issues[0].identities
        or staged.source_path != target.path
        or staged.source_fingerprint != target.fingerprint
        or staged.path.parent.parent != target.path.parent
        or not staged.path.parent.name.startswith(".pullbox-metadata-stage-")
        or staged.path.name != "metadata.cbz"
        or staged.path.resolve(strict=True) != staged.path
        or staged.output_fingerprint[0] != target.fingerprint[0]
    ):
        raise ArchivePublicationError("invalid_plan")
    target.check_unchanged()
    staged.check_unchanged()
    files = read_archive_metadata(staged.path, "cbz", max_solid_scan_bytes=_MAX_PLAN_BYTES)
    try:
        pair = render_archive_metadata(series, issue, files)
        if pair.comicinfo != files.comicinfo.payload or pair.metroninfo != files.metroninfo.payload:
            raise ArchivePublicationError("snapshot_disagrees")
    except ArchiveMetadataRenderError:
        raise ArchivePublicationError("snapshot_disagrees") from None
    _check_cancelled(stop)
    fd = os.open(staged.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        if _stat_fingerprint(os.fstat(stream.fileno())) != staged.output_fingerprint:
            raise ArchivePublicationError("file_changed")
        os.fchmod(stream.fileno(), stat.S_IMODE(target.fingerprint[5]) & 0o777)
        output = _stat_fingerprint(os.fstat(stream.fileno()))
        os.fsync(stream.fileno())
    plan = ArchivePublicationPlan(
        target=target,
        stage_path=staged.path,
        stage_fingerprint=output,
        source_digest=_digest(target.path, target.fingerprint, stop),
        output_digest=_digest(staged.path, output, stop),
        series=series,
        issue=issue,
    )
    _encode(plan)
    return plan


def _encode(plan: ArchivePublicationPlan) -> str:
    encoded = plan.model_dump_json()
    if len(encoded.encode("utf-8")) > _MAX_PLAN_BYTES:
        raise ArchivePublicationError("plan_too_large")
    if ArchivePublicationPlan.model_validate_json(encoded) != plan:
        raise ArchivePublicationError("invalid_plan")
    return encoded


def _receipt(row: ArchiveMetadataPublication) -> ArchivePublicationReceipt:
    try:
        if len(row.plan_json.encode("utf-8")) > _MAX_PLAN_BYTES:
            raise ValueError("Too large")
        plan = ArchivePublicationPlan.model_validate_json(row.plan_json)
        return ArchivePublicationReceipt(UUID(row.operation_id), row.revision, row.state, plan)
    except ValueError:
        raise ArchivePublicationError("invalid_journal") from None


def _clean_session(session: AsyncSession) -> None:
    if session.new or session.dirty or session.deleted:
        raise ArchivePublicationError("pending_session_changes")


async def load_archive_publication(
    session: AsyncSession, operation_id: UUID
) -> ArchivePublicationReceipt | None:
    _clean_session(session)
    row = await session.scalar(
        select(ArchiveMetadataPublication)
        .where(ArchiveMetadataPublication.operation_id == str(operation_id))
        .execution_options(populate_existing=True)
    )
    return _receipt(row) if row else None


async def _lock_binding(session: AsyncSession, plan: ArchivePublicationPlan) -> None:
    from pullbox.services.import_archive_publication import lock_import_archive_owner

    await lock_import_archive_owner(session, plan.import_owner)
    await lock_archive_metadata_binding(session, plan.target.binding)


async def record_archive_publication(
    session: AsyncSession, plan: ArchivePublicationPlan, operation_id: UUID
) -> ArchivePublicationReceipt:
    """Caller must commit this intent before any filesystem publication.

    Reservations do not expire. Recovery needs the same row lock as publication;
    elapsed time is never permission to let another writer take over.
    """
    from pullbox.services.import_archive_publication import require_import_archive_owner
    from pullbox.services.library_conversion_recovery import require_no_library_conversion
    from pullbox.services.library_removal import require_no_library_removal

    _clean_session(session)
    encoded = _encode(plan)
    path_key = hashlib.sha256(os.fsencode(plan.target.path)).hexdigest()
    try:
        async with metadata_write_scope(session):
            await lock_file_mutation_admission(session)
            await require_no_library_removal(
                session, plan.target.path, plan.stage_path, include_descendants=False
            )
            await require_no_library_conversion(
                session, plan.target.path, plan.stage_path, include_descendants=False
            )
            await _lock_binding(session, plan)
            existing = await load_archive_publication(session, operation_id)
            if existing:
                if existing.plan != plan:
                    raise ArchivePublicationError("operation_reused")
                return existing
            busy = await session.scalar(
                select(ArchiveMetadataPublication.id)
                .where(
                    or_(
                        ArchiveMetadataPublication.active_file_id
                        == plan.target.binding.library_file_id,
                        ArchiveMetadataPublication.active_path_key == path_key,
                    )
                )
                .limit(1)
            )
            if busy:
                raise ArchivePublicationError("publication_busy")
            await require_import_archive_owner(session, plan)
            await revalidate_archive_metadata_target(session, plan.target)
            row = ArchiveMetadataPublication(
                operation_id=str(operation_id),
                library_file_id=plan.target.binding.library_file_id,
                active_file_id=plan.target.binding.library_file_id,
                active_path_key=path_key,
                plan_json=encoded,
            )
            session.add(row)
            await session.flush()
            session.info[f"archive_intent:{operation_id}"] = session.sync_session.get_transaction()
            return _receipt(row)
    except IntegrityError:
        raise ArchivePublicationError("publication_busy") from None


async def _locked_row(session: AsyncSession, operation_id: UUID) -> ArchiveMetadataPublication:
    row = await session.scalar(
        select(ArchiveMetadataPublication)
        .where(ArchiveMetadataPublication.operation_id == str(operation_id))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise ArchivePublicationError("publication_missing")
    return row


def _directories_unchanged(plan: ArchivePublicationPlan) -> None:
    for path, device, inode, mode in plan.target.directories:
        info = path.lstat()
        if (info.st_dev, info.st_ino, info.st_mode) != (device, inode, mode):
            raise ArchivePublicationError("directories_changed")


async def publish_archive_publication(
    session: AsyncSession, operation_id: UUID
) -> ArchivePublicationReceipt:
    """Replace under a short DB write lock, using an already committed intent.

    Only stat/rename/directory-sync operations occur in the short offloaded file
    boundary, never archive reads or hashes. Cancellation joins it before releasing
    the DB lock. Commit failure leaves intent for evidence-based recovery. The
    reservation remains until separate canonical DB finalization commits. Existing
    writers still require workflow ownership and rollback integration.
    """
    from pullbox.services.import_archive_publication import require_import_archive_owner

    _clean_session(session)
    if (
        session.info.get(f"archive_intent:{operation_id}") is session.sync_session.get_transaction()
        and session.in_transaction()
    ):
        raise ArchivePublicationError("intent_not_committed")
    async with metadata_write_scope(session):
        receipt = await load_archive_publication(session, operation_id)
        if receipt is None:
            raise ArchivePublicationError("publication_missing")
        await _lock_binding(session, receipt.plan)
        row = await _locked_row(session, operation_id)
        if row.state is not PublicationState.INTENDED:
            raise ArchivePublicationError("publication_not_intended")
        plan = _receipt(row).plan
        await require_import_archive_owner(session, plan)
        await revalidate_archive_metadata_target(session, plan.target)
        await _file_work(lambda stop: _publish_files(plan, stop))
        row.state = PublicationState.PUBLISHED
        row.revision += 1
        await session.flush()
        return _receipt(row)


def _publish_files(plan: ArchivePublicationPlan, stop: Event) -> None:
    plan.target.check_unchanged()
    if (
        plan.stage_path.resolve(strict=True) != plan.stage_path
        or _fingerprint(plan.stage_path) != plan.stage_fingerprint
    ):
        raise ArchivePublicationError("file_changed")
    _check_cancelled(stop)
    os.replace(plan.stage_path, plan.target.path)
    # Persist the destination directory entry before reporting publication. A
    # failed sync leaves intent for inspection, never permission to publish again.
    fd = os.open(plan.target.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


async def inspect_archive_publication(
    receipt: ArchivePublicationReceipt,
) -> ArchivePublicationInspection:
    """Read content outside the DB write boundary; this never repairs or deletes files."""
    return await _file_work(lambda stop: _inspect(receipt, stop))


def _inspect(receipt: ArchivePublicationReceipt, stop: Event) -> ArchivePublicationInspection:
    _check_cancelled(stop)
    plan = receipt.plan
    fingerprint = _fingerprint(plan.target.path)
    digest = None
    if (
        fingerprint is not None
        and stat.S_ISREG(fingerprint[5])
        and fingerprint[2] in {plan.target.fingerprint[2], plan.stage_fingerprint[2]}
    ):
        _directories_unchanged(plan)
        digest = _digest(plan.target.path, fingerprint, stop)
    return ArchivePublicationInspection(receipt.operation_id, receipt.revision, fingerprint, digest)


def _published(plan: ArchivePublicationPlan, inspection: ArchivePublicationInspection) -> bool:
    actual, expected = inspection.fingerprint, plan.stage_fingerprint
    # Rename may change ctime. Identity, mode, size, mtime and actual bytes must agree.
    return bool(
        actual
        and actual[:4] == expected[:4]
        and actual[5] == expected[5]
        and inspection.digest == plan.output_digest
    )


async def reconcile_archive_publication(
    session: AsyncSession,
    receipt: ArchivePublicationReceipt,
    inspection: ArchivePublicationInspection,
) -> ArchivePublicationReceipt:
    """Fence the original publisher, then classify the inspected durable result.

    Published means only filesystem publication, not canonical DB finalization.
    Unknown content remains reserved for review; only a proven untouched original
    can release an intended reservation. No recovery path republishes a stage.
    """
    _clean_session(session)
    async with metadata_write_scope(session):
        row = await _locked_row(session, receipt.operation_id)
        if _receipt(row) != receipt or (inspection.operation_id, inspection.revision) != (
            receipt.operation_id,
            receipt.revision,
        ):
            raise ArchivePublicationError("publication_changed")
        if row.state in {PublicationState.FINALIZED, PublicationState.SETTLED}:
            return receipt
        _directories_unchanged(receipt.plan)
        if _fingerprint(receipt.plan.target.path) != inspection.fingerprint:
            raise ArchivePublicationError("inspection_changed")
        if row.state in {PublicationState.ABANDONED, PublicationState.REVIEW}:
            return receipt
        state = PublicationState.REVIEW
        if _published(receipt.plan, inspection):
            state = PublicationState.PUBLISHED
        elif (
            row.state is PublicationState.INTENDED
            and inspection.fingerprint == receipt.plan.target.fingerprint
            and inspection.digest == receipt.plan.source_digest
        ):
            state = PublicationState.ABANDONED
        if row.state is not state:
            row.state = state
            row.revision += 1
            if state is PublicationState.ABANDONED:
                row.active_file_id = None
                row.active_path_key = None
            await session.flush()
        return _receipt(row)
