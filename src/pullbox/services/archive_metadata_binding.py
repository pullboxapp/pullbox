"""Independent database and filesystem binding for coordinated archive writes."""

import asyncio
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.exceptions import NotFoundError
from pullbox.core.library_file_ownership import (
    ReferencedFileMutationError,
    require_mutable_library_target,
)
from pullbox.core.metadata_identity import (
    ExternalIdentityRef,
    IdentityNamespace,
    MetadataEntityKind,
)
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models import Issue, LibraryFile, LibraryRoot, Series
from pullbox.models.import_job import ImportedFile, ImportJob, ImportJobStatus
from pullbox.models.library import FileFormat, LibraryFileStorageMode
from pullbox.models.metadata_source import MetadataSourceConfig
from pullbox.schemas.metadata_snapshot import MetadataSnapshot
from pullbox.schemas.metadata_sources import MetadataDomain, ProviderIssueRead
from pullbox.services.archive_metadata_reconciliation import ArchiveMetadataReconciliation
from pullbox.services.metadata_assembly import assemble_metadata
from pullbox.services.metadata_series_refresh_state import (
    RefreshEntityState,
    SeriesRefreshState,
    read_series_refresh_state,
)

type FileFingerprint = tuple[int, int, int, int, int, int]
type DirectoryFingerprint = tuple[Path, int, int, int]


class ArchiveMetadataBindingError(ValueError):
    """A file cannot safely use the captured canonical metadata."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ArchiveMetadataBinding:
    """Immutable read set, not a lease, publication journal or filesystem lock."""

    library_file_id: int
    library_root_id: int
    root_path: str
    file_path: str
    file_size: int
    file_modified_at: datetime
    metadata: SeriesRefreshState


def archive_primary_identity(
    binding: ArchiveMetadataBinding, archive: ArchiveMetadataReconciliation
) -> ExternalIdentityRef | None:
    """Retain a verified embedded primary, otherwise use saved core-field priority."""
    return select_archive_primary_identity(binding.metadata, archive)


def select_archive_primary_identity(
    metadata: SeriesRefreshState, archive: ArchiveMetadataReconciliation
) -> ExternalIdentityRef | None:
    """Use the same verified primary selection inside a conversion's single pass."""
    identities = metadata.issues[0].identities
    if archive.metroninfo.metron is not None:
        for item in archive.metroninfo.metron.identities:
            if item.primary and item.evidence.identity in identities:
                return item.evidence.identity
    ranks: dict[IdentityNamespace, int] = {}
    for policy in metadata.policies:
        rank = policy.domain_priorities.get(MetadataDomain.CORE, policy.priority)
        namespace = policy.identity_namespace
        ranks[namespace] = min(rank, ranks.get(namespace, rank))
    return min(
        identities,
        key=lambda ref: (ranks.get(ref.namespace, 1001), ref.namespace.value, ref.external_id),
        default=None,
    )


