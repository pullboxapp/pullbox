"""Synthetic fixtures follow Metron serializers at 7cd77f3, observed 2026-09-28."""

import asyncio
import time
from itertools import pairwise

import httpx
import pytest
from pydantic import SecretStr

from pullbox.core.metadata_identity import MetadataEntityKind, MetadataSource
from pullbox.core.provider_cooldown import ProviderCooldown
from pullbox.providers.metadata.metron import MetronSource
from pullbox.schemas.metadata_sources import SeriesDiscoveryQuery, SourceCapability, SourceStatus
from pullbox.services.metadata_discovery import (
    MetadataSourceError,
    MetadataSourceRegistry,
    SourceRegistration,
)
from pullbox.services.metadata_sources import SourceRuntime, default_policy

TOKEN = "synthetic-metron-token-not-a-credential"
MODIFIED = "Wed, 16 Sep 2026 12:00:00 GMT"


def series_row(identifier=1):
    return {
        "id": identifier,
        "series": f"Fixture {identifier} (2024)",
        "year_began": 2024,
        "year_end": None,
        "volume": 1,
        "issue_count": 3,
        "publisher": {"id": 5, "name": "Fixture Press"},
        "series_type": {"id": 13, "name": "Single Issue"},
        "cv_id": 9000 + identifier,
        "gcd_id": 8000 + identifier,
        "modified": "2026-09-16T12:00:00Z",
    }


def issue_row(identifier=1, number="50-x"):
    return {
        "id": identifier,
        "series": {"id": 8, "name": "Fixture", "volume": 1, "year_began": 2024},
        "number": number,
        "title": "A title",
        "cover_date": "2024-01-01",
        "store_date": None,
        "image": "https://static.metron.cloud/media/issue/fixture.jpg",
        "modified": "2026-09-16T12:00:00Z",
    }


def envelope(rows, *, count=None, next_url=None):
    return {
        "count": len(rows) if count is None else count,
        "results": rows,
        "next": next_url,
        "previous": None,
    }


def source(handler, *, cooldown=None, minimum_interval=0):
    return MetronSource(
        SecretStr(TOKEN),
        transport=httpx.MockTransport(handler),
        cooldown=cooldown or ProviderCooldown(),
        minimum_interval=minimum_interval,
    )


async def test_search_maps_real_list_shape_and_keeps_exact_crosswalks():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=envelope([series_row()]))

    client = source(handle)
    try:
        result = await client.search(SeriesDiscoveryQuery(query="Fixture", year=2024), 0)
        assert len(result.results) == 1
        row = result.results[0]
        assert row.title == "Fixture 1" and row.year_start == 2024
        assert row.publisher == "Fixture Press" and row.external_id == "1"
        assert row.identity_namespace == "metron" and row.source == "metron_api"
        assert {(i.namespace.value, i.external_id) for i in row.cross_identities} == {
            ("comicvine", "9001"),
            ("gcd", "8001"),
        }
        assert row.resource_url is None and row.image_url is None
        assert requests[0].headers["authorization"] == f"Bearer {TOKEN}"
        assert requests[0].url.host == "metron.cloud" and requests[0].url.scheme == "https"
        assert dict(requests[0].url.params) == {
            "name": "Fixture",
            "year_began": "2024",
            "page": "1",
        }
        assert "Pullbox/" in requests[0].headers["user-agent"]
    finally:
        await client.close()


async def test_search_slices_fixed_hundred_row_pages_without_losing_rows():
    seen = []

    def handle(request):
        page = int(request.url.params["page"])
        seen.append(page)
        rows = [series_row(i) for i in range(1, 131)][(page - 1) * 100 : page * 100]
        return httpx.Response(
            200,
            json=envelope(
                rows,
                count=130,
                next_url="https://metron.cloud/api/series/?name=Fixture&page=2"
                if page == 1
                else None,
            ),
        )

    client = source(handle)
    try:
        result = await client.search(SeriesDiscoveryQuery(query="Fixture", limit_per_source=30), 90)
        assert [int(row.external_id) for row in result.results] == list(range(91, 121))
        assert result.total == 130 and result.next_offset == 120 and seen == [1, 2]
    finally:
        await client.close()


