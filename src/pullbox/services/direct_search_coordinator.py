"""Bounded direct-provider fan-out through Pullbox's semantic matcher."""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import structlog
from sqlalchemy import select

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.core.issue_title import collection_title_number
from pullbox.core.log_sanitizer import sanitize_log_mapping, sanitize_log_string
from pullbox.core.name_matcher import NameMatcher
from pullbox.core.type_semantics import TypeFamily, issue_type_family
from pullbox.models.direct_acquisition import (
    DirectAcquisitionAttempt,
    DirectAcquisitionState,
    DirectProviderConfig,
    DirectProviderState,
)
from pullbox.providers.base import ReleaseResult
from pullbox.providers.direct.client import DirectProviderClient, DirectProviderClientError
from pullbox.providers.direct.contract import (
    DirectCandidate,
    DirectSearchIntent,
    DirectSearchRequest,
    DirectSearchResponse,
)
from pullbox.services.direct_provider_quota import refresh_expired_provider_quota
from pullbox.services.direct_provider_source_origin import (
    effective_direct_provider_source_domains,
)
from pullbox.services.release_validator import ReleaseValidator, ValidationResult
from pullbox.services.search_scoring import match_confidence_rank

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.services.direct_resolver_service import (
        ProviderResolverOption,
        ResolverAttemptCallback,
        ResolverAttemptProgress,
    )
    from pullbox.services.search_targets import IssueSearchTarget
    from pullbox.services.search_types import ValidatorKwargs

logger = structlog.get_logger(__name__)

_DEFAULT_SEARCH_SECONDS = 30.0
_DEFAULT_RESULT_LIMIT = 20
_MAX_FANOUT = 4
_COLLECTION_EDITION_QUALIFIERS = ("expanded edition",)


@dataclass(frozen=True, slots=True)
class DirectSearchProvider:
    """One decrypted provider operation detached from its database session."""

    provider_config_id: int
    provider_identity: str
    display_name: str
    endpoint: str
    bearer_token: str = field(repr=False)
    allow_private_http: bool = False
    protocol_version: str = "direct-download-provider/v1"
    provider_priority: int = 50
    provider_config: dict[str, object] = field(default_factory=dict, repr=False)
    source_credentials: dict[str, str] = field(default_factory=dict, repr=False)
    resolver_options: tuple[ProviderResolverOption, ...] = field(default=(), repr=False)
    source_domains: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DirectSearchFailure:
    """Redacted failure from one independently isolated provider search."""

    provider_identity: str
    provider_name: str
    code: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class DirectValidatedCandidate:
    """A provider candidate bound to Pullbox's unchanged validation result."""

    provider: DirectSearchProvider
    candidate: DirectCandidate
    release: ReleaseResult
    validation: ValidationResult
    alternate_results: tuple[DirectValidatedCandidate, ...] = ()


@dataclass(frozen=True, slots=True)
class DirectSearchOutcome:
    """Deterministic combined result from all eligible direct providers."""

    matched: tuple[DirectValidatedCandidate, ...]
    rejected: tuple[DirectValidatedCandidate, ...]
    failures: tuple[DirectSearchFailure, ...]
    providers_searched: int
    elapsed_ms: int
    resolver_attempts: tuple[ResolverAttemptProgress, ...] = ()


@dataclass(frozen=True, slots=True)
class DirectSearchDiscovery:
    """Server-issued durable identity for one direct search candidate."""

    attempt_id: int
    result: DirectValidatedCandidate
    visible: bool = True


class DirectSearchClient(Protocol):
    async def search(self, request: DirectSearchRequest) -> DirectSearchResponse: ...

    async def aclose(self) -> None: ...


class DirectSearchClientFactory(Protocol):
    def __call__(
        self,
        *,
        endpoint: str,
        bearer_token: str,
        allow_private_http: bool,
        provider_id: str,
    ) -> DirectSearchClient: ...


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
        request_timeout_seconds=_DEFAULT_SEARCH_SECONDS,
    )


