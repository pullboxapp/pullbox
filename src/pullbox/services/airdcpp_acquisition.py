"""Persist Direct Connect queue intent before bounded external mutation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePath
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import select

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
            await session.commit()
            if existing.bundle_id is None:
                adopted = await self._find_adoptable(api_client, existing, history.title)
                if adopted is not None:
                    await self._record_bundle(
                        session,
                        existing,
                        history,
                        adopted.bundle_id,
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
        bundle_id: int | None
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
        except AirDcppUnavailableError:
            adopted, recovery_error = await self._try_find_adoptable(
                api_client,
                acquisition,
                target_name,
            )
            bundle_id = adopted.bundle_id if adopted is not None else None
            if adopted is not None:
                acquisition.remote_target = adopted.target.get_secret_value()
            elif recovery_error is not None:
                acquisition.reconciliation_error = recovery_error
        except AirDcppEntityNotFoundError:
            try:
                added = await api_client.create_file_bundle(
                    tth=route.tth,
                    size=route.size_bytes,
                    target_name=target_name,
                    priority=queue_priority,
                )
            except AirDcppError as exc:
                bundle_id = None
                acquisition.reconciliation_error = exc.code
            else:
                bundle_id = added.id
                merged = added.merged
                source_search_pending = True
        except AirDcppError as exc:
            bundle_id = None
            acquisition.reconciliation_error = exc.code

        if bundle_id is not None:
            await self._record_bundle(
                session,
                acquisition,
                history,
                bundle_id,
                client_state=("source_search_pending" if source_search_pending else "queued"),
                next_retry_at=(now if source_search_pending else None),
            )
        else:
            acquisition.client_state = "reconcile_pending"
            acquisition.retry_count = min(acquisition.retry_count + 1, acquisition.max_retries)
            acquisition.next_retry_at = now + timedelta(seconds=30)
            if acquisition.reconciliation_error is None:
                acquisition.reconciliation_error = "queue_outcome_ambiguous"
            history.state = DownloadState.RETRY_PENDING
            history.next_retry_at = acquisition.next_retry_at
            history.error_message = "Waiting to reconcile the AirDC++ queue request."
            await session.commit()
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

    async def _record_bundle(
        self,
        session: AsyncSession,
        acquisition: AirDcppAcquisition,
        history: DownloadHistory,
        bundle_id: int,
        *,
        remote_target: str | None = None,
        client_state: str = "queued",
        next_retry_at: datetime | None = None,
    ) -> None:
        acquisition.bundle_id = bundle_id
        acquisition.client_state = client_state
        acquisition.remote_target = remote_target or acquisition.remote_target
        acquisition.next_retry_at = next_retry_at
        acquisition.reconciliation_error = None
        acquisition.last_reconciled_at = datetime.now(UTC)
        history.external_id = f"airdcpp:{acquisition.client_config_id}:bundle:{bundle_id}"
        history.state = DownloadState.SENT
        history.next_retry_at = None
        history.error_message = None
        await session.commit()


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
