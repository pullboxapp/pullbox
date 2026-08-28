"""Resolve provider candidates into deterministic, URL-free acquisition plans."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from pullbox.core.exceptions import ValidationError
from pullbox.core.log_sanitizer import sanitize_log_string
from pullbox.core.name_matcher import NameMatcher
from pullbox.core.release_parser import issues_match, normalize_issue_number
from pullbox.core.type_semantics import TypeFamily, issue_type_family
from pullbox.models.direct_acquisition import (
    DirectAcquisitionAttempt,
    DirectAcquisitionState,
    DirectArtifactAttempt,
    DirectArtifactFailureClass,
    DirectArtifactHostKind,
    DirectArtifactRouteKind,
    DirectArtifactState,
    DirectHostAccountState,
    DirectHostConfig,
    DirectProviderConfig,
    DirectProviderState,
)
from pullbox.models.issue import IssueType
from pullbox.providers.artifact_hosts.contract import (
    ArtifactHostResolutionError,
    HostResolutionRequest,
    sanitize_provider_headers,
)
from pullbox.providers.artifact_hosts.registry import (
    classify_artifact_host,
    is_retired_artifact_host,
)
from pullbox.providers.direct.client import DirectProviderClient, DirectProviderClientError
from pullbox.providers.direct.contract import (
    DirectArtifact,
    DirectArtifactRoute,
    DirectMirror,
    DirectResolveRequest,
    DirectResolveResponse,
)
from pullbox.services.blocklist_service import BlocklistService
from pullbox.services.direct_acquisition_plan import (
    ArtifactPlanSnapshotInput,
    build_plan_snapshot,
    record_acquisition_plan,
)
from pullbox.services.direct_acquisition_state import (
    advance_acquisition_progress,
    transition_acquisition,
)
from pullbox.services.direct_configuration_service import ProviderSecretMaterial
from pullbox.services.direct_coverage_planner import (
    DirectArtifactOption,
    DirectCoveragePlan,
    DirectRouteOption,
    plan_direct_coverage,
)
from pullbox.services.direct_provider_capabilities import manifest_artifact_host_kinds
from pullbox.services.direct_provider_quota import (
    record_provider_quota,
    record_provider_resolution_error,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

_RESOLVE_SECONDS = 60.0
_RESUMABLE_HOSTS = frozenset(
    {
        DirectArtifactHostKind.GENERIC_HTTPS,
        DirectArtifactHostKind.PIXELDRAIN,
        DirectArtifactHostKind.ROOTZ,
    }
)
_ACCOUNT_REQUIRED_HOSTS = frozenset({DirectArtifactHostKind.TERABOX})
_FORMAT_RANK = {"cbz": 0, "cbr": 1, "cb7": 2, "pdf": 3}
_QUALITY_RANK = {"digital": 0, "retail": 1}


class DirectResolveClient(Protocol):
    async def resolve(self, request: DirectResolveRequest) -> DirectResolveResponse: ...

    async def aclose(self) -> None: ...


class DirectResolveClientFactory(Protocol):
    def __call__(
        self,
        *,
        endpoint: str,
        bearer_token: str,
        allow_private_http: bool,
        provider_id: str,
    ) -> DirectResolveClient: ...


ProviderSecretLoader = Callable[[DirectProviderConfig], ProviderSecretMaterial]


class DirectAcquisitionPlanningError(RuntimeError):
    """Stable planning failure that never includes provider URLs or secrets."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        failure_class: DirectArtifactFailureClass = DirectArtifactFailureClass.USER_ACTION,
        retryable: bool = False,
        intervention: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.failure_class = failure_class
        self.retryable = retryable
        self.intervention = intervention

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


@dataclass(frozen=True, slots=True)
class DirectAcquisitionPlanningResult:
    """Persisted acquisition plan plus its selected artifact row."""

    attempt: DirectAcquisitionAttempt
    selected_artifact: DirectArtifactAttempt
    plan: DirectCoveragePlan
    initial_source: HostResolutionRequest


@dataclass(frozen=True, slots=True)
class _ResolvedRoute:
    artifact: DirectArtifact
    mirror: DirectMirror
    artifact_identity: str
    route: DirectRouteOption
    option: DirectArtifactOption


def direct_route_identity(
    provider_identity: str,
    provider_candidate_id: str,
    provider_artifact_id: str,
    mirror_id: str,
) -> str:
    """Return an opaque server-issued identity without persisting provider URLs."""
    digest = hashlib.sha256(
        "\x1f".join(
            (provider_identity, provider_candidate_id, provider_artifact_id, mirror_id)
        ).encode()
    ).hexdigest()[:32]
    return f"route:{digest}"


