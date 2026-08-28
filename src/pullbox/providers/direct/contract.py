"""Pullbox-native DTOs for direct-download provider protocol v1."""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime  # noqa: TC003 - Pydantic resolves this at runtime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID  # noqa: TC003 - Pydantic resolves this at runtime

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

DIRECT_PROVIDER_PROTOCOL_V1 = "direct-download-provider/v1"
SUPPORTED_DIRECT_PROVIDER_PROTOCOLS = (DIRECT_PROVIDER_PROTOCOL_V1,)
MAX_DIRECT_PROVIDER_RESULTS = 100

_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_ALLOWED_SCHEMA_KEYS = {
    "type",
    "title",
    "description",
    "properties",
    "required",
    "additionalProperties",
}
_ALLOWED_FIELD_KEYS = {
    "type",
    "title",
    "description",
    "default",
    "enum",
    "format",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "x-pullbox-secret",
    "x-pullbox-placeholder",
    "x-pullbox-suggestions",
    "x-pullbox-source-origin",
}
_ALLOWED_FIELD_TYPES = frozenset({"string", "boolean", "integer", "number"})
_ALLOWED_INPUT_FORMATS = frozenset({"uri"})
_SPECIAL_USE_SOURCE_SUFFIXES = (
    "localhost",
    "local",
    "onion",
    "internal",
    "home.arpa",
)


class ProviderConfigurationSchemaError(ValueError):
    """Provider configuration cannot be represented by native safe controls."""


class DirectContractModel(BaseModel):
    """Accept additive optional fields within protocol major v1."""

    model_config = ConfigDict(extra="ignore")


class DirectProviderStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    RATE_LIMITED = "rate_limited"
    CHALLENGE_REQUIRED = "challenge_required"
    AUTHENTICATION_REQUIRED = "authentication_required"
    INCOMPATIBLE = "incompatible"
    UNAVAILABLE = "unavailable"


class DirectArtifactRoute(StrEnum):
    DIRECT_ARTIFACT = "direct_artifact"
    TORRENT_FILE = "torrent_file"
    MAGNET = "magnet"


class DirectConfigurationControl(DirectContractModel):
    name: str
    value_type: str
    title: str
    description: str | None = None
    required: bool = False
    secret: bool = False
    input_format: str | None = None
    default: str | int | float | bool | None = None
    choices: tuple[str | int | float | bool, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    placeholder: str | None = None
    suggestions: tuple[str, ...] = ()
    source_origin: bool = False


class DirectProviderCapabilities(DirectContractModel):
    search: bool
    resolve: bool
    browser_challenge: bool = False
    health: bool = True
    quota: bool = False
    configuration_schema: bool = False


class DirectManifestResponse(DirectContractModel):
    protocol_version: Literal["direct-download-provider/v1"]
    provider_id: str = Field(min_length=1, max_length=255, pattern=r"[a-z0-9][a-z0-9._-]*")
    display_name: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=2_000)
    provider_version: str = Field(min_length=1, max_length=100)
    supported_protocol_versions: list[str] = Field(min_length=1, max_length=10)
    publisher: str = Field(min_length=1, max_length=255)
    license: str = Field(min_length=1, max_length=255)
    homepage_url: str | None = Field(default=None, max_length=2_000)
    documentation_url: str | None = Field(default=None, max_length=2_000)
    support_url: str | None = Field(default=None, max_length=2_000)
    source_domains: list[str] = Field(max_length=100)
    artifact_host_patterns: list[str] = Field(default_factory=list, max_length=100)
    capabilities: DirectProviderCapabilities
    configuration_schema: dict[str, Any]
    min_pullbox_version: str | None = Field(default=None, max_length=100)
    max_pullbox_version: str | None = Field(default=None, max_length=100)
    build: dict[str, str] = Field(default_factory=dict)
    _configuration_controls: tuple[DirectConfigurationControl, ...] = PrivateAttr(default=())

    @model_validator(mode="after")
    def validate_configuration_controls(self) -> Self:
        self._configuration_controls = validate_provider_configuration_schema(
            self.configuration_schema
        )
        return self

    @property
    def configuration_controls(self) -> tuple[DirectConfigurationControl, ...]:
        return self._configuration_controls


