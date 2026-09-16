"""Boundary tests for direct-provider source-origin validation."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pullbox.models.direct_acquisition import DirectProviderConfig
from pullbox.providers.direct.contract import DIRECT_PROVIDER_PROTOCOL_V1
from pullbox.services.direct_provider_source_origin import (
    DirectProviderSourceOriginError,
    _normalize_domain,
    _origin_hostname,
    _public_configuration,
    _resolve_source_addresses,
    configured_direct_provider_source_domain,
    effective_direct_provider_source_domains,
    validate_direct_provider_source_origin,
)


def _manifest(*, source_domains: list[str] | None = None) -> dict[str, object]:
    return {
        "protocol_version": DIRECT_PROVIDER_PROTOCOL_V1,
        "provider_id": "pullbox.test",
        "display_name": "Test Provider",
        "description": "Source origin fixture.",
        "provider_version": "1.0.0",
        "supported_protocol_versions": [DIRECT_PROVIDER_PROTOCOL_V1],
        "publisher": "Pullbox",
        "license": "GPL-3.0-or-later",
        "source_domains": source_domains or ["source.example"],
        "capabilities": {
            "search": True,
            "resolve": True,
            "browser_challenge": True,
            "health": True,
            "quota": False,
            "configuration_schema": True,
        },
        "configuration_schema": {
            "type": "object",
            "properties": {
                "source_url": {
                    "type": "string",
                    "format": "uri",
                    "x-pullbox-source-origin": True,
                }
            },
            "additionalProperties": False,
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_url", ["", " ", "x" * 2_001, 42])
async def test_source_origin_requires_a_bounded_string(raw_url: object) -> None:
    with pytest.raises(DirectProviderSourceOriginError, match="bounded URL"):
        await validate_direct_provider_source_origin(raw_url)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_source_origin_rejects_malformed_port_and_url_shape() -> None:
    with pytest.raises(DirectProviderSourceOriginError, match="malformed"):
        await validate_direct_provider_source_origin("https://source.example:bad")
    with pytest.raises(DirectProviderSourceOriginError, match="one HTTPS origin"):
        await validate_direct_provider_source_origin("https://source.example/path")


@pytest.mark.asyncio
async def test_source_origin_dns_failures_are_fail_open_but_bad_answers_fail_closed() -> None:
    async def unavailable(_host: str, _port: int) -> tuple[str, ...]:
        raise OSError("dns unavailable")

    async def empty(_host: str, _port: int) -> tuple[str, ...]:
        return ()

    async def invalid(_host: str, _port: int) -> tuple[str, ...]:
        return ("not-an-ip",)

    async def private(_host: str, _port: int) -> tuple[str, ...]:
        return ("127.0.0.1",)

    for resolver in (unavailable, empty):
        validated = await validate_direct_provider_source_origin(
            "https://SOURCE.example./",
            resolver=resolver,
        )
        assert validated.url == "https://source.example"
        assert validated.host == "source.example"

    with pytest.raises(DirectProviderSourceOriginError, match="invalid public"):
        await validate_direct_provider_source_origin("https://source.example", resolver=invalid)
    with pytest.raises(DirectProviderSourceOriginError, match="public network"):
        await validate_direct_provider_source_origin("https://source.example", resolver=private)


@pytest.mark.asyncio
async def test_source_origin_accepts_only_global_dns_answers() -> None:
    async def public(_host: str, _port: int) -> tuple[str, ...]:
        return ("1.1.1.1", "2606:4700:4700::1111")

    validated = await validate_direct_provider_source_origin(
        "https://source.example:443",
        resolver=public,
    )

    assert validated.host == "source.example"


def test_effective_domains_survive_invalid_manifest_and_normalize_values() -> None:
    config = DirectProviderConfig(
        provider_id="pullbox.invalid",
        display_name="Invalid",
        endpoint="http://provider:8780",
        manifest_snapshot={
            "source_domains": [".SOURCE.Example.", "source.example", 4],
        },
    )

    assert effective_direct_provider_source_domains(config) == ("source.example",)
    assert configured_direct_provider_source_domain(config) is None


def test_configured_source_origin_precedes_legacy_domain_fallback() -> None:
    config = DirectProviderConfig(
        provider_id="pullbox.test",
        display_name="Test",
        endpoint="http://provider:8780",
        configuration_metadata={
            "public_values": {
                "source_url": "https://CUSTOM.example/",
                "domain": "https://legacy.example",
            }
        },
        manifest_snapshot=_manifest(source_domains=[".SOURCE.EXAMPLE.", "source.example"]),
    )

    assert effective_direct_provider_source_domains(config) == (
        "source.example",
        "custom.example",
    )
    assert configured_direct_provider_source_domain(config) == "custom.example"


def test_public_configuration_and_origin_helpers_fail_closed() -> None:
    config = DirectProviderConfig(
        provider_id="pullbox.test",
        display_name="Test",
        endpoint="http://provider:8780",
        configuration_metadata=[],  # type: ignore[arg-type]
    )

    assert _public_configuration(config) == {}
    config.configuration_metadata = {"public_values": []}
    assert _public_configuration(config) == {}
    assert _origin_hostname("https://source.example:bad") is None
    assert _origin_hostname("https://source.example/path") is None
    assert _normalize_domain(" .SOURCE.Example. ") == "source.example"


@pytest.mark.asyncio
async def test_default_resolver_deduplicates_socket_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [
        (None, None, None, None, ("1.1.1.1", 443)),
        (None, None, None, None, ("1.1.1.1", 443)),
        (None, None, None, None, ("8.8.8.8", 443)),
    ]
    monkeypatch.setattr(
        "pullbox.services.direct_provider_source_origin.asyncio.to_thread",
        AsyncMock(return_value=records),
    )

    assert await _resolve_source_addresses("source.example", 443) == (
        "1.1.1.1",
        "8.8.8.8",
    )
