"""Read-phase helpers for the download monitor task."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState

ACTIVE_DOWNLOAD_STATES = (
    DownloadState.SENT,
    DownloadState.DOWNLOADING,
    DownloadState.FINALIZING,
    DownloadState.PAUSED,
)


@dataclass(frozen=True)
class MonitorReadResult:
    """Registry and detached poll items loaded during the monitor read phase."""

    registry: Any
    poll_items: list[dict[str, object]]


BuildDownloadRegistry = Callable[[AsyncSession], Awaitable[Any | None]]


def build_poll_item(download: Any) -> dict[str, object]:
    """Snapshot fields required after the DB session is closed."""
    return {
        "id": download.id,
        "external_id": download.external_id,
        "title": download.title,
        "download_client": download.download_client,
        "download_client_config_id": download.download_client_config_id,
        "downloaded_path": download.downloaded_path,
        "issue_id": download.issue_id,
        "retry_count": download.retry_count,
        "max_retries": download.max_retries,
    }


async def load_monitor_poll_items(
    session: AsyncSession,
    *,
    build_download_registry: BuildDownloadRegistry,
) -> MonitorReadResult | None:
    """Load the download registry and detached poll items for active downloads."""
    registry = await build_download_registry(session)
    if registry is None:
        return None

    result = await session.execute(
        select(DownloadHistory).where(
            DownloadHistory.state.in_(ACTIVE_DOWNLOAD_STATES),
            DownloadHistory.download_client != DownloadClientType.DIRECT,
        )
    )
    active: Sequence[DownloadHistory] = list(result.scalars().all())
    return MonitorReadResult(
        registry=registry,
        poll_items=[build_poll_item(download) for download in active],
    )
