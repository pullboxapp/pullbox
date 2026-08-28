"""Manual registration lifecycle for external direct-download providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import select

from pullbox.core.exceptions import ValidationError
from pullbox.models.direct_acquisition import (
    DirectProviderConfig,
    DirectProviderState,
    DirectProviderTrustLevel,
)
from pullbox.providers.direct.client import DirectProviderClient, DirectProviderClientError
from pullbox.providers.direct.contract import (
    DirectConfigurationControl,
    DirectHealthResponse,
    DirectManifestResponse,
    DirectProviderStatus,
    negotiate_direct_provider_protocol,
)
from pullbox.services.direct_configuration_service import (
    load_provider_secret_material,
    update_provider_configuration_secrets,
    write_provider_bearer_token,
)
from pullbox.services.direct_provider_quota import (
    automatic_quota_reserve,
    provider_quota_status,
    provider_supports_quota,
    set_automatic_quota_reserve,
)
from pullbox.services.direct_provider_source_origin import (
    DirectProviderSourceOriginError,
    configured_direct_provider_source_domain,
    validate_direct_provider_source_origin,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.providers.direct.endpoint import (
        ProviderEndpointResolver,
        ValidatedProviderEndpoint,
    )


class DirectProviderRegistrationError(RuntimeError):
    """An expected, safe provider lifecycle failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class DirectProviderRegistrationInput:
    """Operator-supplied values for a new provider registration."""

    endpoint: str
    bearer_token: str = field(repr=False)
    allow_private_http: bool = False
    confirm_custom_provider: bool = False
    priority: int = 50


@dataclass(frozen=True, slots=True)
class DirectProviderRecord:
    """Secret-free provider projection shared by APIs and settings UI."""

    id: int
    provider_id: str
    display_name: str
    endpoint: str
    enabled: bool
    priority: int
    state: DirectProviderState
    negotiated_protocol: str | None
    trust_level: DirectProviderTrustLevel
    bearer_token_configured: bool
    resolver_enabled: bool
    provider_version: str | None
    publisher: str | None
    artifact_host_patterns: tuple[str, ...]
    configuration_controls: tuple[DirectConfigurationControl, ...]
    public_configuration: dict[str, str | int | float | bool]
    configured_secret_fields: tuple[str, ...]
    last_health_at: datetime | None
    last_tested_at: datetime | None
    last_error_code: str | None
    quota_supported: bool
    quota_remaining: int | None
    quota_limit: int | None
    quota_window_seconds: int | None
    quota_reset_at: datetime | None
    quota_observed_at: datetime | None
    automatic_quota_reserve: int


@dataclass(frozen=True, slots=True)
class DirectProviderTestResult:
    """One explicit provider connection/compatibility test result."""

    usable: bool
    state: DirectProviderState
    message: str
    checked_at: datetime


class DirectProviderClientProtocol(Protocol):
    async def validate_endpoint(self) -> ValidatedProviderEndpoint: ...

    async def manifest(self) -> DirectManifestResponse: ...

    async def health(self) -> DirectHealthResponse: ...

    async def aclose(self) -> None: ...


class DirectProviderClientFactory(Protocol):
    def __call__(
        self,
        *,
        endpoint: str,
        bearer_token: str,
        allow_private_http: bool,
    ) -> DirectProviderClientProtocol: ...


def _default_client_factory(
    *,
    endpoint: str,
    bearer_token: str,
    allow_private_http: bool,
) -> DirectProviderClient:
    return DirectProviderClient(
        endpoint=endpoint,
        bearer_token=bearer_token,
        allow_private_http=allow_private_http,
    )


