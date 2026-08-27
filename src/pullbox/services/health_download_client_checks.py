"""Download-client-specific health check implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from pullbox.models.health import HealthStatus
from pullbox.services.health_helpers import (
    _STATUS_PRECEDENCE,
    _download_client_endpoint_details,
    _download_client_failure_kind,
    _download_client_type_display,
    _serialize_download_client_summary,
    _serialize_sub_check,
)
from pullbox.services.health_types import CheckOutcome, SubCheckOutcome

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.client import DownloadClientConfig
    from pullbox.providers.base import DownloadClient, ProviderRegistry


async def check_download_clients(
    session: AsyncSession,
    *,
    registry: ProviderRegistry | None,
    bootstrap_errors: dict[str, list[dict[str, str]]],
    check_subject: Callable[[DownloadClientConfig, DownloadClient], Awaitable[CheckOutcome]],
    bootstrap_outcome: Callable[[DownloadClientConfig, Mapping[str, str]], CheckOutcome],
    unknown_outcome: Callable[[DownloadClientConfig], CheckOutcome],
) -> list[CheckOutcome]:
    """Test download clients as a grouped multi-entity health component."""
    from pullbox.models.client import DownloadClientConfig
    from pullbox.models.download import DownloadClientType

    result = await session.execute(
        select(DownloadClientConfig).where(DownloadClientConfig.enabled.is_(True))
    )
    client_configs = list(result.scalars().all())
    direct_outcomes = await _check_direct_download_subjects(session)
    bootstrap_checks = list(bootstrap_errors.get("download_clients", []))

    if not client_configs and not direct_outcomes:
        return [
            CheckOutcome(
                component="download_clients",
                check_name="connectivity",
                status=HealthStatus.UNKNOWN,
                message="Not configured",
                actionable_guidance=(
                    "Configure a download client in Settings > Download Clients or a direct "
                    "provider in Settings > Direct Downloads."
                ),
            )
        ]

    clients_by_id = {
        str(config_id): client
        for config_id, client in (registry.get_download_client_items() if registry else [])
    }
    bootstrap_by_id = {
        str(raw.get("config_id")): raw for raw in bootstrap_checks if raw.get("config_id")
    }
    generic_bootstrap_error = next(
        (raw for raw in bootstrap_checks if not raw.get("config_id")),
        None,
    )

    subject_outcomes: list[CheckOutcome] = []
    summary_checks: list[dict[str, Any]] = []
    flagged_names: list[str] = []
    healthy_count = 0
    total_ms = 0.0

    for config in client_configs:
        config_key = str(config.id)
        if config_key in bootstrap_by_id:
            outcome = bootstrap_outcome(config, bootstrap_by_id[config_key])
        elif config_key in clients_by_id:
            outcome = await check_subject(config, clients_by_id[config_key])
        elif config.client_type is DownloadClientType.AIRDCPP:
            outcome = _airdcpp_supervisor_outcome(config) or unknown_outcome(config)
        elif generic_bootstrap_error:
            outcome = bootstrap_outcome(config, generic_bootstrap_error)
        else:
            outcome = unknown_outcome(config)

        subject_outcomes.append(outcome)
        summary_checks.append(_serialize_download_client_summary(outcome))
        total_ms += outcome.response_time_ms
        if outcome.status == HealthStatus.HEALTHY:
            healthy_count += 1
        else:
            flagged_names.append(config.name)

    subject_outcomes.extend(direct_outcomes)
    for outcome in direct_outcomes:
        summary_checks.append(_serialize_download_client_summary(outcome))
        total_ms += outcome.response_time_ms
        if outcome.status == HealthStatus.HEALTHY:
            healthy_count += 1
        else:
            flagged_names.append(outcome.subject_label or "Direct acquisition")

    total = len(subject_outcomes)

    if flagged_names and healthy_count == 0:
        component_status = HealthStatus.UNHEALTHY
        message = (
            "All acquisition routes need attention"
            if direct_outcomes
            else "All clients unreachable or misconfigured"
        )
        guidance = "Review download-client and direct-download configuration in Settings."
    elif flagged_names:
        component_status = HealthStatus.DEGRADED
        message = (
            f"{len(flagged_names)} of {total} acquisition route(s) need attention"
            if direct_outcomes
            else f"{len(flagged_names)} of {total} client(s) need attention"
        )
        guidance = (
            f"Review {', '.join(flagged_names)} in Settings > Direct Downloads."
            if direct_outcomes
            else f"Review {', '.join(flagged_names)} in Settings > Download Clients."
        )
    elif healthy_count == 0:
        component_status = HealthStatus.UNKNOWN
        message = "No acquisition route has completed a health check"
        guidance = (
            "Test direct download providers and artifact hosts in Settings > Direct Downloads."
        )
    else:
        component_status = HealthStatus.HEALTHY
        message = "All acquisition routes available" if direct_outcomes else "All clients reachable"
        guidance = ""

    return [
        CheckOutcome(
            component="download_clients",
            check_name="connectivity",
            status=component_status,
            message=message,
            details={"checks": summary_checks},
            response_time_ms=total_ms,
            actionable_guidance=guidance,
        ),
        *subject_outcomes,
    ]


def _airdcpp_supervisor_outcome(config: Any) -> CheckOutcome | None:
    """Project the live AirDC++ supervisor without another remote health call."""
    from pullbox.composition.airdcpp import get_airdcpp_supervisor_registry
    from pullbox.providers.airdcpp.supervisor import AirDcppSupervisorState

    registry = get_airdcpp_supervisor_registry()
    supervisor = registry.get(config.id) if registry is not None else None
    if supervisor is None:
        return None
    health = supervisor.health
    state = health.state
    if state is AirDcppSupervisorState.READY:
        status = HealthStatus.HEALTHY
        message = "AirDC++ search and queue services are ready"
        guidance = ""
    elif state in {
        AirDcppSupervisorState.CONNECTING,
        AirDcppSupervisorState.COMPATIBLE_REST,
        AirDcppSupervisorState.SOCKET_CONNECTING,
        AirDcppSupervisorState.DEGRADED_SOCKET,
    }:
        status = HealthStatus.DEGRADED
        message = "AirDC++ is reconnecting"
        guidance = "Wait for the AirDC++ connection to recover, then refresh this check."
    elif state is AirDcppSupervisorState.DISABLED:
        status = HealthStatus.UNKNOWN
        message = "AirDC++ supervisor is disabled"
        guidance = "Enable this client in Settings > Download Clients."
    else:
        status = HealthStatus.UNHEALTHY
        message = _airdcpp_health_message(state)
        guidance = "Test this client in Settings > Download Clients and review its permissions."

    compatible_status = (
        HealthStatus.HEALTHY
        if health.compatible
        else (
            HealthStatus.UNHEALTHY
            if state is AirDcppSupervisorState.INCOMPATIBLE
            else HealthStatus.UNKNOWN
        )
    )
    permission_status = (
        HealthStatus.UNHEALTHY
        if state is AirDcppSupervisorState.PERMISSION_FAILED
        else (HealthStatus.HEALTHY if health.compatible else HealthStatus.UNKNOWN)
    )
    socket_status = (
        HealthStatus.HEALTHY
        if state is AirDcppSupervisorState.READY
        else (
            HealthStatus.DEGRADED
            if state
            in {
                AirDcppSupervisorState.SOCKET_CONNECTING,
                AirDcppSupervisorState.DEGRADED_SOCKET,
            }
            else HealthStatus.UNKNOWN
        )
    )
    cooldown_status = (
        HealthStatus.UNHEALTHY
        if state is AirDcppSupervisorState.UNSAFE_SEARCH_INTERVAL
        else (
            HealthStatus.HEALTHY
            if health.remote_min_search_interval_seconds is not None
            and health.remote_min_search_interval_seconds >= 45
            else HealthStatus.UNKNOWN
        )
    )
    sub_checks = (
        SubCheckOutcome(
            check_name="api_compatibility",
            name="API compatibility",
            status=compatible_status,
            message=(
                f"API v{health.api_version}, feature level {health.api_feature_level}"
                if health.api_version is not None
                else "Waiting for API compatibility check"
            ),
            subject_key=str(config.id),
            subject_label=config.name,
        ),
        SubCheckOutcome(
            check_name="permissions",
            name="Permissions",
            status=permission_status,
            message=(
                "Required least-privilege permissions available"
                if permission_status is HealthStatus.HEALTHY
                else "Required permissions are unavailable"
            ),
            subject_key=str(config.id),
            subject_label=config.name,
        ),
        SubCheckOutcome(
            check_name="socket",
            name="Event connection",
            status=socket_status,
            message=(
                "Connected"
                if socket_status is HealthStatus.HEALTHY
                else "Reconnecting or not yet connected"
            ),
            subject_key=str(config.id),
            subject_label=config.name,
        ),
        SubCheckOutcome(
            check_name="search_cooldown",
            name="Hub search cooldown",
            status=cooldown_status,
            message=(
                f"{health.remote_min_search_interval_seconds} seconds"
                if health.remote_min_search_interval_seconds is not None
                else "Not yet verified"
            ),
            subject_key=str(config.id),
            subject_label=config.name,
        ),
    )
    endpoint = _download_client_endpoint_details(config.url)
    return CheckOutcome(
        component="download_clients",
        check_name="client_summary",
        status=status,
        message=message,
        subject_key=str(config.id),
        subject_label=config.name,
        details={
            "checks": [_serialize_sub_check(check) for check in sub_checks],
            "client_type": config.client_type.value,
            "url": config.url,
            "protocol": endpoint["protocol"],
            "host": endpoint["host"],
            "port": endpoint["port"],
            "supervisor_state": state.value,
            "api_version": health.api_version,
            "api_feature_level": health.api_feature_level,
            "minimum_search_interval_seconds": health.remote_min_search_interval_seconds,
            "reconnect_attempts": health.reconnect_attempts,
            "last_ready_at": health.last_ready_at.isoformat() if health.last_ready_at else None,
            "last_error_code": health.last_error_code,
        },
        actionable_guidance=guidance,
        sub_checks=sub_checks,
    )


def _airdcpp_health_message(state: Any) -> str:
    from pullbox.providers.airdcpp.supervisor import AirDcppSupervisorState

    return {
        AirDcppSupervisorState.AUTHENTICATION_FAILED: "AirDC++ authentication failed",
        AirDcppSupervisorState.PERMISSION_FAILED: "AirDC++ permissions are incomplete",
        AirDcppSupervisorState.INCOMPATIBLE: "AirDC++ API version is incompatible",
        AirDcppSupervisorState.UNSAFE_SEARCH_INTERVAL: (
            "AirDC++ minimum search interval must be at least 45 seconds"
        ),
        AirDcppSupervisorState.UNAVAILABLE: "AirDC++ is unavailable",
        AirDcppSupervisorState.STOPPING: "AirDC++ supervisor is stopping",
    }.get(state, "AirDC++ needs attention")


async def _check_direct_download_subjects(session: AsyncSession) -> list[CheckOutcome]:
    """Project persisted direct-provider and artifact-host health into this component.

    Direct providers and artifact hosts already record bounded protocol and
    reachability checks in their own settings workflows. Reusing those results
    keeps scheduled health non-destructive: it never resolves or downloads an
    artifact just to prove a source is available.
    """
    from pullbox.models.direct_acquisition import (
        DirectArtifactHostKind,
        DirectHostConfig,
        DirectHostReachabilityState,
        DirectProviderConfig,
    )

    providers = list(
        (
            await session.execute(
                select(DirectProviderConfig)
                .where(DirectProviderConfig.enabled.is_(True))
                .order_by(DirectProviderConfig.priority, DirectProviderConfig.display_name)
            )
        )
        .scalars()
        .all()
    )
    configured_hosts = list(
        (
            await session.execute(
                select(DirectHostConfig)
                .where(DirectHostConfig.enabled.is_(True))
                .order_by(DirectHostConfig.preference, DirectHostConfig.host_kind)
            )
        )
        .scalars()
        .all()
    )
    hosts = []
    for host in configured_hosts:
        if host.host_kind is DirectArtifactHostKind.GENERIC_HTTPS:
            host.reachability_state = DirectHostReachabilityState.NOT_CHECKED
            continue
        hosts.append(host)

    return [
        *(_direct_provider_outcome(provider) for provider in providers),
        *(_direct_artifact_host_outcome(host) for host in hosts),
    ]


def _direct_provider_outcome(config: Any) -> CheckOutcome:
    """Build a health subject from a provider's last authenticated protocol test."""
    from pullbox.models.direct_acquisition import DirectProviderState

    status = {
        DirectProviderState.HEALTHY: HealthStatus.HEALTHY,
        DirectProviderState.DEGRADED: HealthStatus.DEGRADED,
        DirectProviderState.RATE_LIMITED: HealthStatus.DEGRADED,
        DirectProviderState.AUTHENTICATION_REQUIRED: HealthStatus.UNHEALTHY,
        DirectProviderState.INCOMPATIBLE: HealthStatus.UNHEALTHY,
        DirectProviderState.UNAVAILABLE: HealthStatus.UNHEALTHY,
        DirectProviderState.DISABLED: HealthStatus.UNKNOWN,
    }.get(config.state, HealthStatus.UNKNOWN)
    endpoint = _download_client_endpoint_details(config.endpoint)
    message = {
        HealthStatus.HEALTHY: "Provider protocol is available",
        HealthStatus.DEGRADED: "Provider is available with limits",
        HealthStatus.UNHEALTHY: "Provider needs attention",
        HealthStatus.UNKNOWN: "Provider has not completed a health check",
    }[status]
    sub_check = SubCheckOutcome(
        check_name="provider_protocol",
        name="Provider protocol",
        status=status,
        message=message,
        subject_key=f"direct-provider:{config.id}",
        subject_label=config.display_name,
    )
    return CheckOutcome(
        component="download_clients",
        check_name="direct_provider_summary",
        status=status,
        message=message,
        subject_key=f"direct-provider:{config.id}",
        subject_label=config.display_name,
        details={
            "checks": [_serialize_sub_check(sub_check)],
            "client_type": "direct_provider",
            "provider_id": config.provider_id,
            "url": config.endpoint,
            "protocol": endpoint["protocol"],
            "host": endpoint["host"],
            "port": endpoint["port"],
            "provider_state": config.state.value,
            "last_error_code": config.last_error_code,
        },
        actionable_guidance=(
            "Test or reconfigure this provider in Settings > Direct Downloads."
            if status != HealthStatus.HEALTHY
            else ""
        ),
        sub_checks=(sub_check,),
    )


