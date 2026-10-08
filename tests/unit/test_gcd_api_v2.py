"""Offline fixtures use GCD beta v2 wire fields observed on 2026-10-06."""

from datetime import date

import httpx
import pytest
from pydantic import SecretStr

from pullbox.core.metadata_identity import MetadataSource
from pullbox.core.provider_cooldown import ProviderCooldown
from pullbox.providers.metadata.gcd_api_v2 import GcdApiV2Source
from pullbox.providers.metadata.sources import metadata_sources
from pullbox.schemas.metadata_sources import SeriesDiscoveryQuery, SourceCapability, SourceStatus
from pullbox.services.metadata_discovery import (
    MetadataSourceError,
    MetadataSourceRegistry,
    describe_source_policies,
)
from pullbox.services.metadata_sources import SourceRuntime, default_policy

TOKEN = "synthetic-gcd-token-not-a-credential"


async def test_server_retry_after_is_rounded_up_and_shared_without_repeating_request():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(503, headers={"Retry-After": "1.5"})

    client = source(handle)
    try:
        with pytest.raises(MetadataSourceError) as raised:
            await client.series("50494")
        assert raised.value.status is SourceStatus.UNAVAILABLE
        assert raised.value.retry_after_seconds == 2
        with pytest.raises(MetadataSourceError) as held:
            await client.series("50494")
        assert held.value.status is SourceStatus.RATE_LIMITED
        assert len(calls) == 1
    finally:
        await client.close()


def series_row(identifier=50494):
    return {
        "id": identifier,
        "name": "Badrock",
        "sort_name": "Badrock",
        "year_began": 1995,
        "year_ended": 1996,
        "language": "en",
        "publisher": {"id": 709, "name": "Image"},
        "issue_count": 2,
        "notes": "",
        "modified": "2026-01-27T16:08:22Z",
        "active_issue_ids": [765609, 2038822, 1096257, 1096289, 765610],
    }


def issue_row(identifier=765609, number="1"):
    return {
        "id": identifier,
        "series": {"id": 50494, "name": "Badrock"},
        "number": number,
        "title": "",
        "variant_of": None,
        "key_date": "1995-03-00",
        "on_sale_date": "",
        "page_count": "36.000",
        "notes": "",
        "modified": "2026-01-27T16:08:21Z",
        "cover_url": "https://images.example.test/not-approved.jpg",
    }


def source(handler):
    return GcdApiV2Source(
        SecretStr(TOKEN), transport=httpx.MockTransport(handler), cooldown=ProviderCooldown()
    )


async def test_exact_series_normalizes_real_shape_without_cover_or_crosswalk_claims():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=series_row())

    client = source(handle)
    try:
        result = await client.series("0050494")
        assert result.status is SourceStatus.OK
        row = result.data
        assert row.external_id == "50494" and row.source is MetadataSource.GCD_API_V2
        assert row.identity_namespace.value == "gcd"
        assert row.title == "Badrock" and row.year_start == 1995
        assert row.publisher == "Image" and row.issue_count == 2
        assert row.resource_url == "https://www.comics.org/series/50494/"
        assert row.image_url is None and row.cross_identities == []
        assert requests[0].url == "https://beta.comics.org/api/v2/series/50494/"
        assert requests[0].headers["authorization"] == f"Token {TOKEN}"
        assert "Pullbox/" in requests[0].headers["user-agent"]
    finally:
        await client.close()
    assert client.client.is_closed


@pytest.mark.parametrize("number,key", [("13a", "13A"), ("50-x", "50-X"), ("-1", "-1")])
async def test_exact_issue_preserves_lettered_numbers_and_does_not_invent_partial_dates(
    number, key
):
    client = source(lambda _: httpx.Response(200, json=issue_row(number=number)))
    try:
        result = await client.issue("765609")
        assert result.status is SourceStatus.OK
        assert result.data.issue_number_text == number and result.data.issue_number_key == key
        assert result.data.series_external_id == "50494"
        assert result.data.cover_date is None and result.data.store_date is None
        assert result.data.page_count == 36 and result.data.image_url is None
        assert result.data.cross_identities == []
    finally:
        await client.close()