async def test_full_collection_walks_actual_metron_pages_without_repeating_network_pages():
    seen, clients = [], []

    def handle(request):
        page = int(request.url.params["page"])
        seen.append(page)
        rows = [series_row(i) for i in range(1, 131)][(page - 1) * 100 : page * 100]
        return httpx.Response(
            200,
            json=envelope(
                rows,
                count=130,
                next_url="https://metron.cloud/api/series/?name=Fixture&page=2"
                if page == 1
                else None,
            ),
        )

    def build(_):
        client = source(handle)
        clients.append(client)
        return client

    slug = MetadataSource.METRON_API
    policy = default_policy(slug).model_copy(update={"enabled": True})
    registry = MetadataSourceRegistry(
        [SourceRuntime(policy)],
        factories={slug: SourceRegistration(frozenset([SourceCapability.SERIES_SEARCH]), build)},
    )
    result = await registry.discover_all(
        SeriesDiscoveryQuery(query="Fixture", limit_per_source=100)
    )
    assert [int(item.external_id) for item in result.results] == list(range(1, 131))
    assert seen == [1, 2]
    assert len(clients) == 1
    assert result.sources[0].total == 130
    assert result.sources[0].status is SourceStatus.OK
    assert result.sources[0].next_offset is None and not result.sources[0].truncated
    assert all(client.client.is_closed for client in clients)


@pytest.mark.parametrize("bad", [True, 0, -1, "4050-12", 1.5])
async def test_malformed_search_identity_is_rejected_not_silently_accepted(bad):
    client = source(
        lambda request: httpx.Response(
            200, json=envelope([{**series_row(), "id": bad}, series_row(9)])
        )
    )
    try:
        result = await client.search(SeriesDiscoveryQuery(query="Fixture"), 0)
        assert result.rejected_results == 1
        assert [row.external_id for row in result.results] == ["9"]
    finally:
        await client.close()


async def test_detail_normalization_and_conditional_status():
    seen = []

    def handle(request):
        seen.append(request)
        if "if-modified-since" in request.headers:
            return httpx.Response(304)
        return httpx.Response(
            200,
            headers={"Last-Modified": MODIFIED},
            json={
                **series_row(8),
                "name": "Fixture (Special)",
                "sort_name": "Fixture Special",
                "desc": "<p>Good</p><script>bad()</script>",
                "language": "en",
                "status": "Ongoing",
                "resource_url": "https://metron.cloud/series/fixture-2024/",
            },
        )

    client = source(handle)
    try:
        result = await client.series("0008")
        assert result.status == SourceStatus.OK and result.data.title == "Fixture (Special)"
        assert result.data.status == "continuing"
        assert result.data.description == "<p>Good</p>" and result.validator == MODIFIED
        unchanged = await client.series("8", validator=result.validator)
        assert unchanged.status == SourceStatus.NOT_MODIFIED and unchanged.data is None
        assert seen[-1].headers["if-modified-since"] == MODIFIED
        assert seen[-1].url.path == "/api/series/8/"
    finally:
        await client.close()


@pytest.mark.parametrize(
    "status,expected",
    [
        ("Ongoing", "continuing"),
        ("Completed", "ended"),
        ("Cancelled", "ended"),
        ("Hiatus", "unknown"),
    ],
)
async def test_series_lifecycle_is_normalized_for_library_adoption(status, expected):
    client = source(
        lambda request: httpx.Response(
            200, json={**series_row(8), "name": "Fixture", "status": status}
        )
    )
    try:
        assert (await client.series("8")).data.status == expected
    finally:
        await client.close()


@pytest.mark.parametrize(
    "type_id,expected",
    [
        (5, "one_shot"),
        (6, "annual"),
        (8, "hardcover"),
        (9, "graphic_novel"),
        (10, "tpb"),
        (11, "standard"),
        (12, "standard"),
        (13, "standard"),
        (14, "omnibus"),
        (999, None),
    ],
)
async def test_series_format_is_normalized_for_library_adoption(type_id, expected):
    client = source(
        lambda request: httpx.Response(
            200,
            json={
                **series_row(8),
                "name": "Fixture",
                "series_type": {"id": type_id, "name": "Provider format"},
            },
        )
    )
    try:
        assert (await client.series("8")).data.series_type == expected
    finally:
        await client.close()


