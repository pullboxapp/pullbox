"""Reconcile download activity with its durable lifecycle owner."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from sqlalchemy import String, and_, cast, or_, select

from pullbox.models.download import DownloadHistory, DownloadState
from pullbox.models.operation_progress import (
    OperationProgress,
    OperationProgressTone,
)
from pullbox.models.operation_progress import (
    OperationProgressState as State,
)
from pullbox.models.operation_progress import (
    OperationProgressType as Kind,
)
from pullbox.services.download_operation_progress import (
    CANCELLED_DOWNLOAD_MESSAGES,
    build_download_operation_update,
    download_was_cancelled,
)
from pullbox.services.operation_progress import OperationProgressUpdate, publish_operation_progress

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_KINDS = (Kind.DOWNLOAD, Kind.POST_PROCESSING)
_ACTIVE = (State.QUEUED, State.RUNNING, State.PAUSED, State.RETRYING)


def _terminal_update(download: DownloadHistory, kind: Kind) -> OperationProgressUpdate | None:
    if download.state == DownloadState.FAILED:
        return replace(build_download_operation_update(download), operation_type=kind)
    if (
        download.imported_at is not None
        or download.state == DownloadState.IMPORTED
        or (kind is Kind.DOWNLOAD and download.state == DownloadState.COMPLETED)
    ):
        return replace(
            build_download_operation_update(download),
            operation_type=kind,
            state=State.COMPLETED,
            phase="completed",
            attention_required=False,
            tone=OperationProgressTone.SUCCESS,
        )
    return None


async def _retire(session: AsyncSession, operation: OperationProgress) -> None:
    await publish_operation_progress(
        session,
        OperationProgressUpdate(
            operation_type=operation.operation_type,
            operation_key=operation.operation_key,
            revision=None,
            state=State.CANCELLED,
            phase="cancelled",
            title=operation.title,
            message="Download history or series removed.",
            group_key=operation.group_key,
            source_label=operation.source_label,
            detail_snapshot=operation.detail_snapshot,
        ),
    )


async def retire_download_operations(session: AsyncSession, download_id: int) -> None:
    """Retire projections before their download owner is removed."""
    operations = await session.scalars(
        select(OperationProgress).where(
            OperationProgress.operation_type.in_(_KINDS),
            OperationProgress.operation_key == str(download_id),
            or_(
                OperationProgress.state.in_(_ACTIVE), OperationProgress.attention_required.is_(True)
            ),
        )
    )
    for operation in operations:
        await _retire(session, operation)


async def reconcile_download_operations(session: AsyncSession, *, limit: int = 100) -> int:
    """Repair a bounded batch of stale projections without modifying downloads."""
    if limit <= 0:
        return 0
    candidates = await session.scalars(
        select(OperationProgress)
        .outerjoin(
            DownloadHistory,
            cast(DownloadHistory.id, String) == OperationProgress.operation_key,
        )
        .where(
            OperationProgress.operation_type.in_(_KINDS),
            or_(
                and_(
                    OperationProgress.state == State.FAILED,
                    DownloadHistory.state == DownloadState.FAILED,
                    DownloadHistory.error_message.in_(CANCELLED_DOWNLOAD_MESSAGES),
                ),
                and_(
                    DownloadHistory.id.is_(None),
                    or_(
                        OperationProgress.state.in_(_ACTIVE),
                        OperationProgress.attention_required.is_(True),
                    ),
                ),
                and_(
                    OperationProgress.state.in_(_ACTIVE),
                    or_(
                        DownloadHistory.state.in_((DownloadState.FAILED, DownloadState.IMPORTED)),
                        DownloadHistory.imported_at.is_not(None),
                        and_(
                            OperationProgress.operation_type == Kind.DOWNLOAD,
                            DownloadHistory.state == DownloadState.COMPLETED,
                        ),
                    ),
                ),
            ),
        )
        .order_by(OperationProgress.id)
        .limit(min(limit, 100))
    )
    repaired = 0
    for operation in candidates:
        # Lock/reload the owner before publishing: a legitimate retry may have
        # started since the candidate query. Never expire work based on age.
        download = await session.scalar(
            select(DownloadHistory)
            .where(
                cast(DownloadHistory.id, String) == operation.operation_key,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        await session.refresh(operation)
        if operation.state not in _ACTIVE and not (
            (download is None and operation.attention_required)
            or (
                download is not None
                and operation.state == State.FAILED
                and download_was_cancelled(download)
            )
        ):
            continue
        if download is None:
            await _retire(session, operation)
        else:
            update = _terminal_update(download, operation.operation_type)
            if update is None:
                continue
            await publish_operation_progress(session, update)
        repaired += 1
    return repaired


async def guard_download_progress(
    session: AsyncSession, update: OperationProgressUpdate
) -> OperationProgressUpdate | None:
    """Revalidate detached download events before they are published."""
    if update.operation_type not in _KINDS:
        return update
    download = await session.scalar(
        select(DownloadHistory)
        .where(
            cast(DownloadHistory.id, String) == update.operation_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if download is None:
        return None
    terminal = _terminal_update(download, update.operation_type)
    return terminal or update
