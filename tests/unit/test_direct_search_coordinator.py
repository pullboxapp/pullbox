"""Direct-provider fan-out through Pullbox's unchanged semantic matcher."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import MagicMock, patch
from uuid import UUID

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.core.encryption import _get_fernet
from pullbox.models.direct_acquisition import (
    DirectAcquisitionAttempt,
    DirectProviderConfig,
    DirectProviderState,
    DirectProviderTrustLevel,
    DirectResolverKind,
)
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import MatchConfidence
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.providers.direct.client import DirectProviderClientError
from pullbox.providers.direct.contract import (
    DIRECT_PROVIDER_PROTOCOL_V1,
    DirectCandidate,
    DirectParsedCandidate,
    DirectResolverProfile,
    DirectSearchRequest,
    DirectSearchResponse,
)
from pullbox.services.direct_configuration_service import (
    update_provider_configuration_secrets,
    write_provider_bearer_token,
)
from pullbox.services.direct_resolver_service import (
    ProviderResolverOption,
    ResolverAttemptProgress,
)
from pullbox.services.direct_search_coordinator import (
    DirectSearchProvider,
    load_direct_search_providers,
    persist_direct_search_discoveries,
    search_direct_issue_target,
)
from pullbox.services.search_targets import IssueSearchTarget

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _target() -> IssueSearchTarget:
    return IssueSearchTarget(
        issue_id=17,
        series_id=7,
        series_title="Absolute Superman",
        issue_number=9,
        issue_type=IssueType.ISSUE,
        series_year=2025,
        alternate_names=["Absolute Superman (2025)"],
    )


def _provider(identity: str, priority: int) -> DirectSearchProvider:
    return DirectSearchProvider(
        provider_config_id=priority,
        provider_identity=identity,
        display_name=identity.rsplit(".", 1)[-1].title(),
        endpoint=f"http://{identity}:8780",
        bearer_token=f"{identity}-bearer-token-with-enough-length",
        allow_private_http=True,
        protocol_version=DIRECT_PROVIDER_PROTOCOL_V1,
        provider_priority=priority,
        provider_config={},
        source_credentials={},
        resolver_options=(),
        source_domains=(f"{identity}.example",),
    )


def _candidate(
    provider: DirectSearchProvider,
    title: str,
    *,
    content_fingerprint: str | None = None,
    provider_confidence: float = 0.95,
) -> DirectCandidate:
    return DirectCandidate(
        provider_candidate_id=f"candidate:{provider.provider_identity}",
        source_reference=f"https://{provider.provider_identity}.example/release",
        display_title=title,
        raw_title=title,
        parsed=DirectParsedCandidate(
            series_title="Absolute Superman",
            issue_numbers=["9"],
            year=2025,
            format="cbz",
        ),
        provider_confidence=provider_confidence,
        content_fingerprint=content_fingerprint,
        provenance={"fixture": True},
    )


def _contiguous_pack_candidate(provider: DirectSearchProvider) -> DirectCandidate:
    return DirectCandidate(
        provider_candidate_id=f"pack:{provider.provider_identity}",
        source_reference=f"https://{provider.provider_identity}.example/pack",
        display_title="Absolute Superman #5 \N{EN DASH} 10 (2025)",
        raw_title="Absolute Superman #5 \N{EN DASH} 10 (2025)",
        parsed=DirectParsedCandidate(
            series_title="Absolute Superman",
            issue_numbers=["5", "6", "7", "8", "9", "10"],
            year=2025,
            format="cbz",
        ),
        provider_confidence=0.95,
        provenance={"fixture": "contiguous-pack"},
    )


class _Client:
    delays: ClassVar[dict[str, float]] = {}
    responses: ClassVar[dict[str, list[DirectCandidate]]] = {}
    failures: ClassVar[set[str]] = set()
    active: ClassVar[int] = 0
    max_active: ClassVar[int] = 0
    requests: ClassVar[list[tuple[str, DirectSearchRequest]]] = []
    challenge_required: ClassVar[set[str]] = set()
    resolver_failures: ClassVar[dict[str, str]] = {}

    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id

    async def search(self, request: DirectSearchRequest) -> DirectSearchResponse:
        type(self).active += 1
        type(self).max_active = max(type(self).max_active, type(self).active)
        type(self).requests.append((self.provider_id, request))
        try:
            await asyncio.sleep(type(self).delays.get(self.provider_id, 0))
            profile = request.resolver_profile
            if self.provider_id in type(self).challenge_required and profile is None:
                raise DirectProviderClientError(
                    "browser_challenge_required",
                    "Browser challenge required.",
                    retryable=True,
                )
            if profile is not None and profile.endpoint in type(self).resolver_failures:
                raise DirectProviderClientError(
                    type(self).resolver_failures[profile.endpoint],
                    "Resolver attempt failed.",
                    retryable=True,
                )
            if self.provider_id in type(self).failures:
                raise RuntimeError("secret upstream failure details")
            return DirectSearchResponse(
                protocol_version=DIRECT_PROVIDER_PROTOCOL_V1,
                request_id=request.request_id,
                candidates=type(self).responses[self.provider_id],
            )
        finally:
            type(self).active -= 1

    async def aclose(self) -> None:
        return None


def _factory(**kwargs: object) -> _Client:
    return _Client(str(kwargs["provider_id"]))


def _reset() -> None:
    _Client.delays = {}
    _Client.responses = {}
    _Client.failures = set()
    _Client.active = 0
    _Client.max_active = 0
    _Client.requests = []
    _Client.challenge_required = set()
    _Client.resolver_failures = {}


def _resolver_option(
    resolver_id: int,
    name: str,
    kind: DirectResolverKind,
    endpoint: str,
) -> ProviderResolverOption:
    return ProviderResolverOption(
        resolver_id=resolver_id,
        resolver_name=name,
        resolver_kind=kind,
        profile=DirectResolverProfile(
            endpoint=endpoint,
            timeout_seconds=60,
            max_concurrency=1,
            declared_domains=["getcomics.org"],
        ),
    )


async def test_fanout_is_concurrent_and_completion_order_does_not_change_results() -> None:
    _reset()
    first = _provider("pullbox.getcomics", 20)
    second = _provider("pullbox.annas_archive", 10)
    _Client.delays = {first.provider_identity: 0.03, second.provider_identity: 0.001}
    _Client.responses = {
        first.provider_identity: [_candidate(first, "Absolute Superman 009 (2025) (Digital)")],
        second.provider_identity: [_candidate(second, "Absolute Superman 009 (2025) (Digital)")],
    }

    outcome = await search_direct_issue_target(
        _target(),
        [first, second],
        client_factory=_factory,
        now=lambda: datetime(2026, 7, 28, tzinfo=UTC),
    )

    assert _Client.max_active == 2
    assert [item.provider.provider_identity for item in outcome.matched] == [
        "pullbox.annas_archive",
        "pullbox.getcomics",
    ]
    assert all(item.validation.confidence is MatchConfidence.HIGH for item in outcome.matched)
    assert all(item.release.protocol is AcquisitionProtocol.DIRECT for item in outcome.matched)
    assert outcome.failures == ()


async def test_cross_provider_fingerprint_collapses_to_one_result_with_alternate() -> None:
    _reset()
    preferred = _provider("pullbox.libgen", 10)
    alternate = _provider("pullbox.annas_archive", 20)
    fingerprint = "md5:0123456789abcdef0123456789abcdef"
    _Client.responses = {
        preferred.provider_identity: [
            _candidate(
                preferred,
                "Absolute Superman 009 (2025) (Digital)",
                content_fingerprint=fingerprint,
            )
        ],
        alternate.provider_identity: [
            _candidate(
                alternate,
                "Absolute Superman 009 (2025) (Digital)",
                content_fingerprint=fingerprint,
            )
        ],
    }

    outcome = await search_direct_issue_target(
        _target(),
        [alternate, preferred],
        client_factory=_factory,
    )

    assert len(outcome.matched) == 1
    assert outcome.matched[0].provider is preferred
    assert [item.provider for item in outcome.matched[0].alternate_results] == [alternate]


async def test_fingerprint_primary_prefers_candidate_confidence_before_provider_priority() -> None:
    _reset()
    configured_first = _provider("pullbox.getcomics", 10)
    stronger_candidate = _provider("pullbox.libgen", 20)
    fingerprint = f"md5:{'4' * 32}"
    _Client.responses = {
        configured_first.provider_identity: [
            _candidate(
                configured_first,
                "Absolute Superman 009 (2025) (Digital)",
                content_fingerprint=fingerprint,
                provider_confidence=0.80,
            )
        ],
        stronger_candidate.provider_identity: [
            _candidate(
                stronger_candidate,
                "Absolute Superman 009 (2025) (Digital)",
                content_fingerprint=fingerprint,
                provider_confidence=0.99,
            )
        ],
    }

    outcome = await search_direct_issue_target(
        _target(),
        [configured_first, stronger_candidate],
        client_factory=_factory,
    )

    assert len(outcome.matched) == 1
    assert outcome.matched[0].provider is stronger_candidate
    assert [item.provider for item in outcome.matched[0].alternate_results] == [configured_first]


async def test_different_fingerprints_remain_distinct_results() -> None:
    _reset()
    first = _provider("pullbox.libgen", 10)
    second = _provider("pullbox.annas_archive", 20)
    _Client.responses = {
        first.provider_identity: [
            _candidate(
                first,
                "Absolute Superman 009 (2025)",
                content_fingerprint=f"md5:{'1' * 32}",
            )
        ],
        second.provider_identity: [
            _candidate(
                second,
                "Absolute Superman 009 (2025)",
                content_fingerprint=f"md5:{'2' * 32}",
            )
        ],
    }

    outcome = await search_direct_issue_target(
        _target(),
        [first, second],
        client_factory=_factory,
    )

    assert len(outcome.matched) == 2
    assert all(item.alternate_results == () for item in outcome.matched)


async def test_fingerprint_never_promotes_a_semantically_rejected_candidate() -> None:
    _reset()
    accepted_provider = _provider("pullbox.libgen", 10)
    rejected_provider = _provider("pullbox.annas_archive", 20)
    fingerprint = f"md5:{'3' * 32}"
    _Client.responses = {
        accepted_provider.provider_identity: [
            _candidate(
                accepted_provider,
                "Absolute Superman 009 (2025)",
                content_fingerprint=fingerprint,
            )
        ],
        rejected_provider.provider_identity: [
            _candidate(
                rejected_provider,
                "Completely Different Series 009 (2025)",
                content_fingerprint=fingerprint,
            )
        ],
    }

    outcome = await search_direct_issue_target(
        _target(),
        [accepted_provider, rejected_provider],
        client_factory=_factory,
    )

    assert len(outcome.matched) == 1
    assert outcome.matched[0].alternate_results == ()
    assert len(outcome.rejected) == 1
    assert outcome.rejected[0].alternate_results == ()


async def test_one_provider_failure_is_isolated_and_redacted() -> None:
    _reset()
    healthy = _provider("pullbox.getcomics", 10)
    broken = _provider("community.broken", 20)
    _Client.responses = {
        healthy.provider_identity: [_candidate(healthy, "Absolute Superman 009 (2025)")],
        broken.provider_identity: [],
    }
    _Client.failures = {broken.provider_identity}

    outcome = await search_direct_issue_target(
        _target(),
        [broken, healthy],
        client_factory=_factory,
    )

    assert len(outcome.matched) == 1
    assert outcome.matched[0].provider == healthy
    assert outcome.failures[0].provider_identity == broken.provider_identity
    assert outcome.failures[0].code == "provider_search_failed"
    assert "secret upstream" not in repr(outcome)


async def test_duplicate_provider_candidate_identity_is_returned_once() -> None:
    _reset()
    provider = _provider("pullbox.getcomics", 10)
    candidate = _candidate(provider, "Absolute Superman 009 (2025)")
    _Client.responses = {provider.provider_identity: [candidate, candidate]}

    outcome = await search_direct_issue_target(
        _target(),
        [provider],
        client_factory=_factory,
    )

    assert len(outcome.matched) == 1
    assert outcome.matched[0].candidate.provider_candidate_id == candidate.provider_candidate_id


async def test_direct_search_accepts_contiguous_pack_for_interior_issue() -> None:
    _reset()
    provider = _provider("pullbox.getcomics", 10)
    _Client.responses = {provider.provider_identity: [_contiguous_pack_candidate(provider)]}

    outcome = await search_direct_issue_target(
        _target(),
        [provider],
        client_factory=_factory,
    )

    assert len(outcome.matched) == 1
    assert outcome.rejected == ()
    assert outcome.matched[0].candidate.parsed.issue_numbers == ["5", "6", "7", "8", "9", "10"]


async def test_provider_search_tries_ordinary_http_then_ranked_resolvers() -> None:
    _reset()
    provider = replace(
        _provider("pullbox.getcomics", 10),
        resolver_options=(
            _resolver_option(
                1,
                "FlareSolverr",
                DirectResolverKind.FLARESOLVERR,
                "http://flaresolverr:8191",
            ),
            _resolver_option(
                2,
                "Byparr",
                DirectResolverKind.BYPARR,
                "http://byparr:8191",
            ),
        ),
    )
    _Client.challenge_required = {provider.provider_identity}
    _Client.resolver_failures = {"http://flaresolverr:8191": "resolver_timed_out"}
    _Client.responses = {
        provider.provider_identity: [_candidate(provider, "Absolute Superman 009 (2025)")]
    }
    progress: list[ResolverAttemptProgress] = []

    async def record_attempt(value: ResolverAttemptProgress) -> None:
        progress.append(value)

    outcome = await search_direct_issue_target(
        _target(),
        [provider],
        client_factory=_factory,
        on_resolver_attempt=record_attempt,
    )

    assert len(outcome.matched) == 1
    assert [
        request.resolver_profile.endpoint if request.resolver_profile else None
        for _, request in _Client.requests
    ] == [None, "http://flaresolverr:8191", "http://byparr:8191"]
    assert [(item.resolver_name, item.attempt, item.total) for item in progress] == [
        ("FlareSolverr", 1, 2),
        ("Byparr", 2, 2),
    ]
    assert outcome.resolver_attempts == tuple(progress)


async def test_rejected_candidate_uses_existing_validator_without_provider_override() -> None:
    _reset()
    provider = _provider("pullbox.getcomics", 10)
    candidate = _candidate(provider, "Completely Different Series 009 (2025)")
    candidate = candidate.model_copy(update={"provider_confidence": 1.0})
    _Client.responses = {provider.provider_identity: [candidate]}

    outcome = await search_direct_issue_target(
        _target(),
        [provider],
        client_factory=_factory,
    )

    assert outcome.matched == ()
    assert len(outcome.rejected) == 1
    assert outcome.rejected[0].validation.is_match is False
    assert outcome.rejected[0].validation.rejection_reason


async def test_request_scopes_each_provider_secret_and_normalized_intent() -> None:
    _reset()
    provider = replace(
        _provider("pullbox.annas_archive", 10),
        provider_config={"domain": "https://annas-archive.gd"},
        source_credentials={"member_secret_key": "member-secret"},
    )
    _Client.responses = {provider.provider_identity: []}

    await search_direct_issue_target(_target(), [provider], client_factory=_factory)

    request = _Client.requests[0][1]
    assert request.intent.series_title == "Absolute Superman"
    assert request.intent.normalized_title == "absolute superman"
    assert request.intent.issue_number == "9"
    assert request.provider_config == provider.provider_config
    assert request.source_credentials == provider.source_credentials
    assert "member-secret" not in repr(request)
    assert isinstance(request.request_id, UUID)


async def test_collection_target_declares_volume_coverage() -> None:
    _reset()
    provider = _provider("pullbox.getcomics", 10)
    _Client.responses = {provider.provider_identity: []}
    target = replace(
        _target(),
        issue_number=1,
        issue_type=IssueType.DELUXE,
    )

    await search_direct_issue_target(target, [provider], client_factory=_factory)

    request = _Client.requests[0][1]
    assert request.intent.issue_number == "1"
    assert request.intent.volume == "1"


async def test_collection_target_uses_explicit_issue_title_volume_ordinal() -> None:
    _reset()
    provider = _provider("pullbox.getcomics", 10)
    _Client.responses = {provider.provider_identity: []}
    target = replace(
        _target(),
        series_title="Clean Room: Exile",
        issue_number=1,
        issue_type=IssueType.VOLUME,
        issue_title="Volume 2",
        series_issue_count=1,
    )

    await search_direct_issue_target(target, [provider], client_factory=_factory)

    request = _Client.requests[0][1]
    assert request.intent.issue_number == "1"
    assert request.intent.volume == "2"


async def test_single_issue_collection_direct_search_uses_subtitle_identity() -> None:
    _reset()
    provider = _provider("pullbox.annas_archive", 10)
    correct = _candidate(
        provider,
        "Clean Room v02 - Exile (2016) (digital-Empire).cbr",
    ).model_copy(update={"provider_candidate_id": "clean-room-exile"})
    sibling = _candidate(
        provider,
        "Clean Room v01 - Immaculate Conception (2016) (Digital) (Zone-Empire).cbr",
    ).model_copy(update={"provider_candidate_id": "clean-room-immaculate-conception"})
    _Client.responses = {provider.provider_identity: [correct, sibling]}
    target = replace(
        _target(),
        series_title="Clean Room: Exile",
        issue_number=1,
        issue_type=IssueType.VOLUME,
        issue_title="Volume 2",
        series_year=2016,
        release_year=2016,
        series_issue_count=1,
        alternate_names=[],
    )

    outcome = await search_direct_issue_target(target, [provider], client_factory=_factory)

    assert [item.candidate.provider_candidate_id for item in outcome.matched] == [
        "clean-room-exile"
    ]
    assert [item.candidate.provider_candidate_id for item in outcome.rejected] == [
        "clean-room-immaculate-conception"
    ]


async def test_single_issue_collection_accepts_matching_book_ordinal() -> None:
    _reset()
    provider = _provider("pullbox.annas_archive", 10)
    candidate = _candidate(
        provider,
        "Marvel action. Spider-Man. Bad luck. Book 3",
    ).model_copy(
        update={
            "provider_candidate_id": "marvel-action-spider-man-bad-luck-book-3",
            "parsed": DirectParsedCandidate(
                series_title="Marvel action. Spider-Man. Bad luck. Book",
                issue_numbers=["3"],
                year=None,
                format=None,
            ),
        }
    )
    _Client.responses = {provider.provider_identity: [candidate]}
    target = replace(
        _target(),
        series_title="Marvel Action: Spider-Man: Bad Luck",
        issue_number=1,
        issue_type=IssueType.TPB,
        issue_title="Book 3",
        series_year=2020,
        release_year=2020,
        series_issue_count=1,
        alternate_names=[],
    )

    outcome = await search_direct_issue_target(target, [provider], client_factory=_factory)

    assert [item.candidate.provider_candidate_id for item in outcome.matched] == [
        "marvel-action-spider-man-bad-luck-book-3"
    ]
    assert outcome.rejected == ()


async def test_collection_intent_carries_title_and_distinct_years() -> None:
    _reset()
    provider = _provider("pullbox.getcomics", 10)
    _Client.responses = {provider.provider_identity: []}
    target = replace(
        _target(),
        series_title="Immortal Thor",
        issue_number=3,
        issue_type=IssueType.VOLUME,
        issue_title="Vol. 3: The End of All Songs",
        series_year=2024,
        release_year=2025,
    )

    await search_direct_issue_target(target, [provider], client_factory=_factory)

    intent = _Client.requests[0][1].intent
    assert intent.issue_title == "Vol. 3: The End of All Songs"
    assert intent.series_year == 2024
    assert intent.release_year == 2025
    assert intent.year == 2025


async def test_collection_target_accepts_trailing_expanded_edition_qualifier() -> None:
    _reset()
    provider = _provider("pullbox.getcomics", 10)
    target = replace(
        _target(),
        series_title="Henchgirl",
        issue_number=1,
        issue_type=IssueType.TPB,
        series_year=2020,
        alternate_names=[],
    )
    candidate = _candidate(provider, "Henchgirl (Expanded Edition) (2020)").model_copy(
        update={
            "parsed": DirectParsedCandidate(
                series_title="Henchgirl (Expanded Edition",
                issue_numbers=[],
                year=2020,
                format=None,
            )
        }
    )
    _Client.responses = {provider.provider_identity: [candidate]}

    outcome = await search_direct_issue_target(
        target,
        [provider],
        client_factory=_factory,
    )

    assert len(outcome.matched) == 1
    assert outcome.rejected == ()
    assert outcome.matched[0].validation.series_similarity == 1.0
    assert outcome.matched[0].validation.match_type == "alternate"


async def test_collection_target_rejects_expanded_edition_for_different_series() -> None:
    _reset()
    provider = _provider("pullbox.getcomics", 10)
    target = replace(
        _target(),
        series_title="Henchgirl",
        issue_number=1,
        issue_type=IssueType.TPB,
        series_year=2020,
        alternate_names=[],
    )
    candidate = _candidate(
        provider,
        "Different Henchgirl (Expanded Edition) (2020)",
    ).model_copy(
        update={
            "parsed": DirectParsedCandidate(
                series_title="Different Henchgirl (Expanded Edition",
                issue_numbers=[],
                year=2020,
                format=None,
            )
        }
    )
    _Client.responses = {provider.provider_identity: [candidate]}

    outcome = await search_direct_issue_target(
        target,
        [provider],
        client_factory=_factory,
    )

    assert outcome.matched == ()
    assert len(outcome.rejected) == 1


async def test_loader_decrypts_only_usable_provider_operations(db_session: AsyncSession) -> None:
    secret_provider = MagicMock()
    secret_provider.secret_key.return_value = "direct-search-coordinator-test-secret"
    _get_fernet.cache_clear()
    with patch("pullbox.core.config_file.get_config_provider", return_value=secret_provider):
        healthy = DirectProviderConfig(
            provider_id="pullbox.annas_archive",
            display_name="Anna's Archive",
            endpoint="http://annas-provider:8780",
            enabled=True,
            priority=10,
            state=DirectProviderState.HEALTHY,
            negotiated_protocol=DIRECT_PROVIDER_PROTOCOL_V1,
            trust_level=DirectProviderTrustLevel.VERIFIED_PULLBOX,
            configuration_metadata={
                "allow_private_http": True,
                "public_values": {"domain": "https://annas-archive.gd"},
                "configured_secret_fields": ["member_secret_key"],
            },
            manifest_snapshot={"source_domains": ["annas-archive.gd"]},
        )
        write_provider_bearer_token(healthy, "provider-bearer-token-with-enough-length")
        update_provider_configuration_secrets(
            healthy,
            {"member_secret_key": "member-secret"},
        )
        healthy.state = DirectProviderState.HEALTHY
        disabled = DirectProviderConfig(
            provider_id="community.disabled",
            display_name="Disabled",
            endpoint="https://disabled.example",
            enabled=False,
            priority=1,
            state=DirectProviderState.DISABLED,
            trust_level=DirectProviderTrustLevel.CUSTOM,
        )
        db_session.add_all([disabled, healthy])
        await db_session.flush()

        providers = await load_direct_search_providers(db_session)

    _get_fernet.cache_clear()
    assert len(providers) == 1
    assert providers[0].provider_identity == "pullbox.annas_archive"
    assert providers[0].provider_config == {"domain": "https://annas-archive.gd"}
    assert providers[0].source_credentials == {"member_secret_key": "member-secret"}
    assert "member-secret" not in repr(providers[0])


async def test_loader_recovers_provider_after_quota_window_expires(
    db_session: AsyncSession,
) -> None:
    secret_provider = MagicMock()
    secret_provider.secret_key.return_value = "direct-search-coordinator-test-secret"
    _get_fernet.cache_clear()
    with patch("pullbox.core.config_file.get_config_provider", return_value=secret_provider):
        provider = DirectProviderConfig(
            provider_id="pullbox.annas_archive",
            display_name="Anna's Archive",
            endpoint="http://annas-provider:8780",
            enabled=True,
            priority=10,
            state=DirectProviderState.RATE_LIMITED,
            negotiated_protocol=DIRECT_PROVIDER_PROTOCOL_V1,
            trust_level=DirectProviderTrustLevel.VERIFIED_PULLBOX,
            configuration_metadata={
                "quota_status": {
                    "remaining": 0,
                    "limit": 25,
                    "window_seconds": 60,
                    "reset_at": "2020-01-01T00:01:00+00:00",
                    "observed_at": "2020-01-01T00:00:00+00:00",
                },
            },
            manifest_snapshot={"capabilities": {"quota": True}},
        )
        write_provider_bearer_token(provider, "provider-bearer-token-with-enough-length")
        db_session.add(provider)
        await db_session.flush()

        providers = await load_direct_search_providers(db_session)

    _get_fernet.cache_clear()
    assert len(providers) == 1
    assert provider.state is DirectProviderState.DEGRADED
    assert provider.configuration_metadata.get("quota_status") is None


async def test_persisted_discovery_is_restart_safe_and_contains_no_urls_or_secrets(
    db_session: AsyncSession,
) -> None:
    series = Series(
        comicvine_id=700_001,
        title="Absolute Superman",
        sort_title="Absolute Superman",
        year_start=2025,
        status=SeriesStatus.CONTINUING,
        series_type=SeriesType.STANDARD,
        monitored=True,
        issue_count=1,
    )
    db_session.add(series)
    await db_session.flush()
    issue = Issue(
        id=17,
        series_id=series.id,
        comicvine_id=700_017,
        issue_number=9,
        issue_type=IssueType.ISSUE,
        status=IssueStatus.WANTED,
    )
    provider_row = DirectProviderConfig(
        id=10,
        provider_id="pullbox.getcomics",
        display_name="GetComics",
        endpoint="https://provider.example",
        enabled=True,
        priority=10,
        state=DirectProviderState.HEALTHY,
        trust_level=DirectProviderTrustLevel.VERIFIED_PULLBOX,
    )
    db_session.add_all([issue, provider_row])
    await db_session.flush()

    provider = _provider("pullbox.getcomics", 10)
    candidate = _candidate(provider, "Absolute Superman 009 (2025)").model_copy(
        update={
            "source_reference": "https://pullbox.getcomics.example/book?token=signed-secret",
            "content_fingerprint": f"md5:{'5' * 32}",
            "provenance": {"token": "provider-secret", "layout": "fixture-v1"},
        }
    )
    _reset()
    _Client.responses = {provider.provider_identity: [candidate]}
    outcome = await search_direct_issue_target(_target(), [provider], client_factory=_factory)

    discoveries = await persist_direct_search_discoveries(db_session, _target(), outcome)
    await db_session.flush()

    assert len(discoveries) == 1
    stored = await db_session.get(DirectAcquisitionAttempt, discoveries[0].attempt_id)
    assert stored is not None
    serialized = json.dumps(stored.candidate_snapshot, sort_keys=True)
    assert "https://" not in serialized
    assert "signed-secret" not in serialized
    assert "provider-secret" not in serialized
    assert f"md5:{'5' * 32}" not in serialized
    assert stored.provider_candidate_id == candidate.provider_candidate_id
    assert stored.state.value == "discovered"
    assert stored.requested_coverage["issue_numbers"] == ["9"]
    assert stored.requested_coverage["volume"] is None


async def test_fingerprint_alternates_are_persisted_but_only_primary_is_visible(
    db_session: AsyncSession,
) -> None:
    series = Series(
        comicvine_id=700_101,
        title="Absolute Superman",
        sort_title="Absolute Superman",
        year_start=2025,
        status=SeriesStatus.CONTINUING,
        series_type=SeriesType.STANDARD,
        monitored=True,
        issue_count=1,
    )
    db_session.add(series)
    await db_session.flush()
    db_session.add(
        Issue(
            id=17,
            series_id=series.id,
            comicvine_id=700_117,
            issue_number=9,
            issue_type=IssueType.ISSUE,
            status=IssueStatus.WANTED,
        )
    )
    for provider_id, identity in (
        (10, "pullbox.libgen"),
        (20, "pullbox.annas_archive"),
        (30, "pullbox.getcomics"),
    ):
        db_session.add(
            DirectProviderConfig(
                id=provider_id,
                provider_id=identity,
                display_name=identity,
                endpoint=f"https://provider-{provider_id}.example",
                enabled=True,
                priority=provider_id,
                state=DirectProviderState.HEALTHY,
                trust_level=DirectProviderTrustLevel.VERIFIED_PULLBOX,
            )
        )
    await db_session.flush()

    primary_provider = _provider("pullbox.libgen", 10)
    alternate_provider = _provider("pullbox.annas_archive", 20)
    final_provider = _provider("pullbox.getcomics", 30)
    fingerprint = f"md5:{'4' * 32}"
    _reset()
    _Client.responses = {
        primary_provider.provider_identity: [
            _candidate(
                primary_provider,
                "Absolute Superman 009 (2025)",
                content_fingerprint=fingerprint,
            )
        ],
        alternate_provider.provider_identity: [
            _candidate(
                alternate_provider,
                "Absolute Superman 009 (2025)",
                content_fingerprint=fingerprint,
            )
        ],
        final_provider.provider_identity: [
            _candidate(
                final_provider,
                "Absolute Superman 009 (2025)",
                content_fingerprint=fingerprint,
            )
        ],
    }
    outcome = await search_direct_issue_target(
        _target(),
        [primary_provider, alternate_provider, final_provider],
        client_factory=_factory,
    )

    discoveries = await persist_direct_search_discoveries(db_session, _target(), outcome)

    assert len(discoveries) == 3
    assert [discovery.visible for discovery in discoveries] == [True, False, False]
    assert [discovery.result.provider.provider_identity for discovery in discoveries] == [
        "pullbox.libgen",
        "pullbox.annas_archive",
        "pullbox.getcomics",
    ]
    primary = await db_session.get(DirectAcquisitionAttempt, discoveries[0].attempt_id)
    alternate = await db_session.get(DirectAcquisitionAttempt, discoveries[1].attempt_id)
    final = await db_session.get(DirectAcquisitionAttempt, discoveries[2].attempt_id)
    assert primary is not None
    assert alternate is not None
    assert final is not None
    assert primary.candidate_snapshot["visible"] is True
    assert primary.candidate_snapshot["alternate_attempt_ids"] == [alternate.id, final.id]
    assert alternate.candidate_snapshot["visible"] is False
    assert alternate.candidate_snapshot["primary_attempt_id"] == primary.id
    assert alternate.candidate_snapshot["alternate_attempt_ids"] == [final.id]
    assert final.candidate_snapshot["visible"] is False
    assert final.candidate_snapshot["primary_attempt_id"] == primary.id
    assert final.candidate_snapshot["alternate_attempt_ids"] == []
    assert fingerprint not in json.dumps(primary.candidate_snapshot, sort_keys=True)
