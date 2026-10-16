"""GCD arc wire contract, without live requests or invented reading order."""

import asyncio

import httpx
import pytest
from pydantic import SecretStr

from pullbox.core.metadata_identity import MetadataSource as Source
from pullbox.schemas.metadata_sources import SourceStatus
from pullbox.services.metadata_discovery import MetadataSourceError, MetadataSourceRegistry
from pullbox.services.metadata_sources import SourceRuntime, default_policy
from tests.unit.test_gcd_api_v2 import TOKEN, issue_row, source


def arc_row(identifier=4):
    return {
        "id": identifier,
        "name": "Civil War",
        "notes": "<script>untrusted</script>",
        "modified": "2026-10-06T12:00:00Z",
        # Story sequence values are local to the comic, not arc reading positions.
        "stories": [{"id": 30, "issue": {"id": 765609}, "sequence_number": 2}],
    }


def envelope(request, rows):
    page = int(request.url.params["page"])
    return {
        "count": len(rows),
        "next": str(request.url.copy_set_param("page", str(page + 1)))
        if len(rows) > page * 100
        else None,
        "results": rows[(page - 1) * 100 : page * 100],
    }


async def test_registry_can_read_native_gcd_arc_through_existing_feature_gate(monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=arc_row())

    monkeypatch.setattr(
        "pullbox.providers.metadata.sources.GcdApiV2Source", lambda _: source(handle)
    )
    policy = default_policy(Source.GCD_API_V2).model_copy(
        update={"enabled": True, "credential_configured": True, "revision": 1}
    )
    registry = MetadataSourceRegistry(
        [SourceRuntime(policy, SecretStr(TOKEN))], gcd_api_enabled=True
    )
    result = await registry.story_arc(Source.GCD_API_V2, "4")
    assert result.status is SourceStatus.OK
    assert result.data.external_id == "4" and result.data.identity_namespace.value == "gcd"
    assert len(calls) == 1


async def test_search_and_detail_keep_native_identity_without_treating_stories_as_issues():
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("/story-arcs/"):
            return httpx.Response(200, json=envelope(request, [arc_row(), arc_row(5)]))
        assert request.url.path == "/api/v2/story-arcs/4/"
        return httpx.Response(200, json=arc_row())

    client = source(handle)
    try:
        results = await client.story_arcs(" Civil War ")
        assert [row.external_id for row in results.results] == ["4", "5"]
        assert results.total == 2 and not results.order_is_reading_order
        assert dict(calls[0].url.params) == {"name": "Civil War", "page": "1", "page_size": "100"}
        arc = (await client.story_arc("0004")).data
        assert arc.title == "Civil War" and arc.cross_identities == [] and arc.image_url is None
        assert arc.description == "&lt;script&gt;untrusted&lt;/script&gt;"
        assert arc.issue_external_ids is None and arc.declared_issue_count is None
        assert not arc.membership_complete and len(arc.warnings) == 2
        assert arc.resource_url == "https://www.comics.org/story_arc/4/"
    finally:
        await client.close()


async def test_members_use_two_bounded_pages_instead_of_one_request_per_issue():
    rows = [issue_row(1000 + i, str(i + 1)) for i in range(103)]
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.path == "/api/v2/story-arcs/4/issues/"
        assert "variant_of" not in request.url.params and "series" not in request.url.params
        return httpx.Response(200, json=envelope(request, rows))

    client = source(handle)
    try:
        first = (await client.story_arc_issues("4")).data
        last = (await client.story_arc_issues("4", page=2)).data
        assert first.total == last.total == 103
        assert first.next_page == 2 and last.next_page is None
        assert len(first.results) == 100 and len(last.results) == 3
        assert not first.order_is_reading_order and not last.truncated
        assert len(calls) == 2
    finally:
        await client.close()


@pytest.mark.parametrize(
    "bad", ["short", "duplicate", "foreign_next", "missing_variant", "bad_variant"]
)
async def test_inconsistent_member_page_cannot_claim_completion(bad):
    row = issue_row()
    payload = {"count": 1, "next": None, "results": [row]}
    if bad == "short":
        payload["count"] = 2
    elif bad == "duplicate":
        payload.update(count=2, results=[row, row])
    elif bad == "foreign_next":
        payload["next"] = "https://foreign.example.test/?page=2"
    elif bad == "missing_variant":
        row.pop("variant_of")
    else:
        row["variant_of"] = True
    client = source(lambda _: httpx.Response(200, json=payload))
    try:
        with pytest.raises(MetadataSourceError) as failure:
            await client.story_arc_issues("4")
        assert failure.value.status is SourceStatus.INCOMPATIBLE_RESPONSE
    finally:
        await client.close()


@pytest.mark.parametrize("parent,status", [(50494, 200), (99, 404), (99, 200)])
async def test_variant_preserves_exact_cross_series_issue_but_refuses_same_series_or_missing_base(
    parent, status
):
    row = {**issue_row(), "variant_of": 88}
    base = {**issue_row(88), "series": {"id": parent}}
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("/issues/"):
            return httpx.Response(200, json=envelope(request, [row]))
        assert request.url.path == "/api/v2/issues/88/"
        return httpx.Response(status, json=base)

    client = source(handle)
    try:
        if parent == 99 and status == 200:
            result = await client.story_arc_issues("4")
            assert result.data.results[0].external_id == "765609"
            assert result.data.results[0].series_external_id == "50494"
        else:
            with pytest.raises(MetadataSourceError) as failure:
                await client.story_arc_issues("4")
            assert failure.value.status is SourceStatus.INCOMPATIBLE_RESPONSE
        assert len(calls) == 2
    finally:
        await client.close()


@pytest.mark.parametrize(
    "code,status",
    [
        (404, SourceStatus.NOT_FOUND),
        (429, SourceStatus.RATE_LIMITED),
        (503, SourceStatus.UNAVAILABLE),
    ],
)
async def test_missing_endpoint_and_provider_failures_never_become_empty_success(code, status):
    client = source(lambda _: httpx.Response(code, text=TOKEN, headers={"Retry-After": "30"}))
    try:
        if code == 404:
            result = await client.story_arc_issues("4")
            assert result.status is status and result.data is None
        else:
            with pytest.raises(MetadataSourceError) as failure:
                await client.story_arc_issues("4")
            assert failure.value.status is status and TOKEN not in str(failure.value)
    finally:
        await client.close()


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_is_typed_and_cancellation_propagates(cancel):
    def handle(_request):
        if cancel:
            raise asyncio.CancelledError
        raise httpx.ReadTimeout("synthetic timeout")

    client = source(handle)
    try:
        with pytest.raises(asyncio.CancelledError if cancel else MetadataSourceError) as failure:
            await client.story_arc_issues("4")
        if not cancel:
            assert failure.value.status is SourceStatus.TIMEOUT
    finally:
        await client.close()