async def plan_direct_acquisition(
    session: AsyncSession,
    *,
    acquisition_id: int,
    pinned_route_identity: str | None = None,
    provider_client_factory: DirectResolveClientFactory | None = None,
    provider_secret_loader: ProviderSecretLoader | None = None,
    now: Callable[[], datetime] | None = None,
) -> DirectAcquisitionPlanningResult:
    """Resolve and persist one direct candidate using current host eligibility."""
    attempt = await _load_attempt(session, acquisition_id)
    pre_plan_review = (
        attempt.state is DirectAcquisitionState.INTERVENTION and not attempt.artifact_attempts
    )
    if attempt.state is not DirectAcquisitionState.DISCOVERED and not pre_plan_review:
        raise DirectAcquisitionPlanningError(
            "acquisition_not_discovered",
            "Only a discovered or pre-plan reviewed direct result can be planned.",
        )
    clock = now or (lambda: datetime.now(UTC))
    try:
        provider = _validate_planning_source(attempt, at=clock())
        response = await _resolve_provider_candidate(
            session,
            attempt,
            provider,
            provider_client_factory=provider_client_factory,
            provider_secret_loader=provider_secret_loader,
            deadline=clock() + timedelta(seconds=_RESOLVE_SECONDS),
        )
        if response.quota is not None:
            record_provider_quota(provider, response.quota, observed_at=clock())
        host_configs = await _load_host_configs(session)
        title_only_override = _allows_title_only_coverage(attempt, response.artifacts)
        resolved_routes = _build_route_options(
            attempt,
            provider,
            response.artifacts,
            host_configs,
            allow_title_only=title_only_override,
        )
        blocked_routes = await BlocklistService.get_blocked_direct_artifact_routes(
            session,
            {route.route.route_identity for route in resolved_routes},
        )
        resolved_routes = _exclude_blocked_routes(resolved_routes, blocked_routes)
        plan = plan_direct_coverage(
            _requested_coverage(attempt),
            _planner_options(resolved_routes),
            pinned_route_identity=pinned_route_identity,
        )
        if not plan.selected or not plan.complete:
            raise DirectAcquisitionPlanningError(
                "no_eligible_complete_plan",
                "No enabled artifact route completely covers this issue.",
            )
        if len(plan.selected) != 1:
            raise DirectAcquisitionPlanningError(
                "multi_artifact_plan_unsupported",
                "This result requires multiple files and cannot be attached to one issue safely.",
            )
        selected_plan = plan.selected[0]
        selected_route = next(
            route
            for route in resolved_routes
            if route.artifact_identity == selected_plan.artifact_identity
            and route.route.route_identity == selected_plan.selected_route_identity
        )
        initial_source = _host_resolution_request(
            artifact_identity=selected_route.route.route_identity,
            host_kind=selected_route.route.host_kind,
            artifact=selected_route.artifact,
            mirror=selected_route.mirror,
        )
        snapshot = _build_durable_snapshot(
            attempt,
            provider,
            resolved_routes,
            plan,
            selected_route,
            title_only_override=title_only_override,
        )
        record_acquisition_plan(
            attempt,
            revision=(attempt.plan_revision or 0) + 1,
            snapshot=snapshot,
        )
        transition_acquisition(attempt, DirectAcquisitionState.PLANNED, at=clock())
        attempt.failure_class = None
        attempt.failure_code = None
        attempt.error_message = None
        advance_acquisition_progress(
            attempt,
            revision=(attempt.progress_revision or 0) + 1,
            snapshot={
                "schema_version": 1,
                "stage": "planned",
                "selected_host": selected_route.route.host_kind.value,
                "complete_coverage": True,
            },
        )
        artifact_row = DirectArtifactAttempt(
            sequence_no=0,
            artifact_identity=selected_route.route.route_identity,
            route_kind=DirectArtifactRouteKind.DIRECT,
            host_kind=selected_route.route.host_kind,
            state=DirectArtifactState.PLANNED,
            is_selected=True,
            expected_size=selected_route.option.expected_size,
        )
        attempt.artifact_attempts.append(artifact_row)
        await session.flush()
        return DirectAcquisitionPlanningResult(
            attempt,
            artifact_row,
            plan,
            initial_source,
        )
    except DirectAcquisitionPlanningError as exc:
        preserve_pre_plan_review = (
            pre_plan_review and exc.failure_class is DirectArtifactFailureClass.PROVIDER_UNAVAILABLE
        )
        state = (
            DirectAcquisitionState.INTERVENTION
            if exc.intervention or preserve_pre_plan_review
            else DirectAcquisitionState.FAILED
        )
        transition_acquisition(attempt, state, at=clock())
        attempt.failure_class = exc.failure_class
        attempt.failure_code = exc.code
        attempt.error_message = sanitize_log_string(str(exc))
        advance_acquisition_progress(
            attempt,
            revision=(attempt.progress_revision or 0) + 1,
            snapshot={
                "schema_version": 1,
                "stage": state.value,
                "failure_code": exc.code,
            },
        )
        raise


