"""Reviewed native files consume the existing single-pass conversion journal."""

import asyncio
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pullbox.core.exceptions import JobCancelledError
from pullbox.models import LibraryFile
from pullbox.models.download import DownloadHistory, DownloadState
from pullbox.models.library_conversion import LibraryConversion
from pullbox.schemas.issue_file_metadata import FileMetadataChoices
from pullbox.services.archive_metadata_binding import (
    lock_archive_metadata_binding,
    require_unowned_metadata_file,
    revalidate_archive_metadata_target,
)
from pullbox.services.archive_metadata_publication import ArchivePublicationError
from pullbox.services.issue_file_service import resolve_configured_utility_trash_dir
from pullbox.services.library_conversion_files import (
    ConversionPlan,
    decode_plan,
    inspect_conversion,
    matches,
)
from pullbox.services.library_conversion_recovery import recover_conversion
from pullbox.services.library_convert_service import convert_library_file
from pullbox.services.library_mutation_coordination import lock_file_mutation_admission
from pullbox.utilities.executors.archive_subprocess import ControlCheck, ProgressCallback
from pullbox.utilities.import_guards import ensure_no_active_import_file_mutation
from pullbox.utilities.job_queue_cancellation import drain_task


async def _verify_conversion(session: AsyncSession, plan: ConversionPlan, issue_id: int) -> None:
    if plan.binding.issue_id != issue_id or plan.reviewed_metadata is None:
        raise ArchivePublicationError("binding_changed")
    inspected = await inspect_conversion(plan)
    for name in ("output", "backup"):
        actual, expected = inspected[name], getattr(plan, name)
        if (
            actual is None
            or not matches(actual.fingerprint, expected.fingerprint)
            or actual.digest != expected.digest
        ):
            raise ArchivePublicationError("source_changed")
    file = await session.get(LibraryFile, plan.binding.file_id)
    if file is None or file.issue_id != issue_id or file.file_path != str(plan.output.path):
        raise ArchivePublicationError("binding_changed")


async def write_native_file_metadata(
    factory: async_sessionmaker[AsyncSession],
    issue_id: int,
    review_key: str,
    operation: UUID,
    *,
    choices: FileMetadataChoices | None,
    check_control: ControlCheck,
    progress: ProgressCallback,
    limit: int,
) -> str:
    from pullbox.services.issue_file_metadata import prepare_file_metadata

    conversion_id = uuid5(operation, "native-metadata")
    await check_control()
    async with factory() as session:
        row = await session.scalar(
            select(LibraryConversion).where(LibraryConversion.operation_id == str(conversion_id))
        )
        recovered = row is not None
        if row is None:
            prepared = await prepare_file_metadata(
                session, issue_id, choices=choices, approved_resource_limit=limit
            )
            if not prepared.preview.ready:
                raise ArchivePublicationError("unresolved_conflicts")
            if prepared.preview.review_key != review_key:
                raise ArchivePublicationError("approval_changed")
            trash = await resolve_configured_utility_trash_dir(session)
            if trash is None:
                raise ArchivePublicationError("trash_not_configured")
            await session.commit()
            try:
                await convert_library_file(
                    session,
                    source=prepared.target.path,
                    trash_dir=trash,
                    trash_relative_path=prepared.target.path.name,
                    operation_id=conversion_id,
                    require_paired_metadata=True,
                    reviewed_metadata=prepared,
                    check_control=check_control,
                    progress=progress,
                )
            except (JobCancelledError, asyncio.CancelledError):

                async def settle() -> str | None:
                    async with factory() as reader:
                        state = await recover_conversion(reader, conversion_id)
                    if state == "complete":
                        return await recover_native_file_metadata(factory, issue_id, operation)
                    return None

                if await drain_task(asyncio.create_task(settle())) == "recovered":
                    return "recovered"
                raise
            row = await session.scalar(
                select(LibraryConversion).where(
                    LibraryConversion.operation_id == str(conversion_id)
                )
            )
        assert row is not None
    await recover_native_file_metadata(factory, issue_id, operation)
    return "recovered" if recovered else "written"


async def recover_native_file_metadata(
    factory: async_sessionmaker[AsyncSession], issue_id: int, operation: UUID
) -> str | None:
    """Settle proven conversion only; recovery never constructs or rewrites an archive."""
    from pullbox.services.issue_file_metadata import prepare_file_metadata

    conversion_id = uuid5(operation, "native-metadata")
    async with factory() as session:
        row = await session.scalar(
            select(LibraryConversion).where(LibraryConversion.operation_id == str(conversion_id))
        )
        if row is None:
            return None
        plan = decode_plan(row.plan_json)
        await session.commit()
        if await recover_conversion(session, conversion_id) != "complete":
            raise ArchivePublicationError("publication_review")
        await session.commit()
        await _verify_conversion(session, plan, issue_id)
        await session.commit()
        # A lost job acknowledgement is not permission to overwrite changed output.
        current = await prepare_file_metadata(session, issue_id)
        if not current.preview.ready or not current.preview.unchanged:
            raise ArchivePublicationError("source_changed")
    # Only this proven native-copy conversion can hand a failed download its CBZ.
    async with factory.begin() as session:
        await lock_file_mutation_admission(session)
        await ensure_no_active_import_file_mutation(session)
        await lock_archive_metadata_binding(session, current.target.binding)
        await revalidate_archive_metadata_target(session, current.target)
        await require_unowned_metadata_file(session, current.target.binding.library_file_id)
        await asyncio.to_thread(current.target.check_unchanged)
        downloads = await session.scalars(
            select(DownloadHistory)
            .where(
                DownloadHistory.issue_id == issue_id,
                DownloadHistory.final_path == str(plan.original.path),
                DownloadHistory.state == DownloadState.FAILED,
                DownloadHistory.imported_at.is_(None),
                DownloadHistory.post_processing_claim_token.is_(None),
            )
            .with_for_update()
        )
        for download in downloads:
            download.final_path = str(plan.output.path)
    return "recovered"
