"""Strict required and additive-compatible AirDC++ wire contracts."""

from __future__ import annotations

from math import isfinite
from typing import Annotated

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)


def _normalize_port(value: object) -> object:
    """Normalize AirDC++'s numeric-string listener ports at the wire boundary."""
    if isinstance(value, str) and 1 <= len(value) <= 5 and value.isascii() and value.isdigit():
        return int(value)
    return value


def _normalize_current_search_id(value: object) -> object:
    """Map AirDC++'s pre-dispatch empty sentinel to the internal zero value."""
    return 0 if value == "" else value


def _normalize_whole_number(value: object) -> object:
    """Normalize AirDC++ whole-number JSON floats without truncating fractions."""
    if type(value) is float and isfinite(value) and value.is_integer():
        return int(value)
    return value


PositiveInt = Annotated[StrictInt, Field(gt=0)]
NonNegativeInt = Annotated[StrictInt, Field(ge=0)]
Port = Annotated[StrictInt, BeforeValidator(_normalize_port), Field(ge=0, le=65535)]
BoundedString = Annotated[StrictStr, Field(min_length=1, max_length=1000)]
SearchId = BoundedString | PositiveInt
CurrentSearchId = Annotated[
    BoundedString | NonNegativeInt,
    BeforeValidator(_normalize_current_search_id),
]
WholeNonNegativeInt = Annotated[
    StrictInt,
    BeforeValidator(_normalize_whole_number),
    Field(ge=0, le=2**63 - 1),
]
WholePositiveInt = Annotated[
    StrictInt,
    BeforeValidator(_normalize_whole_number),
    Field(gt=0, le=2**63 - 1),
]


class AirDcppWireModel(BaseModel):
    """Base policy: strict known fields while accepting additive server fields."""

    model_config = ConfigDict(extra="ignore", strict=True)


class AirDcppSystemInfo(AirDcppWireModel):
    api_version: NonNegativeInt
    api_feature_level: NonNegativeInt
    client_version: BoundedString
    platform: BoundedString
    path_separator: Annotated[StrictStr, Field(min_length=1, max_length=8)]


class AirDcppWebUser(AirDcppWireModel):
    username: Annotated[StrictStr, Field(min_length=1, max_length=255, repr=False)]
    permissions: Annotated[list[BoundedString], Field(max_length=100)]


class AirDcppAuthenticationInfo(AirDcppWireModel):
    session_id: PositiveInt
    auth_token: SecretStr = Field(repr=False)
    token_type: Annotated[StrictStr, Field(pattern=r"(?i)^bearer$", max_length=32)]
    system_info: AirDcppSystemInfo
    user: AirDcppWebUser
    wizard_pending: StrictBool


class AirDcppSession(AirDcppWireModel):
    id: PositiveInt
    user: AirDcppWebUser


class AirDcppHubConnectState(AirDcppWireModel):
    id: BoundedString
    str: Annotated[StrictStr, Field(max_length=500)]


class AirDcppHub(AirDcppWireModel):
    id: PositiveInt
    hub_url: SecretStr = Field(repr=False)
    connect_state: AirDcppHubConnectState

    @property
    def connected(self) -> bool:
        return self.connect_state.id == "connected"


class AirDcppConnectivityStatus(AirDcppWireModel):
    auto_detect: StrictBool
    enabled: StrictBool
    text: Annotated[StrictStr, Field(max_length=500)]
    bind_address: SecretStr = Field(repr=False)
    external_ip: SecretStr = Field(repr=False)


class AirDcppConnectivityInfo(AirDcppWireModel):
    status_v4: AirDcppConnectivityStatus
    status_v6: AirDcppConnectivityStatus
    tcp_port: Port
    tls_port: Port
    udp_port: Port


class AirDcppQueuePriority(AirDcppWireModel):
    id: Annotated[StrictInt, Field(ge=-1, le=6)]
    str: Annotated[StrictStr, Field(max_length=100)]
    auto: StrictBool


class AirDcppQueueSourceInfo(AirDcppWireModel):
    online: NonNegativeInt
    total: NonNegativeInt
    str: Annotated[StrictStr, Field(max_length=100)]

    @model_validator(mode="after")
    def validate_counts(self) -> AirDcppQueueSourceInfo:
        if self.online > self.total:
            raise ValueError("online source count cannot exceed total")
        return self


class AirDcppQueueStatus(AirDcppWireModel):
    id: BoundedString
    failed: StrictBool
    downloaded: StrictBool
    completed: StrictBool
    str: Annotated[StrictStr, Field(max_length=500)]


class AirDcppFileItemType(AirDcppWireModel):
    id: BoundedString