async def register_direct_provider(
    session: AsyncSession,
    registration: DirectProviderRegistrationInput,
    *,
    client_factory: DirectProviderClientFactory = _default_client_factory,
) -> DirectProviderRecord:
    """Validate and persist a disabled provider registration."""
    if registration.priority < 0 or registration.priority > 1_000:
        raise DirectProviderRegistrationError(
            "invalid_provider_priority",
            "Provider priority must be between 0 and 1000.",
        )
    client = client_factory(
        endpoint=registration.endpoint,
        bearer_token=registration.bearer_token,
        allow_private_http=registration.allow_private_http,
    )
    try:
        endpoint = await client.validate_endpoint()
        duplicate_endpoint = await session.scalar(
            select(DirectProviderConfig).where(DirectProviderConfig.endpoint == endpoint.url)
        )
        if duplicate_endpoint is not None:
            raise DirectProviderRegistrationError(
                "provider_endpoint_already_registered",
                "This provider endpoint is already registered.",
            )
        manifest = await client.manifest()
        negotiated = _negotiate_manifest(manifest)
        # Manual registration proves endpoint reachability, not publisher identity.
        # Reserved manifest IDs remain custom until Pullbox has an authenticated
        # endpoint or signed-manifest binding for first-party providers.
        trust_level = DirectProviderTrustLevel.CUSTOM
        if (
            trust_level == DirectProviderTrustLevel.CUSTOM
            and not registration.confirm_custom_provider
        ):
            raise DirectProviderRegistrationError(
                "custom_provider_confirmation_required",
                "Confirm the custom-provider trust warning before registration.",
            )
        duplicate_identity = await session.scalar(
            select(DirectProviderConfig).where(
                DirectProviderConfig.provider_id == manifest.provider_id
            )
        )
        if duplicate_identity is not None:
            raise DirectProviderRegistrationError(
                "provider_identity_already_registered",
                "This provider identity is already registered at another endpoint.",
            )
        health = await client.health()
    except DirectProviderRegistrationError:
        raise
    except DirectProviderClientError as exc:
        raise DirectProviderRegistrationError(exc.code, str(exc)) from exc
    finally:
        await client.aclose()

    checked_at = datetime.now(UTC)
    config = DirectProviderConfig(
        provider_id=manifest.provider_id,
        display_name=manifest.display_name,
        endpoint=endpoint.url,
        enabled=False,
        priority=registration.priority,
        state=DirectProviderState.DISABLED,
        negotiated_protocol=negotiated,
        trust_level=trust_level,
        configuration_metadata={
            "allow_private_http": registration.allow_private_http,
            "public_values": {},
            "configured_secret_fields": [],
        },
        manifest_snapshot=manifest.model_dump(mode="json"),
        last_health_at=checked_at,
        last_tested_at=checked_at,
        last_error_code=_health_error_code(health),
    )
    write_provider_bearer_token(config, registration.bearer_token)
    config.last_health_at = checked_at
    config.last_tested_at = checked_at
    config.last_error_code = _health_error_code(health)
    session.add(config)
    await session.flush()
    return _record(config)


async def list_direct_providers(session: AsyncSession) -> list[DirectProviderRecord]:
    """List registered providers by explicit priority and stable identity."""
    result = await session.execute(
        select(DirectProviderConfig).order_by(
            DirectProviderConfig.priority,
            DirectProviderConfig.display_name,
            DirectProviderConfig.id,
        )
    )
    return [_record(config) for config in result.scalars().all()]


async def list_usable_direct_providers(session: AsyncSession) -> list[DirectProviderRecord]:
    """Return only explicitly enabled providers that may enter search fan-out."""
    result = await session.execute(
        select(DirectProviderConfig)
        .where(
            DirectProviderConfig.enabled.is_(True),
            DirectProviderConfig.state.in_(
                (DirectProviderState.HEALTHY, DirectProviderState.DEGRADED)
            ),
        )
        .order_by(
            DirectProviderConfig.priority,
            DirectProviderConfig.display_name,
            DirectProviderConfig.id,
        )
    )
    return [_record(config) for config in result.scalars().all()]


async def get_direct_provider(
    session: AsyncSession,
    provider_config_id: int,
) -> DirectProviderRecord:
    config = await _get_config(session, provider_config_id)
    return _record(config)


async def test_direct_provider(
    session: AsyncSession,
    provider_config_id: int,
    *,
    client_factory: DirectProviderClientFactory = _default_client_factory,
) -> DirectProviderTestResult:
    """Revalidate identity, compatibility, and health using stored credentials."""
    config = await _get_config(session, provider_config_id)
    checked_at = datetime.now(UTC)
    try:
        material = load_provider_secret_material(config)
        if not material.bearer_token:
            raise DirectProviderClientError(
                "provider_authentication_failed",
                "Provider bearer token is not configured.",
            )
        client = client_factory(
            endpoint=config.endpoint,
            bearer_token=material.bearer_token,
            allow_private_http=_allow_private_http(config),
        )
        try:
            await client.validate_endpoint()
            manifest = await client.manifest()
            if manifest.provider_id != config.provider_id:
                raise DirectProviderClientError(
                    "provider_identity_changed",
                    "Provider identity changed since registration.",
                )
            negotiated = _negotiate_manifest(manifest)
            health = await client.health()
        finally:
            await client.aclose()
    except (DirectProviderClientError, DirectProviderRegistrationError, ValueError) as exc:
        code = getattr(exc, "code", "provider_configuration_unreadable")
        message = getattr(exc, "message", "Provider configuration could not be read.")
        state = _state_for_error(str(code))
        config.state = state
        config.last_tested_at = checked_at
        config.last_error_code = str(code)
        await session.flush()
        return DirectProviderTestResult(False, state, str(message), checked_at)

    configured_source_domain = _configured_source_domain(config)
    state, usable = _state_from_health(
        health,
        configured_source_domain=configured_source_domain,
    )
    config.negotiated_protocol = negotiated
    config.display_name = manifest.display_name
    config.manifest_snapshot = manifest.model_dump(mode="json")
    config.last_health_at = checked_at
    config.last_tested_at = checked_at
    config.last_error_code = _health_error_code(
        health,
        configured_source_domain=configured_source_domain,
    )
    config.state = state
    await session.flush()
    message = (
        "The configured source domain is currently unreachable."
        if config.last_error_code == "provider_source_unavailable"
        else health.message
    )
    return DirectProviderTestResult(usable, state, message, checked_at)


