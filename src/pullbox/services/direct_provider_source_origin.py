"""Validated effective source origins for direct-download providers."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from pullbox.providers.direct.contract import (
    DirectManifestResponse,
    is_public_source_hostname_syntax,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pullbox.models.direct_acquisition import DirectProviderConfig
    from pullbox.providers.direct.endpoint import ProviderEndpointResolver


class DirectProviderSourceOriginError(ValueError):
    """A provider source origin is unsafe or cannot be resolved."""


_MAX_EFFECTIVE_SOURCE_DOMAINS = 100


@dataclass(frozen=True, slots=True)
class ValidatedDirectProviderSourceOrigin:
    url: str
    host: str


async def validate_direct_provider_source_origin(
    raw_url: str,
    *,
    resolver: ProviderEndpointResolver | None = None,
) -> ValidatedDirectProviderSourceOrigin:
    """Validate one HTTPS origin and reject any known unsafe DNS resolution."""
    if not isinstance(raw_url, str) or not raw_url.strip() or len(raw_url) > 2_000:
        raise DirectProviderSourceOriginError("Provider source origin must be a bounded URL.")
    try:
        parsed = urlsplit(raw_url.strip())
        port = parsed.port
    except ValueError as exc:
        raise DirectProviderSourceOriginError("Provider source origin is malformed.") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise DirectProviderSourceOriginError(
            "Provider source origin must be one HTTPS origin without credentials or a path."
        )

    host = parsed.hostname.casefold().rstrip(".")
    if not is_public_source_hostname_syntax(host):
        raise DirectProviderSourceOriginError(
            "Provider source origin must use a public network hostname."
        )

    resolve = resolver or _resolve_source_addresses
    validated = ValidatedDirectProviderSourceOrigin(
        url=urlunsplit(("https", host, "", "", "")),
        host=host,
    )
    try:
        raw_addresses = await resolve(host, 443)
    except (OSError, TimeoutError):
        return validated
    if not raw_addresses:
        return validated
    for raw_address in raw_addresses:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as exc:
            raise DirectProviderSourceOriginError(
                "Provider source origin resolved to an invalid public network address."
            ) from exc
        if not address.is_global:
            raise DirectProviderSourceOriginError(
                "Provider source origin must resolve only to the public network."
            )

    return validated


def effective_direct_provider_source_domains(config: DirectProviderConfig) -> tuple[str, ...]:
    """Return manifest domains plus this provider's selected source origin."""
    raw_manifest = config.manifest_snapshot
    raw_domains = raw_manifest.get("source_domains", []) if isinstance(raw_manifest, dict) else []
    domains = (
        [_normalize_domain(value) for value in raw_domains if isinstance(value, str)]
        if isinstance(raw_domains, list)
        else []
    )
    try:
        manifest = DirectManifestResponse.model_validate(raw_manifest)
    except ValueError:
        unique_domains = tuple(dict.fromkeys(domain for domain in domains if domain))
        return unique_domains[:_MAX_EFFECTIVE_SOURCE_DOMAINS]

    manifest_domains = list(dict.fromkeys(domain for domain in domains if domain))
    configured_domains = list(dict.fromkeys(_configured_source_origin_domains(config, manifest)))
    merged = list(dict.fromkeys([*manifest_domains, *configured_domains]))
    if len(merged) <= _MAX_EFFECTIVE_SOURCE_DOMAINS:
        return tuple(merged)

    configured_set = set(configured_domains)
    retained_manifest = [domain for domain in manifest_domains if domain not in configured_set]
    manifest_limit = max(0, _MAX_EFFECTIVE_SOURCE_DOMAINS - len(configured_domains))
    return tuple(
        [
            *retained_manifest[:manifest_limit],
            *configured_domains[:_MAX_EFFECTIVE_SOURCE_DOMAINS],
        ]
    )


def configured_direct_provider_source_domain(config: DirectProviderConfig) -> str | None:
    """Return the selected source domain used by provider health diagnostics."""
    try:
        manifest = DirectManifestResponse.model_validate(config.manifest_snapshot)
    except ValueError:
        return None
    configured = _configured_source_origin_domains(config, manifest)
    if configured:
        return configured[0]

    # Protocol v1 providers released before source-origin metadata used `domain`.
    public_values = _public_configuration(config)
    raw_domain = public_values.get("domain")
    return _origin_hostname(raw_domain) if isinstance(raw_domain, str) else None


def _configured_source_origin_domains(
    config: DirectProviderConfig,
    manifest: DirectManifestResponse,
) -> list[str]:
    public_values = _public_configuration(config)
    domains: list[str] = []
    for control in manifest.configuration_controls:
        value = public_values.get(control.name)
        if control.source_origin and isinstance(value, str):
            hostname = _origin_hostname(value)
            if hostname:
                domains.append(hostname)
    return domains


def _public_configuration(config: DirectProviderConfig) -> dict[str, object]:
    metadata = config.configuration_metadata
    if not isinstance(metadata, dict):
        return {}
    values = metadata.get("public_values")
    return values if isinstance(values, dict) else {}


def _origin_hostname(raw_url: str) -> str | None:
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    return parsed.hostname.casefold().rstrip(".")


def _normalize_domain(raw_domain: str) -> str:
    return raw_domain.strip().casefold().lstrip(".").rstrip(".")


async def _resolve_source_addresses(host: str, port: int) -> Sequence[str]:
    records = await asyncio.to_thread(
        socket.getaddrinfo,
        host,
        port,
        type=socket.SOCK_STREAM,
    )
    return tuple(sorted({str(record[4][0]) for record in records}))