class AirDcppQueueBundle(AirDcppWireModel):
    id: PositiveInt
    name: BoundedString
    target: SecretStr = Field(repr=False)
    type: AirDcppFileItemType
    size: WholePositiveInt
    downloaded_bytes: WholeNonNegativeInt
    priority: AirDcppQueuePriority
    time_added: WholeNonNegativeInt
    time_finished: WholeNonNegativeInt
    speed: WholeNonNegativeInt
    seconds_left: WholeNonNegativeInt
    sources: AirDcppQueueSourceInfo
    status: AirDcppQueueStatus

    @model_validator(mode="after")
    def validate_progress(self) -> AirDcppQueueBundle:
        if self.downloaded_bytes > self.size:
            raise ValueError("downloaded bytes cannot exceed bundle size")
        return self


class AirDcppQueueBundleAddInfo(AirDcppWireModel):
    """Stable identity returned for both new and merged queue bundles."""

    id: PositiveInt
    merged: StrictBool


class AirDcppSearchDownloadResponse(AirDcppWireModel):
    """File-only projection of the grouped search-result download response."""

    bundle_info: AirDcppQueueBundleAddInfo | None = None
    directory_downloads: list[dict[str, object]] | None = None

    @model_validator(mode="after")
    def validate_file_response(self) -> AirDcppSearchDownloadResponse:
        if self.bundle_info is None or self.directory_downloads is not None:
            raise ValueError("AirDC++ did not return an individual file bundle")
        return self


class AirDcppQueueFile(AirDcppWireModel):
    """Queue file identity used only for exact-TTH mutation recovery."""

    id: PositiveInt
    name: BoundedString
    target: SecretStr = Field(repr=False)
    type: AirDcppFileItemType
    bundle_id: PositiveInt = Field(alias="bundle")
    size: WholePositiveInt
    downloaded_bytes: WholeNonNegativeInt
    priority: AirDcppQueuePriority
    time_added: WholeNonNegativeInt
    time_finished: WholeNonNegativeInt
    speed: WholeNonNegativeInt
    seconds_left: WholeNonNegativeInt
    sources: AirDcppQueueSourceInfo
    status: AirDcppQueueStatus
    tth: Annotated[StrictStr, Field(pattern=r"^[A-Z2-7]{39}$")]

    @model_validator(mode="after")
    def validate_progress(self) -> AirDcppQueueFile:
        if self.downloaded_bytes > self.size:
            raise ValueError("downloaded bytes cannot exceed file size")
        if self.type.id != "file":
            raise ValueError("AirDC++ queue recovery supports file items only")
        return self


class AirDcppSearchInstance(AirDcppWireModel):
    """One temporary AirDC++ result collection instance."""

    id: PositiveInt
    expires_in: NonNegativeInt
    current_search_id: CurrentSearchId
    owner: BoundedString
    queue_time: NonNegativeInt
    queued_count: NonNegativeInt
    result_count: NonNegativeInt
    searches_sent_ago: NonNegativeInt


class AirDcppSearchUsers(AirDcppWireModel):
    """Bounded grouped-source count; peer identity is deliberately ignored."""

    count: NonNegativeInt


class AirDcppSearchSlots(AirDcppWireModel):
    free: NonNegativeInt
    total: NonNegativeInt
    str: Annotated[StrictStr, Field(max_length=100)]

    @model_validator(mode="after")
    def validate_counts(self) -> AirDcppSearchSlots:
        if self.free > self.total:
            raise ValueError("free slot count cannot exceed total")
        return self


class AirDcppSearchResult(AirDcppWireModel):
    """Grouped result without retained peer identity or source path output."""

    id: BoundedString
    name: BoundedString
    relevance: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    hits: WholeNonNegativeInt
    users: AirDcppSearchUsers
    type: AirDcppFileItemType
    path: SecretStr = Field(repr=False)
    tth: Annotated[StrictStr, Field(pattern=r"^[A-Z2-7]{39}$")] | None
    time: WholeNonNegativeInt
    slots: AirDcppSearchSlots
    connection: WholeNonNegativeInt
    size: WholeNonNegativeInt

    @property
    def file_result(self) -> bool:
        return self.type.id == "file"


class AirDcppSearchSentEvent(AirDcppWireModel):
    """Hub-dispatch acknowledgement for one temporary search instance."""

    sent: NonNegativeInt
    search_id: SearchId


class AirDcppSearchResultEvent(AirDcppWireModel):
    """Grouped search result wrapper emitted by the AirDC++ socket."""

    result: AirDcppSearchResult
    search_id: SearchId