async def test_full_dates_are_distinct_and_fractional_page_counts_stay_unknown():
    row = {
        **issue_row(),
        "key_date": "1995-03-01",
        "on_sale_date": "1995-02-15",
        "page_count": "36.5",
    }
    client = source(lambda _: httpx.Response(200, json=row))
    try:
        result = await client.issue("765609")
        assert result.data.cover_date == date(1995, 3, 1)
        assert result.data.store_date == date(1995, 2, 15)
        assert result.data.page_count is None
    finally:
        await client.close()


async def test_issue_catalog_uses_proven_base_variant_filter_and_exact_parent():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200, json={"count": 2, "next": None, "results": [issue_row(), issue_row(765610, "2")]}
        )

    client = source(handle)
    try:
        result = await client.issues("50494")
        assert result.status is SourceStatus.OK
        assert result.data.total == 2 and result.data.next_page is None
        assert [row.external_id for row in result.data.results] == ["765609", "765610"]
        assert dict(requests[0].url.params) == {
            "series": "50494",
            "variant_of": "false",
            "page_size": "100",
            "page": "1",
        }
    finally:
        await client.close()


@pytest.mark.parametrize("bad", [True, 0, -1, "../1", 1.5])
async def test_wrong_or_invalid_detail_identity_is_rejected(bad):
    client = source(lambda _: httpx.Response(200, json=series_row(bad)))
    try:
        with pytest.raises(MetadataSourceError) as raised:
            await client.series("50494")
        assert raised.value.status is SourceStatus.INCOMPATIBLE_RESPONSE
    finally:
        await client.close()


@pytest.mark.parametrize(
    "code,status",
    [
        (401, SourceStatus.AUTHENTICATION_FAILED),
        (403, SourceStatus.AUTHENTICATION_FAILED),
        (429, SourceStatus.RATE_LIMITED),
        (503, SourceStatus.UNAVAILABLE),
    ],
)
async def test_provider_failure_is_typed_and_never_contains_token_or_response_body(code, status):
    client = source(lambda _: httpx.Response(code, text=TOKEN, headers={"Retry-After": "30"}))
    try:
        with pytest.raises(MetadataSourceError) as raised:
            await client.series("50494")
        assert raised.value.status is status and TOKEN not in str(raised.value)
    finally:
        await client.close()


async def test_redirect_does_not_forward_credentials_or_follow_foreign_origin():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://foreign.example.test/"})

    client = source(handle)
    try:
        with pytest.raises(MetadataSourceError) as raised:
            await client.series("50494")
        assert raised.value.status is SourceStatus.INCOMPATIBLE_RESPONSE
        assert len(calls) == 1 and calls[0].url.host == "beta.comics.org"
    finally:
        await client.close()


async def test_beta_search_caps_broad_candidates_and_reuses_one_page_per_collection():
    requests = []
    rows = [{**series_row(i), "name": f"Badrock candidate {i}"} for i in range(1, 101)]

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "count": 3756,
                "next": "https://beta.comics.org/api/v2/series/?name=Badrock&page_size=100&page=2&year_began=1995",
                "results": rows,
            },
        )

    client = source(handle)
    try:
        first = await client.search(
            SeriesDiscoveryQuery(query="Badrock", year=1995, limit_per_source=20), 0
        )
        last = await client.search(
            SeriesDiscoveryQuery(query="Badrock", year=1995, limit_per_source=20), 80
        )
        assert len(first.results) == 20 and first.next_offset == 20
        assert [row.external_id for row in last.results] == [str(i) for i in range(81, 101)]
        assert last.total == 3756 and last.next_offset is None and last.truncated
        assert len(requests) == 1
        assert dict(requests[0].url.params) == {
            "name": "Badrock",
            "year_began": "1995",
            "page_size": "100",
            "page": "1",
        }
    finally:
        await client.close()