DiagnosticScalar = str | int | float | bool | None


class DirectHealthResponse(DirectContractModel):
    protocol_version: Literal["direct-download-provider/v1"]
    process_status: DirectProviderStatus
    source_status: DirectProviderStatus
    message: str = Field(min_length=1, max_length=2_000)
    retry_after_seconds: int | None = Field(default=None, ge=0, le=86_400)
    diagnostics: dict[str, DiagnosticScalar] = Field(default_factory=dict)


class DirectResolverProfile(DirectContractModel):
    endpoint: str = Field(max_length=1_000)
    mode: Literal["flaresolverr_v1", "trawl_scrape"] = "flaresolverr_v1"
    timeout_seconds: float = Field(gt=0, le=300)
    max_concurrency: int = Field(ge=1, le=4)
    declared_domains: list[str] = Field(max_length=100)
    authentication_headers: dict[str, str] = Field(default_factory=dict, repr=False)


class DirectSearchIntent(DirectContractModel):
    series_title: str = Field(min_length=1, max_length=500)
    normalized_title: str = Field(min_length=1, max_length=500)
    alternate_titles: list[str] = Field(default_factory=list, max_length=25)
    issue_number: str | None = Field(default=None, max_length=50)
    issue_type: str | None = Field(default=None, max_length=40)
    volume: str | None = Field(default=None, max_length=100)
    issue_title: str | None = Field(default=None, max_length=500)
    series_year: int | None = Field(default=None, ge=1800, le=2200)
    release_year: int | None = Field(default=None, ge=1800, le=2200)
    year: int | None = Field(default=None, ge=1800, le=2200)
    publisher: str | None = Field(default=None, max_length=300)
    language: str | None = Field(default=None, max_length=20)
    preferred_formats: list[str] = Field(default_factory=list, max_length=20)
    quality_preferences: list[str] = Field(default_factory=list, max_length=20)


class DirectDeadlineRequest(DirectContractModel):
    protocol_version: str
    request_id: UUID
    deadline: datetime
    provider_config: dict[str, Any] = Field(default_factory=dict, repr=False)
    source_credentials: dict[str, str] = Field(default_factory=dict, repr=False)
    resolver_profile: DirectResolverProfile | None = Field(default=None, repr=False)

    @field_validator("deadline")
    @classmethod
    def deadline_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deadline must include a timezone")
        return value


class DirectSearchRequest(DirectDeadlineRequest):
    intent: DirectSearchIntent
    limit: int = Field(default=20, ge=1, le=MAX_DIRECT_PROVIDER_RESULTS)


class DirectParsedCandidate(DirectContractModel):
    series_title: str = Field(min_length=1, max_length=500)
    issue_numbers: list[str] = Field(default_factory=list, max_length=100)
    volume: str | None = Field(default=None, max_length=100)
    year: int | None = Field(default=None, ge=1800, le=2200)
    publisher: str | None = Field(default=None, max_length=300)
    language: str | None = Field(default=None, max_length=20)
    edition: str | None = Field(default=None, max_length=200)
    format: str | None = Field(default=None, max_length=40)
    release_group: str | None = Field(default=None, max_length=200)
    quality: str | None = Field(default=None, max_length=100)


class DirectCandidate(DirectContractModel):
    provider_candidate_id: str = Field(min_length=1, max_length=500)
    source_reference: str = Field(min_length=1, max_length=2_000)
    display_title: str = Field(min_length=1, max_length=1_000)
    raw_title: str = Field(min_length=1, max_length=2_000)
    parsed: DirectParsedCandidate
    provider_confidence: float = Field(ge=0, le=1)
    content_fingerprint: str | None = Field(
        default=None,
        pattern=r"^md5:[0-9a-f]{32}$",
        repr=False,
    )
    provenance: dict[str, DiagnosticScalar] = Field(default_factory=dict)
    can_resolve: bool = True
    expires_at: datetime | None = None