async def plan_direct_acquisition_with_provider_fallback(
    session: AsyncSession,
    *,
    acquisition_id: int,
    pinned_route_identity: str | None = None,
    planner: Callable[..., Awaitable[DirectAcquisitionPlanningResult]] | None = None,
    skip_selected_attempt: bool = False,
) -> DirectAcquisitionPlanningResult:
    """Plan one logical result, trying its opaque provider alternates in order."""
    selected_attempt = await _load_attempt(session, acquisition_id)
    raw_alternate_ids = selected_attempt.candidate_snapshot.get("alternate_attempt_ids", [])
    alternate_ids = (
        [value for value in raw_alternate_ids if isinstance(value, int) and value > 0]
        if isinstance(raw_alternate_ids, list) and pinned_route_identity is None
        else []
    )
    plan = planner or plan_direct_acquisition
    first_error: DirectAcquisitionPlanningError | None = None
    candidate_ids = alternate_ids if skip_selected_attempt else [acquisition_id, *alternate_ids]

    for candidate_id in candidate_ids:
        candidate = await _load_attempt(session, candidate_id)
        if candidate.issue_id != selected_attempt.issue_id:
            continue
        candidate.replace_existing_file = selected_attempt.replace_existing_file
        try:
            planned = await plan(
                session,
                acquisition_id=candidate.id,
                pinned_route_identity=(
                    pinned_route_identity if candidate.id == acquisition_id else None
                ),
            )
        except DirectAcquisitionPlanningError as exc:
            if first_error is None:
                first_error = exc
            continue
        if candidate.id != acquisition_id:
            advance_acquisition_progress(
                selected_attempt,
                revision=selected_attempt.progress_revision + 1,
                snapshot={
                    **selected_attempt.progress_snapshot,
                    "schema_version": 1,
                    "stage": "provider_fallback",
                    "fallback_attempt_id": candidate.id,
                    "fallback_provider_identity": candidate.provider_identity,
                },
            )
            advance_acquisition_progress(
                candidate,
                revision=candidate.progress_revision + 1,
                snapshot={
                    **candidate.progress_snapshot,
                    "schema_version": 1,
                    "fallback_from_attempt_id": selected_attempt.id,
                    "fallback_from_provider_identity": selected_attempt.provider_identity,
                },
            )
            await session.flush()
        return planned

    if first_error is not None:
        raise first_error
    raise DirectAcquisitionPlanningError(
        "provider_fallback_unavailable",
        "No provider route remains available for this direct result.",
        failure_class=DirectArtifactFailureClass.PROVIDER_UNAVAILABLE,
        intervention=False,
    )


async def resolve_planned_artifact_source(
    session: AsyncSession,
    *,
    acquisition_id: int,
    artifact_id: int,
    provider_client_factory: DirectResolveClientFactory | None = None,
    provider_secret_loader: ProviderSecretLoader | None = None,
    now: Callable[[], datetime] | None = None,
) -> HostResolutionRequest:
    """Re-resolve one persisted route and return ephemeral transfer material."""
    attempt = await _load_attempt(session, acquisition_id)
    artifact_row = next(
        (item for item in attempt.artifact_attempts if item.id == artifact_id),
        None,
    )
    if artifact_row is None or not artifact_row.is_selected:
        raise DirectAcquisitionPlanningError(
            "planned_artifact_missing",
            "The selected direct artifact is no longer available.",
        )
    provider = attempt.provider_config
    if provider is None:
        raise DirectAcquisitionPlanningError(
            "provider_configuration_missing",
            "The direct provider configuration is no longer available.",
        )
    mapping = _source_mapping(attempt.plan_snapshot, artifact_row.artifact_identity)
    clock = now or (lambda: datetime.now(UTC))
    response = await _resolve_provider_candidate(
        session,
        attempt,
        provider,
        provider_client_factory=provider_client_factory,
        provider_secret_loader=provider_secret_loader,
        deadline=clock() + timedelta(seconds=_RESOLVE_SECONDS),
    )
    provider_artifact = next(
        (
            item
            for item in response.artifacts
            if item.artifact_id == mapping["provider_artifact_id"]
        ),
        None,
    )
    if provider_artifact is None:
        raise DirectAcquisitionPlanningError(
            "provider_artifact_changed",
            "The provider no longer returns the selected artifact.",
        )
    mirror = next(
        (item for item in provider_artifact.mirrors if item.mirror_id == mapping["mirror_id"]),
        None,
    )
    if mirror is None:
        raise DirectAcquisitionPlanningError(
            "provider_mirror_changed",
            "The provider no longer returns the selected mirror.",
        )
    host_kind = _validated_host_kind(mirror)
    if host_kind is not artifact_row.host_kind:
        raise DirectAcquisitionPlanningError(
            "provider_host_kind_mismatch",
            "The provider artifact host identity changed.",
        )
    return _host_resolution_request(
        artifact_identity=artifact_row.artifact_identity,
        host_kind=host_kind,
        artifact=provider_artifact,
        mirror=mirror,
    )