def _direct_artifact_host_outcome(config: Any) -> CheckOutcome:
    """Build a health subject from an artifact host's persisted reachability."""
    from pullbox.models.direct_acquisition import DirectHostReachabilityState

    status = {
        DirectHostReachabilityState.REACHABLE: HealthStatus.HEALTHY,
        DirectHostReachabilityState.AUTHENTICATION_REQUIRED: HealthStatus.DEGRADED,
        DirectHostReachabilityState.QUOTA_LIMITED: HealthStatus.DEGRADED,
        DirectHostReachabilityState.NOT_REACHABLE: HealthStatus.UNHEALTHY,
        DirectHostReachabilityState.UNAVAILABLE: HealthStatus.UNHEALTHY,
        DirectHostReachabilityState.NOT_CHECKED: HealthStatus.UNKNOWN,
    }.get(config.reachability_state, HealthStatus.UNKNOWN)
    host_name = config.host_kind.value.replace("_", " ").title()
    message = {
        HealthStatus.HEALTHY: "Artifact host reachable",
        HealthStatus.DEGRADED: "Artifact host needs account attention",
        HealthStatus.UNHEALTHY: "Artifact host unavailable",
        HealthStatus.UNKNOWN: "Artifact host has not been checked",
    }[status]
    subject_key = f"artifact-host:{config.host_kind.value}"
    sub_check = SubCheckOutcome(
        check_name="artifact_host_reachability",
        name="Artifact host reachability",
        status=status,
        message=message,
        subject_key=subject_key,
        subject_label=host_name,
    )
    return CheckOutcome(
        component="download_clients",
        check_name="artifact_host_summary",
        status=status,
        message=message,
        subject_key=subject_key,
        subject_label=host_name,
        details={
            "checks": [_serialize_sub_check(sub_check)],
            "client_type": "artifact_host",
            "host_kind": config.host_kind.value,
            "preference": config.preference,
            "reachability_state": config.reachability_state.value,
            "account_state": config.account_state.value,
            "last_error_code": config.last_error_code,
        },
        actionable_guidance=(
            "Test or reconfigure this artifact host in Settings > Direct Downloads."
            if status != HealthStatus.HEALTHY
            else ""
        ),
        sub_checks=(sub_check,),
    )