class DirectSearchResponse(DirectContractModel):
    protocol_version: Literal["direct-download-provider/v1"]
    request_id: UUID
    candidates: Annotated[list[DirectCandidate], Field(max_length=MAX_DIRECT_PROVIDER_RESULTS)]
    truncated: bool = False


class DirectResolveRequest(DirectDeadlineRequest):
    provider_candidate_id: str = Field(min_length=1, max_length=500)


class DirectArtifactCoverage(DirectContractModel):
    issue_numbers: list[str] = Field(default_factory=list, max_length=100)
    issue_ids: list[str] = Field(default_factory=list, max_length=100)
    volume: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=2_000)


class DirectMirror(DirectContractModel):
    mirror_id: str = Field(min_length=1, max_length=500)
    host_kind: str = Field(min_length=1, max_length=100)
    share_url: str | None = Field(default=None, max_length=4_000, repr=False)
    final_url: str | None = Field(default=None, max_length=4_000, repr=False)
    source_headers: dict[str, str] = Field(default_factory=dict, repr=False)
    size_bytes: int | None = Field(default=None, ge=0)
    checksum: str | None = Field(default=None, max_length=500)
    etag: str | None = Field(default=None, max_length=1_000)
    last_modified: str | None = Field(default=None, max_length=200)
    expires_at: datetime | None = None
    retry_after_seconds: int | None = Field(default=None, ge=0, le=86_400)

    @model_validator(mode="after")
    def require_location(self) -> Self:
        if not self.share_url and not self.final_url:
            raise ValueError("mirror requires share_url or final_url")
        return self


class DirectArtifact(DirectContractModel):
    artifact_id: str = Field(min_length=1, max_length=500)
    coverage: DirectArtifactCoverage
    route: DirectArtifactRoute
    format: str | None = Field(default=None, max_length=40)
    quality: str | None = Field(default=None, max_length=100)
    language: str | None = Field(default=None, max_length=20)
    edition: str | None = Field(default=None, max_length=200)
    release_group: str | None = Field(default=None, max_length=200)
    size_bytes: int | None = Field(default=None, ge=0)
    size_is_estimate: bool = False
    mirrors: list[DirectMirror] = Field(default_factory=list, max_length=50)
    magnet_uri: str | None = Field(default=None, max_length=10_000, repr=False)
    limitations: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def require_route_payload(self) -> Self:
        if self.route == DirectArtifactRoute.MAGNET:
            if not self.magnet_uri:
                raise ValueError("magnet route requires magnet_uri")
        elif not self.mirrors:
            raise ValueError("non-magnet route requires at least one mirror")
        return self


class DirectQuotaStatus(DirectContractModel):
    """Optional source-account capacity without account activity history."""

    remaining: int | None = Field(default=None, ge=0, le=1_000_000)
    limit: int | None = Field(default=None, ge=0, le=1_000_000)
    window_seconds: int | None = Field(default=None, ge=1, le=31_536_000)
    reset_at: datetime | None = None


class DirectResolveResponse(DirectContractModel):
    protocol_version: Literal["direct-download-provider/v1"]
    request_id: UUID
    artifacts: Annotated[list[DirectArtifact], Field(max_length=MAX_DIRECT_PROVIDER_RESULTS)]
    quota: DirectQuotaStatus | None = None


def negotiate_direct_provider_protocol(provider_versions: list[str]) -> str:
    """Select the highest exact protocol intersection, failing closed."""
    intersection = [
        version for version in SUPPORTED_DIRECT_PROVIDER_PROTOCOLS if version in provider_versions
    ]
    if not intersection:
        raise ValueError("No compatible direct-download provider protocol version.")
    return intersection[-1]