def test_registry_advertises_only_implemented_gcd_capabilities():
    registration = metadata_sources().get(MetadataSource.GCD_API_V2)
    assert registration is not None
    assert registration.capabilities == frozenset(
        {
            SourceCapability.SERIES_SEARCH,
            SourceCapability.SERIES_DETAILS,
            SourceCapability.ISSUE_LIST,
            SourceCapability.ISSUE_DETAILS,
            SourceCapability.STORY_ARC_SEARCH,
            SourceCapability.STORY_ARC_DETAILS,
            SourceCapability.STORY_ARC_ISSUES,
        }
    )
    descriptor = describe_source_policies(
        [default_policy(MetadataSource.GCD_API_V2).model_copy(update={"enabled": True})],
        gcd_api_enabled=True,
    )[0]
    assert descriptor.availability is SourceStatus.UNCONFIGURED


@pytest.mark.parametrize("operation", ["series", "story_arc", "story_arc_issues"])
async def test_disabled_feature_never_constructs_or_calls_gcd_transport(monkeypatch, operation):
    def unexpected(*args, **kwargs):
        pytest.fail("Disabled GCD feature executed a provider factory")

    monkeypatch.setattr("pullbox.providers.metadata.gcd_api_v2.GcdApiV2Source.__init__", unexpected)
    policy = default_policy(MetadataSource.GCD_API_V2).model_copy(
        update={"enabled": True, "credential_configured": True}
    )
    registry = MetadataSourceRegistry(
        [SourceRuntime(policy, SecretStr(TOKEN))], gcd_api_enabled=False
    )
    result = await getattr(registry, operation)(MetadataSource.GCD_API_V2, "50494")
    assert result.status is SourceStatus.FEATURE_DISABLED


@pytest.mark.parametrize(
    "updates",
    [
        {"count": None},
        {"count": True},
        {"count": 3},
        {"next": "https://foreign.example.test/issues/?page=2"},
        {"results": [issue_row(), issue_row()]},
        {"results": [issue_row(), {**issue_row(765610, "2"), "series": {"id": 9}}]},
        {"results": [issue_row(), {**issue_row(765610, "2"), "variant_of": 765609}]},
    ],
)
async def test_incomplete_duplicate_foreign_or_variant_catalog_cannot_claim_completion(updates):
    payload = {
        "count": 2,
        "next": None,
        "results": [issue_row(), issue_row(765610, "2")],
        **updates,
    }
    client = source(lambda _: httpx.Response(200, json=payload))
    try:
        with pytest.raises(MetadataSourceError) as raised:
            await client.issues("50494")
        assert raised.value.status is SourceStatus.INCOMPATIBLE_RESPONSE
    finally:
        await client.close()


@pytest.mark.parametrize(
    "body,content_type",
    [
        (b'{"id":50494,"id":765609}', "application/json"),
        (b'{"id":NaN}', "application/json"),
        (b"x" * (2 * 1024 * 1024 + 1), "application/json"),
        (b"<html>challenge</html>", "text/html"),
    ],
    ids=["duplicate-id", "non-finite-id", "oversized-body", "html-challenge"],
)
async def test_malformed_or_oversized_provider_body_is_a_safe_failure(body, content_type):
    client = source(
        lambda _: httpx.Response(200, content=body, headers={"Content-Type": content_type})
    )
    try:
        with pytest.raises(MetadataSourceError) as raised:
            await client.series("50494")
        assert raised.value.status is SourceStatus.INCOMPATIBLE_RESPONSE
    finally:
        await client.close()


async def test_rate_limit_is_shared_without_an_automatic_retry():
    calls = []
    cooldown = ProviderCooldown()

    def handle(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "60"})

    for _ in range(2):
        client = GcdApiV2Source(
            SecretStr(TOKEN), transport=httpx.MockTransport(handle), cooldown=cooldown
        )
        try:
            with pytest.raises(MetadataSourceError) as raised:
                await client.issue("765609")
            assert raised.value.status is SourceStatus.RATE_LIMITED
            assert raised.value.retry_after_seconds > 0
        finally:
            await client.close()
    assert len(calls) == 1


async def test_timeout_is_not_reported_as_an_empty_catalog():
    def handle(request):
        raise httpx.ReadTimeout("synthetic timeout")

    client = source(handle)
    try:
        with pytest.raises(MetadataSourceError) as raised:
            await client.issues("50494")
        assert raised.value.status is SourceStatus.TIMEOUT
    finally:
        await client.close()
