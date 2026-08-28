"""Shared, secret-safe contracts for native artifact-host adapters."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

from pullbox.models.direct_acquisition import (
    DirectArtifactFailureClass,
    DirectArtifactHostKind,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from datetime import datetime


class ArtifactHostCredentialMode(enum.StrEnum):
    """Access mode selected for one host operation."""

    ANONYMOUS = "anonymous"
    ACCOUNT = "account"


class ArtifactTransferProtocol(enum.StrEnum):
    """Byte-transfer implementation selected by a resolved host route."""

    HTTPS = "https"
    MEGA_BRIDGE = "mega_bridge"


class ArtifactHostAdapter(Protocol):
    """One native adapter that resolves exactly one registered host family."""

    host_kind: DirectArtifactHostKind

    async def resolve(
        self,
        request: HostResolutionRequest,
        *,
        credentials: Mapping[str, str],
        progress_callback: ArtifactResolutionProgressCallback | None = None,
    ) -> ResolvedTransfer: ...


@dataclass(frozen=True, slots=True)
class ArtifactResolutionProgress:
    """Secret-free progress for one browser-resolver attempt."""

    resolver_id: int
    resolver_name: str
    resolver_kind: str
    attempt: int
    total: int
    scope: str


if TYPE_CHECKING:
    ArtifactResolutionProgressCallback = Callable[[ArtifactResolutionProgress], Awaitable[None]]
else:
    ArtifactResolutionProgressCallback = object


@dataclass(frozen=True, slots=True)
class HostResolutionRequest:
    """One provider mirror detached from provider-owned secrets."""

    artifact_identity: str
    host_kind: DirectArtifactHostKind
    share_url: str | None = field(repr=False)
    final_url: str | None = field(repr=False)
    provider_headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    expected_size: int | None = None
    checksum: str | None = field(default=None, repr=False)
    etag: str | None = field(default=None, repr=False)
    last_modified: str | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ResolvedTransfer:
    """Ephemeral transfer material returned by a host adapter."""

    host_kind: DirectArtifactHostKind
    url: str = field(repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    expected_size: int | None = None
    checksum: str | None = field(default=None, repr=False)
    etag: str | None = field(default=None, repr=False)
    last_modified: str | None = None
    expires_at: datetime | None = None
    filename_hint: str | None = None
    range_supported: bool = False
    prefer_single_response: bool = False
    allowed_domains: tuple[str, ...] = ()
    transport_protocol: ArtifactTransferProtocol = ArtifactTransferProtocol.HTTPS
    bridge_session: str | None = field(default=None, repr=False)


class ArtifactHostResolutionError(RuntimeError):
    """A classified host failure with no sensitive representation."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        failure_class: DirectArtifactFailureClass,
        retryable: bool,
        intervention: bool,
        http_status: int | None = None,
        sensitive_context: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.failure_class = failure_class
        self.retryable = retryable
        self.intervention = intervention
        self.http_status = (
            http_status
            if isinstance(http_status, int)
            and not isinstance(http_status, bool)
            and 100 <= http_status <= 599
            else None
        )
        self._sensitive_context = dict(sensitive_context or {})

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code!r}, "
            f"failure_class={self.failure_class.value!r}, "
            f"retryable={self.retryable!r}, intervention={self.intervention!r})"
        )


_CREDENTIAL_FIELDS: dict[DirectArtifactHostKind, frozenset[str]] = {
    DirectArtifactHostKind.GENERIC_HTTPS: frozenset(),
    DirectArtifactHostKind.PIXELDRAIN: frozenset({"api_key"}),
    DirectArtifactHostKind.MEGA: frozenset({"session"}),
    DirectArtifactHostKind.ROOTZ: frozenset(),
    DirectArtifactHostKind.MEDIAFIRE: frozenset(),
    DirectArtifactHostKind.TERABOX: frozenset({"session_token", "cookie"}),
    DirectArtifactHostKind.DATANODES: frozenset({"username", "password"}),
}
_ACCOUNT_REQUIRED = frozenset(
    {
        DirectArtifactHostKind.TERABOX,
        DirectArtifactHostKind.DATANODES,
    }
)
_REQUIRED_CREDENTIAL_FIELDS: dict[DirectArtifactHostKind, frozenset[str]] = {
    DirectArtifactHostKind.DATANODES: frozenset({"username", "password"}),
}
_SENSITIVE_PROVIDER_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "host",
        "proxy-authorization",
        "x-forwarded-for",
    }
)
_ALLOWED_PROVIDER_HEADERS = {
    "accept": "Accept",
    "referer": "Referer",
}


def credential_mode_for_host(
    host_kind: DirectArtifactHostKind,
    credentials: Mapping[str, str],
) -> ArtifactHostCredentialMode:
    """Select anonymous or account mode without exposing credential values."""
    configured = {name for name, value in credentials.items() if value}
    unsupported = configured - _CREDENTIAL_FIELDS[host_kind]
    if unsupported:
        raise ArtifactHostResolutionError(
            code="invalid_host_credentials",
            message="Artifact host credentials do not match the selected host.",
            failure_class=DirectArtifactFailureClass.USER_ACTION,
            retryable=False,
            intervention=True,
        )
    if configured:
        required = _REQUIRED_CREDENTIAL_FIELDS.get(host_kind, frozenset())
        if not required.issubset(configured):
            raise ArtifactHostResolutionError(
                code="invalid_host_credentials",
                message="Artifact host credentials are incomplete.",
                failure_class=DirectArtifactFailureClass.USER_ACTION,
                retryable=False,
                intervention=True,
            )
        return ArtifactHostCredentialMode.ACCOUNT
    if host_kind in _ACCOUNT_REQUIRED:
        raise ArtifactHostResolutionError(
            code="artifact_host_auth_required",
            message="This artifact host requires an account session.",
            failure_class=DirectArtifactFailureClass.ARTIFACT_HOST_AUTH_REQUIRED,
            retryable=False,
            intervention=True,
        )
    return ArtifactHostCredentialMode.ANONYMOUS


def sanitize_provider_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Retain only bounded non-secret hints from an untrusted provider."""
    result: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        name = raw_name.strip().lower()
        if name in _SENSITIVE_PROVIDER_HEADERS or name.startswith("sec-"):
            raise ArtifactHostResolutionError(
                code="unsafe_provider_header",
                message="The provider supplied an unsafe provider header.",
                failure_class=DirectArtifactFailureClass.CANDIDATE_INVALID,
                retryable=False,
                intervention=True,
            )
        canonical_name = _ALLOWED_PROVIDER_HEADERS.get(name)
        if canonical_name is None:
            continue
        value = raw_value.strip()
        if not value or len(value) > 2_000 or _contains_control_character(value):
            raise ArtifactHostResolutionError(
                code="invalid_provider_header",
                message="The provider supplied an invalid provider header.",
                failure_class=DirectArtifactFailureClass.CANDIDATE_INVALID,
                retryable=False,
                intervention=True,
            )
        if canonical_name == "Referer":
            _validate_referer(value)
        result[canonical_name] = value
    return result


def _validate_referer(value: str) -> None:
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise _referer_error() from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise _referer_error()


def _referer_error() -> ArtifactHostResolutionError:
    return ArtifactHostResolutionError(
        code="invalid_provider_referer",
        message="The provider Referer must be a credential-free HTTPS URL.",
        failure_class=DirectArtifactFailureClass.CANDIDATE_INVALID,
        retryable=False,
        intervention=True,
    )


def _contains_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)
