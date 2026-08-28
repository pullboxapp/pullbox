"""Validation helpers for operator-configured service URLs."""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse

_ALLOWED_PEER_SCHEMES = frozenset({"http", "https"})
_ALLOWED_AIRDCPP_HUB_SCHEMES = frozenset({"adc", "adcs", "dchub", "nmdc"})


def normalize_peer_base_url(
    value: str,
    *,
    reject_query_or_fragment: bool = False,
) -> str:
    """Normalize and validate an HTTP(S) peer/service base URL."""
    raw = value.strip()
    if any(char.isspace() for char in raw):
        raise ValueError("URL must not contain whitespace.")

    parsed = urlparse(raw)

    if parsed.scheme.lower() not in _ALLOWED_PEER_SCHEMES:
        raise ValueError("URL must use http or https.")
    if not parsed.netloc or not parsed.hostname:
        raise ValueError("URL must include a host.")
    if parsed.username or parsed.password:
        raise ValueError("URL must not include embedded credentials.")
    if reject_query_or_fragment and (parsed.params or parsed.query or parsed.fragment):
        raise ValueError("URL must not include a query or fragment.")

    normalized_path = parsed.path.rstrip("/")
    normalized = parsed._replace(scheme=parsed.scheme.lower(), path=normalized_path)
    return urlunparse(normalized)


def normalize_airdcpp_hub_url(value: str) -> str:
    """Normalize a non-secret ADC/NMDC hub URL for an optional allowlist."""
    raw = value.strip()
    if any(char.isspace() for char in raw):
        raise ValueError("Hub URL must not contain whitespace.")

    parsed = urlparse(raw)
    if parsed.scheme.lower() not in _ALLOWED_AIRDCPP_HUB_SCHEMES:
        raise ValueError("Hub URL must use adc, adcs, dchub, or nmdc.")
    if not parsed.netloc or not parsed.hostname:
        raise ValueError("Hub URL must include a host.")
    if parsed.username or parsed.password:
        raise ValueError("Hub URL must not include embedded credentials.")
    if parsed.params or parsed.query or parsed.fragment:
        raise ValueError("Hub URL must not include a query or fragment.")
    if parsed.path not in {"", "/"}:
        raise ValueError("Hub URL must not include a path.")

    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    authority = f"{host}:{parsed.port}" if parsed.port is not None else host
    return urlunparse((parsed.scheme.lower(), authority, "", "", "", ""))