@pytest.mark.parametrize(
    "number,key", [("50-x", "50-X"), ("13a", "13A"), ("-1", "-1"), ("0.5", "0.5"), ("Annual", None)]
)
async def test_issue_numbers_page_field_and_issue_crosswalks(number, key):
    client = source(
        lambda request: httpx.Response(
            200, json={**issue_row(7, number), "page": 48, "cv_id": 901, "gcd_id": 902}
        )
    )
    try:
        result = await client.issue("7")
        assert result.status == SourceStatus.OK
        row = result.data
        assert row.issue_number_text == number and row.issue_number_key == key
        assert row.series_external_id == "8" and row.page_count == 48
        assert all(
            identity.entity_kind == MetadataEntityKind.ISSUE for identity in row.cross_identities
        )
        assert row.cover_date.isoformat() == "2024-01-01"
    finally:
        await client.close()


async def test_story_arc_search_details_and_members_are_not_claimed_as_reading_order():
    def handle(request):
        if request.url.path.endswith("issue_list/"):
            return httpx.Response(200, json=envelope([issue_row()]))
        row = {
            "id": 4,
            "name": "Synthetic arc",
            "cv_id": 92,
            "gcd_id": None,
            "modified": "2026-09-16T12:00:00Z",
        }
        return httpx.Response(200, json=envelope([row]) if request.url.path == "/api/arc/" else row)

    client = source(handle)
    try:
        found = await client.story_arcs("Synthetic")
        assert found.results[0].external_id == "4"
        detail = await client.story_arc("4")
        assert detail.data.cross_identities[0].entity_kind == MetadataEntityKind.STORY_ARC
        members = await client.issues("4", kind=MetadataEntityKind.STORY_ARC)
        assert members.data.results[0].series_external_id == "8"
        assert members.data.order_is_reading_order is False
    finally:
        await client.close()


async def test_series_issue_list_rejects_wrong_parent():
    client = source(lambda request: httpx.Response(200, json=envelope([issue_row()])))
    try:
        with pytest.raises(MetadataSourceError) as error:
            await client.issues("9")
        assert error.value.status == SourceStatus.INCOMPATIBLE_RESPONSE
    finally:
        await client.close()


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, SourceStatus.AUTHENTICATION_FAILED),
        (403, SourceStatus.AUTHENTICATION_FAILED),
        (404, SourceStatus.NOT_FOUND),
        (500, SourceStatus.UNAVAILABLE),
        (302, SourceStatus.INCOMPATIBLE_RESPONSE),
    ],
)
async def test_statuses_are_typed_and_raw_errors_do_not_leak(status, expected, caplog):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(
            status, text=TOKEN, headers={"Location": "https://elsewhere.invalid/steal"}
        )

    client = source(handle)
    try:
        if status == 404:
            assert (await client.issue("7")).status == expected
        else:
            with pytest.raises(MetadataSourceError) as error:
                await client.issue("7")
            assert error.value.status == expected and TOKEN not in str(error.value)
        assert len(seen) == (2 if status == 500 else 1)
        assert TOKEN not in caplog.text
        assert all(request.url.host == "metron.cloud" for request in seen)
    finally:
        await client.close()


