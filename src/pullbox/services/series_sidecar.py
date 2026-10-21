"""Explicit, bounded sidecar writes; no provider calls or archive mutations."""

import asyncio
import hashlib
import json
import os
import stat
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from pydantic import TypeAdapter
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.exceptions import ValidationError
from pullbox.core.metadata_identity import MetadataEntityKind
from pullbox.core.series_sidecar import MAX_SIDECAR_BYTES, render_series_sidecar
from pullbox.models import Issue, LibraryFile, LibraryRoot, Series
from pullbox.models.library import LibraryFileStorageMode
from pullbox.schemas.metadata_snapshot import MetadataSnapshot
from pullbox.schemas.series_sidecar import (
    SeriesSidecarPreview,
    SeriesSidecarTargetRead,
    SeriesSidecarWriteRead,
)
from pullbox.services.archive_metadata_binding import _require_verified
from pullbox.services.library_mutation_coordination import (
    finish_short_mutation,
    lock_file_mutation_admission,
    require_no_archive_publication,
)
from pullbox.services.metadata_assembly import assemble_metadata
from pullbox.services.metadata_series_refresh_state import read_series_refresh_state
from pullbox.utilities.import_guards import ensure_no_active_import_file_mutation

_MAX_FILES = 10000
_MAX_FOLDERS = 50
_COMIC_EXTENSIONS = {".cbz", ".zip", ".cbr", ".rar", ".cb7", ".7z", ".cbt", ".pdf", ".epub"}


@dataclass(frozen=True)
class SidecarLocation:
    directory: str
    root: str
    managed: bool
    files: tuple[str, ...]
    reason: str | None = None


@dataclass(frozen=True)
class SidecarBinding:
    series_id: int
    snapshot: MetadataSnapshot
    aliases: tuple[str, ...]
    updated_at: datetime
    locations: tuple[SidecarLocation, ...]
    evidence: bytes


@dataclass(frozen=True)
class SidecarTarget:
    location: SidecarLocation
    directories: tuple[tuple[int, int, int], ...]
    previous: bytes | None
    fingerprint: tuple[int, ...] | None
    content: bytes
    read: SeriesSidecarTargetRead


@dataclass(frozen=True)
class PreparedSeriesSidecar:
    binding: SidecarBinding
    targets: tuple[SidecarTarget, ...]
    preview: SeriesSidecarPreview