def validate_provider_configuration_schema(
    raw_schema: object,
) -> tuple[DirectConfigurationControl, ...]:
    """Convert a provider schema into a finite set of Pullbox-owned controls."""
    if not isinstance(raw_schema, dict):
        raise ProviderConfigurationSchemaError("Provider configuration schema must be an object.")
    if set(raw_schema) - _ALLOWED_SCHEMA_KEYS:
        raise ProviderConfigurationSchemaError("Provider configuration schema is unsupported.")
    if raw_schema.get("type") != "object" or raw_schema.get("additionalProperties") is not False:
        raise ProviderConfigurationSchemaError("Provider configuration schema must be closed.")
    properties = raw_schema.get("properties")
    if not isinstance(properties, dict) or len(properties) > 50:
        raise ProviderConfigurationSchemaError("Provider configuration fields are invalid.")
    required_raw = raw_schema.get("required", [])
    if not isinstance(required_raw, list) or not all(
        isinstance(item, str) for item in required_raw
    ):
        raise ProviderConfigurationSchemaError("Provider required fields are invalid.")
    required = set(required_raw)
    if not required.issubset(properties):
        raise ProviderConfigurationSchemaError("Provider requires an unknown configuration field.")

    controls: list[DirectConfigurationControl] = []
    for name, raw_field in properties.items():
        if not isinstance(name, str) or not _FIELD_NAME.fullmatch(name):
            raise ProviderConfigurationSchemaError("Provider field names must use snake_case.")
        if not isinstance(raw_field, dict) or set(raw_field) - _ALLOWED_FIELD_KEYS:
            raise ProviderConfigurationSchemaError("Provider configuration control is unsupported.")
        value_type = raw_field.get("type")
        if value_type not in _ALLOWED_FIELD_TYPES:
            raise ProviderConfigurationSchemaError(
                "Provider configuration control type is unsupported."
            )
        secret = raw_field.get("x-pullbox-secret", False)
        if not isinstance(secret, bool) or (secret and value_type != "string"):
            raise ProviderConfigurationSchemaError("Provider secret control is invalid.")
        input_format = raw_field.get("format")
        if input_format is not None and (
            input_format not in _ALLOWED_INPUT_FORMATS or value_type != "string" or secret
        ):
            raise ProviderConfigurationSchemaError(
                "Provider configuration control format is unsupported."
            )
        choices_raw = raw_field.get("enum", [])
        if (
            not isinstance(choices_raw, list)
            or len(choices_raw) > 100
            or ("enum" in raw_field and not choices_raw)
        ):
            raise ProviderConfigurationSchemaError("Provider configuration choices are invalid.")
        choices = tuple(_typed_configuration_value(value, str(value_type)) for value in choices_raw)
        suggestions = _uri_suggestions(
            raw_field.get("x-pullbox-suggestions"),
            value_type=value_type,
            input_format=input_format,
            secret=secret,
        )
        source_origin = raw_field.get("x-pullbox-source-origin", False)
        if not isinstance(source_origin, bool):
            raise ProviderConfigurationSchemaError("Provider source-origin control is invalid.")
        if source_origin and (value_type != "string" or input_format != "uri" or secret):
            raise ProviderConfigurationSchemaError("Provider source-origin control is invalid.")
        default = None
        if "default" in raw_field:
            default = _typed_configuration_value(raw_field["default"], str(value_type))
            if choices and default not in choices:
                raise ProviderConfigurationSchemaError("Provider configuration default is invalid.")
            if (source_origin or suggestions) and (
                not isinstance(default, str) or not _is_safe_https_origin(default)
            ):
                raise ProviderConfigurationSchemaError(
                    "Provider configuration URI default is unsafe."
                )
        minimum = _number_or_none(raw_field.get("minimum"))
        maximum = _number_or_none(raw_field.get("maximum"))
        min_length = _integer_or_none(raw_field.get("minLength"))
        max_length = _integer_or_none(raw_field.get("maxLength"))
        if value_type in {"integer", "number"}:
            if min_length is not None or max_length is not None:
                raise ProviderConfigurationSchemaError(
                    "Numeric provider controls cannot use length bounds."
                )
        elif minimum is not None or maximum is not None:
            raise ProviderConfigurationSchemaError(
                "Non-numeric provider controls cannot use numeric bounds."
            )
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ProviderConfigurationSchemaError("Provider numeric bounds are invalid.")
        if min_length is not None and max_length is not None and min_length > max_length:
            raise ProviderConfigurationSchemaError("Provider length bounds are invalid.")
        controls.append(
            DirectConfigurationControl(
                name=name,
                value_type=str(value_type),
                title=_bounded_control_text(raw_field.get("title"))
                or name.replace("_", " ").title(),
                description=_bounded_control_text(raw_field.get("description")),
                required=name in required,
                secret=secret,
                input_format=input_format if isinstance(input_format, str) else None,
                default=default,
                choices=choices,
                minimum=minimum,
                maximum=maximum,
                min_length=min_length,
                max_length=max_length,
                placeholder=_bounded_control_text(raw_field.get("x-pullbox-placeholder")),
                suggestions=suggestions,
                source_origin=source_origin,
            )
        )
    return tuple(controls)


