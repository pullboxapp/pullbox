"""Remove only explicitly reviewed arc placements and library comics."""

from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from pullbox.core.exceptions import ValidationError
from pullbox.services.story_arc_cleanup import finish_reviewed_cleanup, remove_reviewed_target
from pullbox.services.story_arc_member_cleanup import recover_member_item, run_member_item
from pullbox.services.story_arc_placement_integration import StoryArcPlacementIntegrationError
from pullbox.services.story_arc_service import StoryArcServiceError
from pullbox.utilities.base_executor import (
    ExecutionMode,
    FinalizeResult,
    ItemResult,
    JobExecutor,
    JobRunSummary,
    ProcessedItem,
)
from pullbox.utilities.models import JobState, UtilityJob


class StoryArcCleanupExecutor(JobExecutor):
    execution_mode = ExecutionMode.ASYNC

    async def build_job_context(self, session: Any, job_config: dict[str, Any]) -> dict[str, Any]:
        return {"factory": async_sessionmaker(session.bind, expire_on_commit=False)}

    async def generate_items(
        self, job_config: dict[str, Any], job_context: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return [
            {
                "operation": "finish_member_cleanup"
                if job_config.get("receipt_only")
                else "remove_member_file"
                if "original" in t
                else "remove_arc_placement",
                "file_path": t["file_path"],
                "index": i,
            }
            for i, t in enumerate(job_config["targets"])
        ]

    def process_item(
        self,
        item_data: dict[str, Any],
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> ProcessedItem:
        raise RuntimeError("Story Arc cleanup requires the async executor")

    async def process_item_async(
        self,
        item_data: dict[str, Any],
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> ProcessedItem:
        assert job_context is not None
        item_id = item_data["id"]
        async with job_context["factory"]() as db:
            try:
                index = item_data["index"]
                target = job_config["targets"][index]
                if job_config.get("receipt_only"):
                    result = await recover_member_item(db, job_config, item_id)
                elif "original" in target:
                    result = await run_member_item(db, job_config, index, item_id)
                else:
                    result = await remove_reviewed_target(db, job_config, target, item_id)
                return ProcessedItem(item_id, ItemResult.COMPLETED, after_state=result)
            except (
                StoryArcPlacementIntegrationError,
                StoryArcServiceError,
                ValidationError,
                OSError,
            ) as exc:
                return ProcessedItem(item_id, ItemResult.FAILED, error_message=str(exc))

    async def finalize_job(
        self,
        session: Any,
        job: UtilityJob,
        summary: JobRunSummary,
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None,
    ) -> FinalizeResult:
        if job.state == JobState.CANCELLED:
            return FinalizeResult()
        if summary.failed or summary.skipped or summary.completed != len(job_config["targets"]):
            raise ValueError(
                "Not all selected files were removed. The arc was kept; review its remaining files."
            )
        if job_config.get("receipt_only"):
            return FinalizeResult(final_parts=["file cleanup journal resolved; arc kept"])
        await finish_reviewed_cleanup(session, job, job_config)
        removed = sum("original" in t for t in job_config["targets"])
        return FinalizeResult(
            final_parts=[
                f"arc record deleted; {removed} selected library files removed"
                if removed
                else "arc record deleted; library comics preserved"
            ]
        )

    def rollback_item(
        self,
        item_data: dict[str, Any],
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> ProcessedItem:
        return ProcessedItem(
            item_data["id"], ItemResult.FAILED, error_message="Arc deletion cannot be rolled back."
        )