async def load_direct_search_providers(
    session: AsyncSession,
) -> tuple[DirectSearchProvider, ...]:
    """Load and decrypt only providers eligible for one immediate search."""
    from pullbox.services.direct_configuration_service import load_provider_secret_material
    from pullbox.services.direct_resolver_service import build_provider_resolver_profiles

    result = await session.execute(
        select(DirectProviderConfig)
        .where(
            DirectProviderConfig.enabled.is_(True),
            DirectProviderConfig.state.in_(
                (
                    DirectProviderState.HEALTHY,
                    DirectProviderState.DEGRADED,
                    DirectProviderState.RATE_LIMITED,
                )
            ),
        )
        .order_by(
            DirectProviderConfig.priority,
            DirectProviderConfig.provider_id,
            DirectProviderConfig.id,
        )
    )
    providers: list[DirectSearchProvider] = []
    for config in result.scalars().all():
        quota_refreshed = refresh_expired_provider_quota(config)
        if config.state is DirectProviderState.RATE_LIMITED and not quota_refreshed:
            continue
        if quota_refreshed:
            await session.flush()
        material = load_provider_secret_material(config)
        if not material.bearer_token:
            logger.warning(
                "direct_search_provider_skipped",
                provider_id=config.provider_id,
                failure_code="provider_authentication_failed",
            )
            continue
        metadata = config.configuration_metadata or {}
        raw_public = metadata.get("public_values", {})
        public_config = dict(raw_public) if isinstance(raw_public, dict) else {}
        providers.append(
            DirectSearchProvider(
                provider_config_id=config.id,
                provider_identity=config.provider_id,
                display_name=config.display_name,
                endpoint=config.endpoint,
                bearer_token=material.bearer_token,
                allow_private_http=metadata.get("allow_private_http") is True,
                protocol_version=config.negotiated_protocol or "direct-download-provider/v1",
                provider_priority=config.priority,
                provider_config=public_config,
                source_credentials=material.configuration,
                resolver_options=await build_provider_resolver_profiles(session, config),
                source_domains=effective_direct_provider_source_domains(config),
            )
        )
    return tuple(providers)


async def persist_direct_search_discoveries(
    session: AsyncSession,
    target: IssueSearchTarget,
    outcome: DirectSearchOutcome,
    *,
    search_log_id: int | None = None,
) -> tuple[DirectSearchDiscovery, ...]:
    """Persist redacted candidate evidence and return server-issued IDs."""
    pending: list[tuple[DirectAcquisitionAttempt, DirectValidatedCandidate, bool]] = []
    fingerprint_groups: list[tuple[DirectAcquisitionAttempt, list[DirectAcquisitionAttempt]]] = []
    issue_number = f"{target.issue_number:g}"
    volume = _target_volume(target)
    for primary in (*outcome.matched, *outcome.rejected):
        group_attempts: list[DirectAcquisitionAttempt] = []
        for result, visible in (
            (primary, True),
            *((alternate, False) for alternate in primary.alternate_results),
        ):
            candidate_snapshot = _candidate_snapshot(result)
            candidate_snapshot["visible"] = visible
            attempt = DirectAcquisitionAttempt(
                request_key=f"direct-search:{uuid4().hex}",
                issue_id=target.issue_id,
                search_log_id=search_log_id,
                provider_config_id=result.provider.provider_config_id,
                provider_identity=result.provider.provider_identity,
                provider_candidate_id=result.candidate.provider_candidate_id,
                state=DirectAcquisitionState.DISCOVERED,
                requested_coverage={
                    "issue_numbers": [issue_number],
                    "issue_type": target.issue_type.value,
                    "volume": volume,
                },
                candidate_snapshot=candidate_snapshot,
                plan_snapshot={},
                progress_snapshot={
                    "schema_version": 1,
                    "stage": "discovered",
                },
            )
            session.add(attempt)
            pending.append((attempt, result, visible))
            group_attempts.append(attempt)
        fingerprint_groups.append((group_attempts[0], group_attempts[1:]))
    if pending:
        await session.flush()
    for primary_attempt, alternate_attempts in fingerprint_groups:
        if not alternate_attempts:
            continue
        primary_attempt.candidate_snapshot = {
            **primary_attempt.candidate_snapshot,
            "alternate_attempt_ids": [attempt.id for attempt in alternate_attempts],
        }
        for index, alternate_attempt in enumerate(alternate_attempts):
            alternate_attempt.candidate_snapshot = {
                **alternate_attempt.candidate_snapshot,
                "primary_attempt_id": primary_attempt.id,
                "alternate_attempt_ids": [
                    attempt.id for attempt in alternate_attempts[index + 1 :]
                ],
            }
    return tuple(
        DirectSearchDiscovery(attempt_id=attempt.id, result=result, visible=visible)
        for attempt, result, visible in pending
    )


