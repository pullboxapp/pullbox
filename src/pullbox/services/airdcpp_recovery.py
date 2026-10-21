"""Actionable recovery for unconfirmed AirDC++ queue requests."""

from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import PurePath
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import joinedload

from pullbox.models.airdcpp import AirDcppAcquisition
from pullbox.models.download import DownloadHistory, DownloadState
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
from pullbox.providers.airdcpp.errors import AirDcppEntityNotFoundError, AirDcppError
from pullbox.services.airdcpp_acquisition import AirDcppQueueApi, lock_queue_intents

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.providers.airdcpp.contracts import AirDcppQueueBundle


class AirDcppRecoveryApi(AirDcppQueueApi, Protocol):
    async def get_queue_bundle(self, bundle_id: int) -> AirDcppQueueBundle: ...


RECOVERY_LINK = "/intervention?tab=recovery&protocol=dc"
RECOVERY_MESSAGES = {
    "queue_hash_mismatch": (
        "AirDC++ stopped this download because its file hash does not match (TTH inconsistency). "
        "Check or repair the download in AirDC++, then Recheck AirDC++; "
        "or cancel this request. Pullbox will not bypass the integrity check."
    ),
    "queue_client_failed": (
        "An earlier AirDC++ download needs review. Check its queue error, correct it if needed, "
        "then Recheck AirDC++; or cancel this request. No new download was started."
    ),
    "queue_lookup_failed": (
        "Pullbox could not verify this request in AirDC++. "
        "Recheck the client before retrying or cancelling."
    ),
    "queue_identity_ambiguous": (
        "AirDC++ returned conflicting queue identities. "
        "Check the client; Pullbox has not queued another copy."
    ),
    "queue_request_unconfirmed": (
        "AirDC++ has not confirmed this request. "
        "Recheck the client, retry the request, or cancel it."
    ),
    "queue_bundle_shared": (
        "This AirDC++ bundle is shared by another Pullbox request. "
        "Recheck AirDC++ to reconnect it, or cancel the shared bundle in AirDC++ "
        "before cancelling this request. No download or library file was removed."
    ),
}


def queue_failure_code(bundle: AirDcppQueueBundle) -> str:
    return (
        "queue_hash_mismatch"
        if (bundle.status.failed or bundle.status.id == "download_error")
        and "tth inconsistency" in bundle.status.str.casefold()
        else "queue_client_failed"
    )


async def record_queue_recovery(
    session: AsyncSession, acquisition: AirDcppAcquisition, *, code: str, at: datetime
) -> None:
    """Stop automatic mutation and preserve one actionable record per request."""
    history = acquisition.download_history
    acquisition.client_state = "recovery_required"
    acquisition.next_retry_at = None
    acquisition.last_reconciled_at = at
    acquisition.reconciliation_error = code
    history.state = DownloadState.FAILED
    history.completed_at = at
    history.next_retry_at = None
    history.error_message = RECOVERY_MESSAGES[code]
    locator = f"airdcpp-recovery://download/{history.id}"
    pending = await session.scalar(
        select(PendingMatch).where(
            PendingMatch.issue_id == history.issue_id, PendingMatch.download_url == locator
        )
    )
    details = {
        "source_kind": "dc",
        "provider_name": "AirDC++",
        "failure_class": "queue_confirmation",
        "failure_code": code,
        "dc_download_id": history.id,
    }
    if pending is None:
        pending = PendingMatch(
            issue_id=history.issue_id,
            release_title=history.title,
            download_url=locator,
            file_size=history.file_size,
            confidence="high",
            match_details=details,
        )
        session.add(pending)
    else:
        pending.match_details = details
        pending.status = PendingMatchStatus.PENDING
        pending.resolved_at = None
        pending.resolved_by = None
    await session.flush()


