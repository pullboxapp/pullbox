"""Expose direct acquisitions through the established download-history contract."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import or_, select, update

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.models.blocklist import BlocklistEntry
from pullbox.models.direct_acquisition import (
    DirectAcquisitionAttempt,
    DirectAcquisitionState,
    DirectArtifactAttempt,
)
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import Issue, IssueStatus

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession


_ACTIVE_ISSUE_STATES = frozenset(
    {
        DirectAcquisitionState.RESOLVING,
        DirectAcquisitionState.PLANNED,
        DirectAcquisitionState.QUEUED,
        DirectAcquisitionState.DOWNLOADING,
        DirectAcquisitionState.VALIDATING,
        DirectAcquisitionState.POST_PROCESSING,
        DirectAcquisitionState.RETRY_PENDING,
        DirectAcquisitionState.PAUSED,
    }
)
_FAILED_ISSUE_STATES = frozenset(
    {
        DirectAcquisitionState.CANCELLED,
        DirectAcquisitionState.FAILED,
    }
)


async def ensure_direct_download_history(
    session: AsyncSession,
    attempt: DirectAcquisitionAttempt,
    artifact: DirectArtifactAttempt,
    *,
    at: datetime,
) -> DownloadHistory:
    """Create or return the URL-safe download row for a direct attempt."""
    external_id = f"direct:{attempt.id}"
    download_url = f"pullbox-direct://attempt/{attempt.id}"
    result = await session.execute(
        select(DownloadHistory)
        .where(
            DownloadHistory.download_client == DownloadClientType.DIRECT,
            or_(
                DownloadHistory.external_id == external_id,
                DownloadHistory.download_url == download_url,
            ),
        )
        .order_by(
            (DownloadHistory.external_id == external_id).desc(),
            DownloadHistory.id.desc(),
        )
    )
    histories = list(result.scalars())
    if histories:
        history = histories[0]
        history.external_id = external_id
        history.download_url = download_url
        duplicate_ids = [duplicate.id for duplicate in histories[1:]]
        if duplicate_ids:
            await session.execute(
                update(BlocklistEntry)
                .where(BlocklistEntry.download_history_id.in_(duplicate_ids))
                .values(download_history_id=history.id)
            )
            for duplicate in histories[1:]:
                await session.delete(duplicate)
            await session.flush()
        return history

    display_title = str(
        attempt.candidate_snapshot.get("display_title") or attempt.provider_candidate_id
    )
    history = DownloadHistory(
        issue_id=attempt.issue_id,
        title=display_title,
        download_url=download_url,
        download_client=DownloadClientType.DIRECT,
        protocol=AcquisitionProtocol.DIRECT,
        external_id=external_id,
        state=DownloadState.QUEUED,
        file_size=artifact.expected_size,
        sent_at=at,
        replace_existing_file=attempt.replace_existing_file,
    )
    session.add(history)
    await session.flush()
    return history


async def sync_direct_download_history(
    session: AsyncSession,
    attempt: DirectAcquisitionAttempt,
    artifact: DirectArtifactAttempt,
    *,
    at: datetime,
    final_path: str | None = None,
) -> DownloadHistory:
    """Project durable direct state onto the existing download UI record."""
    history = await ensure_direct_download_history(session, attempt, artifact, at=at)
    history.state = _download_state(DirectAcquisitionState(attempt.state))
    history.file_size = artifact.expected_size
    history.retry_count = attempt.retry_count
    history.max_retries = attempt.max_retries
    history.next_retry_at = attempt.next_retry_at
    history.error_message = attempt.error_message

    if attempt.state is DirectAcquisitionState.COMPLETED:
        history.completed_at = at
        history.imported_at = at
        history.final_path = final_path
    elif attempt.state is DirectAcquisitionState.CANCELLED:
        history.completed_at = at
        history.error_message = "Cancelled by user"
    elif attempt.state is DirectAcquisitionState.FAILED:
        history.completed_at = at
    await _sync_issue_status(session, attempt)
    from pullbox.services.download_operation_progress import (
        project_download_operation_progress,
    )

    await project_download_operation_progress(
        session,
        history,
        _shared_progress_snapshot(attempt, artifact),
    )
    return history


def _shared_progress_snapshot(
    attempt: DirectAcquisitionAttempt,
    artifact: DirectArtifactAttempt,
) -> dict[str, object]:
    snapshot = dict(attempt.progress_snapshot or {})
    raw_percent = snapshot.get("percent")
    percent = (
        max(0.0, min(float(raw_percent) / 100, 1.0))
        if isinstance(raw_percent, int | float)
        else None
    )
    host_kind = str(snapshot.get("host_kind") or artifact.host_kind.value)
    return {
        "progress": percent,
        "speed_bytes": snapshot.get("bytes_per_second"),
        "eta_seconds": snapshot.get("eta_seconds"),
        "size_bytes": snapshot.get("total_bytes") or artifact.expected_size,
        "bytes_transferred": snapshot.get("bytes_transferred"),
        "client_state": _direct_progress_label(snapshot, host_kind),
        "source_label": _direct_source_label(attempt.provider_identity, host_kind),
        "is_indeterminate": percent is None,
        "source_slow": snapshot.get("source_slow") is True,
    }


def _direct_progress_label(snapshot: dict[str, object], host_kind: str) -> str:
    stage = str(snapshot.get("stage") or "direct")
    host_label = _direct_host_label(host_kind)
    if stage == "resolver":
        resolver_name = str(snapshot.get("resolver_name") or "browser resolver").strip()
        resolver_kind = snapshot.get("resolver_kind")
        resolver_scope = snapshot.get("resolver_scope")
        attempt = snapshot.get("resolver_attempt")
        total = snapshot.get("resolver_total")
        if resolver_kind == "trawl" and resolver_scope == "datanodes":
            return "Using TRAWL (required by DataNodes)"
        if isinstance(attempt, int) and isinstance(total, int) and total > 0:
            return f"Trying {resolver_name} (resolver {attempt} of {total})"
        return f"Trying {resolver_name}"
    prefixes = {
        "fallback_queued": "Trying",
        "resolving": "Resolving",
        "downloading": "Downloading from",
        "validating": "Validating",
    }
    prefix = prefixes.get(stage)
    if prefix and host_label:
        suffix = " download" if stage == "validating" else ""
        return f"{prefix} {host_label}{suffix}"
    return stage.replace("_", " ").capitalize()


def _direct_host_label(host_kind: str) -> str:
    labels = {
        "mega": "MEGA",
        "pixeldrain": "PixelDrain",
        "rootz": "Rootz",
        "mediafire": "MediaFire",
        "terabox": "TeraBox",
        "datanodes": "DataNodes",
        "generic_https": "HTTPS",
    }
    return labels.get(host_kind, host_kind.replace("_", " ").title())


def _direct_source_label(provider_identity: str, host_kind: str) -> str:
    provider_labels = {
        "pullbox.getcomics": "GetComics",
        "pullbox.annas_archive": "Anna's Archive",
        "pullbox.libgen": "Library Genesis",
    }
    provider = provider_labels.get(
        provider_identity,
        provider_identity.removeprefix("pullbox.").replace("_", " ").title(),
    )
    host = _direct_host_label(host_kind)
    return f"{provider} via {host}" if host else provider


async def _sync_issue_status(
    session: AsyncSession,
    attempt: DirectAcquisitionAttempt,
) -> None:
    """Keep direct acquisitions aligned with the shared issue-status contract."""
    issue = await session.get(Issue, attempt.issue_id)
    if issue is None:
        return

    state = DirectAcquisitionState(attempt.state)
    if state in _ACTIVE_ISSUE_STATES:
        issue.status = IssueStatus.DOWNLOADING
    elif state in _FAILED_ISSUE_STATES and issue.status is IssueStatus.DOWNLOADING:
        issue.status = IssueStatus.WANTED


def _download_state(state: DirectAcquisitionState) -> DownloadState:
    if state in {DirectAcquisitionState.PLANNED, DirectAcquisitionState.QUEUED}:
        return DownloadState.QUEUED
    if state is DirectAcquisitionState.RESOLVING:
        return DownloadState.SENT
    if state is DirectAcquisitionState.DOWNLOADING:
        return DownloadState.DOWNLOADING
    if state in {
        DirectAcquisitionState.VALIDATING,
        DirectAcquisitionState.POST_PROCESSING,
    }:
        return DownloadState.POST_PROCESSING
    if state is DirectAcquisitionState.COMPLETED:
        return DownloadState.IMPORTED
    if state is DirectAcquisitionState.RETRY_PENDING:
        return DownloadState.RETRY_PENDING
    if state is DirectAcquisitionState.PAUSED:
        return DownloadState.PAUSED
    return DownloadState.FAILED