async def _binding(session: AsyncSession, series_id: int) -> SidecarBinding:
    if session.new or session.dirty or session.deleted:
        raise ValueError("Finish pending library changes before previewing series metadata.")
    state = await read_series_refresh_state(session, series_id, include_issues=False)
    _require_verified(state.series)
    series = await session.get(Series, series_id, populate_existing=True)
    assert series is not None
    snapshot = assemble_metadata(
        MetadataEntityKind.SERIES,
        state.series.identities,
        (),
        state.policies,
        now=series.updated_at,
        current=state.series.values,
        previous=state.series.baseline,
        overrides=state.series.overrides,
    )
    rows = (
        await session.execute(
            select(LibraryFile.file_path, LibraryFile.library_root_id, LibraryFile.storage_mode)
            .join(Issue, Issue.id == LibraryFile.issue_id)
            .where(Issue.series_id == series_id)
            .order_by(LibraryFile.id)
            .limit(_MAX_FILES + 1)
        )
    ).all()
    if len(rows) > _MAX_FILES:
        raise ValueError(
            "This series exceeds the bounded sidecar limit of 10,000 registered files."
        )
    paths = {str(Path(path).parent): root_id for path, root_id, _ in rows}
    if series.path and series.library_root_id:
        paths[series.path] = series.library_root_id
    if len(paths) > _MAX_FOLDERS:
        raise ValueError("This series exceeds the bounded sidecar limit of 50 folders.")
    roots = {
        root.id: root
        for root in await session.scalars(select(LibraryRoot).order_by(LibraryRoot.id).limit(1001))
    }
    if len(roots) > 1000:
        raise ValueError("Too many library roots to safely resolve series sidecar locations.")
    related: Sequence[tuple[str, LibraryFileStorageMode, int | None]] = ()
    if paths:
        related = (
            (
                await session.execute(
                    select(LibraryFile.file_path, LibraryFile.storage_mode, Issue.series_id)
                    .outerjoin(Issue, Issue.id == LibraryFile.issue_id)
                    .where(
                        or_(
                            *(
                                LibraryFile.file_path.startswith(
                                    path.rstrip("/") + "/", autoescape=True
                                )
                                for path in paths
                            )
                        )
                    )
                    .order_by(LibraryFile.id)
                    .limit(_MAX_FILES + 1)
                )
            )
            .tuples()
            .all()
        )
    if len(related) > _MAX_FILES:
        raise ValueError("The selected folders contain too many files for a safe sidecar preview.")
    other_locations = (
        tuple(
            await session.scalars(
                select(Series.path)
                .where(
                    Series.id != series_id,
                    or_(
                        Series.path.in_(paths),
                        *(
                            Series.path.startswith(path.rstrip("/") + "/", autoescape=True)
                            for path in paths
                        ),
                    ),
                )
                .order_by(Series.path, Series.id)
                .limit(_MAX_FOLDERS + 1)
            )
        )
        if paths
        else ()
    )
    locations = []
    for directory, root_id in sorted(paths.items()):
        root = roots.get(root_id)
        reason = None
        if root is None or not root.enabled or not root.allow_managed_writes:
            reason = (
                "This root does not allow managed writes. Files kept in place are not modified."
            )
        children = [
            (path, mode, owner)
            for path, mode, owner in related
            if Path(path).is_relative_to(directory)
        ]
        if any(mode is LibraryFileStorageMode.REFERENCED for _, mode, _ in children):
            reason = "Files kept in place are not modified. Use a separate managed series folder."
        elif any(owner != series_id for _, _, owner in children):
            reason = (
                "This folder contains files from other or unmatched series. "
                "Organize it before writing a series sidecar."
            )
        matches = [
            item
            for item in roots.values()
            if item.enabled and Path(directory).is_relative_to(item.path)
        ]
        if any(
            path is not None and Path(path).is_relative_to(directory) for path in other_locations
        ):
            reason = "Another series uses this folder. Organize it before writing a series sidecar."
        if root and (
            Path(directory) == Path(root.path) or len(matches) != 1 or matches[0].id != root_id
        ):
            reason = (
                "This is a shared or ambiguous library location. "
                "Choose an individual managed series folder."
            )
        locations.append(
            SidecarLocation(
                directory,
                root.path if root else "",
                reason is None,
                tuple(sorted(path for path, _, owner in children if owner == series_id)),
                reason,
            )
        )
    evidence = TypeAdapter(type(state)).dump_json(state)
    evidence += json.dumps(
        (
            series.path,
            series.library_root_id,
            series.alternate_names,
            [
                (root.id, root.path, root.enabled, root.allow_managed_writes)
                for root in roots.values()
            ],
            [tuple(row) for row in related],
            other_locations,
        ),
        sort_keys=True,
    ).encode()
    return SidecarBinding(
        series_id,
        snapshot,
        tuple(series.alternate_names),
        series.updated_at,
        tuple(locations),
        evidence,
    )


@contextmanager
def _directory(location: SidecarLocation) -> Iterator[tuple[int, tuple[tuple[int, int, int], ...]]]:
    """Reject linked components and pin the publication parent with a descriptor."""
    root, directory = Path(location.root), Path(location.directory)
    if (
        any(
            not path.is_absolute()
            or ".." in path.parts
            or any(ord(c) < 32 or ord(c) == 127 for c in str(path))
            for path in (root, directory)
        )
        or not directory.is_relative_to(root)
        or directory == root
    ):
        raise ValueError("The series folder is outside its managed root or contains unsafe paths.")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    fingerprints = []
    try:
        for part in directory.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            fingerprints.append((info.st_dev, info.st_ino, info.st_mode))
        info = os.fstat(descriptor)
        if not info.st_mode & 0o222 or not os.access(directory, os.W_OK | os.X_OK):
            raise ValueError("The series folder is read-only. Check its managed-root permissions.")
        yield descriptor, tuple(fingerprints)
    finally:
        os.close(descriptor)


