from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import func, select

from pullbox.core.encryption import _get_fernet, decrypt_secret, is_encrypted
from pullbox.models.direct_acquisition import (
    DirectProviderConfig,
    DirectProviderState,
    DirectProviderTrustLevel,
)
from pullbox.providers.direct.client import DirectProviderClientError
from pullbox.providers.direct.contract import (
    DIRECT_PROVIDER_PROTOCOL_V1,
    DirectHealthResponse,
    DirectManifestResponse,
)
from pullbox.providers.direct.endpoint import ValidatedProviderEndpoint
from pullbox.services.direct_provider_registration import (
    DirectProviderRegistrationError,
    DirectProviderRegistrationInput,
    enable_direct_provider,
    list_direct_providers,
    list_usable_direct_providers,
    register_direct_provider,
    remove_direct_provider,
    update_direct_provider,
)
from pullbox.services.direct_provider_registration import (
    test_direct_provider as run_direct_provider_test,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


async def _public_source_resolver(_host: str, _port: int) -> Sequence[str]:
    return ("93.184.216.34",)


async def _private_source_resolver(_host: str, _port: int) -> Sequence[str]:
    return ("127.0.0.1",)


async def _mixed_source_resolver(_host: str, _port: int) -> Sequence[str]:
    return ("93.184.216.34", "127.0.0.1")


async def _unresolved_source_resolver(_host: str, _port: int) -> Sequence[str]:
    raise OSError("source is temporarily unavailable")


def _manifest(
    *,
    provider_id: str = "community.example",
    configuration_schema: dict[str, object] | None = None,
) -> DirectManifestResponse:
    return DirectManifestResponse.model_validate(
        {
            "protocol_version": DIRECT_PROVIDER_PROTOCOL_V1,
            "provider_id": provider_id,
            "display_name": "Example Provider",
            "description": "A deterministic provider fixture.",
            "provider_version": "1.0.0",
            "supported_protocol_versions": [DIRECT_PROVIDER_PROTOCOL_V1],
            "publisher": "Example Publisher",
            "license": "GPL-3.0-or-later",
            "source_domains": ["example.test"],
            "capabilities": {
                "search": True,
                "resolve": True,
                "browser_challenge": False,
                "health": True,
                "quota": False,
                "configuration_schema": bool(configuration_schema),
            },
            "configuration_schema": configuration_schema
            or {"type": "object", "properties": {}, "additionalProperties": False},
        }
    )


def _health(
    *,
    process_status: str = "healthy",
    source_status: str = "healthy",
    diagnostics: dict[str, object] | None = None,
) -> DirectHealthResponse:
    return DirectHealthResponse.model_validate(
        {
            "protocol_version": DIRECT_PROVIDER_PROTOCOL_V1,
            "process_status": process_status,
            "source_status": source_status,
            "message": "Provider health checked.",
            "retry_after_seconds": None,
            "diagnostics": diagnostics or {},
        }
    )


class _FakeProviderClient:
    manifest_response: ClassVar[DirectManifestResponse] = _manifest()
    health_response: ClassVar[DirectHealthResponse] = _health()
    failure: ClassVar[DirectProviderClientError | None] = None
    instances: ClassVar[list[_FakeProviderClient]] = []

    def __init__(
        self,
        *,
        endpoint: str,
        bearer_token: str,
        allow_private_http: bool,
    ) -> None:
        self.endpoint = endpoint
        self.bearer_token = bearer_token
        self.allow_private_http = allow_private_http
        self.closed = False
        self.instances.append(self)

    async def validate_endpoint(self) -> ValidatedProviderEndpoint:
        if self.failure:
            raise self.failure
        return ValidatedProviderEndpoint(
            url=self.endpoint.rstrip("/"),
            host="provider",
            port=8780,
            addresses=("172.20.0.8",),
            private_network=True,
            insecure_transport=self.endpoint.startswith("http://"),
        )

    async def manifest(self) -> DirectManifestResponse:
        if self.failure:
            raise self.failure
        return self.manifest_response

    async def health(self) -> DirectHealthResponse:
        if self.failure:
            raise self.failure
        return self.health_response

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _registration_fixtures() -> None:
    provider = MagicMock()
    provider.secret_key.return_value = "direct-provider-registration-test-secret"
    _get_fernet.cache_clear()
    _FakeProviderClient.manifest_response = _manifest()
    _FakeProviderClient.health_response = _health()
    _FakeProviderClient.failure = None
    _FakeProviderClient.instances = []
    with patch("pullbox.core.config_file.get_config_provider", return_value=provider):
        yield
    _get_fernet.cache_clear()


def _factory(**kwargs: object) -> _FakeProviderClient:
    return _FakeProviderClient(**kwargs)  # type: ignore[arg-type]


async def test_custom_provider_requires_explicit_confirmation_before_persistence(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(DirectProviderRegistrationError) as exc_info:
        await register_direct_provider(
            db_session,
            DirectProviderRegistrationInput(
                endpoint="http://provider:8780",
                bearer_token="registration-token-with-sufficient-length",
                allow_private_http=True,
                confirm_custom_provider=False,
            ),
            client_factory=_factory,
        )

    assert exc_info.value.code == "custom_provider_confirmation_required"
    count = await db_session.scalar(select(func.count()).select_from(DirectProviderConfig))
    assert count == 0


async def test_self_declared_pullbox_identity_is_still_treated_as_custom(
    db_session: AsyncSession,
) -> None:
    _FakeProviderClient.manifest_response = _manifest(provider_id="pullbox.getcomics")

    with pytest.raises(DirectProviderRegistrationError) as exc_info:
        await register_direct_provider(
            db_session,
            DirectProviderRegistrationInput(
                endpoint="http://untrusted-provider:8780",
                bearer_token="registration-token-with-sufficient-length",
                allow_private_http=True,
                confirm_custom_provider=False,
            ),
            client_factory=_factory,
        )

    assert exc_info.value.code == "custom_provider_confirmation_required"
    count = await db_session.scalar(select(func.count()).select_from(DirectProviderConfig))
    assert count == 0


async def test_registration_persists_redacted_manifest_and_encrypted_token(
    db_session: AsyncSession,
) -> None:
    registered = await register_direct_provider(
        db_session,
        DirectProviderRegistrationInput(
            endpoint="http://provider:8780/",
            bearer_token="registration-token-with-sufficient-length",
            allow_private_http=True,
            confirm_custom_provider=True,
            priority=25,
        ),
        client_factory=_factory,
    )
    await db_session.flush()
    stored = await db_session.get(DirectProviderConfig, registered.id)

    assert stored is not None
    assert stored.endpoint == "http://provider:8780"
    assert stored.enabled is False
    assert stored.state == DirectProviderState.DISABLED
    assert stored.negotiated_protocol == DIRECT_PROVIDER_PROTOCOL_V1
    assert stored.trust_level == DirectProviderTrustLevel.CUSTOM
    assert stored.priority == 25
    assert is_encrypted(stored.encrypted_bearer_token)
    assert decrypt_secret(stored.encrypted_bearer_token) == (
        "registration-token-with-sufficient-length"
    )
    assert "registration-token" not in str(stored.manifest_snapshot)
    assert registered.bearer_token_configured is True
    assert _FakeProviderClient.instances[-1].closed is True


async def test_registration_rejects_duplicate_endpoint_and_provider_identity(
    db_session: AsyncSession,
) -> None:
    request = DirectProviderRegistrationInput(
        endpoint="http://provider:8780",
        bearer_token="registration-token-with-sufficient-length",
        allow_private_http=True,
        confirm_custom_provider=True,
    )
    await register_direct_provider(db_session, request, client_factory=_factory)
    await db_session.flush()

    with pytest.raises(DirectProviderRegistrationError) as endpoint_error:
        await register_direct_provider(db_session, request, client_factory=_factory)
    assert endpoint_error.value.code == "provider_endpoint_already_registered"

    with pytest.raises(DirectProviderRegistrationError) as identity_error:
        await register_direct_provider(
            db_session,
            DirectProviderRegistrationInput(
                endpoint="http://provider-two:8780",
                bearer_token="another-registration-token-with-length",
                allow_private_http=True,
                confirm_custom_provider=True,
            ),
            client_factory=_factory,
        )
    assert identity_error.value.code == "provider_identity_already_registered"


async def test_search_registry_returns_only_enabled_usable_providers_in_priority_order(
    db_session: AsyncSession,
) -> None:
    states = (
        ("healthy", DirectProviderState.HEALTHY, True, 20),
        ("degraded", DirectProviderState.DEGRADED, True, 10),
        ("disabled", DirectProviderState.HEALTHY, False, 1),
        ("rate-limited", DirectProviderState.RATE_LIMITED, True, 2),
        ("unavailable", DirectProviderState.UNAVAILABLE, True, 3),
        ("incompatible", DirectProviderState.INCOMPATIBLE, True, 4),
    )
    for name, state, enabled, priority in states:
        db_session.add(
            DirectProviderConfig(
                provider_id=f"community.{name}",
                display_name=name.title(),
                endpoint=f"https://{name}.example",
                enabled=enabled,
                priority=priority,
                state=state,
                trust_level=DirectProviderTrustLevel.CUSTOM,
                negotiated_protocol=DIRECT_PROVIDER_PROTOCOL_V1,
                manifest_snapshot=_manifest(provider_id=f"community.{name}").model_dump(
                    mode="json"
                ),
            )
        )
    await db_session.flush()

    usable = await list_usable_direct_providers(db_session)

    assert [provider.provider_id for provider in usable] == [
        "community.degraded",
        "community.healthy",
    ]


async def test_provider_must_pass_usable_health_before_enablement(
    db_session: AsyncSession,
) -> None:
    registered = await register_direct_provider(
        db_session,
        DirectProviderRegistrationInput(
            endpoint="http://provider:8780",
            bearer_token="registration-token-with-sufficient-length",
            allow_private_http=True,
            confirm_custom_provider=True,
        ),
        client_factory=_factory,
    )
    _FakeProviderClient.health_response = _health(source_status="authentication_required")

    with pytest.raises(DirectProviderRegistrationError) as exc_info:
        await enable_direct_provider(
            db_session,
            registered.id,
            client_factory=_factory,
        )

    assert exc_info.value.code == "provider_not_usable"
    stored = await db_session.get(DirectProviderConfig, registered.id)
    assert stored is not None
    assert stored.enabled is False

    _FakeProviderClient.health_response = _health(source_status="degraded")
    enabled = await enable_direct_provider(
        db_session,
        registered.id,
        client_factory=_factory,
    )
    assert enabled.enabled is True
    assert enabled.state == DirectProviderState.DEGRADED


async def test_failed_connection_test_is_classified_and_never_exposes_token(
    db_session: AsyncSession,
) -> None:
    registered = await register_direct_provider(
        db_session,
        DirectProviderRegistrationInput(
            endpoint="http://provider:8780",
            bearer_token="registration-token-with-sufficient-length",
            allow_private_http=True,
            confirm_custom_provider=True,
        ),
        client_factory=_factory,
    )
    _FakeProviderClient.failure = DirectProviderClientError(
        "provider_authentication_failed",
        "Provider rejected its bearer token.",
    )

    result = await run_direct_provider_test(
        db_session,
        registered.id,
        client_factory=_factory,
    )

    assert result.usable is False
    assert result.state == DirectProviderState.AUTHENTICATION_REQUIRED
    assert "registration-token" not in result.message


async def test_connection_test_uses_configured_anna_domain_health(
    db_session: AsyncSession,
) -> None:
    _FakeProviderClient.manifest_response = _manifest(
        provider_id="pullbox.annas_archive",
        configuration_schema={
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "format": "uri",
                    "enum": [
                        "https://annas-archive.gl",
                        "https://annas-archive.pk",
                        "https://annas-archive.gd",
                    ],
                }
            },
            "additionalProperties": False,
        },
    )
    registered = await register_direct_provider(
        db_session,
        DirectProviderRegistrationInput(
            endpoint="http://provider:8780",
            bearer_token="registration-token-with-sufficient-length",
            allow_private_http=True,
            confirm_custom_provider=True,
        ),
        client_factory=_factory,
    )
    await update_direct_provider(
        db_session,
        registered.id,
        public_configuration={"domain": "https://annas-archive.gd"},
    )
    _FakeProviderClient.health_response = _health(
        diagnostics={
            "source": "reachable",
            "source.annas-archive.gl": "reachable",
            "source.annas-archive.pk": "reachable",
            "source.annas-archive.gd": "unreachable",
        }
    )

    result = await run_direct_provider_test(
        db_session,
        registered.id,
        client_factory=_factory,
    )

    assert result.usable is False
    assert result.state == DirectProviderState.DEGRADED
    stored = await db_session.get(DirectProviderConfig, registered.id)
    assert stored is not None
    assert stored.last_error_code == "provider_source_unavailable"


