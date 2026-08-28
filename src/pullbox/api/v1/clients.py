"""Download client API routes — CRUD and test connection."""

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime

import httpx
import structlog
from fastapi import APIRouter
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import selectinload

from pullbox.api.deps import DbSession, InteractiveOperatorUser
from pullbox.composition.airdcpp import refresh_airdcpp_supervisor_registry_from_session
from pullbox.config import get_settings
from pullbox.core.encryption import decrypt_secret, encrypt_secret
from pullbox.core.exceptions import NotFoundError, ProviderError, ValidationError
from pullbox.core.sqlite_lock import (
    SQLITE_LOCK_RETRY_ATTEMPTS,
    is_sqlite_locked_error,
    sqlite_lock_retry_delay,
)
from pullbox.core.url_validation import normalize_peer_base_url
from pullbox.models.airdcpp import AirDcppClientSettings
from pullbox.models.client import DownloadClientConfig
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.providers.airdcpp.api_client import AirDcppApiClient
from pullbox.schemas.airdcpp import AirDcppSettingsResponse
from pullbox.schemas.client import (
    ClientCreate,
    ClientResponse,
    ClientUpdate,
    is_absolute_client_path,
)
from pullbox.services.airdcpp_configuration_service import (
    AirDcppConfigurationService,
    AirDcppConnectionTestResult,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/clients", tags=["clients"], include_in_schema=False)


# ── Helpers ───────────────────────────────────────────────────────────


def _redact_client(client: DownloadClientConfig) -> dict[str, object]:
    """Build response dict with secrets redacted."""
    return {
        "id": client.id,
        "name": client.name,
        "client_type": client.client_type,
        "url": client.url,
        "enabled": client.enabled,
        "priority": client.priority,
        "has_api_key": client.api_key is not None and len(client.api_key) > 0,
        "username": client.username,
        "has_password": client.password is not None and len(client.password) > 0,
        "category": client.category,
        "download_dir": client.download_dir,
        "remote_path": client.remote_path,
        # SABnzbd
        "sab_priority": client.sab_priority,
        "sab_post_processing": client.sab_post_processing,
        # qBittorrent
        "qbt_content_layout": client.qbt_content_layout,
        "qbt_ratio_limit": client.qbt_ratio_limit,
        "qbt_seeding_time_limit": client.qbt_seeding_time_limit,
        # NZBGet
        "nzbget_priority": client.nzbget_priority,
        "nzbget_post_processing": client.nzbget_post_processing,
        # Transmission
        "transmission_bandwidth_priority": client.transmission_bandwidth_priority,
        "transmission_seed_ratio_limit": client.transmission_seed_ratio_limit,
        "transmission_seed_idle_limit": client.transmission_seed_idle_limit,
        # Deluge
        "deluge_label": client.deluge_label,
        "deluge_max_ratio": client.deluge_max_ratio,
        "deluge_move_completed_path": client.deluge_move_completed_path,
        # AirDC++
        "airdcpp": (
            AirDcppSettingsResponse.model_validate(client.airdcpp_settings)
            if client.client_type is DownloadClientType.AIRDCPP
            and client.airdcpp_settings is not None
            else None
        ),
        # Health
        "last_success_at": client.last_success_at,
        "last_failure_at": client.last_failure_at,
        "last_error": client.last_error,
        "last_test_message": client.last_test_message,
        "created_at": client.created_at,
        "updated_at": client.updated_at,
    }


def _validate_airdcpp_configuration(
    *,
    username: str | None,
    password: str | None,
    remote_path: str | None,
    download_dir: str | None,
    category: str | None,
) -> None:
    """Enforce AirDC++-only fields after create/update values are combined."""
    if not username or not username.strip():
        raise ValidationError("Username is required for an AirDC++ client.")
    if not password:
        raise ValidationError("Password is required for an AirDC++ client.")
    if not is_absolute_client_path(remote_path):
        raise ValidationError("Remote path must be absolute for an AirDC++ client.")
    if not is_absolute_client_path(download_dir):
        raise ValidationError("Download directory must be absolute for an AirDC++ client.")
    if category:
        raise ValidationError("Category is not used by an AirDC++ client.")


async def _run_airdcpp_connection_test(
    *,
    url: str,
    username: str,
    password: str,
    minimum_search_interval_seconds: int,
    request_timeout_seconds: int,
    remote_path: str | None,
    download_dir: str | None,
) -> AirDcppConnectionTestResult:
    """Construct the bounded REST client and run the read-only test policy."""
    service = AirDcppConfigurationService(
        lambda: AirDcppApiClient(
            base_url=url,
            username=username,
            password=password,
            timeout_seconds=request_timeout_seconds,
        )
    )
    return await service.test_connection(
        configured_minimum_search_interval_seconds=minimum_search_interval_seconds,
        remote_path=remote_path,
        download_dir=download_dir,
    )


async def _persist_client_test_status_with_retry(
    session: DbSession,
    client_id: int,
    *,
    healthy: bool,
    message: str,
    checked_at: datetime,
) -> None:
    """Persist client test status, retrying transient SQLite write locks."""
    for attempt in range(1, SQLITE_LOCK_RETRY_ATTEMPTS + 1):
        client = await session.get(DownloadClientConfig, client_id)
        if not client:
            raise NotFoundError("Download client", client_id)

        client.last_test_message = message
        if healthy:
            client.last_success_at = checked_at
            client.last_error = None
        else:
            client.last_failure_at = checked_at
            client.last_error = message

        try:
            await session.flush()
            return
        except OperationalError as exc:
            await session.rollback()
            if not is_sqlite_locked_error(exc) or attempt == SQLITE_LOCK_RETRY_ATTEMPTS:
                raise
            delay_seconds = sqlite_lock_retry_delay(attempt)
            logger.warning(
                "client_test_status_persist_retrying_after_sqlite_lock",
                client_id=client_id,
                attempt=attempt,
                max_attempts=SQLITE_LOCK_RETRY_ATTEMPTS,
                delay_seconds=delay_seconds,
            )
            await asyncio.sleep(delay_seconds)


def _decryption_failure_response() -> dict[str, object]:
    """Return a failed test result for unreadable stored credentials."""
    return {
        "healthy": False,
        "message": (
            "Saved credentials could not be decrypted. Re-enter and save this "
            "client configuration, then test again."
        ),
        "response_time_ms": 0.0,
    }


# ── List ─────────────────────────────────────────────────────────────


@router.get("", response_model=list[ClientResponse])
async def list_clients(
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> list[ClientResponse]:
    """List all configured download clients."""
    result = await session.execute(
        select(DownloadClientConfig)
        .options(selectinload(DownloadClientConfig.airdcpp_settings))
        .order_by(DownloadClientConfig.priority, DownloadClientConfig.name)
    )
    clients = result.scalars().all()
    return [ClientResponse.model_validate(_redact_client(c)) for c in clients]


# ── Get single ──────────────────────────────────────────────────────


@router.get("/{client_id}", response_model=ClientResponse)
async def get_client(
    client_id: int,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> ClientResponse:
    """Get a single download client configuration."""
    client = await session.get(
        DownloadClientConfig,
        client_id,
        options=(selectinload(DownloadClientConfig.airdcpp_settings),),
    )
    if not client:
        raise NotFoundError("DownloadClient", client_id)
    return ClientResponse.model_validate(_redact_client(client))


# ── Create ───────────────────────────────────────────────────────────


@router.post("", response_model=ClientResponse, status_code=201)
async def add_client(
    body: ClientCreate,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> ClientResponse:
    """Add a new download client configuration."""
    if body.client_type is not DownloadClientType.AIRDCPP:
        existing = await session.execute(
            select(DownloadClientConfig).where(DownloadClientConfig.client_type == body.client_type)
        )
        if existing.scalar_one_or_none():
            raise ValidationError(
                f"A {body.client_type.value} client is already configured. "
                "Only one instance per client type is allowed."
            )

    if body.client_type is DownloadClientType.AIRDCPP:
        if not get_settings().airdcpp_enabled:
            raise ValidationError("AirDC++ integration is disabled by the feature flag.")
        _validate_airdcpp_configuration(
            username=body.username,
            password=body.password,
            remote_path=body.remote_path,
            download_dir=body.download_dir,
            category=body.category,
        )

    client = DownloadClientConfig(
        name=body.name,
        client_type=body.client_type,
        url=body.url,
        enabled=body.enabled,
        priority=body.priority,
        api_key=encrypt_secret(body.api_key) if body.api_key else body.api_key,
        username=body.username,
        password=encrypt_secret(body.password) if body.password else body.password,
        category=body.category,
        download_dir=body.download_dir,
        remote_path=body.remote_path,
        sab_priority=body.sab_priority,
        sab_post_processing=body.sab_post_processing,
        qbt_content_layout=body.qbt_content_layout,
        qbt_ratio_limit=body.qbt_ratio_limit,
        qbt_seeding_time_limit=body.qbt_seeding_time_limit,
        nzbget_priority=body.nzbget_priority,
        nzbget_post_processing=body.nzbget_post_processing,
        transmission_bandwidth_priority=body.transmission_bandwidth_priority,
        transmission_seed_ratio_limit=body.transmission_seed_ratio_limit,
        transmission_seed_idle_limit=body.transmission_seed_idle_limit,
        deluge_label=body.deluge_label,
        deluge_max_ratio=body.deluge_max_ratio,
        deluge_move_completed_path=body.deluge_move_completed_path,
    )
    if body.client_type is DownloadClientType.AIRDCPP:
        if body.airdcpp is None:  # Defensive; ClientCreate supplies product defaults.
            raise ValidationError("AirDC++ settings are required.")
        client.airdcpp_settings = AirDcppClientSettings(**body.airdcpp.model_dump())
    session.add(client)
    await session.flush()
    if body.client_type is DownloadClientType.AIRDCPP:
        await session.commit()
        await refresh_airdcpp_supervisor_registry_from_session(session)
    return ClientResponse.model_validate(_redact_client(client))


# ── Update ───────────────────────────────────────────────────────────


@router.put("/{client_id}", response_model=ClientResponse)
async def update_client(
    client_id: int,
    body: ClientUpdate,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> ClientResponse:
    """Update a download client configuration."""
    client: DownloadClientConfig | None = await session.get(
        DownloadClientConfig,
        client_id,
        options=(selectinload(DownloadClientConfig.airdcpp_settings),),
    )
    if not client:
        raise NotFoundError("DownloadClient", client_id)

    update_data = body.model_dump(exclude_unset=True)
    airdcpp_data = update_data.pop("airdcpp", None)
    if airdcpp_data is not None and client.client_type is not DownloadClientType.AIRDCPP:
        raise ValidationError("AirDC++ settings are only valid for an AirDC++ client.")

    # Encrypt secrets if provided; omitted fields keep existing encrypted value
    if update_data.get("api_key"):
        update_data["api_key"] = encrypt_secret(update_data["api_key"])
    elif "api_key" in update_data and not update_data["api_key"]:
        # Empty string or None → remove the key
        pass

    if update_data.get("password"):
        update_data["password"] = encrypt_secret(update_data["password"])
    elif "password" in update_data and not update_data["password"]:
        # Empty string submitted → keep existing (don't wipe on edit)
        del update_data["password"]

    if client.client_type is DownloadClientType.AIRDCPP:
        if "url" in update_data:
            try:
                update_data["url"] = normalize_peer_base_url(
                    str(update_data["url"]),
                    reject_query_or_fragment=True,
                )
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
        _validate_airdcpp_configuration(
            username=str(update_data.get("username", client.username) or ""),
            password=str(update_data.get("password", client.password) or ""),
            remote_path=str(update_data.get("remote_path", client.remote_path) or ""),
            download_dir=str(update_data.get("download_dir", client.download_dir) or ""),
            category=(
                str(update_data.get("category", client.category))
                if update_data.get("category", client.category)
                else None
            ),
        )

    for field, value in update_data.items():
        setattr(client, field, value)

    if airdcpp_data is not None:
        if client.airdcpp_settings is None:
            client.airdcpp_settings = AirDcppClientSettings(**airdcpp_data)
        else:
            for field, value in airdcpp_data.items():
                setattr(client.airdcpp_settings, field, value)

    await session.flush()
    refreshed = await session.execute(
        select(DownloadClientConfig)
        .where(DownloadClientConfig.id == client_id)
        .options(selectinload(DownloadClientConfig.airdcpp_settings))
        .execution_options(populate_existing=True)
    )
    client = refreshed.scalar_one()
    if client.client_type is DownloadClientType.AIRDCPP:
        await session.commit()
        await refresh_airdcpp_supervisor_registry_from_session(session)
    return ClientResponse.model_validate(_redact_client(client))


# ── Delete ───────────────────────────────────────────────────────────


@router.delete("/{client_id}", status_code=204)
async def delete_client(
    client_id: int,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> None:
    """Remove a download client configuration."""
    client = await session.get(DownloadClientConfig, client_id)
    if not client:
        raise NotFoundError("DownloadClient", client_id)
    if client.client_type is DownloadClientType.AIRDCPP:
        active_state_values = (
            DownloadState.QUEUED,
            DownloadState.SENT,
            DownloadState.DOWNLOADING,
            DownloadState.FINALIZING,
            DownloadState.PAUSED,
            DownloadState.RETRY_PENDING,
        )
        active_id = await session.scalar(
            select(DownloadHistory.id)
            .where(
                DownloadHistory.download_client_config_id == client_id,
                or_(
                    DownloadHistory.state.in_(active_state_values),
                    and_(
                        DownloadHistory.state == DownloadState.COMPLETED,
                        DownloadHistory.imported_at.is_(None),
                    ),
                ),
            )
            .limit(1)
        )
        if active_id is not None:
            raise ValidationError(
                "AirDC++ client cannot be deleted while it has active acquisitions. "
                "Disable it and wait for active work to reach a terminal state."
            )
    await session.delete(client)
    if client.client_type is DownloadClientType.AIRDCPP:
        await session.commit()
        await refresh_airdcpp_supervisor_registry_from_session(session)


# ── Test Connection ──────────────────────────────────────────────────


@router.post("/test", status_code=200)
async def test_client_inline(
    body: ClientCreate,
    _user: InteractiveOperatorUser,
    session: DbSession,
    existing_id: int | None = None,
) -> dict[str, object]:
    """Test connectivity using form values.

    When editing an existing client and credentials are left blank,
    pass ``existing_id`` to fill in stored (encrypted) credentials.
    """
    from pullbox.providers.download.deluge import DelugeClient
    from pullbox.providers.download.nzbget import NZBGetClient
    from pullbox.providers.download.qbittorrent import QBittorrentClient
    from pullbox.providers.download.sabnzbd import SABnzbdClient
    from pullbox.providers.download.transmission import TransmissionClient

    # For edit mode: fill blank credentials from stored config
    api_key = body.api_key or ""
    username = body.username or ""
    password = body.password or ""

    if existing_id and (not api_key or not password):
        saved = await session.get(DownloadClientConfig, existing_id)
        if saved:
            if saved.client_type is not body.client_type:
                raise ValidationError("Existing client type does not match the test request.")
            try:
                if not api_key and saved.api_key:
                    api_key = decrypt_secret(saved.api_key)
                if not username and saved.username:
                    username = saved.username
                if not password and saved.password:
                    password = decrypt_secret(saved.password)
            except ValueError as exc:
                message = str(_decryption_failure_response()["message"])
                saved.last_failure_at = datetime.now(UTC)
                saved.last_error = message
                saved.last_test_message = message
                await session.flush()
                logger.warning(
                    "client_test_decrypt_failed",
                    client_id=existing_id,
                    error=str(exc),
                )
                return _decryption_failure_response()

    if body.client_type is DownloadClientType.AIRDCPP:
        if not get_settings().airdcpp_enabled:
            raise ValidationError("AirDC++ integration is disabled by the feature flag.")
        _validate_airdcpp_configuration(
            username=username,
            password=password,
            remote_path=body.remote_path,
            download_dir=body.download_dir,
            category=body.category,
        )
        if body.airdcpp is None:
            raise ValidationError("AirDC++ settings are required.")
        # An edit-mode credential lookup may have opened a read transaction.
        # End it before the bounded network call; no DB transaction may span
        # AirDC++ I/O.
        await session.rollback()
        air_result = await _run_airdcpp_connection_test(
            url=body.url,
            username=username,
            password=password,
            minimum_search_interval_seconds=(body.airdcpp.minimum_search_interval_seconds),
            request_timeout_seconds=body.airdcpp.request_timeout_seconds,
            remote_path=body.remote_path,
            download_dir=body.download_dir,
        )
        return asdict(air_result)

    dl_provider: (
        SABnzbdClient | NZBGetClient | QBittorrentClient | TransmissionClient | DelugeClient
    )
    if body.client_type is DownloadClientType.SABNZBD:
        dl_provider = SABnzbdClient(
            url=body.url,
            api_key=api_key,
            category=body.category,
        )
    elif body.client_type is DownloadClientType.NZBGET:
        dl_provider = NZBGetClient(
            url=body.url,
            username=username or "nzbget",
            password=password,
            category=body.category,
        )
    elif body.client_type is DownloadClientType.QBITTORRENT:
        dl_provider = QBittorrentClient(
            url=body.url,
            username=username,
            password=password,
            category=body.category,
        )
    elif body.client_type is DownloadClientType.TRANSMISSION:
        dl_provider = TransmissionClient(
            url=body.url,
            username=username,
            password=password,
        )
    elif body.client_type is DownloadClientType.DELUGE:
        dl_provider = DelugeClient(
            url=body.url,
            password=password,
        )
    else:
        raise ProviderError("download", f"Unknown client type: {body.client_type}")

    # Use a shorter timeout for test connections (5s instead of default 10s)
    if hasattr(dl_provider, "_client") and isinstance(dl_provider._client, httpx.AsyncClient):
        dl_provider._client.timeout = httpx.Timeout(5.0, connect=2.0)

    dl_result = await dl_provider.test_connection()

    return {
        "healthy": dl_result.healthy,
        "message": dl_result.message,
        "response_time_ms": dl_result.response_time_ms,
    }


@router.post("/{client_id}/test", status_code=200)
async def test_client(
    client_id: int,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> dict[str, object]:
    """Test connectivity to a download client."""
    client = await session.get(
        DownloadClientConfig,
        client_id,
        options=(selectinload(DownloadClientConfig.airdcpp_settings),),
    )
    if not client:
        raise NotFoundError("DownloadClient", client_id)

    if client.client_type is DownloadClientType.AIRDCPP:
        if not get_settings().airdcpp_enabled:
            raise ValidationError("AirDC++ integration is disabled by the feature flag.")
        settings = client.airdcpp_settings
        if settings is None:
            raise ValidationError("AirDC++ settings are missing for this client.")
        try:
            password = decrypt_secret(str(client.password or ""))
        except ValueError as exc:
            message = str(_decryption_failure_response()["message"])
            await _persist_client_test_status_with_retry(
                session,
                client_id,
                healthy=False,
                message=message,
                checked_at=datetime.now(UTC),
            )
            logger.warning("client_test_decrypt_failed", client_id=client_id, error=str(exc))
            return _decryption_failure_response()
        _validate_airdcpp_configuration(
            username=client.username,
            password=password,
            remote_path=client.remote_path,
            download_dir=client.download_dir,
            category=client.category,
        )
        client_url = str(client.url)
        username = str(client.username)
        minimum_search_interval_seconds = settings.minimum_search_interval_seconds
        request_timeout_seconds = settings.request_timeout_seconds
        remote_path = client.remote_path
        download_dir = client.download_dir
        # Copy all configuration above, then release the read transaction before
        # contacting AirDC++. Status persistence starts a fresh transaction.
        await session.rollback()
        air_result = await _run_airdcpp_connection_test(
            url=client_url,
            username=username,
            password=password,
            minimum_search_interval_seconds=minimum_search_interval_seconds,
            request_timeout_seconds=request_timeout_seconds,
            remote_path=remote_path,
            download_dir=download_dir,
        )
        await _persist_client_test_status_with_retry(
            session,
            client_id,
            healthy=air_result.healthy,
            message=air_result.message,
            checked_at=datetime.now(UTC),
        )
        logger.info(
            "client_test_connection",
            client_id=client_id,
            healthy=air_result.healthy,
            response_time_ms=air_result.response_time_ms,
        )
        return asdict(air_result)

    from pullbox.providers.download.deluge import DelugeClient
    from pullbox.providers.download.nzbget import NZBGetClient
    from pullbox.providers.download.qbittorrent import QBittorrentClient
    from pullbox.providers.download.sabnzbd import SABnzbdClient
    from pullbox.providers.download.transmission import TransmissionClient

    url = str(client.url)
    category = str(client.category) if client.category else None

    dl_provider: (
        SABnzbdClient | NZBGetClient | QBittorrentClient | TransmissionClient | DelugeClient
    )
    try:
        if client.client_type is DownloadClientType.SABNZBD:
            dl_provider = SABnzbdClient(
                url=url,
                api_key=decrypt_secret(str(client.api_key or "")),
                category=category,
                priority=client.sab_priority,
                post_processing=client.sab_post_processing,
            )
        elif client.client_type is DownloadClientType.NZBGET:
            dl_provider = NZBGetClient(
                url=url,
                username=str(client.username or "nzbget"),
                password=decrypt_secret(str(client.password or "")),
                category=category,
                priority=client.nzbget_priority,
                post_processing=client.nzbget_post_processing,
            )
        elif client.client_type is DownloadClientType.QBITTORRENT:
            dl_provider = QBittorrentClient(
                url=url,
                username=str(client.username or ""),
                password=decrypt_secret(str(client.password or "")),
                category=category,
                content_layout=client.qbt_content_layout,
                ratio_limit=client.qbt_ratio_limit,
                seeding_time_limit=client.qbt_seeding_time_limit,
            )
        elif client.client_type is DownloadClientType.TRANSMISSION:
            dl_provider = TransmissionClient(
                url=url,
                username=str(client.username or ""),
                password=decrypt_secret(str(client.password or "")),
                download_dir=client.transmission_download_dir,
                bandwidth_priority=client.transmission_bandwidth_priority,
                seed_ratio_limit=client.transmission_seed_ratio_limit,
                seed_idle_limit=client.transmission_seed_idle_limit,
            )
        elif client.client_type is DownloadClientType.DELUGE:
            dl_provider = DelugeClient(
                url=url,
                password=decrypt_secret(str(client.password or "")),
                label=client.deluge_label,
                max_ratio=client.deluge_max_ratio,
                move_completed_path=client.deluge_move_completed_path,
            )
        else:
            raise ProviderError("download", f"Unknown client type: {client.client_type}")
    except ValueError as exc:
        message = str(_decryption_failure_response()["message"])
        await _persist_client_test_status_with_retry(
            session,
            client_id,
            healthy=False,
            message=message,
            checked_at=datetime.now(UTC),
        )
        logger.warning("client_test_decrypt_failed", client_id=client_id, error=str(exc))
        return _decryption_failure_response()

    # Shorter timeout for test connections
    if hasattr(dl_provider, "_client") and isinstance(dl_provider._client, httpx.AsyncClient):
        dl_provider._client.timeout = httpx.Timeout(5.0, connect=2.0)

    dl_result = await dl_provider.test_connection()
    checked_at = datetime.now(UTC)
    await _persist_client_test_status_with_retry(
        session,
        client_id,
        healthy=dl_result.healthy,
        message=dl_result.message,
        checked_at=checked_at,
    )

    logger.info(
        "client_test_connection",
        client_id=client_id,
        healthy=dl_result.healthy,
        response_time_ms=dl_result.response_time_ms,
    )
    return {
        "healthy": dl_result.healthy,
        "message": dl_result.message,
        "response_time_ms": dl_result.response_time_ms,
    }
