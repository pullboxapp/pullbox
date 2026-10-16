"""Private paired outputs for the direct pack's existing batch transaction."""

from __future__ import annotations

import asyncio
import copy
import stat
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from pullbox.core.file_publication import publish_file_without_overwrite
from pullbox.core.library_file_ownership import build_file_identity_signature
from pullbox.models.direct_acquisition import DirectAcquisitionAttempt, DirectArtifactAttempt
from pullbox.models.download import DownloadHistory
from pullbox.services.archive_metadata_binding import ArchiveMetadataBindingError
from pullbox.services.direct_paired_metadata import DirectMetadataReviewError
from pullbox.services.manual_paired_metadata import (
    _directories,
    _lock_plan,
    read_manual_metadata_plan,
    stage_manual_metadata,
)
from pullbox.utilities.executors.archive_metadata_staging import ArchiveMetadataStagingError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.issue import IssueStatus
    from pullbox.services.direct_paired_metadata import DirectMetadataHandoff
    from pullbox.services.issue_import_service import PreparedManualIssueImport
    from pullbox.services.manual_paired_metadata import ManualMetadataPlan
    from pullbox.utilities.executors.archive_metadata_staging import StagedArchiveMetadata
    from pullbox.utilities.executors.archive_subprocess import ControlCheck

logger = structlog.get_logger(__name__)


@dataclass
class StagedPackMember:
    plan: ManualMetadataPlan
    staged: StagedArchiveMetadata
    source_pack: Path
    pack_signature: dict[str, int | str]
    root_directories: tuple[tuple[str, int, int], ...]
    cancellation_check: ControlCheck
    published: list[Path] = field(default_factory=list)

    async def materialize(self, source: Path, target: Path, *_args: Any, **_kwargs: Any) -> bool:
        """Publish only the pre-reconciled output while the pack owner holds row locks."""
        await self.cancellation_check()
        if (
            await asyncio.to_thread(build_file_identity_signature, self.source_pack)
            != self.pack_signature
        ):
            raise DirectMetadataReviewError("The downloaded pack changed; review before retrying.")
        root = Path(self.plan.root_path)
        directories = await asyncio.to_thread(_directories, target, root)
        if directories[-len(self.root_directories) :] != self.root_directories:
            raise DirectMetadataReviewError(
                "The library destination changed; review before retrying."
            )
        await asyncio.to_thread(self.staged.check_unchanged)
        if source != self.staged.path and source != self.staged.source_path:
            raise DirectMetadataReviewError("The pack member changed; review before retrying.")
        # Record the intended path before publishing so a registration failure can
        # remove only this exact output, including failures after publication.
        self.published.append(target)
        publication = asyncio.create_task(
            asyncio.to_thread(publish_file_without_overwrite, self.staged.path, target)
        )
        try:
            await asyncio.shield(publication)
        except asyncio.CancelledError:
            # A thread cannot be cancelled. Drain its short atomic claim before
            # the batch cleans private stages or rolls back its owned outputs.
            await publication
            raise
        return True

    async def cleanup(self) -> None:
        for path in self.published:
            await asyncio.to_thread(self._cleanup_output, path)

    def _cleanup_output(self, path: Path) -> None:
        try:
            current = path.lstat()
        except FileNotFoundError:
            return
        expected = self.staged.output_fingerprint
        if (
            stat.S_ISREG(current.st_mode)
            and (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
            == expected[:4]
        ):
            path.unlink()
        else:
            logger.warning("direct_pack_cleanup_output_changed", path=str(path))


@asynccontextmanager
async def stage_direct_pack_metadata(
    session: AsyncSession,
    prepared: Sequence[PreparedManualIssueImport],
    source_pack: Path,
    *,
    handoff: DirectMetadataHandoff | None,
    allow_resource_safety_exception: bool,
    cancellation_check: ControlCheck,
) -> AsyncIterator[dict[int, StagedPackMember]]:
    """Reconcile every member before any registration; no intermediate commits."""
    staged_members: dict[int, StagedPackMember] = {}
    if handoff is None:
        raise DirectMetadataReviewError("The direct pack no longer has a verified owner.")
    attempt = await session.get(DirectAcquisitionAttempt, handoff.acquisition_id)
    if attempt is None:
        raise DirectMetadataReviewError("The direct pack no longer exists.")
    expected_plan = copy.deepcopy(attempt.plan_snapshot)
    statuses: dict[int, IssueStatus] = {item.issue_id: item.issue.status for item in prepared}
    signature = await asyncio.to_thread(build_file_identity_signature, source_pack)
    async with AsyncExitStack() as stack:
        try:
            for item in prepared:
                if not item.ingest_policy.update_embedded_comicinfo_from_match:
                    continue
                plan = await read_manual_metadata_plan(session, item.issue_id)
                if plan.policy != item.ingest_policy or plan.policy.post_processing_method not in {
                    "copy",
                    "move",
                }:
                    raise DirectMetadataReviewError(
                        "Import settings changed; review before retrying."
                    )
                root = Path(plan.root_path)
                directories = await asyncio.to_thread(_directories, root / "pack-stage.cbz", root)
                staged = await stack.enter_async_context(
                    stage_manual_metadata(
                        plan,
                        item.source_path,
                        root,
                        allow_resource_safety_exception=allow_resource_safety_exception,
                        cancellation_check=cancellation_check,
                    )
                )
                staged_members[item.issue_id] = StagedPackMember(
                    plan, staged, source_pack, signature, directories, cancellation_check
                )
            await cancellation_check()
            for model, local_id in (
                (DirectAcquisitionAttempt, handoff.acquisition_id),
                (DirectArtifactAttempt, handoff.artifact_id),
                (DownloadHistory, handoff.download_id),
            ):
                await session.execute(
                    select(model)
                    .where(model.id == local_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            attempt, _ = await handoff.owner(session)
            if attempt.plan_snapshot != expected_plan:
                raise DirectMetadataReviewError(
                    "The direct pack selection changed; review before retrying."
                )
            # All archive work is finished. Lock and revalidate the entire read set
            # before the first flush, not through separate readers after mutation.
            for item in prepared:
                member = staged_members.get(item.issue_id)
                if member is None:
                    continue
                await _lock_plan(session, member.plan)
                if (
                    await read_manual_metadata_plan(session, item.issue_id) != member.plan
                    or item.issue.status != statuses[item.issue_id]
                ):
                    raise DirectMetadataReviewError(
                        "Pack metadata or ownership changed; review before retrying."
                    )
            yield staged_members
        except (ArchiveMetadataBindingError, ArchiveMetadataStagingError, ValueError) as exc:
            for member in staged_members.values():
                await member.cleanup()
            raise DirectMetadataReviewError(
                "Pack metadata needs review. No pack issues were imported. "
                "Check the member's ComicInfo.xml / MetronInfo.xml against its selected issue, "
                "correct the source if needed, then retry."
            ) from exc
        except BaseException:
            for member in staged_members.values():
                await member.cleanup()
            raise