async def _load_attempt(
    session: AsyncSession,
    acquisition_id: int,
) -> DirectAcquisitionAttempt:
    result = await session.execute(
        select(DirectAcquisitionAttempt)
        .where(DirectAcquisitionAttempt.id == acquisition_id)
        .options(
            selectinload(DirectAcquisitionAttempt.provider_config),
            selectinload(DirectAcquisitionAttempt.artifact_attempts),
        )
    )
    attempt = result.scalar_one_or_none()
    if attempt is None:
        raise DirectAcquisitionPlanningError(
            "acquisition_not_found",
            "The direct acquisition attempt was not found.",
        )
    return attempt


async def _load_host_configs(
    session: AsyncSession,
) -> dict[DirectArtifactHostKind, DirectHostConfig]:
    result = await session.execute(select(DirectHostConfig))
    return {config.host_kind: config for config in result.scalars().all()}


async def _resolve_provider_candidate(
    session: AsyncSession,
    attempt: DirectAcquisitionAttempt,
    provider: DirectProviderConfig,
    *,
    provider_client_factory: DirectResolveClientFactory | None,
    provider_secret_loader: ProviderSecretLoader | None,
    deadline: datetime,
) -> DirectResolveResponse:
    from pullbox.services.direct_configuration_service import load_provider_secret_material
    from pullbox.services.direct_resolver_service import (
        ResolverAttemptProgress,
        build_provider_resolver_profiles,
    )

    load_secret = provider_secret_loader or load_provider_secret_material
    material = load_secret(provider)
    if not material.bearer_token:
        raise DirectAcquisitionPlanningError(
            "provider_authentication_failed",
            "The direct provider bearer token is unavailable.",
        )
    metadata = provider.configuration_metadata or {}
    raw_public = metadata.get("public_values", {})
    public_values = dict(raw_public) if isinstance(raw_public, dict) else {}
    factory = provider_client_factory or _default_client_factory
    client = factory(
        endpoint=provider.endpoint,
        bearer_token=material.bearer_token,
        allow_private_http=metadata.get("allow_private_http") is True,
        provider_id=provider.provider_id,
    )
    try:
        resolver_options = await build_provider_resolver_profiles(session, provider)
        options = (None, *resolver_options)
        last_error: DirectProviderClientError | None = None
        for option_index, option in enumerate(options):
            if option is not None:
                progress = ResolverAttemptProgress(
                    resolver_id=option.resolver_id,
                    resolver_name=option.resolver_name,
                    resolver_kind=option.resolver_kind,
                    attempt=option_index,
                    total=len(resolver_options),
                    scope=f"provider:{provider.provider_id}:resolve",
                )
                advance_acquisition_progress(
                    attempt,
                    revision=(attempt.progress_revision or 0) + 1,
                    snapshot={
                        "schema_version": 1,
                        "stage": "resolver",
                        "resolver_id": progress.resolver_id,
                        "resolver_name": progress.resolver_name,
                        "resolver_kind": progress.resolver_kind.value,
                        "resolver_attempt": progress.attempt,
                        "resolver_total": progress.total,
                        "resolver_scope": progress.scope,
                    },
                )
                await session.commit()
            try:
                return await client.resolve(
                    DirectResolveRequest(
                        protocol_version=(
                            provider.negotiated_protocol or "direct-download-provider/v1"
                        ),
                        request_id=uuid4(),
                        deadline=deadline,
                        provider_config=public_values,
                        source_credentials=material.configuration,
                        resolver_profile=option.profile if option is not None else None,
                        provider_candidate_id=attempt.provider_candidate_id,
                    )
                )
            except DirectProviderClientError as exc:
                last_error = exc
                retry_allowed = exc.code == "browser_challenge_required" or exc.code.startswith(
                    "resolver_"
                )
                if not retry_allowed or option_index == len(options) - 1:
                    record_provider_resolution_error(
                        provider,
                        exc.code,
                        retry_after_seconds=exc.retry_after_seconds,
                    )
                    await session.flush()
                    raise _provider_planning_error(exc) from exc
        if last_error is not None:
            raise _provider_planning_error(last_error) from last_error
        raise DirectAcquisitionPlanningError(
            "provider_resolve_unavailable",
            "The direct provider had no executable resolution path.",
        )
    finally:
        await client.aclose()


def _provider_planning_error(error: DirectProviderClientError) -> DirectAcquisitionPlanningError:
    retryable = error.retryable
    source_failure = error.code in {
        "source_quota_limited",
        "source_authentication_required",
        "source_unavailable",
        "source_malformed_response",
        "candidate_not_found",
    }
    failure_class = (
        DirectArtifactFailureClass.CANDIDATE_INVALID
        if error.code == "candidate_not_found"
        else DirectArtifactFailureClass.PROVIDER_UNAVAILABLE
    )
    return DirectAcquisitionPlanningError(
        error.code,
        (
            "The direct provider is temporarily unavailable."
            if retryable
            else (
                "The selected direct result is no longer downloadable. Try another search result."
                if error.code == "candidate_not_found"
                else "The direct provider could not resolve this result."
            )
        ),
        failure_class=failure_class,
        retryable=retryable,
        intervention=not retryable and not source_failure,
    )


