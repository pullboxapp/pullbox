"""AirDC++ runtime composition with feature-flag and exact-client isolation."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING, cast

import structlog
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from pullbox.core.encryption import decrypt_secret
from pullbox.models.client import DownloadClientConfig
from pullbox.models.download import DownloadClientType
from pullbox.providers.airdcpp.api_client import AirDcppApiClient
from pullbox.providers.airdcpp.socket_client import AirDcppSocketClient
from pullbox.providers.airdcpp.supervisor import (
    AirDcppSupervisor,
    AirDcppSupervisorConfig,
    AirDcppSupervisorRegistry,
    AirDcppSupervisorState,
)
from pullbox.services.airdcpp_reconciliation import (
    AirDcppReconciler,
    AirDcppReconciliationApi,
)
from pullbox.services.airdcpp_search_cooldown import AirDcppSearchCooldown
from pullbox.services.airdcpp_search_coordinator import (
    AirDcppSearchApi,
    AirDcppSearchClient,
    AirDcppSearchCoordinator,
    AirDcppSearchSocket,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_registry: AirDcppSupervisorRegistry | None = None
_search_coordinator: AirDcppSearchCoordinator | None = None
_reconciler: AirDcppReconciler | None = None
_reconciliation_task: asyncio.Task[None] | None = None
logger = structlog.get_logger(__name__)


async def load_airdcpp_search_clients(
    session: AsyncSession,
    registry: AirDcppSupervisorRegistry,
    *,
    automatic: bool = False,
) -> tuple[AirDcppSearchClient, ...]:
    """Detach ready, search-enabled exact clients from ORM state."""
    result = await session.execute(
        select(DownloadClientConfig)
        .where(
            DownloadClientConfig.client_type == DownloadClientType.AIRDCPP,
            DownloadClientConfig.enabled.is_(True),
        )
        .options(selectinload(DownloadClientConfig.airdcpp_settings))
        .order_by(DownloadClientConfig.priority, DownloadClientConfig.id)
    )
    clients: list[AirDcppSearchClient] = []
    for client in result.scalars().all():
        settings = client.airdcpp_settings
        supervisor = cast("AirDcppSupervisor | None", registry.get(client.id))
        if (
            settings is None
            or not settings.search_enabled
            or (automatic and not settings.automatic_search_enabled)
            or supervisor is None
            or supervisor.state is not AirDcppSupervisorState.READY
        ):
            continue
        clients.append(
            AirDcppSearchClient(
                config_id=client.id,
                client_identity=f"airdcpp:{client.id}",
                client_name=client.name,
                client_priority=client.priority,
                api_client=cast("AirDcppSearchApi", supervisor.api_client),
                socket_client=cast("AirDcppSearchSocket", supervisor.socket_client),
                manual_collection_seconds=settings.manual_collection_seconds,
                automatic_collection_seconds=settings.automatic_collection_seconds,
                max_results=settings.max_results,
                max_retained_routes=settings.max_retained_routes,
                max_concurrent_searches=settings.max_concurrent_searches,
                search_dispatch_deadline_seconds=(settings.search_dispatch_deadline_seconds),
                hub_allowlist=tuple(settings.hub_allowlist),
            )
        )
    return tuple(clients)


async def build_airdcpp_supervisor_configs(
    session: AsyncSession,
) -> tuple[AirDcppSupervisorConfig, ...]:
    """Load enabled AirDC++ clients and decrypt only their in-memory password."""
    result = await session.execute(
        select(DownloadClientConfig)
        .where(
            DownloadClientConfig.client_type == DownloadClientType.AIRDCPP,
            DownloadClientConfig.enabled.is_(True),
        )
        .options(selectinload(DownloadClientConfig.airdcpp_settings))
        .order_by(DownloadClientConfig.id)
    )
    configs: list[AirDcppSupervisorConfig] = []
    for client in result.scalars().all():
        settings = client.airdcpp_settings
        if settings is None or not client.username or not client.password:
            continue
        configs.append(
            AirDcppSupervisorConfig(
                config_id=client.id,
                client_identity=f"airdcpp:{client.id}",
                name=client.name,
                base_url=client.url,
                username=client.username,
                password=SecretStr(decrypt_secret(client.password)),
                request_timeout_seconds=settings.request_timeout_seconds,
                enabled=client.enabled,
            )
        )
    return tuple(configs)


async def start_airdcpp_supervisor_registry(
    session_factory: async_sessionmaker[AsyncSession] | Callable[[], AsyncSession],
    *,
    enabled: bool,
) -> AirDcppSupervisorRegistry | None:
    """Load local config and schedule remote work without delaying app readiness."""
    global _reconciler, _reconciliation_task, _registry, _search_coordinator
    if not enabled:
        _registry = None
        _search_coordinator = None
        _reconciler = None
        _reconciliation_task = None
        return None

    registry = AirDcppSupervisorRegistry(supervisor_factory=_build_supervisor)
    cooldown = AirDcppSearchCooldown(session_factory)
    _registry = registry
    _reconciler = AirDcppReconciler(session_factory, cooldown=cooldown)
    _search_coordinator = AirDcppSearchCoordinator(cooldown=cooldown)
    async with session_factory() as session:
        configs = await build_airdcpp_supervisor_configs(session)
    await registry.apply(configs)
    _reconciliation_task = asyncio.create_task(
        _run_periodic_reconciliation(),
        name="airdcpp-reconciliation",
    )
    return registry


def get_airdcpp_supervisor_registry() -> AirDcppSupervisorRegistry | None:
    """Return the process registry for search, queue, and health services."""
    return _registry


def get_airdcpp_search_coordinator() -> AirDcppSearchCoordinator | None:
    """Return the process-wide coordinator sharing exact-client concurrency."""
    return _search_coordinator


def get_airdcpp_reconciliation_task_count() -> int:
    """Expose the single bounded scheduler ownership count for diagnostics/tests."""
    task = _reconciliation_task
    return int(task is not None and not task.done())


async def refresh_airdcpp_supervisor_registry(
    session_factory: async_sessionmaker[AsyncSession] | Callable[[], AsyncSession],
) -> None:
    """Apply committed config changes to only the affected supervisors."""
    async with session_factory() as session:
        await refresh_airdcpp_supervisor_registry_from_session(session)


async def refresh_airdcpp_supervisor_registry_from_session(
    session: AsyncSession,
) -> None:
    """Apply committed client state using an existing request session."""
    registry = _registry
    if registry is None:
        return
    configs = await build_airdcpp_supervisor_configs(session)
    await registry.apply(configs)


async def stop_airdcpp_supervisor_registry(
    registry_override: AirDcppSupervisorRegistry | None = None,
) -> None:
    """Stop and clear the process registry, if it was enabled."""
    global _reconciler, _reconciliation_task, _registry, _search_coordinator
    registry = _registry
    task = _reconciliation_task
    _registry = None
    _search_coordinator = None
    _reconciler = None
    _reconciliation_task = None
    if task is not None and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    if registry is not None:
        await registry.stop()
    if registry_override is not None and registry_override is not registry:
        await registry_override.stop()


def _build_supervisor(config: AirDcppSupervisorConfig) -> AirDcppSupervisor:
    api_client = AirDcppApiClient(
        base_url=config.base_url,
        username=config.username,
        password=config.password.get_secret_value(),
        timeout_seconds=config.request_timeout_seconds,
    )
    socket_client = AirDcppSocketClient(
        base_url=config.base_url,
        timeout_seconds=config.request_timeout_seconds,
    )
    return AirDcppSupervisor(
        config=config,
        api_client=api_client,
        socket_client=socket_client,
        reconcile=_reconcile_ready_client,
    )


async def _reconcile_ready_client(config_id: int) -> None:
    registry = _registry
    reconciler = _reconciler
    supervisor = registry.get(config_id) if registry is not None else None
    if (
        reconciler is None
        or supervisor is None
        or supervisor.state is not AirDcppSupervisorState.READY
    ):
        return
    result = await reconciler.reconcile_client(
        config_id,
        cast("AirDcppReconciliationApi", supervisor.api_client),
    )
    if result.completed:
        from pullbox.core.scheduler import get_scheduler

        try:
            get_scheduler().run_task_now("process_completed")
        except Exception:
            logger.warning(
                "airdcpp_post_processing_trigger_failed",
                client_config_id=config_id,
                exc_info=True,
            )


async def _run_periodic_reconciliation() -> None:
    """Use one process task for all ready exact-client queue snapshots."""
    try:
        while True:
            await asyncio.sleep(10)
            registry = _registry
            if registry is None:
                return
            for config_id in registry.config_ids:
                try:
                    await _reconcile_ready_client(config_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "airdcpp_reconciliation_failed",
                        client_config_id=config_id,
                        exc_info=True,
                    )
    except asyncio.CancelledError:
        raise
