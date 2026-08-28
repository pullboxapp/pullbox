"""AirDC++ configuration schemas."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - Pydantic needs this at runtime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pullbox.core.url_validation import normalize_airdcpp_hub_url


class AirDcppSettingsInput(BaseModel):
    """Editable settings for an AirDC++ download client."""

    search_enabled: bool = True
    automatic_search_enabled: bool = False
    minimum_search_interval_seconds: int = Field(45, ge=45, le=3600)
    manual_collection_seconds: int = Field(8, ge=1, le=120)
    automatic_collection_seconds: int = Field(15, ge=1, le=120)
    max_results: int = Field(200, ge=1, le=1000)
    max_retained_routes: int = Field(400, ge=1, le=2000)
    max_concurrent_searches: int = Field(1, ge=1, le=4)
    request_timeout_seconds: int = Field(15, ge=1, le=120)
    search_dispatch_deadline_seconds: int = Field(45, ge=5, le=300)
    reconciliation_interval_seconds: int = Field(30, ge=10, le=300)
    hub_allowlist: list[str] = Field(default_factory=list, max_length=100)
    queue_priority: int | None = Field(None, ge=-1, le=6)

    @field_validator("hub_allowlist")
    @classmethod
    def normalize_hub_allowlist(cls, value: list[str]) -> list[str]:
        """Normalize, deduplicate, and reject secret-bearing hub URLs."""
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            hub_url = normalize_airdcpp_hub_url(item)
            if hub_url not in seen:
                seen.add(hub_url)
                normalized.append(hub_url)
        return normalized

    @model_validator(mode="after")
    def validate_route_capacity(self) -> AirDcppSettingsInput:
        if self.max_retained_routes < self.max_results:
            raise ValueError("max_retained_routes must be at least max_results")
        return self


class AirDcppSettingsUpdate(AirDcppSettingsInput):
    """Complete replacement payload for editable AirDC++ settings."""


class AirDcppSettingsResponse(AirDcppSettingsInput):
    """AirDC++ settings returned with server-managed cooldown state."""

    model_config = ConfigDict(from_attributes=True)

    next_search_allowed_at: datetime | None = None
