"""API schemas for external direct-download provider registration."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - Pydantic resolves this at runtime

from pydantic import BaseModel, ConfigDict, Field

from pullbox.models.direct_acquisition import (  # noqa: TC001 - Pydantic resolves these enums
    DirectProviderState,
    DirectProviderTrustLevel,
)


class DirectProviderRegisterRequest(BaseModel):
    endpoint: str = Field(min_length=1, max_length=1_000)
    bearer_token: str = Field(min_length=32, max_length=16_384, repr=False)
    allow_private_http: bool = False
    confirm_custom_provider: bool = False
    priority: int = Field(default=50, ge=0, le=1_000)


class DirectProviderUpdateRequest(BaseModel):
    priority: int | None = Field(default=None, ge=0, le=1_000)
    bearer_token: str | None = Field(default=None, min_length=32, max_length=16_384, repr=False)
    public_configuration: dict[str, str | int | float | bool] | None = None
    secret_configuration: dict[str, str | None] | None = Field(default=None, repr=False)
    resolver_enabled: bool | None = None
    automatic_quota_reserve: int | None = Field(default=None, ge=0, le=100_000)


class DirectConfigurationControlResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    value_type: str
    title: str
    description: str | None
    required: bool
    secret: bool
    input_format: str | None
    default: str | int | float | bool | None
    choices: tuple[str | int | float | bool, ...]
    minimum: float | None
    maximum: float | None
    min_length: int | None
    max_length: int | None
    placeholder: str | None
    suggestions: tuple[str, ...]
    source_origin: bool


class DirectProviderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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
    configuration_controls: tuple[DirectConfigurationControlResponse, ...]
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


class DirectProviderTestResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    usable: bool
    state: DirectProviderState
    message: str
    checked_at: datetime
