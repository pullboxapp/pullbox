"""Operator lifecycle and bounded handoff for the shared browser resolver."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import select

from pullbox.models.direct_acquisition import (
    DirectProviderConfig,
    DirectProviderState,
    DirectResolverConfig,
    DirectResolverKind,
    DirectResolverState,
)
from pullbox.providers.direct.contract import (
    DirectManifestResponse,
    DirectResolverProfile,
)
from pullbox.providers.direct.resolver import (
    DirectResolverClient,
    DirectResolverError,
    DirectResolverResult,
    ResolverCircuitBreaker,
)
from pullbox.services.direct_provider_source_origin import (
    effective_direct_provider_source_domains,
)
from pullbox.services.direct_resolver_configuration import (
    DirectResolverConfigRead,
    load_resolver_auth_headers,
    read_resolver_config,
    update_resolver_auth_headers,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.indexer import IndexerConfig
    from pullbox.providers.direct.endpoint import ValidatedProviderEndpoint

_RESOLVER_NAME = "default"
_TEST_TARGET = "https://example.com/"
_MAX_RESOLVER_PROFILES = 3
_resolver_runtimes: dict[tuple[int, str, int], ResolverCircuitBreaker] = {}


class DirectResolverServiceError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        cause_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.cause_code = cause_code


@dataclass(frozen=True, slots=True)
class DirectResolverUpdate:
    endpoint: str
    enabled: bool
    allow_private_http: bool = False
    timeout_seconds: int = 60
    max_concurrency: int = 1
    authentication_headers: Mapping[str, str | None] | None = field(
        default=None,
        repr=False,
    )


@dataclass(frozen=True, slots=True)
class DirectResolverCreate:
    name: str
    resolver_kind: DirectResolverKind
    priority: int
    endpoint: str
    enabled: bool
    allow_private_http: bool = False
    timeout_seconds: int = 60
    max_concurrency: int = 1
    authentication_headers: Mapping[str, str | None] | None = field(
        default=None,
        repr=False,
    )


@dataclass(frozen=True, slots=True)
class DirectResolverTestResult:
    usable: bool
    state: DirectResolverState
    message: str
    checked_at: datetime


@dataclass(frozen=True, slots=True)
class ResolverAttemptProgress:
    resolver_id: int
    resolver_name: str
    resolver_kind: DirectResolverKind
    attempt: int
    total: int
    scope: str


ResolverAttemptCallback = Callable[[ResolverAttemptProgress], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ProviderResolverOption:
    resolver_id: int
    resolver_name: str
    resolver_kind: DirectResolverKind
    profile: DirectResolverProfile = field(repr=False)


NativeResolverSolve = Callable[
    [str, Sequence[str], str],
    Awaitable[DirectResolverResult],
]


@dataclass(frozen=True, slots=True)
class NativeResolverOption:
    """One ranked native resolver with all secrets hidden behind its solve boundary."""

    resolver_id: int
    resolver_name: str
    resolver_kind: DirectResolverKind
    _solve: NativeResolverSolve = field(repr=False)

    async def solve(
        self,
        target_url: str,
        *,
        declared_domains: Sequence[str],
        challenge_category: str,
    ) -> DirectResolverResult:
        return await self._solve(target_url, declared_domains, challenge_category)


class DirectResolverClientProtocol(Protocol):
    async def validate_endpoint(self) -> ValidatedProviderEndpoint: ...

    async def solve(
        self,
        target_url: str,
        *,
        declared_domains: Sequence[str],
        challenge_category: str,
    ) -> DirectResolverResult: ...

    async def solve_trawl_native(
        self,
        target_url: str,
        *,
        declared_domains: Sequence[str],
        challenge_category: str,
    ) -> DirectResolverResult: ...

    async def aclose(self) -> None: ...


class DirectResolverClientFactory(Protocol):
    def __call__(
        self,
        *,
        endpoint: str,
        allow_private_http: bool,
        authentication_headers: dict[str, str],
        timeout_seconds: float,
        max_concurrency: int,
        circuit_breaker: ResolverCircuitBreaker | None = None,
    ) -> DirectResolverClientProtocol: ...


def _default_client_factory(
    *,
    endpoint: str,
    allow_private_http: bool,
    authentication_headers: dict[str, str],
    timeout_seconds: float,
    max_concurrency: int,
    circuit_breaker: ResolverCircuitBreaker | None = None,
) -> DirectResolverClient:
    return DirectResolverClient(
        endpoint=endpoint,
        allow_private_http=allow_private_http,
        authentication_headers=authentication_headers,
        timeout_seconds=timeout_seconds,
        max_concurrency=max_concurrency,
        circuit_breaker=circuit_breaker,
    )


async def get_direct_resolver(session: AsyncSession) -> DirectResolverConfigRead:
    return read_resolver_config(await _get_or_create(session))


async def list_direct_resolvers(
    session: AsyncSession,
) -> tuple[DirectResolverConfigRead, ...]:
    result = await session.execute(
        select(DirectResolverConfig).order_by(
            DirectResolverConfig.priority,
            DirectResolverConfig.resolver_kind,
            DirectResolverConfig.id,
        )
    )
    return tuple(read_resolver_config(config) for config in result.scalars().all())


async def create_direct_resolver(
    session: AsyncSession,
    create: DirectResolverCreate,
    *,
    client_factory: DirectResolverClientFactory = _default_client_factory,
) -> DirectResolverConfigRead:
    name = create.name.strip()
    if not name:
        raise DirectResolverServiceError(
            "resolver_name_required",
            "Enter a name for the browser resolver.",
        )
    _validate_resolver_limits(
        priority=create.priority,
        timeout_seconds=create.timeout_seconds,
        max_concurrency=create.max_concurrency,
    )
    existing = await session.execute(select(DirectResolverConfig))
    configs = tuple(existing.scalars().all())
    if any(config.resolver_kind is create.resolver_kind for config in configs):
        raise DirectResolverServiceError(
            "resolver_kind_already_configured",
            f"A {create.resolver_kind.value} resolver is already configured.",
        )
    if any(config.name.casefold() == name.casefold() for config in configs):
        raise DirectResolverServiceError(
            "resolver_name_already_configured",
            "A browser resolver with that name is already configured.",
        )
    if len(configs) >= _MAX_RESOLVER_PROFILES:
        raise DirectResolverServiceError(
            "resolver_profile_limit_reached",
            "Pullbox supports at most three browser resolver profiles.",
        )
    endpoint = await _validate_endpoint(
        endpoint=create.endpoint,
        enabled=create.enabled,
        allow_private_http=create.allow_private_http,
        timeout_seconds=create.timeout_seconds,
        max_concurrency=create.max_concurrency,
        authentication_headers=create.authentication_headers or {},
        client_factory=client_factory,
    )
    config = DirectResolverConfig(
        name=name,
        resolver_kind=create.resolver_kind,
        priority=create.priority,
        endpoint=endpoint,
        enabled=create.enabled,
        state=(DirectResolverState.UNKNOWN if create.enabled else DirectResolverState.DISABLED),
        allow_private_http=create.allow_private_http,
        timeout_seconds=create.timeout_seconds,
        max_concurrency=create.max_concurrency,
    )
    if create.authentication_headers is not None:
        update_resolver_auth_headers(config, create.authentication_headers)
    session.add(config)
    await session.commit()
    await session.refresh(config)
    return read_resolver_config(config)


async def update_direct_resolver_profile(
    session: AsyncSession,
    resolver_id: int,
    update: DirectResolverCreate,
    *,
    client_factory: DirectResolverClientFactory = _default_client_factory,
) -> DirectResolverConfigRead:
    config = await _resolver_by_id(session, resolver_id)
    current_auth_headers = load_resolver_auth_headers(config).headers
    name = update.name.strip()
    if not name:
        raise DirectResolverServiceError(
            "resolver_name_required",
            "Enter a name for the browser resolver.",
        )
    _validate_resolver_limits(
        priority=update.priority,
        timeout_seconds=update.timeout_seconds,
        max_concurrency=update.max_concurrency,
    )
    existing = await session.execute(
        select(DirectResolverConfig).where(DirectResolverConfig.id != resolver_id)
    )
    others = tuple(existing.scalars().all())
    if any(item.resolver_kind is update.resolver_kind for item in others):
        raise DirectResolverServiceError(
            "resolver_kind_already_configured",
            f"A {update.resolver_kind.value} resolver is already configured.",
        )
    if any(item.name.casefold() == name.casefold() for item in others):
        raise DirectResolverServiceError(
            "resolver_name_already_configured",
            "A browser resolver with that name is already configured.",
        )
    preview = DirectResolverConfig(
        name=name,
        resolver_kind=update.resolver_kind,
        priority=update.priority,
        endpoint=update.endpoint.strip().rstrip("/"),
        enabled=update.enabled,
        state=config.state,
        allow_private_http=update.allow_private_http,
        timeout_seconds=update.timeout_seconds,
        max_concurrency=update.max_concurrency,
        encrypted_auth_headers=dict(config.encrypted_auth_headers or {}),
        auth_metadata=dict(config.auth_metadata or {}),
    )
    if update.authentication_headers:
        update_resolver_auth_headers(preview, update.authentication_headers)
    preview_auth_headers = load_resolver_auth_headers(preview).headers
    endpoint = await _validate_endpoint(
        endpoint=update.endpoint,
        enabled=update.enabled,
        allow_private_http=update.allow_private_http,
        timeout_seconds=update.timeout_seconds,
        max_concurrency=update.max_concurrency,
        authentication_headers=preview_auth_headers,
        client_factory=client_factory,
    )
    connection_changed = (
        config.resolver_kind != update.resolver_kind
        or config.endpoint != endpoint
        or bool(config.allow_private_http) != update.allow_private_http
        or config.timeout_seconds != update.timeout_seconds
        or config.max_concurrency != update.max_concurrency
        or current_auth_headers != preview_auth_headers
    )
    enabled_changed = bool(config.enabled) != update.enabled
    config.name = name
    config.resolver_kind = update.resolver_kind
    config.priority = update.priority
    config.endpoint = endpoint
    config.enabled = update.enabled
    config.allow_private_http = update.allow_private_http
    config.timeout_seconds = update.timeout_seconds
    config.max_concurrency = update.max_concurrency
    if update.authentication_headers:
        update_resolver_auth_headers(config, update.authentication_headers)
    if not update.enabled:
        config.state = DirectResolverState.DISABLED
    elif connection_changed or enabled_changed:
        config.state = DirectResolverState.UNKNOWN
    if connection_changed or enabled_changed:
        config.last_error_code = None
    await session.commit()
    _clear_resolver_runtime()
    await session.refresh(config)
    return read_resolver_config(config)


async def delete_direct_resolver(session: AsyncSession, resolver_id: int) -> None:
    config = await _resolver_by_id(session, resolver_id)
    await session.delete(config)
    await session.commit()
    _clear_resolver_runtime()


async def update_direct_resolver(
    session: AsyncSession,
    update: DirectResolverUpdate,
    *,
    client_factory: DirectResolverClientFactory = _default_client_factory,
) -> DirectResolverConfigRead:
    _validate_resolver_limits(
        priority=10,
        timeout_seconds=update.timeout_seconds,
        max_concurrency=update.max_concurrency,
    )

    config = await _get_or_create(session)
    current_auth_headers = load_resolver_auth_headers(config).headers
    preview = DirectResolverConfig(
        name=_RESOLVER_NAME,
        resolver_kind=config.resolver_kind,
        priority=config.priority,
        endpoint=update.endpoint.strip().rstrip("/"),
        enabled=update.enabled,
        state=config.state,
        allow_private_http=update.allow_private_http,
        timeout_seconds=update.timeout_seconds,
        max_concurrency=update.max_concurrency,
        encrypted_auth_headers=dict(config.encrypted_auth_headers or {}),
        auth_metadata=dict(config.auth_metadata or {}),
    )
    if update.authentication_headers:
        update_resolver_auth_headers(preview, update.authentication_headers)
    auth_headers = load_resolver_auth_headers(preview).headers
    endpoint = await _validate_endpoint(
        endpoint=update.endpoint,
        enabled=update.enabled,
        allow_private_http=update.allow_private_http,
        timeout_seconds=update.timeout_seconds,
        max_concurrency=update.max_concurrency,
        authentication_headers=auth_headers,
        client_factory=client_factory,
    )
    connection_changed = (
        config.endpoint != endpoint
        or bool(config.allow_private_http) != update.allow_private_http
        or config.timeout_seconds != update.timeout_seconds
        or config.max_concurrency != update.max_concurrency
        or current_auth_headers != auth_headers
    )
    enabled_changed = bool(config.enabled) != update.enabled

    config.endpoint = endpoint
    config.enabled = update.enabled
    config.allow_private_http = update.allow_private_http
    config.timeout_seconds = update.timeout_seconds
    config.max_concurrency = update.max_concurrency
    if update.authentication_headers:
        update_resolver_auth_headers(config, update.authentication_headers)
    if not update.enabled:
        config.state = DirectResolverState.DISABLED
    elif connection_changed or enabled_changed:
        config.state = DirectResolverState.UNKNOWN
    if connection_changed or enabled_changed:
        config.last_error_code = None
    await session.commit()
    _clear_resolver_runtime()
    await session.refresh(config)
    return read_resolver_config(config)


async def test_direct_resolver(
    session: AsyncSession,
    *,
    resolver_id: int | None = None,
    client_factory: DirectResolverClientFactory = _default_client_factory,
) -> DirectResolverTestResult:
    config = (
        await _get_or_create(session)
        if resolver_id is None
        else await _resolver_by_id(session, resolver_id)
    )
    if not config.endpoint:
        raise DirectResolverServiceError(
            "resolver_endpoint_required",
            "Configure a resolver endpoint before testing it.",
        )
    checked_at = datetime.now(UTC)
    client = _client_for_config(config, client_factory)
    try:
        await client.solve(
            _TEST_TARGET,
            declared_domains=("example.com",),
            challenge_category="connection_test",
        )
    except DirectResolverError as exc:
        state = _state_for_error(exc.code)
        config.state = state
        config.last_tested_at = checked_at
        config.last_error_code = exc.code
        await session.commit()
        return DirectResolverTestResult(
            usable=False,
            state=state,
            message=_test_failure_message(state),
            checked_at=checked_at,
        )
    finally:
        await client.aclose()

    config.state = DirectResolverState.HEALTHY
    config.last_tested_at = checked_at
    config.last_health_at = checked_at
    config.last_error_code = None
    await session.commit()
    return DirectResolverTestResult(
        usable=True,
        state=DirectResolverState.HEALTHY,
        message="Resolver returned a compatible standard /v1 response.",
        checked_at=checked_at,
    )


async def build_provider_resolver_profile(
    session: AsyncSession,
    provider: DirectProviderConfig,
) -> DirectResolverProfile | None:
    """Build the first request-only profile for legacy single-profile callers."""
    profiles = await build_provider_resolver_profiles(session, provider)
    return profiles[0].profile if profiles else None


async def build_provider_resolver_profiles(
    session: AsyncSession,
    provider: DirectProviderConfig,
) -> tuple[ProviderResolverOption, ...]:
    """Build ranked request-only profiles after every provider gate passes."""
    if (
        not provider.enabled
        or provider.state not in {DirectProviderState.HEALTHY, DirectProviderState.DEGRADED}
        or not provider.resolver_enabled
    ):
        return ()
    try:
        manifest = DirectManifestResponse.model_validate(provider.manifest_snapshot)
    except ValueError:
        return ()
    source_domains = effective_direct_provider_source_domains(provider)
    if not manifest.capabilities.browser_challenge or not source_domains:
        return ()
    resolvers = tuple(config for config in await _eligible_resolvers(session) if config.endpoint)
    return tuple(
        ProviderResolverOption(
            resolver_id=resolver.id,
            resolver_name=resolver.name,
            resolver_kind=resolver.resolver_kind,
            profile=DirectResolverProfile(
                endpoint=resolver.endpoint,
                mode=(
                    "trawl_scrape"
                    if resolver.resolver_kind is DirectResolverKind.TRAWL
                    else "flaresolverr_v1"
                ),
                timeout_seconds=float(resolver.timeout_seconds),
                max_concurrency=resolver.max_concurrency,
                declared_domains=list(source_domains),
                authentication_headers=load_resolver_auth_headers(resolver).headers,
            ),
        )
        for resolver in resolvers
    )


async def build_manual_torznab_resolver_options(
    session: AsyncSession,
    indexer: IndexerConfig,
    *,
    client_factory: DirectResolverClientFactory = _default_client_factory,
) -> tuple[NativeResolverOption, ...]:
    """Build an ephemeral ranked chain for an opted-in manual Torznab indexer."""
    if (
        str(indexer.indexer_type) != "torznab"
        or str(indexer.source) != "manual"
        or indexer.resolver_enabled is not True
    ):
        return ()
    return tuple(
        _native_resolver_option(config, client_factory=client_factory)
        for config in await _eligible_resolvers(session)
    )


def _native_resolver_option(
    config: DirectResolverConfig,
    *,
    client_factory: DirectResolverClientFactory,
) -> NativeResolverOption:
    endpoint = config.endpoint
    allow_private_http = bool(config.allow_private_http)
    authentication_headers = load_resolver_auth_headers(config).headers
    timeout_seconds = float(config.timeout_seconds)
    max_concurrency = config.max_concurrency
    circuit_breaker = _get_resolver_runtime(config)
    resolver_kind = config.resolver_kind

    async def solve(
        target_url: str,
        declared_domains: Sequence[str],
        challenge_category: str,
    ) -> DirectResolverResult:
        client = client_factory(
            endpoint=endpoint,
            allow_private_http=allow_private_http,
            authentication_headers=authentication_headers,
            timeout_seconds=timeout_seconds,
            max_concurrency=max_concurrency,
            circuit_breaker=circuit_breaker,
        )
        try:
            if resolver_kind is DirectResolverKind.TRAWL:
                return await client.solve_trawl_native(
                    target_url,
                    declared_domains=declared_domains,
                    challenge_category=challenge_category,
                )
            return await client.solve(
                target_url,
                declared_domains=declared_domains,
                challenge_category=challenge_category,
            )
        finally:
            await client.aclose()

    return NativeResolverOption(
        resolver_id=config.id,
        resolver_name=config.name,
        resolver_kind=config.resolver_kind,
        _solve=solve,
    )


async def resolve_for_host_adapter(
    session: AsyncSession,
    *,
    target_url: str,
    adapter_id: str,
    declared_domains: Sequence[str],
    challenge_category: str,
    client_factory: DirectResolverClientFactory = _default_client_factory,
    on_attempt: ResolverAttemptCallback | None = None,
) -> DirectResolverResult:
    """Use the resolver only through a code-owned host-adapter domain allowlist."""
    if not adapter_id or not declared_domains:
        raise DirectResolverServiceError(
            "resolver_adapter_policy_required",
            "Host adapter resolver use requires a static domain policy.",
        )
    configs = await _eligible_resolvers(session)
    return await _resolve_with_chain(
        configs,
        target_url=target_url,
        declared_domains=declared_domains,
        challenge_category=challenge_category,
        scope=adapter_id,
        native_trawl=False,
        client_factory=client_factory,
        on_attempt=on_attempt,
    )


async def resolve_for_trawl_host_adapter(
    session: AsyncSession,
    *,
    target_url: str,
    adapter_id: str,
    declared_domains: Sequence[str],
    challenge_category: str,
    client_factory: DirectResolverClientFactory = _default_client_factory,
    on_attempt: ResolverAttemptCallback | None = None,
) -> DirectResolverResult:
    """Use TRAWL's native solver for one code-owned host adapter policy."""
    if not adapter_id or not declared_domains:
        raise DirectResolverServiceError(
            "resolver_adapter_policy_required",
            "Host adapter resolver use requires a static domain policy.",
        )
    configs = tuple(
        config
        for config in await _eligible_resolvers(session)
        if config.resolver_kind is DirectResolverKind.TRAWL
    )
    if not configs:
        raise DirectResolverServiceError(
            "trawl_resolver_required",
            "DataNodes requires an enabled and healthy TRAWL resolver.",
        )
    return await _resolve_with_chain(
        configs,
        target_url=target_url,
        declared_domains=declared_domains,
        challenge_category=challenge_category,
        scope=adapter_id,
        native_trawl=True,
        client_factory=client_factory,
        on_attempt=on_attempt,
    )