async def check_download_client_subject(
    config: DownloadClientConfig,
    client: DownloadClient,
    *,
    perf_counter: Callable[[], float],
) -> CheckOutcome:
    """Build a persisted health summary for one download client."""
    test_health = await client.test_connection()
    response_ms = float(test_health.response_time_ms or 0.0)
    failure_kind = _download_client_failure_kind(test_health.message)
    version = str((test_health.details or {}).get("version") or "").strip()
    endpoint_details = _download_client_endpoint_details(config.url)
    guidance_parts: list[str] = []

    if test_health.healthy:
        queue_started = perf_counter()
        try:
            queue_items = await client.get_queue()
            queue_elapsed_ms = (perf_counter() - queue_started) * 1000
            response_ms += queue_elapsed_ms
            queue_check = SubCheckOutcome(
                check_name="queue_access",
                name="Queue access",
                status=HealthStatus.HEALTHY,
                message=(
                    "Queue accessible (empty)"
                    if not queue_items
                    else f"Queue accessible ({len(queue_items)} active)"
                ),
                subject_key=str(config.id),
                subject_label=config.name,
                response_time_ms=queue_elapsed_ms,
                details={"active_count": len(queue_items)},
            )
        except Exception as exc:
            queue_check = SubCheckOutcome(
                check_name="queue_access",
                name="Queue access",
                status=HealthStatus.UNHEALTHY,
                message=f"Queue request failed: {exc}",
                subject_key=str(config.id),
                subject_label=config.name,
            )
            guidance_parts.append(
                "The client answered its identity probe but the queue endpoint failed. "
                "Check the client API logs and permissions."
            )

        sub_checks = (
            SubCheckOutcome(
                check_name="endpoint_reachability",
                name="Endpoint reachability",
                status=HealthStatus.HEALTHY,
                message="Endpoint reachable",
                subject_key=str(config.id),
                subject_label=config.name,
                response_time_ms=test_health.response_time_ms,
            ),
            SubCheckOutcome(
                check_name="authentication",
                name="Authentication",
                status=HealthStatus.HEALTHY,
                message="Credentials accepted",
                subject_key=str(config.id),
                subject_label=config.name,
            ),
            SubCheckOutcome(
                check_name="client_identity",
                name="Client identity",
                status=HealthStatus.HEALTHY,
                message=(
                    f"{_download_client_type_display(config.client_type.value)} {version}"
                    if version
                    else (test_health.message or "Identity probe succeeded")
                ),
                subject_key=str(config.id),
                subject_label=config.name,
                response_time_ms=test_health.response_time_ms,
                details={"version": version} if version else {},
            ),
            queue_check,
        )
    elif failure_kind == "authentication":
        sub_checks = (
            SubCheckOutcome(
                check_name="endpoint_reachability",
                name="Endpoint reachability",
                status=HealthStatus.HEALTHY,
                message="Endpoint responded",
                subject_key=str(config.id),
                subject_label=config.name,
                response_time_ms=test_health.response_time_ms,
            ),
            SubCheckOutcome(
                check_name="authentication",
                name="Authentication",
                status=HealthStatus.UNHEALTHY,
                message=test_health.message or "Authentication failed",
                subject_key=str(config.id),
                subject_label=config.name,
            ),
            SubCheckOutcome(
                check_name="client_identity",
                name="Client identity",
                status=HealthStatus.UNKNOWN,
                message="Blocked by authentication failure",
                subject_key=str(config.id),
                subject_label=config.name,
            ),
            SubCheckOutcome(
                check_name="queue_access",
                name="Queue access",
                status=HealthStatus.UNKNOWN,
                message="Not evaluated",
                subject_key=str(config.id),
                subject_label=config.name,
            ),
        )
        guidance_parts.append(
            "The client endpoint responded but rejected the saved credentials. "
            "Re-save the username, password, or API key in Settings > Download Clients."
        )
    elif failure_kind == "network":
        sub_checks = (
            SubCheckOutcome(
                check_name="endpoint_reachability",
                name="Endpoint reachability",
                status=HealthStatus.UNHEALTHY,
                message=test_health.message or "Endpoint unreachable",
                subject_key=str(config.id),
                subject_label=config.name,
                response_time_ms=test_health.response_time_ms,
            ),
            SubCheckOutcome(
                check_name="authentication",
                name="Authentication",
                status=HealthStatus.UNKNOWN,
                message="Not evaluated",
                subject_key=str(config.id),
                subject_label=config.name,
            ),
            SubCheckOutcome(
                check_name="client_identity",
                name="Client identity",
                status=HealthStatus.UNKNOWN,
                message="Not evaluated",
                subject_key=str(config.id),
                subject_label=config.name,
            ),
            SubCheckOutcome(
                check_name="queue_access",
                name="Queue access",
                status=HealthStatus.UNKNOWN,
                message="Not evaluated",
                subject_key=str(config.id),
                subject_label=config.name,
            ),
        )
        guidance_parts.append(
            "Pullbox could not reach this client. Verify the host, port, protocol, "
            "and that the service is running."
        )
    else:
        sub_checks = (
            SubCheckOutcome(
                check_name="endpoint_reachability",
                name="Endpoint reachability",
                status=HealthStatus.DEGRADED,
                message=test_health.message or "Probe failed",
                subject_key=str(config.id),
                subject_label=config.name,
                response_time_ms=test_health.response_time_ms,
            ),
            SubCheckOutcome(
                check_name="authentication",
                name="Authentication",
                status=HealthStatus.UNKNOWN,
                message="Not evaluated",
                subject_key=str(config.id),
                subject_label=config.name,
            ),
            SubCheckOutcome(
                check_name="client_identity",
                name="Client identity",
                status=HealthStatus.UNKNOWN,
                message="Not evaluated",
                subject_key=str(config.id),
                subject_label=config.name,
            ),
            SubCheckOutcome(
                check_name="queue_access",
                name="Queue access",
                status=HealthStatus.UNKNOWN,
                message="Not evaluated",
                subject_key=str(config.id),
                subject_label=config.name,
            ),
        )
        guidance_parts.append(
            "The client probe failed in an unexpected way. Check the client logs and "
            "network path for more detail."
        )

    worst = max(
        (check.status for check in sub_checks),
        key=lambda status: _STATUS_PRECEDENCE.get(status, 0),
        default=HealthStatus.UNKNOWN,
    )
    if worst == HealthStatus.HEALTHY:
        summary_message = test_health.message or "Connected"
    elif any(
        check.check_name == "authentication" and check.status == HealthStatus.UNHEALTHY
        for check in sub_checks
    ):
        summary_message = "Authentication failed"
    elif any(
        check.check_name == "queue_access" and check.status == HealthStatus.UNHEALTHY
        for check in sub_checks
    ):
        summary_message = "Queue unavailable"
    elif any(
        check.check_name == "endpoint_reachability" and check.status == HealthStatus.UNHEALTHY
        for check in sub_checks
    ):
        summary_message = "Client unreachable"
    else:
        summary_message = "Client needs attention"

    return CheckOutcome(
        component="download_clients",
        check_name="client_summary",
        status=worst,
        message=summary_message,
        subject_key=str(config.id),
        subject_label=config.name,
        details={
            "checks": [_serialize_sub_check(check) for check in sub_checks],
            "client_type": config.client_type.value,
            "url": config.url,
            "protocol": endpoint_details["protocol"],
            "host": endpoint_details["host"],
            "port": endpoint_details["port"],
            "version": version or None,
        },
        response_time_ms=response_ms,
        actionable_guidance=" ".join(dict.fromkeys(guidance_parts)),
        sub_checks=sub_checks,
    )