def _default_client_factory(
    *,
    endpoint: str,
    bearer_token: str,
    allow_private_http: bool,
    provider_id: str,
) -> DirectProviderClient:
    return DirectProviderClient(
        endpoint=endpoint,
        bearer_token=bearer_token,
        allow_private_http=allow_private_http,
        provider_id=provider_id,
        request_timeout_seconds=_RESOLVE_SECONDS,
    )


def _validate_planning_source(
    attempt: DirectAcquisitionAttempt,
    *,
    at: datetime,
) -> DirectProviderConfig:
    provider = attempt.provider_config
    if provider is None or provider.id != attempt.provider_config_id:
        raise DirectAcquisitionPlanningError(
            "provider_configuration_missing",
            "The direct provider configuration is no longer available.",
        )
    if not provider.enabled or provider.state is DirectProviderState.DISABLED:
        raise DirectAcquisitionPlanningError(
            "provider_disabled",
            "The direct provider is disabled.",
        )
    if provider.state is DirectProviderState.RATE_LIMITED:
        raise DirectAcquisitionPlanningError(
            "source_quota_limited",
            "The direct provider source quota is exhausted.",
            failure_class=DirectArtifactFailureClass.PROVIDER_UNAVAILABLE,
            intervention=False,
        )
    if provider.state is DirectProviderState.AUTHENTICATION_REQUIRED:
        raise DirectAcquisitionPlanningError(
            "source_authentication_required",
            "The direct provider source authentication needs attention.",
            failure_class=DirectArtifactFailureClass.PROVIDER_UNAVAILABLE,
            intervention=False,
        )
    if provider.state is DirectProviderState.UNAVAILABLE:
        raise DirectAcquisitionPlanningError(
            "source_unavailable",
            "The direct provider source is temporarily unavailable.",
            failure_class=DirectArtifactFailureClass.PROVIDER_UNAVAILABLE,
            retryable=True,
            intervention=False,
        )

    snapshot = attempt.candidate_snapshot or {}
    if snapshot.get("can_resolve") is False:
        raise DirectAcquisitionPlanningError(
            "candidate_not_resolvable",
            "The direct provider marked this result as unavailable for resolution.",
            failure_class=DirectArtifactFailureClass.CANDIDATE_INVALID,
        )
    raw_expiry = snapshot.get("expires_at")
    if raw_expiry is None:
        return provider
    if not isinstance(raw_expiry, str):
        raise DirectAcquisitionPlanningError(
            "candidate_expiry_invalid",
            "The direct result expiration is invalid.",
            failure_class=DirectArtifactFailureClass.CANDIDATE_INVALID,
        )
    try:
        expiry = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DirectAcquisitionPlanningError(
            "candidate_expiry_invalid",
            "The direct result expiration is invalid.",
            failure_class=DirectArtifactFailureClass.CANDIDATE_INVALID,
        ) from exc
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    if expiry <= at:
        raise DirectAcquisitionPlanningError(
            "candidate_expired",
            "The direct result expired and must be searched again.",
            failure_class=DirectArtifactFailureClass.CANDIDATE_INVALID,
        )
    return provider


def _build_route_options(
    attempt: DirectAcquisitionAttempt,
    provider: DirectProviderConfig,
    artifacts: Sequence[DirectArtifact],
    host_configs: Mapping[DirectArtifactHostKind, DirectHostConfig],
    *,
    allow_title_only: bool = False,
) -> list[_ResolvedRoute]:
    routes: list[_ResolvedRoute] = []
    requested = _requested_coverage(attempt)
    requested_volume = _requested_volume(attempt, requested)
    provider_confidence = _candidate_confidence(attempt.candidate_snapshot)
    declared_host_kinds = manifest_artifact_host_kinds(provider.manifest_snapshot)
    internal_generic_https = declared_host_kinds == {DirectArtifactHostKind.GENERIC_HTTPS}
    for artifact in artifacts:
        _validate_stable_identity("provider artifact", artifact.artifact_id)
        if artifact.route is not DirectArtifactRoute.DIRECT_ARTIFACT:
            continue
        coverage = _artifact_coverage(
            artifact,
            requested,
            requested_volume=requested_volume,
            allow_title_only=allow_title_only,
        )
        for mirror in artifact.mirrors:
            location = mirror.final_url or mirror.share_url
            if (
                mirror.host_kind == DirectArtifactHostKind.GENERIC_HTTPS.value
                and location is not None
                and is_retired_artifact_host(location)
            ):
                continue
            _validate_stable_identity("provider mirror", mirror.mirror_id)
            host_kind = _validated_host_kind(mirror)
            config = host_configs.get(host_kind)
            if declared_host_kinds and host_kind not in declared_host_kinds:
                eligible, code = False, "provider_host_not_declared"
            else:
                eligible, code = _route_eligibility(
                    host_kind,
                    config,
                    internal_generic_https=internal_generic_https,
                )
            route_identity = direct_route_identity(
                attempt.provider_identity,
                attempt.provider_candidate_id,
                artifact.artifact_id,
                mirror.mirror_id,
            )
            route = DirectRouteOption(
                route_identity=route_identity,
                route_kind=DirectArtifactRouteKind.DIRECT,
                host_kind=host_kind,
                transport_rank=0 if host_kind is DirectArtifactHostKind.GENERIC_HTTPS else 1,
                eligible=eligible,
                eligibility_code=code,
                host_preference=config.preference if config is not None else 1_000,
                account_state=(
                    config.account_state
                    if config is not None
                    else DirectHostAccountState.NOT_CONFIGURED
                ),
                quota_remaining=config.quota_remaining if config is not None else None,
                resumable=host_kind in _RESUMABLE_HOSTS,
                resolver_required=False,
            )
            artifact_digest = hashlib.sha256(artifact.artifact_id.encode()).hexdigest()[:32]
            artifact_identity = f"artifact:{artifact_digest}"
            option = DirectArtifactOption(
                provider_identity=attempt.provider_identity,
                provider_candidate_id=attempt.provider_candidate_id,
                artifact_identity=artifact_identity,
                coverage=coverage,
                semantic_rank=0,
                quality_rank=_content_quality_rank(artifact),
                expected_size=mirror.size_bytes or artifact.size_bytes,
                provider_confidence=provider_confidence,
                provider_priority=provider.priority,
                routes=(route,),
            )
            routes.append(_ResolvedRoute(artifact, mirror, artifact_identity, route, option))
    return routes