async def search_direct_issue_target(
    target: IssueSearchTarget,
    providers: Sequence[DirectSearchProvider],
    *,
    validator_kwargs: ValidatorKwargs | None = None,
    client_factory: DirectSearchClientFactory = _default_client_factory,
    result_limit: int = _DEFAULT_RESULT_LIMIT,
    search_seconds: float = _DEFAULT_SEARCH_SECONDS,
    max_fanout: int = _MAX_FANOUT,
    now: Callable[[], datetime] | None = None,
    on_resolver_attempt: ResolverAttemptCallback | None = None,
) -> DirectSearchOutcome:
    """Search eligible providers concurrently and preserve matcher semantics."""
    if not 1 <= result_limit <= 100:
        raise ValueError("Direct provider result limit must be between 1 and 100.")
    if search_seconds <= 0 or search_seconds > 300:
        raise ValueError("Direct provider search timeout must be between 0 and 300 seconds.")
    if not 1 <= max_fanout <= _MAX_FANOUT:
        raise ValueError("Direct provider fan-out must be between 1 and 4.")

    started_at = time.monotonic()
    active_providers = tuple(
        sorted(
            providers,
            key=lambda item: (
                item.provider_priority,
                item.provider_identity,
                item.provider_config_id,
            ),
        )
    )
    if not active_providers:
        return DirectSearchOutcome((), (), (), 0, 0)

    clock = now or (lambda: datetime.now(UTC))
    validator = ReleaseValidator(**(validator_kwargs or {}))
    semaphore = asyncio.Semaphore(min(max_fanout, len(active_providers)))
    resolver_attempts: list[ResolverAttemptProgress] = []

    async def record_resolver_attempt(event: ResolverAttemptProgress) -> None:
        resolver_attempts.append(event)
        if on_resolver_attempt is not None:
            await on_resolver_attempt(event)

    async def _search(
        provider: DirectSearchProvider,
    ) -> tuple[
        list[DirectValidatedCandidate],
        list[DirectValidatedCandidate],
        DirectSearchFailure | None,
    ]:
        async with semaphore:
            return await _search_provider(
                target,
                provider,
                validator=validator,
                client_factory=client_factory,
                result_limit=result_limit,
                deadline=clock() + timedelta(seconds=search_seconds),
                on_resolver_attempt=record_resolver_attempt,
            )

    provider_results = await asyncio.gather(*(_search(provider) for provider in active_providers))
    matched: list[DirectValidatedCandidate] = []
    rejected: list[DirectValidatedCandidate] = []
    failures: list[DirectSearchFailure] = []
    for provider_matched, provider_rejected, failure in provider_results:
        matched.extend(provider_matched)
        rejected.extend(provider_rejected)
        if failure is not None:
            failures.append(failure)

    matched.sort(key=_matched_order_key)
    rejected.sort(key=_rejected_order_key)
    matched = _group_fingerprint_alternates(matched)
    rejected = _group_fingerprint_alternates(rejected)
    failures.sort(key=lambda item: (item.provider_identity, item.code))
    return DirectSearchOutcome(
        matched=tuple(matched),
        rejected=tuple(rejected),
        failures=tuple(failures),
        providers_searched=len(active_providers),
        elapsed_ms=int((time.monotonic() - started_at) * 1000),
        resolver_attempts=tuple(resolver_attempts),
    )