def download_client_bootstrap_outcome(
    config: DownloadClientConfig,
    bootstrap_error: Mapping[str, str],
) -> CheckOutcome:
    """Return a structured subject outcome for a client that could not load."""
    endpoint_details = _download_client_endpoint_details(config.url)
    message = bootstrap_error.get("message") or "Configuration error"
    sub_checks = (
        SubCheckOutcome(
            check_name="endpoint_reachability",
            name="Endpoint reachability",
            status=HealthStatus.UNKNOWN,
            message="Not evaluated",
            subject_key=str(config.id),
            subject_label=config.name,
        ),
        SubCheckOutcome(
            check_name="authentication",
            name="Authentication",
            status=HealthStatus.UNHEALTHY,
            message=message,
            subject_key=str(config.id),
            subject_label=config.name,
        ),
        SubCheckOutcome(
            check_name="client_identity",
            name="Client identity",
            status=HealthStatus.UNKNOWN,
            message="Not evaluated",
            subject_key=str(config.id),
            subject_label=config.name,
        ),
        SubCheckOutcome(
            check_name="queue_access",
            name="Queue access",
            status=HealthStatus.UNKNOWN,
            message="Not evaluated",
            subject_key=str(config.id),
            subject_label=config.name,
        ),
    )
    return CheckOutcome(
        component="download_clients",
        check_name="client_summary",
        status=HealthStatus.UNHEALTHY,
        message="Configuration error",
        subject_key=str(config.id),
        subject_label=config.name,
        details={
            "checks": [_serialize_sub_check(check) for check in sub_checks],
            "client_type": config.client_type.value,
            "url": config.url,
            "protocol": endpoint_details["protocol"],
            "host": endpoint_details["host"],
            "port": endpoint_details["port"],
        },
        actionable_guidance=(
            "Re-save this client in Settings > Download Clients so Pullbox can "
            "rebuild the provider connection."
        ),
        sub_checks=sub_checks,
    )


def download_client_unknown_outcome(
    config: DownloadClientConfig,
    *,
    message: str,
) -> CheckOutcome:
    """Return a placeholder client outcome when no live result exists yet."""
    endpoint_details = _download_client_endpoint_details(config.url)
    sub_checks = tuple(
        SubCheckOutcome(
            check_name=check_name,
            name=name,
            status=HealthStatus.UNKNOWN,
            message="Waiting for the next health check",
            subject_key=str(config.id),
            subject_label=config.name,
        )
        for check_name, name in (
            ("endpoint_reachability", "Endpoint reachability"),
            ("authentication", "Authentication"),
            ("client_identity", "Client identity"),
            ("queue_access", "Queue access"),
        )
    )
    return CheckOutcome(
        component="download_clients",
        check_name="client_summary",
        status=HealthStatus.UNKNOWN,
        message=message,
        subject_key=str(config.id),
        subject_label=config.name,
        details={
            "checks": [_serialize_sub_check(check) for check in sub_checks],
            "client_type": config.client_type.value,
            "url": config.url,
            "protocol": endpoint_details["protocol"],
            "host": endpoint_details["host"],
            "port": endpoint_details["port"],
        },
        sub_checks=sub_checks,
    )
