"""Settle only journaled existing-file metadata writes before utility recovery."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from sqlalchemy import JSON, Text, case, cast, func, select, type_coerce
from sqlalchemy.exc import DBAPIError

from pullbox.models.archive_metadata_publication import ArchiveMetadataPublication, PublicationState
from pullbox.services.archive_metadata_publication import load_archive_publication
from pullbox.services.issue_file_metadata import file_metadata_error, recover_file_metadata
from pullbox.services.native_file_metadata import recover_native_file_metadata
from pullbox.services.utility_operation_progress import project_utility_operation_progress
from pullbox.utilities.models import ItemState, JobState, JobType, UtilityJob, UtilityJobItem

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)
_INLINE_JOB_CONFIG_LIMIT = 65536


async def read_metadata_job_binding(
    session: AsyncSession, job_id: str, small_config: str | None
) -> tuple[object, object, object]:
    if small_config is not None:
        payload = json.loads(small_config)
        if not isinstance(payload, dict):
            raise ValueError("Invalid metadata job configuration")
        return (
            payload.get("issue_id"),
            payload.get("recovery_pending_id"),
            payload.get("review_key"),
        )
    # Large editor drafts are not restart evidence. Project just the binding for this one job.
    document = type_coerce(cast(UtilityJob.config, JSON().with_variant(Text(), "sqlite")), JSON)
    try:
        row = (
            await session.execute(
                select(
                    document["issue_id"], document["recovery_pending_id"], document["review_key"]
                ).where(UtilityJob.id == job_id)
            )
        ).one_or_none()
    except DBAPIError as exc:
        raise ValueError("Cannot read the saved metadata job binding") from exc
    if row is None or (
        row[1] is not None
        and (
            type(row[0]) is not int
            or type(row[1]) is not int
            or not isinstance(row[2], str)
            or len(row[2]) != 64
        )
    ):
        raise ValueError("Invalid metadata recovery job binding")
    return row[0], row[1], row[2]


async def _complete_metadata_item(
    factory: async_sessionmaker[AsyncSession], operation: UUID, job_id: str
) -> None:
    async with factory.begin() as session:
        item = await session.get(UtilityJobItem, operation.hex)
        job = await session.get(UtilityJob, job_id)
        if (
            item is None
            or job is None
            or item.job_id != job.id
            or job.job_type != JobType.FILE_METADATA
        ):
            return
        item.state = ItemState.COMPLETED
        item.error_message = None
        item.completed_at = datetime.now(UTC).isoformat()
        item.after_state = '{"outcome":"recovered"}'
        job.completed_items = await session.scalar(
            select(func.count())
            .select_from(UtilityJobItem)
            .where(UtilityJobItem.job_id == job.id, UtilityJobItem.state == ItemState.COMPLETED)
        )
        job.failed_items = await session.scalar(
            select(func.count())
            .select_from(UtilityJobItem)
            .where(UtilityJobItem.job_id == job.id, UtilityJobItem.state == ItemState.FAILED)
        )
        if job.completed_items == job.total_items and job.state not in {
            JobState.CANCELLING,
            JobState.CANCELLED,
        }:
            job.state = JobState.COMPLETED
            job.completed_at = item.completed_at
            job.error_message = None
        await project_utility_operation_progress(session, job)


async def _recover_native_metadata_jobs(factory: async_sessionmaker[AsyncSession]) -> None:
    cursor = ""
    while True:
        async with factory() as session:
            rows = (
                await session.execute(
                    select(
                        UtilityJobItem.id,
                        UtilityJob.id,
                        case(
                            (
                                func.length(UtilityJob.config) <= _INLINE_JOB_CONFIG_LIMIT,
                                UtilityJob.config,
                            )
                        ),
                    )
                    .join(UtilityJob, UtilityJob.id == UtilityJobItem.job_id)
                    .where(
                        UtilityJobItem.id > cursor,
                        UtilityJobItem.state == ItemState.IN_PROGRESS,
                        UtilityJob.job_type == JobType.FILE_METADATA,
                        UtilityJob.state.in_(
                            (JobState.RUNNING, JobState.PAUSING, JobState.CANCELLING)
                        ),
                    )
                    .order_by(UtilityJobItem.id)
                    .limit(100)
                )
            ).all()
        if not rows:
            return
        for item_id, job_id, config in rows:
            cursor = item_id
            try:
                async with factory() as reader:
                    issue_id, pending_id, _ = await read_metadata_job_binding(
                        reader, job_id, config
                    )
                if type(issue_id) is not int or issue_id <= 0:
                    continue
                operation = UUID(item_id)
                if pending_id is not None:
                    from pullbox.services.direct_metadata_recovery import (
                        recover_direct_metadata_operation,
                    )

                    if await recover_direct_metadata_operation(factory, operation, job_id):
                        await _complete_metadata_item(factory, operation, job_id)
                    continue
                if await recover_native_file_metadata(factory, issue_id, operation) is None:
                    continue
                await _complete_metadata_item(factory, operation, job_id)
            except (ValueError, OSError) as exc:
                logger.warning(
                    "native_metadata_recovery_needs_review",
                    job_id=job_id,
                    reason=getattr(exc, "code", "unavailable"),
                )
                async with factory.begin() as session:
                    job = await session.get(UtilityJob, job_id)
                    if job is not None and job.job_type == JobType.FILE_METADATA:
                        job.state = JobState.FAILED
                        job.error_message = file_metadata_error(exc)
                        await project_utility_operation_progress(session, job)


async def recover_file_metadata_jobs(factory: async_sessionmaker[AsyncSession]) -> None:
    await _recover_native_metadata_jobs(factory)
    cursor = 0
    while True:
        async with factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(
                            ArchiveMetadataPublication.id,
                            ArchiveMetadataPublication.operation_id,
                        )
                        .where(
                            ArchiveMetadataPublication.id > cursor,
                            ArchiveMetadataPublication.active_path_key.is_not(None),
                        )
                        .order_by(ArchiveMetadataPublication.id)
                        .limit(100)
                    )
                ).all()
            )
        if not rows:
            return
        for row_id, operation_id in rows:
            cursor = row_id
            async with factory() as session:
                receipt = await load_archive_publication(session, UUID(operation_id))
            if receipt is None or receipt.plan.metadata_job_id is None:
                continue
            try:
                state = await recover_file_metadata(factory, receipt.operation_id)
                if state is not PublicationState.FINALIZED:
                    continue
                async with factory() as session:
                    _, pending_id, _ = await read_metadata_job_binding(
                        session, receipt.plan.metadata_job_id, None
                    )
                    is_recovery = pending_id is not None
                if is_recovery:
                    # A settled archive alone does not resolve its download recovery entry.
                    from pullbox.services.direct_metadata_recovery import (
                        recover_direct_metadata_operation,
                    )

                    if not await recover_direct_metadata_operation(
                        factory, receipt.operation_id, receipt.plan.metadata_job_id
                    ):
                        continue
            except (ValueError, OSError) as exc:
                logger.warning(
                    "file_metadata_recovery_needs_review",
                    operation_id=operation_id,
                    reason=getattr(exc, "code", "unavailable"),
                )
                async with factory.begin() as session:
                    job = await session.get(UtilityJob, receipt.plan.metadata_job_id)
                    if job is not None and job.job_type == JobType.FILE_METADATA:
                        job.state = JobState.FAILED
                        job.error_message = file_metadata_error(exc)
                        await project_utility_operation_progress(session, job)
                continue
            await _complete_metadata_item(
                factory, receipt.operation_id, receipt.plan.metadata_job_id
            )
