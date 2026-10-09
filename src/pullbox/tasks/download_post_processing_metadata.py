"""Completed downloads consume existing paired publication/conversion owners."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from pullbox.core.exceptions import ValidationError
from pullbox.core.file_safety import get_archive_size_limit_bytes
from pullbox.models import LibraryFile
from pullbox.models.archive_metadata_publication import PublicationState
from pullbox.models.download import DownloadHistory, DownloadState
from pullbox.models.library import FileFormat
from pullbox.models.library_conversion import LibraryConversion
from pullbox.services.issue_file_metadata import (
    file_metadata_error,
    prepare_file_metadata,
    recover_file_metadata,
    write_file_metadata,
)
from pullbox.services.issue_file_service import resolve_configured_utility_trash_dir
from pullbox.services.library_conversion_files import decode_plan, inspect_conversion, matches
from pullbox.services.library_conversion_recovery import recover_conversion
from pullbox.services.library_convert_service import convert_library_file
from pullbox.tasks.post_processing_progress import PostProcessingPhase

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.tasks.download_post_processing_runtime import PostProcessingRuntime


async def finish_download_metadata(
    session: AsyncSession, download: DownloadHistory, runtime: PostProcessingRuntime
) -> Path:
    """Keep a durable managed copy; only the queue marks the download imported.

    A conflict leaves the original and registered copy unchanged for the existing
    issue metadata review. Retry resumes this exact copy, never an arbitrary file
    at the expected destination. Archive work uses clean, short-lived sessions.
    """
    download_id, issue_id = download.id, download.issue_id
    final_path, claim = download.final_path, download.post_processing_claim_token
    if not final_path:
        raise ValidationError("The download has no registered library copy to update.")
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    operation = uuid5(NAMESPACE_URL, f"pullbox:download-metadata:{download_id}")

    async def check_control(expected_path: str | None = None) -> None:
        async with factory() as reader:
            current = await reader.get(DownloadHistory, download_id)
            if (
                current is None
                or current.issue_id != issue_id
                or current.final_path != (expected_path or final_path)
                or current.state is not DownloadState.COMPLETED
                or current.imported_at is not None
                or current.post_processing_claim_token != claim
            ):
                raise ValidationError("The download changed; review it before retrying.")

    async def progress(_stage: str, _current: int, _total: int, _unit: str) -> None:
        await check_control()

    runtime.enter_phase(PostProcessingPhase.WRITING_METADATA)
    try:
        await check_control()
        conversion_id = uuid5(operation, "conversion")
        async with factory() as reader:
            conversion = await reader.scalar(
                select(LibraryConversion).where(
                    LibraryConversion.operation_id == str(conversion_id)
                )
            )
            if conversion is not None:
                plan = decode_plan(conversion.plan_json)
                if (
                    plan.binding.issue_id != issue_id
                    or final_path not in {str(plan.original.path), str(plan.output.path)}
                    or plan.metadata_state_digest is None
                ):
                    raise ValidationError("The download conversion needs review before retrying.")
                await reader.commit()
                if await recover_conversion(reader, conversion_id) != "complete":
                    raise ValidationError(
                        "The download conversion needs review; "
                        "its original and recovery evidence were preserved."
                    )
                inspected = await inspect_conversion(plan)
                for name in ("output", "backup"):
                    actual, expected = inspected[name], getattr(plan, name)
                    if (
                        actual is None
                        or not matches(actual.fingerprint, expected.fingerprint)
                        or actual.digest != expected.digest
                    ):
                        raise ValidationError(
                            "The converted download copy changed; review before retrying."
                        )
                final_path = str(plan.output.path)
            file = await reader.scalar(select(LibraryFile).where(LibraryFile.issue_id == issue_id))
            if file is None or file.file_path != final_path:
                raise ValidationError(
                    "The registered download copy changed; review before retrying."
                )
            if conversion is not None and file.id != plan.binding.file_id:
                raise ValidationError("The download conversion's library binding changed.")
            if file.file_format in {FileFormat.CBR, FileFormat.CB7, FileFormat.PDF}:
                trash_dir = await resolve_configured_utility_trash_dir(reader)
                if trash_dir is None:
                    raise ValidationError(
                        "Configure a library trash directory before converting this download."
                    )
                await reader.commit()
                result = await convert_library_file(
                    reader,
                    source=Path(final_path),
                    trash_dir=trash_dir,
                    trash_relative_path=Path(final_path).name,
                    operation_id=conversion_id,
                    require_paired_metadata=True,
                )
                final_path = result.target_path
        if final_path != download.final_path:
            async with factory.begin() as writer:
                current = await writer.get(DownloadHistory, download_id, with_for_update=True)
                if (
                    current is None
                    or current.final_path != download.final_path
                    or current.issue_id != issue_id
                    or current.state is not DownloadState.COMPLETED
                    or current.imported_at is not None
                    or current.post_processing_claim_token != claim
                ):
                    raise ValidationError(
                        "The download changed during conversion; review before retrying."
                    )
                current.final_path = final_path
            download.final_path = final_path
        recovered = await recover_file_metadata(factory, operation)
        async with factory() as reader:
            file = await reader.scalar(select(LibraryFile).where(LibraryFile.issue_id == issue_id))
            if file is None or file.file_path != final_path:
                raise ValidationError(
                    "The registered download copy changed; review before retrying."
                )
            if file.file_format is not FileFormat.CBZ:
                raise ValidationError(
                    "Paired download metadata currently requires a CBZ library copy. "
                    "Convert this copy to CBZ before retrying; the download original is unchanged."
                )
            limit = await get_archive_size_limit_bytes(reader)
            prepared = await prepare_file_metadata(reader, issue_id)
        if not prepared.preview.ready:
            raise ValidationError(
                "The download's embedded metadata disagrees with its library metadata. "
                "Review file metadata on the issue, then retry post-processing. "
                "The download original and library copy were left unchanged."
            )
        if prepared.preview.unchanged:
            await check_control()
            return Path(final_path)
        if recovered is PublicationState.FINALIZED:
            raise ValidationError(
                "The previously written download copy changed. "
                "Review its file metadata before retrying."
            )
        if recovered is PublicationState.ABANDONED:
            operation = uuid5(operation, prepared.preview.review_key)
        await write_file_metadata(
            factory,
            issue_id,
            prepared.preview.review_key,
            operation,
            limit=limit,
            check_control=check_control,
            progress=progress,
        )
        await check_control()
        return Path(final_path)
    except ValueError as exc:
        raise ValidationError(
            f"{file_metadata_error(exc)} "
            "Review file metadata on the issue, then retry post-processing."
        ) from exc