async def finish_queue_recovery(
    session: AsyncSession, download_id: int, *, cancelled: bool = False
) -> None:
    pending = await session.scalar(
        select(PendingMatch).where(
            PendingMatch.download_url == f"airdcpp-recovery://download/{download_id}",
            PendingMatch.status == PendingMatchStatus.PENDING,
        )
    )
    if pending is not None:
        pending.status = PendingMatchStatus.REJECTED if cancelled else PendingMatchStatus.APPROVED
        pending.resolved_at = datetime.now(UTC)
        pending.resolved_by = "cancelled" if cancelled else "queue_confirmed"


async def act_on_queue_recovery(
    session: AsyncSession, pending_id: int, action: str, api: AirDcppRecoveryApi
) -> str:
    """Recheck exact provenance before any explicit retry or cancellation."""
    from pullbox.services.airdcpp_reconciliation import (
        _apply_adopted_file,
        _apply_existing_bundle_owner,
        _apply_recovered_mutation,
        _RecoveredMutation,
        _shared_progress_snapshot,
        apply_airdcpp_bundle,
    )
    from pullbox.services.download_operation_progress import project_download_operation_progress

    if action not in {"recheck", "retry", "cancel"}:
        raise ValueError("Unknown AirDC++ recovery action.")
    pending = await session.get(PendingMatch, pending_id)
    if pending is None or pending.status != PendingMatchStatus.PENDING:
        raise ValueError("This recovery item is no longer pending.")
    details = pending.match_details or {}
    download_id = details.get("dc_download_id")
    if details.get("source_kind") != "dc" or type(download_id) is not int:
        raise ValueError("This is not an AirDC++ recovery request.")
    acquisition = await session.scalar(
        select(AirDcppAcquisition)
        .options(joinedload(AirDcppAcquisition.download_history))
        .where(AirDcppAcquisition.download_history_id == download_id)
    )
    if (
        acquisition is None
        or acquisition.client_state != "recovery_required"
        or acquisition.download_history.issue_id != pending.issue_id
        or acquisition.download_history.state is not DownloadState.FAILED
        or acquisition.download_history.imported_at is not None
    ):
        raise ValueError("This AirDC++ request has changed. Refresh before trying again.")
    history = acquisition.download_history
    # A durable claim prevents concurrent buttons from repeating an external mutation;
    # the periodic reconciler can recover this claim after a process restart.
    deadline = datetime.now(UTC) + timedelta(minutes=5)
    acquisition_id = acquisition.id
    client_identity = acquisition.client_identity
    expected_bundle = acquisition.bundle_id
    await lock_queue_intents(session, acquisition.client_config_id)
    claimed = await session.scalar(
        update(AirDcppAcquisition)
        .where(
            AirDcppAcquisition.id == acquisition.id,
            AirDcppAcquisition.client_state == "recovery_required",
            AirDcppAcquisition.bundle_id == expected_bundle,
        )
        .values(client_state="recovery_check_pending", next_retry_at=deadline)
        .returning(AirDcppAcquisition.id)
    )
    if claimed is None:
        await session.rollback()
        raise ValueError("This AirDC++ request is already being checked.")
    history.state = DownloadState.RETRY_PENDING
    history.next_retry_at = deadline
    await session.commit()

    code = "queue_lookup_failed"
    created_bundle: int | None = None
    try:
        files = await api.get_queue_files_by_tth(acquisition.tth)
        exact = [
            item
            for item in files
            if item.tth == acquisition.tth
            and item.size == acquisition.size_bytes
            and PurePath(item.target.get_secret_value().replace("\\", "/")).name
            == acquisition.original_name
        ]
        bundle_ids = {item.bundle_id for item in exact}
        if files and (len(bundle_ids) != 1 or len(exact) != len(files)):
            code = "queue_identity_ambiguous"
            raise ValueError(RECOVERY_MESSAGES[code])
        bundle_id = exact[0].bundle_id if exact else None
        bundle = None
        probe_id = bundle_id or expected_bundle
        if expected_bundle is not None and bundle_id not in {None, expected_bundle}:
            code = "queue_identity_ambiguous"
            raise ValueError(RECOVERY_MESSAGES[code])
        if probe_id is not None:
            try:
                bundle = await api.get_queue_bundle(probe_id)
            except AirDcppEntityNotFoundError:
                if exact:
                    raise ValueError(RECOVERY_MESSAGES[code]) from None
            if bundle is not None:
                if (
                    not exact
                    or bundle.id != probe_id
                    or bundle.type.id != "file"
                    or bundle.size != acquisition.size_bytes
                    or PurePath(bundle.target.get_secret_value().replace("\\", "/")).name
                    != acquisition.original_name
                ):
                    code = "queue_identity_ambiguous"
                    raise ValueError(RECOVERY_MESSAGES[code])
                if (
                    bundle.status.failed
                    or bundle.status.id in {"download_error", "completion_validation_error"}
                ) and action != "cancel":
                    code = queue_failure_code(bundle)
                    raise ValueError(RECOVERY_MESSAGES[code])
        owner_id = (
            await session.scalar(
                select(AirDcppAcquisition.id).where(
                    AirDcppAcquisition.client_identity == acquisition.client_identity,
                    AirDcppAcquisition.bundle_id == bundle_id,
                    AirDcppAcquisition.id != acquisition.id,
                )
            )
            if bundle_id is not None
            else None
        )
        shared_bundle = owner_id is not None
        if action == "cancel" and bundle_id is not None:
            # Intents admitted before our claim may still be merging remotely.
            # Later admissions are blocked by the durable claim above.
            shared_bundle = (
                await session.scalar(
                    select(AirDcppAcquisition.id)
                    .join(DownloadHistory)
                    .where(
                        AirDcppAcquisition.client_identity == client_identity,
                        AirDcppAcquisition.id != acquisition_id,
                        or_(
                            AirDcppAcquisition.bundle_id == bundle_id,
                            and_(
                                AirDcppAcquisition.tth == acquisition.tth,
                                AirDcppAcquisition.size_bytes == acquisition.size_bytes,
                                AirDcppAcquisition.client_state.is_distinct_from("cancelled"),
                                DownloadHistory.imported_at.is_(None),
                            ),
                        ),
                    )
                    .limit(1)
                )
            ) is not None
        issue = await session.get(Issue, history.issue_id)
        another_active = await session.scalar(
            select(DownloadHistory.id)
            .where(
                DownloadHistory.issue_id == history.issue_id,
                DownloadHistory.id != history.id,
                DownloadHistory.state.in_(
                    [
                        DownloadState.QUEUED,
                        DownloadState.SENT,
                        DownloadState.DOWNLOADING,
                        DownloadState.RETRY_PENDING,
                        DownloadState.PAUSED,
                        DownloadState.FINALIZING,
                        DownloadState.POST_PROCESSING,
                    ]
                ),
            )
            .limit(1)
        )
        still_claimed = await session.scalar(
            select(AirDcppAcquisition.id).where(
                AirDcppAcquisition.id == acquisition_id,
                AirDcppAcquisition.client_state == "recovery_check_pending",
                AirDcppAcquisition.next_retry_at == deadline,
                AirDcppAcquisition.bundle_id == expected_bundle,
            )
        )
        await session.commit()
        if still_claimed is None:
            raise ValueError(
                "This request changed while it was being checked. Refresh before retrying."
            )
        if action == "cancel" and shared_bundle and expected_bundle == bundle_id:
            code = "queue_bundle_shared"
            raise ValueError(RECOVERY_MESSAGES[code])
        added = None
        if not files and action == "retry":
            if issue is None or issue.status is IssueStatus.OWNED or another_active is not None:
                raise ValueError(
                    "This issue is already owned or has another active download. "
                    "No new request was sent."
                )
            priority = (acquisition.route_snapshot or {}).get("queue_priority")
            added = await api.create_file_bundle(
                tth=acquisition.tth,
                size=acquisition.size_bytes,
                target_name=acquisition.original_name,
                priority=priority if type(priority) is int else None,
            )
            bundle_id = added.id
            created_bundle = None if added.merged else added.id
        elif action == "cancel" and bundle_id is not None and not shared_bundle:
            with suppress(AirDcppEntityNotFoundError):
                await api.remove_queue_bundle(bundle_id)

        current = await session.scalar(
            update(AirDcppAcquisition)
            .where(
                AirDcppAcquisition.id == acquisition.id,
                AirDcppAcquisition.client_state == "recovery_check_pending",
                AirDcppAcquisition.next_retry_at == deadline,
                AirDcppAcquisition.bundle_id == expected_bundle,
            )
            .values(client_state="recovery_check_pending")
            .returning(AirDcppAcquisition.id)
        )
        if current is None:
            await session.rollback()
            if created_bundle is not None:
                owner = await session.scalar(
                    select(AirDcppAcquisition.id).where(
                        AirDcppAcquisition.client_identity == client_identity,
                        AirDcppAcquisition.bundle_id == created_bundle,
                    )
                )
                await session.commit()
                if owner is None:
                    await api.remove_queue_bundle(created_bundle)
            raise ValueError(
                "The request changed while AirDC++ was responding. "
                "Refresh to see its current state."
            )
        await session.refresh(history)
        if issue is not None:
            await session.refresh(issue)
        now = datetime.now(UTC)
        if action == "cancel":
            acquisition.client_state = "cancelled"
            acquisition.next_retry_at = None
            acquisition.reconciliation_error = None
            history.state = DownloadState.FAILED
            history.error_message = "Cancelled by user"
            history.completed_at = now
            history.next_retry_at = None
            if (
                issue is not None
                and issue.status is IssueStatus.DOWNLOADING
                and another_active is None
            ):
                issue.status = IssueStatus.WANTED
            message = "AirDC++ request cancelled. No library files were deleted."
            if shared_bundle:
                message += " The shared AirDC++ bundle was kept for another request."
        elif bundle_id is not None:
            owner_id = await session.scalar(
                select(AirDcppAcquisition.id).where(
                    AirDcppAcquisition.client_identity == acquisition.client_identity,
                    AirDcppAcquisition.bundle_id == bundle_id,
                    AirDcppAcquisition.id != acquisition.id,
                )
            )
            if owner_id is not None:
                _apply_existing_bundle_owner(acquisition, owner_acquisition_id=owner_id, at=now)
                message = "This request is already covered by another Pullbox download."
            elif exact:
                history.state = DownloadState.QUEUED
                _apply_adopted_file(acquisition, exact[0], at=now)
                if bundle is not None:
                    apply_airdcpp_bundle(acquisition, bundle, at=now)
                message = "Existing AirDC++ download reconnected."
            else:
                _apply_recovered_mutation(
                    acquisition,
                    _RecoveredMutation(
                        bundle_id, "source_search_pending", created_bundle is not None
                    ),
                    at=now,
                )
                message = "AirDC++ request queued."
            if owner_id is None and issue is not None and issue.status is IssueStatus.WANTED:
                issue.status = IssueStatus.DOWNLOADING
        else:
            await record_queue_recovery(
                session, acquisition, code="queue_request_unconfirmed", at=now
            )
            message = (
                "No matching AirDC++ queue item was found. You can retry or cancel this request."
            )
        if action == "cancel" or bundle_id is not None:
            await finish_queue_recovery(session, history.id, cancelled=action == "cancel")
        await project_download_operation_progress(
            session, history, _shared_progress_snapshot(acquisition)
        )
        await session.commit()
        return message
    except (AirDcppError, ValueError) as exc:
        await session.rollback()
        current = await session.scalar(
            update(AirDcppAcquisition)
            .where(
                AirDcppAcquisition.id == acquisition_id,
                AirDcppAcquisition.client_state == "recovery_check_pending",
                AirDcppAcquisition.next_retry_at == deadline,
            )
            .values(client_state="recovery_required")
            .returning(AirDcppAcquisition.id)
        )
        if current is not None:
            await session.refresh(acquisition)
            await session.refresh(history)
            await record_queue_recovery(session, acquisition, code=code, at=datetime.now(UTC))
            await project_download_operation_progress(session, history)
            await session.commit()
        if isinstance(exc, AirDcppError):
            raise ValueError(RECOVERY_MESSAGES[code]) from exc
        raise