def _build_durable_snapshot(
    attempt: DirectAcquisitionAttempt,
    provider: DirectProviderConfig,
    routes: Sequence[_ResolvedRoute],
    plan: DirectCoveragePlan,
    selected: _ResolvedRoute,
    *,
    title_only_override: bool,
) -> dict[str, object]:
    fallback_identity = _verified_alternative_fallback_identity(
        attempt,
        routes,
        title_only_override=title_only_override,
    )
    ranking_inputs = [
        ArtifactPlanSnapshotInput(
            artifact_identity=route.route.route_identity,
            content_identity=route.artifact_identity,
            content_rank=route.option.semantic_rank + route.option.quality_rank,
            transport_rank=route.route.transport_rank,
            route_kind=route.route.route_kind,
            host_kind=route.route.host_kind,
            eligible=route.route.eligible,
            eligibility_code=route.route.eligibility_code,
            host_preference=route.route.host_preference,
            account_state=route.route.account_state,
            provider_priority=route.option.provider_priority,
            quota_remaining=route.route.quota_remaining,
            range_supported=route.route.resumable,
            resolver_required=route.route.resolver_required,
            expected_size=route.option.expected_size,
            fallback_identity=fallback_identity,
        )
        for route in routes
    ]
    snapshot = build_plan_snapshot(
        provider_identity=attempt.provider_identity,
        provider_candidate_id=attempt.provider_candidate_id,
        selected_artifact_identity=selected.route.route_identity,
        provider_state=provider.state or DirectProviderState.HEALTHY,
        artifacts=ranking_inputs,
    )
    snapshot["coverage"] = {
        "requested": sorted(plan.requested),
        "selected_content_issue_numbers": sorted(selected.option.coverage),
        "uncovered": sorted(plan.uncovered),
        "complete": plan.complete,
        "explanation_code": plan.explanation_code,
        "pinned_route_applied": plan.pinned_route_applied,
        "title_only_override": title_only_override,
    }
    snapshot["route_sources"] = [
        {
            "route_identity": route.route.route_identity,
            "provider_artifact_id": route.artifact.artifact_id,
            "mirror_id": route.mirror.mirror_id,
        }
        for route in sorted(routes, key=lambda item: item.route.route_identity)
    ]
    return snapshot


def _verified_alternative_fallback_identity(
    attempt: DirectAcquisitionAttempt,
    routes: Sequence[_ResolvedRoute],
    *,
    title_only_override: bool,
) -> str | None:
    if not title_only_override or len({route.artifact_identity for route in routes}) < 2:
        return None
    parsed = (attempt.candidate_snapshot or {}).get("parsed")
    candidate_title = parsed.get("series_title") if isinstance(parsed, dict) else None
    if not isinstance(candidate_title, str):
        return None
    normalized_title = NameMatcher.normalize(candidate_title)
    if not normalized_title:
        return None
    requested = "|".join(sorted(_requested_coverage(attempt)))
    digest = hashlib.sha256(f"{normalized_title}|{requested}".encode()).hexdigest()[:32]
    return f"coverage:{digest}"


