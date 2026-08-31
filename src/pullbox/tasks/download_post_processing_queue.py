"""Completed-download post-processing queue drain."""

from __future__ import annotations

import asyncio
import secrets
import time as _time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.database import get_session_factory
from pullbox.models.download import DownloadHistory, DownloadState
from pullbox.models.issue import Issue, IssueStatus
from pullbox.services.post_processing_operation_progress import (
    project_post_processing_operation_progress,
)
from pullbox.services.story_arc_sync_queue import request_story_arc_sync_now
from pullbox.tasks.post_processing_progress import (
    PostProcessingPhase,
    _clear_post_processing,
    _mark_post_processing_complete,
    _set_post_processing_phase,
    get_all_post_processing_progress,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult

logger = structlog.get_logger(__name__)

RunPostProcessing = Callable[[AsyncSession, DownloadHistory], Awaitable[None]]
_process_completed_lock = asyncio.Lock()
_POST_PROCESSING_CLAIM_LEASE = timedelta(minutes=15)


async def _claim_completed_download(
    session: AsyncSession,
    download_id: int,
    *,
    now: datetime,
) -> str | None:
    """Atomically lease one completed row before any post-processing I/O."""
    token = secrets.token_urlsafe(24)
    stale_before = now - _POST_PROCESSING_CLAIM_LEASE
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(DownloadHistory)
            .where(
                DownloadHistory.id == download_id,
                DownloadHistory.state == DownloadState.COMPLETED,
                DownloadHistory.imported_at.is_(None),
                or_(
                    DownloadHistory.post_processing_claim_token.is_(None),
                    DownloadHistory.post_processing_claimed_at.is_(None),
                    DownloadHistory.post_processing_claimed_at <= stale_before,
                ),
            )
            .values(
                post_processing_claim_token=token,
                post_processing_claimed_at=now,
            )
        ),
    )
    await session.commit()
    return token if result.rowcount == 1 else None


async def process_completed(
    run_post_processing: RunPostProcessing,
    *,
    session_factory: Any | None = None,
    event_logger: Any | None = None,
) -> None:
    """Post-process downloads that completed but have not yet been imported."""
    log = event_logger or logger
    if _process_completed_lock.locked():
        log.debug("process_completed_skipped_locked")
        return

    await _process_completed_lock.acquire()
    try:
        factory = session_factory or get_session_factory()

        start: float | None = None
        queued_total = 0
        processed = 0
        failed = 0

        while True:
            # Drain the queue until no completed, unimported items remain.
            download_ids: list[int] = []
            async with factory() as session:
                try:
                    result = await session.execute(
                        select(DownloadHistory.id).where(
                            DownloadHistory.state == DownloadState.COMPLETED,
                            DownloadHistory.imported_at.is_(None),
                            or_(
                                DownloadHistory.post_processing_claim_token.is_(None),
                                DownloadHistory.post_processing_claimed_at.is_(None),
                                DownloadHistory.post_processing_claimed_at
                                <= datetime.now(UTC) - _POST_PROCESSING_CLAIM_LEASE,
                            ),
                        )
                    )
                    download_ids = [row[0] for row in result.all()]
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise

            if not download_ids:
                if start is None:
                    return
                break

            if start is None:
                start = _time.monotonic()
                log.info("process_completed_start", count=len(download_ids))
            else:
                log.debug("process_completed_drain_batch", count=len(download_ids))

            queued_total += len(download_ids)

            # Process each download in its own session so file I/O does not
            # hold long SQLite write locks.
            for dl_id in download_ids:
                completed_age_ms: float | None = None
                handoff_start: float | None = None
                async with factory(autoflush=False) as session:
                    try:
                        claim_token = await _claim_completed_download(
                            session,
                            dl_id,
                            now=datetime.now(UTC),
                        )
                        if claim_token is None:
                            continue
                        download = await session.get(DownloadHistory, dl_id)
                        if (
                            not download
                            or download.state != DownloadState.COMPLETED
                            or download.imported_at is not None
                        ):
                            continue

                        completed_age_ms = (
                            max(
                                0.0,
                                (datetime.now(UTC) - download.completed_at).total_seconds() * 1000,
                            )
                            if download.completed_at is not None
                            else None
                        )
                        handoff_start = _time.monotonic()
                        log.info(
                            "post_processing_handoff_started",
                            download_id=dl_id,
                            completed_age_ms=round(completed_age_ms, 1)
                            if completed_age_ms is not None
                            else None,
                        )
                        _set_post_processing_phase(dl_id, PostProcessingPhase.RESOLVING_SOURCE)
                        await run_post_processing(session, download)

                        # State stays at COMPLETED; imported_at marks success.
                        download.imported_at = datetime.now(UTC)
                        download.error_message = None
                        download.post_processing_claim_token = None
                        download.post_processing_claimed_at = None
                        _mark_post_processing_complete(dl_id)
                        await project_post_processing_operation_progress(
                            session,
                            download,
                            get_all_post_processing_progress().get(dl_id),
                        )
                        processed += 1
                        log.info(
                            "post_processing_handoff_complete",
                            download_id=dl_id,
                            completed_age_ms=round(completed_age_ms, 1)
                            if completed_age_ms is not None
                            else None,
                            post_processing_duration_ms=round(
                                (_time.monotonic() - handoff_start) * 1000,
                                1,
                            ),
                        )

                        await session.commit()
                        try:
                            request_story_arc_sync_now()
                        except Exception:
                            # Canonical completion is already durable. This
                            # latency-only nudge must never enter failure repair.
                            log.warning(
                                "story_arc_sync_trigger_failed_after_completion",
                                download_id=dl_id,
                                exc_info=True,
                            )
                    except Exception as exc:
                        failed_duration_ms = (
                            round((_time.monotonic() - handoff_start) * 1000, 1)
                            if handoff_start is not None
                            else None
                        )
                        _clear_post_processing(dl_id)
                        await session.rollback()
                        failed += 1
                        # Mark failed in a fresh mini-session so the error is persisted.
                        async with factory() as err_session:
                            try:
                                dl = await err_session.get(DownloadHistory, dl_id)
                                if dl:
                                    dl.state = DownloadState.FAILED
                                    dl.error_message = str(exc) or "Post-processing failed"
                                    dl.post_processing_claim_token = None
                                    dl.post_processing_claimed_at = None

                                    issue = await err_session.get(Issue, dl.issue_id)
                                    if issue and issue.status == IssueStatus.DOWNLOADING:
                                        issue.status = IssueStatus.WANTED

                                    await project_post_processing_operation_progress(
                                        err_session,
                                        dl,
                                    )

                                await err_session.commit()
                            except Exception:
                                await err_session.rollback()

                        log.info(
                            "post_processing_handoff_failed",
                            download_id=dl_id,
                            completed_age_ms=round(completed_age_ms, 1)
                            if completed_age_ms is not None
                            else None,
                            post_processing_duration_ms=failed_duration_ms,
                        )
                        log.exception(
                            "process_completed_failed",
                            download_id=dl_id,
                        )

        duration_ms = (_time.monotonic() - start) * 1000
        log.info(
            "process_completed_done",
            processed=processed,
            failed=failed,
            total=queued_total,
            duration_ms=round(duration_ms, 1),
        )
    finally:
        _process_completed_lock.release()