async def _search_provider(
    target: IssueSearchTarget,
    provider: DirectSearchProvider,
    *,
    validator: ReleaseValidator,
    client_factory: DirectSearchClientFactory,
    result_limit: int,
    deadline: datetime,
    on_resolver_attempt: ResolverAttemptCallback | None,
) -> tuple[
    list[DirectValidatedCandidate],
    list[DirectValidatedCandidate],
    DirectSearchFailure | None,
]:
    client = client_factory(
        endpoint=provider.endpoint,
        bearer_token=provider.bearer_token,
        allow_private_http=provider.allow_private_http,
        provider_id=provider.provider_identity,
    )
    try:
        response = await _search_with_resolver_fallback(
            client,
            provider,
            target=target,
            result_limit=result_limit,
            deadline=deadline,
            on_resolver_attempt=on_resolver_attempt,
        )
    except asyncio.CancelledError:
        raise
    except DirectProviderClientError as exc:
        logger.warning(
            "direct_search_provider_failed",
            provider_id=provider.provider_identity,
            failure_code=exc.code,
            retryable=exc.retryable,
        )
        return (
            [],
            [],
            DirectSearchFailure(
                provider_identity=provider.provider_identity,
                provider_name=provider.display_name,
                code=exc.code,
                retryable=exc.retryable,
            ),
        )
    except Exception:
        logger.exception(
            "direct_search_provider_failed",
            provider_id=provider.provider_identity,
            failure_code="provider_search_failed",
        )
        return (
            [],
            [],
            DirectSearchFailure(
                provider_identity=provider.provider_identity,
                provider_name=provider.display_name,
                code="provider_search_failed",
                retryable=True,
            ),
        )
    finally:
        await client.aclose()

    matched: list[DirectValidatedCandidate] = []
    rejected: list[DirectValidatedCandidate] = []
    seen_candidate_ids: set[str] = set()
    for candidate in response.candidates:
        if candidate.provider_candidate_id in seen_candidate_ids:
            logger.warning(
                "direct_search_duplicate_candidate_ignored",
                provider_id=provider.provider_identity,
            )
            continue
        seen_candidate_ids.add(candidate.provider_candidate_id)
        release = _candidate_release(provider, candidate)
        validation = _validate_direct_candidate(
            validator,
            release=release,
            candidate=candidate,
            target=target,
        )
        result = DirectValidatedCandidate(
            provider=provider,
            candidate=candidate,
            release=release,
            validation=validation,
        )
        (matched if validation.is_match else rejected).append(result)
    return matched, rejected, None


def _validate_direct_candidate(
    validator: ReleaseValidator,
    *,
    release: ReleaseResult,
    candidate: DirectCandidate,
    target: IssueSearchTarget,
) -> ValidationResult:
    """Validate a direct candidate, allowing only explicitly-covered issue packs.

    Generic torrent and Usenet releases remain conservative: their contents cannot
    be safely assumed to be separable. Direct candidates carry provider-parsed
    coverage and pass a dedicated nested-archive validation path before import.
    """

    def validate(
        candidate_release: ReleaseResult,
    ) -> tuple[list[ValidationResult], list[ValidationResult]]:
        return validator.validate_all_results(
            [candidate_release],
            wanted_series=target.series_title,
            wanted_issue=target.issue_number,
            wanted_year=target.search_year,
            wanted_issue_type=target.issue_type,
            alternate_names=_validation_alternate_names(target, candidate),
            wanted_issue_title=target.issue_title,
            wanted_series_issue_count=target.series_issue_count,
        )

    accepted, declined = validate(release)
    validation = accepted[0] if accepted else declined[0]
    if validation.is_match or not _is_explicit_direct_pack_for_target(candidate, target):
        return validation
    rejection_reason = validation.rejection_reason or ""
    if not (
        rejection_reason.startswith("Multi-issue pack")
        or rejection_reason.startswith("Issue mismatch")
    ):
        return validation

    # Validate title, type, and year with the requested member as a synthetic
    # single-issue title, then retain the original range evidence for display.
    pack_member_release = replace(
        release,
        title=_direct_pack_member_title(candidate, target),
    )
    accepted, _declined = validate(pack_member_release)
    if not accepted:
        return validation
    return replace(accepted[0], parsed=validation.parsed, release=release)


