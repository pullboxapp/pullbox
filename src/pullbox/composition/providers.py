"""Provider registry composition from persisted configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from pullbox.core.encryption import decrypt_secret
from pullbox.models.client import DownloadClientConfig
from pullbox.models.config import SystemConfig
from pullbox.models.download import DownloadClientType
from pullbox.models.indexer import IndexerConfig, IndexerSource, IndexerType
from pullbox.providers.base import ProviderRegistry
from pullbox.providers.download.deluge import DelugeClient
from pullbox.providers.download.nzbget import NZBGetClient
from pullbox.providers.download.qbittorrent import QBittorrentClient
from pullbox.providers.download.sabnzbd import SABnzbdClient
from pullbox.providers.download.transmission import TransmissionClient
from pullbox.providers.indexer.newznab import NewznabIndexer
from pullbox.providers.indexer.prowlarr import ProwlarrIndexer
from pullbox.providers.indexer.torznab import TorznabIndexer
from pullbox.services.direct_resolver_service import build_manual_torznab_resolver_options

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

# Sentinel config ID for the aggregate Prowlarr indexer.
# Prowlarr-synced configs are collapsed into a single ProwlarrIndexer
# and registered under this ID (no per-indexer health tracking -
# Prowlarr handles that internally).
_PROWLARR_AGGREGATE_CONFIG_ID = -1
_JACKETT_RATE_LIMIT_PER_MINUTE = 60
_JACKETT_REQUEST_TIMEOUT_SECONDS = 60.0

# Cache download clients so we reuse authenticated sessions across task cycles.
# Keyed by (client_type, url, updated_at) so config changes invalidate the cache.
_download_client_cache: dict[tuple[str, str, str], object] = {}


async def register_indexers(
    session: AsyncSession,
    registry: ProviderRegistry,
) -> dict[int, IndexerConfig]:
    """Query enabled indexer configs and register them on *registry*.

    Prowlarr-synced **Torznab** indexers are collapsed into a single
    aggregate ``ProwlarrIndexer`` that searches all of them in one HTTP
    request.  Prowlarr-synced **Newznab** indexers are kept as individual
    ``NewznabIndexer`` instances via their proxy URLs - Prowlarr's REST
    API search returns different (often worse) results for Newznab
    indexers compared to the direct Newznab proxy endpoint with category
    filtering.  Manual (non-Prowlarr) indexers are always registered
    individually.

    Returns a mapping of indexer config ID to config for health tracking.
    Prowlarr-synced Torznab indexers do NOT appear in the map - Prowlarr
    handles their health internally.
    """
    result = await session.execute(
        select(IndexerConfig).where(
            IndexerConfig.enabled.is_(True),
            IndexerConfig.manager_available.is_(True),
        )
    )
    indexer_configs = list(result.scalars().all())

    configs_map: dict[int, IndexerConfig] = {}
    prowlarr_torznab_ids: list[int] = []
    prowlarr_indexer_rankings: dict[int, tuple[int, int]] = {}

    for idx_cfg in indexer_configs:
        if not idx_cfg.manager_available:
            continue
        is_prowlarr_synced = str(idx_cfg.source) == IndexerSource.PROWLARR
        is_jackett_synced = str(idx_cfg.source) == IndexerSource.JACKETT

        # Prowlarr-synced Torznab: aggregate into single ProwlarrIndexer
        if is_prowlarr_synced and idx_cfg.indexer_type == IndexerType.TORZNAB:
            if idx_cfg.prowlarr_indexer_id is not None:
                prowlarr_torznab_ids.append(idx_cfg.prowlarr_indexer_id)
                prowlarr_indexer_rankings[idx_cfg.prowlarr_indexer_id] = (
                    idx_cfg.id,
                    idx_cfg.priority,
                )
            continue

        # Prowlarr-synced Newznab and all manual indexers: register individually
        decrypted_api_key = decrypt_secret(idx_cfg.api_key)

        if idx_cfg.indexer_type == IndexerType.TORZNAB:
            resolver_options = (
                ()
                if is_jackett_synced
                else await build_manual_torznab_resolver_options(session, idx_cfg)
            )
            resolver_enabled = False if is_jackett_synced else bool(idx_cfg.resolver_enabled)
            cache_namespace = (
                f"jackett-torznab:{idx_cfg.id}"
                if is_jackett_synced
                else f"manual-torznab:{idx_cfg.id}"
            )
            if is_jackett_synced:
                indexer: NewznabIndexer = TorznabIndexer(
                    name=idx_cfg.name,
                    url=idx_cfg.url,
                    api_key=decrypted_api_key,
                    rate_limit_per_minute=_JACKETT_RATE_LIMIT_PER_MINUTE,
                    request_timeout=_JACKETT_REQUEST_TIMEOUT_SECONDS,
                    resolver_enabled=resolver_enabled,
                    resolver_options=resolver_options,
                    cache_namespace=cache_namespace,
                )
            else:
                indexer = TorznabIndexer(
                    name=idx_cfg.name,
                    url=idx_cfg.url,
                    api_key=decrypted_api_key,
                    resolver_enabled=resolver_enabled,
                    resolver_options=resolver_options,
                    cache_namespace=cache_namespace,
                )
        else:
            indexer = NewznabIndexer(
                name=idx_cfg.name,
                url=idx_cfg.url,
                api_key=decrypted_api_key,
            )
        registry.register_indexer(idx_cfg.id, indexer)
        configs_map[idx_cfg.id] = idx_cfg

    # Register a single aggregate ProwlarrIndexer for Torznab indexers
    if prowlarr_torznab_ids:
        prowlarr_url, prowlarr_api_key = await _load_prowlarr_credentials(session)
        if prowlarr_url and prowlarr_api_key:
            aggregate = ProwlarrIndexer(
                url=prowlarr_url,
                api_key=prowlarr_api_key,
                indexer_ids=prowlarr_torznab_ids,
                indexer_rankings=prowlarr_indexer_rankings,
            )
            registry.register_indexer(_PROWLARR_AGGREGATE_CONFIG_ID, aggregate)
            for config_id, _priority in prowlarr_indexer_rankings.values():
                registry.register_indexer_alias(config_id, _PROWLARR_AGGREGATE_CONFIG_ID)
            logger.debug(
                "prowlarr_aggregate_registered",
                indexer_count=len(prowlarr_torznab_ids),
                indexer_ids=prowlarr_torznab_ids,
            )
        else:
            logger.debug("prowlarr_aggregate_skipped", reason="missing credentials")

    return configs_map


async def _load_prowlarr_credentials(
    session: AsyncSession,
) -> tuple[str | None, str | None]:
    """Load and decrypt Prowlarr URL + API key from SystemConfig."""
    url_row = await session.execute(select(SystemConfig).where(SystemConfig.key == "prowlarr_url"))
    key_row = await session.execute(
        select(SystemConfig).where(SystemConfig.key == "prowlarr_api_key")
    )
    url_cfg = url_row.scalar_one_or_none()
    key_cfg = key_row.scalar_one_or_none()

    if not url_cfg or not key_cfg:
        return None, None

    return url_cfg.value, decrypt_secret(key_cfg.value)


async def register_download_clients(
    session: AsyncSession,
    registry: ProviderRegistry,
) -> list[dict[str, str]]:
    """Query enabled download client configs and register them on *registry*.

    Secrets are decrypted before being passed to provider constructors.
    """
    result = await session.execute(
        select(DownloadClientConfig).where(DownloadClientConfig.enabled.is_(True))
    )
    client_configs = list(result.scalars().all())
    failures: list[dict[str, str]] = []

    for dl_cfg in client_configs:
        updated = str(dl_cfg.updated_at) if dl_cfg.updated_at else ""
        cache_key = (str(dl_cfg.client_type), dl_cfg.url, updated)
        cached = _download_client_cache.get(cache_key)
        prio = dl_cfg.priority

        try:
            if dl_cfg.client_type == DownloadClientType.SABNZBD:
                if isinstance(cached, SABnzbdClient):
                    registry.register_download_client(dl_cfg.id, cached, priority=prio)
                else:
                    sab_client = SABnzbdClient(
                        url=dl_cfg.url,
                        api_key=decrypt_secret(dl_cfg.api_key or ""),
                        category=dl_cfg.category,
                        priority=dl_cfg.sab_priority,
                        post_processing=dl_cfg.sab_post_processing,
                    )
                    _download_client_cache[cache_key] = sab_client
                    registry.register_download_client(dl_cfg.id, sab_client, priority=prio)

            elif dl_cfg.client_type == DownloadClientType.NZBGET:
                if isinstance(cached, NZBGetClient):
                    registry.register_download_client(dl_cfg.id, cached, priority=prio)
                else:
                    nzb_client = NZBGetClient(
                        url=dl_cfg.url,
                        username=dl_cfg.username or "nzbget",
                        password=decrypt_secret(dl_cfg.password or ""),
                        category=dl_cfg.category,
                        priority=dl_cfg.nzbget_priority,
                        post_processing=dl_cfg.nzbget_post_processing,
                    )
                    _download_client_cache[cache_key] = nzb_client
                    registry.register_download_client(dl_cfg.id, nzb_client, priority=prio)

            elif dl_cfg.client_type == DownloadClientType.QBITTORRENT:
                if isinstance(cached, QBittorrentClient):
                    registry.register_download_client(dl_cfg.id, cached, priority=prio)
                else:
                    qbt_client = QBittorrentClient(
                        url=dl_cfg.url,
                        username=dl_cfg.username or "",
                        password=decrypt_secret(dl_cfg.password or ""),
                        category=dl_cfg.category,
                        content_layout=dl_cfg.qbt_content_layout,
                        ratio_limit=dl_cfg.qbt_ratio_limit,
                        seeding_time_limit=dl_cfg.qbt_seeding_time_limit,
                    )
                    _download_client_cache[cache_key] = qbt_client
                    registry.register_download_client(dl_cfg.id, qbt_client, priority=prio)

            elif dl_cfg.client_type == DownloadClientType.TRANSMISSION:
                if isinstance(cached, TransmissionClient):
                    registry.register_download_client(dl_cfg.id, cached, priority=prio)
                else:
                    tr_client = TransmissionClient(
                        url=dl_cfg.url,
                        username=dl_cfg.username or "",
                        password=decrypt_secret(dl_cfg.password or ""),
                        download_dir=dl_cfg.transmission_download_dir,
                        bandwidth_priority=dl_cfg.transmission_bandwidth_priority,
                        seed_ratio_limit=dl_cfg.transmission_seed_ratio_limit,
                        seed_idle_limit=dl_cfg.transmission_seed_idle_limit,
                    )
                    _download_client_cache[cache_key] = tr_client
                    registry.register_download_client(dl_cfg.id, tr_client, priority=prio)

            elif dl_cfg.client_type == DownloadClientType.DELUGE:
                if isinstance(cached, DelugeClient):
                    registry.register_download_client(dl_cfg.id, cached, priority=prio)
                else:
                    del_client = DelugeClient(
                        url=dl_cfg.url,
                        password=decrypt_secret(dl_cfg.password or ""),
                        label=dl_cfg.deluge_label,
                        max_ratio=dl_cfg.deluge_max_ratio,
                        move_completed_path=dl_cfg.deluge_move_completed_path,
                    )
                    _download_client_cache[cache_key] = del_client
                    registry.register_download_client(dl_cfg.id, del_client, priority=prio)
        except Exception as exc:
            logger.warning(
                "download_client_registration_skipped",
                client_id=dl_cfg.id,
                name=dl_cfg.name,
                client_type=str(dl_cfg.client_type),
                error=str(exc),
            )
            failures.append(
                {
                    "config_id": str(dl_cfg.id),
                    "name": dl_cfg.name,
                    "client_type": str(dl_cfg.client_type),
                    "url": dl_cfg.url,
                    "status": "unhealthy",
                    "message": (
                        "Configuration error: saved credentials could not be loaded. "
                        "Re-save this client in Settings > Download Clients."
                    ),
                }
            )
            continue

    return failures


async def build_registry(
    session: AsyncSession,
    *,
    include_download_clients: bool = True,
) -> tuple[ProviderRegistry, dict[int, IndexerConfig]] | None:
    """Build a full ProviderRegistry from enabled DB configs.

    Returns ``None`` if no indexers are configured so the caller can
    exit early.  Otherwise returns (registry, indexer_configs_map).
    """
    registry = ProviderRegistry()

    configs_map = await register_indexers(session, registry)
    if not configs_map and not registry.get_indexer_items():
        return None

    if include_download_clients:
        await register_download_clients(session, registry)

    return registry, configs_map