@pytest.mark.parametrize(
    "bad",
    [
        "https://evil.invalid/api/series/?page=2",
        "https://metron.cloud/api/issue/?page=2",
        "https://metron.cloud/api/series/?page=1",
        "https://metron.cloud/api/series/?page=2&name=Other",
    ],
)
async def test_untrusted_pagination_cannot_change_origin_endpoint_or_query(bad):
    client = source(
        lambda request: httpx.Response(
            200, json=envelope([series_row(i) for i in range(1, 101)], count=101, next_url=bad)
        )
    )
    try:
        with pytest.raises(MetadataSourceError) as error:
            await client.search(SeriesDiscoveryQuery(query="Fixture"), 0)
        assert error.value.status == SourceStatus.INCOMPATIBLE_RESPONSE
    finally:
        await client.close()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"count": True, "results": [], "next": None},
        {"count": 2, "results": [], "next": None},
        {"count": 1, "results": "wrong", "next": None},
    ],
)
async def test_invalid_success_envelope_is_not_an_empty_success(payload):
    client = source(lambda request: httpx.Response(200, json=payload))
    try:
        with pytest.raises(MetadataSourceError) as error:
            await client.search(SeriesDiscoveryQuery(query="Fixture"), 0)
        assert error.value.status == SourceStatus.INCOMPATIBLE_RESPONSE
    finally:
        await client.close()


async def test_shared_cooldown_honors_retry_after_and_sustained_headers():
    cooldown = ProviderCooldown()
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(
            429,
            headers={
                "Retry-After": "45",
                "X-RateLimit-Sustained-Remaining": "0",
                "X-RateLimit-Sustained-Reset": str(time.time() + 120),
            },
            text=TOKEN,
        )

    for _ in range(2):
        client = source(handle, cooldown=cooldown)
        try:
            with pytest.raises(MetadataSourceError) as error:
                await client.check()
            assert error.value.status == SourceStatus.RATE_LIMITED
            assert 115 <= error.value.retry_after_seconds <= 121
        finally:
            await client.close()
    assert count == 1


async def test_success_zero_remaining_prevents_next_client_request():
    cooldown = ProviderCooldown()
    first = source(
        lambda request: httpx.Response(
            200,
            json=envelope([]),
            headers={
                "X-RateLimit-Burst-Remaining": "0",
                "X-RateLimit-Burst-Reset": str(time.time() + 60),
            },
        ),
        cooldown=cooldown,
    )
    await first.check()
    await first.close()
    second = source(lambda request: pytest.fail("cooldown must prevent network"), cooldown=cooldown)
    try:
        with pytest.raises(MetadataSourceError) as error:
            await second.check()
        assert error.value.status == SourceStatus.RATE_LIMITED
    finally:
        await second.close()


async def test_cancellation_stops_owned_request_and_does_not_retry():
    entered, exited = asyncio.Event(), asyncio.Event()

    async def handle(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    client = source(handle)
    task = asyncio.create_task(client.check())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert exited.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.close()


async def test_registry_constructs_enabled_metron_and_closes_transport(monkeypatch):
    requests = []
    closed = []

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            requests.append(request)
            return httpx.Response(200, json=envelope([series_row()]))

        async def aclose(self):
            closed.append(True)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(**{**kwargs, "transport": Transport()}),
        raising=False,
    )
    policy = default_policy(MetadataSource.METRON_API).model_copy(update={"enabled": True})
    result = await MetadataSourceRegistry([SourceRuntime(policy, SecretStr(TOKEN))]).discover(
        SeriesDiscoveryQuery(query="Fixture")
    )
    assert result.sources[0].status == SourceStatus.OK
    assert result.results[0].source == MetadataSource.METRON_API
    assert len(requests) == len(closed) == 1


@pytest.mark.parametrize(
    "type_id,label",
    [
        (12, "Fixture (2024) Digital"),
        (10, "Fixture TPB (2024)"),
        (8, "Fixture HC (2024)"),
        (9, "Fixture GN (2024)"),
    ],
)
async def test_series_display_format_is_not_part_of_canonical_title(type_id, label):
    client = source(
        lambda request: httpx.Response(
            200,
            json=envelope(
                [
                    {
                        **series_row(),
                        "series": label,
                        "series_type": {"id": type_id, "name": "Format"},
                    }
                ]
            ),
        )
    )
    try:
        result = await client.search(SeriesDiscoveryQuery(query="Fixture"), 0)
        assert result.results[0].title == "Fixture"
    finally:
        await client.close()