def _planner_options(routes: Sequence[_ResolvedRoute]) -> list[DirectArtifactOption]:
    """Collapse mirror rows into one content option with multiple routes."""
    grouped: dict[str, list[_ResolvedRoute]] = {}
    for route in routes:
        grouped.setdefault(route.artifact_identity, []).append(route)

    options: list[DirectArtifactOption] = []
    for artifact_identity, grouped_routes in grouped.items():
        representative = grouped_routes[0].option
        options.append(
            DirectArtifactOption(
                provider_identity=representative.provider_identity,
                provider_candidate_id=representative.provider_candidate_id,
                artifact_identity=artifact_identity,
                coverage=representative.coverage,
                semantic_rank=representative.semantic_rank,
                quality_rank=representative.quality_rank,
                expected_size=representative.expected_size,
                provider_confidence=representative.provider_confidence,
                provider_priority=representative.provider_priority,
                routes=tuple(route.route for route in grouped_routes),
            )
        )
    return options


def _exclude_blocked_routes(
    routes: Sequence[_ResolvedRoute],
    blocked_route_identities: set[str],
) -> list[_ResolvedRoute]:
    return [
        replace(
            route,
            route=replace(
                route.route,
                eligible=False,
                eligibility_code="route_blocklisted",
            ),
        )
        if route.route.route_identity in blocked_route_identities
        else route
        for route in routes
    ]


def _requested_coverage(attempt: DirectAcquisitionAttempt) -> frozenset[str]:
    raw = attempt.requested_coverage or {}
    values = raw.get("issue_numbers", [])
    if not isinstance(values, list):
        raise DirectAcquisitionPlanningError(
            "requested_coverage_invalid",
            "The direct acquisition coverage is invalid.",
        )
    requested = frozenset(str(value).strip() for value in values if str(value).strip())
    if not requested:
        raise DirectAcquisitionPlanningError(
            "requested_coverage_invalid",
            "The direct acquisition has no requested issue coverage.",
        )
    return requested


def _artifact_coverage(
    artifact: DirectArtifact,
    requested: frozenset[str],
    *,
    requested_volume: str | None,
    allow_title_only: bool,
) -> frozenset[str]:
    coverage = frozenset(str(value).strip() for value in artifact.coverage.issue_numbers)
    if coverage:
        return coverage
    if artifact.coverage.issue_ids:
        return requested
    if allow_title_only:
        return requested
    artifact_volume = str(artifact.coverage.volume or "").strip()
    if (
        requested_volume is not None
        and artifact_volume
        and _coverage_numbers_match(requested_volume, artifact_volume)
    ):
        return requested
    return frozenset()


def _allows_title_only_coverage(
    attempt: DirectAcquisitionAttempt,
    artifacts: Sequence[DirectArtifact],
) -> bool:
    if not artifacts or len(_requested_coverage(attempt)) != 1:
        return False
    if any(
        artifact.route is not DirectArtifactRoute.DIRECT_ARTIFACT
        or artifact.coverage.issue_numbers
        or artifact.coverage.issue_ids
        or artifact.coverage.volume
        for artifact in artifacts
    ):
        return False
    raw_type = (attempt.requested_coverage or {}).get("issue_type")
    try:
        issue_type = IssueType(str(raw_type))
    except ValueError:
        return False
    if issue_type_family(issue_type) is TypeFamily.STANDARD:
        return False
    semantic = (attempt.candidate_snapshot or {}).get("semantic_decision")
    if not isinstance(semantic, dict):
        return False
    similarity = semantic.get("series_similarity")
    if not isinstance(similarity, int | float) or float(similarity) < 0.98:
        return False
    if len(artifacts) == 1:
        return True
    return semantic.get("is_match") is True and _alternatives_match_candidate_title(
        attempt,
        artifacts,
    )


def _alternatives_match_candidate_title(
    attempt: DirectAcquisitionAttempt,
    artifacts: Sequence[DirectArtifact],
) -> bool:
    parsed = (attempt.candidate_snapshot or {}).get("parsed")
    if not isinstance(parsed, dict):
        return False
    candidate_title = parsed.get("series_title")
    if not isinstance(candidate_title, str):
        return False
    normalized_candidate = NameMatcher.normalize(candidate_title)
    if not normalized_candidate:
        return False
    return all(
        isinstance(artifact.coverage.description, str)
        and NameMatcher.normalize(artifact.coverage.description) == normalized_candidate
        for artifact in artifacts
    )


def _requested_volume(
    attempt: DirectAcquisitionAttempt,
    requested: frozenset[str],
) -> str | None:
    raw = attempt.requested_coverage or {}
    raw_issue_type = raw.get("issue_type")
    try:
        issue_type = IssueType(str(raw_issue_type))
    except ValueError:
        return None
    if issue_type_family(issue_type) is not TypeFamily.COLLECTION:
        return None

    raw_volume = raw.get("volume")
    if raw_volume is None:
        return next(iter(requested)) if len(requested) == 1 else None
    if not isinstance(raw_volume, str):
        raise DirectAcquisitionPlanningError(
            "requested_coverage_invalid",
            "The direct acquisition volume coverage is invalid.",
        )
    volume = raw_volume.strip()
    if not volume or len(volume) > 100:
        raise DirectAcquisitionPlanningError(
            "requested_coverage_invalid",
            "The direct acquisition volume coverage is invalid.",
        )
    return volume