async def enable_direct_provider(
    session: AsyncSession,
    provider_config_id: int,
    *,
    client_factory: DirectProviderClientFactory = _default_client_factory,
) -> DirectProviderRecord:
    """Enable a provider only after required configuration and a usable test."""
    config = await _get_config(session, provider_config_id)
    _require_complete_configuration(config)
    result = await test_direct_provider(
        session,
        provider_config_id,
        client_factory=client_factory,
    )
    if not result.usable:
        config.enabled = False
        await session.flush()
        raise DirectProviderRegistrationError(
            "provider_not_usable",
            "Provider must report healthy or degraded before it can be enabled.",
        )
    config.enabled = True
    config.state = result.state
    await session.flush()
    return _record(config)


async def disable_direct_provider(
    session: AsyncSession,
    provider_config_id: int,
) -> DirectProviderRecord:
    """Disable future provider use without deleting provenance."""
    config = await _get_config(session, provider_config_id)
    config.enabled = False
    config.state = DirectProviderState.DISABLED
    await session.flush()
    return _record(config)


async def update_direct_provider(
    session: AsyncSession,
    provider_config_id: int,
    *,
    priority: int | None = None,
    bearer_token: str | None = None,
    public_configuration: Mapping[str, object] | None = None,
    secret_configuration: Mapping[str, str | None] | None = None,
    resolver_enabled: bool | None = None,
    automatic_quota_reserve_value: int | None = None,
    source_origin_resolver: ProviderEndpointResolver | None = None,
) -> DirectProviderRecord:
    """Update native provider settings while keeping secrets write-only."""
    config = await _get_config(session, provider_config_id)
    if priority is not None:
        if priority < 0 or priority > 1_000:
            raise DirectProviderRegistrationError(
                "invalid_provider_priority",
                "Provider priority must be between 0 and 1000.",
            )
        config.priority = priority
    configuration_changed = False
    if bearer_token is not None:
        write_provider_bearer_token(config, bearer_token)
        configuration_changed = True
    if public_configuration is not None or secret_configuration is not None:
        manifest = _stored_manifest(config)
        controls = {control.name: control for control in manifest.configuration_controls}
        if public_configuration is not None:
            public_values = await _validate_public_configuration(
                public_configuration,
                controls,
                source_origin_resolver=source_origin_resolver,
            )
            metadata = _metadata(config)
            metadata["public_values"] = public_values
            config.configuration_metadata = metadata
            configuration_changed = True
        if secret_configuration is not None:
            _validate_secret_configuration(secret_configuration, controls)
            try:
                update_provider_configuration_secrets(config, secret_configuration)
            except ValidationError as exc:
                raise DirectProviderRegistrationError(
                    "invalid_provider_configuration",
                    str(exc),
                ) from exc
            configuration_changed = True
    if resolver_enabled is not None:
        config.resolver_enabled = resolver_enabled
    if automatic_quota_reserve_value is not None:
        try:
            set_automatic_quota_reserve(config, automatic_quota_reserve_value)
        except ValueError as exc:
            raise DirectProviderRegistrationError(
                "invalid_automatic_quota_reserve",
                str(exc),
            ) from exc
    if configuration_changed:
        config.enabled = False
        config.state = DirectProviderState.DISABLED
    await session.flush()
    return _record(config)


async def remove_direct_provider(session: AsyncSession, provider_config_id: int) -> None:
    """Remove registration while database FKs preserve detached attempt history."""
    config = await _get_config(session, provider_config_id)
    await session.delete(config)
    await session.flush()