async def test_configuration_updates_validate_controls_and_disable_until_retested(
    db_session: AsyncSession,
) -> None:
    _FakeProviderClient.manifest_response = _manifest(
        configuration_schema={
            "type": "object",
            "properties": {
                "member_token": {
                    "type": "string",
                    "title": "Member token",
                    "x-pullbox-secret": True,
                },
                "result_limit": {
                    "type": "integer",
                    "title": "Result limit",
                    "minimum": 1,
                    "maximum": 100,
                },
                "source_url": {
                    "type": "string",
                    "title": "Official URL",
                    "format": "uri",
                    "enum": [
                        "https://annas-archive.gl",
                        "https://annas-archive.pk",
                        "https://annas-archive.gd",
                    ],
                    "default": "https://annas-archive.gd",
                },
            },
            "additionalProperties": False,
        }
    )
    registered = await register_direct_provider(
        db_session,
        DirectProviderRegistrationInput(
            endpoint="http://provider:8780",
            bearer_token="registration-token-with-sufficient-length",
            allow_private_http=True,
            confirm_custom_provider=True,
        ),
        client_factory=_factory,
    )
    await enable_direct_provider(db_session, registered.id, client_factory=_factory)

    updated = await update_direct_provider(
        db_session,
        registered.id,
        priority=10,
        public_configuration={
            "result_limit": 25,
            "source_url": "https://annas-archive.gl",
        },
        secret_configuration={"member_token": "member-secret-value"},
    )

    assert updated.priority == 10
    assert updated.enabled is False
    stored = await db_session.get(DirectProviderConfig, registered.id)
    assert stored is not None
    assert stored.configuration_metadata["public_values"] == {
        "result_limit": 25,
        "source_url": "https://annas-archive.gl",
    }
    assert "member-secret-value" not in str(stored.encrypted_configuration)

    with pytest.raises(DirectProviderRegistrationError, match="unknown_field"):
        await update_direct_provider(
            db_session,
            registered.id,
            public_configuration={"unknown_field": True},
        )

    with pytest.raises(DirectProviderRegistrationError, match="choice is invalid"):
        await update_direct_provider(
            db_session,
            registered.id,
            public_configuration={"source_url": "https://annas-archive.gd.evil.example"},
        )


