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
from pullbox.schemas.issue_metadata_editor import IssueMetadataEditorReview
from pullbox.services.issue_file_metadata import file_metadata_error, write_file_metadata
from pullbox.services.operation_progress import (
    OperationItemProgress,
    OperationProgressMeasure,
    publish_operation_progress,
)
from pullbox.services.series_metadata_editor import read_editor_plan, run_series_editor_target
from pullbox.services.utility_operation_progress import build_utility_operation_update
from pullbox.utilities.base_executor import ExecutionMode, ItemResult, JobExecutor, ProcessedItem
from pullbox.utilities.import_guards import ensure_no_active_import_file_mutation
from pullbox.utilities.models import JobState, UtilityJob, UtilityJobItem


class FileMetadataExecutor(JobExecutor):
    execution_mode = ExecutionMode.ASYNC

    def validate_config(self, job_config: dict[str, Any]) -> list[str]:
        if "series_editor" in job_config:
            try:
                read_editor_plan(job_config)
            except (ValueError, TypeError):
                return ["Review the saved series metadata plan before retrying."]
            return []
        metadata_config = {
            key: value for key, value in job_config.items() if key != "recovery_pending_id"
        }
        if "recovery_pending_id" in job_config and (
            type(job_config["recovery_pending_id"]) is not int
            or job_config["recovery_pending_id"] <= 0
        ):
            return ["Choose a pending metadata recovery."]
        if (
            set(metadata_config)
            not in (
                {"issue_id", "review_key"},
                {"issue_id", "review_key", "choices"},
                {"issue_id", "review_key", "editor"},
            )
            or ("editor" in job_config and "recovery_pending_id" in job_config)
            or type(job_config.get("issue_id")) is not int
            or job_config["issue_id"] <= 0
            or not isinstance(job_config.get("review_key"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", job_config["review_key"])
        ):
            return ["Choose an issue and approve its current file metadata preview."]
        try:
            FileMetadataReview(choices=job_config.get("choices", {}))
            if (
                "editor" in job_config
                and not IssueMetadataEditorReview.model_validate(job_config["editor"]).edits
            ):
                return ["Select at least one issue field to update."]
        except ValidationError:
            return ["Review the metadata choices before writing."]
        return []

    async def build_job_context(self, session: Any, job_config: dict[str, Any]) -> dict[str, Any]:
        errors = self.validate_config(job_config)
        if errors:
            raise ValueError(errors[0])
        if "series_editor" in job_config:
            return {"factory": async_sessionmaker(session.bind, expire_on_commit=False)}
        await ensure_no_active_import_file_mutation(session)
        return {
            "factory": async_sessionmaker(session.bind, expire_on_commit=False),
            "limit": await get_archive_size_limit_bytes(session),
        }

    async def generate_items(
        self, job_config: dict[str, Any], job_context: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if "series_editor" in job_config:
            plan = read_editor_plan(job_config)
            return [
                {
                    "operation": "series_sidecar",
                    "target_index": index,
                    "file_path": str(target.location.directory),
                }
                for index, target in enumerate(plan.targets)
                if target.read.action in {"create", "update"}
            ] or [{"operation": "series_metadata_saved"}]
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
        if "series_editor" in job_config:
            plan = read_editor_plan(job_config)
            if item_data["operation"] == "series_metadata_saved":
                return ProcessedItem(
                    item_data["id"], ItemResult.COMPLETED, after_state={"library_saved": True}
                )
            try:
                async with factory() as session:
                    item = await session.get(UtilityJobItem, item_data["id"])
                    job = await session.get(UtilityJob, item.job_id) if item else None
                    if job is None or job.state != JobState.RUNNING:
                        raise JobCancelledError(
                            "Sidecar writing stopped; library metadata is saved."
                        )
                    series_outcome = await run_series_editor_target(
                        session, plan, item_data["target_index"]
                    )
                return ProcessedItem(
                    item_data["id"],
                    ItemResult.COMPLETED
                    if series_outcome["outcome"] == "saved"
                    else ItemResult.FAILED,
                    after_state={"library_saved": True, **series_outcome},
                    error_message=str(series_outcome["reason"])
                    if series_outcome["outcome"] == "failed"
                    else None,
                )
            except (JobCancelledError, asyncio.CancelledError):
                return ProcessedItem(item_data["id"], ItemResult.CANCELLED)
            except (ValueError, OSError) as exc:
                return ProcessedItem(
                    item_data["id"],
                    ItemResult.FAILED,
                    after_state={"library_saved": True, "outcome": "failed"},
                    error_message="Library saved; sidecar is unavailable. Preview again."
                    if isinstance(exc, OSError)
                    else str(exc),
                )
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
            from pullbox.services.direct_metadata_recovery import (
                DirectMetadataRecoveryError,
                run_direct_metadata_recovery,
            )

            recovery_id = job_config.get("recovery_pending_id")
            writer = (
                run_direct_metadata_recovery if recovery_id is not None else write_file_metadata
            )
            if "editor" in job_config:
                outcome = await write_file_metadata(
                    factory,
                    job_config["issue_id"],
                    job_config["review_key"],
                    UUID(item_data["id"]),
                    limit=context["limit"],
                    check_control=check_control,
                    progress=progress,
                    job_id=job_id,
                    editor=IssueMetadataEditorReview.model_validate(job_config["editor"]),
                )
            else:
                outcome = await writer(
                    factory,
                    recovery_id if recovery_id is not None else job_config["issue_id"],
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
                item_data["id"],
                ItemResult.FAILED,
                error_message=str(exc)
                if isinstance(exc, DirectMetadataRecoveryError)
                else file_metadata_error(exc),
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
