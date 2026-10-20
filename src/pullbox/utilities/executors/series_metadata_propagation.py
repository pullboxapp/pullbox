"""Selected-field series jobs share the guarded issue writer, not another file lifecycle."""

from typing import Any
from uuid import UUID

from pullbox.services.archive_metadata_rendering import ArchiveMetadataRenderError
from pullbox.services.issue_file_metadata import prepare_file_metadata, write_file_metadata
from pullbox.services.series_metadata_propagation import read_plan, series_key
from pullbox.utilities.base_executor import ItemResult, ProcessedItem


async def process_projection(
    item_data: dict[str, Any],
    config: dict[str, Any],
    context: dict[str, Any],
    check_control: Any,
    progress: Any,
    job_id: str,
) -> ProcessedItem:
    plan = read_plan(config)
    target = plan.targets[item_data["target_index"]]
    await check_control()
    if plan.mode == "preview":
        async with context["factory"]() as session:
            if await series_key(session, plan.series_id) != plan.series_key:
                raise ArchiveMetadataRenderError("approval_changed")
            prepared = await prepare_file_metadata(
                session, target.issue_id, propagation_fields=frozenset(plan.fields)
            )
        if prepared.target.binding.metadata.series.local_id != plan.series_id:
            raise ArchiveMetadataRenderError("approval_changed")
        data = prepared.preview.model_dump(mode="json")
        data["outcome"] = "current" if prepared.preview.unchanged else "ready"
        return ProcessedItem(item_data["id"], ItemResult.COMPLETED, after_state=data)
    assert target.review_key is not None
    outcome = await write_file_metadata(
        context["factory"],
        target.issue_id,
        target.review_key,
        UUID(target.operation_id),
        limit=context["limit"],
        check_control=check_control,
        progress=progress,
        job_id=job_id,
        propagation_fields=frozenset(plan.fields),
    )
    return ProcessedItem(
        item_data["id"],
        ItemResult.COMPLETED,
        after_state={"outcome": "current" if outcome == "unchanged" else "written"},
    )
