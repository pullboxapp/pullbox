"""Write-phase helpers for the download monitor task."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.models.download import DownloadHistory, DownloadState
from pullbox.models.issue import Issue, IssueStatus


@dataclass(frozen=True)
class MonitorApplyResult:
    """Counts produced by applying download monitor updates."""

    completed: int = 0
    failed: int = 0


HandleDownloadFailure = Callable[[AsyncSession, DownloadHistory, str], Awaitable[None]]
ClearProgress = Callable[[int], None]
EmitLifecycleSummary = Callable[..., None]
PublishProgress = Callable[[AsyncSession, DownloadHistory, object | None], Awaitable[None]]


async def apply_monitor_updates(
    session: AsyncSession,
    updates: Sequence[Mapping[str, object]],
    *,
    first_active_observed_at: Mapping[int, object],
    clear_progress: ClearProgress,
    handle_download_failure: HandleDownloadFailure | None,
    emit_download_lifecycle_summary: EmitLifecycleSummary,
    event_logger: Any,
    publish_progress: PublishProgress | None = None,
) -> MonitorApplyResult:
    """Apply collected monitor updates inside a short DB session."""
    completed = 0
    failed = 0

    for update in updates:
        dl = await session.get(DownloadHistory, update["id"])
        if not dl:
            continue

        if "external_id" in update and not dl.external_id:
            dl.external_id = str(update["external_id"])
            event_logger.debug(
                "download_matched_by_title",
                download_id=dl.id,
                external_id=update["external_id"],
            )

        if "removed_externally" in update:
            # Download deleted from client; skip retries and go straight to FAILED.
            failed += 1
            observed_at = first_active_observed_at.get(dl.id)
            dl.state = DownloadState.FAILED
            dl.error_message = str(update.get("error_message", ""))
            clear_progress(dl.id)
            issue = await session.get(Issue, update["issue_id"])
            if issue and issue.status == IssueStatus.DOWNLOADING:
                issue.status = IssueStatus.WANTED
            event_logger.info(
                "download_removed_externally_cleaned",
                download_id=dl.id,
                issue_id=update["issue_id"],
            )
            emit_download_lifecycle_summary(
                dl,
                outcome="removed_externally",
                client_state=None,
                downloaded_path=dl.downloaded_path,
                observed_at=observed_at,
            )
        elif "failed" in update:
            failed += 1
            observed_at = first_active_observed_at.get(dl.id)
            event_logger.info(
                "download_state_transition",
                download_id=dl.id,
                from_state=str(dl.state),
                to_state="FAILED",
                error=str(update.get("error_message", ""))[:200],
            )
            if handle_download_failure is not None:
                await handle_download_failure(session, dl, str(update.get("error_message", "")))
            clear_progress(dl.id)
            if dl.state == DownloadState.FAILED:
                issue = await session.get(Issue, dl.issue_id)
                if issue and issue.status == IssueStatus.DOWNLOADING:
                    issue.status = IssueStatus.WANTED
            outcome = "retry_scheduled" if dl.state == DownloadState.RETRY_PENDING else "failed"
            emit_download_lifecycle_summary(
                dl,
                outcome=outcome,
                client_state=(
                    str(update.get("client_state"))
                    if update.get("client_state") is not None
                    else None
                ),
                downloaded_path=dl.downloaded_path,
                observed_at=observed_at,
            )
        elif "state" in update:
            new_state = update["state"]
            if dl.state != new_state:
                event_logger.info(
                    "download_state_transition",
                    download_id=dl.id,
                    from_state=str(dl.state),
                    to_state=str(new_state),
                )
            dl.state = new_state  # type: ignore[assignment]
            if "completed_at" in update:
                dl.completed_at = update["completed_at"]  # type: ignore[assignment]
                completed += 1
            if "downloaded_path" in update:
                dl.downloaded_path = str(update["downloaded_path"])
            if new_state == DownloadState.COMPLETED:
                emit_download_lifecycle_summary(
                    dl,
                    outcome="completed",
                    client_state=(
                        str(update.get("client_state"))
                        if update.get("client_state") is not None
                        else None
                    ),
                    downloaded_path=dl.downloaded_path,
                    observed_at=first_active_observed_at.get(dl.id),
                )
                clear_progress(dl.id)
        elif "heartbeat" in update:
            # Successful poll but no state change; touch updated_at so time-based
            # stall detection does not fire on active downloads.
            dl.updated_at = datetime.now(UTC)

        if publish_progress is not None:
            await publish_progress(session, dl, update.get("progress_snapshot"))

    return MonitorApplyResult(completed=completed, failed=failed)