@pytest.mark.parametrize(
    "payload",
    [
        b'{"id":7,"id":8,"series":{"id":8},"number":"1"}',
        b'{"id":7,"series":{"id":8},"number":"1","page":NaN}',
        b'{"id":7,"series":{"id":8},"number":"1","extra":Infinity}',
        b"not json",
        b"[" * 1200,
    ],
)
async def test_ambiguous_or_non_json_success_is_rejected(payload):
    client = source(
        lambda request: httpx.Response(
            200, content=payload, headers={"Content-Type": "application/json"}
        )
    )
    try:
        with pytest.raises(MetadataSourceError) as error:
            await client.issue("7")
        assert error.value.status == SourceStatus.INCOMPATIBLE_RESPONSE
    finally:
        await client.close()


async def test_duplicate_json_identity_never_selects_the_last_value():
    client = source(
        lambda request: httpx.Response(
            200,
            content=b'{"id":8,"id":7,"series":{"id":8},"number":"1"}',
            headers={"Content-Type": "application/json"},
        )
    )
    try:
        with pytest.raises(MetadataSourceError) as error:
            await client.issue("7")
        assert error.value.status == SourceStatus.INCOMPATIBLE_RESPONSE
    finally:
        await client.close()


@pytest.mark.parametrize(
    "data",
    [
        {"id": 8},
        {"cv_id": True},
        {"gcd_id": "4050-1"},
        {"number": None},
        {"cover_date": "2024-13-99"},
        {"modified": "2024-01-01T00:00:00"},
        {"page": -1},
    ],
)
async def test_detail_mismatch_and_bad_metadata_are_not_accepted(data):
    client = source(lambda request: httpx.Response(200, json={**issue_row(7), **data}))
    try:
        with pytest.raises(MetadataSourceError) as error:
            await client.issue("7")
        assert error.value.status == SourceStatus.INCOMPATIBLE_RESPONSE
    finally:
        await client.close()


@pytest.mark.parametrize(
    "value",
    [
        "http://static.metron.cloud/media/x.jpg",
        "https://metron.cloud@evil.invalid/media/x.jpg",
        "https://evil.invalid/media/x.jpg",
        "https://static.metron.cloud/media/%2e%2e/private",
        "https://static.metron.cloud/media/x.jpg?token=private",
    ],
)
async def test_untrusted_artwork_urls_are_not_exposed(value):
    client = source(
        lambda request: httpx.Response(
            200, json={**issue_row(7), "image": value, "resource_url": value}
        )
    )
    try:
        row = (await client.issue("7")).data
        assert row.image_url is None and row.resource_url is None
    finally:
        await client.close()


@pytest.mark.parametrize(
    "credential,status",
    [
        ("", SourceStatus.UNCONFIGURED),
        ("enc:private", SourceStatus.INVALID_CONFIG),
        ("private\r\nvalue", SourceStatus.INVALID_CONFIG),
        ("secret\x7f", SourceStatus.INVALID_CONFIG),
    ],
)
async def test_invalid_credentials_never_construct_http_client(credential, status, monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: pytest.fail("must not construct"))
    with pytest.raises(MetadataSourceError) as error:
        MetronSource(SecretStr(credential))
    assert error.value.status == status


async def test_invalid_identity_and_validator_are_rejected_before_network():
    client = source(lambda request: pytest.fail("must not request"))
    try:
        with pytest.raises(ValueError):
            await client.issue("../7")
        with pytest.raises(ValueError):
            await client.issue("7", validator="private\r\nAuthorization: replacement")
        with pytest.raises(ValueError):
            await client.issues("8", page=0)
        with pytest.raises(ValueError):
            await client.story_arcs(" ")
    finally:
        await client.close()


async def test_unsolicited_304_is_not_success():
    client = source(lambda request: httpx.Response(304))
    try:
        with pytest.raises(MetadataSourceError) as error:
            await client.issue("7")
        assert error.value.status == SourceStatus.INCOMPATIBLE_RESPONSE
    finally:
        await client.close()


async def test_stream_size_is_bounded_and_closed():
    closed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(40):
                yield b"x" * 65536

        async def aclose(self):
            closed.append(True)

    client = source(
        lambda request: httpx.Response(
            200, stream=Stream(), headers={"Content-Type": "application/json"}
        )
    )
    try:
        with pytest.raises(MetadataSourceError) as error:
            await client.issue("7")
        assert error.value.status == SourceStatus.INCOMPATIBLE_RESPONSE
        assert closed == [True]
    finally:
        await client.close()


