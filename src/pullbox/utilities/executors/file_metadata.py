"""One approved paired write using the existing durable utility lifecycle."""

from __future__ import annotations

import asyncio
import re
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker

from pullbox.core.exceptions import JobCancelledError
from pullbox.core.file_safety import get_archive_size_limit_bytes
from pullbox.schemas.issue_file_metadata import FileMetadataReview
from pullbox.services.issue_file_metadata import file_metadata_error, write_file_metadata
from pullbox.services.operation_progress import (
    OperationItemProgress,
    OperationProgressMeasure,
    publish_operation_progress,
)
from pullbox.services.utility_operation_progress import build_utility_operation_update
from pullbox.utilities.base_executor import ExecutionMode, ItemResult, JobExecutor, ProcessedItem
from pullbox.utilities.import_guards import ensure_no_active_import_file_mutation
from pullbox.utilities.models import JobState, UtilityJob, UtilityJobItem


class FileMetadataExecutor(JobExecutor):
    execution_mode = ExecutionMode.ASYNC

    def validate_config(self, job_config: dict[str, Any]) -> list[str]:
        if (
            set(job_config)
            not in ({"issue_id", "review_key"}, {"issue_id", "review_key", "choices"})
            or type(job_config.get("issue_id")) is not int
            or job_config["issue_id"] <= 0
            or not isinstance(job_config.get("review_key"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", job_config["review_key"])
        ):
            return ["Choose an issue and approve its current file metadata preview."]
        try:
            FileMetadataReview(choices=job_config.get("choices", {}))
        except ValidationError:
            return ["Review the metadata choices before writing."]
        return []

    async def build_job_context(self, session: Any, job_config: dict[str, Any]) -> dict[str, Any]:
        errors = self.validate_config(job_config)
        if errors:
            raise ValueError(errors[0])
        await ensure_no_active_import_file_mutation(session)
        return {
            "factory": async_sessionmaker(session.bind, expire_on_commit=False),
            "limit": await get_archive_size_limit_bytes(session),
        }

    async def generate_items(
        self, job_config: dict[str, Any], job_context: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return [{"operation": "file_metadata"}]

    def process_item(
        self,
        item_data: dict[str, Any],
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> ProcessedItem:
        raise RuntimeError("Metadata publication requires the async executor")

    async def process_item_async(
        self,
        item_data: dict[str, Any],
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> ProcessedItem:
        context = job_context or {}
        factory = context["factory"]
        async with factory() as session:
            item = await session.get(UtilityJobItem, item_data["id"])
            if item is None:
                raise ValueError("Metadata job item was removed")
            job_id = item.job_id

        async def check_control() -> None:
            async with factory() as session:
                job = await session.get(UtilityJob, job_id)
                if job is None or job.state != JobState.RUNNING:
                    raise JobCancelledError("Metadata write stopped before publication")
                await ensure_no_active_import_file_mutation(session)

        async def progress(stage: str, current: int, total: int, unit: str) -> None:
            from dataclasses import replace

            async with factory.begin() as session:
                job = await session.get(UtilityJob, job_id)
                if job is None:
                    return
                update = build_utility_operation_update(job)
                ratio = min(current / total, 1) if total else 0
                # Stage-local measurements are real; overall reserves the commit/finalize tail.
                percent = (
                    (5 + 65 * ratio)
                    if stage in {"transferring", "extracting", "rendering"}
                    else (70 + 20 * ratio)
                    if stage == "verifying"
                    else 90
                )
                await publish_operation_progress(
                    session,
                    replace(
                        update,
                        phase=stage,
                        message="Rendering comic pages"
                        if stage == "rendering"
                        else "Preparing reconciled metadata"
                        if stage in {"transferring", "extracting"}
                        else "Verifying preserved comic pages",
                        overall=OperationProgressMeasure(percent=percent),
                        item=OperationItemProgress(
                            item_data["id"],
                            job.display_name,
                            stage,
                            measure=OperationProgressMeasure(
                                current=current, total=total, unit=unit
                            ),
                        ),
                    ),
                )

        try:
            outcome = await write_file_metadata(
                factory,
                job_config["issue_id"],
                job_config["review_key"],
                UUID(item_data["id"]),
                limit=context["limit"],
                check_control=check_control,
                progress=progress,
                job_id=job_id,
                choices=FileMetadataReview(choices=job_config.get("choices", {})).choices,
            )
            return ProcessedItem(
                item_data["id"],
                ItemResult.COMPLETED,
                after_state={"outcome": outcome},
                log_entries=[
                    (
                        "INFO",
                        "Reconciled metadata complete; comic pages preserved.",
                        {"outcome": outcome},
                    )
                ],
            )
        except (JobCancelledError, asyncio.CancelledError):
            return ProcessedItem(item_data["id"], ItemResult.CANCELLED)
        except Exception as exc:
            return ProcessedItem(
                item_data["id"], ItemResult.FAILED, error_message=file_metadata_error(exc)
            )

    def rollback_item(
        self,
        item_data: dict[str, Any],
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> ProcessedItem:
        return ProcessedItem(
            item_data["id"],
            ItemResult.FAILED,
            error_message="Metadata writes do not support utility rollback.",
        )
