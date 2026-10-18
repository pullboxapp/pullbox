"""Resolver deadlines preserve request identity, policy, and cancellation boundaries."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from pullbox.providers.direct.client import DirectProviderClientError
from pullbox.providers.direct.contract import DirectResolveRequest, DirectResolverProfile
from pullbox.services.direct_resolver_budget import ProviderResolverBudget, resolver_retry_allowed


async def test_budget_clips_only_ephemeral_timeout_and_deadline() -> None:
    now = datetime(2026, 10, 18, tzinfo=UTC)
    deadline = now + timedelta(seconds=30)
    profile = DirectResolverProfile(
        endpoint="http://resolver:8191",
        mode="trawl_scrape",
        timeout_seconds=60,
        max_concurrency=1,
        declared_domains=["source.example"],
        authentication_headers={"Authorization": "private-resolver-token"},
    )
    request = DirectResolveRequest(
        protocol_version="direct-download-provider/v1",
        request_id=uuid4(),
        deadline=deadline,
        resolver_profile=profile,
        provider_candidate_id="candidate-1",
        source_credentials={"token": "private-source-token"},
        provider_config={"locale": "en"},
    )
    seen: list[DirectResolveRequest] = []

    async def invoke(value: DirectResolveRequest) -> str:
        seen.append(value)
        return "resolved"

    budget = ProviderResolverBudget(deadline, now=lambda: now)
    assert await budget.run(request, invoke, remaining_resolvers=3) == "resolved"
    bounded = seen[0]
    assert bounded.request_id == request.request_id
    assert bounded.provider_candidate_id == request.provider_candidate_id
    assert bounded.source_credentials == request.source_credentials
    assert bounded.provider_config == request.provider_config
    assert bounded.resolver_profile is not None
    assert 9 < bounded.resolver_profile.timeout_seconds <= 10
    assert bounded.deadline < deadline
    assert bounded.resolver_profile.model_dump(exclude={"timeout_seconds"}) == profile.model_dump(
        exclude={"timeout_seconds"}
    )
    assert profile.timeout_seconds == 60
    assert request.deadline == deadline

    # A late or exhausted operation must not begin another request or reset its deadline.
    now = deadline + timedelta(seconds=1)
    with pytest.raises(DirectProviderClientError, match="deadline expired"):
        await budget.run(request, invoke, remaining_resolvers=2)
    assert len(seen) == 1


async def test_shorter_operator_timeout_remains_a_ceiling() -> None:
    now = datetime.now(UTC)
    request = DirectResolveRequest(
        protocol_version="direct-download-provider/v1",
        request_id=uuid4(),
        deadline=now + timedelta(seconds=30),
        provider_candidate_id="candidate-1",
        resolver_profile=DirectResolverProfile(
            endpoint="http://resolver:8191",
            timeout_seconds=2,
            max_concurrency=1,
            declared_domains=["source.example"],
        ),
    )

    async def invoke(value: DirectResolveRequest) -> DirectResolveRequest:
        return value

    bounded = await ProviderResolverBudget(request.deadline, now=lambda: now).run(
        request,
        invoke,
        remaining_resolvers=3,
    )
    assert bounded.resolver_profile.timeout_seconds == 2


@pytest.mark.parametrize("using_resolver", [False, True])
@pytest.mark.parametrize(
    "code",
    [
        "provider_authentication_failed",
        "source_quota_limited",
        "source_unavailable",
        "provider_malformed_response",
        "candidate_not_found",
        "provider_unavailable",
    ],
)
def test_unrelated_provider_failures_do_not_trigger_resolvers(
    code: str, using_resolver: bool
) -> None:
    assert not resolver_retry_allowed(
        DirectProviderClientError(code, "Failure", retryable=True),
        using_resolver=using_resolver,
    )
