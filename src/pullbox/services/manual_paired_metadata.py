"""Pre-publication paired metadata for the existing manual issue-import owner."""

from __future__ import annotations

import asyncio
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from pullbox.core.archive import ArchiveReader
from pullbox.core.archive_metadata import MAX_METADATA_BYTES
from pullbox.core.file_publication import publish_file_without_overwrite
from pullbox.core.file_safety import (
    FileSafetyError,
    check_archive_size,
    get_archive_size_limit_bytes,
    is_dangerous_file_blocking_enabled,
)
from pullbox.core.library_file_ownership import (
    build_file_identity_signature,
    require_mutable_library_target,
)
from pullbox.core.library_policy import (
    load_effective_library_ingest_policy,
    load_library_ingest_policy,
)
from pullbox.core.library_root_resolution import preferred_managed_root_id, resolve_library_root
from pullbox.models.archive_metadata_publication import ArchiveMetadataPublication
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile, LibraryRoot
from pullbox.models.library_conversion import LibraryConversion
from pullbox.models.metadata_source import MetadataSourceConfig
from pullbox.models.series import Series
from pullbox.services.archive_metadata_binding import (
    ArchiveMetadataBindingError,
    assemble_archive_metadata_state,
    require_unowned_metadata_file,
    require_verified_archive_metadata,
)
from pullbox.services.metadata_series_refresh_state import read_series_refresh_state
from pullbox.utilities.executors.archive_metadata_staging import stage_cbz_metadata_interruptible

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from pullbox.core.library_policy import LibraryIngestPolicy
    from pullbox.services.metadata_series_refresh_state import SeriesRefreshState
    from pullbox.utilities.executors.archive_subprocess import ControlCheck, ProgressCallback


@dataclass(frozen=True)
class ManualMetadataPlan:
    metadata: SeriesRefreshState
    root_id: int
    root_path: str
    policy: LibraryIngestPolicy
    series_placement: tuple[str | None, int | None, int | None]
    existing_file: tuple[int, str] | None
    limit: int
    block_dangerous: bool


async def read_manual_metadata_plan(session: AsyncSession, issue_id: int) -> ManualMetadataPlan:
    """Read one exact issue, destination and policy; no provider calls or archive I/O."""
    issue = await session.scalar(
        select(Issue)
        .options(joinedload(Issue.series), joinedload(Issue.library_file))
        .where(Issue.id == issue_id)
        .execution_options(populate_existing=True)
    )
    if issue is None:
        raise ValueError("The selected issue no longer exists.")
    series: Series = issue.series
    existing = issue.library_file
    if existing is not None:
        await require_unowned_metadata_file(session, existing.id)
        await require_mutable_library_target(
            session, Path(existing.file_path), include_descendants=False, operation="replaced"
        )
        publication = await session.scalar(
            select(ArchiveMetadataPublication.id).where(
                ArchiveMetadataPublication.active_file_id == existing.id
            )
        )
        conversion = await session.scalar(
            select(LibraryConversion.id)
            .where(
                LibraryConversion.library_file_id == existing.id,
                LibraryConversion.active.is_(True),
            )
            .limit(1)
        )
        if publication is not None or conversion is not None:
            raise ArchiveMetadataBindingError("publication_busy")
    existing_file = (existing.id, existing.file_path) if existing is not None else None
    metadata = await read_series_refresh_state(
        session, series.id, issue_ids=(issue_id,), allow_partial_catalog=True
    )
    require_verified_archive_metadata(metadata)
    root = await resolve_library_root(
        session, Path("/"), preferred_managed_root_id(series), series=series
    )
    policy = (
        await load_effective_library_ingest_policy(session, series.library_root_id)
        if series.library_root_id is not None
        else await load_library_ingest_policy(session)
    )
    return ManualMetadataPlan(
        metadata,
        root.id,
        root.path,
        policy,
        (series.path, series.library_root_id, series.preferred_library_root_id),
        existing_file,
        await get_archive_size_limit_bytes(session),
        await is_dangerous_file_blocking_enabled(session),
    )


async def _lock_plan(session: AsyncSession, plan: ManualMetadataPlan) -> None:
    await session.execute(
        select(MetadataSourceConfig.id)
        .order_by(MetadataSourceConfig.source)
        .with_for_update(read=True)
    )
    for model, local_id in (
        (Series, plan.metadata.series.local_id),
        (Issue, plan.metadata.issues[0].local_id),
        (LibraryRoot, plan.root_id),
    ):
        await session.execute(select(model.id).where(model.id == local_id).with_for_update())
    if plan.existing_file is not None:
        await session.execute(
            select(LibraryFile.id).where(LibraryFile.id == plan.existing_file[0]).with_for_update()
        )


def _directories(target: Path, root: Path) -> tuple[tuple[str, int, int], ...]:
    """Do not publish through a changed or symlinked destination directory."""
    if not target.is_relative_to(root):
        raise FileSafetyError("Manual metadata destination is outside its library root")
    result = []
    for directory in (target.parent, *target.parent.parents):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise FileSafetyError("Manual metadata destination must not contain symbolic links")
        result.append((str(directory), info.st_dev, info.st_ino))
    return tuple(result)


async def materialize_manual_metadata(
    factory: async_sessionmaker[AsyncSession],
    plan: ManualMetadataPlan,
    original: Path,
    source_signature: dict[str, int | str],
    source: Path,
    target: Path,
    *,
    allow_resource_safety_exception: bool = False,
    cancellation_check: ControlCheck | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    """Stage both XML files privately, revalidate, then publish without overwrite."""
    directories = await asyncio.to_thread(_directories, target, Path(plan.root_path))
    limit = plan.limit
    if not allow_resource_safety_exception:
        await asyncio.to_thread(check_archive_size, source, limit)
    if allow_resource_safety_exception:
        members = await asyncio.to_thread(ArchiveReader(source).list_members)
        limit = max(limit, sum(member.size for member in members) + 2 * MAX_METADATA_BYTES)
    series, issue = assemble_archive_metadata_state(plan.metadata, None, now=datetime.now(UTC))
    async with (
        stage_cbz_metadata_interruptible(
            source,
            target.parent,
            series,
            issue,
            max_uncompressed_bytes=limit,
            block_dangerous=plan.block_dangerous,
            metadata_state=plan.metadata,
            cancellation_check=cancellation_check,
            progress_callback=progress_callback,
        ) as staged,
        factory() as reader,
    ):
        await _lock_plan(reader, plan)
        if await read_manual_metadata_plan(reader, plan.metadata.issues[0].local_id) != plan:
            raise ValueError("Import metadata or library settings changed; review before retrying.")
        if await asyncio.to_thread(build_file_identity_signature, original) != source_signature:
            raise FileSafetyError("The selected import source changed during metadata preparation")
        if await asyncio.to_thread(_directories, target, Path(plan.root_path)) != directories:
            raise FileSafetyError(
                "The manual import destination changed during metadata preparation"
            )
        if cancellation_check is not None:
            await cancellation_check()
        await asyncio.to_thread(staged.check_unchanged)
        # Keep metadata/root row locks through the short publication boundary.
        await asyncio.to_thread(publish_file_without_overwrite, staged.path, target)
    return True