async def _eligible_resolvers(
    session: AsyncSession,
) -> tuple[DirectResolverConfig, ...]:
    result = await session.execute(
        select(DirectResolverConfig)
        .where(
            DirectResolverConfig.enabled.is_(True),
            DirectResolverConfig.state.in_(
                (DirectResolverState.HEALTHY, DirectResolverState.DEGRADED)
            ),
        )
        .order_by(
            DirectResolverConfig.priority,
            DirectResolverConfig.resolver_kind,
            DirectResolverConfig.id,
        )
    )
    return tuple(result.scalars().all())


async def _resolve_with_chain(
    configs: Sequence[DirectResolverConfig],
    *,
    target_url: str,
    declared_domains: Sequence[str],
    challenge_category: str,
    scope: str,
    native_trawl: bool,
    client_factory: DirectResolverClientFactory,
    on_attempt: ResolverAttemptCallback | None,
) -> DirectResolverResult:
    if not configs:
        raise DirectResolverServiceError(
            "resolver_unavailable",
            "A healthy browser resolver is not configured.",
        )
    total = len(configs)
    last_error: DirectResolverError | None = None
    for attempt, config in enumerate(configs, start=1):
        if on_attempt is not None:
            await on_attempt(
                ResolverAttemptProgress(
                    resolver_id=config.id,
                    resolver_name=config.name,
                    resolver_kind=config.resolver_kind,
                    attempt=attempt,
                    total=total,
                    scope=scope,
                )
            )
        client = _client_for_config(config, client_factory)
        try:
            if native_trawl:
                return await client.solve_trawl_native(
                    target_url,
                    declared_domains=declared_domains,
                    challenge_category=challenge_category,
                )
            return await client.solve(
                target_url,
                declared_domains=declared_domains,
                challenge_category=challenge_category,
            )
        except DirectResolverError as exc:
            last_error = exc
            if exc.code == "resolver_target_rejected":
                raise DirectResolverServiceError(exc.code, str(exc)) from exc
        finally:
            await client.aclose()
    message = (
        "Every compatible browser resolver failed this request."
        if last_error is None
        else "Every compatible browser resolver failed this request. "
        "Check resolver health and retry."
    )
    raise DirectResolverServiceError(
        "resolver_chain_exhausted",
        message,
        retryable=bool(last_error and last_error.retryable),
        cause_code=last_error.code if last_error is not None else None,
    ) from last_error