async def test_transport_timeout_and_transient_retry_are_bounded(monkeypatch):
    import pullbox.providers.metadata.metron as module

    monkeypatch.setattr(module, "_REQUEST_TIMEOUT", 0.03)

    async def wait(request):
        await asyncio.Event().wait()

    client = source(wait)
    try:
        with pytest.raises(MetadataSourceError) as error:
            await client.issue("7")
        assert error.value.status == SourceStatus.TIMEOUT
    finally:
        await client.close()
    requests = []

    def fail(request):
        requests.append(request)
        raise httpx.ReadError(TOKEN)

    monkeypatch.setattr(module, "_REQUEST_TIMEOUT", 2)
    client = source(fail)
    try:
        with pytest.raises(MetadataSourceError) as error:
            await client.issue("7")
        assert error.value.status == SourceStatus.UNAVAILABLE and TOKEN not in str(error.value)
        assert len(requests) == 2
    finally:
        await client.close()


async def test_pacing_does_not_consume_the_network_timeout(monkeypatch):
    import pullbox.providers.metadata.metron as module

    monkeypatch.setattr(module, "_REQUEST_TIMEOUT", 0.02)
    cooldown = ProviderCooldown()
    cooldown.last_request_time = module.time.monotonic()
    client = source(
        lambda request: httpx.Response(200, json=issue_row(7)),
        cooldown=cooldown,
        minimum_interval=0.04,
    )
    try:
        assert (await client.issue("7")).status is SourceStatus.OK
    finally:
        await client.close()


async def test_busy_request_queue_is_not_a_provider_failure(monkeypatch):
    import pullbox.providers.metadata.metron as module

    monkeypatch.setattr(module, "_REQUEST_TIMEOUT", 0.02)
    monkeypatch.setattr(module, "_QUEUE_TIMEOUT", 0.01, raising=False)
    cooldown = ProviderCooldown()
    client = source(lambda request: pytest.fail("queue must prevent network"), cooldown=cooldown)
    try:
        async with cooldown.request_lock:
            with pytest.raises(MetadataSourceError) as error:
                await client.issue("7")
        assert error.value.status is SourceStatus.RATE_LIMITED
        assert error.value.retry_after_seconds == 3
        assert error.value.account_failure is False
        assert cooldown.remaining_seconds == 0
    finally:
        await client.close()


async def test_cancelling_a_queued_request_does_not_release_another_requests_lock():
    cooldown = ProviderCooldown()
    client = source(
        lambda request: pytest.fail("cancelled request must not run"), cooldown=cooldown
    )
    try:
        async with cooldown.request_lock:
            task = asyncio.create_task(client.issue("7"))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert cooldown.request_lock.locked()
        assert cooldown.remaining_seconds == 0
    finally:
        await client.close()


async def test_consecutive_clients_share_pacing_without_false_network_timeouts(monkeypatch):
    import pullbox.providers.metadata.metron as module

    monkeypatch.setattr(module, "_REQUEST_TIMEOUT", 0.02)
    cooldown = ProviderCooldown()
    starts = []

    def handle(request):
        starts.append(module.time.monotonic())
        return httpx.Response(200, json=issue_row(7))

    clients = [source(handle, cooldown=cooldown, minimum_interval=0.04) for _ in range(3)]
    try:
        results = await asyncio.gather(*(client.issue("7") for client in clients))
        assert all(result.status is SourceStatus.OK for result in results)
        assert len(starts) == 3
        assert all(b - a >= 0.035 for a, b in pairwise(starts))
        assert cooldown.remaining_seconds == 0
    finally:
        for client in clients:
            await client.close()


