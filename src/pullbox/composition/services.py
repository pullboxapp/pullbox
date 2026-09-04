"""Service composition helpers shared by API, UI, and task entrypoints."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from pullbox.composition import providers
from pullbox.composition.events import build_domain_event_bus, build_scoped_event_bus
from pullbox.config import get_settings
from pullbox.core.comicvine_key import get_comicvine_api_key
from pullbox.models.config import SystemConfig
from pullbox.models.direct_acquisition import DirectArtifactFailureClass
from pullbox.providers.artifact_hosts.contract import (
    ArtifactHostResolutionError,
    ArtifactResolutionProgress,
)
from pullbox.providers.artifact_hosts.datanodes import DataNodesAdapter
from pullbox.providers.artifact_hosts.generic import GenericHttpsAdapter
from pullbox.providers.artifact_hosts.helpers import challenge_required
from pullbox.providers.artifact_hosts.mediafire import MediaFireAdapter
from pullbox.providers.artifact_hosts.mega import MegaArtifactHostAdapter, MegaBridgeRunner
from pullbox.providers.artifact_hosts.pixeldrain import PixelDrainAdapter
from pullbox.providers.artifact_hosts.resolver import ArtifactHostResolver
from pullbox.providers.artifact_hosts.rootz import RootzAdapter
from pullbox.providers.artifact_hosts.terabox import TeraBoxAdapter
from pullbox.providers.artifact_hosts.transport import HttpArtifactTransport
from pullbox.providers.metadata.comicvine import ComicVineProvider
from pullbox.services import download_service
from pullbox.services.cover_resolver import resolve_covers_dir
from pullbox.services.direct_acquisition_executor import (
    DirectAcquisitionExecutor,
    DirectExecutionResult,
)
from pullbox.services.direct_artifact_quarantine import DirectArtifactQuarantine
from pullbox.services.import_provider_cache import build_persistent_import_metadata_provider
from pullbox.services.import_service import ImportService
from pullbox.services.matching_service import MatchingService
from pullbox.services.metadata_service import MetadataService
from pullbox.services.series_service import SeriesService

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.indexer import IndexerConfig
    from pullbox.providers.artifact_hosts.contract import (
        ArtifactResolutionProgressCallback,
        HostResolutionRequest,
    )
    from pullbox.providers.base import ProviderRegistry
    from pullbox.providers.direct.resolver import DirectResolverResult
    from pullbox.services.direct_resolver_service import ResolverAttemptProgress
    from pullbox.services.download_service import DownloadService


_DIRECT_HTTP_TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=60.0, pool=15.0)


class DirectAcquisitionRuntime:
    """Own the shared native direct-download executor and its HTTP client."""

    def __init__(
        self,
        *,
        executor: DirectAcquisitionExecutor,
        http_client: httpx.AsyncClient,
        host_kinds: tuple[object, ...],
    ) -> None:
        self._executor = executor
        self._http_client = http_client
        self.host_kinds = host_kinds
        self.closed = False

    async def execute(
        self,
        session: AsyncSession,
        *,
        acquisition_id: int,
        artifact_id: int,
        source_factory: Callable[[], Awaitable[HostResolutionRequest]],
        cancel_event: asyncio.Event | None = None,
    ) -> DirectExecutionResult:
        """Delegate one persisted acquisition to the shared executor."""
        return await self._executor.execute(
            session,
            acquisition_id=acquisition_id,
            artifact_id=artifact_id,
            source_factory=source_factory,
            cancel_event=cancel_event,
        )

    async def cancel(
        self,
        session: AsyncSession,
        *,
        acquisition_id: int,
        artifact_id: int,
    ) -> bool:
        """Delegate durable cancellation when no process-local worker is active."""
        return await self._executor.cancel(
            session,
            acquisition_id=acquisition_id,
            artifact_id=artifact_id,
        )

    async def aclose(self) -> None:
        """Close shared network resources exactly once."""
        if self.closed:
            return
        await self._http_client.aclose()
        self.closed = True


def build_direct_acquisition_runtime() -> DirectAcquisitionRuntime:
    """Construct the closed native host registry and durable executor runtime."""
    settings = get_settings()
    http_client = httpx.AsyncClient(
        timeout=_DIRECT_HTTP_TIMEOUT,
        follow_redirects=False,
        trust_env=False,
    )
    adapters = (
        GenericHttpsAdapter(http_client),
        PixelDrainAdapter(http_client),
        MegaArtifactHostAdapter(),
        RootzAdapter(http_client),
        MediaFireAdapter(http_client),
        TeraBoxAdapter(http_client),
        DataNodesAdapter(http_client, login_solver=_solve_datanodes_login),
    )
    executor = DirectAcquisitionExecutor(
        host_resolver=ArtifactHostResolver(adapters),
        http_transport=HttpArtifactTransport(client=http_client),
        mega_runner=MegaBridgeRunner(),
        quarantine=DirectArtifactQuarantine(
            Path(settings.data_dir) / "direct-downloads" / "quarantine"
        ),
    )
    return DirectAcquisitionRuntime(
        executor=executor,
        http_client=http_client,
        host_kinds=tuple(adapter.host_kind for adapter in adapters),
    )


async def _solve_datanodes_login(
    target_url: str,
    progress_callback: ArtifactResolutionProgressCallback | None = None,
) -> DirectResolverResult:
    """Resolve DataNodes' login challenge without handing credentials to TRAWL."""
    from pullbox.database import get_session_factory
    from pullbox.providers.direct.resolver import DirectResolverError
    from pullbox.services.direct_resolver_service import (
        DirectResolverServiceError,
        resolve_for_trawl_host_adapter,
    )

    factory = get_session_factory()
    try:
        async with factory() as session:

            async def publish_attempt(event: ResolverAttemptProgress) -> None:
                if progress_callback is None:
                    return
                await progress_callback(
                    ArtifactResolutionProgress(
                        resolver_id=event.resolver_id,
                        resolver_name=event.resolver_name,
                        resolver_kind=event.resolver_kind.value,
                        attempt=event.attempt,
                        total=event.total,
                        scope=event.scope,
                    )
                )

            return await resolve_for_trawl_host_adapter(
                session,
                target_url=target_url,
                adapter_id="datanodes",
                declared_domains=("datanodes.to",),
                challenge_category="artifact_host_login",
                on_attempt=publish_attempt,
            )
    except (DirectResolverError, DirectResolverServiceError) as exc:
        raise _datanodes_login_failure(exc) from exc


