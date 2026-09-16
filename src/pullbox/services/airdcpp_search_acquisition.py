"""Server-owned AirDC++ search handoff and durable review routes."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import TypeAdapter
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from pullbox.composition import airdcpp
from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.core.encryption import decrypt_secret, encrypt_secret, is_encrypted
from pullbox.core.exceptions import ProviderError
from pullbox.core.release_parser import parse_release_title
from pullbox.models.client import DownloadClientConfig
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile, MatchConfidence
from pullbox.providers.airdcpp.supervisor import AirDcppSupervisorState
from pullbox.providers.base import ReleaseResult
from pullbox.services.airdcpp_acquisition import AirDcppQueueAcquisitionService
from pullbox.services.airdcpp_search_types import DcMetrics, DcRoute, DcValidatedCandidate
from pullbox.services.blocklist_service import BlocklistService
from pullbox.services.release_validator import ValidationResult

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.pending_match import PendingMatch
    from pullbox.providers.airdcpp.supervisor import RegistrySupervisor

_ROUTE_ADAPTER = TypeAdapter(DcRoute)
_ACTIVE_STATES = (
    DownloadState.QUEUED,
    DownloadState.SENT,
    DownloadState.DOWNLOADING,
    DownloadState.FINALIZING,
    DownloadState.PAUSED,
    DownloadState.RETRY_PENDING,
    DownloadState.POST_PROCESSING,
    DownloadState.COMPLETED,
)


class DcIssueAlreadyOwnedError(ProviderError):
    """Stop automatic fallback rather than replacing an already-owned issue."""


async def ready_dc_client(
    session: AsyncSession,
    candidate: DcValidatedCandidate,
    *,
    automatic: bool,
) -> tuple[DownloadClientConfig, RegistrySupervisor]:
    """Recheck exact client ownership and opt-in after potentially slow search."""
    client = await session.scalar(
        select(DownloadClientConfig)
        .options(selectinload(DownloadClientConfig.airdcpp_settings))
        .where(
            DownloadClientConfig.id == candidate.route.client_config_id,
            DownloadClientConfig.client_type == DownloadClientType.AIRDCPP,
            DownloadClientConfig.enabled.is_(True),
        )
        .execution_options(populate_existing=True)
    )
    if (
        client is None
        or client.airdcpp_settings is None
        or not client.airdcpp_settings.search_enabled
        or (automatic and not client.airdcpp_settings.automatic_search_enabled)
    ):
        raise ProviderError(
            "airdcpp", "The selected AirDC++ client is not enabled for this search."
        )
    if automatic and not await BlocklistService.filter_results(session, [candidate.release]):
        raise ProviderError("airdcpp", "The selected AirDC++ result is blocklisted.")
    registry = airdcpp.get_airdcpp_supervisor_registry()
    supervisor = registry.get(client.id) if registry else None
    if supervisor is None or supervisor.state is not AirDcppSupervisorState.READY:
        raise ProviderError("airdcpp", "The selected AirDC++ client is not ready.")
    return client, supervisor


async def acquire_dc_candidate(
    session: AsyncSession,
    *,
    candidate: DcValidatedCandidate,
    issue_id: int,
    search_log_id: int | None,
    request_key: str,
    automatic: bool,
) -> tuple[DownloadHistory, bool]:
    """Reuse durable queue intent; never create a second active issue download."""
    await session.commit()
    # Serialize the eligibility check and durable intent across workers. The
    # acquisition service commits this short transaction before remote mutation.
    await session.execute(update(Issue).where(Issue.id == issue_id).values(status=Issue.status))
    client, supervisor = await ready_dc_client(session, candidate, automatic=automatic)
    if await session.scalar(select(LibraryFile.id).where(LibraryFile.issue_id == issue_id)):
        await session.commit()
        raise DcIssueAlreadyOwnedError(
            "airdcpp", "This issue already has a library file; use Find Alternative."
        )
    existing = await session.scalar(
        select(DownloadHistory)
        .where(
            DownloadHistory.issue_id == issue_id,
            DownloadHistory.state.in_(_ACTIVE_STATES),
            DownloadHistory.imported_at.is_(None),
        )
        .order_by(DownloadHistory.id.desc())
        .limit(1)
    )
    if existing is not None:
        await session.commit()
        return existing, False
    assert client.airdcpp_settings is not None
    result = await AirDcppQueueAcquisitionService().acquire(
        session,
        candidate=candidate,
        issue_id=issue_id,
        request_key=request_key,
        search_log_id=search_log_id,
        api_client=supervisor.api_client,
        queue_priority=client.airdcpp_settings.queue_priority,
        replace_existing_file=False,
    )
    history = await session.get(DownloadHistory, result.download_history_id)
    assert history is not None
    return history, True


def dc_review_snapshot(
    candidate: DcValidatedCandidate, *, issue_id: int, search_log_id: int
) -> str:
    """Keep hub/search route details opaque outside the server, including after restart."""
    return encrypt_secret(
        json.dumps(
            {
                "version": 1,
                "issue_id": issue_id,
                "search_log_id": search_log_id,
                "route": _ROUTE_ADAPTER.dump_python(candidate.route, mode="json"),
            }
        )
    )


def dc_review_candidate(pending: PendingMatch) -> tuple[DcValidatedCandidate, int | None]:
    """Rehydrate only a server-issued route bound to this pending issue."""
    snapshot = pending.match_details.get("dc_route_snapshot")
    if not isinstance(snapshot, str) or not is_encrypted(snapshot):
        raise ValueError("The Direct Connect review route is unavailable; search again.")
    data = json.loads(decrypt_secret(snapshot))
    if data.get("version") != 1 or data.get("issue_id") != pending.issue_id:
        raise ValueError("The Direct Connect review route belongs to another issue.")
    route = _ROUTE_ADAPTER.validate_python(data["route"])
    if route.size_bytes != pending.file_size:
        raise ValueError("The Direct Connect review file size does not match its route.")
    release = ReleaseResult(
        title=pending.release_title,
        indexer_name=str(pending.match_details.get("indexer_name", "AirDC++")),
        download_url=pending.download_url,
        size_bytes=route.size_bytes,
        age_days=None,
        seeders=None,
        leechers=None,
        grabs=None,
        is_torrent=False,
        category=None,
        published_at=None,
        protocol=AcquisitionProtocol.DC,
    )
    parsed = parse_release_title(release.title)
    if parsed is None:
        raise ValueError("The Direct Connect review title is invalid; search again.")
    validation = ValidationResult(
        is_match=True,
        confidence=MatchConfidence(pending.confidence),
        parsed=parsed,
        release=release,
    )
    return DcValidatedCandidate(release, validation, route, DcMetrics(0, 0, 0, None)), data.get(
        "search_log_id"
    )