async def _get_config(session: AsyncSession, provider_config_id: int) -> DirectProviderConfig:
    config = await session.get(DirectProviderConfig, provider_config_id)
    if config is None:
        raise DirectProviderRegistrationError("provider_not_found", "Provider was not found.")
    return config


def _negotiate_manifest(manifest: DirectManifestResponse) -> str:
    try:
        return negotiate_direct_provider_protocol(manifest.supported_protocol_versions)
    except ValueError as exc:
        raise DirectProviderRegistrationError("provider_incompatible", str(exc)) from exc


def _state_from_health(
    health: DirectHealthResponse,
    *,
    configured_source_domain: str | None = None,
) -> tuple[DirectProviderState, bool]:
    if health.process_status == DirectProviderStatus.HEALTHY and _configured_source_is_unreachable(
        health, configured_source_domain
    ):
        return DirectProviderState.DEGRADED, False
    status = (
        health.process_status
        if health.process_status != DirectProviderStatus.HEALTHY
        else health.source_status
    )
    mapping = {
        DirectProviderStatus.HEALTHY: (DirectProviderState.HEALTHY, True),
        DirectProviderStatus.DEGRADED: (DirectProviderState.DEGRADED, True),
        DirectProviderStatus.RATE_LIMITED: (DirectProviderState.RATE_LIMITED, False),
        DirectProviderStatus.AUTHENTICATION_REQUIRED: (
            DirectProviderState.AUTHENTICATION_REQUIRED,
            False,
        ),
        DirectProviderStatus.INCOMPATIBLE: (DirectProviderState.INCOMPATIBLE, False),
    }
    return mapping.get(status, (DirectProviderState.UNAVAILABLE, False))


def _health_error_code(
    health: DirectHealthResponse,
    *,
    configured_source_domain: str | None = None,
) -> str | None:
    if _configured_source_is_unreachable(health, configured_source_domain):
        return "provider_source_unavailable"
    state, usable = _state_from_health(
        health,
        configured_source_domain=configured_source_domain,
    )
    return None if usable else state.value


def _configured_source_domain(config: DirectProviderConfig) -> str | None:
    return configured_direct_provider_source_domain(config)


def _configured_source_is_unreachable(
    health: DirectHealthResponse,
    configured_source_domain: str | None,
) -> bool:
    if not configured_source_domain:
        return False
    value = health.diagnostics.get(f"source.{configured_source_domain}")
    return isinstance(value, str) and value.casefold() == "unreachable"


def _state_for_error(code: str) -> DirectProviderState:
    if "authentication" in code:
        return DirectProviderState.AUTHENTICATION_REQUIRED
    if "incompatible" in code or "identity_changed" in code:
        return DirectProviderState.INCOMPATIBLE
    return DirectProviderState.UNAVAILABLE


def _stored_manifest(config: DirectProviderConfig) -> DirectManifestResponse:
    try:
        return DirectManifestResponse.model_validate(config.manifest_snapshot)
    except ValueError as exc:
        raise DirectProviderRegistrationError(
            "provider_manifest_unreadable",
            "Saved provider manifest is invalid; test the connection again.",
        ) from exc


def _allow_private_http(config: DirectProviderConfig) -> bool:
    return bool(_metadata(config).get("allow_private_http", False))


def _metadata(config: DirectProviderConfig) -> dict[str, object]:
    return (
        dict(config.configuration_metadata)
        if isinstance(config.configuration_metadata, dict)
        else {}
    )


def _record(config: DirectProviderConfig) -> DirectProviderRecord:
    manifest: DirectManifestResponse | None
    try:
        manifest = _stored_manifest(config)
    except DirectProviderRegistrationError:
        manifest = None
    metadata = _metadata(config)
    raw_public = metadata.get("public_values", {})
    public_values = (
        {
            str(key): value
            for key, value in raw_public.items()
            if isinstance(key, str) and isinstance(value, (str, int, float, bool))
        }
        if isinstance(raw_public, dict)
        else {}
    )
    raw_secret_fields = metadata.get("configured_secret_fields", [])
    secret_fields = (
        tuple(sorted(item for item in raw_secret_fields if isinstance(item, str)))
        if isinstance(raw_secret_fields, list)
        else ()
    )
    if config.id is None:
        raise ValueError("Direct provider must be persisted before it can be read.")
    quota = provider_quota_status(config)
    return DirectProviderRecord(
        id=config.id,
        provider_id=config.provider_id,
        display_name=config.display_name,
        endpoint=config.endpoint,
        enabled=bool(config.enabled),
        priority=config.priority,
        state=config.state,
        negotiated_protocol=config.negotiated_protocol,
        trust_level=config.trust_level,
        bearer_token_configured=bool(config.encrypted_bearer_token),
        resolver_enabled=bool(config.resolver_enabled),
        provider_version=manifest.provider_version if manifest else None,
        publisher=manifest.publisher if manifest else None,
        artifact_host_patterns=tuple(manifest.artifact_host_patterns) if manifest else (),
        configuration_controls=manifest.configuration_controls if manifest else (),
        public_configuration=public_values,
        configured_secret_fields=secret_fields,
        last_health_at=config.last_health_at,
        last_tested_at=config.last_tested_at,
        last_error_code=config.last_error_code,
        quota_supported=provider_supports_quota(config),
        quota_remaining=quota.remaining if quota else None,
        quota_limit=quota.limit if quota else None,
        quota_window_seconds=quota.window_seconds if quota else None,
        quota_reset_at=quota.reset_at if quota else None,
        quota_observed_at=quota.observed_at if quota else None,
        automatic_quota_reserve=automatic_quota_reserve(config),
    )