def _datanodes_login_failure(exc: Exception) -> ArtifactHostResolutionError:
    if bool(getattr(exc, "retryable", False)):
        return ArtifactHostResolutionError(
            code="artifact_host_resolver_unavailable",
            message="The TRAWL browser pool is temporarily busy or unavailable.",
            failure_class=DirectArtifactFailureClass.RESOLVER,
            retryable=True,
            intervention=False,
        )
    return challenge_required(
        "DataNodes requires a healthy TRAWL resolver to complete account login."
    )


async def build_metadata_service(session: AsyncSession) -> MetadataService:
    """Construct a MetadataService using persisted ComicVine settings."""
    settings = get_settings()
    api_key = await get_comicvine_api_key(session)
    provider = ComicVineProvider(api_key=api_key)
    provider = build_persistent_import_metadata_provider(session, provider)
    covers_dir = await resolve_covers_dir(session)
    return MetadataService(
        provider=provider,
        covers_dir=covers_dir,
        refresh_days=settings.metadata_refresh_days,
    )


def build_series_service(metadata_service: MetadataService) -> SeriesService:
    """Construct a SeriesService for domain flows with shared side effects."""
    return SeriesService(metadata_service=metadata_service, event_bus=build_domain_event_bus())


async def build_domain_series_service(session: AsyncSession) -> SeriesService:
    """Construct a SeriesService using persisted metadata settings and domain events."""
    return build_series_service(await build_metadata_service(session))


def build_matching_service() -> MatchingService:
    """Construct a MatchingService for domain flows with shared side effects."""
    return MatchingService(event_bus=build_domain_event_bus())


async def build_import_service(
    session: AsyncSession,
    *,
    min_burst_limit: int | None = None,
) -> ImportService:
    """Construct an ImportService using persisted ComicVine settings."""
    settings = get_settings()
    api_key = await get_comicvine_api_key(session)
    persisted_rate_config = await session.get(SystemConfig, "comicvine_rate_limit_per_second")
    persisted_rate_value: str | None = None
    if persisted_rate_config is not None:
        candidate_value = getattr(persisted_rate_config, "value", None)
        if isinstance(candidate_value, str | int | float):
            persisted_rate_value = str(candidate_value).strip()

    if persisted_rate_value:
        try:
            burst_limit = max(1, min(int(persisted_rate_value), 10))
        except ValueError:
            burst_limit = 1
    else:
        burst_limit = 1

    if min_burst_limit is not None:
        burst_limit = max(burst_limit, max(1, min(int(min_burst_limit), 10)))

    rate_limit = max(1, int(getattr(settings, "comicvine_rate_limit", 200)))

    provider = ComicVineProvider(
        api_key=api_key or "",
        rate_limit=rate_limit,
        burst_limit=burst_limit,
    )
    provider = build_persistent_import_metadata_provider(session, provider)
    metadata_svc = MetadataService(
        provider,
        covers_dir=await resolve_covers_dir(session),
        refresh_days=settings.metadata_refresh_days,
    )
    event_bus = build_scoped_event_bus()
    series_svc = SeriesService(metadata_svc, event_bus)

    return ImportService(
        series_service=series_svc,
        metadata_service=metadata_svc,
        event_bus=event_bus,
    )


def build_import_control_service() -> ImportService:
    """Construct an ImportService for DB-only import review/control helpers."""
    return ImportService(
        series_service=None,  # type: ignore[arg-type]
        metadata_service=None,  # type: ignore[arg-type]
        event_bus=None,  # type: ignore[arg-type]
    )


def build_download_service(registry: ProviderRegistry) -> DownloadService:
    """Construct a DownloadService using the shared domain event bus."""
    return download_service.DownloadService(registry=registry, event_bus=build_domain_event_bus())


async def build_domain_download_service(
    session: AsyncSession,
) -> tuple[DownloadService, dict[int, IndexerConfig]] | None:
    """Build a registry-backed DownloadService for route/task domain flows."""
    built = await providers.build_registry(session)
    if built is None:
        return None

    registry, indexer_configs = built
    return build_download_service(registry), indexer_configs