def _is_explicit_direct_pack_for_target(
    candidate: DirectCandidate,
    target: IssueSearchTarget,
) -> bool:
    target_issue = f"{target.issue_number:g}"
    return (
        len(candidate.parsed.issue_numbers) > 1 and target_issue in candidate.parsed.issue_numbers
    )


def _direct_pack_member_title(
    candidate: DirectCandidate,
    target: IssueSearchTarget,
) -> str:
    year = candidate.parsed.year or target.search_year
    suffix = f" ({year})" if year is not None else ""
    return f"{candidate.parsed.series_title} #{target.issue_number:g}{suffix}"


def _validation_alternate_names(
    target: IssueSearchTarget,
    candidate: DirectCandidate,
) -> list[str]:
    """Recognize bounded edition qualifiers without weakening title matching."""
    alternate_names = list(target.alternate_names or [])
    if issue_type_family(target.issue_type) is not TypeFamily.COLLECTION:
        return alternate_names

    candidate_title = candidate.parsed.series_title.strip()
    normalized_candidate = NameMatcher.normalize(candidate_title)
    for base_title in (target.series_title, *alternate_names):
        normalized_base = NameMatcher.normalize(base_title)
        if normalized_candidate in {
            f"{normalized_base} {qualifier}" for qualifier in _COLLECTION_EDITION_QUALIFIERS
        }:
            if candidate_title not in alternate_names:
                alternate_names.append(candidate_title)
            break
    return alternate_names


async def _search_with_resolver_fallback(
    client: DirectSearchClient,
    provider: DirectSearchProvider,
    *,
    target: IssueSearchTarget,
    result_limit: int,
    deadline: datetime,
    on_resolver_attempt: ResolverAttemptCallback | None,
) -> DirectSearchResponse:
    options = (None, *provider.resolver_options)
    last_error: DirectProviderClientError | None = None
    for option_index, option in enumerate(options):
        if option is not None and on_resolver_attempt is not None:
            from pullbox.services.direct_resolver_service import ResolverAttemptProgress

            await on_resolver_attempt(
                ResolverAttemptProgress(
                    resolver_id=option.resolver_id,
                    resolver_name=option.resolver_name,
                    resolver_kind=option.resolver_kind,
                    attempt=option_index,
                    total=len(provider.resolver_options),
                    scope=f"provider:{provider.provider_identity}:search",
                )
            )
        try:
            return await client.search(
                DirectSearchRequest(
                    protocol_version=provider.protocol_version,
                    request_id=uuid4(),
                    deadline=deadline,
                    provider_config=provider.provider_config,
                    source_credentials=provider.source_credentials,
                    resolver_profile=option.profile if option is not None else None,
                    intent=_build_intent(target),
                    limit=result_limit,
                )
            )
        except DirectProviderClientError as exc:
            last_error = exc
            if not _resolver_retry_allowed(exc) or option_index == len(options) - 1:
                raise
    if last_error is not None:
        raise last_error
    raise RuntimeError("Direct provider search had no executable request path.")


def _resolver_retry_allowed(exc: DirectProviderClientError) -> bool:
    return exc.code == "browser_challenge_required" or exc.code.startswith("resolver_")


def _build_intent(target: IssueSearchTarget) -> DirectSearchIntent:
    issue_number = f"{target.issue_number:g}"
    return DirectSearchIntent(
        series_title=target.series_title,
        normalized_title=NameMatcher.normalize(target.series_title),
        alternate_titles=list(target.alternate_names or []),
        issue_number=issue_number,
        issue_type=target.issue_type.value,
        volume=_target_volume(target),
        issue_title=target.issue_title,
        series_year=target.series_year,
        release_year=target.release_year,
        year=target.search_year,
        preferred_formats=["cbz", "cbr", "cb7", "pdf"],
        quality_preferences=["digital", "retail"],
    )