async def _validate_public_configuration(
    values: Mapping[str, object],
    controls: Mapping[str, DirectConfigurationControl],
    *,
    source_origin_resolver: ProviderEndpointResolver | None,
) -> dict[str, str | int | float | bool]:
    result: dict[str, str | int | float | bool] = {}
    for name, value in values.items():
        control = controls.get(name)
        if control is None:
            raise DirectProviderRegistrationError(name, "Unknown provider configuration field.")
        if control.secret:
            raise DirectProviderRegistrationError(name, "Secret field requires a write-only value.")
        _validate_control_value(control, value)
        if control.source_origin and isinstance(value, str):
            try:
                value = (
                    await validate_direct_provider_source_origin(
                        value,
                        resolver=source_origin_resolver,
                    )
                ).url
            except DirectProviderSourceOriginError as exc:
                raise DirectProviderRegistrationError(name, str(exc)) from exc
        if not isinstance(value, (str, int, float, bool)):
            raise DirectProviderRegistrationError(name, "Provider configuration value is invalid.")
        result[name] = value
    return result


def _validate_secret_configuration(
    values: Mapping[str, str | None],
    controls: Mapping[str, DirectConfigurationControl],
) -> None:
    for name, value in values.items():
        control = controls.get(name)
        if control is None:
            raise DirectProviderRegistrationError(name, "Unknown provider configuration field.")
        if not control.secret:
            raise DirectProviderRegistrationError(name, "Field is not a secret control.")
        if value is not None:
            _validate_control_value(control, value)


def _validate_control_value(control: DirectConfigurationControl, value: object) -> None:
    valid_type = (
        (control.value_type == "string" and isinstance(value, str))
        or (control.value_type == "boolean" and isinstance(value, bool))
        or (
            control.value_type == "integer"
            and isinstance(value, int)
            and not isinstance(value, bool)
        )
        or (
            control.value_type == "number"
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        )
    )
    if not valid_type:
        raise DirectProviderRegistrationError(
            control.name,
            "Provider configuration value has the wrong type.",
        )
    if control.choices and value not in control.choices:
        raise DirectProviderRegistrationError(
            control.name, "Provider configuration choice is invalid."
        )
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if control.minimum is not None and value < control.minimum:
            raise DirectProviderRegistrationError(control.name, "Provider value is below minimum.")
        if control.maximum is not None and value > control.maximum:
            raise DirectProviderRegistrationError(control.name, "Provider value exceeds maximum.")
    if isinstance(value, str):
        if control.min_length is not None and len(value) < control.min_length:
            raise DirectProviderRegistrationError(control.name, "Provider value is too short.")
        if control.max_length is not None and len(value) > control.max_length:
            raise DirectProviderRegistrationError(control.name, "Provider value is too long.")


def _require_complete_configuration(config: DirectProviderConfig) -> None:
    manifest = _stored_manifest(config)
    metadata = _metadata(config)
    public_values = metadata.get("public_values", {})
    secret_fields = metadata.get("configured_secret_fields", [])
    configured = set(public_values) if isinstance(public_values, dict) else set()
    if isinstance(secret_fields, list):
        configured.update(item for item in secret_fields if isinstance(item, str))
    missing = [
        control.name
        for control in manifest.configuration_controls
        if control.required and control.default is None and control.name not in configured
    ]
    if missing:
        raise DirectProviderRegistrationError(
            "provider_configuration_required",
            f"Configure required provider field: {missing[0]}.",
        )