async def _get_or_create(session: AsyncSession) -> DirectResolverConfig:
    config = await session.scalar(
        select(DirectResolverConfig).where(DirectResolverConfig.name == _RESOLVER_NAME)
    )
    if config is None:
        config = DirectResolverConfig(name=_RESOLVER_NAME)
        session.add(config)
        await session.flush()
    return config


async def _resolver_by_id(session: AsyncSession, resolver_id: int) -> DirectResolverConfig:
    config = await session.get(DirectResolverConfig, resolver_id)
    if config is None:
        raise DirectResolverServiceError(
            "resolver_not_found",
            "The browser resolver profile was not found.",
        )
    return config


def _client_for_config(
    config: DirectResolverConfig,
    factory: DirectResolverClientFactory,
) -> DirectResolverClientProtocol:
    return factory(
        endpoint=config.endpoint,
        allow_private_http=bool(config.allow_private_http),
        authentication_headers=load_resolver_auth_headers(config).headers,
        timeout_seconds=float(config.timeout_seconds),
        max_concurrency=config.max_concurrency,
        circuit_breaker=_get_resolver_runtime(config),
    )


def _get_resolver_runtime(config: DirectResolverConfig) -> ResolverCircuitBreaker:
    key = (config.id, config.endpoint, config.max_concurrency)
    runtime = _resolver_runtimes.get(key)
    if runtime is None:
        runtime = ResolverCircuitBreaker(max_concurrency=config.max_concurrency)
        _resolver_runtimes[key] = runtime
    return runtime


