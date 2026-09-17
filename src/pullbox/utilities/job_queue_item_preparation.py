"""Prepare utility job items for dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from pullbox.services.utility_operation_progress import project_utility_operation_progress
from pullbox.utilities.job_queue_items import build_generated_job_items
from pullbox.utilities.job_queue_state import transition_job_state
from pullbox.utilities.models import ItemState, JobState, UtilityJob, UtilityJobItem

if TYPE_CHECKING:
    from pullbox.utilities.base_executor import JobExecutor


@dataclass(frozen=True, slots=True)
class PreparedDispatchItems:
    """Job context and pending DB items ready for worker dispatch."""

    job_context: dict[str, Any] | None
    pending_items: list[UtilityJobItem]


async def mark_item_generation_failed(
    session: Any,
    *,
    job_id: str,
    exc: BaseException,
) -> bool:
    """Mark a dispatch job failed because item generation could not complete."""
    job = await session.get(UtilityJob, job_id)
    if job is None:
        return False

    transition_job_state(job, JobState.FAILED)
    job.error_message = f"Item generation failed: {exc}"
    await project_utility_operation_progress(session, job)
    await session.commit()
    return True


async def prepare_dispatch_items(
    session_factory: Any,
    *,
    job_id: str,
    executor: JobExecutor,
    config: dict[str, Any],
) -> PreparedDispatchItems | None:
    """Build context, create generated items once, then load pending items."""
    async with session_factory() as session:
        job_context = await executor.build_job_context(session, config)
        existing_result = await session.execute(
            select(UtilityJobItem)
            .where(UtilityJobItem.job_id == job_id)
            .order_by(UtilityJobItem.item_index)
        )
        existing_items = list(existing_result.scalars().all())

        if not existing_items:
            items_data = await executor.run_generate_items(config, job_context)
            current_job = await session.get(UtilityJob, job_id)
            if current_job is None:
                return None
            for item in build_generated_job_items(
                job_id=current_job.id,
                items_data=items_data,
            ):
                session.add(item)
            current_job.total_items = len(items_data)
            await session.commit()

    async with session_factory() as session:
        result = await session.execute(
            select(UtilityJobItem)
            .where(
                UtilityJobItem.job_id == job_id,
                UtilityJobItem.state == ItemState.PENDING,
            )
            .order_by(UtilityJobItem.item_index)
        )
        pending_items = list(result.scalars().all())
    return PreparedDispatchItems(
        job_context=job_context,
        pending_items=pending_items,
    )
