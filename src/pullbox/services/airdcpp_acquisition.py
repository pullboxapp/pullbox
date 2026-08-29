"""Persist Direct Connect queue intent before bounded external mutation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePath
from typing import TYPE_CHECKING, Protocol

import structlog
from sqlalchemy import select, update

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.models.airdcpp import AirDcppAcquisition
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import Issue, IssueStatus
from pullbox.providers.airdcpp.errors import (
    AirDcppEntityNotFoundError,
    AirDcppError,
    AirDcppUnavailableError,
)

_INITIAL_MUTATION_RECOVERY_DELAY = timedelta(minutes=5)
_TERMINAL_STATES = {
    DownloadState.COMPLETED,
    DownloadState.FAILED,
    DownloadState.IMPORTED,
}

logger = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.providers.airdcpp.contracts import (
        AirDcppQueueBundleAddInfo,
        AirDcppQueueFile,
    )
    from pullbox.services.airdcpp_search_types import DcValidatedCandidate


class AirDcppQueueApi(Protocol):
    async def download_search_result(
        self,
        instance_id: int,
        result_id: str,
        *,
        target_name: str,
        priority: int | None,
    ) -> AirDcppQueueBundleAddInfo: ...

    async def get_queue_files_by_tth(self, tth: str) -> list[AirDcppQueueFile]: ...

    async def create_file_bundle(
        self,
        *,
        tth: str,
        size: int,
        target_name: str,
        priority: int | None,
    ) -> AirDcppQueueBundleAddInfo: ...

    async def remove_queue_bundle(self, bundle_id: int) -> None: ...


@dataclass(frozen=True, slots=True)
class AirDcppQueueAcquisitionResult:
    acquisition_id: int
    download_history_id: int
    bundle_id: int | None
    merged: bool | None
    state: DownloadState


class AirDcppQueueAcquisitionService:
    """Create one idempotent intent and reconcile ambiguous mutation outcomes."""

    async def acquire(
        self,
        session: AsyncSession,
        *,
        candidate: DcValidatedCandidate,
        issue_id: int,
        request_key: str,
        search_log_id: int | None,
        api_client: AirDcppQueueApi,
        queue_priority: int | None,
        replace_existing_file: bool,
    ) -> AirDcppQueueAcquisitionResult:
        if not request_key or len(request_key) > 255:
            raise ValueError("Invalid Direct Connect request identity")
        existing = (
            await session.execute(
                select(AirDcppAcquisition).where(AirDcppAcquisition.request_key == request_key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            history = await session.get(DownloadHistory, existing.download_history_id)
            if history is None:  # pragma: no cover - protected by the foreign key
                raise RuntimeError("Direct Connect history is unavailable")
            expected_client_state = existing.client_state
            expected_history_state = DownloadState(history.state)
            expected_retry_count = existing.retry_count
            await session.commit()
            if (
                existing.bundle_id is None
                and expected_history_state not in _TERMINAL_STATES
                and expected_client_state in {"mutation_pending", "reconcile_pending"}
            ):
                adopted = await self._find_adoptable(api_client, existing, history.title)
                if adopted is not None:
                    await self._record_bundle_if_current(
                        session,
                        existing,
                        history,
                        adopted.bundle_id,
                        expected_client_state=expected_client_state,
                        expected_history_state=expected_history_state,
                        expected_retry_count=expected_retry_count,
                        remote_target=adopted.target.get_secret_value(),
                    )
            return _result(existing, history, merged=None)

        issue = await session.get(Issue, issue_id)
        if issue is None:
            raise ValueError("Direct Connect issue is unavailable")
        route = candidate.route
        if route.client_config_id <= 0 or candidate.release.size_bytes != route.size_bytes:
            raise ValueError("Direct Connect route identity is invalid")
        target_name = _safe_target_name(candidate.release.title)
        now = datetime.now(UTC)
        history = DownloadHistory(
            issue_id=issue_id,
            download_client_config_id=route.client_config_id,
            title=target_name,
            download_url=f"airdcpp://intent/{request_key}",
            download_client=DownloadClientType.AIRDCPP,
            protocol=AcquisitionProtocol.DC,
            state=DownloadState.QUEUED,
            file_size=route.size_bytes,
            max_retries=3,
            sent_at=now,
            replace_existing_file=replace_existing_file,
        )
        session.add(history)
        await session.flush()
        acquisition = AirDcppAcquisition(
            download_history_id=history.id,
            request_key=request_key,
            client_config_id=route.client_config_id,
            client_identity=route.client_identity,
            search_log_id=search_log_id,
            tth=route.tth,
            size_bytes=route.size_bytes,
            original_name=target_name,
            search_instance_id=route.search_instance_id,
            grouped_result_id=route.grouped_result_id,
            result_expires_at=route.result_expires_at,
            client_state="mutation_pending",
            route_snapshot={
                "version": 1,
                "source_count": candidate.metrics.source_count,
                "free_slots": candidate.metrics.free_slots,
                "total_slots": candidate.metrics.total_slots,
                "result_kind": "file",
                "queue_priority": queue_priority,
            },
            retry_count=0,
            max_retries=3,
            next_retry_at=now + _INITIAL_MUTATION_RECOVERY_DELAY,
        )
        session.add(acquisition)
        issue.status = IssueStatus.DOWNLOADING
        await session.flush()
        await session.commit()

        merged: bool | None = None
        bundle_id: int | None = None
        remote_target: str | None = None
        mutation_error: str | None = None
        bundle_created = False
        source_search_pending = False
        try:
            added = await api_client.download_search_result(
                route.search_instance_id,
                route.grouped_result_id,
                target_name=target_name,
                priority=queue_priority,
            )
            bundle_id = added.id
            merged = added.merged
            bundle_created = not added.merged
        except AirDcppUnavailableError:
            adopted, recovery_error = await self._try_find_adoptable(
                api_client,
                acquisition,
                target_name,
            )
            bundle_id = adopted.bundle_id if adopted is not None else None
            if adopted is not None:
                remote_target = adopted.target.get_secret_value()
            elif recovery_error is not None:
                mutation_error = recovery_error
        except AirDcppEntityNotFoundError:
            try:
                added = await api_client.create_file_bundle(
                    tth=route.tth,
                    size=route.size_bytes,
                    target_name=target_name,
                    priority=queue_priority,
                )
            except AirDcppError as exc:
                mutation_error = exc.code
            else:
                bundle_id = added.id
                merged = added.merged
                bundle_created = not added.merged
                source_search_pending = True
        except AirDcppError as exc:
            mutation_error = exc.code

        if bundle_id is not None:
            recorded = await self._record_bundle_if_current(
                session,
                acquisition,
                history,
                bundle_id,
                expected_client_state="mutation_pending",
                expected_history_state=DownloadState.QUEUED,
                expected_retry_count=0,
                remote_target=remote_target,
                client_state=("source_search_pending" if source_search_pending else "queued"),
                next_retry_at=(now if source_search_pending else None),
            )
            if not recorded and bundle_created:
                await self._remove_superseded_bundle(api_client, acquisition, bundle_id)
        else:
            await self._record_retry_if_current(
                session,
                acquisition,
                history,
                now=now,
                mutation_error=mutation_error,
            )
        return _result(acquisition, history, merged=merged)

    async def _find_adoptable(
        self,
        api_client: AirDcppQueueApi,
        acquisition: AirDcppAcquisition,
        target_name: str,
    ) -> AirDcppQueueFile | None:
        files = await api_client.get_queue_files_by_tth(acquisition.tth)
        exact = [
            item
            for item in files
            if item.size == acquisition.size_bytes
            and PurePath(item.target.get_secret_value().replace("\\", "/")).name == target_name
        ]
        bundle_ids = {item.bundle_id for item in exact}
        return exact[0] if len(bundle_ids) == 1 else None

    async def _try_find_adoptable(
        self,
        api_client: AirDcppQueueApi,
        acquisition: AirDcppAcquisition,
        target_name: str,
    ) -> tuple[AirDcppQueueFile | None, str | None]:
        try:
            return (
                await self._find_adoptable(api_client, acquisition, target_name),
                None,
            )
        except AirDcppError as exc:
            return None, exc.code

    async def _record_bundle_if_current(
        self,
        session: AsyncSession,
        acquisition: AirDcppAcquisition,
        history: DownloadHistory,
        bundle_id: int,
        *,
        expected_client_state: str,
        expected_history_state: DownloadState,
        expected_retry_count: int,
        remote_target: str | None = None,
        client_state: str = "queued",
        next_retry_at: datetime | None = None,
    ) -> bool:
        values: dict[str, object] = {
            "bundle_id": bundle_id,
            "client_state": client_state,
            "next_retry_at": next_retry_at,
            "reconciliation_error": None,
            "last_reconciled_at": datetime.now(UTC),
        }
        if remote_target is not None:
            values["remote_target"] = remote_target
        acquisition_result = await session.execute(
            update(AirDcppAcquisition)
            .where(
                AirDcppAcquisition.id == acquisition.id,
                AirDcppAcquisition.bundle_id.is_(None),
                AirDcppAcquisition.client_state == expected_client_state,
                AirDcppAcquisition.retry_count == expected_retry_count,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if acquisition_result.rowcount != 1:  # type: ignore[attr-defined]
            await self._refresh_after_claim_loss(session, acquisition, history)
            return False
        history_result = await session.execute(
            update(DownloadHistory)
            .where(
                DownloadHistory.id == history.id,
                DownloadHistory.state == expected_history_state,
            )
            .values(
                external_id=f"airdcpp:{acquisition.client_config_id}:bundle:{bundle_id}",
                state=DownloadState.SENT,
                next_retry_at=None,
                error_message=None,
            )
            .execution_options(synchronize_session=False)
        )
        if history_result.rowcount != 1:  # type: ignore[attr-defined]
            await self._refresh_after_claim_loss(session, acquisition, history)
            return False
        await session.commit()
        await session.refresh(acquisition)
        await session.refresh(history)
        return True

    async def _record_retry_if_current(
        self,
        session: AsyncSession,
        acquisition: AirDcppAcquisition,
        history: DownloadHistory,
        *,
        now: datetime,
        mutation_error: str | None,
    ) -> bool:
        next_retry_at = now + timedelta(seconds=30)
        acquisition_result = await session.execute(
            update(AirDcppAcquisition)
            .where(
                AirDcppAcquisition.id == acquisition.id,
                AirDcppAcquisition.bundle_id.is_(None),
                AirDcppAcquisition.client_state == "mutation_pending",
                AirDcppAcquisition.retry_count == 0,
            )
            .values(
                client_state="reconcile_pending",
                retry_count=1,
                next_retry_at=next_retry_at,
                reconciliation_error=mutation_error or "queue_outcome_ambiguous",
            )
            .execution_options(synchronize_session=False)
        )
        if acquisition_result.rowcount != 1:  # type: ignore[attr-defined]
            await self._refresh_after_claim_loss(session, acquisition, history)
            return False
        history_result = await session.execute(
            update(DownloadHistory)
            .where(
                DownloadHistory.id == history.id,
                DownloadHistory.state == DownloadState.QUEUED,
            )
            .values(
                state=DownloadState.RETRY_PENDING,
                next_retry_at=next_retry_at,
                error_message="Waiting to reconcile the AirDC++ queue request.",
            )
            .execution_options(synchronize_session=False)
        )
        if history_result.rowcount != 1:  # type: ignore[attr-defined]
            await self._refresh_after_claim_loss(session, acquisition, history)
            return False
        await session.commit()
        await session.refresh(acquisition)
        await session.refresh(history)
        return True

    async def _refresh_after_claim_loss(
        self,
        session: AsyncSession,
        acquisition: AirDcppAcquisition,
        history: DownloadHistory,
    ) -> None:
        await session.rollback()
        await session.refresh(acquisition)
        await session.refresh(history)
        await session.commit()

    async def _remove_superseded_bundle(
        self,
        api_client: AirDcppQueueApi,
        acquisition: AirDcppAcquisition,
        bundle_id: int,
    ) -> None:
        if acquisition.bundle_id == bundle_id:
            return
        try:
            await api_client.remove_queue_bundle(bundle_id)
        except AirDcppEntityNotFoundError:
            return
        except AirDcppError as exc:
            logger.warning(
                "airdcpp_superseded_acquisition_cleanup_failed",
                acquisition_id=acquisition.id,
                bundle_id=bundle_id,
                error_code=exc.code,
            )


def _safe_target_name(value: str) -> str:
    normalized = value.replace("\\", "/")
    name = PurePath(normalized).name.strip().strip(".")
    name = "".join(character for character in name if ord(character) >= 32)
    if (
        not name
        or len(name) > 255
        or PurePath(name).suffix.casefold()
        not in {
            ".cbz",
            ".cbr",
            ".pdf",
        }
    ):
        raise ValueError("Direct Connect result has an unsafe target name")
    return name


def _result(
    acquisition: AirDcppAcquisition,
    history: DownloadHistory,
    *,
    merged: bool | None,
) -> AirDcppQueueAcquisitionResult:
    return AirDcppQueueAcquisitionResult(
        acquisition_id=acquisition.id,
        download_history_id=history.id,
        bundle_id=acquisition.bundle_id,
        merged=merged,
        state=DownloadState(history.state),
    )