async def read_archive_metadata_binding(
    session: AsyncSession,
    library_file_id: int,
    *,
    expected_issue_id: int | None = None,
    allow_conversion_source: bool = False,
) -> ArchiveMetadataBinding:
    """Read one managed CBZ and its exact parent without provider or file I/O.

    Use a short clean reader session, then release it before inspecting/staging
    archives. The invoking workflow must revalidate this read set within its
    journal/serialization boundary; a successful read does not authorize a later
    blind replacement. Reference-only and unverified legacy bindings fail closed.
    Conversion callers may explicitly read a managed CBR, CB7 or PDF instead.
    """
    if session.new or session.dirty or session.deleted:
        raise ArchiveMetadataBindingError("pending_session_changes")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in (
            library_file_id,
            *((expected_issue_id,) if expected_issue_id is not None else ()),
        )
    ):
        raise ArchiveMetadataBindingError("invalid_target")
    row = (
        await session.execute(
            select(LibraryFile, LibraryRoot, Issue.series_id)
            .join(LibraryRoot, LibraryRoot.id == LibraryFile.library_root_id)
            .join(Issue, Issue.id == LibraryFile.issue_id)
            .where(LibraryFile.id == library_file_id)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        raise ArchiveMetadataBindingError("target_missing")
    file, root, series_id = row
    if file.storage_mode is not LibraryFileStorageMode.MANAGED:
        raise ArchiveMetadataBindingError("reference_only")
    if not root.enabled or not root.allow_managed_writes:
        raise ArchiveMetadataBindingError("root_not_managed")
    if file.file_format is not FileFormat.CBZ and not (
        allow_conversion_source
        and file.file_format in {FileFormat.CBR, FileFormat.CB7, FileFormat.PDF}
    ):
        raise ArchiveMetadataBindingError("unsupported_format")
    if file.issue_id is None or (
        expected_issue_id is not None and expected_issue_id != file.issue_id
    ):
        raise ArchiveMetadataBindingError("issue_changed")
    try:
        # Writing one verified issue does not require the entire catalog to be complete.
        metadata = await read_series_refresh_state(
            session, series_id, issue_ids=(file.issue_id,), allow_partial_catalog=True
        )
        for entity in (metadata.series, metadata.issues[0]):
            _require_verified(entity)
        if not {ref.namespace for ref in metadata.issues[0].identities} <= {
            ref.namespace for ref in metadata.series.identities
        }:
            raise ArchiveMetadataBindingError("parent_identity_missing")
        # Validate baseline ownership even before reading untrusted archive input.
        assemble_archive_metadata_state(metadata, None, now=datetime.now(UTC))
    except (ValueError, NotFoundError) as exc:
        if isinstance(exc, ArchiveMetadataBindingError):
            raise
        raise ArchiveMetadataBindingError("metadata_requires_review") from None
    return ArchiveMetadataBinding(
        file.id,
        root.id,
        root.path,
        file.file_path,
        file.file_size,
        file.file_modified_at,
        metadata,
    )


def _require_verified(entity: RefreshEntityState) -> None:
    if not entity.claims or any(
        state is not IdentityVerificationState.VERIFIED for _, state, _ in entity.claims
    ):
        raise ArchiveMetadataBindingError("identity_requires_review")
    comicvine = next(
        (ref for ref in entity.identities if ref.namespace is IdentityNamespace.COMICVINE), None
    )
    if (comicvine is not None and entity.comicvine_id != int(comicvine.external_id)) or (
        entity.comicvine_id is not None and comicvine is None
    ):
        raise ArchiveMetadataBindingError("identity_requires_review")


async def revalidate_archive_metadata_binding(
    session: AsyncSession, binding: ArchiveMetadataBinding
) -> None:
    """Reject stale targets, ownership, policies, baselines and user values."""
    current = await read_archive_metadata_binding(
        session, binding.library_file_id, expected_issue_id=binding.metadata.issues[0].local_id
    )
    if current != binding:
        raise ArchiveMetadataBindingError("binding_changed")


async def lock_archive_metadata_binding(
    session: AsyncSession, binding: ArchiveMetadataBinding
) -> None:
    """Lock the captured metadata rows in the publication's established order."""
    await session.execute(
        select(MetadataSourceConfig.id)
        .order_by(MetadataSourceConfig.source)
        .with_for_update(read=True)
    )
    for model, local_id in (
        (Series, binding.metadata.series.local_id),
        (Issue, binding.metadata.issues[0].local_id),
        (LibraryRoot, binding.library_root_id),
        (LibraryFile, binding.library_file_id),
    ):
        await session.execute(select(model.id).where(model.id == local_id).with_for_update())


async def require_unowned_metadata_file(session: AsyncSession, file_id: int) -> None:
    """An independent write cannot replace retained import rollback evidence."""
    protected = await session.scalar(
        select(ImportedFile.id)
        .join(ImportJob)
        .where(
            ImportedFile.library_file_id == file_id,
            ImportJob.status != ImportJobStatus.ROLLED_BACK,
        )
        .limit(1)
    )
    if protected:
        raise ArchiveMetadataBindingError("import_rollback_protected")


@dataclass(frozen=True)
class ArchiveMetadataTarget:
    binding: ArchiveMetadataBinding
    path: Path
    fingerprint: FileFingerprint
    directories: tuple[DirectoryFingerprint, ...]

    def check_unchanged(self) -> None:
        """Synchronous stat boundary; offload on async paths and check before publish."""
        if _inspect_target(self.binding, allow_conversion_source=True) != self:
            raise ArchiveMetadataBindingError("source_changed")


async def inspect_archive_metadata_target(
    binding: ArchiveMetadataBinding, *, allow_conversion_source: bool = False
) -> ArchiveMetadataTarget:
    """Inspect outside the DB session; never open archives or perform write probes."""
    return await asyncio.to_thread(
        _inspect_target, binding, allow_conversion_source=allow_conversion_source
    )


async def revalidate_archive_metadata_target(
    session: AsyncSession, target: ArchiveMetadataTarget
) -> None:
    """Revalidate the DB binding and canonical-path ownership before publication."""
    await revalidate_archive_metadata_binding(session, target.binding)
    try:
        await require_mutable_library_target(
            session, target.path, include_descendants=False, operation="updated"
        )
    except ReferencedFileMutationError:
        raise ArchiveMetadataBindingError("reference_only") from None


def _inspect_target(
    binding: ArchiveMetadataBinding, *, allow_conversion_source: bool = False
) -> ArchiveMetadataTarget:
    try:
        raw_root, raw_file = Path(binding.root_path), Path(binding.file_path)
        if any(
            not path.is_absolute()
            or ".." in path.parts
            or any(ord(c) < 32 or ord(c) == 127 for c in str(path))
            for path in (raw_root, raw_file)
        ):
            raise ArchiveMetadataBindingError("unsafe_path")
        if not raw_file.is_relative_to(raw_root) or raw_file == raw_root:
            raise ArchiveMetadataBindingError("outside_root")
        if raw_file.suffix.casefold() not in {".cbz", ".zip"} and not (
            allow_conversion_source and raw_file.suffix.casefold() in {".cbr", ".cb7", ".pdf"}
        ):
            raise ArchiveMetadataBindingError("unsupported_format")
        root = raw_root.resolve(strict=True)
        path = root / raw_file.relative_to(raw_root)
        if path.resolve(strict=True) != path:
            raise ArchiveMetadataBindingError("unsafe_path")
        directories = []
        parent = root
        for part in ("", *path.parent.relative_to(root).parts):
            parent = parent / part
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise ArchiveMetadataBindingError("unsafe_path")
            directories.append((parent, info.st_dev, info.st_ino, info.st_mode))
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ArchiveMetadataBindingError("unsafe_path")
        if (
            not info.st_mode & 0o222
            or not directories[-1][3] & 0o222
            or not os.access(path, os.R_OK | os.W_OK)
            or not os.access(path.parent, os.W_OK | os.X_OK)
        ):
            raise ArchiveMetadataBindingError("readonly_source")
        if (
            info.st_size != binding.file_size
            or datetime.fromtimestamp(info.st_mtime, UTC) != binding.file_modified_at
        ):
            raise ArchiveMetadataBindingError("source_changed")
        return ArchiveMetadataTarget(
            binding,
            path,
            (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
                info.st_mode,
            ),
            tuple(directories),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ArchiveMetadataBindingError):
            raise
        raise ArchiveMetadataBindingError("source_unavailable") from None


def assemble_bound_archive_metadata(
    binding: ArchiveMetadataBinding,
    archive: ArchiveMetadataReconciliation,
    *,
    now: datetime,
    issue_candidates: Sequence[ProviderIssueRead] = (),
) -> tuple[MetadataSnapshot, MetadataSnapshot]:
    """Combine DB values and local evidence without granting embedded IDs ownership."""
    try:
        return assemble_archive_metadata_state(
            binding.metadata, archive, now=now, issue_candidates=issue_candidates
        )
    except ValueError:
        raise ArchiveMetadataBindingError("metadata_requires_review") from None


def assemble_archive_metadata_state(
    metadata: SeriesRefreshState,
    archive: ArchiveMetadataReconciliation | None,
    *,
    now: datetime,
    issue_candidates: Sequence[ProviderIssueRead] = (),
) -> tuple[MetadataSnapshot, MetadataSnapshot]:
    """Pure assembly shared by the bound writer and its one-pass conversion worker."""
    series, issue = metadata.series, metadata.issues[0]
    return (
        assemble_metadata(
            MetadataEntityKind.SERIES,
            series.identities,
            (),
            metadata.policies,
            now=now,
            current=series.values,
            previous=series.baseline,
            overrides=series.overrides,
            archive=archive,
        ),
        assemble_metadata(
            MetadataEntityKind.ISSUE,
            issue.identities,
            issue_candidates,
            metadata.policies,
            now=now,
            current=issue.values,
            previous=issue.baseline,
            overrides=issue.overrides,
            parent_identities=series.identities,
            archive=archive,
        ),
    )