async def test_open_source_origin_accepts_safe_custom_url_and_rejects_private_dns(
    db_session: AsyncSession,
) -> None:
    _FakeProviderClient.manifest_response = _manifest(
        configuration_schema={
            "type": "object",
            "properties": {
                "source_url": {
                    "type": "string",
                    "format": "uri",
                    "default": "https://source.example",
                    "x-pullbox-suggestions": ["https://source.example"],
                    "x-pullbox-source-origin": True,
                }
            },
            "additionalProperties": False,
        }
    )
    registered = await register_direct_provider(
        db_session,
        DirectProviderRegistrationInput(
            endpoint="http://provider:8780",
            bearer_token="registration-token-with-sufficient-length",
            allow_private_http=True,
            confirm_custom_provider=True,
        ),
        client_factory=_factory,
    )

    updated = await update_direct_provider(
        db_session,
        registered.id,
        public_configuration={"source_url": "https://custom-source.example"},
        source_origin_resolver=_public_source_resolver,
    )

    assert updated.public_configuration["source_url"] == "https://custom-source.example"

    unavailable = await update_direct_provider(
        db_session,
        registered.id,
        public_configuration={"source_url": "https://temporarily-unavailable.example"},
        source_origin_resolver=_unresolved_source_resolver,
    )

    assert unavailable.public_configuration["source_url"] == (
        "https://temporarily-unavailable.example"
    )

    with pytest.raises(DirectProviderRegistrationError, match="public network"):
        await update_direct_provider(
            db_session,
            registered.id,
            public_configuration={"source_url": "https://private-source.example"},
            source_origin_resolver=_private_source_resolver,
        )

    with pytest.raises(DirectProviderRegistrationError, match="public network"):
        await update_direct_provider(
            db_session,
            registered.id,
            public_configuration={"source_url": "https://mixed-source.example"},
            source_origin_resolver=_mixed_source_resolver,
        )