def _existing(descriptor: int) -> tuple[bytes | None, tuple[int, ...] | None]:
    try:
        file_descriptor = os.open(
            "series.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
        )
    except FileNotFoundError:
        return None, None
    with os.fdopen(file_descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_SIDECAR_BYTES:
            raise ValueError(
                "Existing series.json is linked, unsupported, or too large. Review it first."
            )
        if not info.st_mode & 0o222 or not os.access("series.json", os.W_OK, dir_fd=descriptor):
            raise ValueError("Existing series.json is read-only. Check its permissions first.")
        payload = stream.read(MAX_SIDECAR_BYTES + 1)
        after = os.fstat(stream.fileno())
        fingerprint = (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            info.st_mode,
        )
        if fingerprint != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_mode,
        ):
            raise ValueError("Existing series.json changed while being read. Preview again.")
        return payload, fingerprint


def _require_registered_folder(descriptor: int, location: SidecarLocation) -> None:
    registered = {
        Path(path).name for path in location.files if Path(path).parent == Path(location.directory)
    }
    # Do not put a single-series identity on a folder of unregistered comics.
    with os.scandir(descriptor) as entries:
        for index, entry in enumerate(entries):
            if entry.name.casefold() == "series.json" and entry.name != "series.json":
                raise ValueError(
                    "This folder has a differently cased series.json. Review it first."
                )
            if index > _MAX_FILES or (
                Path(entry.name).suffix.lower() in _COMIC_EXTENSIONS
                and entry.name not in registered
            ):
                raise ValueError(
                    "This folder contains unregistered comic files. "
                    "Rescan or organize it before writing series metadata."
                )


def _inspect(
    binding: SidecarBinding,
    location: SidecarLocation,
    edited_fields: frozenset[str] = frozenset(),
) -> SidecarTarget:
    if not location.managed:
        return SidecarTarget(
            location,
            (),
            None,
            None,
            b"",
            SeriesSidecarTargetRead(
                directory=location.directory, action="blocked", reason=location.reason
            ),
        )
    try:
        with _directory(location) as (descriptor, directories):
            _require_registered_folder(descriptor, location)
            previous, fingerprint = _existing(descriptor)
            content = render_series_sidecar(
                binding.snapshot,
                binding.aliases,
                previous,
                updated_at=binding.updated_at,
                edited_fields=edited_fields,
            )
            unchanged = previous is not None and json.loads(previous) == json.loads(content)
            read = SeriesSidecarTargetRead(
                directory=location.directory,
                action="unchanged" if unchanged else "update" if previous is not None else "create",
                changes=[]
                if unchanged
                else [
                    "Compiled series fields and verified provider IDs",
                    "Saved field provenance and source freshness",
                    "Safe existing custom fields preserved",
                ],
            )
            return SidecarTarget(location, directories, previous, fingerprint, content, read)
    except (OSError, ValueError, RuntimeError) as exc:
        message = (
            str(exc)
            if isinstance(exc, ValueError)
            else "This folder or series.json is unavailable, linked, or not writable. "
            "Check its location and permissions."
        )
        return SidecarTarget(
            location,
            (),
            None,
            None,
            b"",
            SeriesSidecarTargetRead(directory=location.directory, action="blocked", reason=message),
        )