async def test_changed_total_between_search_pages_is_not_a_complete_result():
    def handle(request):
        first = request.url.params["page"] == "1"
        return httpx.Response(
            200,
            json=envelope(
                [series_row(i) for i in (range(1, 101) if first else range(101, 111))],
                count=130 if first else 110,
                next_url="https://metron.cloud/api/series/?name=Fixture&page=2" if first else None,
            ),
        )

    client = source(handle)
    try:
        with pytest.raises(MetadataSourceError) as error:
            await client.search(SeriesDiscoveryQuery(query="Fixture", limit_per_source=30), 90)
        assert error.value.status == SourceStatus.INCOMPATIBLE_RESPONSE
    finally:
        await client.close()


async def test_default_registry_disabled_and_unconfigured_do_not_construct(monkeypatch):
    from pullbox.services.metadata_discovery import describe_source_policies

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: pytest.fail("must not construct"))
    for enabled, expected in [(False, SourceStatus.DISABLED), (True, SourceStatus.UNCONFIGURED)]:
        policy = default_policy(MetadataSource.METRON_API).model_copy(update={"enabled": enabled})
        descriptor = describe_source_policies([policy], gcd_api_enabled=False)[0]
        assert descriptor.availability == expected
        registry = MetadataSourceRegistry([SourceRuntime(policy)])
        assert (await registry.check(MetadataSource.METRON_API)).status == expected


async def test_default_cooldown_survives_client_recreation():
    first = MetronSource(
        SecretStr(TOKEN),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": "60"})
        ),
    )
    try:
        with pytest.raises(MetadataSourceError):
            await first.check()
    finally:
        await first.close()
    second = MetronSource(
        SecretStr(TOKEN),
        transport=httpx.MockTransport(lambda request: pytest.fail("must not request")),
    )
    try:
        with pytest.raises(MetadataSourceError) as error:
            await second.check()
        assert error.value.status == SourceStatus.RATE_LIMITED
    finally:
        await second.close()


async def test_issue_list_parent_kind_cannot_silently_switch_endpoint():
    client = source(lambda request: pytest.fail("must not request"))
    try:
        with pytest.raises(ValueError):
            await client.issues("8", kind="series")
    finally:
        await client.close()


async def test_page_cap_is_explicitly_truncated_not_complete():
    rows = [issue_row(i) for i in range(1, 101)]
    client = source(
        lambda request: httpx.Response(
            200,
            json=envelope(
                rows,
                count=1000001,
                next_url="https://metron.cloud/api/series/8/issue_list/?page=10001",
            ),
        )
    )
    try:
        result = await client.issues("8", page=10000)
        assert result.data.next_page is None
        assert result.data.truncated is True and result.data.total == 1000001
    finally:
        await client.close()


async def test_issue_pages_are_conditional_and_preserve_membership():
    seen = []

    def handle(request):
        seen.append(request)
        if "if-modified-since" in request.headers:
            return httpx.Response(304)
        if request.url.params["page"] == "2":
            return httpx.Response(200, json=envelope([issue_row(101)], count=101))
        return httpx.Response(
            200,
            headers={"Last-Modified": MODIFIED},
            json=envelope(
                [issue_row(i) for i in range(1, 101)],
                count=101,
                next_url="https://metron.cloud/api/series/8/issue_list/?page=2",
            ),
        )

    client = source(handle)
    try:
        first = await client.issues("8")
        assert len(first.data.results) == 100 and first.data.next_page == 2
        second = await client.issues("8", page=2)
        assert second.data.results[0].external_id == "101" and second.data.next_page is None
        cached = await client.issues("8", validator=first.validator)
        assert cached.status == SourceStatus.NOT_MODIFIED and cached.data is None
        assert len(seen) == 3
    finally:
        await client.close()


async def test_transient_retry_success_and_cancellation_before_retry(monkeypatch):
    import pullbox.providers.metadata.metron as module

    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(503) if len(seen) == 1 else httpx.Response(200, json=issue_row(7))

    client = source(handle)
    try:
        assert (await client.issue("7")).data.external_id == "7"
        assert len(seen) == 2
    finally:
        await client.close()

    sleeping = asyncio.Event()

    async def blocked_sleep(delay):
        sleeping.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(module.asyncio, "sleep", blocked_sleep)
    seen.clear()
    client = source(handle)
    task = asyncio.create_task(client.issue("7"))
    try:
        await asyncio.wait_for(sleeping.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(seen) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.close()