@pytest.mark.parametrize(
    "source_url",
    [
        "http://source.example",
        "https://user:secret@source.example",
        "https://source.example/path",
        "https://source.example?mirror=one",
        "https://source.example#fragment",
        "https://127.0.0.1",
        "https://localhost",
        "https://provider.localhost",
        "https://2130706433",
        "https://0x7f000001",
        "https://017700000001",
        "https://source.local",
        "https://source.onion",
        "https://source.internal",
        "https://source.home.arpa",
        "https://bad host.example",
        "https://-bad.example",
        "https://bad-.example",
        "https://bad..example",
        "https://singlelabel",
    ],
)
async def test_open_source_origin_rejects_unsafe_url_shapes(
    db_session: AsyncSession,
    source_url: str,
) -> None:
    _FakeProviderClient.manifest_response = _manifest(
        provider_id="community.unsafe",
        configuration_schema={
            "type": "object",
            "properties": {
                "source_url": {
                    "type": "string",
                    "format": "uri",
                    "x-pullbox-suggestions": ["https://source.example"],
                    "x-pullbox-source-origin": True,
                }
            },
            "additionalProperties": False,
        },
    )
    registered = await register_direct_provider(
        db_session,
        DirectProviderRegistrationInput(
            endpoint="http://provider:8780",
            bearer_token="registration-token-with-sufficient-length",
            allow_private_http=True,
            confirm_custom_provider=True,
        ),
        client_factory=_factory,
    )

    with pytest.raises(DirectProviderRegistrationError):
        await update_direct_provider(
            db_session,
            registered.id,
            public_configuration={"source_url": source_url},
            source_origin_resolver=_public_source_resolver,
        )


async def test_removal_preserves_history_contract_and_list_order(
    db_session: AsyncSession,
) -> None:
    first = await register_direct_provider(
        db_session,
        DirectProviderRegistrationInput(
            endpoint="http://provider:8780",
            bearer_token="registration-token-with-sufficient-length",
            allow_private_http=True,
            confirm_custom_provider=True,
            priority=50,
        ),
        client_factory=_factory,
    )
    _FakeProviderClient.manifest_response = _manifest(provider_id="community.second")
    second = await register_direct_provider(
        db_session,
        DirectProviderRegistrationInput(
            endpoint="http://provider-two:8780",
            bearer_token="another-registration-token-with-length",
            allow_private_http=True,
            confirm_custom_provider=True,
            priority=10,
        ),
        client_factory=_factory,
    )

    listed = await list_direct_providers(db_session)
    assert [item.id for item in listed] == [second.id, first.id]

    await remove_direct_provider(db_session, first.id)
    assert await db_session.get(DirectProviderConfig, first.id) is None
    assert datetime.now(UTC).tzinfo is UTC
