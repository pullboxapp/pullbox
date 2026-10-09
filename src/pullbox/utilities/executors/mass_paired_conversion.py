"""Serial Mass Convert adapter for the existing coordinated paired converter."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import func, select

from pullbox.config import get_settings
from pullbox.core.exceptions import JobCancelledError, ValidationError
from pullbox.models import LibraryFile
from pullbox.models.library import LibraryFileStorageMode
from pullbox.models.library_conversion import LibraryConversion
from pullbox.services.archive_metadata_binding import (
    ArchiveMetadataBindingError,
    require_unowned_metadata_file,
)
from pullbox.services.archive_metadata_publication import _digest, _fingerprint
from pullbox.services.library_conversion_files import (
    check_directories,
    decode_plan,
    inspect_conversion,
    matches,
    require_writable_conversion_source,
)
from pullbox.services.library_conversion_recovery import read_conversion_binding, recover_conversion
from pullbox.services.library_convert_service import convert_library_file
from pullbox.utilities.base_executor import ItemResult, ProcessedItem
from pullbox.utilities.import_guards import ensure_no_active_import_file_mutation
from pullbox.utilities.models import ItemState, JobState, JobType, UtilityJob, UtilityJobItem

logger = structlog.get_logger(__name__)


def paired_mass_enabled(config: dict[str, Any]) -> bool:
    return get_settings().metadata_paired_conversion_writer_enabled and 2 in config.get("steps", [])


async def completed_receipt(factory: Any, item_id: str) -> ProcessedItem | None:
    async with factory() as session:
        row = await session.scalar(
            select(LibraryConversion).where(LibraryConversion.operation_id == str(UUID(item_id)))
        )
        if row is None:
            return None
        encoded = row.plan_json
        state = await recover_conversion(session, UUID(item_id))
        if state != "complete":
            raise ValidationError("Conversion recovery needs review; the original was preserved.")
        plan = decode_plan(encoded)
        actual = await inspect_conversion(plan)
        unchanged = plan.same_path or actual["original"] is None
        for name in ("output", "backup"):
            artifact = actual[name]
            unchanged = unchanged and bool(
                artifact is not None
                and matches(artifact.fingerprint, getattr(plan, name).fingerprint)
                and artifact.digest == getattr(plan, name).digest
            )
        if not unchanged:
            raise ValidationError(
                "Converted files changed; review them before retrying or rollback."
            )
        binding = await read_conversion_binding(session, plan.output.path)
        if (binding.file_id, binding.issue_id, binding.root_id) != (
            plan.binding.file_id,
            plan.binding.issue_id,
            plan.binding.root_id,
        ):
            raise ValidationError("The converted file's library match changed; review it first.")
        item = await session.get(UtilityJobItem, item_id)
        before = json.loads(item.before_state or "{}") if item else {}
        before.update(path=str(plan.original.path), format=plan.original.path.suffix.lstrip("."))
        return ProcessedItem(
            item_id,
            ItemResult.COMPLETED,
            before_state=before,
            after_state={
                "path": str(plan.output.path),
                "format": "cbz",
                "original_path": str(plan.backup.path),
                "metadata_embedded": True,
                "verified": True,
                "conversion_plan": encoded,
            },
            log_entries=[
                (
                    "INFO",
                    "Converted with reconciled ComicInfo.xml and MetronInfo.xml; "
                    "original backed up.",
                    {},
                )
            ],
        )


async def process_paired_mass(
    item_data: dict[str, Any], config: dict[str, Any], context: dict[str, Any]
) -> ProcessedItem:
    from pullbox.utilities.executors.mass_convert_pipeline import _resolve_effective_trash_directory

    item_id = item_data["id"]
    source = Path(item_data["file_path"])
    factory = context["factory"]
    task: asyncio.Task[Any] | None = None

    def skipped(reason: str) -> ProcessedItem:
        return ProcessedItem(
            item_id,
            ItemResult.SKIPPED,
            warning_message=reason,
            before_state={"path": str(source)},
            after_state={"path": str(source), "reason": reason},
            log_entries=[("WARNING", reason, {})],
        )

    async def check_control(job_id: str) -> None:
        async with factory() as session:
            job = await session.get(UtilityJob, job_id)
            if job is None or job.state not in {JobState.RUNNING, JobState.PAUSING}:
                raise JobCancelledError("Conversion cancelled; private preparation stopped.")
            await ensure_no_active_import_file_mutation(session)

    try:
        receipt = await completed_receipt(factory, item_id)
        if receipt is not None:
            return receipt
        if not paired_mass_enabled(config):
            raise ValidationError(
                "Paired conversion was disabled. Queue a new job after reviewing settings."
            )
        if source.suffix.casefold() not in {".cbz", ".cb7", ".cbr", ".pdf"}:
            return skipped(
                "Paired Mass Convert supports managed CBZ, CBR, CB7, and PDF files only."
            )
        async with factory.begin() as session:
            item = await session.get(UtilityJobItem, item_id)
            if item is None:
                raise ValidationError("Conversion job item is no longer available.")
            job_id = item.job_id
            file = await session.scalar(
                select(LibraryFile).where(LibraryFile.file_path == str(source))
            )
            if file is None or file.issue_id is None:
                return skipped(
                    "A verified library issue match is required; the file was left unchanged."
                )
            if file.storage_mode is not LibraryFileStorageMode.MANAGED:
                return skipped(
                    "Referenced files cannot be converted; the source was left unchanged."
                )
            await require_unowned_metadata_file(session, file.id)
            try:
                await read_conversion_binding(session, source)
            except ValidationError:
                return skipped(
                    "This library root does not allow managed conversion; "
                    "the source was left unchanged."
                )
            try:
                require_writable_conversion_source(source)
            except ValidationError:
                return skipped(
                    "Read-only source files cannot be converted; the source was left unchanged."
                )
            before = json.loads(item.before_state or "{}")
            before.update(
                {
                    "path": str(source),
                    "format": source.suffix.lstrip("."),
                    "has_comicinfo": file.has_comicinfo,
                }
            )
            item.before_state = json.dumps(before)
        await check_control(job_id)

        async def convert() -> None:
            async with factory() as session:
                await convert_library_file(
                    session,
                    source=source,
                    trash_dir=_resolve_effective_trash_directory(config.get("trash_folder")),
                    trash_relative_path=item_data.get("trash_relative_path") or source.name,
                    operation_id=UUID(item_id),
                    require_paired_metadata=True,
                    repack_cbz=True,
                )

        task = asyncio.create_task(convert())
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=0.2)
            if not done:
                await check_control(job_id)
        await task
        receipt = await completed_receipt(factory, item_id)
        if receipt is None:
            raise ValidationError(
                "Conversion completed without its durable receipt; review the file."
            )
        return receipt
    except (JobCancelledError, asyncio.CancelledError):
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        # A short publication may already have committed. Keep its rollback receipt.
        try:
            receipt = await completed_receipt(factory, item_id)
            if receipt is not None:
                return receipt
        except ValidationError:
            pass
        return ProcessedItem(item_id, ItemResult.CANCELLED)
    except ArchiveMetadataBindingError as exc:
        if exc.code == "import_rollback_protected":
            return skipped(
                "This file belongs to an import rollback journal; it was left unchanged."
            )
        return ProcessedItem(
            item_id,
            ItemResult.FAILED,
            error_message="Review the file's issue identity before converting.",
        )
    except Exception as exc:
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        with suppress(ValidationError):
            receipt = await completed_receipt(factory, item_id)
            if receipt is not None:
                return receipt
        return ProcessedItem(item_id, ItemResult.FAILED, error_message=str(exc))


def guard_paired_rollback(before: dict[str, Any], after: dict[str, Any]) -> None:
    encoded = after.get("conversion_plan")
    if encoded is None:
        return
    plan = decode_plan(encoded)
    if (before.get("path"), after.get("path"), after.get("original_path")) != (
        str(plan.original.path),
        str(plan.output.path),
        str(plan.backup.path),
    ):
        raise ValidationError("Conversion rollback paths disagree with their durable evidence.")
    check_directories(plan)
    if not plan.same_path and _fingerprint(plan.original.path) is not None:
        raise ValidationError("The original location is occupied; rollback was not applied.")
    for artifact in (plan.output, plan.backup):
        actual = _fingerprint(artifact.path)
        if (
            not matches(actual, artifact.fingerprint)
            or actual is None
            or _digest(artifact.path, actual, Event()) != artifact.digest
        ):
            raise ValidationError("Converted output or backup changed; rollback was not applied.")


async def recover_paired_mass_jobs(factory: Any) -> None:
    """Settle only interrupted Mass items carrying an existing conversion receipt."""
    from pullbox.services.utility_operation_progress import project_utility_operation_progress

    cursor = 0
    while True:
        async with factory() as session:
            rows = (
                await session.execute(
                    select(LibraryConversion.id, UtilityJobItem.id)
                    .join(
                        UtilityJobItem,
                        func.replace(LibraryConversion.operation_id, "-", "") == UtilityJobItem.id,
                    )
                    .join(UtilityJob, UtilityJob.id == UtilityJobItem.job_id)
                    .where(
                        LibraryConversion.id > cursor,
                        UtilityJob.job_type == JobType.MASS_CONVERT_PIPELINE,
                        UtilityJobItem.state.in_([ItemState.IN_PROGRESS, ItemState.PENDING]),
                        UtilityJob.state.in_(
                            [
                                JobState.RUNNING,
                                JobState.PAUSING,
                                JobState.CANCELLING,
                                JobState.PAUSED,
                            ]
                        ),
                    )
                    .order_by(LibraryConversion.id)
                    .limit(64)
                )
            ).all()
        if not rows:
            return
        for row_id, item_id in rows:
            cursor = row_id
            try:
                receipt = await completed_receipt(factory, item_id)
                if receipt is None:
                    continue
            except (ValidationError, OSError, ValueError):
                logger.warning("mass_conversion_receipt_needs_review", item_id=item_id)
                continue
            async with factory.begin() as session:
                item = await session.get(UtilityJobItem, item_id)
                if item is None or item.state not in {ItemState.PENDING, ItemState.IN_PROGRESS}:
                    continue
                job = await session.get(UtilityJob, item.job_id)
                if job is None:
                    continue
                item.state = ItemState.COMPLETED
                item.before_state = json.dumps(receipt.before_state)
                item.after_state = json.dumps(receipt.after_state)
                item.error_message = None
                item.completed_at = datetime.now(UTC).isoformat()
                job.completed_items = await session.scalar(
                    select(func.count())
                    .select_from(UtilityJobItem)
                    .where(
                        UtilityJobItem.job_id == job.id, UtilityJobItem.state == ItemState.COMPLETED
                    )
                )
                if job.completed_items == job.total_items and job.state != JobState.CANCELLING:
                    job.state = JobState.COMPLETED
                    job.completed_at = item.completed_at
                await project_utility_operation_progress(session, job)