def _target_volume(target: IssueSearchTarget) -> str | None:
    """Map collection issue numbering onto provider volume coverage."""
    if issue_type_family(target.issue_type) is not TypeFamily.COLLECTION:
        return None
    return collection_title_number(target.issue_title) or f"{target.issue_number:g}"


def _candidate_release(
    provider: DirectSearchProvider,
    candidate: DirectCandidate,
) -> ReleaseResult:
    identity = hashlib.sha256(
        f"{provider.provider_config_id}:{candidate.provider_candidate_id}".encode()
    ).hexdigest()[:24]
    return ReleaseResult(
        title=candidate.raw_title,
        indexer_name=provider.display_name,
        download_url=f"direct://candidate/{identity}",
        size_bytes=None,
        age_days=None,
        seeders=None,
        leechers=None,
        grabs=None,
        protocol=AcquisitionProtocol.DIRECT,
        category="Books/Comics",
        published_at=None,
        info_url=_safe_source_reference(candidate.source_reference, provider.source_domains),
        ranking_priority=provider.provider_priority,
    )


def _safe_source_reference(raw_url: str, domains: tuple[str, ...]) -> str | None:
    try:
        parsed = urlsplit(raw_url)
        _ = parsed.port
    except ValueError:
        return None
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    safe_domains = tuple(domain.casefold().rstrip(".") for domain in domains)
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or not any(hostname == domain or hostname.endswith(f".{domain}") for domain in safe_domains)
    ):
        return None
    return urlunsplit(("https", parsed.netloc, parsed.path, "", ""))


def _candidate_snapshot(result: DirectValidatedCandidate) -> dict[str, object]:
    """Build durable evidence without source URLs or provider secrets."""
    candidate = result.candidate
    validation = result.validation
    provenance = sanitize_log_mapping(candidate.provenance)
    return {
        "schema_version": 1,
        "display_title": sanitize_log_string(candidate.display_title),
        "raw_title": sanitize_log_string(candidate.raw_title),
        "parsed": candidate.parsed.model_dump(mode="json"),
        "provider_confidence": candidate.provider_confidence,
        "provenance": provenance,
        "can_resolve": candidate.can_resolve,
        "expires_at": candidate.expires_at.isoformat() if candidate.expires_at else None,
        "semantic_decision": {
            "is_match": validation.is_match,
            "confidence": validation.confidence.value,
            "series_similarity": validation.series_similarity,
            "match_type": validation.match_type,
            "rejection_reason": sanitize_log_string(validation.rejection_reason or ""),
        },
    }


def _matched_order_key(item: DirectValidatedCandidate) -> tuple[object, ...]:
    return (
        match_confidence_rank(item.validation.confidence),
        -item.validation.series_similarity,
        -item.candidate.provider_confidence,
        item.provider.provider_priority,
        item.provider.provider_identity,
        item.candidate.provider_candidate_id,
    )


def _rejected_order_key(item: DirectValidatedCandidate) -> tuple[object, ...]:
    return (
        -item.validation.series_similarity,
        item.provider.provider_priority,
        -item.candidate.provider_confidence,
        item.provider.provider_identity,
        item.candidate.provider_candidate_id,
    )


def _group_fingerprint_alternates(
    results: list[DirectValidatedCandidate],
) -> list[DirectValidatedCandidate]:
    """Collapse identical content only after semantic accept/reject decisions."""
    grouped: list[DirectValidatedCandidate] = []
    primary_indexes: dict[str, int] = {}
    for result in results:
        fingerprint = result.candidate.content_fingerprint
        if fingerprint is None:
            grouped.append(result)
            continue
        primary_index = primary_indexes.get(fingerprint)
        if primary_index is None:
            primary_indexes[fingerprint] = len(grouped)
            grouped.append(result)
            continue
        primary = grouped[primary_index]
        grouped[primary_index] = replace(
            primary,
            alternate_results=(*primary.alternate_results, result),
        )
    return grouped
