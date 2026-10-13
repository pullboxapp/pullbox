"""Indexer API routes — CRUD, test connection, Prowlarr sync, and priority management."""

import asyncio
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from pullbox.api.deps import DbSession, InteractiveOperatorUser
from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.core.encryption import decrypt_secret, encrypt_secret
from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.core.sqlite_lock import (
    SQLITE_LOCK_RETRY_ATTEMPTS,
    is_sqlite_locked_error,
    sqlite_lock_retry_delay,
)
from pullbox.models.client import DownloadClientConfig
from pullbox.models.download import DownloadClientType
from pullbox.models.indexer import IndexerConfig, IndexerType
from pullbox.schemas.indexer import (
    IndexerCreate,
    IndexerResponse,
    IndexerUpdate,
    JackettSyncRequest,
    JackettSyncResult,
    ProwlarrSyncRequest,
    ProwlarrSyncResult,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/indexers", tags=["indexers"], include_in_schema=False)


# ── Helpers ───────────────────────────────────────────────────────────


def _redact_indexer(indexer: IndexerConfig) -> dict[str, object]:
    """Build response dict with api_key redacted."""
    return {
        "id": indexer.id,
        "name": indexer.name,
        "indexer_type": indexer.indexer_type,
        "url": indexer.url,
        "has_api_key": bool(indexer.api_key),
        "enabled": indexer.enabled,
        "priority": indexer.priority,
        "categories": indexer.categories,
        "source": indexer.source,
        "prowlarr_indexer_id": indexer.prowlarr_indexer_id,
        "manager_indexer_id": indexer.manager_indexer_id,
        "manager_available": indexer.manager_available,
        "enable_rss": indexer.enable_rss,
        "enable_automatic_search": indexer.enable_automatic_search,
        "enable_interactive_search": indexer.enable_interactive_search,
        "resolver_enabled": indexer.resolver_enabled,
        "download_client_id": indexer.download_client_id,
        "last_success_at": indexer.last_success_at,
        "last_failure_at": indexer.last_failure_at,
        "last_error": indexer.last_error,
        "failure_count": indexer.failure_count,
        "disabled_until": indexer.disabled_until,
        "created_at": indexer.created_at,
        "updated_at": indexer.updated_at,
    }


async def _persist_indexer_test_status_with_retry(
    session: DbSession,
    indexer_id: int,
    *,
    healthy: bool,
    message: str,
    checked_at: datetime,
) -> None:
    """Persist indexer test status, retrying transient SQLite write locks."""
    for attempt in range(1, SQLITE_LOCK_RETRY_ATTEMPTS + 1):
        indexer = await session.get(IndexerConfig, indexer_id)
        if not indexer:
            raise NotFoundError("Indexer", indexer_id)

        if healthy:
            indexer.last_success_at = checked_at
            indexer.last_error = None
            indexer.failure_count = 0
            indexer.disabled_until = None
        else:
            indexer.last_failure_at = checked_at
            indexer.last_error = message

        try:
            await session.flush()
            return
        except OperationalError as exc:
            await session.rollback()
            if not is_sqlite_locked_error(exc) or attempt == SQLITE_LOCK_RETRY_ATTEMPTS:
                raise
            delay_seconds = sqlite_lock_retry_delay(attempt)
            logger.warning(
                "indexer_test_status_persist_retrying_after_sqlite_lock",
                indexer_id=indexer_id,
                attempt=attempt,
                max_attempts=SQLITE_LOCK_RETRY_ATTEMPTS,
                delay_seconds=delay_seconds,
            )
            await asyncio.sleep(delay_seconds)


# ── List ─────────────────────────────────────────────────────────────


@router.get("", response_model=list[IndexerResponse])
async def list_indexers(
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> list[IndexerResponse]:
    """List all configured indexers."""
    result = await session.execute(
        select(IndexerConfig).order_by(IndexerConfig.priority, IndexerConfig.name)
    )
    indexers = result.scalars().all()
    return [IndexerResponse.model_validate(_redact_indexer(i)) for i in indexers]


# ── Create ───────────────────────────────────────────────────────────


@router.post("", response_model=IndexerResponse, status_code=201)
async def add_indexer(
    body: IndexerCreate,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> IndexerResponse:
    """Add a new indexer configuration."""
    _validate_resolver_scope(
        indexer_type=body.indexer_type,
        source="manual",
        resolver_enabled=body.resolver_enabled,
    )
    await _validate_download_client(session, body.indexer_type, body.download_client_id)
    indexer = IndexerConfig(
        name=body.name,
        indexer_type=body.indexer_type,
        url=body.url,
        api_key=encrypt_secret(body.api_key),
        enabled=body.enabled,
        priority=body.priority,
        categories=body.categories,
        source="manual",
        enable_rss=body.enable_rss,
        enable_automatic_search=body.enable_automatic_search,
        enable_interactive_search=body.enable_interactive_search,
        resolver_enabled=body.resolver_enabled,
        download_client_id=body.download_client_id,
    )
    session.add(indexer)
    await session.flush()
    return IndexerResponse.model_validate(_redact_indexer(indexer))


# ══════════════════════════════════════════════════════════════════════
# Manager routes — MUST be before /{indexer_id} to avoid path conflicts
# ══════════════════════════════════════════════════════════════════════


@router.post("/prowlarr/test", status_code=200)
async def test_prowlarr_connection(
    body: ProwlarrSyncRequest,
    _user: InteractiveOperatorUser,
) -> dict[str, object]:
    """Test connectivity to a Prowlarr instance and return indexer count."""
    from pullbox.providers.indexer.prowlarr import ProwlarrIndexer

    provider = ProwlarrIndexer(url=body.prowlarr_url, api_key=body.prowlarr_api_key)
    try:
        result = await provider.test_connection()
        return {
            "healthy": result.healthy,
            "message": result.message,
            "response_time_ms": result.response_time_ms,
        }
    finally:
        await provider.close()


@router.post("/prowlarr/test-stored", status_code=200)
async def test_prowlarr_stored(
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> dict[str, object]:
    """Test Prowlarr connection using stored credentials."""
    from pullbox.models.config import SystemConfig
    from pullbox.providers.indexer.prowlarr import ProwlarrIndexer

    url_row = await session.execute(select(SystemConfig).where(SystemConfig.key == "prowlarr_url"))
    key_row = await session.execute(
        select(SystemConfig).where(SystemConfig.key == "prowlarr_api_key")
    )
    url_cfg = url_row.scalar_one_or_none()
    key_cfg = key_row.scalar_one_or_none()

    if not url_cfg or not key_cfg or not key_cfg.value:
        raise NotFoundError("Prowlarr credentials", 0)

    provider = ProwlarrIndexer(url=url_cfg.value, api_key=decrypt_secret(key_cfg.value))
    try:
        result = await provider.test_connection()
        return {
            "healthy": result.healthy,
            "message": result.message,
            "response_time_ms": result.response_time_ms,
        }
    finally:
        await provider.close()


@router.post("/prowlarr/sync", response_model=ProwlarrSyncResult)
async def sync_prowlarr_indexers(
    body: ProwlarrSyncRequest,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> ProwlarrSyncResult:
    """Sync indexers from Prowlarr.

    Pulls the indexer list from Prowlarr and creates/updates local entries.
    Each Prowlarr indexer becomes a Newznab or Torznab entry pointing at
    Prowlarr's proxy endpoint for that indexer.
    """
    from pullbox.providers.indexer.prowlarr import ProwlarrIndexer

    prowlarr_url = body.prowlarr_url.rstrip("/")
    provider = ProwlarrIndexer(url=prowlarr_url, api_key=body.prowlarr_api_key)

    try:
        remote_indexers = await provider.get_indexers()
    except Exception as exc:
        logger.exception("prowlarr_sync_failed")
        raise NotFoundError("Prowlarr", 0) from exc
    finally:
        await provider.close()

    # Encrypt the Prowlarr API key for storage
    encrypted_api_key = encrypt_secret(body.prowlarr_api_key)

    # Load existing Prowlarr-sourced indexers
    existing_result = await session.execute(
        select(IndexerConfig).where(IndexerConfig.source == "prowlarr")
    )
    existing_by_prowlarr_id: dict[int, IndexerConfig] = {
        idx.prowlarr_indexer_id: idx
        for idx in existing_result.scalars().all()
        if idx.prowlarr_indexer_id is not None
    }

    added = 0
    updated = 0
    seen_ids: set[int] = set()

    for remote in remote_indexers:
        remote_id = remote.get("id")
        if remote_id is None:
            continue
        seen_ids.add(remote_id)

        # Determine protocol: Prowlarr exposes each indexer as newznab or torznab
        protocol = remote.get("protocol", "usenet").lower()
        indexer_type = "torznab" if protocol == "torrent" else "newznab"

        # Build the proxy URL through Prowlarr
        # NewznabIndexer._request() appends /api, so we only need the base
        proxy_url = f"{prowlarr_url}/{remote_id}"

        remote_name = remote.get("name", f"Indexer {remote_id}")
        display_name = f"{remote_name} (Prowlarr)"
        remote_priority = remote.get("priority", 25)
        remote_enabled = remote.get("enable", True)

        # Extract categories
        categories_list = []
        for cap in remote.get("capabilities", {}).get("categories", []):
            cat_id = cap.get("id")
            if cat_id:
                categories_list.append(str(cat_id))
        categories_str = ",".join(categories_list) if categories_list else None

        if remote_id in existing_by_prowlarr_id:
            # Update existing
            idx = existing_by_prowlarr_id[remote_id]
            idx.name = display_name
            idx.indexer_type = indexer_type  # type: ignore[assignment]
            idx.url = proxy_url
            idx.api_key = encrypted_api_key
            idx.priority = remote_priority
            idx.enabled = remote_enabled
            idx.categories = categories_str
            idx.manager_indexer_id = str(remote_id)
            idx.manager_available = True
            idx.resolver_enabled = False
            updated += 1
        else:
            # Create new
            idx = IndexerConfig(
                name=display_name,
                indexer_type=indexer_type,
                url=proxy_url,
                api_key=encrypted_api_key,
                enabled=remote_enabled,
                priority=remote_priority,
                categories=categories_str,
                source="prowlarr",
                prowlarr_indexer_id=remote_id,
                manager_indexer_id=str(remote_id),
                manager_available=True,
                resolver_enabled=False,
            )
            session.add(idx)
            added += 1

    # Retain missing manager rows for historical attribution, but remove them
    # from active search composition until Prowlarr reports them again.
    removed = 0
    for prowlarr_id, idx in existing_by_prowlarr_id.items():
        if prowlarr_id not in seen_ids and idx.manager_available:
            idx.manager_available = False
            removed += 1

    await session.flush()

    # Save Prowlarr connection settings to SystemConfig (encrypted)
    from pullbox.models.config import SystemConfig

    for key, value, value_type in [
        ("prowlarr_url", body.prowlarr_url, "string"),
        ("prowlarr_api_key", encrypted_api_key, "secret"),
    ]:
        existing = await session.execute(select(SystemConfig).where(SystemConfig.key == key))
        cfg = existing.scalar_one_or_none()
        if cfg:
            cfg.value = value
            cfg.value_type = value_type
        else:
            session.add(SystemConfig(key=key, value=value, value_type=value_type))

    await session.flush()

    # Return updated list
    all_result = await session.execute(
        select(IndexerConfig)
        .where(
            IndexerConfig.source == "prowlarr",
            IndexerConfig.manager_available.is_(True),
        )
        .order_by(IndexerConfig.priority, IndexerConfig.name)
    )
    all_indexers = [
        IndexerResponse.model_validate(_redact_indexer(i)) for i in all_result.scalars().all()
    ]

    logger.info(
        "prowlarr_sync_complete",
        added=added,
        updated=updated,
        removed=removed,
        total=len(all_indexers),
    )

    return ProwlarrSyncResult(
        added=added,
        updated=updated,
        removed=removed,
        total=len(all_indexers),
        indexers=all_indexers,
    )


@router.post("/prowlarr/resync", response_model=ProwlarrSyncResult)
async def resync_prowlarr_indexers(
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> ProwlarrSyncResult:
    """Re-sync indexers from Prowlarr using stored credentials.

    Uses the Prowlarr URL and encrypted API key previously saved to
    SystemConfig, so the user does not need to re-enter the key.
    """
    from pullbox.models.config import SystemConfig

    prowlarr_url_row = await session.execute(
        select(SystemConfig).where(SystemConfig.key == "prowlarr_url")
    )
    prowlarr_key_row = await session.execute(
        select(SystemConfig).where(SystemConfig.key == "prowlarr_api_key")
    )
    url_cfg = prowlarr_url_row.scalar_one_or_none()
    key_cfg = prowlarr_key_row.scalar_one_or_none()

    if not url_cfg or not key_cfg or not key_cfg.value:
        raise NotFoundError("Prowlarr credentials", 0)

    # Build a ProwlarrSyncRequest with decrypted credentials
    decrypted_key = decrypt_secret(key_cfg.value)
    body = ProwlarrSyncRequest(
        prowlarr_url=url_cfg.value,
        prowlarr_api_key=decrypted_key,
    )
    return await sync_prowlarr_indexers(body, _user, session)


@router.post("/jackett/test", status_code=200)
async def test_jackett_connection(
    body: JackettSyncRequest,
    _user: InteractiveOperatorUser,
) -> dict[str, object]:
    """Test connectivity to Jackett and return its configured tracker count."""
    from pullbox.services.jackett_sync_service import test_jackett_credentials

    return await test_jackett_credentials(body.jackett_url, body.jackett_api_key)


@router.post("/jackett/test-stored", status_code=200)
async def test_jackett_stored(
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> dict[str, object]:
    """Test Jackett using the encrypted credentials already stored by Pullbox."""
    from pullbox.services.jackett_sync_service import (
        load_jackett_credentials,
        test_jackett_credentials,
    )

    url, api_key = await load_jackett_credentials(session)
    return await test_jackett_credentials(url, api_key)


@router.post("/jackett/sync", response_model=JackettSyncResult)
async def sync_jackett_indexers(
    body: JackettSyncRequest,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> JackettSyncResult:
    """Discover Jackett trackers and synchronize individual Torznab feeds."""
    from pullbox.services.jackett_sync_service import sync_jackett

    return await sync_jackett(
        session,
        url=body.jackett_url,
        api_key=body.jackett_api_key,
    )


@router.post("/jackett/resync", response_model=JackettSyncResult)
async def resync_jackett_indexers(
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> JackettSyncResult:
    """Re-sync Jackett trackers using encrypted stored credentials."""
    from pullbox.services.jackett_sync_service import load_jackett_credentials, sync_jackett

    url, api_key = await load_jackett_credentials(session)
    return await sync_jackett(session, url=url, api_key=api_key)


# ══════════════════════════════════════════════════════════════════════
# Single-indexer routes (parameterized — must come after static routes)
# ══════════════════════════════════════════════════════════════════════


# ── Get Single ───────────────────────────────────────────────────────


@router.get("/{indexer_id}", response_model=IndexerResponse)
async def get_indexer(
    indexer_id: int,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> IndexerResponse:
    """Get a single indexer by ID."""
    indexer = await session.get(IndexerConfig, indexer_id)
    if not indexer:
        raise NotFoundError("Indexer", indexer_id)
    return IndexerResponse.model_validate(_redact_indexer(indexer))


# ── Update ───────────────────────────────────────────────────────────


@router.put("/{indexer_id}", response_model=IndexerResponse)
async def update_indexer(
    indexer_id: int,
    body: IndexerUpdate,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> IndexerResponse:
    """Update an indexer configuration."""
    indexer = await session.get(IndexerConfig, indexer_id)
    if not indexer:
        raise NotFoundError("Indexer", indexer_id)

    update_data = body.model_dump(exclude_unset=True)
    if update_data.get("resolver_enabled"):
        _validate_resolver_scope(
            indexer_type=indexer.indexer_type,
            source=str(indexer.source),
            resolver_enabled=True,
        )

    if update_data.get("download_client_id") is not None:
        await _validate_download_client(
            session, indexer.indexer_type, update_data["download_client_id"]
        )

    # Encrypt api_key if provided; omitted → keep existing encrypted value
    if update_data.get("api_key"):
        update_data["api_key"] = encrypt_secret(update_data["api_key"])
    elif "api_key" in update_data and not update_data["api_key"]:
        # Empty string submitted → keep existing (don't wipe on edit)
        del update_data["api_key"]

    for field, value in update_data.items():
        setattr(indexer, field, value)

    await session.flush()
    await session.refresh(indexer)
    return IndexerResponse.model_validate(_redact_indexer(indexer))


def _validate_resolver_scope(
    *,
    indexer_type: object,
    source: str,
    resolver_enabled: bool,
) -> None:
    if not resolver_enabled:
        return
    if indexer_type != "torznab" or source != "manual":
        raise ValidationError(
            "Browser resolver support is available only for a manual Torznab indexer."
        )


_PROTOCOL_BY_INDEXER_TYPE: dict[str, tuple[AcquisitionProtocol, ...]] = {
    IndexerType.TORZNAB: (AcquisitionProtocol.TORRENT,),
    IndexerType.NEWZNAB: (AcquisitionProtocol.USENET,),
}


async def _validate_download_client(
    session: DbSession,
    indexer_type: object,
    download_client_id: int | None,
) -> None:
    """Check a pinned client exists and takes the protocol this indexer returns."""
    if download_client_id is None:
        return
    client = await session.get(DownloadClientConfig, download_client_id)
    if client is None:
        raise ValidationError(f"Download client {download_client_id} does not exist.")
    wanted = _PROTOCOL_BY_INDEXER_TYPE.get(
        str(indexer_type),
        (AcquisitionProtocol.TORRENT, AcquisitionProtocol.USENET),
    )
    if DownloadClientType(client.client_type).acquisition_protocol not in wanted:
        kind = " or ".join(protocol.value for protocol in wanted)
        raise ValidationError(
            f'"{client.name}" is not a {kind} download client, so it cannot '
            "take releases from this indexer."
        )


# ── Delete ───────────────────────────────────────────────────────────


@router.delete("/{indexer_id}", status_code=204)
async def delete_indexer(
    indexer_id: int,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> None:
    """Remove an indexer configuration."""
    indexer = await session.get(IndexerConfig, indexer_id)
    if not indexer:
        raise NotFoundError("Indexer", indexer_id)
    await session.delete(indexer)


# ── Test Connection ──────────────────────────────────────────────────


@router.post("/{indexer_id}/test", status_code=200)
async def test_indexer(
    indexer_id: int,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> dict[str, object]:
    """Test connectivity to an indexer."""
    indexer = await session.get(IndexerConfig, indexer_id)
    if not indexer:
        raise NotFoundError("Indexer", indexer_id)

    from pullbox.providers.indexer.newznab import NewznabIndexer
    from pullbox.providers.indexer.prowlarr import ProwlarrIndexer
    from pullbox.providers.indexer.torznab import TorznabIndexer
    from pullbox.services.direct_resolver_service import (
        build_manual_torznab_resolver_options,
    )

    provider: NewznabIndexer | TorznabIndexer | ProwlarrIndexer
    name = str(indexer.name)
    url = str(indexer.url)
    api_key = decrypt_secret(str(indexer.api_key))

    if indexer.indexer_type == "prowlarr":
        provider = ProwlarrIndexer(url=url, api_key=api_key)
    elif indexer.indexer_type == "torznab":
        resolver_options = await build_manual_torznab_resolver_options(session, indexer)
        provider = TorznabIndexer(
            name=name,
            url=url,
            api_key=api_key,
            resolver_enabled=bool(indexer.resolver_enabled),
            resolver_options=resolver_options,
        )
    else:
        provider = NewznabIndexer(name=name, url=url, api_key=api_key)

    result = await provider.test_connection()

    await _persist_indexer_test_status_with_retry(
        session,
        indexer_id,
        healthy=result.healthy,
        message=result.message,
        checked_at=datetime.now(UTC),
    )

    logger.info(
        "indexer_test_connection",
        indexer_id=indexer_id,
        healthy=result.healthy,
        response_time_ms=result.response_time_ms,
    )
    return {
        "healthy": result.healthy,
        "message": result.message,
        "response_time_ms": result.response_time_ms,
    }