def _uri_suggestions(
    raw: object,
    *,
    value_type: object,
    input_format: object,
    secret: bool,
) -> tuple[str, ...]:
    if raw is None:
        return ()
    if value_type != "string" or input_format != "uri" or secret:
        raise ProviderConfigurationSchemaError("Provider URI suggestions are invalid.")
    if not isinstance(raw, list) or not raw or len(raw) > 20:
        raise ProviderConfigurationSchemaError("Provider URI suggestions are invalid.")
    suggestions: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not _is_safe_https_origin(value):
            raise ProviderConfigurationSchemaError("Provider URI suggestions are invalid.")
        if value in suggestions:
            raise ProviderConfigurationSchemaError("Provider URI suggestions are duplicated.")
        suggestions.append(value)
    return tuple(suggestions)


def _is_safe_https_origin(raw: str) -> bool:
    if not raw or len(raw) > 2_000:
        return False
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return False
    hostname = parsed.hostname
    if hostname is None:
        return False
    normalized_hostname = hostname.casefold().rstrip(".")
    if not is_public_source_hostname_syntax(normalized_hostname):
        return False

    return bool(
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


def is_public_source_hostname_syntax(hostname: str) -> bool:
    """Reject IP literals, legacy numeric forms, and special-use namespaces."""
    labels = hostname.split(".")
    if (
        len(hostname) > 253
        or len(labels) < 2
        or any(_DNS_LABEL.fullmatch(label) is None for label in labels)
        or not any(character.isalpha() for character in labels[-1])
    ):
        return False
    if any(
        hostname == suffix or hostname.endswith(f".{suffix}")
        for suffix in _SPECIAL_USE_SOURCE_SUFFIXES
    ):
        return False
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return False
    return not _looks_like_legacy_ipv4_literal(hostname)


def _looks_like_legacy_ipv4_literal(hostname: str) -> bool:
    labels = hostname.split(".")
    if not 1 <= len(labels) <= 4:
        return False
    for label in labels:
        if not label:
            return False
        if label.startswith("0x"):
            digits = label[2:]
            if not digits or any(character not in "0123456789abcdef" for character in digits):
                return False
        elif not label.isascii() or not label.isdigit():
            return False
    return True


def _bounded_control_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 2_000:
        raise ProviderConfigurationSchemaError("Provider configuration text is invalid.")
    return value.strip()


def _typed_configuration_value(
    value: object,
    value_type: str,
) -> str | int | float | bool:
    if value_type == "string" and isinstance(value, str):
        return value
    if value_type == "boolean" and isinstance(value, bool):
        return value
    if value_type == "integer" and isinstance(value, int) and not isinstance(value, bool):
        return value
    if value_type == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    raise ProviderConfigurationSchemaError("Provider configuration value has the wrong type.")


def _number_or_none(value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ProviderConfigurationSchemaError("Provider configuration bound is invalid.")
    return float(value)


def _integer_or_none(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProviderConfigurationSchemaError("Provider configuration length is invalid.")
    return value
