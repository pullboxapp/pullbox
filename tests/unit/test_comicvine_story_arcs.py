"""Provider-boundary contracts for observed Comic Vine story-arc responses."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import httpx
import pytest

from pullbox.providers.metadata.comicvine import ComicVineError, ComicVineProvider
from pullbox.providers.story_arcs import MAX_STORY_ARC_MEMBERS, StoryArcMetadataProvider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
async def provider() -> AsyncIterator[ComicVineProvider]:
    client = ComicVineProvider(api_key="never-echo-this-key", rate_limit=999_999)
    try:
        yield client
    finally:
        await client.close()


def _arc(*, ids: tuple[int, ...] = (300, 100, 200)) -> dict[str, Any]:
    # Observed detail shape: the envelope counts arcs, not their issue members;
    # the misspelled issue-appearance counter can be zero for nonempty arcs.
    return {
        "id": 55766,
        "name": "Blackest Night",
        "description": "<p>Darkness &amp; light</p>",
        "publisher": {"id": 10, "name": "DC Comics"},
        "image": {"medium_url": "https://example.test/arc.jpg"},
        "site_detail_url": "https://comicvine.gamespot.com/blackest-night/4045-55766/",
        "count_of_isssue_appearances": 0,
        "issues": [
            {
                "id": issue_id,
                "name": f"Member {issue_id}",
                "api_detail_url": f"https://example.test/api/issue/4000-{issue_id}/",
                "site_detail_url": f"https://example.test/issue/4000-{issue_id}/",
            }
            for issue_id in ids
        ],
    }


def _issue(issue_id: int, number: str = "001") -> dict[str, Any]:
    return {
        "id": issue_id,
        "volume": {"id": 42, "name": "Batman"},
        "issue_number": number,
        "name": "The Court of Owls",
        "description": "<p>An issue &amp; a clue</p>",
        "cover_date": "2011-11-01",
        "store_date": "2011-09-21",
        "image": {"medium_url": "https://example.test/issue.jpg"},
        "site_detail_url": f"https://example.test/issue/4000-{issue_id}/",
    }


def _page(items: list[dict[str, Any]], *, total: int | None = None) -> dict[str, Any]:
    return {
        "status_code": 1,
        "number_of_total_results": len(items) if total is None else total,
        "number_of_page_results": len(items),
        "results": items,
    }


def _mock_request(provider: ComicVineProvider, response: Any) -> AsyncMock:
    mock = AsyncMock(return_value=response)
    provider._request = mock  # type: ignore[method-assign]
    return mock


async def test_story_arc_capability_is_optional_and_structural(provider: ComicVineProvider) -> None:
    assert isinstance(provider, StoryArcMetadataProvider)
    assert not isinstance(object(), StoryArcMetadataProvider)


async def test_search_uses_arc_name_filter_and_real_offset(provider: ComicVineProvider) -> None:
    item = _arc()
    item["count_of_isssue_appearances"] = 86
    request = _mock_request(provider, _page([item], total=41))

    results, total = await provider.search_story_arcs_page(" Blackest Night ", limit=20, offset=40)

    assert total == 41
    assert len(results) == 1
    result = results[0]
    assert result.provider_id == "55766"
    assert result.title == "Blackest Night"
    assert result.description == "Darkness & light"
    assert result.publisher == "DC Comics"
    assert result.cover_url == "https://example.test/arc.jpg"
    assert result.declared_issue_count == 86
    assert result.comicvine_url is not None
    endpoint, params = request.call_args.args
    assert endpoint == "/story_arcs/"
    assert params["filter"] == "name:Blackest,name:Night"
    assert params["offset"] == 40
    assert params["limit"] == 20
    assert "issues" not in params["field_list"].split(",")
    with pytest.raises(FrozenInstanceError):
        result.title = "changed"  # type: ignore[misc]


async def test_search_list_wrapper_and_unreliable_zero_count(provider: ComicVineProvider) -> None:
    _mock_request(provider, _page([_arc()]))
    results = await provider.search_story_arcs("Blackest Night")
    assert results[0].declared_issue_count is None


@pytest.mark.parametrize("query", ["", "   ", ":,|!"])
async def test_empty_name_search_spends_no_requests(
    provider: ComicVineProvider, query: str
) -> None:
    request = _mock_request(provider, {})
    assert await provider.search_story_arcs_page(query) == ([], 0)
    request.assert_not_awaited()


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 101}, {"limit": True}, {"offset": -1}])
async def test_search_rejects_unbounded_pagination(
    provider: ComicVineProvider, kwargs: dict[str, Any]
) -> None:
    request = _mock_request(provider, {})
    with pytest.raises(ValueError):
        await provider.search_story_arcs_page("Batman", **kwargs)
    request.assert_not_awaited()


@pytest.mark.parametrize(
    "response",
    [
        {"results": [], "number_of_total_results": True},
        {"results": {}, "number_of_total_results": 1},
        {"results": [_arc()], "number_of_total_results": 0},
        _page([_arc(), _arc()]),
        _page([{"id": True, "name": "Wrong"}]),
        _page([{"id": 1, "name": None}]),
    ],
)
async def test_search_rejects_incompatible_or_duplicate_results(
    provider: ComicVineProvider, response: dict[str, Any]
) -> None:
    _mock_request(provider, response)
    with pytest.raises(ComicVineError, match="incompatible"):
        await provider.search_story_arcs_page("Batman")


async def test_detail_preserves_response_order_and_unknown_counter(
    provider: ComicVineProvider,
) -> None:
    request = _mock_request(provider, {"results": _arc(), "number_of_total_results": 1})

    result = await provider.get_story_arc("55766")

    assert result.issue_provider_ids == ("300", "100", "200")
    assert result.order_basis == "response_order"
    assert result.membership_complete is True
    assert result.declared_issue_count is None
    assert result.warnings == ("unreliable_zero_issue_count",)
    endpoint, params = request.call_args.args
    assert endpoint == "/story_arc/4045-55766/"
    assert "issues" in params["field_list"].split(",")
    assert "count_of_isssue_appearances" in params["field_list"].split(",")
    # Detail offset/limit do not paginate nested members in the live API.
    assert "offset" not in params
    assert "limit" not in params
    request.assert_awaited_once()


async def test_detail_keeps_more_than_one_issue_page(provider: ComicVineProvider) -> None:
    ids = tuple(range(1, 151))
    item = _arc(ids=ids)
    _mock_request(provider, {"results": item, "number_of_total_results": 1})
    result = await provider.get_story_arc("55766")
    assert result.issue_provider_ids == tuple(str(value) for value in ids)
    assert result.membership_complete


@pytest.mark.parametrize("count,complete", [(3, True), (4, False), (2, False)])
async def test_positive_detail_counter_mismatch_is_explicit(
    provider: ComicVineProvider, count: int, complete: bool
) -> None:
    item = _arc()
    item["count_of_isssue_appearances"] = count
    _mock_request(provider, {"results": item})
    result = await provider.get_story_arc("55766")
    assert result.declared_issue_count == count
    assert result.membership_complete is complete
    assert result.issue_provider_ids == ("300", "100", "200")
    assert result.warnings == (() if complete else ("issue_count_mismatch",))


async def test_explicit_empty_membership_is_distinct_from_missing_field(
    provider: ComicVineProvider,
) -> None:
    _mock_request(provider, {"results": _arc(ids=())})
    result = await provider.get_story_arc("55766")
    assert result.issue_provider_ids == ()
    assert result.membership_complete


@pytest.mark.parametrize(
    "change",
    [
        {"id": 99},
        {"issues": None},
        {"issues": {}},
        {"issues": [{"id": 3}, {"id": 3}]},
        {"issues": [{"id": True}]},
        {"issues": [{"id": "../../secrets"}]},
        {"issues": ["unexpected"]},
        {"count_of_isssue_appearances": -1},
        {"publisher": []},
    ],
)
async def test_detail_rejects_invalid_members_or_identity(
    provider: ComicVineProvider, change: dict[str, Any]
) -> None:
    item = _arc()
    item.update(change)
    _mock_request(provider, {"results": item})
    with pytest.raises(ComicVineError, match="incompatible"):
        await provider.get_story_arc("55766")


async def test_detail_missing_membership_is_not_empty(provider: ComicVineProvider) -> None:
    item = _arc()
    del item["issues"]
    _mock_request(provider, {"results": item})
    with pytest.raises(ComicVineError, match="incompatible"):
        await provider.get_story_arc("55766")


@pytest.mark.parametrize("provider_id", ["0", "-1", "4000-123", "../x", "1,2", "1|2", "1e3"])
async def test_invalid_requested_identity_makes_no_request(
    provider: ComicVineProvider, provider_id: str
) -> None:
    request = _mock_request(provider, {})
    with pytest.raises(ValueError):
        await provider.get_story_arc(provider_id)
    with pytest.raises(ValueError):
        await provider.get_story_arc_issues([provider_id])
    request.assert_not_awaited()


async def test_bulk_hydration_batches_ids_and_preserves_requested_order(
    provider: ComicVineProvider,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def request(endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((endpoint, params))
        ids = params["filter"].removeprefix("id:").split("|")
        return _page([_issue(int(value)) for value in reversed(ids)])

    provider._request = AsyncMock(side_effect=request)  # type: ignore[method-assign]
    requested_ids = [str(value) for value in range(1, 103)]
    results = await provider.get_story_arc_issues(requested_ids)

    assert [result.provider_id for result in results] == requested_ids
    assert all(result.series_provider_id == "42" for result in results)
    assert [len(call[1]["filter"].removeprefix("id:").split("|")) for call in calls] == [100, 2]
    assert all(call[0] == "/issues/" for call in calls)
    assert all("volume" in call[1]["field_list"].split(",") for call in calls)


async def test_bulk_hydration_handles_short_pages_without_skipping_ids(
    provider: ComicVineProvider,
) -> None:
    request = AsyncMock(side_effect=[_page([_issue(2)], total=2), _page([_issue(1)], total=2)])
    provider._request = request  # type: ignore[method-assign]
    results = await provider.get_story_arc_issues(["1", "2"])
    assert [result.provider_id for result in results] == ["1", "2"]
    assert [call.args[1]["offset"] for call in request.call_args_list] == [0, 1]


@pytest.mark.parametrize(
    "number,expected", [("001", "1"), ("1AU", "1AU"), ("½", "0.5"), ("1e86", "1" + "0" * 86)]
)
async def test_bulk_hydration_preserves_exact_issue_number_semantics(
    provider: ComicVineProvider, number: str, expected: str
) -> None:
    _mock_request(provider, _page([_issue(1, number)]))
    result = (await provider.get_story_arc_issues(["1"]))[0]
    assert result.issue_number_text == expected
    assert result.description == "An issue & a clue"
    assert result.release_date == "2011-11-01"
    assert result.store_date == "2011-09-21"


@pytest.mark.parametrize(
    "change",
    [
        {"volume": None},
        {"volume": {"id": True}},
        {"issue_number": None},
        {"issue_number": "Annual unknown"},
        {"issue_number": "NaN"},
        {"issue_number": 1.0},
        {"id": 999},
    ],
)
async def test_bulk_hydration_rejects_unusable_canonical_identity(
    provider: ComicVineProvider, change: dict[str, Any]
) -> None:
    item = _issue(1)
    item.update(change)
    _mock_request(provider, _page([item]))
    with pytest.raises(ComicVineError, match="incompatible"):
        await provider.get_story_arc_issues(["1"])


@pytest.mark.parametrize(
    "pages",
    [
        [_page([_issue(1)], total=1)],
        [_page([_issue(1), _issue(1)], total=2)],
        [_page([_issue(1)], total=2), _page([_issue(1)], total=2)],
        [_page([_issue(1)], total=2), _page([], total=2)],
        [_page([_issue(1)], total=2), _page([_issue(2)], total=3)],
    ],
)
async def test_bulk_hydration_never_returns_partial_or_duplicate_sets(
    provider: ComicVineProvider, pages: list[dict[str, Any]]
) -> None:
    request = AsyncMock(side_effect=pages)
    provider._request = request  # type: ignore[method-assign]
    with pytest.raises(ComicVineError, match="incompatible"):
        await provider.get_story_arc_issues(["1", "2"])
    assert request.await_count <= len(pages)


async def test_empty_hydration_spends_no_requests(provider: ComicVineProvider) -> None:
    request = _mock_request(provider, {})
    assert await provider.get_story_arc_issues([]) == []
    request.assert_not_awaited()


async def test_duplicate_requested_members_are_rejected_before_hydration(
    provider: ComicVineProvider,
) -> None:
    request = _mock_request(provider, {})
    with pytest.raises(ValueError):
        await provider.get_story_arc_issues(["1", "1"])
    request.assert_not_awaited()


async def test_oversized_member_arrays_and_hydration_fail_instead_of_truncate(
    provider: ComicVineProvider,
) -> None:
    ids = tuple(range(1, MAX_STORY_ARC_MEMBERS + 2))
    request = _mock_request(provider, {"results": _arc(ids=ids)})
    with pytest.raises(ComicVineError, match="incompatible"):
        await provider.get_story_arc("55766")
    request.reset_mock()
    with pytest.raises(ValueError):
        await provider.get_story_arc_issues([str(value) for value in ids])
    request.assert_not_awaited()


async def test_hydration_pagination_has_a_hard_request_bound(provider: ComicVineProvider) -> None:
    request = AsyncMock(side_effect=[_page([_issue(value)], total=100) for value in range(1, 101)])
    provider._request = request  # type: ignore[method-assign]
    with pytest.raises(ComicVineError, match="incompatible"):
        await provider.get_story_arc_issues([str(value) for value in range(1, 101)])
    assert request.await_count == 10


async def test_missing_counter_is_unknown_not_a_completeness_failure(
    provider: ComicVineProvider,
) -> None:
    item = _arc()
    del item["count_of_isssue_appearances"]
    _mock_request(provider, {"results": item})
    result = await provider.get_story_arc("55766")
    assert result.declared_issue_count is None
    assert result.membership_complete
    assert result.warnings == ()


@pytest.mark.parametrize(
    "field,value", [("number_of_page_results", 2), ("number_of_total_results", -1)]
)
async def test_hydration_rejects_untrustworthy_pagination_counts(
    provider: ComicVineProvider, field: str, value: int
) -> None:
    page = _page([_issue(1)])
    page[field] = value
    _mock_request(provider, page)
    with pytest.raises(ComicVineError, match="incompatible"):
        await provider.get_story_arc_issues(["1"])


async def test_hydration_failure_never_publishes_earlier_successful_batches(
    provider: ComicVineProvider,
) -> None:
    request = AsyncMock(
        side_effect=[
            _page([_issue(value) for value in range(1, 101)]),
            ComicVineError(107, "never-echo-this-key", retryable=True),
        ]
    )
    provider._request = request  # type: ignore[method-assign]
    with pytest.raises(ComicVineError) as error:
        await provider.get_story_arc_issues([str(value) for value in range(1, 102)])
    assert error.value.status_code == 107
    assert error.value.retryable
    assert "never-echo-this-key" not in str(error.value)
    assert request.await_count == 2


async def test_real_request_plumbing_keeps_limiter_and_hides_key(
    provider: ComicVineProvider,
) -> None:
    coordinator = AsyncMock()
    provider._rate_coordinator = coordinator
    response = httpx.Response(
        200,
        json={"status_code": 1, "results": _arc()},
        request=httpx.Request("GET", "https://example.test/"),
    )
    provider._client.get = AsyncMock(return_value=response)  # type: ignore[method-assign]
    result = await provider.get_story_arc("55766")
    assert result.membership_complete
    coordinator.acquire.assert_awaited_once_with(
        "story_arc",
        requests_per_second=1,
    )
    assert provider._client.get.call_args.kwargs["params"]["api_key"] == "never-echo-this-key"


@pytest.mark.parametrize("body", [[{"api_key": "never-echo-this-key"}], "private body", None])
async def test_malformed_json_envelope_is_safe_provider_error(
    provider: ComicVineProvider, body: Any
) -> None:
    response = httpx.Response(
        200,
        json=body,
        request=httpx.Request("GET", "https://example.test/"),
    )
    provider._client.get = AsyncMock(return_value=response)  # type: ignore[method-assign]
    with pytest.raises(ComicVineError) as error:
        await provider.get_story_arc("55766")
    assert "never-echo-this-key" not in str(error.value)
    assert "private body" not in str(error.value)