async def prepare_series_sidecar(session: AsyncSession, series_id: int) -> PreparedSeriesSidecar:
    binding = await _binding(session, series_id)
    await session.commit()
    targets = tuple(await asyncio.to_thread(_inspect_all, binding))
    digest = hashlib.sha256(
        binding.evidence
        + TypeAdapter(SidecarBinding).dump_json(binding)
        + TypeAdapter(tuple[SidecarTarget, ...]).dump_json(targets)
    ).hexdigest()
    return PreparedSeriesSidecar(
        binding,
        targets,
        SeriesSidecarPreview(
            series_id=series_id,
            snapshot=binding.snapshot,
            aliases=list(binding.aliases),
            targets=[target.read for target in targets],
            ready=any(target.read.action in {"create", "update"} for target in targets),
            review_key=digest,
        ),
    )


def _inspect_all(binding: SidecarBinding) -> list[SidecarTarget]:
    return [_inspect(binding, location) for location in binding.locations]


async def read_series_sidecar_binding(session: AsyncSession, series_id: int) -> SidecarBinding:
    """Read the bounded binding without committing the caller's transaction."""
    return await _binding(session, series_id)


async def inspect_series_sidecar_binding(
    binding: SidecarBinding, edited_fields: frozenset[str] = frozenset()
) -> tuple[SidecarTarget, ...]:
    return tuple(
        await asyncio.to_thread(
            lambda: [_inspect(binding, location, edited_fields) for location in binding.locations]
        )
    )


def _stage_or_replay(target: SidecarTarget) -> tuple[int, str]:
    with _directory(target.location) as (descriptor, directories):
        if directories != target.directories:
            raise ValueError("The sidecar folder changed. Preview again.")
        if _existing(descriptor)[0] == target.content:
            return os.dup(descriptor), ""
    return _stage(target)


async def write_editor_sidecar_target(
    session: AsyncSession,
    binding: SidecarBinding,
    target: SidecarTarget,
    edited_fields: frozenset[str],
) -> SeriesSidecarTargetRead:
    """Replay one retained destination, never discover or approve additional folders."""
    descriptor: int | None = None
    stage = ""
    try:
        expected = render_series_sidecar(
            binding.snapshot,
            binding.aliases,
            target.previous,
            updated_at=binding.updated_at,
            edited_fields=edited_fields,
        )
        if target.content != expected or target.read.action not in {"create", "update"}:
            raise ValueError("The retained sidecar plan is invalid. Preview again.")
        descriptor, stage = await asyncio.to_thread(_stage_or_replay, target)
        await lock_file_mutation_admission(session)
        await ensure_no_active_import_file_mutation(session)
        await session.execute(
            select(Series.id).where(Series.id == binding.series_id).with_for_update()
        )
        current = await _binding(session, binding.series_id)
        if replace(current, snapshot=binding.snapshot) != binding or target.location not in (
            location for location in current.locations if location.managed
        ):
            raise ValueError("Saved library metadata or folder ownership changed. Preview again.")
        await require_no_archive_publication(
            session, Path(target.location.directory), include_descendants=True
        )

        def publish() -> None:
            assert descriptor is not None
            if stage:
                _publish(target, descriptor, stage)
            else:
                with _directory(target.location) as (current_fd, directories):
                    if directories != target.directories or _existing(current_fd)[0] != expected:
                        raise ValueError("The sidecar changed before recovery. Preview again.")
                    _require_registered_folder(current_fd, target.location)
            os.fsync(descriptor)

        await asyncio.to_thread(publish)
        return target.read.model_copy(update={"action": "unchanged", "changes": []})
    except (ValueError, ValidationError, OSError) as exc:
        return target.read.model_copy(
            update={
                "action": "blocked",
                "reason": (
                    "Library metadata is saved. Sidecar writing needs retry; "
                    "check permissions and free space."
                )
                if isinstance(exc, OSError)
                else str(exc),
            }
        )
    finally:
        await session.rollback()
        if descriptor is not None:
            try:
                if stage:
                    os.unlink(stage, dir_fd=descriptor)
            except FileNotFoundError:
                pass
            finally:
                os.close(descriptor)


