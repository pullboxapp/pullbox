"""Download API routes — queue, history, cancel, retry, and blocklist actions."""

import structlog
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import joinedload, selectinload

from pullbox.api.deps import AuthenticatedUser, DbSession
from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.core.exceptions import NotFoundError
from pullbox.models.blocklist import BlocklistReason
from pullbox.models.direct_acquisition import DirectAcquisitionAttempt
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import Issue, IssueStatus
from pullbox.providers.base import DownloadClient, ProviderRegistry
from pullbox.providers.download.qbittorrent import QBittorrentError
from pullbox.providers.indexer.newznab import NewznabError
from pullbox.schemas.blocklist import BlocklistEntryResponse
from pullbox.schemas.download import (
    DirectSourceAlternative,
    DirectSourceCurrent,
    DirectSourceOptionsResponse,
    DirectSourceSwitchRequest,
    DirectSourceSwitchResponse,
    DownloadHistoryItem,
    DownloadQueueItem,
)
from pullbox.schemas.pagination import PaginatedResponse
from pullbox.services.blocklist_service import BlocklistService
from pullbox.services.direct_acquisition_switch import (
    DirectSourceSwitchError,
    direct_source_host_label,
    list_source_switch_options,
)
from pullbox.services.download_history_classification import (
    download_history_clause,
    post_processing_history_clause,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/downloads", tags=["downloads"])


# ── Helpers ───────────────────────────────────────────────────────────


def _enrich_download(download: DownloadHistory) -> dict[str, object]:
    """Add computed fields (series_title, issue_number) to a download."""
    return {
        "id": download.id,
        "issue_id": download.issue_id,
        "title": download.title,
        "state": download.state,
        "download_client": download.download_client,
        "protocol": download.protocol,
        "external_id": download.external_id,
        "file_size": download.file_size,
        "error_message": download.error_message,
        "sent_at": download.sent_at,
        "completed_at": download.completed_at,
        "imported_at": download.imported_at,
        "created_at": download.created_at,
        "series_title": (
            download.issue.series.title if download.issue and download.issue.series else None
        ),
        "issue_number": download.issue.issue_number if download.issue else None,
    }


def _blocklist_entry_to_response(entry: object) -> BlocklistEntryResponse:
    """Convert a blocklist ORM row to the shared response schema."""
    from pullbox.models.blocklist import BlocklistEntry

    assert isinstance(entry, BlocklistEntry)
    return BlocklistEntryResponse(
        id=entry.id,
        release_title=entry.release_title,
        release_title_normalized=entry.release_title_normalized,
        download_url=entry.download_url,
        series_id=entry.series_id,
        issue_id=entry.issue_id,
        indexer_id=entry.indexer_id,
        reason=entry.reason,
        error_message=entry.error_message,
        release_group=entry.release_group,
        download_history_id=entry.download_history_id,
        series_title=entry.series.title if entry.series else None,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


def _registered_client_for_download(
    registry: ProviderRegistry,
    download: DownloadHistory,
) -> DownloadClient | None:
    """Resolve exact client identity, with type fallback for historical rows."""
    if download.download_client_config_id is not None:
        return registry.get_download_client(download.download_client_config_id)
    return registry.get_client_for_type(str(download.download_client))


# ── Queue ────────────────────────────────────────────────────────────


_QUEUE_STATES = [
    DownloadState.QUEUED,
    DownloadState.SENT,
    DownloadState.DOWNLOADING,
    DownloadState.FINALIZING,
    DownloadState.PAUSED,
]


@router.get("/queue", response_model=list[DownloadQueueItem])
async def download_queue(
    _user: AuthenticatedUser,
    session: DbSession,
) -> list[DownloadQueueItem]:
    """Get all active downloads in the queue."""
    result = await session.execute(
        select(DownloadHistory)
        .options(joinedload(DownloadHistory.issue).joinedload(Issue.series))
        .where(DownloadHistory.state.in_(_QUEUE_STATES))
        .order_by(DownloadHistory.created_at.desc())
    )
    downloads = result.unique().scalars().all()
    return [DownloadQueueItem.model_validate(_enrich_download(d)) for d in downloads]


# ── History ──────────────────────────────────────────────────────────


_HISTORY_FILTER = download_history_clause()
_POST_PROCESSING_HISTORY_FILTER = post_processing_history_clause()


@router.get("/history", response_model=PaginatedResponse[DownloadHistoryItem])
async def download_history(
    _user: AuthenticatedUser,
    session: DbSession,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> PaginatedResponse[DownloadHistoryItem]:
    """Get download history with pagination.

    Shows download-client outcomes only:
    - completed downloads that have not yet been imported
    - failed/cancelled downloads that never became post-processing runs

    Imported records and failed post-processing rows belong on the
    post-processing history page instead.
    """
    total = (
        await session.execute(select(func.count(DownloadHistory.id)).where(_HISTORY_FILTER))
    ).scalar_one()

    result = await session.execute(
        select(DownloadHistory)
        .options(joinedload(DownloadHistory.issue).joinedload(Issue.series))
        .where(_HISTORY_FILTER)
        .order_by(DownloadHistory.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    downloads = result.unique().scalars().all()

    items = [DownloadHistoryItem.model_validate(_enrich_download(d)) for d in downloads]
    return PaginatedResponse[DownloadHistoryItem](
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + limit) < total,
    )


# ── Clear History ──────────────────────────────────────────────────


@router.delete("/history")
async def clear_download_history(
    _user: AuthenticatedUser,
    session: DbSession,
) -> dict[str, int]:
    """Delete download history entries visible on the downloads page.

    Removes only rows that belong to the downloads history page.
    Does NOT remove active downloads, imported records, or failed
    post-processing rows.
    """
    cursor_result = await session.execute(delete(DownloadHistory).where(_HISTORY_FILTER))
    count: int = cursor_result.rowcount  # type: ignore[attr-defined]
    logger.info("download_history_cleared", count=count)
    return {"deleted": count}


@router.delete("/history/post-processing")
async def clear_post_processing_history(
    _user: AuthenticatedUser,
    session: DbSession,
) -> dict[str, int]:
    """Delete imported and failed post-processing history entries."""
    cursor_result = await session.execute(
        delete(DownloadHistory).where(_POST_PROCESSING_HISTORY_FILTER)
    )
    count: int = cursor_result.rowcount  # type: ignore[attr-defined]
    logger.info("post_processing_history_cleared", count=count)
    return {"deleted": count}


# ── Cancel / Remove ─────────────────────────────────────────────────

_CANCELLABLE_STATES = frozenset(
    {
        DownloadState.QUEUED,
        DownloadState.SENT,
        DownloadState.DOWNLOADING,
        DownloadState.FINALIZING,
        DownloadState.PAUSED,
        DownloadState.RETRY_PENDING,
    }
)


async def _cancel_on_client(download: DownloadHistory, session: DbSession) -> None:
    """Cancel on the download client before changing local state.

    Builds a provider registry, locates the client for the download's type,
    and calls ``remove_download``. Legacy clients remain best effort, while
    AirDC++ cancellation must be confirmed because its exact remote bundle can
    otherwise continue downloading after Pullbox reports it as cancelled.
    """
    if download.download_client is DownloadClientType.DIRECT:
        attempt_id = _direct_attempt_id(download.external_id)
        if attempt_id is None:
            logger.warning(
                "cancel_direct_reference_invalid",
                download_id=download.id,
            )
            return
        from pullbox.tasks.direct_acquisition_task import get_direct_acquisition_runner

        try:
            runner = get_direct_acquisition_runner()
            cancelled = await runner.cancel(attempt_id)
        except RuntimeError:
            cancelled = False
        logger.info(
            "direct_download_cancel_requested",
            download_id=download.id,
            acquisition_id=attempt_id,
            active=cancelled,
        )
        return

    if download.download_client is DownloadClientType.AIRDCPP:
        from pullbox.composition.airdcpp import get_airdcpp_supervisor_registry
        from pullbox.models.airdcpp import AirDcppAcquisition
        from pullbox.providers.airdcpp.errors import AirDcppEntityNotFoundError
        from pullbox.providers.airdcpp.supervisor import AirDcppSupervisorState

        acquisition = (
            await session.execute(
                select(AirDcppAcquisition).where(
                    AirDcppAcquisition.download_history_id == download.id
                )
            )
        ).scalar_one_or_none()
        if acquisition is None:
            logger.warning("cancel_airdcpp_reference_invalid", download_id=download.id)
            return
        if acquisition.bundle_id is None:
            cancellation = await session.execute(
                update(AirDcppAcquisition)
                .where(
                    AirDcppAcquisition.id == acquisition.id,
                    AirDcppAcquisition.bundle_id.is_(None),
                )
                .values(
                    client_state="cancelled",
                    next_retry_at=None,
                    reconciliation_error=None,
                )
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            if cancellation.rowcount == 1:  # type: ignore[attr-defined]
                await session.refresh(acquisition)
                logger.info(
                    "airdcpp_pre_bundle_cancelled",
                    download_id=download.id,
                    client_config_id=acquisition.client_config_id,
                )
                return
            await session.refresh(acquisition)
            if acquisition.bundle_id is None:
                logger.warning("cancel_airdcpp_reference_invalid", download_id=download.id)
                return
        air_registry = get_airdcpp_supervisor_registry()
        supervisor = (
            air_registry.get(acquisition.client_config_id)
            if air_registry is not None and acquisition.client_config_id is not None
            else None
        )
        if supervisor is None or supervisor.state is not AirDcppSupervisorState.READY:
            logger.warning(
                "cancel_airdcpp_client_unavailable",
                download_id=download.id,
                client_config_id=acquisition.client_config_id,
            )
            raise HTTPException(
                status_code=503,
                detail="AirDC++ is unavailable; the download was not cancelled.",
            )
        bundle_id = acquisition.bundle_id
        # Release the route transaction before the external mutation.
        await session.commit()
        try:
            await supervisor.api_client.remove_queue_bundle(bundle_id)
        except AirDcppEntityNotFoundError:
            pass
        except Exception as exc:
            logger.warning(
                "cancel_airdcpp_failed",
                download_id=download.id,
                client_config_id=acquisition.client_config_id,
                exc_info=True,
            )
            raise HTTPException(
                status_code=502,
                detail="AirDC++ could not confirm cancellation; the download remains active.",
            ) from exc
        acquisition.client_state = "cancelled"
        acquisition.next_retry_at = None
        acquisition.reconciliation_error = None
        logger.info(
            "airdcpp_bundle_cancelled",
            download_id=download.id,
            bundle_id=bundle_id,
            client_config_id=acquisition.client_config_id,
        )
        return

    if not download.external_id:
        return

    from pullbox.composition.providers import register_download_clients

    registry = ProviderRegistry()
    await register_download_clients(session, registry)

    client = _registered_client_for_download(registry, download)

    if not client:
        logger.warning(
            "cancel_no_client_configured",
            download_id=download.id,
            client_type=download.download_client,
        )
        return

    try:
        removed = await client.remove_download(download.external_id, delete_files=True)
        if removed:
            logger.info("download_cancelled_on_client", download_id=download.id)
        else:
            logger.info(
                "download_not_found_on_client",
                download_id=download.id,
                external_id=download.external_id,
            )
    except Exception:
        logger.exception("cancel_client_error", download_id=download.id)


def _direct_attempt_id(external_id: str | None) -> int | None:
    prefix = "direct:"
    if not external_id or not external_id.startswith(prefix):
        return None
    raw_id = external_id.removeprefix(prefix)
    return int(raw_id) if raw_id.isdigit() and int(raw_id) > 0 else None


async def _direct_attempt_for_download(
    session: DbSession,
    download_id: int,
) -> tuple[DownloadHistory, DirectAcquisitionAttempt]:
    download = await session.get(DownloadHistory, download_id)
    if download is None:
        raise NotFoundError("Download", download_id)
    if download.download_client is not DownloadClientType.DIRECT:
        raise HTTPException(status_code=409, detail="Only direct downloads can change sources.")
    attempt_id = _direct_attempt_id(download.external_id)
    if attempt_id is None:
        raise HTTPException(status_code=409, detail="The direct download reference is invalid.")
    attempt = (
        await session.execute(
            select(DirectAcquisitionAttempt)
            .options(selectinload(DirectAcquisitionAttempt.artifact_attempts))
            .where(DirectAcquisitionAttempt.id == attempt_id)
        )
    ).scalar_one_or_none()
    if attempt is None:
        raise HTTPException(status_code=409, detail="The direct download plan is unavailable.")
    return download, attempt


@router.get("/{download_id}/sources", response_model=DirectSourceOptionsResponse)
async def direct_download_sources(
    download_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> DirectSourceOptionsResponse:
    """List available and unblocked equivalent artifact routes."""
    _download, attempt = await _direct_attempt_for_download(session, download_id)
    selected = [artifact for artifact in attempt.artifact_attempts if artifact.is_selected]
    if len(selected) != 1:
        raise HTTPException(
            status_code=409,
            detail="This direct download does not have one active artifact source.",
        )
    current = selected[0]
    options = await list_source_switch_options(session, attempt)
    return DirectSourceOptionsResponse(
        download_id=download_id,
        current=DirectSourceCurrent(
            artifact_identity=current.artifact_identity,
            host_kind=current.host_kind,
            host_label=direct_source_host_label(current.host_kind),
            bytes_transferred=current.bytes_transferred,
        ),
        alternatives=[
            DirectSourceAlternative(
                artifact_identity=option.artifact_identity,
                host_kind=option.host_kind,
                host_label=direct_source_host_label(option.host_kind),
                expected_size=option.expected_size,
                is_next=index == 0,
            )
            for index, option in enumerate(options)
        ],
    )


@router.post("/{download_id}/switch-source", response_model=DirectSourceSwitchResponse)
async def switch_direct_download_source(
    download_id: int,
    body: DirectSourceSwitchRequest,
    _user: AuthenticatedUser,
    session: DbSession,
) -> DirectSourceSwitchResponse:
    """Stop one direct transfer and restart it through an equivalent route."""
    _download, attempt = await _direct_attempt_for_download(session, download_id)
    from pullbox.tasks.direct_acquisition_task import get_direct_acquisition_runner

    try:
        runner = get_direct_acquisition_runner()
        outcome = await runner.switch_source(
            attempt.id,
            target_artifact_identity=body.artifact_identity,
            block_current=body.block_current,
        )
    except DirectSourceSwitchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Direct download source switching is not available right now.",
        ) from exc
    logger.info(
        "direct_download_source_switch_requested",
        download_id=download_id,
        acquisition_id=attempt.id,
        previous_host=outcome.previous_host.value,
        selected_host=outcome.selected.host_kind.value,
        current_route_blocklisted=outcome.current_route_blocklisted,
    )
    return DirectSourceSwitchResponse(
        previous_host=direct_source_host_label(outcome.previous_host),
        selected_host=direct_source_host_label(outcome.selected.host_kind),
        current_route_blocklisted=outcome.current_route_blocklisted,
    )


@router.post("/{download_id}/retry-processing", status_code=200)
async def retry_post_processing(
    download_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> dict[str, str]:
    """Retry post-processing for a failed download.

    Re-queues a FAILED download that has a downloaded_path so the
    process_completed task picks it up again. The last error is preserved
    until the import actually succeeds.
    """
    download = await session.get(DownloadHistory, download_id)
    if not download:
        raise NotFoundError("Download", download_id)

    if download.state != DownloadState.FAILED or not download.downloaded_path:
        raise HTTPException(
            status_code=409,
            detail="Only failed post-processing items can be retried.",
        )

    download.state = DownloadState.COMPLETED
    download.post_processing_claim_token = None
    download.post_processing_claimed_at = None
    await session.flush()

    # Trigger post-processing immediately
    from pullbox.core.scheduler import get_scheduler

    get_scheduler().run_task_now("process_completed")

    logger.info("post_processing_retry_triggered", download_id=download_id)
    return {"status": "queued"}


@router.post("/{download_id}/blocklist", response_model=BlocklistEntryResponse, status_code=201)
async def blocklist_failed_download(
    download_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> BlocklistEntryResponse:
    """Add a failed download or failed post-processing row to the blocklist."""
    from pullbox.core.release_parser import parse_release_title

    result = await session.execute(
        select(DownloadHistory)
        .options(joinedload(DownloadHistory.issue))
        .where(DownloadHistory.id == download_id)
    )
    download = result.scalar_one_or_none()
    if not download:
        raise NotFoundError("Download", download_id)

    if download.state != DownloadState.FAILED:
        raise HTTPException(
            status_code=409,
            detail="Only failed download history items can be blocklisted.",
        )

    if download.error_message == "Cancelled by user":
        raise HTTPException(
            status_code=409,
            detail="Cancelled downloads cannot be blocklisted.",
        )

    parsed = parse_release_title(download.title)
    release_group = parsed.scan_group if parsed else None
    series_id = download.issue.series_id if download.issue else None

    entry = await BlocklistService.add_entry(
        session,
        download.title,
        BlocklistReason.FAILED,
        download_url=download.download_url,
        series_id=series_id,
        issue_id=download.issue_id,
        indexer_id=download.indexer_id,
        error_message=download.error_message,
        release_group=release_group,
        download_history_id=download.id,
    )
    if entry is None:
        raise HTTPException(
            status_code=409,
            detail={"error": {"message": "Release already in blocklist"}},
        )

    logger.info(
        "download_history_blocklisted",
        download_id=download.id,
        issue_id=download.issue_id,
        state=download.state.value,
        has_downloaded_path=bool(download.downloaded_path),
    )
    return _blocklist_entry_to_response(entry)


@router.post("/{download_id}/retry", status_code=200)
async def retry_download(
    download_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> dict[str, str]:
    """Retry a failed or cancelled download.

    Re-sends the original download URL to the download client and resets
    the download state to SENT.
    """
    from datetime import UTC, datetime, timedelta

    from pullbox.composition.providers import register_download_clients, register_indexers
    from pullbox.composition.services import build_download_service

    download = await session.get(DownloadHistory, download_id)
    if not download:
        raise NotFoundError("Download", download_id)

    if download.state != DownloadState.FAILED:
        raise HTTPException(status_code=409, detail="Only failed downloads can be retried.")

    # PP failures should use the retry-processing endpoint
    if download.downloaded_path and download.error_message != "Cancelled by user":
        raise HTTPException(
            status_code=409,
            detail="Use retry-processing for post-processing failures.",
        )

    if download.download_client is DownloadClientType.DIRECT:
        attempt_id = _direct_attempt_id(download.external_id)
        if attempt_id is None:
            raise HTTPException(
                status_code=409,
                detail="The direct download reference is invalid.",
            )
        from pullbox.tasks.direct_acquisition_task import get_direct_acquisition_runner

        try:
            queued = await get_direct_acquisition_runner().retry(attempt_id)
        except RuntimeError as exc:
            raise HTTPException(
                status_code=503,
                detail="Direct download recovery is not available right now.",
            ) from exc
        if not queued:
            raise HTTPException(
                status_code=409,
                detail="This direct download cannot be retried from its current state.",
            )
        issue = await session.get(Issue, download.issue_id)
        if issue and issue.status in (IssueStatus.WANTED, IssueStatus.OWNED):
            issue.status = IssueStatus.DOWNLOADING
        await session.flush()
        logger.info(
            "direct_download_retry_queued",
            download_id=download.id,
            acquisition_id=attempt_id,
        )
        return {"status": "sent"}

    if download.download_client is DownloadClientType.AIRDCPP:
        from pullbox.composition.airdcpp import get_airdcpp_supervisor_registry
        from pullbox.models.airdcpp import AirDcppAcquisition, AirDcppClientSettings
        from pullbox.providers.airdcpp.errors import AirDcppError
        from pullbox.providers.airdcpp.supervisor import AirDcppSupervisorState

        acquisition = (
            await session.execute(
                select(AirDcppAcquisition).where(
                    AirDcppAcquisition.download_history_id == download.id
                )
            )
        ).scalar_one_or_none()
        if acquisition is None:
            raise HTTPException(
                status_code=409,
                detail="The AirDC++ queue provenance is unavailable.",
            )
        now = datetime.now(UTC)
        missing_bundle = (
            acquisition.client_state == "missing"
            or acquisition.reconciliation_error == "external_bundle_missing"
        )
        if acquisition.bundle_id is None and not missing_bundle:
            raise HTTPException(
                status_code=409,
                detail="The AirDC++ queue provenance is unavailable.",
            )
        if missing_bundle:
            client_config_id = acquisition.client_config_id
            supervisor_registry = get_airdcpp_supervisor_registry()
            supervisor = (
                supervisor_registry.get(client_config_id)
                if supervisor_registry is not None and client_config_id is not None
                else None
            )
            if supervisor is None or supervisor.state is not AirDcppSupervisorState.READY:
                raise HTTPException(
                    status_code=503,
                    detail="AirDC++ is unavailable; the missing queue item was not recreated.",
                )
            settings = await session.scalar(
                select(AirDcppClientSettings).where(
                    AirDcppClientSettings.client_config_id == client_config_id
                )
            )
            queue_priority = settings.queue_priority if settings is not None else None
            previous_client_state = acquisition.client_state
            previous_reconciliation_error = acquisition.reconciliation_error
            previous_error_message = download.error_message
            claim_deadline = now + timedelta(minutes=5)
            claim_result = await session.execute(
                update(AirDcppAcquisition)
                .where(
                    AirDcppAcquisition.id == acquisition.id,
                    AirDcppAcquisition.client_state == previous_client_state,
                    AirDcppAcquisition.reconciliation_error == previous_reconciliation_error,
                )
                .values(
                    client_state="retry_mutation_pending",
                    next_retry_at=claim_deadline,
                )
                .execution_options(synchronize_session=False)
            )
            if claim_result.rowcount != 1:  # type: ignore[attr-defined]
                await session.rollback()
                raise HTTPException(
                    status_code=409,
                    detail="This AirDC++ download is already being retried.",
                )
            download.state = DownloadState.RETRY_PENDING
            download.next_retry_at = claim_deadline
            download.error_message = "Recreating the missing AirDC++ queue item."
            await session.commit()
            try:
                added = await supervisor.api_client.create_file_bundle(
                    tth=acquisition.tth,
                    size=acquisition.size_bytes,
                    target_name=acquisition.original_name,
                    priority=queue_priority,
                )
            except AirDcppError as exc:
                await session.refresh(acquisition)
                await session.refresh(download)
                if (
                    acquisition.client_state == "retry_mutation_pending"
                    and download.state is DownloadState.RETRY_PENDING
                    and acquisition.next_retry_at == claim_deadline
                    and download.next_retry_at == claim_deadline
                ):
                    acquisition.client_state = previous_client_state
                    acquisition.next_retry_at = None
                    acquisition.reconciliation_error = previous_reconciliation_error
                    download.state = DownloadState.FAILED
                    download.next_retry_at = None
                    download.error_message = previous_error_message
                    await session.commit()
                raise HTTPException(
                    status_code=502,
                    detail="AirDC++ could not recreate the missing queue item.",
                ) from exc
            except Exception as exc:
                logger.warning(
                    "airdcpp_missing_bundle_recreation_failed",
                    download_id=download.id,
                    client_config_id=client_config_id,
                    exc_info=True,
                )
                await session.refresh(acquisition)
                await session.refresh(download)
                if (
                    acquisition.client_state == "retry_mutation_pending"
                    and download.state is DownloadState.RETRY_PENDING
                    and acquisition.next_retry_at == claim_deadline
                    and download.next_retry_at == claim_deadline
                ):
                    acquisition.client_state = previous_client_state
                    acquisition.next_retry_at = None
                    acquisition.reconciliation_error = previous_reconciliation_error
                    download.state = DownloadState.FAILED
                    download.next_retry_at = None
                    download.error_message = previous_error_message
                    await session.commit()
                raise HTTPException(
                    status_code=502,
                    detail="AirDC++ could not recreate the missing queue item.",
                ) from exc
            await session.refresh(acquisition)
            await session.refresh(download)
            if (
                acquisition.client_state != "retry_mutation_pending"
                or download.state is not DownloadState.RETRY_PENDING
                or acquisition.next_retry_at != claim_deadline
                or download.next_retry_at != claim_deadline
            ):
                await session.commit()
                if not added.merged:
                    try:
                        await supervisor.api_client.remove_queue_bundle(added.id)
                    except AirDcppError as exc:
                        logger.warning(
                            "airdcpp_superseded_retry_cleanup_failed",
                            download_id=download.id,
                            bundle_id=added.id,
                            error_code=exc.code,
                        )
                raise HTTPException(
                    status_code=409,
                    detail="The AirDC++ retry was superseded before it completed.",
                )
            acquisition.bundle_id = added.id
            acquisition.remote_target = None
            acquisition.last_reconciled_at = None
            route_snapshot = dict(acquisition.route_snapshot or {})
            route_snapshot.pop("queue", None)
            acquisition.route_snapshot = route_snapshot
            download.external_id = f"airdcpp:{client_config_id}:bundle:{added.id}"
        acquisition.client_state = "source_search_pending"
        acquisition.retry_count = 0
        acquisition.next_retry_at = now
        acquisition.reconciliation_error = None
        download.state = DownloadState.RETRY_PENDING
        download.retry_count = 0
        download.next_retry_at = now
        download.error_message = None
        download.completed_at = None
        issue = await session.get(Issue, download.issue_id)
        if issue and issue.status in (IssueStatus.WANTED, IssueStatus.OWNED):
            issue.status = IssueStatus.DOWNLOADING
        await session.flush()
        logger.info(
            "airdcpp_download_retry_queued",
            download_id=download.id,
            acquisition_id=acquisition.id,
            bundle_id=acquisition.bundle_id,
            client_config_id=acquisition.client_config_id,
        )
        return {"status": "sent"}

    # Build provider registry and get the right client
    registry = ProviderRegistry()
    await register_download_clients(session, registry)
    await register_indexers(session, registry)

    client = _registered_client_for_download(registry, download)
    if not client:
        raise HTTPException(
            status_code=503,
            detail="No download client configured for this download type.",
        )

    # Re-send to client
    protocol = AcquisitionProtocol(download.protocol)
    try:
        if protocol is AcquisitionProtocol.TORRENT:
            external_id = await build_download_service(registry).add_torrent_to_client(
                client,
                url=download.download_url,
                title=download.title,
                indexer_id=download.indexer_id,
                download_id=download.id,
            )
        elif protocol is AcquisitionProtocol.USENET:
            external_id = await client.add_nzb(download.download_url, download.title)
        else:
            raise HTTPException(
                status_code=409,
                detail=f"Unsupported acquisition protocol: {protocol.value}",
            )
    except (NewznabError, QBittorrentError) as exc:
        logger.warning(
            "download_retry_client_rejected",
            download_id=download.id,
            issue_id=download.issue_id,
            indexer_id=download.indexer_id,
            client_type=str(download.download_client),
            protocol=protocol.value,
            error=str(exc),
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    download.external_id = external_id
    download.state = DownloadState.SENT
    download.sent_at = datetime.now(UTC)
    download.error_message = None
    download.downloaded_path = None
    download.completed_at = None

    # Set issue back to DOWNLOADING
    issue = await session.get(Issue, download.issue_id)
    if issue and issue.status in (IssueStatus.WANTED, IssueStatus.OWNED):
        issue.status = IssueStatus.DOWNLOADING

    await session.flush()
    logger.info("download_retry_sent", download_id=download_id, external_id=external_id)
    return {"status": "sent"}


@router.delete("/{download_id}", status_code=204)
async def cancel_download(
    download_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> None:
    """Cancel an active download or remove a history entry.

    For active downloads (QUEUED, SENT, DOWNLOADING, FINALIZING, PAUSED, RETRY_PENDING):
    - Cancels on the download client (SABnzbd/qBittorrent)
    - Clears the in-memory progress cache entry
    - Reverts the related issue status from DOWNLOADING → WANTED
    - Marks the download as FAILED with "Cancelled by user"

    For POST_PROCESSING: rejects deletion (must finish processing).
    For terminal-state records (COMPLETED, FAILED, IMPORTED): deletes from history.
    """
    download = await session.get(DownloadHistory, download_id)
    if not download:
        raise NotFoundError("Download", download_id)

    if download.state in _CANCELLABLE_STATES:
        # Cancel on the download client (best-effort)
        await _cancel_on_client(download, session)

        # Clear progress cache
        from pullbox.tasks.download_task import _clear_progress

        _clear_progress(download_id)

        # Revert issue status
        issue = await session.get(Issue, download.issue_id)
        if issue and issue.status == IssueStatus.DOWNLOADING:
            issue.status = IssueStatus.WANTED

        download.state = DownloadState.FAILED
        download.error_message = "Cancelled by user"
        logger.info("download_cancelled", download_id=download_id)
    else:
        await session.delete(download)
        logger.info("download_history_removed", download_id=download_id)
