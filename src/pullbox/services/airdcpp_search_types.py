"""Typed, bounded Direct Connect search outcomes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from pullbox.core.acquisition import AcquisitionProtocol

if TYPE_CHECKING:
    from datetime import datetime

    from pullbox.providers.base import ReleaseResult
    from pullbox.services.release_validator import ValidationResult

_TTH_PATTERN = re.compile(r"^[A-Z2-7]{39}$")


class AirDcppSearchProgressState(StrEnum):
    """User-facing lifecycle states without transport implementation details."""

    COOLDOWN = "cooldown"
    STARTING = "starting"
    QUEUED = "queued"
    COLLECTING = "collecting"
    FINISHING = "finishing"
    COMPLETE = "complete"
    ZERO_HUBS = "zero_hubs"
    FAILED = "failed"


class DcClientSearchStatus(StrEnum):
    COMPLETED = "completed"
    DEFERRED_COOLDOWN = "deferred_cooldown"
    ZERO_HUBS = "zero_hubs"
    DISPATCH_TIMEOUT = "dispatch_timeout"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class AirDcppSearchProgress:
    config_id: int
    client_name: str
    state: AirDcppSearchProgressState
    remaining_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class DcRoute:
    client_config_id: int
    client_identity: str
    search_instance_id: int
    grouped_result_id: str
    result_expires_at: datetime
    tth: str
    size_bytes: int

    def __post_init__(self) -> None:
        if self.client_config_id <= 0 or self.search_instance_id <= 0:
            raise ValueError("Direct Connect route IDs must be positive")
        if self.client_identity != f"airdcpp:{self.client_config_id}":
            raise ValueError("Direct Connect route must use its exact client identity")
        if not self.grouped_result_id or len(self.grouped_result_id) > 1000:
            raise ValueError("Direct Connect grouped result ID is invalid")
        if not _TTH_PATTERN.fullmatch(self.tth) or self.size_bytes <= 0:
            raise ValueError("Direct Connect route content identity is invalid")
        if self.result_expires_at.tzinfo is None:
            raise ValueError("Direct Connect route expiry must be timezone-aware")


@dataclass(frozen=True, slots=True)
class DcMetrics:
    source_count: int
    free_slots: int
    total_slots: int
    aggregate_connection_bytes_per_second: int | None
    hub_labels: tuple[str, ...] = ()
    adc_source_count: int | None = None
    nmdc_source_count: int | None = None

    def __post_init__(self) -> None:
        counts = (self.source_count, self.free_slots, self.total_slots)
        if any(count < 0 for count in counts) or self.free_slots > self.total_slots:
            raise ValueError("Direct Connect source metrics are invalid")
        if (
            self.aggregate_connection_bytes_per_second is not None
            and self.aggregate_connection_bytes_per_second < 0
        ):
            raise ValueError("Direct Connect connection metric is invalid")
        if len(self.hub_labels) > 10 or any(
            not label or len(label) > 100 for label in self.hub_labels
        ):
            raise ValueError("Direct Connect hub labels exceed their display bound")


@dataclass(frozen=True, slots=True)
class DcValidatedCandidate:
    release: ReleaseResult
    validation: ValidationResult
    route: DcRoute
    metrics: DcMetrics
    alternate_routes: tuple[DcRoute, ...] = ()

    def __post_init__(self) -> None:
        if self.release.protocol is not AcquisitionProtocol.DC:
            raise ValueError("Direct Connect candidate must use the DC protocol")
        if self.validation.release is not self.release:
            raise ValueError("Direct Connect validation must belong to the exact release")
        if self.release.size_bytes != self.route.size_bytes:
            raise ValueError("Direct Connect release and route size must agree")
        if len(self.alternate_routes) > 20:
            raise ValueError("Direct Connect alternate route bound exceeded")


@dataclass(frozen=True, slots=True)
class DcClientSearchSummary:
    client_config_id: int
    client_identity: str
    client_name: str
    status: DcClientSearchStatus
    raw_count: int
    retained_count: int
    dropped_count: int
    elapsed_ms: int


@dataclass(frozen=True, slots=True)
class DcSearchOutcome:
    matched: tuple[DcValidatedCandidate, ...]
    rejected: tuple[DcValidatedCandidate, ...]
    client_summaries: tuple[DcClientSearchSummary, ...]
    raw_count: int
    deduplicated_count: int
    dropped_count: int
    elapsed_ms: int
    partial: bool