def _stage(target: SidecarTarget) -> tuple[int, str]:
    with _directory(target.location) as (descriptor, directories):
        if directories != target.directories or _existing(descriptor) != (
            target.previous,
            target.fingerprint,
        ):
            raise ValueError("The folder or sidecar changed after preview. Preview again.")
        stage = f".pullbox-series-{uuid4().hex}.tmp"
        stage_fd = os.open(
            stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor
        )
        try:
            with os.fdopen(stage_fd, "wb") as stream:
                stream.write(target.content)
                stream.flush()
                os.fchmod(
                    stream.fileno(),
                    stat.S_IMODE(target.fingerprint[-1]) if target.fingerprint else 0o644,
                )
                os.fsync(stream.fileno())
            return os.dup(descriptor), stage
        except BaseException:
            os.unlink(stage, dir_fd=descriptor)
            raise


def _publish(target: SidecarTarget, descriptor: int, stage: str) -> None:
    with _directory(target.location) as (current, directories):
        if (
            directories != target.directories
            or os.fstat(current).st_ino != os.fstat(descriptor).st_ino
            or _existing(descriptor) != (target.previous, target.fingerprint)
        ):
            raise ValueError("The folder or sidecar changed after preview. Preview again.")
        _require_registered_folder(descriptor, target.location)
        # Existing files use atomic replacement; a new file must never overwrite a collision.
        if target.previous is None:
            os.link(
                stage,
                "series.json",
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
                follow_symlinks=False,
            )
            os.unlink(stage, dir_fd=descriptor)
        else:
            os.replace(stage, "series.json", src_dir_fd=descriptor, dst_dir_fd=descriptor)


async def write_series_sidecar(
    session: AsyncSession, series_id: int, review_key: str
) -> SeriesSidecarWriteRead:
    prepared = await prepare_series_sidecar(session, series_id)
    if prepared.preview.review_key != review_key:
        raise ValueError("The metadata, folders, or sidecars changed after preview. Preview again.")
    if not prepared.preview.ready:
        raise ValueError("No writable series sidecars need updating. Review the preview first.")
    results = []
    written = 0
    for target in prepared.targets:
        if target.read.action not in {"create", "update"}:
            results.append(target.read)
            continue
        # Join the whole short item, including stage cleanup, before releasing its session.
        result = await finish_short_mutation(
            asyncio.create_task(_write_target(session, prepared.binding, target))
        )
        written += result.action == "unchanged"
        results.append(result)
    return SeriesSidecarWriteRead(
        written=written,
        unchanged=sum(target.read.action == "unchanged" for target in prepared.targets),
        targets=results,
    )


async def _write_target(
    session: AsyncSession, binding: SidecarBinding, target: SidecarTarget
) -> SeriesSidecarTargetRead:
    descriptor: int | None = None
    stage = ""
    try:
        stage_descriptor, stage = await asyncio.to_thread(_stage, target)
        descriptor = stage_descriptor
        await lock_file_mutation_admission(session)
        await ensure_no_active_import_file_mutation(session)
        if await _binding(session, binding.series_id) != binding:
            raise ValueError("The series metadata or locations changed. Preview again.")
        await require_no_archive_publication(
            session, Path(target.location.directory), include_descendants=True
        )
        await asyncio.to_thread(_publish, target, stage_descriptor, stage)
        return target.read.model_copy(update={"action": "unchanged", "changes": []})
    except (ValueError, ValidationError, OSError) as exc:
        message = (
            "Writing this sidecar failed. Its previous contents were preserved; "
            "check permissions and free space."
            if isinstance(exc, OSError)
            else str(exc)
        )
        return target.read.model_copy(update={"action": "blocked", "reason": message})
    finally:
        await session.rollback()
        if descriptor is not None:
            try:
                os.unlink(stage, dir_fd=descriptor)
            except FileNotFoundError:
                pass
            finally:
                os.close(descriptor)