def _clear_resolver_runtime() -> None:
    _resolver_runtimes.clear()


def _validate_resolver_limits(
    *,
    priority: int,
    timeout_seconds: int,
    max_concurrency: int,
) -> None:
    if priority < 1 or priority > 1000:
        raise DirectResolverServiceError(
            "invalid_resolver_priority",
            "Resolver priority must be between 1 and 1000.",
        )
    if timeout_seconds < 1 or timeout_seconds > 60:
        raise DirectResolverServiceError(
            "invalid_resolver_timeout",
            "Resolver timeout must be between 1 and 60 seconds.",
        )
    if max_concurrency < 1 or max_concurrency > 4:
        raise DirectResolverServiceError(
            "invalid_resolver_concurrency",
            "Resolver concurrency must be between 1 and 4.",
        )


async def _validate_endpoint(
    *,
    endpoint: str,
    enabled: bool,
    allow_private_http: bool,
    timeout_seconds: int,
    max_concurrency: int,
    authentication_headers: Mapping[str, str | None],
    client_factory: DirectResolverClientFactory,
) -> str:
    normalized = endpoint.strip().rstrip("/")
    if enabled and not normalized:
        raise DirectResolverServiceError(
            "resolver_endpoint_required",
            "Configure a resolver endpoint before enabling it.",
        )
    if not normalized:
        return ""
    headers = {name: value for name, value in authentication_headers.items() if value is not None}
    client = client_factory(
        endpoint=normalized,
        allow_private_http=allow_private_http,
        authentication_headers=headers,
        timeout_seconds=float(timeout_seconds),
        max_concurrency=max_concurrency,
    )
    try:
        validated = await client.validate_endpoint()
        return validated.url
    except DirectResolverError as exc:
        raise DirectResolverServiceError(exc.code, str(exc)) from exc
    finally:
        await client.aclose()


def _state_for_error(code: str) -> DirectResolverState:
    if "authentication" in code:
        return DirectResolverState.AUTHENTICATION_REQUIRED
    if "malformed" in code or "incompatible" in code:
        return DirectResolverState.INCOMPATIBLE
    return DirectResolverState.UNAVAILABLE


def _test_failure_message(state: DirectResolverState) -> str:
    if state is DirectResolverState.AUTHENTICATION_REQUIRED:
        return "Resolver rejected its configured authentication."
    if state is DirectResolverState.INCOMPATIBLE:
        return "Resolver did not return a compatible standard /v1 response."
    return "Resolver could not complete the bounded connection test."