def _coverage_numbers_match(requested: str, offered: str) -> bool:
    if requested.casefold() == offered.casefold():
        return True
    requested_number = normalize_issue_number(requested)
    offered_number = normalize_issue_number(offered)
    return requested_number is not None and issues_match(requested_number, offered_number)


def _candidate_confidence(snapshot: Mapping[str, object]) -> float:
    value = snapshot.get("provider_confidence", 0.0)
    return float(value) if isinstance(value, int | float) and 0 <= float(value) <= 1 else 0.0


def _content_quality_rank(artifact: DirectArtifact) -> int:
    format_rank = _FORMAT_RANK.get((artifact.format or "").casefold(), len(_FORMAT_RANK))
    quality_rank = _QUALITY_RANK.get((artifact.quality or "").casefold(), len(_QUALITY_RANK))
    return format_rank * 10 + quality_rank


def _route_eligibility(
    host_kind: DirectArtifactHostKind,
    config: DirectHostConfig | None,
    *,
    internal_generic_https: bool = False,
) -> tuple[bool, str]:
    if internal_generic_https and host_kind is DirectArtifactHostKind.GENERIC_HTTPS:
        return True, "eligible"
    if config is None or not config.enabled:
        return False, "host_disabled"
    state = config.account_state
    credentials_configured = bool(config.encrypted_credentials)
    if state is DirectHostAccountState.AUTHENTICATION_REQUIRED or (
        host_kind in _ACCOUNT_REQUIRED_HOSTS and not credentials_configured
    ):
        return False, "authentication_required"
    if state is DirectHostAccountState.QUOTA_LIMITED:
        return False, "quota_limited"
    if state is DirectHostAccountState.UNAVAILABLE:
        return False, "host_unavailable"
    return True, "eligible"


def _validated_host_kind(mirror: DirectMirror) -> DirectArtifactHostKind:
    location = mirror.final_url or mirror.share_url
    if location is None:
        raise DirectAcquisitionPlanningError(
            "provider_mirror_missing",
            "The provider returned a mirror without a location.",
        )
    try:
        claimed = DirectArtifactHostKind(mirror.host_kind)
        actual = classify_artifact_host(location)
    except (ValueError, ValidationError) as exc:
        raise DirectAcquisitionPlanningError(
            "provider_host_kind_invalid",
            "The provider returned an unsupported artifact host identity.",
        ) from exc
    except Exception as exc:
        raise DirectAcquisitionPlanningError(
            "provider_host_kind_invalid",
            "The provider returned an unsafe artifact location.",
        ) from exc
    if claimed is not actual:
        raise DirectAcquisitionPlanningError(
            "provider_host_kind_mismatch",
            "The provider artifact host identity does not match its location.",
        )
    return actual


def _source_mapping(snapshot: object, route_identity: str) -> dict[str, str]:
    if not isinstance(snapshot, dict):
        raise DirectAcquisitionPlanningError(
            "plan_snapshot_invalid",
            "The direct acquisition plan is invalid.",
        )
    raw_mappings = snapshot.get("route_sources", [])
    if not isinstance(raw_mappings, list):
        raw_mappings = []
    for raw in raw_mappings:
        if not isinstance(raw, dict) or raw.get("route_identity") != route_identity:
            continue
        artifact_id = raw.get("provider_artifact_id")
        mirror_id = raw.get("mirror_id")
        if isinstance(artifact_id, str) and isinstance(mirror_id, str):
            return {"provider_artifact_id": artifact_id, "mirror_id": mirror_id}
    raise DirectAcquisitionPlanningError(
        "plan_source_mapping_missing",
        "The direct acquisition source mapping is unavailable.",
    )


def _validate_stable_identity(label: str, value: str) -> None:
    if (
        not value
        or len(value) > 500
        or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
            for character in value
        )
    ):
        raise DirectAcquisitionPlanningError(
            "provider_identity_invalid",
            f"The {label} identity is not safe for durable planning.",
        )


def _host_resolution_request(
    *,
    artifact_identity: str,
    host_kind: DirectArtifactHostKind,
    artifact: DirectArtifact,
    mirror: DirectMirror,
) -> HostResolutionRequest:
    try:
        provider_headers = sanitize_provider_headers(mirror.source_headers)
    except ArtifactHostResolutionError as exc:
        raise DirectAcquisitionPlanningError(
            exc.code,
            exc.message,
            failure_class=exc.failure_class,
            retryable=exc.retryable,
            intervention=exc.intervention,
        ) from exc
    expected_size = mirror.size_bytes
    if expected_size is None and not artifact.size_is_estimate:
        expected_size = artifact.size_bytes
    return HostResolutionRequest(
        artifact_identity=artifact_identity,
        host_kind=host_kind,
        share_url=mirror.share_url,
        final_url=mirror.final_url,
        provider_headers=provider_headers,
        expected_size=expected_size,
        checksum=mirror.checksum,
        etag=mirror.etag,
        last_modified=mirror.last_modified,
        expires_at=mirror.expires_at,
    )
