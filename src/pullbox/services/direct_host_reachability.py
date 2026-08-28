"""Non-downloading artifact-host reachability and operational status tracking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

import httpx
from sqlalchemy import select

from pullbox.models.direct_acquisition import (
    DirectArtifactFailureClass,
    DirectArtifactHostKind,
    DirectHostAccountState,
    DirectHostConfig,
    DirectHostOperationalResult,
    DirectHostReachabilityState,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession


_PROBE_ENDPOINTS: dict[DirectArtifactHostKind, str] = {
    DirectArtifactHostKind.PIXELDRAIN: "https://pixeldrain.com/",
    DirectArtifactHostKind.MEGA: "https://mega.nz/",
    DirectArtifactHostKind.ROOTZ: "https://rootz.so/",
    DirectArtifactHostKind.MEDIAFIRE: "https://www.mediafire.com/",
    DirectArtifactHostKind.TERABOX: "https://www.terabox.com/",
    DirectArtifactHostKind.DATANODES: "https://datanodes.to/",
}
_ACCOUNT_REQUIRED_HOSTS = frozenset(
    {DirectArtifactHostKind.TERABOX, DirectArtifactHostKind.DATANODES}
)
_STREAMED_GET_PROBE_HOSTS = frozenset({DirectArtifactHostKind.MEGA})
_PROBE_TIMEOUT = httpx.Timeout(connect=10.0, read=10.0, write=10.0, pool=10.0)


@dataclass(frozen=True, slots=True)
class DirectHostProbeObservation:
    """Secret-free result of one bounded service-endpoint request."""

    contacted: bool
    status_code: int | None
    error_code: str | None = None


class DirectHostProbe(Protocol):
    async def __call__(
        self,
        host_kind: DirectArtifactHostKind,
    ) -> DirectHostProbeObservation: ...


class DirectHostOperationError(Protocol):
    code: str
    failure_class: DirectArtifactFailureClass


@dataclass(frozen=True, slots=True)
class DirectHostReachabilityCheck:
    """Operator-facing result of a reachability check that fetched no artifact."""

    reachable: bool
    state: DirectHostReachabilityState
    message: str
    checked_at: datetime
    last_reachable_at: datetime | None


async def probe_direct_host_endpoint(
    host_kind: DirectArtifactHostKind,
) -> DirectHostProbeObservation:
    """Issue one bounded request to a fixed service-owned endpoint."""
    endpoint = _PROBE_ENDPOINTS.get(host_kind)
    if endpoint is None:
        return DirectHostProbeObservation(contacted=False, status_code=None)
    try:
        async with httpx.AsyncClient(
            timeout=_PROBE_TIMEOUT,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            if host_kind in _STREAMED_GET_PROBE_HOSTS:
                async with client.stream(
                    "GET",
                    endpoint,
                    headers={"Accept": "*/*"},
                ) as response:
                    status_code = response.status_code
            else:
                response = await client.head(endpoint, headers={"Accept": "*/*"})
                status_code = response.status_code
    except httpx.TimeoutException:
        return DirectHostProbeObservation(
            contacted=False,
            status_code=None,
            error_code="artifact_host_probe_timeout",
        )
    except httpx.RequestError:
        return DirectHostProbeObservation(
            contacted=False,
            status_code=None,
            error_code="artifact_host_probe_unreachable",
        )
    return DirectHostProbeObservation(contacted=True, status_code=status_code)


async def check_direct_host_reachability(
    session: AsyncSession,
    host_kind: DirectArtifactHostKind,
    *,
    probe: DirectHostProbe = probe_direct_host_endpoint,
    now: Callable[[], datetime] | None = None,
) -> DirectHostReachabilityCheck:
    """Check service reachability without resolving or transferring an artifact."""
    checked_at = (now or (lambda: datetime.now(UTC)))()
    config = await _get_or_create_config(session, host_kind)

    if host_kind is DirectArtifactHostKind.GENERIC_HTTPS:
        config.reachability_state = DirectHostReachabilityState.NOT_CHECKED
        config.last_tested_at = checked_at
        config.last_error_code = None
        result = DirectHostReachabilityCheck(
            reachable=False,
            state=DirectHostReachabilityState.NOT_CHECKED,
            message=(
                "Generic HTTPS has no single service endpoint and is verified per download; "
                "this check does not download an artifact."
            ),
            checked_at=checked_at,
            last_reachable_at=config.last_reachable_at,
        )
    elif host_kind in _ACCOUNT_REQUIRED_HOSTS and not config.encrypted_credentials:
        config.reachability_state = DirectHostReachabilityState.AUTHENTICATION_REQUIRED
        config.last_tested_at = checked_at
        config.last_error_code = "artifact_host_auth_required"
        result = DirectHostReachabilityCheck(
            reachable=False,
            state=DirectHostReachabilityState.AUTHENTICATION_REQUIRED,
            message="Configure the required account credentials before testing this host.",
            checked_at=checked_at,
            last_reachable_at=config.last_reachable_at,
        )
    else:
        observation = await probe(host_kind)
        state = _state_for_observation(config, observation)
        config.reachability_state = state
        config.last_tested_at = checked_at
        config.last_error_code = observation.error_code or _error_code_for_state(state)
        if observation.contacted:
            config.last_reachable_at = checked_at
        result = DirectHostReachabilityCheck(
            reachable=state is DirectHostReachabilityState.REACHABLE,
            state=state,
            message=_message_for_state(host_kind, state),
            checked_at=checked_at,
            last_reachable_at=config.last_reachable_at,
        )

    await session.commit()
    return result


async def record_direct_host_operational_result(
    session: AsyncSession,
    *,
    host_config_id: int | None,
    occurred_at: datetime,
    succeeded: bool,
    error: DirectHostOperationError | None,
) -> None:
    """Record a real user-requested host operation independently of a probe."""
    if host_config_id is None:
        return
    config = await session.get(DirectHostConfig, host_config_id)
    if config is None:
        return

    config.last_operational_result = (
        DirectHostOperationalResult.SUCCESSFUL if succeeded else DirectHostOperationalResult.FAILED
    )
    config.last_operational_at = occurred_at
    config.last_error_code = None if succeeded else getattr(error, "code", None)

    # Generic HTTPS represents arbitrary final-file origins, not one host whose
    # global reachability can be inferred from a single transfer.
    if config.host_kind is DirectArtifactHostKind.GENERIC_HTTPS:
        config.reachability_state = DirectHostReachabilityState.NOT_CHECKED
        return

    has_credentials = bool(config.encrypted_credentials)

    if succeeded:
        config.reachability_state = DirectHostReachabilityState.REACHABLE
        config.last_reachable_at = occurred_at
        if has_credentials:
            config.account_state = DirectHostAccountState.HEALTHY
        return

    if error is None:
        return
    failure_class = error.failure_class
    if failure_class is DirectArtifactFailureClass.ARTIFACT_HOST_AUTH_REQUIRED:
        config.reachability_state = DirectHostReachabilityState.AUTHENTICATION_REQUIRED
        config.last_reachable_at = occurred_at
        if has_credentials:
            config.account_state = DirectHostAccountState.AUTHENTICATION_REQUIRED
    elif failure_class is DirectArtifactFailureClass.HOST_QUOTA:
        config.reachability_state = DirectHostReachabilityState.QUOTA_LIMITED
        config.last_reachable_at = occurred_at
        config.quota_remaining = 0
        if has_credentials:
            config.account_state = DirectHostAccountState.QUOTA_LIMITED
    elif failure_class is DirectArtifactFailureClass.TRANSIENT_HOST:
        config.reachability_state = DirectHostReachabilityState.UNAVAILABLE
        if has_credentials:
            config.account_state = DirectHostAccountState.UNAVAILABLE
    elif failure_class in {
        DirectArtifactFailureClass.PERMANENT_MIRROR,
        DirectArtifactFailureClass.UNSUPPORTED_ARTIFACT_HOST,
        DirectArtifactFailureClass.ARTIFACT_HOST_CHALLENGE,
        DirectArtifactFailureClass.RESOLVER,
    }:
        config.reachability_state = DirectHostReachabilityState.REACHABLE
        config.last_reachable_at = occurred_at


async def _get_or_create_config(
    session: AsyncSession,
    host_kind: DirectArtifactHostKind,
) -> DirectHostConfig:
    config = (
        await session.execute(
            select(DirectHostConfig).where(DirectHostConfig.host_kind == host_kind)
        )
    ).scalar_one_or_none()
    if config is None:
        config = DirectHostConfig(host_kind=host_kind)
        session.add(config)
        await session.flush()
    return config


def _state_for_observation(
    config: DirectHostConfig,
    observation: DirectHostProbeObservation,
) -> DirectHostReachabilityState:
    if not observation.contacted or observation.status_code is None:
        return DirectHostReachabilityState.NOT_REACHABLE
    if observation.status_code == 401 and config.encrypted_credentials:
        return DirectHostReachabilityState.AUTHENTICATION_REQUIRED
    if observation.status_code == 429 and config.encrypted_credentials:
        return DirectHostReachabilityState.QUOTA_LIMITED
    if observation.status_code >= 500:
        return DirectHostReachabilityState.UNAVAILABLE
    return DirectHostReachabilityState.REACHABLE


def _error_code_for_state(state: DirectHostReachabilityState) -> str | None:
    return {
        DirectHostReachabilityState.NOT_REACHABLE: "artifact_host_not_reachable",
        DirectHostReachabilityState.AUTHENTICATION_REQUIRED: "artifact_host_auth_required",
        DirectHostReachabilityState.QUOTA_LIMITED: "artifact_host_quota_limited",
        DirectHostReachabilityState.UNAVAILABLE: "artifact_host_unavailable",
    }.get(state)


def _message_for_state(
    host_kind: DirectArtifactHostKind,
    state: DirectHostReachabilityState,
) -> str:
    display_name = host_kind.value.replace("_", " ").title()
    suffix = " This check does not download an artifact."
    messages = {
        DirectHostReachabilityState.REACHABLE: f"{display_name} is reachable.",
        DirectHostReachabilityState.NOT_REACHABLE: f"{display_name} could not be reached.",
        DirectHostReachabilityState.AUTHENTICATION_REQUIRED: (
            f"{display_name} requires account authentication."
        ),
        DirectHostReachabilityState.QUOTA_LIMITED: f"{display_name} reports a quota limit.",
        DirectHostReachabilityState.UNAVAILABLE: (
            f"{display_name} responded but is currently unavailable."
        ),
        DirectHostReachabilityState.NOT_CHECKED: f"{display_name} was not checked.",
    }
    return messages[state] + suffix
